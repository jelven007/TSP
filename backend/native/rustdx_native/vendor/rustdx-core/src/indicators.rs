//! 技术指标计算库
//!
//! 提供常用的技术分析指标计算，包括：
//! - 移动平均线（SMA, EMA）
//! - MACD（指数平滑异同移动平均线）
//! - RSI（相对强弱指标）
//! - 布林带（Bollinger Bands）
//! - KDJ（随机指标）
//!
//! # 设计原则
//!
//! - 纯 Rust 实现，性能优异
//! - 类型安全，编译期检查
//! - 零外部依赖
//! - 与 K线数据无缝集成
//!
//! # 使用示例
//!
//! ```rust
//! use rustdx_complete::indicators::{sma, ema, macd, rsi};
//!
//! let closes = vec![10.0, 10.5, 11.0, 10.8, 11.2];
//!
//! // 计算简单移动平均
//! let sma_3 = sma(&closes, 3);
//!
//! // 计算指数移动平均
//! let ema_3 = ema(&closes, 3);
//!
//! // 计算 MACD
//! let macd_result = macd(&closes, 12, 26, 9);
//!
//! // 计算 RSI
//! let rsi_14 = rsi(&closes, 14);
//! ```

mod incremental;
pub use incremental::{
    EmaState, IndicatorError, MacdParameters, MacdPoint, MacdState, try_ema, try_macd,
};

// ============================================================================
// 移动平均线
// ============================================================================

/// 计算简单移动平均线 (Simple Moving Average)
///
/// # 参数
///
/// - `data`: 价格数据序列
/// - `period`: 周期（如 5, 10, 20, 60）
///
/// # 返回
///
/// 返回一个向量，前 `period-1` 个元素为 `None`（数据不足），
/// 从第 `period` 个元素开始为计算出的 SMA 值
///
/// # 示例
///
/// ```rust
/// use rustdx_complete::indicators::sma;
///
/// let data = vec![1.0, 2.0, 3.0, 4.0, 5.0];
/// let result = sma(&data, 3);
/// assert_eq!(result, vec![None, None, Some(2.0), Some(3.0), Some(4.0)]);
/// ```
///
/// # 数学公式
///
/// ```text
/// SMA = (P1 + P2 + ... + Pn) / n
///
/// 其中：
/// - P1, P2, ..., Pn 是最近 n 个周期的价格
/// - n 是周期数
/// ```
pub fn sma(data: &[f64], period: usize) -> Vec<Option<f64>> {
    if data.is_empty() {
        return vec![];
    }

    if period == 0 {
        return vec![None; data.len()];
    }

    let mut state = RollingSma::new(period);
    data.iter().map(|&value| state.update(value)).collect()
}

/// 通常 O(1) 更新的移动均值；非有限值返回None，离开窗口后恢复。
/// 累加溢出时重算当前窗口，避免后续永久失效。
pub struct RollingSma {
    period: usize,
    window: std::collections::VecDeque<f64>,
    sum: f64,
    correction: f64,
    invalid: usize,
}

impl RollingSma {
    pub fn new(period: usize) -> Self {
        Self {
            period,
            window: std::collections::VecDeque::new(),
            sum: 0.0,
            correction: 0.0,
            invalid: 0,
        }
    }
    pub fn update(&mut self, value: f64) -> Option<f64> {
        if self.period == 0 {
            return None;
        }
        if self.window.len() == self.period {
            let old = self.window.pop_front().unwrap();
            if old.is_finite() {
                Self::add(&mut self.sum, &mut self.correction, -old);
            } else {
                self.invalid -= 1;
            }
        }
        self.window.push_back(value);
        if value.is_finite() {
            Self::add(&mut self.sum, &mut self.correction, value);
        } else {
            self.invalid += 1;
        }
        if !(self.sum + self.correction).is_finite() {
            self.sum = 0.0;
            self.correction = 0.0;
            for &value in self.window.iter().filter(|v| v.is_finite()) {
                Self::add(&mut self.sum, &mut self.correction, value);
            }
        }
        let sum = self.sum + self.correction;
        (self.window.len() == self.period && self.invalid == 0 && sum.is_finite())
            .then_some(sum / self.period as f64)
    }

