//! 0feb uses a date selector. The server does not echo the actual trading date.
use super::{PrecisionSnapshot, PriceScale, command_request::*};
use crate::tcp::{Tcp, reader::Reader};
use serde::Serialize;
use std::io;

use super::client::batch_budget::Retained;
super::client::batch_budget::impl_heap!(RecentMinutesRequest; symbol);
super::client::batch_budget::impl_heap!(MinuteDateControl;);
super::client::batch_budget::impl_heap!(MinuteDateReference; symbol, controls, source, sha256);
super::client::batch_budget::impl_heap!(DateBinding; reference, content, source, payload);

pub const RECENT_MINUTES_COMMAND: u16 = 0x0feb;
/// Version 2 encodes the negated YYYYMMDD integer, not a Gregorian ordinal.
pub const RECENT_MINUTES_DATE_SCHEMA_VERSION: u16 = 2;

/// A selector, nonempty response, or synthetic time label cannot qualify a date.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[non_exhaustive]
pub enum MinuteDateUnavailableReason {
    ServerDoesNotEchoTradingDate,
    EvidenceBindingChanged,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[non_exhaustive]
pub enum MinuteDateQualification {
    Unverified {
        reason: MinuteDateUnavailableReason,
    },
    /// Caller-maintained independent evidence; never a server-echoed date.
    IndependentlyReferenced {
        trading_date: u32,
    },
}

/// Independently dated price controls. Prices use the declared integer scale.
/// Volume and synthetic time labels are not qualified by these controls.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct MinuteDateControl {
    pub index: u16,
    pub price_raw: i64,
}

/// The caller must preserve the independently dated reference's original bytes.
/// Construction validates its scope, not the truth of its external source.
#[derive(Debug, Clone, Serialize)]
pub struct MinuteDateReference {
    symbol: CommandSymbol,
    trading_date: u32,
    scale: PriceScale,
    controls: Vec<MinuteDateControl>,
    source: String,
    sha256: String,
}

impl MinuteDateReference {
    pub fn new(
        symbol: CommandSymbol,
        trading_date: u32,
        scale: PriceScale,
        controls: Vec<MinuteDateControl>,
        source: &str,
        sha256: &str,
    ) -> io::Result<Self> {
        date(trading_date)?;
        if !source.starts_with("https://")
            || source.len() <= 8
            || source.len() > 4096
            || sha256.len() != 64
            || !sha256.bytes().all(|b| b.is_ascii_hexdigit())
            || controls.is_empty()
            || controls.len() > 240
            || controls
                .iter()
                .enumerate()
                .any(|(i, p)| p.index as usize != i || p.price_raw < 0)
        {
            return Err(input("分时日期参照的来源、摘要或完整价格点非法"));
        }
        Ok(Self {
            symbol,
            trading_date,
            scale,
            controls,
            source: source.into(),
            sha256: sha256.to_ascii_lowercase(),
        })
    }
}

