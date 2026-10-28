//! 数据完整性验证模块
//!
//! 提供轻量级的数据验证功能，包括：
//! - K线数据连续性验证
//! - 财务数据一致性验证
//! - 异常值检测
//! - 多数据源交叉验证
//!
//! # 设计原则
//!
//! - 零侵入性：不修改现有数据结构
//! - 轻量级：按需调用验证函数
//! - 可扩展：使用 Trait 支持自定义验证
//!
//! # 使用示例
//!
//! ```ignore
//! use rustdx_complete::tcp::stock::{Kline, validator::*};
//!
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! # let mut tcp = rustdx_complete::tcp::Tcp::new()?;
//! let mut kline = Kline::new(1, "600000", 9, 0, 100);
//! kline.recv_parsed(&mut tcp)?;
//!
//! // 验证数据连续性
//! let result = validate_kline_continuity(kline.result(), "600000");
//! match result.level {
//!     ValidationLevel::Ok => println!("✅ 数据验证通过"),
//!     ValidationLevel::Warning(_) => println!("⚠️  发现警告"),
//!     ValidationLevel::Error(_) => println!("❌ 发现错误"),
//! }
//! # Ok(())
//! # }
//! ```

use super::{FinanceField, FinanceInfoData, FinanceSemanticStatus, FinanceSnapshot, KlineData};
use crate::tcp::helper::DateTime;

// ============================================================================
// 核心数据结构
// ============================================================================

/// 验证级别
#[derive(Debug, Clone, PartialEq)]
pub enum ValidationLevel {
    /// 数据正常，无需处理
    Ok,
    /// 警告（可能异常但不致命）
    Warning(String),
    /// 错误（数据有问题，需要修复）
    Error(String),
}

/// 数据位置标识
#[derive(Debug, Clone)]
pub struct DataLocation {
    /// 股票代码
    pub code: String,
    /// 日期（可选）
    pub date: Option<DateTime>,
    /// 字段名（可选）
    pub field: Option<String>,
}

/// 验证结果
#[derive(Debug, Clone)]
pub struct ValidationResult {
    /// 验证级别
    pub level: ValidationLevel,
    /// 问题描述列表
    pub details: Vec<String>,
    /// 修复建议列表
    pub suggestions: Vec<String>,
    /// 错误位置（可选）
    pub location: Option<DataLocation>,
}

impl ValidationResult {
    /// 创建成功结果
    pub fn ok(message: impl Into<String>) -> Self {
        Self {
            level: ValidationLevel::Ok,
            details: vec![message.into()],
            suggestions: vec![],
            location: None,
        }
    }

    /// 创建警告结果
    pub fn warning(
        message: impl Into<String>,
        details: Vec<String>,
        suggestions: Vec<String>,
    ) -> Self {
        Self {
            level: ValidationLevel::Warning(message.into()),
            details,
            suggestions,
            location: None,
        }
    }

    /// 创建错误结果
    pub fn error(
        message: impl Into<String>,
        details: Vec<String>,
        suggestions: Vec<String>,
    ) -> Self {
        Self {
            level: ValidationLevel::Error(message.into()),
            details,
            suggestions,
            location: None,
        }
    }

    /// 检查是否通过验证（无错误和警告）
    pub fn is_valid(&self) -> bool {
        matches!(self.level, ValidationLevel::Ok)
    }
}

// ============================================================================
// 验证函数
// ============================================================================