    // Neumaier补偿求和，保留大数进出窗口时丢失的小数项。
    fn add(sum: &mut f64, correction: &mut f64, value: f64) {
        let next = *sum + value;
        if next.is_finite() {
            *correction += if sum.abs() >= value.abs() {
                (*sum - next) + value
            } else {
                (value - next) + *sum
            };
        } else {
            *correction = 0.0;
        }
        *sum = next;
    }
}

/// 计算指数移动平均线 (Exponential Moving Average)
///
/// # 参数
///
/// - `data`: 价格数据序列
/// - `period`: 周期（如 12, 26）
///
/// # 返回
///
/// 首个有效价格作为初值；非有限输入返回 NaN 并重置，下一有效价格重新起算。
/// 周期为0时返回全NaN。返回长度始终与输入一致。
///
/// # 示例
///
/// ```rust
/// use rustdx_complete::indicators::ema;
///
/// let data = vec![22.27, 22.19, 22.08, 22.17, 22.18];
/// let result = ema(&data, 5);
/// ```
///
/// # 数学公式
///
/// ```text
/// EMA(today) = Price(today) × k + EMA(yesterday) × (1 − k)
///
/// 其中：
/// - k = 2 / (n + 1) 是平滑系数
/// - n 是周期数
/// - 本实现使用每段首个有效价格作为初始值
/// ```
pub fn ema(data: &[f64], period: usize) -> Vec<f64> {
    match EmaState::new(period) {
        Ok(mut state) => data
            .iter()
            .map(|&price| state.update(Some(price)).unwrap_or(f64::NAN))
            .collect(),
        Err(_) => vec![f64::NAN; data.len()],
    }
}

// ============================================================================
// MACD
// ============================================================================

/// MACD 指标结果
#[derive(Debug, Clone)]
pub struct MacdResult {
    /// MACD 线（快线 - 慢线）
    pub macd: Vec<Option<f64>>,
    /// 信号线（MACD 的 EMA）
    pub signal: Vec<Option<f64>>,
    /// 柱状图（MACD - Signal）
    pub histogram: Vec<Option<f64>>,
}

/// 计算 MACD 指标 (Moving Average Convergence Divergence)
///
/// # 参数
///
/// - `data`: 价格数据序列（通常是收盘价）
/// - `fast_period`: 快线周期（默认 12）
/// - `slow_period`: 慢线周期（默认 26）
/// - `signal_period`: 信号线周期（默认 9）
///
/// # 返回
///
/// 返回包含 MACD 线、信号线和柱状图的结构体。
/// 非法周期返回全None；缺口返回None并重置平滑状态，保留原始位置。
///
/// # 示例
///
/// ```rust
/// use rustdx_complete::indicators::macd;
///
/// let closes = vec![10.0, 10.5, 11.0, 10.8, 11.2, 11.5, 11.3, 11.8];
/// let result = macd(&closes, 12, 26, 9);
///
/// println!("MACD: {:?}", result.macd);
/// println!("Signal: {:?}", result.signal);
/// println!("Histogram: {:?}", result.histogram);
/// ```
///
/// # 数学公式
///
/// ```text
/// MACD Line = EMA(fast) − EMA(slow)
/// Signal Line = EMA(MACD Line, signal_period)
/// Histogram = MACD Line − Signal Line
///
/// 其中：
/// - EMA(fast) 是快速 EMA（通常 12 日）
/// - EMA(slow) 是慢速 EMA（通常 26 日）
/// - Signal 是 MACD 线的 EMA（通常 9 日）
/// ```
///
/// # 交易信号
///
/// - **金叉**：MACD 线上穿信号线 → 买入信号
/// - **死叉**：MACD 线下穿信号线 → 卖出信号
/// - **柱状图 > 0**：多头市场
/// - **柱状图 < 0**：空头市场
pub fn macd(
    data: &[f64],
    fast_period: usize,
    slow_period: usize,
    signal_period: usize,
) -> MacdResult {
    try_macd(
        data,
        MacdParameters {
            fast_period,
            slow_period,
            signal_period,
            histogram_scale: 1.0,
        },
    )
    .unwrap_or_else(|_| MacdResult {
        macd: vec![None; data.len()],
        signal: vec![None; data.len()],
        histogram: vec![None; data.len()],
    })
}

