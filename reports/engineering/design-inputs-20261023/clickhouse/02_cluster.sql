-- 自建 ClickHouse 集群部署版本，字段定义来自 01_schema.sql。
-- 与 01_schema.sql 二选一，在空的 tsp_history 命名空间部署。
-- 需预先配置 tsp_analytics 集群、Keeper、shard/replica 宏和权限。
-- 每个 shard 的 internal_replication=true；研究读取禁止跳过不可用 shard。
-- 此文件不适用于 ClickHouse Cloud 的自动存储/计算架构。
-- 小维表/清单在全部节点完整复制；只有五张事实表做 instrument_id 分片。
CREATE DATABASE IF NOT EXISTS tsp_history ON CLUSTER tsp_analytics;

CREATE TABLE IF NOT EXISTS tsp_history.instrument ON CLUSTER tsp_analytics
(
    instrument_id UInt32,
    symbol FixedString(9),
    exchange Enum8('SH' = 1, 'SZ' = 2),
    asset_type Enum8('stock' = 1, 'etf' = 2, 'index' = 3),
    created_at DateTime64(3, 'Asia/Shanghai')
)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/global/tsp_history/instrument', '{shard}-{replica}')
ORDER BY instrument_id;

CREATE TABLE IF NOT EXISTS tsp_history.instrument_state ON CLUSTER tsp_analytics
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
ENGINE = ReplicatedMergeTree('/clickhouse/tables/global/tsp_history/instrument_state', '{shard}-{replica}')
ORDER BY (segment_id, instrument_id, valid_from, record_version);

CREATE TABLE IF NOT EXISTS tsp_history.quote_second_local ON CLUSTER tsp_analytics
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
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/tsp_history/quote_second_local', '{replica}')
PARTITION BY toYYYYMM(trade_date)
PRIMARY KEY (segment_id, instrument_id, observed_at)
ORDER BY (segment_id, instrument_id, observed_at, capture_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS tsp_history.quote_second ON CLUSTER tsp_analytics
AS tsp_history.quote_second_local
ENGINE = Distributed(tsp_analytics, tsp_history, quote_second_local, cityHash64(instrument_id))
SETTINGS skip_unavailable_shards = 0;

CREATE TABLE IF NOT EXISTS tsp_history.quote_1m_envelope_local ON CLUSTER tsp_analytics
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
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/tsp_history/quote_1m_envelope_local', '{replica}')
PARTITION BY toYYYYMM(trade_date)
ORDER BY (segment_id, trade_date, minute_start, instrument_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS tsp_history.quote_1m_envelope ON CLUSTER tsp_analytics
AS tsp_history.quote_1m_envelope_local
ENGINE = Distributed(tsp_analytics, tsp_history, quote_1m_envelope_local, cityHash64(instrument_id))
SETTINGS skip_unavailable_shards = 0;

CREATE TABLE IF NOT EXISTS tsp_history.bar_1m_local ON CLUSTER tsp_analytics
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
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/tsp_history/bar_1m_local', '{replica}')
PARTITION BY toYYYYMM(trade_date)
ORDER BY (segment_id, instrument_id, bar_start)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS tsp_history.bar_1m ON CLUSTER tsp_analytics
AS tsp_history.bar_1m_local
ENGINE = Distributed(tsp_analytics, tsp_history, bar_1m_local, cityHash64(instrument_id))
SETTINGS skip_unavailable_shards = 0;

CREATE TABLE IF NOT EXISTS tsp_history.feature_1d_local ON CLUSTER tsp_analytics
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
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/tsp_history/feature_1d_local', '{replica}')
PARTITION BY toYYYYMM(trade_date)
ORDER BY (segment_id, trade_date, instrument_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS tsp_history.feature_1d ON CLUSTER tsp_analytics
AS tsp_history.feature_1d_local
ENGINE = Distributed(tsp_analytics, tsp_history, feature_1d_local, cityHash64(instrument_id))
SETTINGS skip_unavailable_shards = 0;

CREATE TABLE IF NOT EXISTS tsp_history.signal_event_local ON CLUSTER tsp_analytics
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
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/tsp_history/signal_event_local', '{replica}')
PARTITION BY toYYYYMM(trade_date)
ORDER BY (segment_id, definition_id, trade_date, event_at, instrument_id, event_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS tsp_history.signal_event ON CLUSTER tsp_analytics
AS tsp_history.signal_event_local
ENGINE = Distributed(tsp_analytics, tsp_history, signal_event_local, cityHash64(instrument_id))
SETTINGS skip_unavailable_shards = 0;

CREATE TABLE IF NOT EXISTS tsp_history.research_manifest ON CLUSTER tsp_analytics
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
ENGINE = ReplicatedMergeTree('/clickhouse/tables/global/tsp_history/research_manifest', '{shard}-{replica}')
ORDER BY (snapshot_id, table_name, variant, segment_date);
