//! 可替换且声明覆盖范围的交易日历。
use std::collections::BTreeSet;

use chrono::{Datelike, NaiveDate, NaiveDateTime, Timelike};
use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum CalendarError {
    #[error("日历 {version} 不覆盖 {date}（覆盖 {start}–{end}）")]
    OutOfRange {
        date: NaiveDate,
        start: NaiveDate,
        end: NaiveDate,
        version: String,
    },
    #[error("无效日历: {0}")]
    Invalid(&'static str),
}

pub type CalendarResult<T> = Result<T, CalendarError>;

#[derive(Debug, Clone, Copy, Serialize)]
pub struct CalendarMetadata<'a> {
    pub version: &'a str,
    pub source: &'a str,
    pub start: NaiveDate,
    pub end: NaiveDate,
}

/// 所有查询必须在声明的覆盖区间内；寻找前后交易日越界也返回错误。
pub trait CalendarProvider: Send + Sync {
    fn metadata(&self) -> CalendarMetadata<'_>;
    fn is_trading_day(&self, date: NaiveDate) -> CalendarResult<bool>;

    fn check_range(&self, date: NaiveDate) -> CalendarResult<()> {
        let m = self.metadata();
        if date < m.start || date > m.end {
            return Err(CalendarError::OutOfRange {
                date,
                start: m.start,
                end: m.end,
                version: m.version.to_owned(),
            });
        }
        Ok(())
    }

    fn previous_trading_day(&self, date: NaiveDate) -> CalendarResult<NaiveDate> {
        self.check_range(date)?;
        let mut current = date;
        loop {
            current = current
                .pred_opt()
                .ok_or(CalendarError::Invalid("日期下溢"))?;
            if self.is_trading_day(current)? {
                return Ok(current);
            }
        }
    }

    fn next_trading_day(&self, date: NaiveDate) -> CalendarResult<NaiveDate> {
        self.check_range(date)?;
        let mut current = date;
        loop {
            current = current
                .succ_opt()
                .ok_or(CalendarError::Invalid("日期上溢"))?;
            if self.is_trading_day(current)? {
                return Ok(current);
            }
        }
    }

    /// 输入是UTC+8本地时间；9:30之前使用前一个交易日。
    fn market_trading_day_at(&self, datetime: NaiveDateTime) -> CalendarResult<NaiveDate> {
        let date = datetime.date();
        if self.is_trading_day(date)? && datetime.time().num_seconds_from_midnight() >= 34200 {
            Ok(date)
        } else {
            self.previous_trading_day(date)
        }
    }

    fn trading_days_between(
        &self,
        start: NaiveDate,
        end: NaiveDate,
    ) -> CalendarResult<Vec<NaiveDate>> {
        if start > end {
            return Err(CalendarError::Invalid("起止日期颠倒"));
        }
        self.check_range(start)?;
        self.check_range(end)?;
        let mut result = Vec::new();
        let mut current = start;
        loop {
            if self.is_trading_day(current)? {
                result.push(current);
            }
            if current == end {
                return Ok(result);
            }
            current = current
                .succ_opt()
                .ok_or(CalendarError::Invalid("日期上溢"))?;
        }
    }

    /// 从指定日期向后取 count 个交易日（含起始日），不足覆盖时返回错误。
    fn get_trading_days(&self, start: NaiveDate, count: usize) -> CalendarResult<Vec<NaiveDate>> {
        self.check_range(start)?;
        let mut result = Vec::new();
        let mut current = start;
        while result.len() < count {
            if self.is_trading_day(current)? {
                result.push(current);
            }
            if result.len() < count {
                current = current
                    .succ_opt()
                    .ok_or(CalendarError::Invalid("日期上溢"))?;
            }
        }
        Ok(result)
    }

    /// 从指定日期向前取 count 个交易日（含结束日），返回升序结果。
    fn get_trading_days_before(
        &self,
        end: NaiveDate,
        count: usize,
    ) -> CalendarResult<Vec<NaiveDate>> {
        self.check_range(end)?;
        let mut result = Vec::new();
        let mut current = end;
        while result.len() < count {
            if self.is_trading_day(current)? {
                result.push(current);
            }
            if result.len() < count {
                current = current
                    .pred_opt()
                    .ok_or(CalendarError::Invalid("日期下溢"))?;
            }
        }
        result.reverse();
        Ok(result)
    }
}

#[derive(Debug, Clone, Copy)]
pub struct BuiltinCalendar;

