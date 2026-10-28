//! 按日期选择规则。输入必须是当日状态，不能将当前 ST/上市资料回填历史。
use chrono::NaiveDate;
use thiserror::Error;

use super::{Board, LimitStatus, limit_price, same_fen};

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum ListingStage {
    /// 已结束上市初期的普通交易日。
    Seasoned,
    /// IPO 上市交易日序号，首日为 1。注册制须由证券上市资料确认。
    Ipo { session: u32, registration: bool },
    /// 北交所精选层平移（平移首日仍为 ±30%）。
    BeijingTransfer,
    /// 交易所公告明确不设涨跌幅限制的日期，如重新上市/退市整理首日。
    AnnouncedUnrestricted,
    /// 2020改革前进入、改革当日仍处于退市整理的创业板股票。
    LegacyChiNextDelisting,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LimitRule {
    Limited { up_pct: u8, down_pct: u8 },
    Unrestricted,
}

#[derive(Debug, Clone, Copy)]
pub struct RuleContext {
    pub date: NaiveDate,
    pub board: Board,
    pub st: bool,
    pub stage: ListingStage,
}

#[derive(Debug, Error)]
pub enum LimitRuleError {
    #[error("该日期/板块超出已核实规则范围")]
    UnsupportedDate,
    #[error("无效上市阶段: {0}")]
    InvalidStage(&'static str),
    #[error("旧核准制 IPO 首日需使用发行价及当时专门规则")]
    LegacyIpoFirstDay,
    #[error("价格必须为正、有限、分单位报价，且 OHLC 关系合法")]
    InvalidPrice,
}

/// 已核实的规则版本；来源及例外见 docs/requirements/LIMIT_RULES.md。
pub const RULE_VERSION: &str = "exchange-rules/2026-07-06";
/// 维护规则时必须同时复核各交易所的实施公告；不能仅移动此日期。
pub const RULE_CHECKED_THROUGH: &str = "2026-10-22";

pub fn rule_at(ctx: RuleContext) -> Result<LimitRule, LimitRuleError> {
    let ymd =
        |year, month, day| NaiveDate::from_ymd_opt(year, month, day).expect("固定规则生效日期");
    let reform = ymd(2020, 8, 24);
    let main_registration = ymd(2023, 4, 10);
    let main_st_reform = ymd(2026, 7, 6);
    // 主板只声明已核实的现代规则区间；不宣称覆盖更早制度或未来规则。
    let start = match ctx.board {
        Board::Main => ymd(2014, 1, 1),
        Board::ChiNext => ymd(2009, 10, 30),
        Board::Star => ymd(2019, 7, 22),
        Board::Beijing => ymd(2021, 11, 15),
    };
    if ctx.date < start || ctx.date > RULE_CHECKED_THROUGH.parse().expect("固定核实日期") {
        return Err(LimitRuleError::UnsupportedDate);
    }
    match ctx.stage {
        ListingStage::AnnouncedUnrestricted => return Ok(LimitRule::Unrestricted),
        ListingStage::BeijingTransfer if ctx.board != Board::Beijing => {
            return Err(LimitRuleError::InvalidStage("精选层平移只适用于北交所"));
        }
        ListingStage::LegacyChiNextDelisting => {
            if ctx.board != Board::ChiNext || ctx.date < reform {
                return Err(LimitRuleError::InvalidStage("创业板历史退市例外不适用"));
            }
            return Ok(LimitRule::Limited {
                up_pct: 10,
                down_pct: 10,
            });
        }
        ListingStage::Ipo {
            session,
            registration,
        } => {
            if session == 0 {
                return Err(LimitRuleError::InvalidStage("IPO交易日序号须从1开始"));
            }
            let registered_from = match ctx.board {
                Board::Main => main_registration,
                Board::ChiNext => reform,
                Board::Star => start,
                Board::Beijing => start,
            };
            if registration && ctx.date < registered_from {
                return Err(LimitRuleError::InvalidStage("注册制标记早于制度实施"));
            }
            if ctx.board == Board::Beijing {
                if session == 1 {
                    return Ok(LimitRule::Unrestricted);
                }
            } else if registration {
                if session <= 5 {
                    return Ok(LimitRule::Unrestricted);
                }
            } else if ctx.board == Board::Star {
                return Err(LimitRuleError::InvalidStage("科创板IPO须为注册制"));
            } else if session == 1 {
                return Err(LimitRuleError::LegacyIpoFirstDay);
            }
        }
        _ => {}
    }
    let pct = match ctx.board {
        Board::Main if ctx.st && ctx.date < main_st_reform => 5,
        Board::Main => 10,
        Board::ChiNext if ctx.date < reform => {
            if ctx.st {
                5
            } else {
                10
            }
        }
        Board::ChiNext | Board::Star => 20,
        Board::Beijing => 30,
    };
    Ok(LimitRule::Limited {
        up_pct: pct,
        down_pct: pct,
    })
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct LimitPrices {
    pub up: f64,
    pub down: f64,
}

/// 无涨跌幅限制时明确返回 None；非法价格返回错误。
pub fn prices_at(prev_close: f64, ctx: RuleContext) -> Result<Option<LimitPrices>, LimitRuleError> {
    match rule_at(ctx)? {
        LimitRule::Unrestricted => Ok(None),
        LimitRule::Limited { up_pct, down_pct } => Ok(Some(LimitPrices {
            up: limit_price(prev_close, 100 + u64::from(up_pct))
                .ok_or(LimitRuleError::InvalidPrice)?,
            down: limit_price(prev_close, 100 - u64::from(down_pct))
                .ok_or(LimitRuleError::InvalidPrice)?,
        })),
    }
}

/// 无涨跌幅限制的日期不参与封板/炸板分类。
pub fn status_at(
    close: f64,
    high: f64,
    prev_close: f64,
    ctx: RuleContext,
) -> Result<Option<LimitStatus>, LimitRuleError> {
    if !close.is_finite() || !high.is_finite() || close <= 0.0 || high < close {
        return Err(LimitRuleError::InvalidPrice);
    }
    let Some(prices) = prices_at(prev_close, ctx)? else {
        return Ok(None);
    };
    Ok(Some(if same_fen(close, prices.up) {
        LimitStatus::Sealed
    } else if high >= prices.up || same_fen(high, prices.up) {
        LimitStatus::Blown
    } else {
        LimitStatus::Untouched
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn context(date: &str, board: Board, st: bool) -> RuleContext {
        RuleContext {
            date: date.parse().unwrap(),
            board,
            st,
            stage: ListingStage::Seasoned,
        }
    }
    fn pct(ctx: RuleContext) -> u8 {
        match rule_at(ctx).unwrap() {
            LimitRule::Limited { up_pct, .. } => up_pct,
            _ => panic!("预期有幅度限制"),
        }
    }

    #[test]
    fn effective_dates_and_st_rules() {
        assert_eq!(pct(context("2020-08-21", Board::ChiNext, false)), 10);
        assert_eq!(pct(context("2020-08-21", Board::ChiNext, true)), 5);
        assert_eq!(pct(context("2020-08-24", Board::ChiNext, true)), 20);
        assert_eq!(pct(context("2026-07-03", Board::Main, true)), 5);
        assert_eq!(pct(context("2026-07-06", Board::Main, true)), 10);
        assert_eq!(pct(context("2019-07-22", Board::Star, true)), 20);
        assert!(rule_at(context("2021-11-12", Board::Beijing, false)).is_err());
        assert!(rule_at(context("2027-01-04", Board::Main, false)).is_err());
    }

    #[test]
    fn ipo_and_transfer_are_distinct() {
        for (board, date, unrestricted) in [
            (Board::Main, "2023-04-10", 5),
            (Board::ChiNext, "2020-08-24", 5),
            (Board::Star, "2019-07-22", 5),
            (Board::Beijing, "2021-11-15", 1),
        ] {
            let mut ctx = context(date, board, false);
            ctx.stage = ListingStage::Ipo {
                session: unrestricted,
                registration: true,
            };
            assert_eq!(prices_at(10.0, ctx).unwrap(), None);
            assert_eq!(status_at(20.0, 21.0, 10.0, ctx).unwrap(), None);
            ctx.stage = ListingStage::Ipo {
                session: unrestricted + 1,
                registration: true,
            };
            assert!(prices_at(10.0, ctx).unwrap().is_some());
        }
        let mut ctx = context("2021-11-15", Board::Beijing, false);
        ctx.stage = ListingStage::BeijingTransfer;
        assert_eq!(pct(ctx), 30);
        ctx = context("2023-04-07", Board::Main, false);
        ctx.stage = ListingStage::Ipo {
            session: 1,
            registration: false,
        };
        assert!(matches!(
            rule_at(ctx),
            Err(LimitRuleError::LegacyIpoFirstDay)
        ));
    }
}