/// 检查K线数据的日期连续性，识别缺失的交易日
///
/// # 参数
///
/// - `data`: K线数据切片
/// - `code`: 股票代码
///
/// # 返回
///
/// 验证结果，包含缺失的交易日列表
///
/// # 示例
///
/// ```rust
/// use rustdx_complete::tcp::stock::validator::validate_kline_continuity;
/// # use rustdx_complete::tcp::stock::KlineData;
///
/// # let data = vec![];
/// let result = validate_kline_continuity(&data, "600000");
/// if !result.is_valid() {
///     println!("发现缺失日期: {:?}", result.details);
/// }
/// ```
pub fn validate_kline_continuity(data: &[KlineData], code: &str) -> ValidationResult {
    if data.is_empty() {
        return ValidationResult::error(
            "数据为空",
            vec!["K线数据不包含任何记录".to_string()],
            vec!["请检查网络连接后重新获取".to_string()],
        );
    }

    let mut issues = Vec::new();
    let mut prev_dt: Option<chrono::NaiveDateTime> = None;
    for bar in data {
        let dt = match bar.dt.try_into_naive() {
            Ok(dt) => dt,
            Err(e) => return ValidationResult::error("非法日期", vec![e.to_string()], vec![]),
        };
        if let Some(prev) = prev_dt {
            if dt <= prev {
                return ValidationResult::error(
                    "日期重复或未升序",
                    vec![format!("{code}: {prev} -> {dt}")],
                    vec![],
                );
            }
            if dt.date() != prev.date() {
                match crate::calendar::TradingCalendar::trading_days_between(prev.date(), dt.date())
                {
                    Ok(days) => {
                        for day in days
                            .into_iter()
                            .filter(|d| *d > prev.date() && *d < dt.date())
                        {
                            issues.push(format!("{code}: 缺少交易日 {day}（原因待核实）"));
                        }
                    }
                    Err(e) => {
                        if !issues.iter().any(|i| i.contains("日历")) {
                            issues.push(e.to_string());
                        }
                    }
                }
            }
        }
        prev_dt = Some(dt);
    }

    if issues.is_empty() {
        ValidationResult::ok(format!("K线数据连续性检查通过（共 {} 条）", data.len()))
    } else {
        ValidationResult::warning(
            format!("发现 {} 个可能的日期不连续", issues.len()),
            issues,
            vec![
                "使用增量更新补全缺失数据".to_string(),
                "核实上市/停牌状态后解释缺口，日历覆盖外需提供准确休市数据".to_string(),
            ],
        )
    }
}

/// 验证财务数据内部的逻辑一致性
///
/// # 检查项
///
/// - 总股本 >= 流通股本
/// - 净资产 <= 总资产
/// - 净利润的合理性
///
/// # 参数
///
/// - `data`: 财务数据引用
///
/// # 返回
///
/// 验证结果
///
/// # 示例
///
/// ```rust
/// use rustdx_complete::tcp::stock::validator::validate_finance_consistency;
/// # use rustdx_complete::tcp::stock::FinanceInfoData;
///
/// # let data = FinanceInfoData::default();
/// let result = validate_finance_consistency(&data);
/// if !result.is_valid() {
///     println!("财务数据存在一致性问题: {:?}", result.details);
/// }
/// ```
pub fn validate_finance_consistency(data: &FinanceInfoData) -> ValidationResult {
    let mut issues = Vec::new();

    // 检查 1: 总股本 >= 流通股本
    if data.zongguben < data.liutongguben {
        issues.push(format!(
            "总股本({:.0}) 小于流通股本({:.0})，违背常理",
            data.zongguben, data.liutongguben
        ));
    }

    // 检查 2: 净资产 <= 总资产
    if data.jingzichan > data.zongzichan {
        issues.push(format!(
            "净资产({:.0}) 大于总资产({:.0})，数据异常",
            data.jingzichan, data.zongzichan
        ));
    }

    // 检查 3: 净资产 >= 0
    if data.jingzichan < 0.0 {
        issues.push(format!(
            "净资产为负({:.0})，可能已资不抵债",
            data.jingzichan
        ));
    }

    // 检查 4: 流通股本 <= 总股本
    if data.liutongguben > 0.0 && data.zongguben > 0.0 {
        let ratio = data.liutongguben / data.zongguben;
        if ratio > 1.0 {
            issues.push(format!(
                "流通股本比例异常: {:.1}% (不应超过100%)",
                ratio * 100.0
            ));
        }
    }

    if issues.is_empty() {
        ValidationResult::ok(format!("财务数据一致性检查通过（股票：{}）", data.code))
    } else {
        ValidationResult::error(
            format!("财务数据存在 {} 个一致性问题", issues.len()),
            issues,
            vec![
                "检查原始数据源".to_string(),
                "联系数据提供方确认".to_string(),
                "或排除数据异常的上市公司".to_string(),
            ],
        )
    }
}