// ============================================================================
// RSI
// ============================================================================

/// 计算相对强弱指标 (Relative Strength Index)
///
/// # 参数
///
/// - `data`: 价格数据序列（通常是收盘价）
/// - `period`: 周期（默认 14）
///
/// # 返回
///
/// 返回 RSI 值向量，范围 0-100，前面不足的数据为 None
///
/// # 示例
///
/// ```rust
/// use rustdx_complete::indicators::rsi;
///
/// let closes = vec![10.0, 10.5, 11.0, 10.8, 11.2, 11.5, 11.3, 11.8, 12.0, 11.9];
/// let result = rsi(&closes, 14);
/// ```
///
/// # 数学公式
///
/// ```text
/// RSI = 100 − (100 / (1 + RS))
///
/// 其中：
/// - RS = 平均涨幅 / 平均跌幅
/// - 平均涨幅 = 最近 N 周期内涨幅之和 / N
/// - 平均跌幅 = 最近 N 周期内跌幅之和 / N
/// ```
///
/// # 交易信号
///
/// - **RSI > 70**：超买，可能回调 → 考虑卖出
/// - **RSI < 30**：超卖，可能反弹 → 考虑买入
/// - **RSI = 50**：中性区域
/// - **背离**：价格创新高但 RSI 未创新高 → 顶背离（看跌）
pub fn rsi(data: &[f64], period: usize) -> Vec<Option<f64>> {
    if period == 0 || data.len() <= period {
        return vec![None; data.len()];
    }
    let mut result = vec![None; data.len()];
    let mut gains = RollingSma::new(period);
    let mut losses = RollingSma::new(period);
    for (i, pair) in data.windows(2).enumerate() {
        let change = pair[1] - pair[0];
        let gain = if change.is_finite() {
            change.max(0.0)
        } else {
            f64::NAN
        };
        let loss = if change.is_finite() {
            (-change).max(0.0)
        } else {
            f64::NAN
        };
        if let (Some(gain), Some(loss)) = (gains.update(gain), losses.update(loss)) {
            result[i + 1] = Some(if loss <= f64::EPSILON {
                if gain <= f64::EPSILON { 50.0 } else { 100.0 }
            } else {
                (100.0 * gain / (gain + loss)).clamp(0.0, 100.0)
            });
        }
    }

    result
}

// ============================================================================
// 布林带
// ============================================================================

/// 布林带指标结果
#[derive(Debug, Clone)]
pub struct BollingerBandsResult {
    /// 上轨
    pub upper: Vec<Option<f64>>,
    /// 中轨（SMA）
    pub middle: Vec<Option<f64>>,
    /// 下轨
    pub lower: Vec<Option<f64>>,
}

// Two aggregate stacks form a queue: each value is pushed/transferred/popped once.
// Shifted Chan moments avoid subtracting two large squared sums. Removing a value
// restores a stored aggregate rather than accumulating subtraction error.
#[derive(Clone, Copy, Default)]
struct Moments {
    count: usize,
    invalid: usize,
    origin: f64,
    mean: f64,
    m2: f64,
}

impl Moments {
    fn value(value: f64) -> Self {
        if value.is_finite() {
            Self {
                count: 1,
                origin: value,
                ..Self::default()
            }
        } else {
            Self {
                invalid: 1,
                ..Self::default()
            }
        }
    }

    fn merge(self, other: Self) -> Self {
        let invalid = self.invalid + other.invalid;
        if self.count == 0 {
            return Self { invalid, ..other };
        }
        if other.count == 0 {
            return Self { invalid, ..self };
        }
        let count = self.count + other.count;
        let delta = (other.origin - self.origin) + (other.mean - self.mean);
        let weight = other.count as f64 / count as f64;
        Self {
            count,
            invalid,
            origin: self.origin,
            mean: self.mean + delta * weight,
            m2: self.m2 + other.m2 + delta * (delta * weight) * self.count as f64,
        }
    }
}

struct RollingVariance {
    front: Vec<(f64, Moments)>,
    back: Vec<(f64, Moments)>,
    period: usize,
}

