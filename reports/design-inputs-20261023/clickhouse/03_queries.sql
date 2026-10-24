-- 参数使用 ClickHouse 原生 {name:Type} 语法；由受信后端绑定，不拼接用户字符串。
-- 示例没有硬编码股票、历史日期或“默认最新版本”。
-- 01_schema.sql 的单节点表与 02_cluster.sql 的 Distributed 门面使用同名。
-- 每条语句独立调用；清单完整性和权限校验须先于执行。

-- Q1: 解析固定研究快照。调用方校验每个 table/variant/交易日 恰好一条，
-- 且覆盖日历要求的所有交易日；空清单不能解释成“没有行情”。
SELECT table_name, variant, segment_date, partition_month, segment_id,
       source_id, transform_version, expected_rows, manifest_sha256
FROM tsp_history.research_manifest
WHERE snapshot_id = {snapshot_id:UUID}
ORDER BY table_name, variant, segment_date;

-- Q2: 十年日频初筛。这里的决策截止是每个交易日结束，
-- 不是该日盘中，也不允许宣称按该日收盘价立即成交。
-- 返回 available_at 供回测对齐后续可成交时刻。
SELECT instrument_id, trade_date, available_at, raw_close,
       change_ratio, turnover_ratio, volume_ratio_20, ma20, ma60
FROM tsp_history.feature_1d
PREWHERE trade_date BETWEEN {date_from:Date} AND {date_to:Date}
    AND segment_id GLOBAL IN
    (
        SELECT DISTINCT segment_id
        FROM tsp_history.research_manifest
        WHERE snapshot_id = {snapshot_id:UUID}
          AND table_name = 'feature_1d' AND variant = {daily_variant:String}
          AND segment_date BETWEEN {date_from:Date} AND {date_to:Date}
    )
WHERE pit_eligible = 1 AND instrument_state_known = 1
    AND is_st_asof = 0 AND is_listed_asof = 1
    AND available_at < toDateTime64(trade_date + INTERVAL 1 DAY, 3, 'Asia/Shanghai')
    AND input_cutoff_at <= available_at
    AND feature_set_id = {feature_set_id:UInt32}
    AND change_ratio >= {min_change:Float64}
    AND turnover_ratio >= {min_turnover:Float64}
    AND volume_ratio_20 >= {min_volume_ratio:Float64}
    AND close_for_features > ma20 AND ma20 > ma60
ORDER BY trade_date, instrument_id;

-- Q3: 为“同一秒涨幅与换手同时达到阈值”生成保守的分钟候选。
-- 各最值可能发生在不同秒，所以此结果不是最终信号。
SELECT quote_segment_id, instrument_id, minute_start,
       sample_count, expected_sample_count, max_gap_ms, boundary_gap
FROM tsp_history.quote_1m_envelope
PREWHERE trade_date BETWEEN {date_from:Date} AND {date_to:Date}
    AND segment_id GLOBAL IN
    (
        SELECT DISTINCT segment_id
        FROM tsp_history.research_manifest
        WHERE snapshot_id = {snapshot_id:UUID}
          AND table_name = 'quote_1m_envelope' AND variant = {envelope_variant:String}
          AND segment_date BETWEEN {date_from:Date} AND {date_to:Date}
    )
WHERE minute_start >= {minute_from:DateTime64(3, 'Asia/Shanghai')}
    AND minute_start < {minute_to:DateTime64(3, 'Asia/Shanghai')}
    AND (isNull(max_change_ratio) OR max_change_ratio >= {min_change:Float64})
    AND (isNull(max_turnover_ratio) OR max_turnover_ratio >= {min_turnover:Float64})
ORDER BY minute_start, instrument_id;

-- Q4: 回到明细精确复核。适用于已划定的有界时间窗；
-- 大量候选应由后端拆成“segment + instrument + 连续时间段”任务，
-- 避免构造无界 GLOBAL IN 集合。返回的是有效采样命中，不是完整逐笔触发证明。
SELECT instrument_id, observed_at, capture_id,
       last_price, change_ratio, turnover_ratio, quality_mask
FROM tsp_history.quote_second
PREWHERE trade_date BETWEEN {date_from:Date} AND {date_to:Date}
    AND observed_at >= {minute_from:DateTime64(3, 'Asia/Shanghai')}
    AND observed_at < {minute_to:DateTime64(3, 'Asia/Shanghai')}
    AND segment_id GLOBAL IN
    (
        SELECT DISTINCT segment_id
        FROM tsp_history.research_manifest
        WHERE snapshot_id = {snapshot_id:UUID}
          AND table_name = 'quote_second' AND variant = {quote_variant:String}
          AND segment_date BETWEEN {date_from:Date} AND {date_to:Date}
    )
WHERE (segment_id, instrument_id, toStartOfMinute(observed_at)) GLOBAL IN
    (
        SELECT quote_segment_id, instrument_id, toStartOfMinute(minute_start)
        FROM tsp_history.quote_1m_envelope
        WHERE trade_date BETWEEN {date_from:Date} AND {date_to:Date}
          AND minute_start >= {minute_from:DateTime64(3, 'Asia/Shanghai')}
          AND minute_start < {minute_to:DateTime64(3, 'Asia/Shanghai')}
          AND segment_id GLOBAL IN
          (
              SELECT DISTINCT segment_id
              FROM tsp_history.research_manifest
              WHERE snapshot_id = {snapshot_id:UUID}
                AND table_name = 'quote_1m_envelope' AND variant = {envelope_variant:String}
                AND segment_date BETWEEN {date_from:Date} AND {date_to:Date}
          )
          AND (isNull(max_change_ratio) OR max_change_ratio >= {min_change:Float64})
          AND (isNull(max_turnover_ratio) OR max_turnover_ratio >= {min_turnover:Float64})
    )
    AND input_valid = 1 AND session = 'continuous'
    AND change_ratio >= {min_change:Float64}
    AND turnover_ratio >= {min_turnover:Float64}
