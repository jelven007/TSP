//! Builder 模式 API
//!
//! 提供流畅的链式调用接口，简化数据查询操作
//!
//! # 特性
//!
//! - ✅ 流畅的链式调用
//! - ✅ 编译期类型安全
//! - ✅ 必填参数检查
//! - ✅ 合理的默认值
//!
//! # 使用示例
//!
//! ```rust,no_run
//! use rustdx_complete::builder::KlineBuilder;
//! use rustdx_complete::tcp::{Tcp, Tdx};
//!
//! // 创建 TCP 连接
//! let mut tcp = Tcp::new().unwrap();
//!
//! // 使用 Builder 模式获取K线数据
//! let mut kline = KlineBuilder::new()
//!     .code("600000")           // 设置股票代码
//!     .category(9)              // 日线
//!     .count(100)               // 获取100条
//!     .build()
//!     .unwrap();
//!
//! // 发送请求并解析数据
//! kline.recv_parsed(&mut tcp).unwrap();
//! let data = kline.result();
//! ```

use crate::data::{Period, Symbol};
use crate::tcp::stock::{Kline, KlineRequest};

// ============================================================================
// KlineBuilder
// ============================================================================

/// K线查询构建器
///
/// 提供流畅的链式调用接口来构建K线查询
///
/// # 示例
///
/// ```rust
/// use rustdx_complete::builder::KlineBuilder;
///
/// let kline = KlineBuilder::new()
///     .code("600000")
///     .category(9)  // 日线
///     .count(100)
///     .build()
///     .unwrap();
/// ```
#[derive(Debug, Clone)]
pub struct KlineBuilder<'a> {
    code: Option<&'a str>,
    market: Option<u16>,
    category: Option<u16>,
    start: Option<u16>,
    count: Option<u16>,
}

impl<'a> KlineBuilder<'a> {
    /// 创建新的KlineBuilder
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let builder = KlineBuilder::new();
    /// ```
    pub fn new() -> Self {
        Self {
            code: None,
            market: None,
            category: None,
            start: None,
            count: None,
        }
    }

    /// 设置股票代码（必填）
    ///
    /// # 参数
    ///
    /// - `code`: 6位股票代码，如 "600000"
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let builder = KlineBuilder::new()
    ///     .code("600000");
    /// ```
    pub fn code(mut self, code: &'a str) -> Self {
        self.code = Some(code);
        self
    }

    /// 使用带交易所的标准代码，避免sh000001和sz000001被混淆。
    ///
    /// ```
    /// use rustdx_complete::{KlineBuilder, data::Symbol};
    /// let symbol: Symbol = "sh000001".parse().unwrap();
    /// let request = KlineBuilder::new().symbol(&symbol).build().unwrap();
    /// assert_eq!(request.market, 1);
    /// assert_eq!(request.code, "000001");
    /// ```
    pub fn symbol(mut self, symbol: &'a Symbol) -> Self {
        self.code = Some(symbol.code());
        // 保留build返回错误的接口，2将被协议市场校验拒绝。
        self.market = Some(symbol.market().tdx_market().unwrap_or(2));
        self
    }

    pub fn period(self, period: Period) -> Self {
        self.category(period.tdx_category())
    }

    /// 设置市场代码（可选，默认保留旧的股票代码推断；指数等应使用symbol）
    ///
    /// # 参数
    ///
    /// - `market`: 0=深圳, 1=上海
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let builder = KlineBuilder::new()
    ///     .code("600000")
    ///     .market(1);  // 上海
    /// ```
    pub fn market(mut self, market: u16) -> Self {
        self.market = Some(market);
        self
    }

    /// 设置K线类别（可选，默认9=日线）
    ///
    /// # K线类别
    ///
    /// - 0: 5分钟K线
    /// - 1: 15分钟K线
    /// - 2: 30分钟K线
    /// - 3: 1小时K线
    /// - 4: 日K线
    /// - 5: 周K线
    /// - 6: 月K线
    /// - 7: 1分钟K线
    /// - 8: 1分钟K线
    /// - 9: 日K线（推荐）
    /// - 10: 季K线
    /// - 11: 年K线
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let builder = KlineBuilder::new()
    ///     .code("600000")
    ///     .category(9);  // 日线
    /// ```
    pub fn category(mut self, category: u16) -> Self {
        self.category = Some(category);
        self
    }

    /// 设置起始位置（可选，默认0）
    ///
    /// # 参数
    ///
    /// - `start`: 起始位置，从0开始
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let builder = KlineBuilder::new()
    ///     .code("600000")
    ///     .start(0);  // 从最新开始
    /// ```
    pub fn start(mut self, start: u16) -> Self {
        self.start = Some(start);
        self
    }