impl RollingVariance {
    fn new(period: usize) -> Self {
        Self {
            front: vec![],
            back: vec![],
            period,
        }
    }

    fn aggregate(stack: &[(f64, Moments)]) -> Moments {
        stack
            .last()
            .map(|(_, moments)| *moments)
            .unwrap_or_default()
    }

    fn push(stack: &mut Vec<(f64, Moments)>, value: f64) {
        stack.push((value, Self::aggregate(stack).merge(Moments::value(value))));
    }

    fn update(&mut self, value: f64) -> Option<f64> {
        if self.front.len() + self.back.len() == self.period {
            if self.front.is_empty() {
                while let Some((value, _)) = self.back.pop() {
                    Self::push(&mut self.front, value);
                }
            }
            self.front.pop();
        }
        Self::push(&mut self.back, value);
        let moments = Self::aggregate(&self.front).merge(Self::aggregate(&self.back));
        let variance = moments.m2 / self.period as f64;
        (moments.count == self.period && moments.invalid == 0 && variance.is_finite())
            .then_some(variance.max(0.0))
    }
}

/// 计算布林带 (Bollinger Bands)
///
/// # 参数
///
/// - `data`: 价格数据序列（通常是收盘价）
/// - `period`: 周期（默认 20）
/// - `std_dev`: 标准差倍数（默认 2.0）
///
/// # 返回
///
/// 返回包含上轨、中轨、下轨的结构体。
/// 周期小于2或倍数非有限/负数时全None；窗口缺口或溢出时相应轨道为None。
///
/// # 示例
///
/// ```rust
/// use rustdx_complete::indicators::bollinger_bands;
///
/// let closes = vec![10.0, 10.5, 11.0, 10.8, 11.2];
/// let result = bollinger_bands(&closes, 20, 2.0);
///
/// println!("上轨: {:?}", result.upper);
/// println!("中轨: {:?}", result.middle);
/// println!("下轨: {:?}", result.lower);
/// ```
///
/// # 数学公式
///
/// ```text
/// 中轨 = SMA(period)
/// 上轨 = 中轨 + (std_dev × 标准差)
/// 下轨 = 中轨 − (std_dev × 标准差)
///
/// 其中：
/// - std_dev 通常是 2（即 2 倍标准差）
/// ```
///
/// # 交易信号
///
/// - **价格触及上轨**：可能超买 → 考虑卖出
/// - **价格触及下轨**：可能超卖 → 考虑买入
/// - **布林带收口**：波动率降低，即将突破
/// - **布林带开口**：波动率增加，趋势开始
pub fn bollinger_bands(
    data: &[f64],
    period: usize,
    std_dev_multiplier: f64,
) -> BollingerBandsResult {
    if period < 2 || !std_dev_multiplier.is_finite() || std_dev_multiplier < 0.0 {
        return BollingerBandsResult {
            upper: vec![None; data.len()],
            middle: vec![None; data.len()],
            lower: vec![None; data.len()],
        };
    }

    // 计算中轨（SMA）
    let sma_values = sma(data, period);

    let mut variance = RollingVariance::new(period);
    let mut upper_band = Vec::with_capacity(data.len());
    let mut lower_band = Vec::with_capacity(data.len());
    for (&value, &middle) in data.iter().zip(&sma_values) {
        let spread = variance
            .update(value)
            .map(|v| std_dev_multiplier * v.sqrt());
        let bands = middle.zip(spread);
        upper_band.push(
            bands.and_then(|(mean, spread)| (mean + spread).is_finite().then_some(mean + spread)),
        );
        lower_band.push(
            bands.and_then(|(mean, spread)| (mean - spread).is_finite().then_some(mean - spread)),
        );
    }

    BollingerBandsResult {
        upper: upper_band,
        middle: sma_values,
        lower: lower_band,
    }
}

// ============================================================================
// KDJ 随机指标
// ============================================================================

/// KDJ 指标结果
#[derive(Debug, Clone)]
pub struct KdjResult {
    /// K 值（快速确认线）
    pub k: Vec<Option<f64>>,
    /// D 值（慢速确认线）
    pub d: Vec<Option<f64>>,
    /// J 值（提前反应线）
    pub j: Vec<Option<f64>>,
}