/// Semantic warnings are separate from malformed protocol records, which the
/// checked decoder rejects. Only verified values participate in comparisons.
/// Negative earnings, cash flows and equity are valid financial observations.
pub fn validate_finance_snapshot(data: &FinanceSnapshot) -> ValidationResult {
    let mut details = Vec::new();
    if data
        .fields()
        .iter()
        .any(|field| field.semantic_status() != FinanceSemanticStatus::Verified)
    {
        details.push("存在候选或未知字段，业务使用前需核实字段证据".into());
    }
    if data.report_period.is_none() {
        details.push("报告期未知；更新日不能替代报告期".into());
    }
    if let (Some(total), Some(circulating)) = (data.total_shares(), data.circulating_shares())
        && circulating > total
    {
        details.push("流通股本超过总股本，需核实披露口径".into());
    }
    if let (Some(assets), Some(equity)) = (
        data.total_assets_yuan(),
        data.field(FinanceField::NetAssets).value(),
    ) && equity > assets
    {
        details.push("股东权益超过总资产，需核实披露口径".into());
    }
    for field in [
        FinanceField::NetProfit,
        FinanceField::ReportedEps,
        FinanceField::NetAssets,
        FinanceField::OperatingCashFlow,
        FinanceField::CashFlowNetChange,
    ] {
        if data.field(field).value().is_some_and(|value| value < 0.0) {
            details.push(format!("{field:?}为负；这是经营风险提示，非坏报文"));
        }
    }
    if details.is_empty() {
        ValidationResult::ok(format!("财务语义检查通过：{}", data.code()))
    } else {
        ValidationResult::warning(
            "财务语义需关注",
            details,
            vec!["按证券、报告期及字段证据核实；不强制净利润/股本等于EPS".into()],
        )
    }
}

