//! 中国A股交易日历。所有查询都校验实际数据覆盖范围，未知年份返回错误。
//! 内置数据覆盖2024–2026；其他区间通过 [`HolidayCalendar`] 提供完整休市表。
//!
//! ```rust
//! use rustdx_complete::calendar::TradingCalendar;
//! use chrono::NaiveDate;
//! # fn example() -> rustdx_complete::Result<()> {
//! let date = NaiveDate::from_ymd_opt(2025, 1, 6).unwrap();
//! assert!(TradingCalendar::is_trading_day(&date)?);
//! let next = TradingCalendar::next_trading_day(&date)?;
//! assert_eq!(next, NaiveDate::from_ymd_opt(2025, 1, 7).unwrap());
//! let unknown = NaiveDate::from_ymd_opt(2027, 1, 1).unwrap();
//! assert!(TradingCalendar::is_trading_day(&unknown).is_err());
//! # Ok(())
//! # }
//! # example().unwrap();
//! ```

use chrono::{NaiveDate, NaiveDateTime, Timelike};

mod provider;
pub use provider::*;

/// 内置中国A股交易日历。2.0起，原非checked名称也返回Result；
/// `try_*`保留为同语义入口，方便已有严格调用方迁移。
pub struct TradingCalendar;

impl TradingCalendar {
    pub fn supported_range() -> (NaiveDate, NaiveDate) {
        let metadata = BuiltinCalendar.metadata();
        (metadata.start, metadata.end)
    }

    pub fn try_is_trading_day(date: &NaiveDate) -> crate::Result<bool> {
        Ok(BuiltinCalendar.is_trading_day(*date)?)
    }

    pub fn try_previous_trading_day(date: &NaiveDate) -> crate::Result<NaiveDate> {
        Ok(BuiltinCalendar.previous_trading_day(*date)?)
    }

    pub fn try_next_trading_day(date: &NaiveDate) -> crate::Result<NaiveDate> {
        Ok(BuiltinCalendar.next_trading_day(*date)?)
    }

    pub fn try_get_trading_days(date: &NaiveDate, count: usize) -> crate::Result<Vec<NaiveDate>> {
        Ok(BuiltinCalendar.get_trading_days(*date, count)?)
    }

    pub fn try_get_trading_days_before(
        date: &NaiveDate,
        count: usize,
    ) -> crate::Result<Vec<NaiveDate>> {
        Ok(BuiltinCalendar.get_trading_days_before(*date, count)?)
    }

    /// UTC+8本地时间，9:30之前取前一个交易日；跨覆盖范围返回错误。
    pub fn try_market_trading_day_at(datetime: NaiveDateTime) -> crate::Result<NaiveDate> {
        Ok(BuiltinCalendar.market_trading_day_at(datetime)?)
    }

    /// 返回闭区间内交易日；颠倒区间、覆盖不足均返回错误。
    pub fn trading_days_between(start: NaiveDate, end: NaiveDate) -> crate::Result<Vec<NaiveDate>> {
        Ok(BuiltinCalendar.trading_days_between(start, end)?)
    }

    /// true为已知交易日，false为已知休市日；未知日期返回错误。
    pub fn is_trading_day(date: &NaiveDate) -> crate::Result<bool> {
        Self::try_is_trading_day(date)
    }

    /// UTC+8交易时间9:30–11:30、13:00–15:00，包含收盘端点。
    pub fn is_trading_time(datetime: &NaiveDateTime) -> crate::Result<bool> {
        if !Self::is_trading_day(&datetime.date())? {
            return Ok(false);
        }
        let seconds = datetime.time().num_seconds_from_midnight();
        Ok((34200..=41400).contains(&seconds) || (46800..=54000).contains(&seconds))
    }

    pub fn previous_trading_day(date: &NaiveDate) -> crate::Result<NaiveDate> {
        Self::try_previous_trading_day(date)
    }

    pub fn next_trading_day(date: &NaiveDate) -> crate::Result<NaiveDate> {
        Self::try_next_trading_day(date)
    }

    /// 含起始日，返回升序列表；覆盖不足不会交付部分结果。
    pub fn get_trading_days(start: &NaiveDate, count: usize) -> crate::Result<Vec<NaiveDate>> {
        Self::try_get_trading_days(start, count)
    }

    /// 含结束日，返回升序列表；覆盖不足不会交付部分结果。
    pub fn get_trading_days_before(end: &NaiveDate, count: usize) -> crate::Result<Vec<NaiveDate>> {
        Self::try_get_trading_days_before(end, count)
    }

