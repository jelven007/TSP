//! Exact cumulative-quantity boundaries and amount differences, never repairs.
use super::{
    AmountAvailability, ReportedTurnover, TradeAmountContext, TradeCollection, TradeCoverage,
    TradeEstimateOptions, TradeEstimateSummary, TradePhase, TradeQualification,
    TurnoverAvailability, TurnoverScope, reported_turnover::compensated_sum, trade_amount::invalid,
};
use serde::Serialize;
use std::{io, ops::Range};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum TradeAmountAlignment {
    Exact,
    QuantityMismatch,
    AmbiguousBoundary,
    CoverageInsufficient,
    UnsupportedScope,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum ReconciliationSourceStatus {
    Matched,
    MissingOrArchived,
}

#[derive(Debug, Clone, Serialize)]
pub struct TradeAmountInterval {
    time_minutes: u16,
    /// Positions in chronological supported regular events, preserving duplicates.
    event_range: Range<usize>,
    quantity_shares: i128,
    estimated_amount_yuan: f64,
    reported_amount_yuan: f64,
    estimate_minus_reported_yuan: f64,
    amount_encoding_step_yuan: f64,
}
impl TradeAmountInterval {
    pub fn time_minutes(&self) -> u16 {
        self.time_minutes
    }
    pub fn event_range(&self) -> Range<usize> {
        self.event_range.clone()
    }
    pub fn quantity_shares(&self) -> i128 {
        self.quantity_shares
    }
    pub fn estimate_minus_reported_yuan(&self) -> f64 {
        self.estimate_minus_reported_yuan
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct TradeAmountReconciliation {
    estimates: TradeEstimateSummary,
    minute_source: ReportedTurnover,
    daily_source: Option<ReportedTurnover>,
    alignment: TradeAmountAlignment,
    source_status: ReconciliationSourceStatus,
    full_scope_available: bool,
    intervals: Vec<TradeAmountInterval>,
    failed_bar_index: Option<usize>,
    estimate_minus_minute_yuan: Option<f64>,
    minute_minus_day_yuan: Option<f64>,
    estimate_minus_day_yuan: Option<f64>,
    minute_encoding_step_sum_yuan: f64,
    daily_encoding_step_yuan: Option<f64>,
    /// A hypothetical encoding bound, never a source-statistics guarantee.
    one_step_bound_assumption: &'static str,
}
impl TradeAmountReconciliation {
    pub fn estimates(&self) -> &TradeEstimateSummary {
        &self.estimates
    }
    pub fn alignment(&self) -> TradeAmountAlignment {
        self.alignment
    }
    pub fn source_status(&self) -> ReconciliationSourceStatus {
        self.source_status
    }
    pub fn full_scope_available(&self) -> bool {
        self.full_scope_available
    }
    pub fn intervals(&self) -> &[TradeAmountInterval] {
        &self.intervals
    }
    pub fn failed_bar_index(&self) -> Option<usize> {
        self.failed_bar_index
    }
    pub fn estimate_minus_minute_yuan(&self) -> Option<f64> {
        self.estimate_minus_minute_yuan
    }
    pub fn minute_minus_day_yuan(&self) -> Option<f64> {
        self.minute_minus_day_yuan
    }
    pub fn estimate_minus_day_yuan(&self) -> Option<f64> {
        self.estimate_minus_day_yuan
    }
    pub fn minute_encoding_step_sum_yuan(&self) -> f64 {
        self.minute_encoding_step_sum_yuan
    }
}

fn check_identity(trades: &TradeCollection, reported: &ReportedTurnover) -> io::Result<()> {
    let query = reported.query();
    if query.market() != trades.query().market()
        || query.code() != trades.query().code()
        || trades.query().date() != Some(query.date())
    {
        return Err(invalid("对账证券或请求日期与源K线日期不匹配"));
    }
    Ok(())
}

/// Align regular events with 1m/5m bars using exact integer share prefixes.
/// Missing provenance/partial coverage remains visible even if boundaries fit.
pub fn reconcile_trade_amounts(
    trades: &TradeCollection,
    context: &TradeAmountContext,
    minute: &ReportedTurnover,
    daily: Option<&ReportedTurnover>,
) -> io::Result<TradeAmountReconciliation> {
    check_identity(trades, minute)?;
    if let Some(day) = daily {
        check_identity(trades, day)?;
    }
    let estimates = trades.amount_estimate_summary(context, TradeEstimateOptions::default())?;
    let mut source_status = ReconciliationSourceStatus::Matched;
    let mut common: Option<&super::TurnoverSource> = None;
    for report in std::iter::once(minute).chain(daily) {
        for page in report.pages() {
            if let Some(source) = page.source() {
                if common.is_some_and(|previous| !previous.same_connection(source)) {
                    return Err(invalid("对账源K线来自不同连接或档案"));
                }
                common = Some(source);
            } else {
                source_status = ReconciliationSourceStatus::MissingOrArchived;
            }
        }
    }
    for page in trades.pages() {
        match (page.source(), common) {
            (Some(source), Some(reported)) if reported.archive_id().is_none() => {
                if source.endpoint() != reported.endpoint()
                    || source.connection_id() != reported.connection_id()
                {
                    return Err(invalid("成交明细与源K线来自不同连接"));
                }
            }
            _ => source_status = ReconciliationSourceStatus::MissingOrArchived,
        }
    }
    let estimate_amount = estimates.regular_with_auctions().estimated_amount_yuan();
    let minute_amount = minute.reported_amount_yuan();
    let daily_amount = daily.and_then(ReportedTurnover::reported_amount_yuan);
    let mut result = TradeAmountReconciliation {
        full_scope_available: source_status == ReconciliationSourceStatus::Matched
            && trades.coverage() == TradeCoverage::SourceEnumerated
            && estimates.regular_with_auctions().availability() == AmountAvailability::Available
            && minute.availability() == TurnoverAvailability::Available
            && daily.is_none_or(|d| d.availability() == TurnoverAvailability::Available),
        alignment: TradeAmountAlignment::Exact,
        source_status,
        intervals: vec![],
        failed_bar_index: None,
        estimate_minus_minute_yuan: estimate_amount.zip(minute_amount).map(|(a, b)| a - b),
        minute_minus_day_yuan: minute_amount.zip(daily_amount).map(|(a, b)| a - b),
        estimate_minus_day_yuan: estimate_amount.zip(daily_amount).map(|(a, b)| a - b),
        minute_encoding_step_sum_yuan: compensated_sum(
            minute.bars().iter().map(|b| b.amount_encoding_step_yuan()),
        )?,
        daily_encoding_step_yuan: daily
            .map(|d| compensated_sum(d.bars().iter().map(|b| b.amount_encoding_step_yuan())))
            .transpose()?,
        one_step_bound_assumption: "Assume at most one binary32 encoding step per amount; not a vendor guarantee or a full statistics error bound.",
        estimates,
        minute_source: minute.clone(),
        daily_source: daily.cloned(),
    };
    if !matches!(
        minute.query().scope(),
        TurnoverScope::Minute1 | TurnoverScope::Minute5
    ) || daily.is_some_and(|d| d.query().scope() != TurnoverScope::RegularWithAuctions)
    {
        result.alignment = TradeAmountAlignment::UnsupportedScope;
        result.full_scope_available = false;
        return Ok(result);
    }
    let (Some(trade_scale), Some(bar_scale)) = (
        context.quantity_unit().shares_per_unit(),
        minute
            .quantity_unit()
            .and_then(|unit| unit.shares_per_unit()),
    ) else {
        result.alignment = TradeAmountAlignment::UnsupportedScope;
        result.full_scope_available = false;
        return Ok(result);
    };
    if trades.coverage() == TradeCoverage::Partial
        || result.estimates.candidate_records() > 0
        || result.estimates.unknown_records() > 0
        || minute.bars().is_empty()
    {
        result.alignment = TradeAmountAlignment::CoverageInsufficient;
        result.full_scope_available = false;
        return Ok(result);
    }
    let records: Vec<_> = trades
        .pages()
        .iter()
        .rev()
        .flat_map(|p| p.records())
        .filter(|r| {
            r.qualification() == TradeQualification::ConfirmedByProfile
                && matches!(
                    r.phase(),
                    TradePhase::Opening | TradePhase::Regular | TradePhase::ClosingMatch
                )
        })
        .collect();
    if records
        .windows(2)
        .any(|pair| pair[0].time_minutes() > pair[1].time_minutes())
    {
        return Err(invalid("对账成交事件时间非递增"));
    }
    let mut prefix = vec![0i128];
    for record in &records {
        let quantity = i128::from(record.volume_raw())
            .checked_mul(trade_scale)
            .ok_or_else(|| invalid("对账股数转换溢出"))?;
        prefix.push(
            prefix
                .last()
                .copied()
                .expect("initial prefix")
                .checked_add(quantity)
                .ok_or_else(|| invalid("对账股数合计溢出"))?,
        );
    }
    let (mut cumulative, mut left) = (0i128, 0usize);
    for (bar_index, bar) in minute.bars().iter().enumerate() {
        let volume = bar.reported_volume_raw() * bar_scale as f64;
        // Float-to-integer conversion is accepted only in the exact f64 range.
        if volume.fract() != 0.0 || !(0.0..=9_007_199_254_740_991.0).contains(&volume) {
            result.alignment = TradeAmountAlignment::QuantityMismatch;
            result.failed_bar_index = Some(bar_index);
            break;
        }
        cumulative = cumulative
            .checked_add(volume as i128)
            .ok_or_else(|| invalid("源股数合计溢出"))?;
        let lower = prefix.partition_point(|v| *v < cumulative).max(left);
        let upper = prefix.partition_point(|v| *v <= cumulative);
        let candidates: Vec<_> = (lower..upper)
            .filter(|&i| {
                (i == records.len() || records[i].time_minutes() >= bar.time_minutes())
                    && (i == 0 || records[i - 1].time_minutes() <= bar.time_minutes())
            })
            .collect();
        if candidates.len() != 1 {
            result.alignment = if candidates.is_empty() {
                TradeAmountAlignment::QuantityMismatch
            } else {
                TradeAmountAlignment::AmbiguousBoundary
            };
            result.failed_bar_index = Some(bar_index);
            break;
        }
        let right = candidates[0];
        let mut fen = 0i128;
        for record in &records[left..right] {
            fen = fen
                .checked_add(
                    i128::from(record.price_raw())
                        .checked_mul(i128::from(record.volume_raw()))
                        .and_then(|v| v.checked_mul(trade_scale))
                        .ok_or_else(|| invalid("对账区间金额乘法溢出"))?,
                )
                .ok_or_else(|| invalid("对账区间金额合计溢出"))?;
        }
        let amount = fen as f64 / 100.0;
        result.intervals.push(TradeAmountInterval {
            time_minutes: bar.time_minutes(),
            event_range: left..right,
            quantity_shares: volume as i128,
            estimated_amount_yuan: amount,
            reported_amount_yuan: bar.reported_amount_yuan(),
            estimate_minus_reported_yuan: amount - bar.reported_amount_yuan(),
            amount_encoding_step_yuan: bar.amount_encoding_step_yuan(),
        });
        left = right;
    }
    if result.alignment == TradeAmountAlignment::Exact && left != records.len() {
        result.alignment = TradeAmountAlignment::CoverageInsufficient;
    }
    if result.alignment != TradeAmountAlignment::Exact {
        result.full_scope_available = false;
    }
    Ok(result)
}
