//! 统一行情语义。代码必须显式指定市场；价格/金额为元，标准成交量为股。
use std::{fmt, str::FromStr};

use chrono::{FixedOffset, NaiveDate, NaiveDateTime};
use serde::Serialize;

use crate::{Error, Result, file::day, tcp::stock::KlineData};

mod model;
pub use model::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Market {
    Shanghai,
    Shenzhen,
    Beijing,
}

impl Market {
    /// 当前股票行情协议仅核实了深圳0和上海1，北交所不隐式映射。
    pub fn tdx_market(self) -> Result<u16> {
        match self {
            Self::Shenzhen => Ok(0),
            Self::Shanghai => Ok(1),
            Self::Beijing => Err(Error::Custom("当前行情协议未核实北交所市场映射")),
        }
    }

    pub fn prefix(self) -> &'static str {
        match self {
            Self::Shanghai => "sh",
            Self::Shenzhen => "sz",
            Self::Beijing => "bj",
        }
    }
}

/// 保留前导零及六位字母数字代码，不根据首位猜测交易所。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize)]
pub struct Symbol {
    market: Market,
    code: String,
}

impl Symbol {
    pub fn new(market: Market, code: &str) -> Result<Self> {
        if code.len() != 6 || !code.bytes().all(|b| b.is_ascii_alphanumeric()) {
            return Err(Error::Custom("证券代码必须为六位ASCII字母数字"));
        }
        Ok(Self {
            market,
            code: code.to_owned(),
        })
    }

    pub fn market(&self) -> Market {
        self.market
    }

    pub fn code(&self) -> &str {
        &self.code
    }

    /// 按交易所代码段判断是否为沪深京 A 股；指数、基金、债券等返回 false。
    pub fn is_a_share(&self) -> bool {
        if !self.code.bytes().all(|b| b.is_ascii_digit()) {
            return false;
        }
        match self.market {
            Market::Shanghai => ["600", "601", "603", "605", "688", "689"]
                .iter()
                .any(|prefix| self.code.starts_with(prefix)),
            Market::Shenzhen => ["000", "001", "002", "003", "300", "301", "302"]
                .iter()
                .any(|prefix| self.code.starts_with(prefix)),
            Market::Beijing => {
                self.code.starts_with('4')
                    || self.code.starts_with('8')
                    || self.code.starts_with("92")
            }
        }
    }
}

impl FromStr for Symbol {
    type Err = Error;

    fn from_str(value: &str) -> Result<Self> {
        if !value.is_ascii() || value.len() != 8 {
            return Err(Error::Custom("完整证券代码应为市场前缀加六位代码"));
        }
        let market = match &value[..2] {
            "sh" => Market::Shanghai,
            "sz" => Market::Shenzhen,
            "bj" => Market::Beijing,
            _ => return Err(Error::Custom("未知证券市场")),
        };
        Self::new(market, &value[2..])
    }
}

impl fmt::Display for Symbol {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}{}", self.market.prefix(), self.code)
    }
}

#[derive(Debug, Clone, Copy, Serialize)]
pub enum VolumeUnit {
    Shares,
    /// 100股/手；用于现有日线 CSV 及通达信股票K线。
    Lots,
}

impl VolumeUnit {
    pub fn to_shares(self, volume: f64) -> f64 {
        match self {
            Self::Shares => volume,
            Self::Lots => volume * 100.0,
        }
    }
}

pub fn exchange_timezone() -> FixedOffset {
    FixedOffset::east_opt(8 * 3600).expect("UTC+8")
}

pub fn date_from_u32(value: u32) -> Result<NaiveDate> {
    NaiveDate::from_ymd_opt((value / 10000) as i32, value / 100 % 100, value % 100)
        .ok_or(Error::Custom("非法行情日期"))
}

/// 带市场的标准K线，时间为上海交易所本地时间（UTC+8）。
#[derive(Debug, Clone, Serialize)]
pub struct Bar {
    pub symbol: Symbol,
    pub datetime: NaiveDateTime,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume_shares: f64,
    pub amount: f64,
}