    pub fn count_trading_days(start: &NaiveDate, end: &NaiveDate) -> crate::Result<usize> {
        Ok(Self::trading_days_between(*start, *end)?.len())
    }

    /// 如果今天休市，取下一个已知交易日。
    pub fn current_trading_day() -> crate::Result<NaiveDate> {
        let today = chrono::Utc::now()
            .with_timezone(&crate::data::exchange_timezone())
            .date_naive();
        if Self::is_trading_day(&today)? {
            Ok(today)
        } else {
            Self::next_trading_day(&today)
        }
    }

    pub fn market_trading_day() -> crate::Result<NaiveDate> {
        Self::market_trading_day_at(
            chrono::Utc::now()
                .with_timezone(&crate::data::exchange_timezone())
                .naive_local(),
        )
    }

    pub fn market_trading_day_at(datetime: NaiveDateTime) -> crate::Result<NaiveDate> {
        Self::try_market_trading_day_at(datetime)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn date(value: &str) -> NaiveDate {
        value.parse().unwrap()
    }

    #[test]
    fn known_holidays_navigation_and_lists_keep_their_values() {
        assert!(!TradingCalendar::is_trading_day(&date("2025-01-01")).unwrap());
        assert!(!TradingCalendar::is_trading_day(&date("2025-01-04")).unwrap());
        assert!(TradingCalendar::is_trading_day(&date("2025-01-06")).unwrap());
        assert_eq!(
            TradingCalendar::next_trading_day(&date("2025-01-03")).unwrap(),
            date("2025-01-06")
        );
        assert_eq!(
            TradingCalendar::previous_trading_day(&date("2025-01-06")).unwrap(),
            date("2025-01-03")
        );
        let days: Vec<_> = (6..=10)
            .map(|day| NaiveDate::from_ymd_opt(2025, 1, day).unwrap())
            .collect();
        assert_eq!(
            TradingCalendar::get_trading_days(&days[0], 5).unwrap(),
            days
        );
        assert_eq!(
            TradingCalendar::get_trading_days_before(&days[4], 5).unwrap(),
            days
        );
        assert_eq!(
            TradingCalendar::count_trading_days(&days[0], &days[4]).unwrap(),
            5
        );
    }

    #[test]
    fn all_entry_points_reject_unknown_dates_without_fallback() {
        for day in [
            date("2023-12-29"),
            date("2027-01-01"),
            NaiveDate::MIN,
            NaiveDate::MAX,
        ] {
            assert!(matches!(
                TradingCalendar::is_trading_day(&day),
                Err(crate::Error::Calendar(CalendarError::OutOfRange { .. }))
            ));
            assert!(TradingCalendar::previous_trading_day(&day).is_err());
            assert!(TradingCalendar::next_trading_day(&day).is_err());
            assert!(TradingCalendar::get_trading_days(&day, 0).is_err());
            assert!(TradingCalendar::get_trading_days_before(&day, 1).is_err());
            assert!(TradingCalendar::count_trading_days(&day, &day).is_err());
            let time = day.and_hms_opt(10, 0, 0).unwrap();
            assert!(TradingCalendar::is_trading_time(&time).is_err());
            assert!(TradingCalendar::market_trading_day_at(time).is_err());
        }
        let (start, end) = TradingCalendar::supported_range();
        assert!(TradingCalendar::previous_trading_day(&start).is_err());
        assert!(TradingCalendar::next_trading_day(&end).is_err());
        assert!(TradingCalendar::get_trading_days(&end, 2).is_err());
        assert!(TradingCalendar::get_trading_days_before(&start, 2).is_err());
        assert!(TradingCalendar::count_trading_days(&end, &start).is_err());
    }

    #[test]
    fn market_hours_and_preopen_rule() {
        for (time, expected) in [
            ("09:29:59", false),
            ("09:30:00", true),
            ("11:30:00", true),
            ("11:30:01", false),
            ("12:00:00", false),
            ("13:00:00", true),
            ("15:00:00", true),
            ("15:00:01", false),
        ] {
            let at =
                NaiveDateTime::parse_from_str(&format!("2025-01-06 {time}"), "%Y-%m-%d %H:%M:%S")
                    .unwrap();
            assert_eq!(TradingCalendar::is_trading_time(&at).unwrap(), expected);
            let expected_day = if time < "09:30:00" {
                "2025-01-03"
            } else {
                "2025-01-06"
            };
            assert_eq!(
                TradingCalendar::market_trading_day_at(at).unwrap(),
                date(expected_day)
            );
        }
    }
}
