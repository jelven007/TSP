//! Per-client metadata with immutable generations. Never performs I/O under a lock.
use super::{PriceScale, QuoteData, QuotesRequest, SecurityListData};
use chrono::{DateTime, NaiveDate, Utc};
use serde::Serialize;
use std::{
    collections::BTreeMap,
    io,
    net::SocketAddr,
    sync::{Arc, RwLock},
};

#[derive(Debug, Clone, Serialize)]
pub struct MarketPrecision {
    pub market: u16,
    pub fetched_at: DateTime<Utc>,
    pub as_of_date: NaiveDate,
    pub source_endpoint: Option<SocketAddr>,
    pub securities: usize,
    pub unsupported: usize,
}

#[derive(Debug, Clone)]
pub struct PrecisionSnapshot {
    generation: u64,
    entries: BTreeMap<u16, BTreeMap<String, u8>>,
    markets: BTreeMap<u16, MarketPrecision>,
}

impl PrecisionSnapshot {
    /// Explicit offline metadata; no code-prefix inference or implicit global table.
    pub fn from_decimals(
        entries: &[(u16, &str, u8)],
        fetched_at: DateTime<Utc>,
        source_endpoint: Option<SocketAddr>,
    ) -> io::Result<Self> {
        let mut snapshot = Self::empty();
        for &(market, code, decimal) in entries {
            QuotesRequest::new(vec![(market, code)])?;
            if snapshot
                .entries
                .entry(market)
                .or_default()
                .insert(code.into(), decimal)
                .is_some()
            {
                return Err(io::Error::new(io::ErrorKind::InvalidInput, "精度证券重复"));
            }
        }
        for market in [0, 1] {
            let values: Vec<_> = entries.iter().filter(|entry| entry.0 == market).collect();
            if !values.is_empty() {
                snapshot.markets.insert(
                    market,
                    MarketPrecision {
                        market,
                        fetched_at,
                        as_of_date: local_date(fetched_at),
                        source_endpoint,
                        securities: values.len(),
                        unsupported: values
                            .iter()
                            .filter(|entry| PriceScale::from_metadata(entry.2).is_err())
                            .count(),
                    },
                );
            }
        }
        Ok(snapshot)
    }
    fn empty() -> Self {
        Self {
            generation: 0,
            entries: BTreeMap::new(),
            markets: BTreeMap::new(),
        }
    }
    pub fn generation(&self) -> u64 {
        self.generation
    }
    pub fn status(&self) -> Vec<MarketPrecision> {
        self.markets.values().cloned().collect()
    }
    pub fn is_fresh(&self, market: u16, at: DateTime<Utc>) -> bool {
        self.markets
            .get(&market)
            .is_some_and(|entry| entry.as_of_date == local_date(at) && entry.fetched_at <= at)
    }
    pub fn scale(&self, market: u16, code: &str) -> io::Result<PriceScale> {
        let decimal = self
            .entries
            .get(&market)
            .and_then(|entries| entries.get(code))
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::NotFound,
                    format!("MissingPrecision: market={market}, code={code}; 请刷新完整目录"),
                )
            })?;
        PriceScale::from_metadata(*decimal)
    }
}

#[derive(Debug)]
pub struct QuoteMetadataStore {
    current: RwLock<Arc<PrecisionSnapshot>>,
}

impl Default for QuoteMetadataStore {
    fn default() -> Self {
        Self {
            current: RwLock::new(Arc::new(PrecisionSnapshot::empty())),
        }
    }
}

impl QuoteMetadataStore {
    pub fn snapshot(&self) -> io::Result<Arc<PrecisionSnapshot>> {
        self.current
            .read()
            .map(|value| Arc::clone(&value))
            .map_err(|_| io::Error::other("精度缓存锁已损坏"))
    }
    /// Explicit injection for offline consumers. Replaces all markets atomically.
    pub fn replace(&self, mut value: PrecisionSnapshot) -> io::Result<Arc<PrecisionSnapshot>> {
        let mut current = self
            .current
            .write()
            .map_err(|_| io::Error::other("精度缓存锁已损坏"))?;
        value.generation = current
            .generation
            .checked_add(1)
            .ok_or_else(|| io::Error::other("精度版本溢出"))?;
        let value = Arc::new(value);
        *current = Arc::clone(&value);
        Ok(value)
    }
    /// Publish only complete, checked directories. Failed construction leaves
    /// every previous market and generation unchanged.
    pub(super) fn publish(
        &self,
        directories: Vec<(u16, Vec<SecurityListData>, Option<SocketAddr>)>,
        fetched_at: DateTime<Utc>,
    ) -> io::Result<Arc<PrecisionSnapshot>> {
        let mut candidates = BTreeMap::new();
        for (market, rows, endpoint) in directories {
            if rows.is_empty() || rows.len() > u16::MAX as usize || candidates.contains_key(&market)
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "完整精度目录为空、超量或市场重复",
                ));
            }
            let inputs: Vec<_> = rows
                .iter()
                .map(|row| (market, row.code.as_str(), row.decimal_point))
                .collect();
            candidates.insert(
                market,
                PrecisionSnapshot::from_decimals(&inputs, fetched_at, endpoint)?,
            );
        }
        let mut current = self
            .current
            .write()
            .map_err(|_| io::Error::other("精度缓存锁已损坏"))?;
        let mut next = (**current).clone();
        for candidate in candidates.into_values() {
            next.entries.extend(candidate.entries);
            next.markets.extend(candidate.markets);
        }
        next.generation = current
            .generation
            .checked_add(1)
            .ok_or_else(|| io::Error::other("精度版本溢出"))?;
        let next = Arc::new(next);
        *current = Arc::clone(&next);
        Ok(next)
    }
}

