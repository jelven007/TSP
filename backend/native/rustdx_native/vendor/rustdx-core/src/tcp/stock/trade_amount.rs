//! Explicit price-times-quantity estimates. Never repairs or fetches trades.
use std::io::{self, ErrorKind};

use serde::{Serialize, Serializer};

use super::{
    EnhancedHistoryTransactionPage, EnhancedHistoryTransactionRequest, TradeCollection,
    TradeCoverage, TradePage, TradePhase, TradeQualification, TradeQuery, TradeRecord,
    TransactionCollection, decode_enhanced_history_transactions, decode_trade_page,
};

pub const TRADE_AMOUNT_SCHEMA_VERSION: u16 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum TradeQuantityUnit {
    Unknown,
    Shares,
    LotsOf100Shares,
}

impl TradeQuantityUnit {
    pub(super) fn shares_per_unit(self) -> Option<i128> {
        match self {
            Self::Unknown => None,
            Self::Shares => Some(1),
            Self::LotsOf100Shares => Some(100),
        }
    }
}

/// Caller-confirmed A-share units, scoped to one exact trade query.
/// Code restrictions establish the supported instrument profile, not its unit.
///
/// ```
/// use rustdx_complete::tcp::stock::*;
/// let query = TradeQuery::history(0, "300308", 20261023)?;
/// let context = TradeAmountContext::a_share(
///     query.clone(), TradeQuantityUnit::LotsOf100Shares, "independent unit evidence",
/// )?;
/// let payload = [1, 0, 0, 0, 0, 0, 88, 2, 168, 15, 2, 1, 0, 0];
/// let page = decode_trade_page(&query, 0, 1, &payload)?;
/// assert_eq!(page.amount_estimates(&context)?[0].estimate().unwrap()
///     .estimated_amount_fen(), 200_000);
/// # Ok::<(), std::io::Error>(())
/// ```
#[derive(Debug, Clone, Serialize)]
pub struct TradeAmountContext {
    query: TradeQuery,
    quantity_unit: TradeQuantityUnit,
    unit_evidence: Option<String>,
}

impl TradeAmountContext {
    pub fn unknown(query: TradeQuery) -> Self {
        Self {
            query,
            quantity_unit: TradeQuantityUnit::Unknown,
            unit_evidence: None,
        }
    }

    pub fn a_share(
        query: TradeQuery,
        quantity_unit: TradeQuantityUnit,
        evidence: &str,
    ) -> io::Result<Self> {
        if !is_a_share(query.market(), query.code())
            || quantity_unit == TradeQuantityUnit::Unknown
            || evidence.trim().is_empty()
        {
            return Err(invalid("A股金额上下文需要受支持证券、明确单位与证据"));
        }
        Ok(Self {
            query,
            quantity_unit,
            unit_evidence: Some(evidence.into()),
        })
    }
    pub fn query(&self) -> &TradeQuery {
        &self.query
    }
    pub fn quantity_unit(&self) -> TradeQuantityUnit {
        self.quantity_unit
    }
    pub fn unit_evidence(&self) -> Option<&str> {
        self.unit_evidence.as_deref()
    }
    fn check(&self, query: &TradeQuery) -> io::Result<()> {
        if &self.query != query {
            return Err(invalid("金额上下文与证券、日期或成交协议不匹配"));
        }
        Ok(())
    }
}

