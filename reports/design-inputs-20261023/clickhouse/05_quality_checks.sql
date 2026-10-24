-- 发布门禁中的 SQL 检查示例：均为只读，限定待发布 segment / snapshot。
-- V1/V2/V3/V4/V5 返回空集才满足该项要求；V6 与清单预期行数比较。
-- 这些检查不替代交易日历、来源时钟、分片清单校验与跨副本发布屏障。

-- V1: 采样业务键唯一。PRIMARY KEY 不会替你强制执行这一规则。
SELECT instrument_id, capture_id, count() AS duplicates
FROM tsp_history.quote_second
WHERE segment_id = {quote_segment_id:UInt64}
GROUP BY instrument_id, capture_id
HAVING duplicates > 1;

-- V2: 研究清单每个表/变体/交易日只有一个选定版本。
SELECT table_name, variant, segment_date, count() AS versions
FROM tsp_history.research_manifest
WHERE snapshot_id = {snapshot_id:UUID}
GROUP BY table_name, variant, segment_date
HAVING versions != 1;

-- V3: 声称严格可用的日频特征必须有完整的可得时间和证券状态证据。
SELECT instrument_id, trade_date
FROM tsp_history.feature_1d
WHERE segment_id = {daily_segment_id:UInt64}
  AND pit_eligible = 1
  AND (
      isNull(available_at) OR isNull(input_cutoff_at)
      OR input_cutoff_at > available_at
      OR instrument_state_known != 1
      OR isNull(is_st_asof) OR isNull(is_listed_asof)
      OR (feature_price_basis = 'adjusted_asof' AND isNull(adjustment_anchor_date))
  );

-- V4: 分钟范围索引的业务键唯一。
SELECT instrument_id, minute_start, count() AS duplicates
FROM tsp_history.quote_1m_envelope
WHERE segment_id = {envelope_segment_id:UInt64}
GROUP BY instrument_id, minute_start
HAVING duplicates > 1;

-- V5: 每条有效原始采样都被正确的范围索引覆盖，且最大值不会导致漏候选。
-- Q3 允许 NULL 最大值进入候选，因此 NULL 不产生假阴性，但会降低裁剪效率。
SELECT q.instrument_id, q.observed_at, q.capture_id
FROM
(
    SELECT instrument_id, observed_at, capture_id, change_ratio, turnover_ratio
    FROM tsp_history.quote_second
    WHERE segment_id = {quote_segment_id:UInt64} AND input_valid = 1
) AS q
GLOBAL LEFT JOIN
(
    SELECT segment_id, instrument_id, minute_start, max_change_ratio, max_turnover_ratio
    FROM tsp_history.quote_1m_envelope
    WHERE segment_id = {envelope_segment_id:UInt64}
      AND quote_segment_id = {quote_segment_id:UInt64}
) AS e
ON q.instrument_id = e.instrument_id
   AND toStartOfMinute(q.observed_at) = e.minute_start
WHERE isNull(e.segment_id)
   OR e.max_change_ratio < q.change_ratio
   OR e.max_turnover_ratio < q.turnover_ratio
SETTINGS join_use_nulls = 1;

-- V6: 实际行数与 expected_rows 核对；每个 shard 只读一个完整副本。
SELECT segment_id, count() AS rows,
       uniqExact(instrument_id) AS instruments,
       min(trade_date) AS first_date, max(trade_date) AS last_date
FROM tsp_history.quote_second
WHERE segment_id = {quote_segment_id:UInt64}
GROUP BY segment_id;