pub(super) fn local_date(at: DateTime<Utc>) -> NaiveDate {
    at.with_timezone(&crate::data::exchange_timezone())
        .date_naive()
}

#[derive(Debug, Clone, Serialize)]
pub struct QuotePrecision {
    pub market: u16,
    pub code: String,
    pub decimal: u8,
    pub source_endpoint: Option<SocketAddr>,
}

#[derive(Debug, Clone, Serialize)]
pub struct QuotePage {
    pub records: Vec<QuoteData>,
    pub requested_codes: Vec<(u16, String)>,
    pub returned_codes: Vec<(u16, String)>,
    pub missing_codes: Vec<(u16, String)>,
    pub precision: Vec<QuotePrecision>,
    pub metadata_generation: u64,
    pub fetched_at: DateTime<Utc>,
    pub source_endpoint: Option<SocketAddr>,
}

pub(super) fn ensure_metadata(
    tcp: &mut crate::tcp::Tcp,
    store: &QuoteMetadataStore,
    markets: &[u16],
    force: bool,
) -> io::Result<Arc<PrecisionSnapshot>> {
    if markets.iter().any(|market| *market > 1) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "market须为0或1",
        ));
    }
    let start = Utc::now();
    let snapshot = store.snapshot()?;
    let mut directories = Vec::new();
    for &market in markets {
        if directories.iter().any(|(m, _, _)| *m == market) {
            continue;
        }
        if force || !snapshot.is_fresh(market, start) {
            let rows = super::stocks(tcp, market).map_err(|error| match error {
                crate::Error::Io(error) => error,
                error => io::Error::other(error),
            })?;
            directories.push((market, rows, Some(tcp.get_ref().0.peer_addr()?)));
        }
    }
    let end = Utc::now();
    if local_date(start) != local_date(end) {
        return Err(io::Error::other("精度加载跨日，请重新预热"));
    }
    if directories.is_empty() {
        Ok(snapshot)
    } else {
        store.publish(directories, end)
    }
}

pub(super) fn make_page(
    request: &QuotesRequest<'_>,
    records: Vec<QuoteData>,
    returned_codes: Vec<(u16, String)>,
    snapshot: &PrecisionSnapshot,
    endpoint: Option<SocketAddr>,
) -> io::Result<QuotePage> {
    let requested_codes: Vec<_> = request
        .stocks()
        .iter()
        .map(|&(m, c)| (m, c.into()))
        .collect();
    let missing_codes = requested_codes
        .iter()
        .filter(|key| !returned_codes.contains(key))
        .cloned()
        .collect();
    let precision = returned_codes
        .iter()
        .map(|(market, code)| {
            Ok(QuotePrecision {
                market: *market,
                code: code.clone(),
                decimal: snapshot.scale(*market, code)?.decimal(),
                source_endpoint: snapshot.markets.get(market).and_then(|m| m.source_endpoint),
            })
        })
        .collect::<io::Result<_>>()?;
    Ok(QuotePage {
        records,
        requested_codes,
        returned_codes,
        missing_codes,
        precision,
        metadata_generation: snapshot.generation(),
        fetched_at: Utc::now(),
        source_endpoint: endpoint,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_generations_preserve_old_readers_and_replace_only_one_market() {
        let store = Arc::new(QuoteMetadataStore::default());
        let now = Utc::now();
        let old = store
            .replace(
                PrecisionSnapshot::from_decimals(&[(0, "000001", 2), (1, "000001", 3)], now, None)
                    .unwrap(),
            )
            .unwrap();
        assert_ne!(
            old.scale(0, "000001").unwrap(),
            old.scale(1, "000001").unwrap()
        );
        let row = SecurityListData {
            code: "000002".into(),
            decimal_point: 2,
            name: "test".into(),
            volunit: 100,
            pre_close: 10.0,
        };
        let next = store.publish(vec![(0, vec![row], None)], now).unwrap();
        assert_eq!(next.generation(), old.generation() + 1);
        assert!(next.scale(0, "000001").is_err());
        assert!(next.scale(0, "000002").is_ok());
        assert_eq!(next.scale(1, "000001").unwrap().decimal(), 3);
        assert!(old.scale(0, "000001").is_ok());
        let generation = next.generation();
        assert!(store.publish(vec![(1, vec![], None)], now).is_err());
        assert_eq!(store.snapshot().unwrap().generation(), generation);
        assert!(!next.is_fresh(0, now + chrono::Duration::days(1)));
        assert!(!next.is_fresh(0, now - chrono::Duration::seconds(1)));
    }
}