    /// 设置获取数量（可选，默认100，最大800）
    ///
    /// # 参数
    ///
    /// - `count`: K线数量，最大800
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let builder = KlineBuilder::new()
    ///     .code("600000")
    ///     .count(200);  // 获取200条
    /// ```
    pub fn count(mut self, count: u16) -> Self {
        self.count = Some(count);
        self
    }

    /// 构建Kline对象
    ///
    /// # 返回
    ///
    /// - `Ok(Kline)`: 构建成功
    /// - `Err(String)`: 缺少必填参数（股票代码）
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let kline = KlineBuilder::new()
    ///     .code("600000")
    ///     .build()
    ///     .unwrap();
    /// ```
    pub fn build(self) -> Result<Kline<'a>, String> {
        self.build_request().map(Kline::from_request)
    }

    /// 构建不可变请求，供独立股票/指数解码器使用。
    ///
    /// ```
    /// use rustdx_complete::KlineBuilder;
    /// let request = KlineBuilder::new().code("600000").build_request().unwrap();
    /// assert_eq!(request.market(), 1);
    /// ```
    pub fn build_request(self) -> Result<KlineRequest<'a>, String> {
        // 检查必填参数
        let code = self.code.ok_or("缺少必填参数: code（股票代码）")?;
        if code.len() != 6 || !code.bytes().all(|b| b.is_ascii_alphanumeric()) {
            return Err("code必须为六位ASCII字母数字".into());
        }

        // 设置默认值
        let market = self.market.unwrap_or_else(|| {
            // 自动识别市场：6开头的上海，其他深圳
            if code.starts_with('6') {
                1 // 上海
            } else {
                0 // 深圳（0/3 开头及其他默认深圳）
            }
        });

        let category = self.category.unwrap_or(9); // 默认日线
        let start = self.start.unwrap_or(0); // 默认从0开始
        let count = self.count.unwrap_or(100); // 默认100条

        if market > 1 {
            return Err("行情协议仅支持0深圳或1上海".into());
        }
        if category > 11 || count == 0 {
            return Err("category必须为0–11，count必须大于0".into());
        }
        // 验证参数范围
        if count > 800 {
            return Err("count 参数不能超过 800".to_string());
        }

        KlineRequest::new(market, code, category, start, count).map_err(|e| e.to_string())
    }
}

impl<'a> Default for KlineBuilder<'a> {
    fn default() -> Self {
        Self::new()
    }
}

// ============================================================================
// 便捷方法
// ============================================================================