#[derive(Debug, Clone)]
struct DateBinding {
    reference: MinuteDateReference,
    content: std::sync::Arc<[u8]>,
    source: std::sync::Arc<[u8]>,
    payload: std::sync::Arc<[u8]>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RecentMinutesRequest {
    symbol: CommandSymbol,
    requested_date: u32,
    selector: u32,
}
impl RecentMinutesRequest {
    pub fn new(market: u16, code: &str, requested_date: u32) -> io::Result<Self> {
        date(requested_date)?;
        // Wire selector is the two's complement of YYYYMMDD. The former
        // ordinal-based formula selected unrelated dates (Aug 17 -> Jun 9).
        let selector = requested_date.wrapping_neg();
        Ok(Self {
            symbol: CommandSymbol::new(market, code)?,
            requested_date,
            selector,
        })
    }
    pub fn encode(&self, id: u32) -> io::Result<Vec<u8>> {
        let mut body = self.selector.to_le_bytes().to_vec();
        self.symbol.encode(&mut body, false);
        crate::tcp::frame::request(RECENT_MINUTES_COMMAND, id, &body)
    }
    pub fn recv(&self, tcp: &mut Tcp, metadata: &PrecisionSnapshot) -> io::Result<MinuteSeries> {
        if !metadata.is_fresh(self.symbol.market(), chrono::Utc::now()) {
            return Err(input("近期分时精度目录过期"));
        }
        metadata.scale(self.symbol.market(), self.symbol.code())?;
        let wire_request = self.encode(crate::tcp::frame::sequence())?;
        let bytes = tcp.exchange_checked(&wire_request)?;
        if !metadata.is_fresh(self.symbol.market(), chrono::Utc::now()) {
            return Err(invalid("近期分时请求跨日，请重取目录"));
        }
        let mut series = decode_recent_minutes(self, &bytes, metadata)?;
        series.wire_request = Some(wire_request.into());
        series.page.source.attach(tcp)?;
        Ok(series)
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct MinutePoint {
    pub index: u16,
    /// Synthetic session label; it is not present on the wire.
    pub time_label: String,
    pub price: f64,
    pub average_price: f64,
    pub volume_raw: i64,
    pub price_field_raw: i64,
    pub average_field_raw: i64,
    pub price_raw: i64,
    pub average_raw: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum MinuteReferencePosition {
    SessionOpen,
    MorningClose,
    AfternoonOpen,
    CapturedTail,
    Interior,
}

#[derive(Debug, Clone, Serialize)]
pub struct MinuteReferenceDifference {
    pub index: u16,
    pub synthetic_label: String,
    pub position: MinuteReferencePosition,
    pub tcp_price_raw: i64,
    pub reference_price_raw: i64,
    pub tcp_cumulative_volume_raw: i64,
    pub reference_cumulative_volume_raw: Option<i64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct MinuteReferenceComparison {
    pub all_prices_match: bool,
    /// Raw comparison only; this never establishes either provider's unit.
    pub all_raw_cumulative_volumes_match: Option<bool>,
    pub differences: Vec<MinuteReferenceDifference>,
}

#[derive(Debug, Clone)]
pub struct MinuteSeries {
    pub request: RecentMinutesRequest,
    pub requested_date: u32,
    /// Compatibility metadata, never authoritative for publication.
    pub server_trading_date: Option<u32>,
    date_qualification: MinuteDateQualification,
    date_binding: Option<DateBinding>,
    wire_request: Option<std::sync::Arc<[u8]>>,
    pub date_selector_raw: u32,
    pub previous_close: f32,
    pub open_price: f32,
    pub previous_close_bits: u32,
    pub open_bits: u32,
    pub scale: PriceScale,
    pub page: CommandPage<MinutePoint>,
}

impl Retained for MinuteSeries {
    fn heap_bytes(&self) -> usize {
        self.request
            .heap_bytes()
            .saturating_add(self.page.heap_bytes())
            .saturating_add(self.date_binding.heap_bytes())
            .saturating_add(self.wire_request.heap_bytes())
    }
    fn rows(&self) -> usize {
        self.page.records.len()
    }
}

impl Serialize for MinuteSeries {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serde_json::json!({
            "request": self.request, "requested_date": self.requested_date,
            "server_trading_date": self.server_trading_date,
            "date_qualification": self.date_qualification(),
            "date_reference": self.date_reference(),
            "wire_request": self.wire_request(),
            "date_selector_raw": self.date_selector_raw,
            "previous_close": self.previous_close, "open_price": self.open_price,
            "previous_close_bits": self.previous_close_bits, "open_bits": self.open_bits,
            "scale": self.scale, "page": self.page,
        })
        .serialize(serializer)
    }
}

impl MinuteSeries {
    pub fn date_qualification(&self) -> MinuteDateQualification {
        if self.date_binding.is_some() && !self.binding_matches() {
            MinuteDateQualification::Unverified {
                reason: MinuteDateUnavailableReason::EvidenceBindingChanged,
            }
        } else {
            self.date_qualification
        }
    }

    /// Date-dependent adapters must call this before assigning a partition.
    /// Qualification is scoped to this exact response and caller reference.
    pub fn require_trading_date(&self) -> io::Result<u32> {
        if let MinuteDateQualification::IndependentlyReferenced { trading_date } =
            self.date_qualification()
        {
            Ok(trading_date)
        } else {
            Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "0feb交易日期未核实或证据绑定已改变，不能按请求日期发布",
            ))
        }
    }

    pub fn date_reference(&self) -> Option<&MinuteDateReference> {
        self.binding_matches().then(|| {
            &self
                .date_binding
                .as_ref()
                .expect("matched binding")
                .reference
        })
    }

    /// Exact encoded request captured by recv, including its message identity.
    /// Offline decoding cannot reconstruct this acquisition evidence.
    pub fn wire_request(&self) -> Option<&[u8]> {
        self.wire_request.as_deref()
    }

    /// Diagnose a complete independently dated reference without qualifying the
    /// date, time labels, or volume units. Optional volume controls must be raw
    /// cumulative values aligned to the same reference price points. The first
    /// TCP point includes earlier session volume; no artificial 09:30 is added.
    pub fn compare_reference(
        &self,
        reference: &MinuteDateReference,
        cumulative_volume_raw: Option<&[i64]>,
    ) -> io::Result<MinuteReferenceComparison> {
        if reference.symbol != self.request.symbol
            || reference.trading_date != self.request.requested_date
            || reference.scale != self.scale
            || reference.controls.len() != self.page.records.len()
            || self.page.source.opcode != RECENT_MINUTES_COMMAND
            || cumulative_volume_raw.is_some_and(|values| {
                values.len() != reference.controls.len()
                    || values.iter().any(|value| *value < 0)
                    || values.windows(2).any(|pair| pair[0] > pair[1])
            })
        {
            return Err(invalid("分时参照范围或完整原始累计量不一致"));
        }
        let mut decoded = decode_with_scale(&self.request, &self.page.raw_payload, self.scale)?;
        decoded.wire_request = self.wire_request.clone();
        if self.content()? != decoded.content()? {
            return Err(invalid("分时公开字段与原始正文不一致"));
        }
        let mut result = MinuteReferenceComparison {
            all_prices_match: true,
            all_raw_cumulative_volumes_match: cumulative_volume_raw.map(|_| true),
            differences: Vec::new(),
        };
        let mut cumulative = 0_i64;
        for (index, (control, point)) in reference
            .controls
            .iter()
            .zip(&self.page.records)
            .enumerate()
        {
            cumulative = add(cumulative, point.volume_raw)?;
            let reference_volume = cumulative_volume_raw.map(|values| values[index]);
            let price_matches =
                control.index == point.index && control.price_raw == point.price_raw;
            let volume_matches = reference_volume.is_none_or(|value| value == cumulative);
            result.all_prices_match &= price_matches;
            if let Some(matches) = &mut result.all_raw_cumulative_volumes_match {
                *matches &= volume_matches;
            }
            if !price_matches || !volume_matches {
                result.differences.push(MinuteReferenceDifference {
                    index: point.index,
                    synthetic_label: point.time_label.clone(),
                    position: match index {
                        0 => MinuteReferencePosition::SessionOpen,
                        119 => MinuteReferencePosition::MorningClose,
                        120 => MinuteReferencePosition::AfternoonOpen,
                        _ if index + 1 == self.page.records.len() => {
                            MinuteReferencePosition::CapturedTail
                        }
                        _ => MinuteReferencePosition::Interior,
                    },
                    tcp_price_raw: point.price_raw,
                    reference_price_raw: control.price_raw,
                    tcp_cumulative_volume_raw: cumulative,
                    reference_cumulative_volume_raw: reference_volume,
                });
            }
        }
        Ok(result)
    }

    /// Match all price points against an independently dated reference, then
    /// bind the exact request, raw bytes, scale, decoded values and acquisition.
    /// This does not qualify volumes, averages or synthetic time labels.
    pub fn with_date_reference(mut self, reference: MinuteDateReference) -> io::Result<Self> {
        if reference.symbol != self.request.symbol
            || reference.trading_date != self.request.requested_date
            || reference.scale != self.scale
            || reference.controls.len() != self.page.records.len()
            || self.page.source.opcode != RECENT_MINUTES_COMMAND
            || self.page.source.endpoint.is_none()
            || self.page.source.fetched_at.is_none()
            || self.page.source.connection_id.is_none()
            || self.page.source.metadata_generation.is_none()
            || self.wire_request.is_none()
            || reference
                .controls
                .iter()
                .zip(&self.page.records)
                .any(|(a, b)| a.index != b.index || a.price_raw != b.price_raw)
        {
            return Err(invalid("分时日期证据与证券、日期、节点或完整价格点不一致"));
        }
        // Re-decode before creating the binding: callers cannot first mutate a
        // public DTO and then qualify values that disagree with the raw bytes.
        let wire = self
            .wire_request()
            .ok_or_else(|| invalid("分时请求原码缺失"))?;
        let id = u32::from_le_bytes(
            wire.get(1..5)
                .ok_or_else(|| invalid("分时请求原码长度"))?
                .try_into()
                .map_err(|_| invalid("分时请求消息号"))?,
        );
        if self.request.encode(id)? != wire {
            return Err(invalid("分时请求原码与公开请求不一致"));
        }
        let mut decoded = decode_with_scale(&self.request, &self.page.raw_payload, self.scale)?;
        decoded.wire_request = self.wire_request.clone();
        if self.content()? != decoded.content()? {
            return Err(invalid("分时公开字段与原始正文不一致"));
        }
        let trading_date = reference.trading_date;
        self.date_binding = Some(DateBinding {
            reference,
            content: self.content()?.into(),
            source: serde_json::to_vec(&self.page.source)?.into(),
            payload: self.page.raw_payload.clone(),
        });
        self.date_qualification = MinuteDateQualification::IndependentlyReferenced { trading_date };
        Ok(self)
    }

    fn content(&self) -> io::Result<Vec<u8>> {
        serde_json::to_vec(&(
            &self.request,
            self.requested_date,
            self.server_trading_date,
            self.date_selector_raw,
            self.previous_close,
            self.open_price,
            self.previous_close_bits,
            self.open_bits,
            self.scale,
            &self.page.records,
            self.wire_request(),
        ))
        .map_err(io::Error::other)
    }

    fn binding_matches(&self) -> bool {
        self.date_binding.as_ref().is_some_and(|binding| {
            self.page.raw_payload == binding.payload
                && self
                    .content()
                    .is_ok_and(|bytes| bytes.as_slice() == binding.content.as_ref())
                && serde_json::to_vec(&self.page.source)
                    .is_ok_and(|bytes| bytes.as_slice() == binding.source.as_ref())
        })
    }
}

pub fn decode_recent_minutes(
    request: &RecentMinutesRequest,
    bytes: &[u8],
    metadata: &PrecisionSnapshot,
) -> io::Result<MinuteSeries> {
    let scale = metadata.scale(request.symbol.market(), request.symbol.code())?;
    let mut series = decode_with_scale(request, bytes, scale)?;
    series.page.source.metadata_generation = Some(metadata.generation());
    Ok(series)
}

fn decode_with_scale(
    request: &RecentMinutesRequest,
    bytes: &[u8],
    scale: PriceScale,
) -> io::Result<MinuteSeries> {
    bounded(bytes)?;
    let mut r = Reader::new(bytes);
    let n = usize::from(r.u16()?);
    let previous_close = r.f32()?;
    let open_price = r.f32()?;
    if previous_close < 0.0 || open_price < 0.0 {
        return Err(invalid("0feb昨收或今开为负"));
    }
    if n > 240 || n > r.remaining() / 3 {
        return Err(invalid("0feb点数或正文长度异常"));
    }
    let mut base = (0, 0);
    let mut points = Vec::with_capacity(n);
    for index in 0..n {
        let price_field_raw = r.varint64()?;
        let average_field_raw = r.varint64()?;
        let volume_raw = r.varint64()?;
        let price_raw = add(base.0, price_field_raw)?;
        let average_raw = add(base.1, average_field_raw)?;
        if index == 0 {
            base = (price_raw, average_raw);
        }
        if price_raw < 0 || average_raw < 0 || volume_raw < 0 {
            return Err(invalid("0feb负价格/均价/数量"));
        }
        let minute = if index < 120 {
            571 + index
        } else {
            781 + index - 120
        };
        points.push(MinutePoint {
            index: index as u16,
            time_label: format!("{:02}:{:02}", minute / 60, minute % 60),
            price: price_raw as f64 / f64::from(scale.divisor()),
            average_price: average_raw as f64 / (100.0 * f64::from(scale.divisor())),
            volume_raw,
            price_field_raw,
            average_field_raw,
            price_raw,
            average_raw,
        });
    }
    exact(&r)?;
    let page = CommandPage::new(RECENT_MINUTES_COMMAND, points, bytes);
    Ok(MinuteSeries {
        request: request.clone(),
        requested_date: request.requested_date,
        server_trading_date: None,
        date_qualification: MinuteDateQualification::Unverified {
            reason: MinuteDateUnavailableReason::ServerDoesNotEchoTradingDate,
        },
        date_binding: None,
        wire_request: None,
        date_selector_raw: request.selector,
        previous_close,
        open_price,
        previous_close_bits: previous_close.to_bits(),
        open_bits: open_price.to_bits(),
        scale,
        page,
    })
}

#[cfg(test)]
mod date_tests {
    use super::*;