impl Bar {
    /// 兼容A股日线的手单位转换。此记录不含周期，分钟线及非A股
    /// 必须用from_kline_with_unit或CanonicalBar显式指定源单位。
    pub fn from_kline(value: &KlineData<'_>, market: Market) -> Result<Self> {
        if !Symbol::new(market, value.code)?.is_a_share() {
            return Err(Error::Custom("非A股TCP行情须显式指定成交量单位"));
        }
        Self::from_kline_with_unit(value, market, VolumeUnit::Lots)
    }

    pub fn from_kline_with_unit(
        value: &KlineData<'_>,
        market: Market,
        unit: VolumeUnit,
    ) -> Result<Self> {
        market.tdx_market()?;
        let bar = Self {
            symbol: Symbol::new(market, value.code)?,
            datetime: value.dt.try_into_naive()?,
            open: value.open,
            high: value.high,
            low: value.low,
            close: value.close,
            volume_shares: unit.to_shares(value.vol),
            amount: value.amount,
        };
        bar.validate()?;
        Ok(bar)
    }

    pub fn from_day(value: &day::Day) -> Result<Self> {
        let bar = Self {
            symbol: value.code.parse()?,
            datetime: date_from_u32(value.date)?
                .and_hms_opt(15, 0, 0)
                .expect("固定收盘时间15:00"),
            open: value.open,
            high: value.high,
            low: value.low,
            close: value.close,
            volume_shares: value.vol as f64,
            amount: value.amount,
        };
        bar.validate()?;
        Ok(bar)
    }

    pub fn validate(&self) -> Result<()> {
        validate_ohlcv(
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume_shares,
            self.amount,
        )
    }
}

pub fn validate_ohlcv(
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    volume: f64,
    amount: f64,
) -> Result<()> {
    validate_bar_values(open, high, low, close, volume, amount)?;
    if [open, high, low, close].iter().any(|price| *price <= 0.0)
        || high < low
        || open < low
        || open > high
        || close < low
        || close > high
    {
        return Err(Error::Custom("OHLC价格范围非法"));
    }
    Ok(())
}

/// 校验所有品种通用的数值约束，不假定非股票品种的历史 OHLC 区间严格一致。
pub fn validate_bar_values(
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    volume: f64,
    amount: f64,
) -> Result<()> {
    if ![open, high, low, close, volume, amount]
        .iter()
        .all(|n| n.is_finite())
    {
        return Err(Error::Custom("行情包含NaN或无限值"));
    }
    if [open, high, low, close].iter().any(|price| *price < 0.0) {
        return Err(Error::Custom("价格不能为负"));
    }
    if volume < 0.0 || amount < 0.0 {
        return Err(Error::Custom("成交量或成交额为负"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn symbols_and_units_are_explicit() {
        let stock = "sz000001".parse::<Symbol>().unwrap();
        assert_eq!(stock.code(), "000001");
        assert!(stock.is_a_share());
        assert!(!"sh000001".parse::<Symbol>().unwrap().is_a_share());
        assert!("bj920001".parse::<Symbol>().unwrap().is_a_share());
        assert!("sz200b07".parse::<Symbol>().is_ok());
        assert!(!"sz000abc".parse::<Symbol>().unwrap().is_a_share());
        assert!(!"sh600abc".parse::<Symbol>().unwrap().is_a_share());
        assert!(!"bj920abc".parse::<Symbol>().unwrap().is_a_share());
        for s in ["000001", "xx600000", "sh中文", "sh../abc"] {
            assert!(s.parse::<Symbol>().is_err());
        }
        assert_eq!(VolumeUnit::Lots.to_shares(12.5), 1250.0);
        assert!(date_from_u32(20250229).is_err());
        assert!(validate_ohlcv(f64::NAN, 11.0, 9.0, 10.0, 1.0, 1.0).is_err());
    }
}
