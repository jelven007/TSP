-- TSP 历史研究逻辑表：单节点可执行 DDL。
-- 不连接现有 TSP，也不迁移现有数据。生产集群使用 02_cluster.sql。
-- MergeTree 的 ORDER BY / PRIMARY KEY 不提供唯一约束。
-- segment_id 表示一个表、一个逻辑变体、一个交易日的不可变已校验版本。
-- 所有研究查询必须从固定 research_manifest 解析 segment_id。
CREATE DATABASE IF NOT EXISTS tsp_history;

-- 稳定证券身份。instrument_id 由导入控制层分配且永不复用。
-- 仅存身份；历史名称、ST、上市状态、行业和股本不从本表取得。
CREATE TABLE IF NOT EXISTS tsp_history.instrument
(
    instrument_id UInt32,
    symbol FixedString(9),
    exchange Enum8('SH' = 1, 'SZ' = 2),
    asset_type Enum8('stock' = 1, 'etf' = 2, 'index' = 3),
    created_at DateTime64(3, 'Asia/Shanghai')
)
ENGINE = MergeTree
ORDER BY instrument_id;

-- 小型历史维表；同一个有效期可以有多个“当时才知道”的修订。
-- NULL available_at 代表无法证明何时可得，不能用于严格 PIT 研究。
CREATE TABLE IF NOT EXISTS tsp_history.instrument_state
(
    segment_id UInt64,
    instrument_id UInt32,
    valid_from Date,
    valid_to Nullable(Date),
    available_at Nullable(DateTime64(3, 'Asia/Shanghai')),
    record_version UInt64,
    name String,
    is_st Nullable(UInt8),
    is_listed Nullable(UInt8),
    listing_date Nullable(Date),
    industry_id Nullable(UInt32),
    float_shares Nullable(UInt64),
    source_id UInt16,
    CONSTRAINT valid_interval CHECK isNull(valid_to) OR valid_from < assumeNotNull(valid_to)
)
ENGINE = MergeTree
ORDER BY (segment_id, instrument_id, valid_from, record_version);