pub(super) fn is_a_share(market: u16, code: &str) -> bool {
    code.len() == 6
        && code.bytes().all(|b| b.is_ascii_digit())
        && match market {
            0 => ["000", "001", "002", "003", "300", "301"]
                .iter()
                .any(|p| code.starts_with(p)),
            1 => ["600", "601", "603", "605", "688", "689"]
                .iter()
                .any(|p| code.starts_with(p)),
            _ => false,
        }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum AmountOrigin {
    RecordPriceTimesQuantity,
    VendorReported,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum AmountQualification {
    SupportedByProfile,
    Candidate,
    Excluded,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum AmountUnavailableReason {
    UnknownQuantityUnit,
    NotExecution,
    UnqualifiedEvent,
    UnsupportedProfile,
    NoSupportedExecutions,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum AmountAvailability {
    Available,
    Partial,
    Unavailable,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum TradeAmountDateBasis {
    RequestedDate,
    ObservedDateUnverified,
    Unavailable,
}

#[derive(Debug, Clone, Serialize)]
pub struct TradeAmountEstimate {
    amount_origin: AmountOrigin,
    #[serde(serialize_with = "fen_string")]
    estimated_amount_fen: i128,
    quantity_shares: i128,
}

impl TradeAmountEstimate {
    pub fn amount_origin(&self) -> AmountOrigin {
        self.amount_origin
    }
    pub fn estimated_amount_fen(&self) -> i128 {
        self.estimated_amount_fen
    }
    /// Display conversion only; use integer fen for storage and addition.
    pub fn estimated_amount_yuan(&self) -> f64 {
        self.estimated_amount_fen as f64 / 100.0
    }
    pub fn quantity_shares(&self) -> i128 {
        self.quantity_shares
    }
}

fn fen_string<S: Serializer>(value: &i128, serializer: S) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(&value.to_string())
}

fn optional_fen_string<S: Serializer>(
    value: &Option<i128>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    match value {
        Some(value) => serializer.serialize_some(&value.to_string()),
        None => serializer.serialize_none(),
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct TradeAmountOutcome {
    record_index: usize,
    page_start: u16,
    phase: TradePhase,
    qualification: AmountQualification,
    estimate: Option<TradeAmountEstimate>,
    unavailable_reason: Option<AmountUnavailableReason>,
}

impl TradeAmountOutcome {
    pub fn record_index(&self) -> usize {
        self.record_index
    }
    pub fn page_start(&self) -> u16 {
        self.page_start
    }
    pub fn phase(&self) -> TradePhase {
        self.phase
    }
    pub fn qualification(&self) -> AmountQualification {
        self.qualification
    }
    /// Candidates are exposed here, but require an explicit summary option.
    pub fn estimate(&self) -> Option<&TradeAmountEstimate> {
        self.estimate.as_ref()
    }
    pub fn unavailable_reason(&self) -> Option<AmountUnavailableReason> {
        self.unavailable_reason
    }
}

#[derive(Debug, Clone, Copy, Default)]
pub struct TradeEstimateOptions {
    pub include_candidates: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct TradeEstimateComponent {
    availability: AmountAvailability,
    unavailable_reason: Option<AmountUnavailableReason>,
    records: usize,
    quantity_shares: i128,
    #[serde(serialize_with = "optional_fen_string")]
    estimated_amount_fen: Option<i128>,
}

impl TradeEstimateComponent {
    pub fn availability(&self) -> AmountAvailability {
        self.availability
    }
    pub fn unavailable_reason(&self) -> Option<AmountUnavailableReason> {
        self.unavailable_reason
    }
    pub fn records(&self) -> usize {
        self.records
    }
    pub fn quantity_shares(&self) -> i128 {
        self.quantity_shares
    }
    pub fn estimated_amount_fen(&self) -> Option<i128> {
        self.estimated_amount_fen
    }
    pub fn estimated_amount_yuan(&self) -> Option<f64> {
        self.estimated_amount_fen.map(|v| v as f64 / 100.0)
    }
    fn empty() -> Self {
        Self {
            availability: AmountAvailability::Unavailable,
            unavailable_reason: Some(AmountUnavailableReason::NoSupportedExecutions),
            records: 0,
            quantity_shares: 0,
            estimated_amount_fen: None,
        }
    }
    fn add(&mut self, value: &TradeAmountEstimate) -> io::Result<()> {
        self.estimated_amount_fen = Some(
            self.estimated_amount_fen
                .unwrap_or(0)
                .checked_add(value.estimated_amount_fen)
                .ok_or_else(|| invalid("估算金额合计溢出"))?,
        );
        self.quantity_shares = self
            .quantity_shares
            .checked_add(value.quantity_shares)
            .ok_or_else(|| invalid("估算股数合计溢出"))?;
        self.records += 1;
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct TradeEstimateSummary {
    schema_version: u16,
    amount_origin: AmountOrigin,
    context: TradeAmountContext,
    trade_coverage: TradeCoverage,
    date_basis: TradeAmountDateBasis,
    /// In request order, including the empty page. Missing provenance stays None.
    page_sources: Vec<Option<super::TradeSource>>,
    regular_with_auctions: TradeEstimateComponent,
    after_hours: TradeEstimateComponent,
    candidates: TradeEstimateComponent,
    raw_records: usize,
    excluded_records: usize,
    candidate_records: usize,
    unknown_records: usize,
    unavailable_records: usize,
}

impl TradeEstimateSummary {
    pub fn context(&self) -> &TradeAmountContext {
        &self.context
    }
    pub fn trade_coverage(&self) -> TradeCoverage {
        self.trade_coverage
    }
    pub fn date_basis(&self) -> TradeAmountDateBasis {
        self.date_basis
    }
    pub fn page_sources(&self) -> &[Option<super::TradeSource>] {
        &self.page_sources
    }
    pub fn regular_with_auctions(&self) -> &TradeEstimateComponent {
        &self.regular_with_auctions
    }
    pub fn after_hours(&self) -> &TradeEstimateComponent {
        &self.after_hours
    }
    pub fn candidates(&self) -> &TradeEstimateComponent {
        &self.candidates
    }
    pub fn raw_records(&self) -> usize {
        self.raw_records
    }
    pub fn excluded_records(&self) -> usize {
        self.excluded_records
    }
    pub fn candidate_records(&self) -> usize {
        self.candidate_records
    }
    pub fn unknown_records(&self) -> usize {
        self.unknown_records
    }
    pub fn unavailable_records(&self) -> usize {
        self.unavailable_records
    }
}

pub(super) fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidInput, message)
}

fn estimate(
    record: &TradeRecord,
    context: &TradeAmountContext,
    start: u16,
    index: usize,
) -> io::Result<TradeAmountOutcome> {
    let qualification = match record.qualification() {
        TradeQualification::ConfirmedByProfile => AmountQualification::SupportedByProfile,
        TradeQualification::Candidate => AmountQualification::Candidate,
        TradeQualification::ExcludedByProfile => AmountQualification::Excluded,
        TradeQualification::Unknown => AmountQualification::Unknown,
    };
    let reason = if qualification == AmountQualification::Excluded {
        Some(AmountUnavailableReason::NotExecution)
    } else if qualification == AmountQualification::Unknown {
        Some(AmountUnavailableReason::UnqualifiedEvent)
    } else if !is_a_share(context.query.market(), context.query.code()) {
        Some(AmountUnavailableReason::UnsupportedProfile)
    } else if context.quantity_unit == TradeQuantityUnit::Unknown {
        Some(AmountUnavailableReason::UnknownQuantityUnit)
    } else {
        None
    };
    let value = if reason.is_none() {
        if record.price_raw() < 0 || record.volume_raw() < 0 {
            return Err(invalid("估算价格或数量为负"));
        }
        let shares = i128::from(record.volume_raw())
            .checked_mul(
                context
                    .quantity_unit
                    .shares_per_unit()
                    .ok_or_else(|| invalid("未知数量单位"))?,
            )
            .ok_or_else(|| invalid("估算数量转换溢出"))?;
        let fen = i128::from(record.price_raw())
            .checked_mul(shares)
            .ok_or_else(|| invalid("估算金额乘法溢出"))?;
        Some(TradeAmountEstimate {
            amount_origin: AmountOrigin::RecordPriceTimesQuantity,
            estimated_amount_fen: fen,
            quantity_shares: shares,
        })
    } else {
        None
    };
    Ok(TradeAmountOutcome {
        record_index: index,
        page_start: start,
        phase: record.phase(),
        qualification,
        estimate: value,
        unavailable_reason: reason,
    })
}

impl TradePage {
    pub fn amount_estimates(
        &self,
        context: &TradeAmountContext,
    ) -> io::Result<Vec<TradeAmountOutcome>> {
        context.check(self.query())?;
        self.records()
            .iter()
            .enumerate()
            .map(|(i, r)| estimate(r, context, self.start(), i))
            .collect()
    }
}

impl EnhancedHistoryTransactionPage {
    /// Mutable compatibility DTOs are checked against a fresh wire decode.
    pub fn amount_estimates(
        &self,
        context: &TradeAmountContext,
    ) -> io::Result<Vec<TradeAmountOutcome>> {
        let request = EnhancedHistoryTransactionRequest::new(
            self.market,
            &self.code,
            self.start,
            self.request_count,
            self.date,
        )?;
        let decoded = decode_enhanced_history_transactions(&request, &self.raw_payload)?;
        if decoded.records != self.records
            || decoded.price_base_raw_bytes != self.price_base_raw_bytes
            || decoded.price_base_raw_f32.to_bits() != self.price_base_raw_f32.to_bits()
        {
            return Err(invalid("增强成交DTO与原始响应不一致"));
        }
        let query = TradeQuery::history(self.market, &self.code, self.date)?;
        decode_trade_page(&query, self.start, self.request_count, &self.raw_payload)?
            .amount_estimates(context)
    }
}

fn summarize(
    query: &TradeQuery,
    pages: &[TradePage],
    coverage: TradeCoverage,
    context: &TradeAmountContext,
    options: TradeEstimateOptions,
) -> io::Result<TradeEstimateSummary> {
    context.check(query)?;
    let mut result = TradeEstimateSummary {
        schema_version: TRADE_AMOUNT_SCHEMA_VERSION,
        amount_origin: AmountOrigin::RecordPriceTimesQuantity,
        context: context.clone(),
        trade_coverage: coverage,
        date_basis: if query.date().is_some() {
            TradeAmountDateBasis::RequestedDate
        } else if !pages.is_empty() && pages.iter().all(|page| page.source().is_some()) {
            TradeAmountDateBasis::ObservedDateUnverified
        } else {
            TradeAmountDateBasis::Unavailable
        },
        page_sources: pages.iter().map(|page| page.source().cloned()).collect(),
        regular_with_auctions: TradeEstimateComponent::empty(),
        after_hours: TradeEstimateComponent::empty(),
        candidates: TradeEstimateComponent::empty(),
        raw_records: 0,
        excluded_records: 0,
        candidate_records: 0,
        unknown_records: 0,
        unavailable_records: 0,
    };
    for page in pages {
        for outcome in page.amount_estimates(context)? {
            result.raw_records += 1;
            match outcome.qualification {
                AmountQualification::Excluded => result.excluded_records += 1,
                AmountQualification::Unknown => result.unknown_records += 1,
                AmountQualification::Candidate => result.candidate_records += 1,
                AmountQualification::SupportedByProfile => {}
            }
            if let Some(value) = &outcome.estimate {
                if outcome.qualification == AmountQualification::Candidate {
                    if options.include_candidates {
                        result.candidates.add(value)?;
                    }
                } else if outcome.phase == TradePhase::AfterHoursFixedPrice {
                    result.after_hours.add(value)?;
                } else {
                    result.regular_with_auctions.add(value)?;
                }
            } else {
                result.unavailable_records += 1;
            }
        }
    }
    let incomplete = coverage != TradeCoverage::SourceEnumerated
        || result.candidate_records > 0
        || result.unknown_records > 0
        || result.unavailable_records > result.excluded_records;
    for component in [&mut result.regular_with_auctions, &mut result.after_hours] {
        if component.records > 0 {
            component.unavailable_reason = None;
            component.availability = if incomplete {
                AmountAvailability::Partial
            } else {
                AmountAvailability::Available
            };
        } else if context.quantity_unit == TradeQuantityUnit::Unknown {
            component.unavailable_reason = Some(AmountUnavailableReason::UnknownQuantityUnit);
        }
    }
    if result.candidates.records > 0 {
        result.candidates.availability = AmountAvailability::Partial;
        result.candidates.unavailable_reason = None;
    }
    Ok(result)
}

impl TradeCollection {
    pub fn amount_estimate_summary(
        &self,
        context: &TradeAmountContext,
        options: TradeEstimateOptions,
    ) -> io::Result<TradeEstimateSummary> {
        summarize(
            self.query(),
            self.pages(),
            self.coverage(),
            context,
            options,
        )
    }
}

impl TransactionCollection {
    pub fn amount_estimate_summary(
        &self,
        context: &TradeAmountContext,
        options: TradeEstimateOptions,
    ) -> io::Result<TradeEstimateSummary> {
        // Public DTO fields must never promote coverage or change authoritative inputs.
        let first = self
            .event_pages
            .first()
            .ok_or_else(|| invalid("兼容集合缺少原始事件页"))?;
        let query = first.query();
        let command = match query.protocol() {
            super::TradeProtocol::Today0fc5 => 0x0fc5,
            super::TradeProtocol::EnhancedHistory0fc6 => 0x0fc6,
            super::TradeProtocol::LegacyHistory0fb5 => 0x0fb5,
        };
        if self.market != query.market()
            || self.code != query.code()
            || self.requested_date != query.date()
            || self.command != command
            || first.start() != 0
            || self.pages.len() != self.event_pages.len()
            || self.event_pages.last().is_none_or(|p| p.raw_count() != 0)
        {
            return Err(invalid("兼容成交集合身份或终止页不一致"));
        }
        let mut next = 0;
        let mut source = None;
        let mut all_sources = true;
        for (index, (page, info)) in self.event_pages.iter().zip(&self.pages).enumerate() {
            if page.query() != query
                || u32::from(page.start()) != next
                || info.start != page.start()
                || info.raw_count != page.raw_count()
                || info.request_count != page.request_count()
                || (page.raw_count() == 0 && index + 1 != self.event_pages.len())
            {
                return Err(invalid("兼容成交集合页信息不一致"));
            }
            next = page.next_offset();
            if let Some(current) = page.source() {
                if current.endpoint() != self.source_endpoint
                    || source.is_some_and(|old: &super::TradeSource| {
                        old.connection_id() != current.connection_id()
                            || (query.protocol() == super::TradeProtocol::Today0fc5
                                && old.observed_date() != current.observed_date())
                    })
                {
                    return Err(invalid("兼容成交集合来源不一致"));
                }
                source = Some(current);
            } else {
                all_sources = false;
            }
        }
        let original: Vec<_> = self
            .event_pages
            .iter()
            .rev()
            .flat_map(|page| page.records().iter().map(TradeRecord::to_legacy))
            .collect();
        let mut cursor = 0;
        for (page, info) in self.event_pages.iter().zip(&self.pages).rev() {
            let end = cursor + page.raw_count();
            if info.record_range != (cursor..end) {
                return Err(invalid("兼容成交集合记录范围不一致"));
            }
            cursor = end;
        }
        if serde_comparable_records(&original) != serde_comparable_records(&self.records) {
            return Err(invalid("兼容成交记录与原始事件页不一致"));
        }
        if original.windows(2).any(|p| p[0].time > p[1].time) {
            return Err(invalid("兼容成交集合时间顺序不一致"));
        }
        let coverage = if all_sources && query.date().is_some() {
            TradeCoverage::SourceEnumerated
        } else {
            TradeCoverage::Unknown
        };
        summarize(query, &self.event_pages, coverage, context, options)
    }
}

fn serde_comparable_records(records: &[super::TransactionData]) -> Vec<(&str, u64, i32, i32, i32)> {
    records
        .iter()
        .map(|r| {
            (
                r.time.as_str(),
                r.price.to_bits(),
                r.vol,
                r.num,
                r.buyorsell,
            )
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn overflow_is_an_error_not_a_wrapped_amount() {
        let mut component = TradeEstimateComponent::empty();
        component
            .add(&TradeAmountEstimate {
                amount_origin: AmountOrigin::RecordPriceTimesQuantity,
                estimated_amount_fen: i128::MAX,
                quantity_shares: 1,
            })
            .unwrap();
        assert!(
            component
                .add(&TradeAmountEstimate {
                    amount_origin: AmountOrigin::RecordPriceTimesQuantity,
                    estimated_amount_fen: 1,
                    quantity_shares: 1,
                })
                .is_err()
        );
    }
}
