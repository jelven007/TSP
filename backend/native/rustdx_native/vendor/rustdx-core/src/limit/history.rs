//! 公告支持的证券状态区间。缺口返回未知，不用当前名称推断历史。
use chrono::NaiveDate;
use serde::{Deserialize, Serialize};
use thiserror::Error;

use super::{Board, ListingStage, RuleContext};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Announcement {
    pub url: String,
    /// 保存的公告原始字节摘要，便于维护者复核。
    pub sha256: String,
    pub published: NaiveDate,
    pub checked: NaiveDate,
}

impl Announcement {
    pub fn validate(&self) -> Result<(), HistoryError> {
        if !self.url.starts_with("https://")
            || self.url.len() <= 8
            || self.sha256.len() != 64
            || !self.sha256.bytes().all(|b| b.is_ascii_hexdigit())
            || self.checked < self.published
        {
            return Err(HistoryError::Invalid("公告来源、摘要或核实日期非法"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum TradingState {
    Trading,
    Suspended,
    Delisted,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SecurityInterval {
    pub code: String,
    /// 闭区间；只声明实际核实的区间，不自动继承到未来。
    pub start: NaiveDate,
    pub end: NaiveDate,
    pub board: Board,
    pub st: bool,
    pub stage: ListingStage,
    pub state: TradingState,
    pub announcement: Announcement,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SecurityHistoryDataset {
    pub version: String,
    pub checked_through: NaiveDate,
    pub intervals: Vec<SecurityInterval>,
}

#[derive(Debug, Error)]
pub enum HistoryError {
    #[error("证券历史数据非法: {0}")]
    Invalid(&'static str),
    #[error("{0}状态区间重叠")]
    Overlap(String),
}

#[derive(Debug, Clone)]
pub struct SecurityHistory {
    dataset: SecurityHistoryDataset,
}

impl TryFrom<SecurityHistoryDataset> for SecurityHistory {
    type Error = HistoryError;

    fn try_from(dataset: SecurityHistoryDataset) -> Result<Self, Self::Error> {
        Self::validate_dataset(dataset, false)
    }
}

impl SecurityHistory {
    /// Validate announcement-backed state independently of price-rule qualification.
    /// This does not extend the checked rule horizon; `context_at` remains qualified.
    pub fn from_state_dataset(dataset: SecurityHistoryDataset) -> Result<Self, HistoryError> {
        Self::validate_dataset(dataset, true)
    }

    fn validate_dataset(
        mut dataset: SecurityHistoryDataset,
        state_only: bool,
    ) -> Result<Self, HistoryError> {
        if dataset.version.trim().is_empty() {
            return Err(HistoryError::Invalid("版本不能为空"));
        }
        dataset
            .intervals
            .sort_by(|a, b| (&a.code, a.start).cmp(&(&b.code, b.start)));
        for interval in &dataset.intervals {
            let symbol = interval
                .code
                .parse::<crate::data::Symbol>()
                .map_err(|_| HistoryError::Invalid("证券代码非法"))?;
            if !symbol.is_a_share()
                || super::board_of(symbol.code()) != Some(interval.board)
                || interval.start > interval.end
                || interval.end > dataset.checked_through
                || interval.announcement.checked > dataset.checked_through
            {
                return Err(HistoryError::Invalid("证券、板块或覆盖区间非法"));
            }
            interval.announcement.validate()?;
            if matches!(interval.stage, ListingStage::Ipo { session: 0, .. })
                || (interval.stage == ListingStage::BeijingTransfer
                    && interval.board != Board::Beijing)
                || (interval.stage == ListingStage::LegacyChiNextDelisting
                    && (interval.board != Board::ChiNext
                        || interval.start
                            < NaiveDate::from_ymd_opt(2020, 8, 24).expect("reform date")))
                || matches!(
                    interval.stage,
                    ListingStage::Ipo {
                        registration: false,
                        ..
                    }
                ) && interval.board == Board::Star
            {
                return Err(HistoryError::Invalid("不支持的上市阶段"));
            }
            // IPO 序号是单日事实，不能作为一个多日区间的相同状态。
            if matches!(
                interval.stage,
                ListingStage::Ipo { .. } | ListingStage::AnnouncedUnrestricted
            ) && interval.start != interval.end
            {
                return Err(HistoryError::Invalid("上市阶段/无幅度限制公告须逐日声明"));
            }
            for date in [interval.start, interval.end] {
                match super::rule_at(RuleContext {
                    date,
                    board: interval.board,
                    st: interval.st,
                    stage: interval.stage,
                }) {
                    Ok(_) => {}
                    Err(
                        super::LimitRuleError::UnsupportedDate
                        | super::LimitRuleError::LegacyIpoFirstDay,
                    ) if state_only => {}
                    Err(_) => return Err(HistoryError::Invalid("不支持的规则或上市阶段")),
                }
            }
        }
        for pair in dataset.intervals.windows(2) {
            if pair[0].code == pair[1].code && pair[1].start <= pair[0].end {
                return Err(HistoryError::Overlap(pair[0].code.clone()));
            }
        }
        Ok(Self { dataset })
    }
}

impl SecurityHistory {
    pub fn dataset(&self) -> &SecurityHistoryDataset {
        &self.dataset
    }

    /// 未覆盖、停牌或摘牌都不产生可计算的交易日规则。
    pub fn context_at(&self, code: &str, date: NaiveDate) -> Option<RuleContext> {
        let interval = self.state_at(code, date)?;
        let context = RuleContext {
            date,
            board: interval.board,
            st: interval.st,
            stage: interval.stage,
        };
        (interval.state == TradingState::Trading && super::rule_at(context).is_ok())
            .then_some(context)
    }

    pub fn state_at(&self, code: &str, date: NaiveDate) -> Option<&SecurityInterval> {
        let index = self
            .dataset
            .intervals
            .partition_point(|i| i.code.as_str() < code || (i.code == code && i.start <= date));
        let interval = self.dataset.intervals.get(index.checked_sub(1)?)?;
        (interval.code == code && date <= interval.end).then_some(interval)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn state_facts_survive_rule_expiry_without_qualifying_price_rules() {
        let mut dataset: SecurityHistoryDataset = serde_json::from_value(serde_json::json!({
            "version":"fixture","checked_through":"2026-10-28","intervals":[{
                "code":"sh600000","start":"2026-10-28","end":"2026-10-28",
                "board":"Main","st":false,"stage":"Seasoned","state":"Trading",
                "announcement":{"url":"https://example.org/fixture","sha256":"a".repeat(64),
                    "published":"2026-10-27","checked":"2026-10-28"}
            }]
        }))
        .unwrap();
        assert!(SecurityHistory::try_from(dataset.clone()).is_err());
        let history = SecurityHistory::from_state_dataset(dataset.clone()).unwrap();
        let date = dataset.intervals[0].start;
        assert!(history.state_at("sh600000", date).is_some());
        assert!(history.context_at("sh600000", date).is_none());
        dataset.intervals[0].stage = ListingStage::Ipo {
            session: 0,
            registration: true,
        };
        assert!(SecurityHistory::from_state_dataset(dataset.clone()).is_err());
        dataset.intervals[0].stage = ListingStage::BeijingTransfer;
        assert!(SecurityHistory::from_state_dataset(dataset.clone()).is_err());
        dataset.intervals[0].stage = ListingStage::Seasoned;
        dataset.intervals.push(dataset.intervals[0].clone());
        assert!(matches!(
            SecurityHistory::from_state_dataset(dataset),
            Err(HistoryError::Overlap(_))
        ));
    }

    #[test]
    fn gaps_overlaps_and_single_day_stages_are_explicit() {
        let interval = SecurityInterval {
            code: "sh600000".into(),
            start: "2026-07-06".parse().unwrap(),
            end: "2026-07-07".parse().unwrap(),
            board: Board::Main,
            st: true,
            stage: ListingStage::Seasoned,
            state: TradingState::Trading,
            announcement: Announcement {
                url: "https://example.org/test-fixture".into(),
                sha256: "a".repeat(64),
                published: "2026-07-03".parse().unwrap(),
                checked: "2026-07-08".parse().unwrap(),
            },
        };
        let mut dataset = SecurityHistoryDataset {
            version: "test-fixture".into(),
            checked_through: "2026-07-08".parse().unwrap(),
            intervals: vec![interval.clone()],
        };
        let history = SecurityHistory::try_from(dataset.clone()).unwrap();
        assert!(
            history
                .state_at("sh600000", "2026-07-05".parse().unwrap())
                .is_none()
        );
        assert!(history.context_at("sh600000", interval.end).unwrap().st);
        assert!(
            history
                .state_at("sh600000", "2026-07-08".parse().unwrap())
                .is_none()
        );
        dataset.intervals.push(interval);
        assert!(matches!(
            SecurityHistory::try_from(dataset.clone()),
            Err(HistoryError::Overlap(_))
        ));
        dataset.intervals.pop();
        dataset.intervals[0].stage = ListingStage::Ipo {
            session: 1,
            registration: true,
        };
        assert!(SecurityHistory::try_from(dataset).is_err());
    }
}
