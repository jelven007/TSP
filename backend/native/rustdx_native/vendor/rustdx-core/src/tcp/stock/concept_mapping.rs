//! 旧静态概念查询兼容入口。未核实的样例数据已移除。
//!
//! 概念成分请通过 `Client::block("block_gn.dat")` 获取，按源板块名筛选，
//! 并由调用方记录采集日期；不同数据源的同名板块不保证含义一致。

/// 旧静态概念股数据类型；保留以兼容调用方类型引用。
#[derive(Debug, Clone)]
pub struct ConceptStock {
    pub code: &'static str,
    pub name: &'static str,
}

/// 已移除静态样例，始终返回 `None`；不代表该概念不存在。
#[deprecated(
    since = "1.12.1",
    note = "静态样例未核实；请使用Client::block获取源板块成分"
)]
pub fn get_concept_stocks(_concept: &str) -> Option<Vec<ConceptStock>> {
    None
}

/// 已移除静态样例，始终返回空列表。
#[deprecated(since = "1.12.1", note = "请从Client::block的结果获取源板块名称")]
pub fn get_concept_names() -> Vec<&'static str> {
    Vec::new()
}

/// 已移除静态样例，始终返回 `None`。
#[deprecated(since = "1.12.1", note = "请从Client::block的结果统计源板块成分")]
pub fn get_concept_info(_concept: &str) -> Option<(&'static str, usize)> {
    None
}
