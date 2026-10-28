//! Explicit day-file reference consumption; never rewrites a 052b response.
use super::{DailyBar, DailyBarsPage, DailyBarsRequest, decode_daily_bars};
use crate::{data::CanonicalBar, file::day::Day};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, io, sync::Arc};

#[derive(Debug, Clone)]
pub struct DayFileReference {
    request: DailyBarsRequest,
    symbol: String,
    source: String,
    sha256: String,
    bytes: Arc<[u8]>,
    dates: BTreeMap<u32, usize>,
}

impl DayFileReference {
    /// Validate the exact reference bytes and declared symbol. The caller must
    /// establish that the external source/hash belongs to this symbol; a digest
    /// proves byte integrity, not exchange authority or transport authenticity.
    pub fn new(
        market: u16,
        code: &str,
        bytes: &[u8],
        source: &str,
        sha256: &str,
    ) -> io::Result<Self> {
        let request = DailyBarsRequest::new(market, code, 0, 1)?;
        if bytes.is_empty()
            || bytes.len() > 2 * 1024 * 1024
            || !bytes.len().is_multiple_of(32)
            || !source.starts_with("https://")
            || source.len() <= 8
            || source.len() > 4096
            || sha256.len() != 64
            || !sha256.bytes().all(|b| b.is_ascii_hexdigit())
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "day参照范围、来源或摘要非法",
            ));
        }
        let actual = format!("{:x}", Sha256::digest(bytes));
        if !actual.eq_ignore_ascii_case(sha256) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "day参照SHA256与原字节不符",
            ));
        }
        let prefix = match market {
            0 => "sz",
            1 => "sh",
            _ => "bj",
        };
        let symbol = format!("{prefix}{}", request.code());
        let mut dates = BTreeMap::new();
        let mut previous = None;
        for (index, chunk) in bytes.chunks_exact(32).enumerate() {
            crate::operation::check()?;
            let day = Day::try_from_bytes(&symbol, chunk).map_err(io::Error::other)?;
            CanonicalBar::from_day(&day).map_err(io::Error::other)?;
            if previous.is_some_and(|date| date >= day.date) {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "day参照日期须严格递增且唯一",
                ));
            }
            previous = Some(day.date);
            dates.insert(day.date, index * 32);
        }
        Ok(Self {
            request,
            symbol,
            source: source.into(),
            sha256: actual,
            bytes: bytes.into(),
            dates,
        })
    }

    pub fn symbol(&self) -> &str {
        &self.symbol
    }
    pub fn raw_bytes(&self) -> &[u8] {
        &self.bytes
    }

    /// Re-decode the TCP payload before comparison, refusing modified DTOs.
    /// Every TCP date must exist in the reference. Output business values are
    /// explicitly sourced from the day file, alongside unchanged TCP records.
    pub fn compare_page(&self, page: &DailyBarsPage) -> io::Result<DailyReferenceComparison> {
        if page.request.market() != self.request.market()
            || page.request.code() != self.request.code()
            || page.source.opcode != super::DELISTED_BARS_COMMAND
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "052b证券或命令与day参照不符",
            ));
        }
        let decoded = decode_daily_bars(&page.request, &page.raw_payload)?;
        if serde_json::to_vec(&decoded.records)? != serde_json::to_vec(&page.records)? {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "052b公开字段与原码不符",
            ));
        }
        let mut records = Vec::with_capacity(decoded.records.len());
        for tcp in decoded.records {
            crate::operation::check()?;
            let offset = self.dates.get(&tcp.date_raw).copied().ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("day参照缺少052b日期: {}", tcp.date_raw),
                )
            })?;
            let chunk = &self.bytes[offset..offset + 32];
            let integer = |offset| {
                u32::from_le_bytes(
                    chunk[offset..offset + 4]
                        .try_into()
                        .expect("32-byte record"),
                )
            };
            let mut differing_fields = Vec::new();
            for (field, matches) in [
                (
                    DailyReferenceField::Open,
                    tcp.open_price_milli == i64::from(integer(4)) * 10,
                ),
                (
                    DailyReferenceField::High,
                    tcp.high_price_milli == i64::from(integer(8)) * 10,
                ),
                (
                    DailyReferenceField::Low,
                    tcp.low_price_milli == i64::from(integer(12)) * 10,
                ),
                (
                    DailyReferenceField::Close,
                    tcp.close_price_milli == i64::from(integer(16)) * 10,
                ),
                (DailyReferenceField::Amount, tcp.amount_raw == integer(20)),
                (DailyReferenceField::Volume, tcp.volume_raw == integer(24)),
            ] {
                if !matches {
                    differing_fields.push(field);
                }
            }
            records.push(DailyReferenceRecord {
                tcp,
                reference: CanonicalBar::from_day(&Day::from_bytes(&self.symbol, chunk))
                    .map_err(io::Error::other)?,
                differing_fields,
            });
        }
        Ok(DailyReferenceComparison {
            reference_source: self.source.clone(),
            reference_sha256: self.sha256.clone(),
            tcp_source: page.source.clone(),
            records,
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum DailyReferenceField {
    Open,
    High,
    Low,
    Close,
    Amount,
    Volume,
}

#[derive(Debug, Clone, Serialize)]
pub struct DailyReferenceRecord {
    pub tcp: DailyBar,
    pub reference: CanonicalBar,
    pub differing_fields: Vec<DailyReferenceField>,
}

#[derive(Debug, Clone, Serialize)]
pub struct DailyReferenceComparison {
    pub reference_source: String,
    pub reference_sha256: String,
    pub tcp_source: super::CommandSource,
    pub records: Vec<DailyReferenceRecord>,
}
