//! Vendor-reported K-line turnover, retaining binary32 codes and wire pages.
use super::{
    AmountOrigin, KlineRequest, TradeQuantityUnit,
    trade_amount::{invalid, is_a_share},
};
use chrono::{DateTime, Timelike, Utc};
use serde::Serialize;
use std::{
    io::{self, ErrorKind},
    net::SocketAddr,
    num::NonZeroU64,
    ops::Range,
    time::Duration,
};

/// Amounts and reconciliation volumes use IEEE binary32, without legacy
/// volume-helper branches. Zero is zero; nonfinite/negative values are errors.
pub fn decode_nonnegative_binary32(raw: u32) -> io::Result<f64> {
    let value = f32::from_bits(raw);
    if !value.is_finite() || value < 0.0 {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "量额编码为非有限值或负值",
        ));
    }
    Ok(f64::from(value))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum TurnoverScope {
    Minute1,
    Minute5,
    RegularWithAuctions,
    AfterHours,
    Combined,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum TurnoverAvailability {
    Available,
    Partial,
    Unavailable,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum TurnoverUnavailableReason {
    UnconfirmedProfile,
    NoBarsForDate,
    NoIndependentAfterHoursSource,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum TurnoverDateBasis {
    WireBarDate,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum TurnoverSampling {
    PostSession,
    IntradayOrUnobserved,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum TurnoverSummation {
    NeumaierCompensated,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct TurnoverSource {
    endpoint: SocketAddr,
    connection_id: NonZeroU64,
    fetched_at: DateTime<Utc>,
    /// Required when process-local connection IDs are restored from an archive.
    archive_id: Option<String>,
}
impl TurnoverSource {
    pub fn archived(
        endpoint: SocketAddr,
        connection_id: NonZeroU64,
        fetched_at: DateTime<Utc>,
        archive_id: &str,
    ) -> io::Result<Self> {
        if archive_id.trim().is_empty() {
            return Err(invalid("离线成交额来源需要档案ID"));
        }
        Ok(Self {
            endpoint,
            connection_id,
            fetched_at,
            archive_id: Some(archive_id.into()),
        })
    }
    pub(super) fn capture(tcp: &crate::tcp::Tcp) -> io::Result<Self> {
        Ok(Self {
            endpoint: tcp.get_ref().0.peer_addr()?,
            connection_id: tcp.connection_id(),
            fetched_at: Utc::now(),
            archive_id: None,
        })
    }
    pub fn endpoint(&self) -> SocketAddr {
        self.endpoint
    }
    pub fn connection_id(&self) -> NonZeroU64 {
        self.connection_id
    }
    pub fn fetched_at(&self) -> DateTime<Utc> {
        self.fetched_at
    }
    pub fn archive_id(&self) -> Option<&str> {
        self.archive_id.as_deref()
    }
    pub(super) fn same_connection(&self, other: &Self) -> bool {
        self.endpoint == other.endpoint
            && self.connection_id == other.connection_id
            && self.archive_id == other.archive_id
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct TurnoverQuery {
    market: u16,
    code: String,
    date: u32,
    scope: TurnoverScope,
    category: u16,
}
impl TurnoverQuery {
    /// Categories 8/0/9(or explicitly 4) map to 1m/5m/regular daily turnover.
    /// AfterHours/Combined retain the daily component; no combined source total.
    pub fn new(
        market: u16,
        code: &str,
        date: u32,
        scope: TurnoverScope,
        category: u16,
    ) -> io::Result<Self> {
        KlineRequest::new(market, code, category, 0, 1)?;
        super::requests::checked_date(date, ErrorKind::InvalidInput)?;
        let valid = match scope {
            TurnoverScope::Minute1 => category == 8,
            TurnoverScope::Minute5 => category == 0,
            _ => matches!(category, 9 | 4),
        };
        if !valid {
            return Err(invalid("源成交额时段与原始K线周期不匹配"));
        }
        Ok(Self {
            market,
            code: code.into(),
            date,
            scope,
            category,
        })
    }
    pub fn market(&self) -> u16 {
        self.market
    }
    pub fn code(&self) -> &str {
        &self.code
    }
    pub fn date(&self) -> u32 {
        self.date
    }
    pub fn scope(&self) -> TurnoverScope {
        self.scope
    }
    pub fn category(&self) -> u16 {
        self.category
    }
}

/// Caller evidence for this security/date/endpoint, including regular-session
/// scope (auctions included, after-hours excluded) and quantity units by category.
#[derive(Debug, Clone, Serialize)]
pub struct TurnoverProfile {
    market: u16,
    code: String,
    date: u32,
    endpoint: SocketAddr,
    evidence: String,
}
impl TurnoverProfile {
    pub fn a_share(
        market: u16,
        code: &str,
        date: u32,
        endpoint: SocketAddr,
        evidence: &str,
    ) -> io::Result<Self> {
        super::requests::checked_date(date, ErrorKind::InvalidInput)?;
        if !is_a_share(market, code) || evidence.trim().is_empty() {
            return Err(invalid("源成交额profile需要受支持A股与时段/量额单位证据"));
        }
        Ok(Self {
            market,
            code: code.into(),
            date,
            endpoint,
            evidence: evidence.into(),
        })
    }
    pub fn evidence(&self) -> &str {
        &self.evidence
    }
    pub fn endpoint(&self) -> SocketAddr {
        self.endpoint
    }
    fn matches(&self, query: &TurnoverQuery, pages: &[ReportedTurnoverSeries]) -> bool {
        self.market == query.market
            && self.code == query.code
            && self.date == query.date
            && !pages.is_empty()
            && pages.iter().all(|p| {
                p.source
                    .as_ref()
                    .is_some_and(|s| s.endpoint == self.endpoint)
            })
    }
    pub fn quantity_unit(&self, category: u16) -> Option<TradeQuantityUnit> {
        match category {
            0 | 8 | 9 => Some(TradeQuantityUnit::Shares),
            4 => Some(TradeQuantityUnit::LotsOf100Shares),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ReportedTurnoverBar {
    date: u32,
    time_minutes: u16,
    wire_index: usize,
    byte_range: Range<usize>,
    raw_volume_u32: u32,
    raw_amount_u32: u32,
    reported_amount_yuan: f64,
    reported_volume_raw: f64,
}
impl ReportedTurnoverBar {
    pub fn date(&self) -> u32 {
        self.date
    }
    pub fn time_minutes(&self) -> u16 {
        self.time_minutes
    }
    pub fn wire_index(&self) -> usize {
        self.wire_index
    }
    pub fn byte_range(&self) -> Range<usize> {
        self.byte_range.clone()
    }
    pub fn raw_volume_u32(&self) -> u32 {
        self.raw_volume_u32
    }
    pub fn raw_amount_u32(&self) -> u32 {
        self.raw_amount_u32
    }
    pub fn reported_amount_yuan(&self) -> f64 {
        self.reported_amount_yuan
    }
    pub fn reported_volume_raw(&self) -> f64 {
        self.reported_volume_raw
    }
    /// One encoding step, not an error guarantee from the vendor.
    pub fn amount_encoding_step_yuan(&self) -> f64 {
        // At f32::MAX use the downward spacing; subnormals/zero also supported.
        let raw = self.raw_amount_u32 & 0x7fff_ffff;
        if raw < 0x7f7f_ffff {
            f64::from(f32::from_bits(raw + 1)) - self.reported_amount_yuan
        } else {
            self.reported_amount_yuan - f64::from(f32::from_bits(raw - 1))
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ReportedTurnoverSeries {
    market: u16,
    code: String,
    category: u16,
    start: u16,
    request_count: u16,
    command: u16,
    bars: Vec<ReportedTurnoverBar>,
    payload: Vec<u8>,
    source: Option<TurnoverSource>,
}
impl ReportedTurnoverSeries {
    pub fn market(&self) -> u16 {
        self.market
    }
    pub fn code(&self) -> &str {
        &self.code
    }
    pub fn category(&self) -> u16 {
        self.category
    }
    pub fn start(&self) -> u16 {
        self.start
    }
    pub fn request_count(&self) -> u16 {
        self.request_count
    }
    pub fn bars(&self) -> &[ReportedTurnoverBar] {
        &self.bars
    }
    pub fn payload(&self) -> &[u8] {
        &self.payload
    }
    pub fn source(&self) -> Option<&TurnoverSource> {
        self.source.as_ref()
    }
}

/// Pure decoder. Does not infer date, units, availability, or session coverage.
pub fn decode_reported_turnover(
    request: &KlineRequest<'_>,
    payload: &[u8],
    source: Option<TurnoverSource>,
) -> io::Result<ReportedTurnoverSeries> {
    if !matches!(request.category(), 0 | 4 | 8 | 9) {
        return Err(invalid("源成交额周期尚未支持"));
    }
    let bars = super::kline_request::decode_stock_bars_raw(request, payload)?
        .into_iter()
        .map(|raw| {
            let dt = raw.bar.dt;
            Ok(ReportedTurnoverBar {
                date: u32::from(dt.year) * 10000 + u32::from(dt.month) * 100 + u32::from(dt.day),
                time_minutes: dt.hour * 60 + dt.minute,
                wire_index: raw.wire_index,
                byte_range: raw.byte_range,
                raw_volume_u32: raw.raw_volume_u32,
                raw_amount_u32: raw.raw_amount_u32,
                reported_amount_yuan: raw.bar.amount,
                reported_volume_raw: decode_nonnegative_binary32(raw.raw_volume_u32)?,
            })
        })
        .collect::<io::Result<Vec<_>>>()?;
    Ok(ReportedTurnoverSeries {
        market: request.market(),
        code: request.code().into(),
        category: request.category(),
        start: request.start(),
        request_count: request.count(),
        command: 0x052d,
        bars,
        payload: payload.to_vec(),
        source,
    })
}

pub(super) fn compensated_sum(values: impl IntoIterator<Item = f64>) -> io::Result<f64> {
    let (mut sum, mut correction) = (0.0f64, 0.0f64);
    for value in values {
        if !value.is_finite() {
            return Err(invalid("成交额求和输入非有限"));
        }
        let next = sum + value;
        correction += if sum.abs() >= value.abs() {
            (sum - next) + value
        } else {
            (value - next) + sum
        };
        sum = next;
        if !sum.is_finite() || !correction.is_finite() {
            return Err(invalid("成交额求和溢出"));
        }
    }
    let result = sum + correction;
    if !result.is_finite() {
        return Err(invalid("成交额求和结果非有限"));
    }
    Ok(result)
}

#[derive(Debug, Clone, Serialize)]
pub struct ReportedTurnover {
    schema_version: u16,
    amount_origin: AmountOrigin,
    query: TurnoverQuery,
    availability: TurnoverAvailability,
    unavailable_reason: Option<TurnoverUnavailableReason>,
    regular_component_availability: TurnoverAvailability,
    reported_amount_yuan: Option<f64>,
    regular_component_reported_amount_yuan: Option<f64>,
    quantity_unit: Option<TradeQuantityUnit>,
    profile: Option<TurnoverProfile>,
    date_basis: TurnoverDateBasis,
    sampling: TurnoverSampling,
    summation: TurnoverSummation,
    pages: Vec<ReportedTurnoverSeries>,
    bars: Vec<ReportedTurnoverBar>,
}
pub type TurnoverOutcome = ReportedTurnover;

impl ReportedTurnover {
    /// Validate all pages, filter by wire date, then derive expected bar coverage.
    /// A single offline/intraday page never establishes a complete trading day.
    pub fn from_pages(
        query: TurnoverQuery,
        pages: Vec<ReportedTurnoverSeries>,
        profile: Option<&TurnoverProfile>,
    ) -> io::Result<Self> {
        let mut next = pages.first().map_or(0, |p| u32::from(p.start));
        let mut source: Option<&TurnoverSource> = None;
        let mut empty = false;
        let mut oldest = None;
        for page in &pages {
            if page.market != query.market
                || page.code != query.code
                || page.category != query.category
                || u32::from(page.start) != next
                || empty
            {
                return Err(invalid("源成交额页身份、周期或偏移不一致"));
            }
            next += page.bars.len() as u32;
            empty = page.bars.is_empty();
            let stamps: Vec<_> = page.bars.iter().map(|b| (b.date, b.time_minutes)).collect();
            if stamps.windows(2).any(|p| p[0] >= p[1])
                || oldest
                    .is_some_and(|previous| stamps.last().is_some_and(|stamp| *stamp >= previous))
            {
                return Err(invalid("源成交额页时间重叠或分页方向不一致"));
            }
            oldest = stamps.first().copied().or(oldest);
            if let Some(current) = &page.source {
                if source.is_some_and(|previous| !previous.same_connection(current)) {
                    return Err(invalid("源成交额页跨连接或档案"));
                }
                source = Some(current);
            }
        }
        let mut bars: Vec<_> = pages
            .iter()
            .flat_map(|p| &p.bars)
            .filter(|b| b.date == query.date)
            .cloned()
            .collect();
        bars.sort_by_key(|b| b.time_minutes);
        if bars
            .windows(2)
            .any(|pair| pair[0].time_minutes >= pair[1].time_minutes)
        {
            return Err(invalid("源成交额包含重复时间，不能去重后拼接"));
        }
        let profile = profile.filter(|p| p.matches(&query, &pages)).cloned();
        let sampling = if !pages.is_empty()
            && pages.iter().all(|p| {
                p.source.as_ref().is_some_and(|s| {
                    let observed = s.fetched_at + chrono::Duration::hours(8);
                    let target =
                        crate::data::date_from_u32(query.date).expect("checked query date");
                    observed.date_naive() > target
                        || (observed.date_naive() == target
                            && observed.hour() * 60 + observed.minute() > 900)
                })
            }) {
            TurnoverSampling::PostSession
        } else {
            TurnoverSampling::IntradayOrUnobserved
        };
        let labels: Vec<u16> = match query.category {
            8 => (571..=690).chain(781..=900).collect(),
            0 => (575..=690)
                .step_by(5)
                .chain((785..=900).step_by(5))
                .collect(),
            _ => vec![900],
        };
        if bars.iter().any(|b| !labels.contains(&b.time_minutes)) {
            return Err(invalid("目标日期K线时间不属于已支持的常规交易时段"));
        }
        let complete = bars.iter().map(|b| b.time_minutes).eq(labels)
            && sampling == TurnoverSampling::PostSession;
        let reason = if profile.is_none() {
            Some(TurnoverUnavailableReason::UnconfirmedProfile)
        } else if bars.is_empty() {
            Some(TurnoverUnavailableReason::NoBarsForDate)
        } else {
            None
        };
        let regular_amount = if reason.is_none() {
            Some(compensated_sum(
                bars.iter().map(|b| b.reported_amount_yuan),
            )?)
        } else {
            None
        };
        let regular_availability = if regular_amount.is_none() {
            TurnoverAvailability::Unavailable
        } else if complete {
            TurnoverAvailability::Available
        } else {
            TurnoverAvailability::Partial
        };
        let unsupported = matches!(
            query.scope,
            TurnoverScope::AfterHours | TurnoverScope::Combined
        );
        let quantity_unit = profile
            .as_ref()
            .and_then(|p| p.quantity_unit(query.category));
        Ok(Self {
            schema_version: super::TRADE_AMOUNT_SCHEMA_VERSION,
            amount_origin: AmountOrigin::VendorReported,
            query,
            availability: if unsupported {
                TurnoverAvailability::Unavailable
            } else {
                regular_availability
            },
            unavailable_reason: if unsupported {
                Some(TurnoverUnavailableReason::NoIndependentAfterHoursSource)
            } else {
                reason
            },
            regular_component_availability: regular_availability,
            reported_amount_yuan: if unsupported { None } else { regular_amount },
            regular_component_reported_amount_yuan: regular_amount,
            quantity_unit,
            profile,
            date_basis: TurnoverDateBasis::WireBarDate,
            sampling,
            summation: TurnoverSummation::NeumaierCompensated,
            pages,
            bars,
        })
    }
    pub fn query(&self) -> &TurnoverQuery {
        &self.query
    }
    pub fn availability(&self) -> TurnoverAvailability {
        self.availability
    }
    pub fn unavailable_reason(&self) -> Option<TurnoverUnavailableReason> {
        self.unavailable_reason
    }
    pub fn reported_amount_yuan(&self) -> Option<f64> {
        self.reported_amount_yuan
    }
    pub fn regular_component_reported_amount_yuan(&self) -> Option<f64> {
        self.regular_component_reported_amount_yuan
    }
    pub fn regular_component_availability(&self) -> TurnoverAvailability {
        self.regular_component_availability
    }
    pub fn quantity_unit(&self) -> Option<TradeQuantityUnit> {
        self.quantity_unit
    }
    pub fn sampling(&self) -> TurnoverSampling {
        self.sampling
    }
    pub fn profile(&self) -> Option<&TurnoverProfile> {
        self.profile.as_ref()
    }
    pub fn date_basis(&self) -> TurnoverDateBasis {
        self.date_basis
    }
    pub fn summation(&self) -> TurnoverSummation {
        self.summation
    }
    pub fn bars(&self) -> &[ReportedTurnoverBar] {
        &self.bars
    }
    pub fn pages(&self) -> &[ReportedTurnoverSeries] {
        &self.pages
    }
}

#[derive(Debug, Clone)]
pub struct TurnoverOptions {
    pub page_size: u16,
    pub max_pages: usize,
    pub max_records: usize,
    pub max_bytes: usize,
    pub timeout: Duration,
    pub page_delay: Duration,
    /// Regular daily defaults to 9. Set 4 explicitly for the alternate profile.
    pub daily_category: u16,
    pub profile: Option<TurnoverProfile>,
}
impl Default for TurnoverOptions {
    fn default() -> Self {
        Self {
            page_size: 800,
            max_pages: 100,
            max_records: 65536,
            max_bytes: 8 * 1024 * 1024,
            timeout: Duration::from_secs(120),
            page_delay: Duration::from_millis(100),
            daily_category: 9,
            profile: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compensated_sum_retains_small_terms_and_rejects_nonfinite_results() {
        assert_eq!(compensated_sum([1e20, 1.0, -1e20]).unwrap(), 1.0);
        assert!(compensated_sum([f64::MAX, f64::MAX]).is_err());
        assert!(compensated_sum([f64::NAN]).is_err());
    }
}
