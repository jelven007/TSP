-- 可选：确有“某一秒/短时间窗全市场横截面”需求时才加。
-- 需要支持 _part_offset 的 ClickHouse 版本（25.5 起）。
-- 单节点目标为 quote_second；自建集群需改为
-- quote_second_local ON CLUSTER tsp_analytics。
-- 此脚本不自动为历史分区物化；不要对十年数据一次性 MATERIALIZE。
-- 投影含逐行键与偏移，不是零存储成本；宽范围回表可能比顺序扫描更慢。
ALTER TABLE tsp_history.quote_second
ADD PROJECTION IF NOT EXISTS by_observed_time
(
    SELECT segment_id, observed_at, instrument_id, _part_offset
    ORDER BY (segment_id, observed_at, instrument_id)
);