impl<'a> KlineBuilder<'a> {
    /// 日线（快捷方法）
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let kline = KlineBuilder::daily("600000", 100).unwrap();
    /// ```
    pub fn daily(code: &'a str, count: u16) -> Result<Kline<'a>, String> {
        Self::new().code(code).category(9).count(count).build()
    }

    /// 60分钟线（快捷方法）
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let kline = KlineBuilder::min60("600000", 100).unwrap();
    /// ```
    pub fn min60(code: &'a str, count: u16) -> Result<Kline<'a>, String> {
        Self::new().code(code).category(3).count(count).build()
    }

    /// 30分钟线（快捷方法）
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let kline = KlineBuilder::min30("600000", 100).unwrap();
    /// ```
    pub fn min30(code: &'a str, count: u16) -> Result<Kline<'a>, String> {
        Self::new().code(code).category(2).count(count).build()
    }

    /// 15分钟线（快捷方法）
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let kline = KlineBuilder::min15("600000", 100).unwrap();
    /// ```
    pub fn min15(code: &'a str, count: u16) -> Result<Kline<'a>, String> {
        Self::new().code(code).category(1).count(count).build()
    }

    /// 5分钟线（快捷方法）
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let kline = KlineBuilder::min5("600000", 100).unwrap();
    /// ```
    pub fn min5(code: &'a str, count: u16) -> Result<Kline<'a>, String> {
        Self::new().code(code).category(0).count(count).build()
    }

    /// 1分钟线（快捷方法）
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let kline = KlineBuilder::min1("600000", 100).unwrap();
    /// ```
    pub fn min1(code: &'a str, count: u16) -> Result<Kline<'a>, String> {
        Self::new().code(code).category(7).count(count).build()
    }

    /// 周线（快捷方法）
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let kline = KlineBuilder::weekly("600000", 100).unwrap();
    /// ```
    pub fn weekly(code: &'a str, count: u16) -> Result<Kline<'a>, String> {
        Self::new().code(code).category(5).count(count).build()
    }

    /// 月线（快捷方法）
    ///
    /// # 示例
    ///
    /// ```rust
    /// use rustdx_complete::builder::KlineBuilder;
    ///
    /// let kline = KlineBuilder::monthly("600000", 100).unwrap();
    /// ```
    pub fn monthly(code: &'a str, count: u16) -> Result<Kline<'a>, String> {
        Self::new().code(code).category(6).count(count).build()
    }
}

// ============================================================================
// 单元测试
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_builder_basic() {
        let kline = KlineBuilder::new()
            .code("600000")
            .category(9)
            .count(100)
            .build()
            .unwrap();

        assert_eq!(kline.code, "600000");
        assert_eq!(kline.category, 9);
        assert_eq!(kline.count, 100);
    }

    #[test]
    fn test_builder_defaults() {
        let kline = KlineBuilder::new().code("600000").build().unwrap();

        // 检查默认值
        assert_eq!(kline.code, "600000");
        assert_eq!(kline.category, 9); // 默认日线
        assert_eq!(kline.count, 100); // 默认100条
        assert_eq!(kline.start, 0); // 默认从0开始
    }

    #[test]
    fn test_builder_auto_market() {
        // 上海市场（6开头）
        let kline1 = KlineBuilder::new().code("600000").build().unwrap();
        assert_eq!(kline1.market, 1); // 上海

        // 深圳市场（0开头）
        let kline2 = KlineBuilder::new().code("000001").build().unwrap();
        assert_eq!(kline2.market, 0); // 深圳

        // 创业板（3开头）
        let kline3 = KlineBuilder::new().code("300001").build().unwrap();
        assert_eq!(kline3.market, 0); // 深圳
    }

    #[test]
    fn explicit_symbols_preserve_exchange_and_reject_unverified_mapping() {
        let index: Symbol = "sh000001".parse().unwrap();
        let stock: Symbol = "sz000001".parse().unwrap();
        assert_eq!(
            KlineBuilder::new()
                .symbol(&index)
                .period(Period::Day)
                .build()
                .unwrap()
                .market,
            1
        );
        assert_eq!(
            KlineBuilder::new().symbol(&stock).build().unwrap().market,
            0
        );
        let beijing: Symbol = "bj920001".parse().unwrap();
        assert!(KlineBuilder::new().symbol(&beijing).build().is_err());
    }

    #[test]
    fn test_builder_convenience_methods() {
        // 日线
        let kline1 = KlineBuilder::daily("600000", 100).unwrap();
        assert_eq!(kline1.category, 9);

        // 60分钟
        let kline2 = KlineBuilder::min60("600000", 100).unwrap();
        assert_eq!(kline2.category, 3);

        // 30分钟
        let kline3 = KlineBuilder::min30("600000", 100).unwrap();
        assert_eq!(kline3.category, 2);

        // 15分钟
        let kline4 = KlineBuilder::min15("600000", 100).unwrap();
        assert_eq!(kline4.category, 1);

        // 5分钟
        let kline5 = KlineBuilder::min5("600000", 100).unwrap();
        assert_eq!(kline5.category, 0);

        // 1分钟
        let kline6 = KlineBuilder::min1("600000", 100).unwrap();
        assert_eq!(kline6.category, 7);

        // 周线
        let kline7 = KlineBuilder::weekly("600000", 100).unwrap();
        assert_eq!(kline7.category, 5);

        // 月线
        let kline8 = KlineBuilder::monthly("600000", 100).unwrap();
        assert_eq!(kline8.category, 6);
    }

    #[test]
    fn test_builder_missing_code() {
        let result = KlineBuilder::new().build();
        assert!(result.is_err());
        assert_eq!(result.unwrap_err(), "缺少必填参数: code（股票代码）");
    }

    #[test]
    fn invalid_builder_inputs_return_errors() {
        for code in ["", "中文", "12345", "sh600000"] {
            assert!(KlineBuilder::new().code(code).build().is_err());
        }
        assert!(
            KlineBuilder::new()
                .code("600000")
                .market(2)
                .build()
                .is_err()
        );
        assert!(
            KlineBuilder::new()
                .code("600000")
                .category(12)
                .build()
                .is_err()
        );
        assert!(KlineBuilder::new().code("600000").count(0).build().is_err());
    }

    #[test]
    fn test_builder_count_too_large() {
        let result = KlineBuilder::new().code("600000").count(801).build();

        assert!(result.is_err());
        assert_eq!(result.unwrap_err(), "count 参数不能超过 800");
    }

    #[test]
    fn test_builder_chain() {
        let kline = KlineBuilder::new()
            .code("600000")
            .market(1)
            .category(9)
            .start(0)
            .count(200)
            .build()
            .unwrap();

        assert_eq!(kline.code, "600000");
        assert_eq!(kline.market, 1);
        assert_eq!(kline.category, 9);
        assert_eq!(kline.start, 0);
        assert_eq!(kline.count, 200);
    }
}
