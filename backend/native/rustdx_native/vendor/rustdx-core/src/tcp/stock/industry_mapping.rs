//! 旧行业、省份名称查询的兼容入口。
//!
//! 原静态映射缺少可核实的来源与版本，已移除。
//! 财务接口仍保留原始 industry/province 编码；名称需由调用方使用
//! 同源、带版本的字典解析。未知名称不表示证券没有行业或省份。

/// 始终返回“未知行业”。
#[deprecated(note = "静态映射未核实；请保留原始行业编码并使用同源版本字典")]
pub fn get_industry_name(_code: u16) -> &'static str {
    "未知行业"
}

/// 保留原始编码，名称和大类分别返回“未知行业”“未分类”。
#[deprecated(note = "静态映射未核实；请保留原始行业编码并使用同源版本字典")]
pub fn get_industry_info(code: u16) -> (u16, &'static str, &'static str) {
    (code, "未知行业", "未分类")
}

/// 始终返回“未知省份”。
#[deprecated(note = "静态映射未核实；请保留原始省份编码并使用同源版本字典")]
pub fn get_province_name(_code: u16) -> &'static str {
    "未知省份"
}
