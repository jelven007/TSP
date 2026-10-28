use super::Gbbq;
use crate::file::day::Day;
use crate::{Error, Result, data::Symbol};

#[derive(serde::Deserialize, Debug, Clone)]
pub struct Factor {
    pub date: String,
    pub code: String,
    #[serde(rename(deserialize = "close"))]
    pub preclose: f64,
    /// 从上市日起累计的收益指数，包含现金/送配股事件；不是价格乘数。
    pub factor: f64,
}

impl Factor {
    #[inline]
    pub fn compute_factor(&self, close: f64) -> f64 {
        self.factor * (close / self.preclose)
    }
}

#[derive(Debug, Clone)]
pub struct Fq {
    pub code: String,
    pub date: u32,
    pub factor: f64,
    pub close: f64,
    pub preclose: f64,
    pub trading: bool,
    pub xdxr: bool,
}

impl Fq {
    /// 首个交易日为基期（因子1），不应用基期及其之前的事件。
    pub fn new(
        days: impl ExactSizeIterator<Item = Day> + Clone,
        events: &[Gbbq],
    ) -> Option<Vec<Self>> {
        Self::new_checked(days, events).ok()
    }

    /// 校验完整证券身份；所有日线和事件必须属于同一证券，包括非除权类事件。
    pub fn new_checked(
        days: impl ExactSizeIterator<Item = Day> + Clone,
        events: &[Gbbq],
    ) -> Result<Vec<Self>> {
        let first = days.clone().next().ok_or(Error::Custom("复权日线为空"))?;
        Self::build(days, events, first.close, 1.0, true)
    }

    /// 增量 events 必须限定为上次基准日期之后；停牌期间事件依次作用于理论前收。
    pub fn concat(
        days: impl ExactSizeIterator<Item = Day> + Clone,
        events: &[Gbbq],
        preclose: f64,
        factor: f64,
    ) -> Option<Vec<Self>> {
        Self::concat_checked(days, events, preclose, factor).ok()
    }

    /// 增量事件须由调用方限制在基准日之后；此入口校验证券身份和数值。
    pub fn concat_checked(
        days: impl ExactSizeIterator<Item = Day> + Clone,
        events: &[Gbbq],
        preclose: f64,
        factor: f64,
    ) -> Result<Vec<Self>> {
        Self::build(days, events, preclose, factor, false)
    }

    pub(crate) fn check_events(symbol: &Symbol, events: &[Gbbq]) -> Result<()> {
        // Local GBBQ market tags include Beijing; TCP command support is a
        // separate contract and must not restrict local factor calculation.
        let market = match symbol.market() {
            crate::data::Market::Shenzhen => 0,
            crate::data::Market::Shanghai => 1,
            crate::data::Market::Beijing => 2,
        };
        for event in events {
            if event.market != market || event.code != symbol.code() {
                return Err(Error::Invalid {
                    expected: format!("{} 的复权事件", symbol),
                    found: format!("market={} code={}", event.market, event.code),
                });
            }
        }
        Ok(())
    }

    fn build(
        days: impl ExactSizeIterator<Item = Day>,
        events: &[Gbbq],
        mut preclose: f64,
        mut factor: f64,
        initial: bool,
    ) -> Result<Vec<Self>> {
        if !preclose.is_finite() || preclose <= 0.0 || !factor.is_finite() || factor <= 0.0 {
            return Err(Error::Custom("复权基准价格和因子必须为有限正数"));
        }
        let mut days = days.peekable();
        let first = days.peek().ok_or(Error::Custom("复权日线为空"))?;
        let symbol: Symbol = first.code.parse()?;
        let code = first.code.clone();
        Self::check_events(&symbol, events)?;
        let mut events: Vec<_> = events.iter().filter(|g| g.category == 1).collect();
        events.sort_by_key(|g| g.date);
        let mut events = events.into_iter().peekable();
        let mut result = Vec::with_capacity(days.len());
        let mut previous_date = None;
        for (i, day) in days.enumerate() {
            if day.code != code {
                return Err(Error::Invalid {
                    expected: code,
                    found: day.code,
                });
            }
            crate::data::date_from_u32(day.date)?;
            if !day.close.is_finite()
                || day.close <= 0.0
                || previous_date.is_some_and(|date| day.date <= date)
            {
                return Err(Error::Custom("复权日线价格非法或日期未严格递增"));
            }
            let mut applied = false;
            while let Some(event) = events.peek().filter(|g| g.date <= day.date) {
                if !initial || i != 0 {
                    preclose = event.compute_pre_pct(preclose, preclose, true)[0];
                    if !preclose.is_finite() || preclose <= 0.0 {
                        return Err(Error::Custom("除权事件产生非法理论前收盘价"));
                    }
                    applied = true;
                }
                events.next();
            }
            factor *= day.close / preclose;
            if !factor.is_finite() {
                return Err(Error::Custom("累计复权因子溢出"));
            }
            result.push(Self {
                code: day.code,
                date: day.date,
                factor,
                close: day.close,
                preclose,
                trading: true,
                xdxr: applied,
            });
            preclose = day.close;
            previous_date = Some(day.date);
        }
        Ok(result)
    }