/// 计算随机指标 (KDJ - Stochastic Oscillator)
///
/// # 参数
///
/// - `high`: 最高价序列
/// - `low`: 最低价序列
/// - `close`: 收盘价序列
/// - `k_period`: RSV 最高/最低价窗口（通常 9）
/// - `d_period`: K 和 D 共用的 EMA 平滑周期（通常 3）
/// - `j_multiplier`: J 线倍数（通常 3）
///
/// # 返回
///
/// 返回包含 K、D、J 三条线的结构体
///
/// # 示例
///
/// ```rust
/// use rustdx_complete::indicators::kdj;
///
/// let highs = vec![11.0, 11.2, 11.5, 11.3, 11.8];
/// let lows = vec![10.0, 10.5, 10.8, 10.6, 11.0];
/// let closes = vec![10.5, 11.0, 11.2, 11.1, 11.5];
///
/// let result = kdj(&highs, &lows, &closes, 9, 3, 3);
/// ```
///
/// # 数学公式
///
/// ```text
/// RSV = (收盘价 − 最低价) / (最高价 − 最低价) × 100
/// alpha = 2 / (d_period + 1)
/// K = K_prev + alpha × (RSV − K_prev)
/// D = D_prev + alpha × (K − D_prev)
/// J = j_multiplier × K − (j_multiplier − 1) × D
/// ```
///
/// 每段首个有效RSV初始化K和D；缺口重置状态。此口径不同于部分软件
/// 使用 alpha=1/3、初值50的默认KDJ，回测时须保持一致。
///
/// # 交易信号
///
/// - **K > D**：上升趋势 → 持有或买入
/// - **K < D**：下降趋势 → 考虑卖出
/// - **K < 20 且 D < 20**：超卖区 → 买入信号
/// - **K > 80 且 D > 80**：超买区 → 卖出信号
/// - **J > 100**：严重超买 → 强烈卖出信号
/// - **J < 0**：严重超卖 → 强烈买入信号
pub fn kdj(
    high: &[f64],
    low: &[f64],
    close: &[f64],
    k_period: usize,
    d_period: usize,
    j_multiplier: usize,
) -> KdjResult {
    let len = close.len();

    if len == 0 {
        return KdjResult {
            k: vec![],
            d: vec![],
            j: vec![],
        };
    }

    if len != high.len() || len != low.len() || k_period == 0 || d_period == 0 || j_multiplier == 0
    {
        return KdjResult {
            k: vec![None; len],
            d: vec![None; len],
            j: vec![None; len],
        };
    }

    // 计算 RSV (Raw Stochastic Value)
    let mut rsv_values = Vec::with_capacity(len);
    let mut maxima = std::collections::VecDeque::<usize>::new();
    let mut minima = std::collections::VecDeque::<usize>::new();
    let mut last_invalid = None;
    for i in 0..len {
        let start = (i + 1).saturating_sub(k_period);
        while maxima.front().is_some_and(|&index| index < start) {
            maxima.pop_front();
        }
        while minima.front().is_some_and(|&index| index < start) {
            minima.pop_front();
        }
        if !high[i].is_finite()
            || !low[i].is_finite()
            || !close[i].is_finite()
            || high[i] < low[i]
            || close[i] < low[i]
            || close[i] > high[i]
        {
            last_invalid = Some(i);
        } else {
            while maxima.back().is_some_and(|&index| high[index] <= high[i]) {
                maxima.pop_back();
            }
            while minima.back().is_some_and(|&index| low[index] >= low[i]) {
                minima.pop_back();
            }
            maxima.push_back(i);
            minima.push_back(i);
        }
        if i + 1 < k_period {
            rsv_values.push(None);
        } else {
            if last_invalid.is_some_and(|index| index >= start) {
                rsv_values.push(None);
                continue;
            }

            let highest_high = high[*maxima.front().unwrap()];
            let lowest_low = low[*minima.front().unwrap()];

            if highest_high - lowest_low > 0.0 {
                let rsv = (close[i] - lowest_low) / (highest_high - lowest_low) * 100.0;
                rsv_values.push(rsv.is_finite().then_some(rsv));
            } else {
                rsv_values.push(Some(50.0)); // 最高价 = 最低价时，RSV = 50
            }
        }
    }

    let mut k_line = vec![None; len];
    let mut d_line = vec![None; len];
    let mut state = None;
    let alpha = 2.0 / (d_period as f64 + 1.0);
    for (i, rsv) in rsv_values.into_iter().enumerate() {
        if let Some(rsv) = rsv {
            let (k, d) = state.unwrap_or((rsv, rsv));
            let k = k + alpha * (rsv - k);
            let d = d + alpha * (k - d);
            state = Some((k, d));
            k_line[i] = Some(k);
            d_line[i] = Some(d);
        } else {
            state = None;
        }
    }

    // 计算 J 值
    let mut j_line = Vec::new();
    for (k_val, d_val) in k_line.iter().zip(d_line.iter()) {
        match (k_val, d_val) {
            (Some(k), Some(d)) => {
                let j = j_multiplier as f64 * k - (j_multiplier as f64 - 1.0) * d;
                j_line.push(j.is_finite().then_some(j));
            }
            _ => {
                j_line.push(None);
            }
        }
    }

    KdjResult {
        k: k_line,
        d: d_line,
        j: j_line,
    }
}