    // Constructed transport/source controls, not live date evidence.
    fn sample() -> MinuteSeries {
        let request = RecentMinutesRequest::new(0, "000001", 20261022).unwrap();
        let mut bytes = 2_u16.to_le_bytes().to_vec();
        bytes.extend_from_slice(&10_f32.to_le_bytes());
        bytes.extend_from_slice(&10_f32.to_le_bytes());
        bytes.extend_from_slice(&[10, 20, 1, 1, 1, 2]);
        let mut series =
            decode_with_scale(&request, &bytes, PriceScale::from_metadata(2).unwrap()).unwrap();
        series.page.source.endpoint = Some("127.0.0.1:7709".parse().unwrap());
        series.page.source.connection_id = Some(1);
        series.page.source.fetched_at = Some(chrono::Utc::now());
        series.page.source.metadata_generation = Some(1);
        series.wire_request = Some(request.encode(7).unwrap().into());
        series
    }

    fn reference(series: &MinuteSeries) -> MinuteDateReference {
        MinuteDateReference::new(
            CommandSymbol::new(0, "000001").unwrap(),
            20261022,
            series.scale,
            series
                .page
                .records
                .iter()
                .map(|p| MinuteDateControl {
                    index: p.index,
                    price_raw: p.price_raw,
                })
                .collect(),
            "https://example.org/synthetic-unit-test-only",
            &"a".repeat(64),
        )
        .unwrap()
    }