-- 秒级采样事实。非逐笔，不从交易所事件时间推导采集时间。
-- 同一 capture_id 可以覆盖不同证券；去重业务键是 segment/instrument/capture。
CREATE TABLE IF NOT EXISTS tsp_history.quote_second
(
    segment_id UInt64 CODEC(Delta, ZSTD(3)),
    instrument_id UInt32 CODEC(ZSTD(3)),
    observed_at DateTime64(3, 'Asia/Shanghai') CODEC(Delta, ZSTD(3)),
    trade_date Date MATERIALIZED toDate(observed_at),
    capture_id UInt64 CODEC(Delta, ZSTD(3)),
    provider_at Nullable(DateTime64(3, 'Asia/Shanghai')),
    source_at Nullable(DateTime64(3, 'Asia/Shanghai')),
    source_time_raw String CODEC(ZSTD(3)),
    source_date_inferred UInt8,
    source_clock_basis Enum8(
        'unknown' = 0, 'received' = 1, 'source_event' = 2, 'verified_snapshot' = 3
    ),
    last_price Nullable(Decimal64(4)) CODEC(ZSTD(3)),
    prev_close Nullable(Decimal64(4)) CODEC(ZSTD(3)),
    day_open Nullable(Decimal64(4)) CODEC(ZSTD(3)),
    day_high Nullable(Decimal64(4)) CODEC(ZSTD(3)),
    day_low Nullable(Decimal64(4)) CODEC(ZSTD(3)),
    cum_volume_shares Nullable(UInt64) CODEC(ZSTD(3)),
    cum_amount_yuan Nullable(Decimal64(2)) CODEC(ZSTD(3)),
    change_ratio Nullable(Float64) CODEC(ZSTD(3)),
    turnover_ratio Nullable(Float64) CODEC(ZSTD(3)),
    bid1 Nullable(Decimal64(4)) CODEC(ZSTD(3)),
    ask1 Nullable(Decimal64(4)) CODEC(ZSTD(3)),
    bid1_shares Nullable(UInt64) CODEC(ZSTD(3)),
    ask1_shares Nullable(UInt64) CODEC(ZSTD(3)),
    session Enum8(
        'unknown' = 0, 'auction' = 1, 'continuous' = 2, 'break' = 3, 'after_close' = 4
    ),
    quality_mask UInt16,
    input_valid UInt8,
    CONSTRAINT valid_source_inference CHECK source_date_inferred IN (0, 1),
    CONSTRAINT valid_input_flag CHECK input_valid IN (0, 1)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(trade_date)
PRIMARY KEY (segment_id, instrument_id, observed_at)
ORDER BY (segment_id, instrument_id, observed_at, capture_id)
SETTINGS index_granularity = 8192;

-- 从秒级样本计算的分钟范围索引，不是权威分钟 OHLC。
-- 保存最值可排除不可能命中的分钟，但不能证明不同指标在同一秒同时命中。
-- 缺失最值按“不能排除”处理；候选必须回秒级明细精确判断。
CREATE TABLE IF NOT EXISTS tsp_history.quote_1m_envelope
(
    segment_id UInt64,
    quote_segment_id UInt64,
    instrument_id UInt32,
    minute_start DateTime64(3, 'Asia/Shanghai') CODEC(Delta, ZSTD(3)),
    trade_date Date MATERIALIZED toDate(minute_start),
    first_observed_at Nullable(DateTime64(3, 'Asia/Shanghai')),
    last_observed_at Nullable(DateTime64(3, 'Asia/Shanghai')),
    sample_count UInt32,
    valid_sample_count UInt32,
    expected_sample_count UInt32,
    max_gap_ms UInt32,
    boundary_gap UInt8,
    min_price Nullable(Decimal64(4)),
    max_price Nullable(Decimal64(4)),
    min_change_ratio Nullable(Float64),
    max_change_ratio Nullable(Float64),
    max_turnover_ratio Nullable(Float64),
    source_verified_count UInt32,
    quality_mask UInt16,
    CONSTRAINT minute_aligned CHECK minute_start = toStartOfMinute(minute_start),
    CONSTRAINT counts_ordered CHECK valid_sample_count <= sample_count
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(trade_date)
ORDER BY (segment_id, trade_date, minute_start, instrument_id)
SETTINGS index_granularity = 8192;

-- 完成的可信分钟 K；未完成当前柱留在在线状态，不在本研究表反复覆盖。
-- bar_start/end 统一为左闭右开区间，源端“结束时刻标签”需在导入时转换。
CREATE TABLE IF NOT EXISTS tsp_history.bar_1m
(
    segment_id UInt64,
    instrument_id UInt32,
    bar_start DateTime64(3, 'Asia/Shanghai') CODEC(Delta, ZSTD(3)),
    bar_end DateTime64(3, 'Asia/Shanghai'),
    trade_date Date MATERIALIZED toDate(bar_start),
    available_at Nullable(DateTime64(3, 'Asia/Shanghai')),
    bar_basis Enum8('provider' = 1, 'verified_tick' = 2),
    raw_open Nullable(Decimal64(4)),
    raw_high Nullable(Decimal64(4)),
    raw_low Nullable(Decimal64(4)),
    raw_close Nullable(Decimal64(4)),
    volume_shares Nullable(UInt64),
    amount_yuan Nullable(Decimal64(2)),
    feature_set_id UInt32,
    ma20 Nullable(Float64),
    rsi14 Nullable(Float64),
    feature_price_basis Enum8('raw' = 1, 'adjusted_asof' = 2),
    adjustment_anchor_date Nullable(Date),
    input_cutoff_at Nullable(DateTime64(3, 'Asia/Shanghai')),
    pit_eligible UInt8 DEFAULT 0,
    quality_mask UInt16,
    CONSTRAINT bar_interval CHECK bar_end = bar_start + INTERVAL 1 MINUTE,
    CONSTRAINT bar_visibility CHECK isNull(available_at) OR assumeNotNull(available_at) >= bar_end,
    CONSTRAINT bar_pit CHECK pit_eligible IN (0, 1)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(trade_date)
ORDER BY (segment_id, instrument_id, bar_start)
SETTINGS index_granularity = 8192;

-- 十年快速选股首先读此表。每个 segment 的 feature_set_id 必须一致。
-- 因子值是已验证输入算出的缓存，不用今日证券状态或最新复权锚点回填旧值。
CREATE TABLE IF NOT EXISTS tsp_history.feature_1d
(
    segment_id UInt64,
    instrument_id UInt32,
    trade_date Date,
    available_at Nullable(DateTime64(3, 'Asia/Shanghai')),
    feature_set_id UInt32,
    raw_open Nullable(Decimal64(4)),
    raw_high Nullable(Decimal64(4)),
    raw_low Nullable(Decimal64(4)),
    raw_close Nullable(Decimal64(4)),
    raw_prev_close Nullable(Decimal64(4)),
    volume_shares Nullable(UInt64),
    amount_yuan Nullable(Decimal64(2)),
    change_ratio Nullable(Float64),
    turnover_ratio Nullable(Float64),
    volume_ratio_20 Nullable(Float64),
    close_for_features Nullable(Float64),
    ma20 Nullable(Float64),
    ma60 Nullable(Float64),
    rsi14 Nullable(Float64),
    volatility20 Nullable(Float64),
    feature_price_basis Enum8('raw' = 1, 'adjusted_asof' = 2),
    adjustment_anchor_date Nullable(Date),
    industry_id_asof Nullable(UInt32),
    float_shares_asof Nullable(UInt64),
    is_st_asof Nullable(UInt8),
    is_listed_asof Nullable(UInt8),
    instrument_state_known UInt8,
    input_cutoff_at Nullable(DateTime64(3, 'Asia/Shanghai')),
    pit_eligible UInt8 DEFAULT 0,
    quality_mask UInt16,
    CONSTRAINT daily_pit CHECK pit_eligible IN (0, 1),
    CONSTRAINT daily_known CHECK instrument_state_known IN (0, 1),
    PROJECTION by_instrument
    (
        SELECT *
        ORDER BY (segment_id, instrument_id, trade_date)
    )
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(trade_date)
ORDER BY (segment_id, trade_date, instrument_id)
SETTINGS index_granularity = 8192;

-- 稀疏的、已版本化公共信号。只预计算有明确需求的定义，避免规则组合爆炸。
-- definition_id 必须唯一确定代码、参数、输入契约，不能重用 ID 修改规则。
CREATE TABLE IF NOT EXISTS tsp_history.signal_event
(
    segment_id UInt64,
    definition_id UInt32,
    instrument_id UInt32,
    event_at DateTime64(3, 'Asia/Shanghai') CODEC(Delta, ZSTD(3)),
    trade_date Date MATERIALIZED toDate(event_at),
    available_at Nullable(DateTime64(3, 'Asia/Shanghai')),
    event_id UUID,
    quote_segment_id UInt64,
    input_capture_id UInt64,
    window_start DateTime64(3, 'Asia/Shanghai'),
    window_end DateTime64(3, 'Asia/Shanghai'),
    value Nullable(Float64),
    threshold Nullable(Float64),
    pit_eligible UInt8 DEFAULT 0,
    quality_mask UInt16,
    CONSTRAINT signal_window CHECK window_start <= window_end AND window_end <= event_at,
    CONSTRAINT signal_pit CHECK pit_eligible IN (0, 1)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(trade_date)
ORDER BY (segment_id, definition_id, trade_date, event_at, instrument_id, event_id)
SETTINGS index_granularity = 8192;

-- 只镜像已发布的完整清单；事务控制、租户权限与发布状态仍由 TSP/PG 负责。
-- 同一 snapshot/table/variant/segment_date 必须唯一。CH 不负责强制该唯一性。
-- instrument_state 是小维表，segment_date 固定为 1970-01-01。
CREATE TABLE IF NOT EXISTS tsp_history.research_manifest
(
    snapshot_id UUID,
    table_name LowCardinality(String),
    variant LowCardinality(String),
    segment_date Date,
    partition_month Date MATERIALIZED toStartOfMonth(segment_date),
    segment_id UInt64,
    source_id UInt16,
    transform_version String,
    expected_rows UInt64,
    manifest_uri String,
    manifest_sha256 FixedString(64),
    validation_report_uri String,
    published_at DateTime64(3, 'Asia/Shanghai')
)
ENGINE = MergeTree
ORDER BY (snapshot_id, table_name, variant, segment_date);