/// 检测价格和成交量的异常值
///
/// # 参数
///
/// - `data`: K线数据切片
/// - `threshold`: 异常阈值（标准差倍数，默认 3.0）
///
/// # 返回
///
/// 验证结果，包含检测到的异常值列表
///
/// # 示例
///
/// ```rust
/// use rustdx_complete::tcp::stock::validator::detect_anomalies;
/// # use rustdx_complete::tcp::stock::KlineData;
///
/// # let data = vec![];
/// let result = detect_anomalies(&data, 3.0);
/// if !result.is_valid() {
///     println!("检测到 {} 个异常值", result.details.len());
/// }
/// ```
pub fn detect_anomalies(data: &[KlineData], threshold: f64) -> ValidationResult {
    if !threshold.is_finite() || threshold <= 0.0 {
        return ValidationResult::error("异常阈值必须为有限正数", vec![], vec![]);
    }
    if data.is_empty() {
        return ValidationResult::error(
            "数据为空",
            vec!["无法检测异常值".to_string()],
            vec!["请先获取数据".to_string()],
        );
    }

    let invalid: Vec<_> = data
        .iter()
        .flat_map(|bar| {
            let result = bar.validate();
            if result.is_valid() {
                vec![]
            } else {
                result.details
            }
        })
        .collect();
    if !invalid.is_empty() {
        return ValidationResult::error("行情数值非法", invalid, vec!["请检查数据源".into()]);
    }
    if data.len() < 10 {
        return ValidationResult::warning(
            "数据量不足".to_string(),
            vec![format!("当前只有 {} 条数据，建议至少 10 条", data.len())],
            vec!["获取更多数据后重新检测".to_string()],
        );
    }

    let mut anomalies = Vec::new();

    // 1. 检测价格异常波动（单日涨跌幅超过 20%）
    for i in 1..data.len() {
        let prev = &data[i - 1];
        let curr = &data[i];

        if prev.close > 0.0 {
            let change_pct = (curr.close - prev.close).abs() / prev.close;

            // A股正常涨跌幅限制是10%（科创板/创业板是20%）
            if change_pct > 0.20 {
                anomalies.push(format!(
                    "{:04}-{:02}-{:02} 价格异常波动: {:.2}% (前收:{:.2}, 今收:{:.2})",
                    curr.dt.year,
                    curr.dt.month,
                    curr.dt.day,
                    change_pct * 100.0,
                    prev.close,
                    curr.close
                ));
            }
        }
    }

    // 2. 检测成交量异常（Z-score 方法）
    let volumes: Vec<f64> = data.iter().map(|k| k.vol).collect();

    // 计算均值和标准差
    let mean_vol = volumes.iter().sum::<f64>() / volumes.len() as f64;
    let variance =
        volumes.iter().map(|v| (v - mean_vol).powi(2)).sum::<f64>() / volumes.len() as f64;
    let std_vol = variance.sqrt();

    // 只检查有明显波动的数据（std > 0）
    if std_vol > 0.0 && mean_vol > 0.0 {
        for bar in data {
            let z_score = (bar.vol - mean_vol) / std_vol;

            if z_score.abs() > threshold {
                anomalies.push(format!(
                    "{:04}-{:02}-{:02} 成交量异常: {:.0} (Z-score: {:.1}, 均值: {:.0})",
                    bar.dt.year, bar.dt.month, bar.dt.day, bar.vol, z_score, mean_vol
                ));
            }
        }
    }

    // 3. 检测价格逻辑错误
    for bar in data {
        if bar.high < bar.low {
            anomalies.push(format!(
                "{:04}-{:02}-{:02} 最高价({:.2}) 低于最低价({:.2})",
                bar.dt.year, bar.dt.month, bar.dt.day, bar.high, bar.low
            ));
        }

        if bar.close > bar.high {
            anomalies.push(format!(
                "{:04}-{:02}-{:02} 收盘价({:.2}) 高于最高价({:.2})",
                bar.dt.year, bar.dt.month, bar.dt.day, bar.close, bar.high
            ));
        }

        if bar.close < bar.low {
            anomalies.push(format!(
                "{:04}-{:02}-{:02} 收盘价({:.2}) 低于最低价({:.2})",
                bar.dt.year, bar.dt.month, bar.dt.day, bar.close, bar.low
            ));
        }

        if bar.open <= 0.0 || bar.close <= 0.0 {
            anomalies.push(format!(
                "{:04}-{:02}-{:02} 价格为零或负数（开:{:.2}, 收:{:.2}）",
                bar.dt.year, bar.dt.month, bar.dt.day, bar.open, bar.close
            ));
        }
    }

    if anomalies.is_empty() {
        ValidationResult::ok(format!(
            "未检测到明显异常（共 {} 条数据，阈值: {:.1}σ）",
            data.len(),
            threshold
        ))
    } else {
        ValidationResult::warning(
            format!("检测到 {} 个异常值", anomalies.len()),
            anomalies,
            vec![
                "检查是否为除权除息日".to_string(),
                "检查是否发布重大公告（复牌等）".to_string(),
                "或调整阈值参数以降低敏感度".to_string(),
            ],
        )
    }
}

// ============================================================================
// Trait 定义（为未来扩展预留）
// ============================================================================

/// 可验证数据 Trait
///
/// 为数据类型实现此 Trait 以支持统一验证接口
pub trait Validatable {
    /// 执行完整验证
    fn validate(&self) -> ValidationResult;

    /// 快速检查（只返回是否通过）
    fn is_valid(&self) -> bool {
        self.validate().is_valid()
    }
}

// 为 KlineData 实现 Validatable（可选）
impl<'a> Validatable for KlineData<'a> {
    fn validate(&self) -> ValidationResult {
        let mut issues = Vec::new();

        if let Err(e) = crate::data::validate_ohlcv(
            self.open,
            self.high,
            self.low,
            self.close,
            self.vol,
            self.amount,
        ) {
            issues.push(e.to_string());
        }
        if let Err(e) = self.dt.try_into_naive() {
            issues.push(e.to_string());
        }
        // 基础价格检查
        if self.open <= 0.0 {
            issues.push("开盘价必须大于0".to_string());
        }
        if self.close <= 0.0 {
            issues.push("收盘价必须大于0".to_string());
        }
        if self.high < self.low {
            issues.push("最高价不能低于最低价".to_string());
        }
        if self.close > self.high {
            issues.push("收盘价不能高于最高价".to_string());
        }
        if self.close < self.low {
            issues.push("收盘价不能低于最低价".to_string());
        }
        if self.vol < 0.0 {
            issues.push("成交量不能为负".to_string());
        }

        if issues.is_empty() {
            ValidationResult::ok(format!(
                "{:04}-{:02}-{:02} 数据验证通过",
                self.dt.year, self.dt.month, self.dt.day
            ))
        } else {
            ValidationResult::error(
                "单条数据验证失败".to_string(),
                issues,
                vec!["请检查数据源".to_string()],
            )
        }
    }
}