    #[test]
    fn unverified_and_mismatched_controls_are_rejected() {
        let series = sample();
        assert_eq!(
            series.require_trading_date().unwrap_err().kind(),
            io::ErrorKind::Unsupported
        );
        for fault in 0..5 {
            let mut evidence = reference(&series);
            match fault {
                0 => evidence.symbol = CommandSymbol::new(1, "000001").unwrap(),
                1 => evidence.trading_date = 20261021,
                2 => evidence.scale = PriceScale::from_metadata(3).unwrap(),
                3 => {
                    evidence.controls.pop();
                }
                _ => evidence.controls[1].price_raw += 1,
            }
            assert!(series.clone().with_date_reference(evidence).is_err());
        }
        let mut changed = series.clone();
        changed.page.records[0].price = 99.0;
        assert!(changed.with_date_reference(reference(&series)).is_err());
    }

    #[test]
    fn all_public_acquisition_and_value_changes_invalidate_the_binding() {
        let series = sample();
        let qualified = series
            .clone()
            .with_date_reference(reference(&series))
            .unwrap();
        assert_eq!(qualified.require_trading_date().unwrap(), 20261022);
        for fault in 0..19 {
            let mut changed = qualified.clone();
            match fault {
                0 => changed.request = RecentMinutesRequest::new(0, "000002", 20261022).unwrap(),
                1 => changed.requested_date += 1,
                2 => changed.server_trading_date = Some(20261022),
                3 => changed.date_selector_raw += 1,
                4 => changed.previous_close += 1.0,
                5 => changed.open_bits += 1,
                6 => changed.scale = PriceScale::from_metadata(3).unwrap(),
                7 => changed.page.records[0].time_label = "09:30".into(),
                8 => changed.page.records[0].volume_raw += 1,
                9 => changed.page.source.endpoint = Some("127.0.0.2:7709".parse().unwrap()),
                10 => changed.page.source.metadata_generation = Some(2),
                11 => changed.page.source.connection_id = Some(2),
                12 => changed.page.raw_payload = [0_u8].as_slice().into(),
                13 => changed.page.source.opcode = super::super::DELISTED_BARS_COMMAND,
                14 => changed.page.source.fetched_at = None,
                15 => changed.wire_request = Some(changed.request.encode(8).unwrap().into()),
                16 => {
                    changed.page.records.pop();
                }
                17 => changed.page.records[0].average_raw += 1,
                _ => {
                    let mut payload = changed.page.raw_payload.to_vec();
                    // Another syntactically valid response still loses the old binding.
                    *payload.last_mut().unwrap() += 1;
                    assert!(decode_with_scale(&changed.request, &payload, changed.scale).is_ok());
                    changed.page.raw_payload = payload.into();
                }
            }
            assert!(changed.require_trading_date().is_err(), "fault={fault}");
            assert!(changed.date_reference().is_none());
            assert!(
                serde_json::to_string(&changed)
                    .unwrap()
                    .contains("EvidenceBindingChanged")
            );
        }
    }
}
