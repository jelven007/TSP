//! Price multiplier calculation, independent of network fetching.
use super::{Adj, DateTime, KlineData, XdxrData};

pub(super) fn checked_ex_right_reference(preclose: f64, ev: &XdxrData) -> Option<f64> {
    if !preclose.is_finite()
        || preclose <= 0.0
        || [ev.fh_qltp, ev.pgj_qzgb, ev.sg_hltp, ev.pg_hzgb]
            .iter()
            .any(|v| !v.is_finite() || *v < 0.0)
    {
        return None;
    }
    let numerator = preclose * 10.0 - ev.fh_qltp as f64 + ev.pgj_qzgb as f64 * ev.pg_hzgb as f64;
    let denominator = 10.0 + ev.sg_hltp as f64 + ev.pg_hzgb as f64;
    let reference = numerator / denominator;
    if denominator > 0.0 && reference.is_finite() && reference > 0.0 {
        let rounded = (reference * 100.0).round() / 100.0;
        return (rounded.is_finite() && rounded > 0.0).then_some(rounded);
    }
    None
}

pub(super) fn adjusted_multipliers(
    days: &[KlineData<'_>],
    xdxrs: &[XdxrData],
    adj: Adj,
) -> std::io::Result<Vec<f64>> {
    let Some(first) = days.first() else {
        return Ok(Vec::new());
    };
    let invalid = || {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "复权价格、事件或累计因子非法",
        )
    };
    let first_date = DateTime::to_u32(first.dt);
    // First available close already reflects earlier events.
    let mut events: Vec<&XdxrData> = xdxrs
        .iter()
        .filter(|x| x.category == 1 && x.date > first_date)
        .collect();
    events.sort_by_key(|e| e.date);
    let mut events = events.iter().peekable();
    let mut scales = Vec::with_capacity(days.len());
    let mut scale = 1.0f64;
    let mut preclose = first.close;
    for day in days {
        if [day.open, day.high, day.low, day.close]
            .iter()
            .any(|p| !p.is_finite() || *p <= 0.0)
        {
            return Err(invalid());
        }
        let date = DateTime::to_u32(day.dt);
        while let Some(&event) = events.peek() {
            if event.date > date {
                break;
            }
            let reference = checked_ex_right_reference(preclose, event).ok_or_else(invalid)?;
            scale *= reference / preclose;
            if !scale.is_finite() || scale <= 0.0 {
                return Err(invalid());
            }
            // Chain all events during suspension before the next actual close.
            preclose = reference;
            events.next();
        }
        scales.push(scale);
        preclose = day.close;
    }
    let last = *scales.last().unwrap_or(&1.0);
    scales
        .into_iter()
        .map(|scale| {
            let multiplier = match adj {
                Adj::Hfq => 1.0 / scale,
                Adj::Qfq => last / scale,
            };
            if multiplier.is_finite() && multiplier > 0.0 {
                Ok(multiplier)
            } else {
                Err(invalid())
            }
        })
        .collect()
}