// 为 FinanceInfoData 实现 Validatable（可选）
impl Validatable for FinanceInfoData {
    fn validate(&self) -> ValidationResult {
        validate_finance_consistency(self)
    }
}

impl Validatable for FinanceSnapshot {
    fn validate(&self) -> ValidationResult {
        validate_finance_snapshot(self)
    }
}

// ============================================================================
// 单元测试
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn continuity_checks_single_missing_day_duplicates_and_holidays() {
        let data = create_test_kline_data(
            vec![
                create_test_datetime(2025, 1, 6),
                create_test_datetime(2025, 1, 8),
            ],
            vec![10.0, 10.0],
        );
        assert!(
            validate_kline_continuity(&data, "600000")
                .details
                .iter()
                .any(|d| d.contains("2025-01-07"))
        );
        let mut duplicates = data.clone();
        duplicates[1].dt = duplicates[0].dt;
        assert!(matches!(
            validate_kline_continuity(&duplicates, "600000").level,
            ValidationLevel::Error(_)
        ));
        let holidays = create_test_kline_data(
            vec![
                create_test_datetime(2025, 9, 30),
                create_test_datetime(2025, 10, 9),
            ],
            vec![10.0, 10.0],
        );
        assert!(validate_kline_continuity(&holidays, "600000").is_valid());
        let mut bad = data[0].clone();
        bad.open = f64::NAN;
        assert!(!bad.is_valid());
    }

    fn create_test_datetime(y: u16, m: u16, d: u16) -> DateTime {
        DateTime {
            year: y,
            month: m,
            day: d,
            hour: 15,
            minute: 0,
        }
    }

    fn create_test_kline_data(
        dates: Vec<DateTime>,
        close_prices: Vec<f64>,
    ) -> Vec<KlineData<'static>> {
        dates
            .into_iter()
            .zip(close_prices)
            .map(|(dt, close)| KlineData {
                dt,
                code: "600000",
                open: close * 0.98,
                close,
                high: close * 1.02,
                low: close * 0.97,
                vol: 1000000.0,
                amount: close * 1000000.0,
            })
            .collect()
    }

    #[test]
    fn test_validate_kline_continuity_normal() {
        // 正常连续数据（间隔1天）
        let dates = vec![
            create_test_datetime(2024, 1, 1),
            create_test_datetime(2024, 1, 2),
            create_test_datetime(2024, 1, 3),
            create_test_datetime(2024, 1, 4),
        ];
        let prices = vec![10.0, 10.1, 10.2, 10.3];
        let data = create_test_kline_data(dates, prices);

        let result = validate_kline_continuity(&data, "600000");

        assert!(result.is_valid());
        assert!(matches!(result.level, ValidationLevel::Ok));
    }

    #[test]
    fn test_validate_kline_continuity_missing_dates() {
        // 缺失交易日（间隔 > 3天）
        let dates = vec![
            create_test_datetime(2024, 1, 1),
            create_test_datetime(2024, 1, 6), // 间隔5天
        ];
        let prices = vec![10.0, 10.1];
        let data = create_test_kline_data(dates, prices);

        let result = validate_kline_continuity(&data, "600000");

        assert!(!result.is_valid());
        assert!(matches!(result.level, ValidationLevel::Warning(_)));
        assert!(!result.details.is_empty());
    }

    #[test]
    fn test_validate_kline_continuity_empty() {
        let data: Vec<KlineData> = vec![];
        let result = validate_kline_continuity(&data, "600000");

        assert!(!result.is_valid());
        assert!(matches!(result.level, ValidationLevel::Error(_)));
    }

    #[test]
    fn test_validate_kline_continuity_single() {
        let dates = vec![create_test_datetime(2024, 1, 1)];
        let prices = vec![10.0];
        let data = create_test_kline_data(dates, prices);

        let result = validate_kline_continuity(&data, "600000");

        // 只有一条数据应该通过（无法判断连续性）
        assert!(result.is_valid());
    }

    #[test]
    fn test_finance_consistency_normal() {
        let data = FinanceInfoData {
            code: "600000".to_string(),
            zongguben: 1000000000.0,    // 10亿股
            liutongguben: 800000000.0,  // 8亿股
            jingzichan: 50000000000.0,  // 500亿
            zongzichan: 100000000000.0, // 1000亿
            ..Default::default()
        };

        let result = validate_finance_consistency(&data);
        assert!(result.is_valid());
    }

    #[test]
    fn test_finance_consistency_invalid() {
        let data = FinanceInfoData {
            code: "600000".to_string(),
            zongguben: 1000.0,
            liutongguben: 2000.0, // 异常：大于总股本
            jingzichan: 500.0,
            zongzichan: 1000.0,
            ..Default::default()
        };

        let result = validate_finance_consistency(&data);
        assert!(!result.is_valid());
        assert!(!result.details.is_empty());
    }

    #[test]
    fn test_detect_anomalies_empty() {
        let data: Vec<KlineData> = vec![];
        let result = detect_anomalies(&data, 3.0);

        assert!(!result.is_valid());
        assert!(matches!(result.level, ValidationLevel::Error(_)));
    }

    #[test]
    fn test_detect_anomalies_insufficient_data() {
        let dates = vec![
            create_test_datetime(2024, 1, 1),
            create_test_datetime(2024, 1, 2),
        ];
        let prices = vec![10.0, 10.1];
        let data = create_test_kline_data(dates, prices);

        let result = detect_anomalies(&data, 3.0);

        assert!(!result.is_valid());
        assert!(matches!(result.level, ValidationLevel::Warning(_)));
    }

    #[test]
    fn test_detect_anomalies_price_spike() {
        let dates: Vec<DateTime> = (0..15)
            .map(|i| create_test_datetime(2024, 1, 1 + i))
            .collect();
        let prices: Vec<f64> = (0..15).map(|_| 10.0).collect();

        let mut data = create_test_kline_data(dates, prices);
        // 人为制造一个价格暴涨
        data[10].close = 15.0; // 涨幅50%，远超20%阈值
        data[10].high = 15.5;

        let result = detect_anomalies(&data, 3.0);
        assert!(!result.is_valid());
        assert!(result.details.iter().any(|s| s.contains("价格异常波动")));
    }

    #[test]
    fn test_detect_anomalies_logic_error() {
        let dates: Vec<DateTime> = (0..15)
            .map(|i| create_test_datetime(2024, 1, 1 + i))
            .collect();
        let prices: Vec<f64> = (0..15).map(|i| 10.0 + i as f64 * 0.1).collect();

        let mut data = create_test_kline_data(dates, prices);
        // 人为制造逻辑错误
        data[5].high = 9.0; // 最高价低于最低价
        data[5].low = 10.0;

        let result = detect_anomalies(&data, 3.0);
        assert!(!result.is_valid());
        assert!(
            result
                .details
                .iter()
                .any(|s| s.contains("最高价") || s.contains("OHLC"))
        );
    }

    #[test]
    fn test_kline_data_validatable() {
        let dt = create_test_datetime(2024, 1, 1);
        let data = KlineData {
            dt,
            code: "600000",
            open: 10.0,
            close: 10.5,
            high: 11.0,
            low: 9.5,
            vol: 1000000.0,
            amount: 10500000.0,
        };

        assert!(data.is_valid());
    }

    #[test]
    fn test_kline_data_invalid() {
        let dt = create_test_datetime(2024, 1, 1);
        let data = KlineData {
            dt,
            code: "600000",
            open: 10.0,
            close: 10.5,
            high: 9.0, // 错误：最高价低于收盘价
            low: 10.0,
            vol: 1000000.0,
            amount: 10500000.0,
        };

        assert!(!data.is_valid());
    }
}