impl CalendarProvider for BuiltinCalendar {
    fn metadata(&self) -> CalendarMetadata<'_> {
        CalendarMetadata {
            version: "trade_date_a/2026.0",
            source: "https://crates.io/crates/trade_date_a/2026.0.0",
            start: NaiveDate::from_ymd_opt(2024, 1, 1).expect("固定覆盖起始日期"),
            end: NaiveDate::from_ymd_opt(2026, 12, 31).expect("固定覆盖结束日期"),
        }
    }

    fn is_trading_day(&self, date: NaiveDate) -> CalendarResult<bool> {
        self.check_range(date)?;
        let value =
            i64::from(date.year()) * 10000 + i64::from(date.month()) * 100 + i64::from(date.day());
        Ok(trade_date_a::is_work_day(value))
    }
}

/// 外部数据的完整休市表；周末自动休市，holidays 必须包含区间内所有工作日休市日。
/// 此结构只负责格式检查，不替调用方证明数据来源的完整性。
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CalendarDataset {
    pub version: String,
    pub source: String,
    pub start: NaiveDate,
    pub end: NaiveDate,
    pub holidays: Vec<NaiveDate>,
}

#[derive(Debug, Clone)]
pub struct HolidayCalendar {
    dataset: CalendarDataset,
    holidays: BTreeSet<NaiveDate>,
}

impl TryFrom<CalendarDataset> for HolidayCalendar {
    type Error = CalendarError;

    fn try_from(dataset: CalendarDataset) -> CalendarResult<Self> {
        if dataset.version.trim().is_empty() || dataset.source.trim().is_empty() {
            return Err(CalendarError::Invalid("版本及来源不能为空"));
        }
        if dataset.start > dataset.end {
            return Err(CalendarError::Invalid("起止日期颠倒"));
        }
        if dataset
            .holidays
            .iter()
            .any(|date| *date < dataset.start || *date > dataset.end)
        {
            return Err(CalendarError::Invalid("休市日超出声明区间"));
        }
        let holidays: BTreeSet<_> = dataset.holidays.iter().copied().collect();
        if holidays.len() != dataset.holidays.len() {
            return Err(CalendarError::Invalid("休市日重复"));
        }
        Ok(Self { dataset, holidays })
    }
}

impl CalendarProvider for HolidayCalendar {
    fn metadata(&self) -> CalendarMetadata<'_> {
        CalendarMetadata {
            version: &self.dataset.version,
            source: &self.dataset.source,
            start: self.dataset.start,
            end: self.dataset.end,
        }
    }

    fn is_trading_day(&self, date: NaiveDate) -> CalendarResult<bool> {
        self.check_range(date)?;
        Ok(date.weekday().number_from_monday() <= 5 && !self.holidays.contains(&date))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn date(value: &str) -> NaiveDate {
        value.parse().unwrap()
    }

    #[test]
    fn strict_navigation_propagates_coverage_boundaries() {
        let (start, end) = super::super::TradingCalendar::supported_range();
        assert!(BuiltinCalendar.previous_trading_day(start).is_err());
        assert!(BuiltinCalendar.next_trading_day(end).is_err());
        assert!(
            BuiltinCalendar
                .trading_days_between(end, date("2027-01-01"))
                .is_err()
        );
    }

    #[test]
    fn external_calendar_uses_declared_holidays_and_rejects_invalid_datasets() {
        // 仅为局部查询的测试夹具，不声明整年覆盖。
        let mut dataset = CalendarDataset {
            version: "test-fixture".into(),
            source: "unit-test".into(),
            start: date("2025-01-01"),
            end: date("2025-01-06"),
            holidays: vec![date("2025-01-01")],
        };
        let calendar = HolidayCalendar::try_from(dataset.clone()).unwrap();
        assert_eq!(
            calendar.next_trading_day(date("2025-01-03")).unwrap(),
            date("2025-01-06")
        );
        assert!(!calendar.is_trading_day(date("2025-01-01")).unwrap());
        assert_eq!(
            calendar
                .trading_days_between(dataset.start, dataset.end)
                .unwrap(),
            [date("2025-01-02"), date("2025-01-03"), date("2025-01-06")]
        );
        assert!(calendar.is_trading_day(date("2025-01-07")).is_err());
        dataset.holidays.push(dataset.holidays[0]);
        assert!(HolidayCalendar::try_from(dataset).is_err());
    }
}