ORDER BY observed_at, instrument_id, capture_id
SETTINGS max_execution_time = 600, max_rows_in_set = 1000000, set_overflow_mode = 'throw';

-- Q5: 回测 worker 读取少量证券的秒级窗口。只选择本次计算需要的列。
-- 明确“半开区间”；导出可使用 ArrowStream，并在客户端流式读取。
SELECT instrument_id, observed_at, capture_id, last_price,
       bid1, ask1, bid1_shares, ask1_shares,
       cum_volume_shares, cum_amount_yuan, source_at,
       source_clock_basis, source_date_inferred, input_valid, quality_mask
FROM tsp_history.quote_second
PREWHERE instrument_id IN {instrument_ids:Array(UInt32)}
    AND observed_at >= {time_from:DateTime64(3, 'Asia/Shanghai')}
    AND observed_at < {time_to:DateTime64(3, 'Asia/Shanghai')}
    AND trade_date BETWEEN {date_from:Date} AND {date_to:Date}
    AND segment_id GLOBAL IN
    (
        SELECT DISTINCT segment_id
        FROM tsp_history.research_manifest
        WHERE snapshot_id = {snapshot_id:UUID}
          AND table_name = 'quote_second' AND variant = {quote_variant:String}
          AND segment_date BETWEEN {date_from:Date} AND {date_to:Date}
    )
ORDER BY instrument_id, observed_at, capture_id;

-- Q6: 已物化公共信号的年度分布。definition_id 固定代码与参数。
SELECT toYear(trade_date) AS year, count() AS events,
       uniqExact(instrument_id) AS instruments
FROM tsp_history.signal_event
WHERE trade_date BETWEEN {date_from:Date} AND {date_to:Date}
    AND definition_id = {definition_id:UInt32}
    AND pit_eligible = 1 AND available_at <= event_at
    AND segment_id GLOBAL IN
    (
        SELECT DISTINCT segment_id
        FROM tsp_history.research_manifest
        WHERE snapshot_id = {snapshot_id:UUID}
          AND table_name = 'signal_event' AND variant = {signal_variant:String}
          AND segment_date BETWEEN {date_from:Date} AND {date_to:Date}
    )
GROUP BY year
ORDER BY year;

-- Q7: 严格历史证券状态。先筛选“决策时刻已知”的版本，
-- 再在每个有效期内挑选修订，最后检查有效期，不直接用当前维表。
-- tuple 包装保留 NULL，不让 argMax 分别跳过 NULL 拼出不存在的状态。
SELECT instrument_id, valid_from,
       tupleElement(state, 1) AS valid_to,
       tupleElement(state, 2) AS name,
       tupleElement(state, 3) AS is_st,
       tupleElement(state, 4) AS is_listed,
       tupleElement(state, 5) AS industry_id,
       tupleElement(state, 6) AS float_shares
FROM
(
    SELECT instrument_id, valid_from,
           argMax(
               tuple(valid_to, name, is_st, is_listed, industry_id, float_shares),
               tuple(assumeNotNull(available_at), record_version)
           ) AS state
    FROM tsp_history.instrument_state
    WHERE instrument_id IN {instrument_ids:Array(UInt32)}
      AND available_at <= {decision_at:DateTime64(3, 'Asia/Shanghai')}
      AND segment_id GLOBAL IN
      (
          SELECT DISTINCT segment_id
          FROM tsp_history.research_manifest
          WHERE snapshot_id = {snapshot_id:UUID}
            AND table_name = 'instrument_state' AND variant = {state_variant:String}
      )
    GROUP BY instrument_id, valid_from
)
WHERE valid_from <= toDate({decision_at:DateTime64(3, 'Asia/Shanghai')})
    AND (isNull(tupleElement(state, 1))
         OR tupleElement(state, 1) > toDate({decision_at:DateTime64(3, 'Asia/Shanghai')}))
ORDER BY instrument_id;

-- Q8: 可信分钟输入。当前未完成柱和未证明可得时间的数据不能进入严格回测。
SELECT instrument_id, bar_start, bar_end, available_at,
       raw_open, raw_high, raw_low, raw_close, volume_shares, amount_yuan
FROM tsp_history.bar_1m
WHERE instrument_id IN {instrument_ids:Array(UInt32)}
    AND trade_date BETWEEN {date_from:Date} AND {date_to:Date}
    AND bar_start >= {time_from:DateTime64(3, 'Asia/Shanghai')}
    AND bar_end <= {time_to:DateTime64(3, 'Asia/Shanghai')}
    AND available_at <= {decision_at:DateTime64(3, 'Asia/Shanghai')}
    AND input_cutoff_at <= available_at
    AND pit_eligible = 1
    AND segment_id GLOBAL IN
    (
        SELECT DISTINCT segment_id
        FROM tsp_history.research_manifest
        WHERE snapshot_id = {snapshot_id:UUID}
          AND table_name = 'bar_1m' AND variant = {minute_variant:String}
          AND segment_date BETWEEN {date_from:Date} AND {date_to:Date}
    )
ORDER BY instrument_id, bar_start;
