use crate::{
    Error::Custom,
    Result,
    file::gbbq::{Factor, Fq, Gbbq},
};
use std::path::Path;

/// 注意：成交量的单位为 “手”，而不是股。在通达信和交易所数据里，单位为股。
#[derive(Debug, Clone, serde::Serialize)]
pub struct Day {
    pub date: String,
    pub code: String,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub amount: f64,
    pub vol: f64,
    pub preclose: f64,
    pub factor: f64,
}

impl Day {
    pub fn new(code: &str, p: impl AsRef<Path>, gbbqs: Option<&[Gbbq]>) -> Result<Vec<Self>> {
        let raw = std::fs::read(p)?;
        Self::from_bytes(code, &raw, gbbqs)
    }

    pub fn from_bytes(code: &str, raw: &[u8], gbbqs: Option<&[Gbbq]>) -> Result<Vec<Self>> {
        let days = super::Day::iter_checked(code, raw)?;
        let fq = Fq::new_checked(days.clone(), gbbqs.unwrap_or(&[]))?
            .into_iter()
            .filter(|d| d.trading);
        Ok(days
            .zip(fq)
            .map(|(d, f)| Self {
                date: crate::bytes_helper::date_string(d.date),
                code: d.code,
                open: d.open,
                high: d.high,
                low: d.low,
                close: d.close,
                amount: d.amount,
                vol: d.vol as f64 / 100.,
                preclose: f.preclose,
                factor: f.factor,
            })
            .collect())
    }

    pub fn concat(
        code: &str,
        p: impl AsRef<Path>,
        gbbqs: Option<&[Gbbq]>,
        f: Option<&Factor>,
    ) -> Result<Vec<Self>> {
        let raw = std::fs::read(p)?;
        Self::concat_bytes(code, &raw, gbbqs, f)
    }

    pub fn concat_bytes(
        code: &str,
        raw: &[u8],
        gbbqs: Option<&[Gbbq]>,
        f: Option<&Factor>,
    ) -> Result<Vec<Self>> {
        let days = super::Day::iter_checked(code, raw)?;
        let first = days.clone().next().ok_or(Custom("初始化前收盘价失败"))?;
        Fq::check_events(&code.parse()?, gbbqs.unwrap_or(&[]))?;
        if let Some(previous) = f {
            let date = chrono::NaiveDate::parse_from_str(&previous.date, "%Y-%m-%d")
                .map_err(|_| Custom("前因子日期非法"))?;
            use chrono::Datelike;
            let date = (date.year() as u32) * 10000 + date.month() * 100 + date.day();
            if previous.code != code || date >= first.date {
                return Err(Custom("前因子代码不匹配或日期不早于增量首日"));
            }
        }
        let (preclose, factor) = f
            .map(|f| (f.preclose, f.factor))
            .unwrap_or((first.close, 1.0));
        let events: Vec<_> = gbbqs
            .unwrap_or(&[])
            .iter()
            .filter(|g| f.is_none_or(|f| crate::bytes_helper::date_string(g.date) > f.date))
            .cloned()
            .collect();
        let fq = Fq::concat_checked(days.clone(), &events, preclose, factor)?
            .into_iter()
            .filter(|d| d.trading);
        Ok(days
            .zip(fq)
            .map(|(d, f)| Self {
                date: crate::bytes_helper::date_string(d.date),
                code: d.code,
                open: d.open,
                high: d.high,
                low: d.low,
                close: d.close,
                amount: d.amount,
                vol: d.vol as f64 / 100.,
                preclose: f.preclose,
                factor: f.factor,
            })
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn raw() -> Vec<u8> {
        let mut raw = 20250106u32.to_le_bytes().to_vec();
        for _ in 0..4 {
            raw.extend_from_slice(&1000u32.to_le_bytes());
        }
        raw.extend_from_slice(&100.0f32.to_le_bytes());
        raw.extend_from_slice(&100u32.to_le_bytes());
        raw.extend_from_slice(&0u32.to_le_bytes());
        raw
    }

    #[test]
    fn byte_wrappers_reject_wrong_events_and_incremental_baselines() {
        let event = Gbbq {
            market: 0,
            code: "000002",
            date: 19910601,
            category: 1,
            fh_qltp: 10.0,
            pgj_qzgb: 0.0,
            sg_hltp: 0.0,
            pg_hzgb: 0.0,
        };
        let mut previous = Factor {
            date: "2025-01-03".into(),
            code: "sz000001".into(),
            preclose: 10.0,
            factor: 1.0,
        };
        assert!(matches!(
            Day::from_bytes("sz000001", &raw(), Some(std::slice::from_ref(&event))),
            Err(crate::Error::Invalid { .. })
        ));
        // 旧事件也须拒绝，不能先按基准日过滤而藏起身份错误。
        assert!(matches!(
            Day::concat_bytes("sz000001", &raw(), Some(&[event]), Some(&previous)),
            Err(crate::Error::Invalid { .. })
        ));
        assert!(Day::concat_bytes("sz000001", &raw(), None, Some(&previous)).is_ok());
        previous.code = "sh000001".into();
        assert!(Day::concat_bytes("sz000001", &raw(), None, Some(&previous)).is_err());
        previous.code = "sz000001".into();
        previous.date = "2025-01-06".into();
        assert!(Day::concat_bytes("sz000001", &raw(), None, Some(&previous)).is_err());
    }
}