// ============================================================================
// 单元测试
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rolling_bands_match_centered_reference_and_recover_from_overflow() {
        let data: Vec<_> = (0..10_000)
            .map(|i| 1e12 + ((i * 17 % 127) as f64 - 63.0) * 0.125)
            .collect();
        for period in [2, 9, 200, 2000] {
            let result = bollinger_bands(&data, period, 2.0);
            for (i, window) in data.windows(period).enumerate().step_by(17) {
                let origin = window[0];
                let mean = window.iter().map(|v| v - origin).sum::<f64>() / period as f64;
                let variance = window
                    .iter()
                    .map(|v| (v - origin - mean).powi(2))
                    .sum::<f64>()
                    / period as f64;
                let index = i + period - 1;
                let expected = origin + mean + 2.0 * variance.sqrt();
                assert!((result.upper[index].unwrap() - expected).abs() <= 0.0005);
            }
        }
        let bands = bollinger_bands(&[f64::MAX, -f64::MAX, f64::NAN, 1.0, 2.0, 3.0], 2, 2.0);
        assert!(bands.upper[1..4].iter().all(Option::is_none));
        assert_eq!(bands.upper[5], Some(3.5));
        assert_eq!(bollinger_bands(&[42.0; 100], 20, 2.0).upper[99], Some(42.0));
    }

    #[test]
    fn monotonic_kdj_matches_scanned_windows_including_invalid_bars() {
        let mut close: Vec<_> = (0..1000).map(|i| 10.0 + (i as f64 * 0.3).sin()).collect();
        let high: Vec<_> = close.iter().map(|v| v + 1.0).collect();
        let low: Vec<_> = close.iter().map(|v| v - 1.0).collect();
        close[37] = f64::NAN;
        close[500] = 1000.0;
        for period in [1, 9, 200, 2000] {
            let result = kdj(&high, &low, &close, period, 3, 3);
            let mut state = None;
            for i in 0..close.len() {
                let rsv = if i + 1 < period {
                    None
                } else {
                    let start = i + 1 - period;
                    if (start..=i)
                        .any(|j| !close[j].is_finite() || close[j] < low[j] || close[j] > high[j])
                    {
                        None
                    } else {
                        let hi = high[start..=i]
                            .iter()
                            .copied()
                            .fold(f64::NEG_INFINITY, f64::max);
                        let lo = low[start..=i].iter().copied().fold(f64::INFINITY, f64::min);
                        Some((close[i] - lo) / (hi - lo) * 100.0)
                    }
                };
                if let Some(rsv) = rsv {
                    let (k, d) = state.unwrap_or((rsv, rsv));
                    let k = k + 0.5 * (rsv - k);
                    let d = d + 0.5 * (k - d);
                    assert_eq!(result.k[i], Some(k));
                    assert_eq!(result.d[i], Some(d));
                    assert_eq!(result.j[i], Some(3.0 * k - 2.0 * d));
                    state = Some((k, d));
                } else {
                    assert_eq!(result.k[i], None);
                    state = None;
                }
            }
        }
    }

    #[test]
    fn indicator_gaps_preserve_alignment_and_recover() {
        let data = [10.0, 12.0, f64::NAN, 20.0, 22.0];
        let values = ema(&data, 3);
        assert_eq!(&values[..2], &[10.0, 11.0]);
        assert!(values[2].is_nan());
        assert_eq!(&values[3..], &[20.0, 21.0]);
        assert!(ema(&data, 0).iter().all(|v| v.is_nan()));
        let values = macd(&data, 2, 3, 2);
        assert_eq!(values.macd[2], None);
        assert_eq!(values.signal[2], None);
        assert_eq!(values.histogram[2], None);
        assert_eq!(values.macd[3], Some(0.0));
        assert_eq!(values.signal[3], Some(0.0));
        assert!(macd(&data, 0, 3, 2).macd.iter().all(Option::is_none));
        for multiplier in [f64::NAN, f64::INFINITY, -1.0] {
            let bands = bollinger_bands(&data, 2, multiplier);
            assert!(
                bands
                    .upper
                    .iter()
                    .chain(&bands.middle)
                    .chain(&bands.lower)
                    .all(Option::is_none)
            );
        }
        let values = sma(&[f64::MAX, f64::MAX, 1.0, 2.0, 3.0], 2);
        assert_eq!(values[1], None);
        assert_eq!(values[4], Some(2.5));
        let values = kdj(
            &[11.0; 5],
            &[9.0; 5],
            &[10.0, 12.0, 10.0, 10.0, 10.0],
            2,
            3,
            3,
        );
        assert_eq!(values.k[..3], [None, None, None]);
        assert_eq!(values.k[3], Some(50.0));
    }

    #[test]
    fn rolling_values_match_reference_and_recover_after_gaps() {
        let data: Vec<_> = (0..100).map(|i| 10.0 + (i as f64 * 0.3).sin()).collect();
        for period in [1, 5, 20] {
            let result = sma(&data, period);
            for (i, window) in data.windows(period).enumerate() {
                let reference = window.iter().sum::<f64>() / period as f64;
                assert!((result[i + period - 1].unwrap() - reference).abs() < 1e-12);
            }
        }
        assert_eq!(rsi(&[10.0; 20], 14)[14], Some(50.0));
        assert!(rsi(&data, usize::MAX).iter().all(Option::is_none));
        assert!(rsi(&data, 0).iter().all(Option::is_none));
        let result = sma(&[1.0, f64::NAN, 3.0, 4.0], 2);
        assert_eq!(result, vec![None, None, None, Some(3.5)]);
        let kdj = kdj(&[11.0; 3], &[9.0; 3], &[10.0; 3], 9, 3, 3);
        assert_eq!(kdj.k, vec![None; 3]);
        assert_eq!(kdj.d, vec![None; 3]);
    }

    #[test]
    fn test_sma() {
        let data = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        let result = sma(&data, 3);

        assert_eq!(result.len(), 5);
        assert_eq!(result[0], None);
        assert_eq!(result[1], None);
        assert_eq!(result[2], Some(2.0));
        assert_eq!(result[3], Some(3.0));
        assert_eq!(result[4], Some(4.0));
    }

    #[test]
    fn test_ema() {
        let data = vec![22.27, 22.19, 22.08, 22.17, 22.18];
        let result = ema(&data, 5);

        assert_eq!(result.len(), 5);
        assert!((result[0] - 22.27).abs() < 0.01); // 第一个值是第一个价格
        // 后续值应该是递归计算的 EMA
        assert!(result[1..].iter().all(|&v| v > 0.0));
    }

    #[test]
    fn test_macd() {
        let data = vec![
            10.0, 10.5, 11.0, 10.8, 11.2, 11.5, 11.3, 11.8, 12.0, 11.9, 12.2, 12.5, 12.3, 12.8,
            13.0,
        ];

        let result = macd(&data, 12, 26, 9);

        // 检查返回值结构
        assert_eq!(result.macd.len(), data.len());
        assert_eq!(result.signal.len(), data.len());
        assert_eq!(result.histogram.len(), data.len());

        // 检查是否有值
        let has_values = result.macd.iter().any(|&v| v.is_some());
        assert!(has_values);
    }

    #[test]
    fn test_rsi() {
        let data = vec![
            10.0, 10.5, 11.0, 10.8, 11.2, 11.5, 11.3, 11.8, 12.0, 11.9, 12.2, 12.5, 12.3, 12.8,
            13.0,
        ];

        let period = 14;
        let result = rsi(&data, period);

        assert_eq!(result.len(), data.len());

        // 前面应该有 None
        assert_eq!(
            result[..period].iter().filter(|&&v| v.is_none()).count(),
            period
        );

        // 后面应该有值
        let has_values = result[period..].iter().any(|&v| v.is_some());
        assert!(has_values);

        // RSI 应该在 0-100 范围内
        for value in result.iter().filter_map(|&v| v) {
            assert!((0.0..=100.0).contains(&value));
        }
    }

    #[test]
    fn test_rsi_extreme_cases() {
        // 所有价格上涨
        let rising: Vec<f64> = (0..20).map(|i| 10.0 + i as f64).collect();
        let result = rsi(&rising, 14);
        if let Some(&Some(value)) = result.last() {
            assert_eq!(value, 100.0); // 应该接近 100
        }

        // 所有价格下跌
        let falling: Vec<f64> = (0..20).map(|i| 30.0 - i as f64).collect();
        let result = rsi(&falling, 14);
        if let Some(&Some(value)) = result.last() {
            assert!(value < 50.0); // 应该小于 50
        }
    }

    #[test]
    fn test_bollinger_bands() {
        let data = vec![
            10.0, 10.5, 11.0, 10.8, 11.2, 11.5, 11.3, 11.8, 12.0, 11.9, 12.2, 12.5, 12.3, 12.8,
            13.0,
        ];

        let result = bollinger_bands(&data, 5, 2.0);

        // 检查结构
        assert_eq!(result.upper.len(), data.len());
        assert_eq!(result.middle.len(), data.len());
        assert_eq!(result.lower.len(), data.len());

        // 检查是否有值
        let has_values = result.upper.iter().any(|&v| v.is_some());
        assert!(has_values);

        // 检查上轨 >= 中轨 >= 下轨
        for i in 0..data.len() {
            if let (Some(u), Some(m), Some(l)) =
                (result.upper[i], result.middle[i], result.lower[i])
            {
                assert!(u >= m);
                assert!(m >= l);
            }
        }
    }

    #[test]
    fn test_kdj() {
        let highs: Vec<f64> = (0..20).map(|i| 10.0 + i as f64 * 0.1 + 0.5).collect();
        let lows: Vec<f64> = (0..20).map(|i| 10.0 + i as f64 * 0.1 - 0.5).collect();
        let closes: Vec<f64> = (0..20).map(|i| 10.0 + i as f64 * 0.1).collect();

        let result = kdj(&highs, &lows, &closes, 9, 3, 3);

        // 检查结构
        assert_eq!(result.k.len(), closes.len());
        assert_eq!(result.d.len(), closes.len());
        assert_eq!(result.j.len(), closes.len());

        // 检查是否有值
        let has_values = result.k.iter().any(|&v| v.is_some());
        assert!(has_values);

        // KDJ 应该在 0-100 范围内（J 可能超出）
        for value in result.k.iter().filter_map(|&v| v) {
            assert!((0.0..=100.0).contains(&value));
        }
        for value in result.d.iter().filter_map(|&v| v) {
            assert!((0.0..=100.0).contains(&value));
        }
    }

    #[test]
    fn test_empty_data() {
        let data: Vec<f64> = vec![];

        assert_eq!(sma(&data, 5), vec![None; 0]);
        assert!(ema(&data, 5).is_empty());
        assert_eq!(rsi(&data, 14), vec![None; 0]);
    }

    #[test]
    fn test_single_value() {
        let data = vec![10.0];

        let sma_result = sma(&data, 5);
        // SMA 需要至少 period 个数据才能开始计算
        // 对于单个值和 period=5，返回 None
        assert_eq!(sma_result.len(), 1);
        assert_eq!(sma_result[0], None);

        let ema_result = ema(&data, 5);
        // EMA 对于单个值直接返回该值
        assert_eq!(ema_result.len(), 1);
        assert_eq!(ema_result[0], 10.0);
    }
}