    pub fn no_gbbq(days: impl ExactSizeIterator<Item = Day> + Clone) -> Option<Vec<Self>> {
        Self::new(days, &[])
    }

    pub fn no_gbbq_with_previous(
        days: impl ExactSizeIterator<Item = Day> + Clone,
        preclose: f64,
        factor: f64,
    ) -> Option<Vec<Self>> {
        Self::concat(days, &[], preclose, factor)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn day(code: &str) -> Day {
        Day {
            code: code.into(),
            date: 20250106,
            open: 10.0,
            high: 10.0,
            low: 10.0,
            close: 10.0,
            amount: 100.0,
            vol: 100,
        }
    }

    #[test]
    fn rejects_foreign_events_even_when_category_or_date_would_be_filtered() {
        let mut event = Gbbq {
            market: 0,
            code: "000002",
            date: 19910601,
            category: 2,
            fh_qltp: 10.0,
            pgj_qzgb: 0.0,
            sg_hltp: 0.0,
            pg_hzgb: 0.0,
        };
        let days = vec![day("sz000001")];
        assert!(matches!(
            Fq::new_checked(days.clone().into_iter(), &[event.clone()]),
            Err(Error::Invalid { .. })
        ));
        event.code = "000001";
        event.market = 1;
        assert!(Fq::new(days.clone().into_iter(), &[event.clone()]).is_none());
        event.market = 0;
        assert!(Fq::new_checked(days.into_iter(), &[event]).is_ok());
    }

    #[test]
    fn rejects_mixed_or_invalid_daily_symbols() {
        let mut second = day("sh000001");
        second.date += 1;
        assert!(matches!(
            Fq::new_checked(vec![day("sz000001"), second].into_iter(), &[]),
            Err(Error::Invalid { .. })
        ));
        assert!(Fq::new_checked(vec![day("000001")].into_iter(), &[]).is_err());
    }

    #[test]
    fn local_beijing_events_use_the_local_market_tag() {
        let mut event = Gbbq {
            market: 2,
            code: "830001",
            date: 20250106,
            category: 1,
            fh_qltp: 0.0,
            pgj_qzgb: 0.0,
            sg_hltp: 0.0,
            pg_hzgb: 0.0,
        };
        assert!(Fq::new_checked(vec![day("bj830001")].into_iter(), &[event.clone()]).is_ok());
        event.market = 0;
        assert!(Fq::new_checked(vec![day("bj830001")].into_iter(), &[event]).is_err());
    }

    #[test]
    fn suspended_events_match_full_and_incremental_series() {
        let make_day = |date, close| Day {
            code: "sz000001".into(),
            date,
            open: close,
            high: close,
            low: close,
            close,
            amount: 100.0,
            vol: 100,
        };
        let days = vec![make_day(20250106, 10.0), make_day(20250110, 9.0)];
        let event = Gbbq {
            market: 0,
            code: "000001",
            date: 20250108,
            category: 1,
            fh_qltp: 10.0,
            pgj_qzgb: 0.0,
            sg_hltp: 0.0,
            pg_hzgb: 0.0,
        };
        let full = Fq::new(days.clone().into_iter(), std::slice::from_ref(&event)).unwrap();
        let incremental = Fq::concat(days[1..].iter().cloned(), &[event], 10.0, 1.0).unwrap();
        assert_eq!(full[1].preclose, 9.0);
        assert_eq!(full[1].factor, incremental[0].factor);
        assert_eq!(full[1].factor, 1.0);
    }
}
