use crate::tcp::{Tcp, reader::Reader};
use chrono::{DateTime, NaiveDate, Utc};
use serde::Serialize;
use std::{io, net::SocketAddr, sync::Arc, time::Duration};

super::client::batch_budget::impl_heap!(CommandSymbol; code);

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize)]
pub struct CommandSymbol {
    market: u16,
    code: String,
}
impl CommandSymbol {
    pub fn new(market: u16, code: &str) -> io::Result<Self> {
        super::requests::symbol(market, code)?;
        Ok(Self {
            market,
            code: code.into(),
        })
    }
    pub fn market(&self) -> u16 {
        self.market
    }
    pub fn code(&self) -> &str {
        &self.code
    }
    pub(crate) fn marker(&self) -> [u8; 7] {
        let mut marker = [0; 7];
        marker[0] = self.market as u8;
        marker[1..].copy_from_slice(self.code.as_bytes());
        marker
    }
    pub(crate) fn encode(&self, bytes: &mut Vec<u8>, wide: bool) {
        bytes.push(self.market as u8);
        if wide {
            bytes.push(0);
        }
        bytes.extend_from_slice(self.code.as_bytes());
    }
}

pub(crate) fn symbols(stocks: &[(u16, &str)], max: usize) -> io::Result<Vec<CommandSymbol>> {
    if stocks.is_empty() || stocks.len() > max {
        return Err(input("证券数量为空或超出客户端上限"));
    }
    let items = stocks
        .iter()
        .map(|&(m, c)| CommandSymbol::new(m, c))
        .collect::<io::Result<Vec<_>>>()?;
    let set: std::collections::BTreeSet<_> = items.iter().collect();
    if set.len() != items.len() {
        return Err(input("请求证券重复"));
    }
    Ok(items)
}

pub(crate) fn input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
pub(crate) fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
pub(crate) fn date(value: u32) -> io::Result<NaiveDate> {
    if !(10_000_101..=99_991_231).contains(&value) {
        return Err(input("日期必须为有效YYYYMMDD"));
    }
    NaiveDate::from_ymd_opt((value / 10000) as i32, value / 100 % 100, value % 100)
        .ok_or_else(|| input("日期必须为有效YYYYMMDD"))
}
pub(crate) fn exact(reader: &Reader<'_>) -> io::Result<()> {
    if reader.remaining() != 0 {
        Err(invalid("响应包含多余字节"))
    } else {
        Ok(())
    }
}
pub(crate) fn bounded(bytes: &[u8]) -> io::Result<()> {
    if bytes.len() > u16::MAX as usize {
        Err(invalid("正文超过单帧上限"))
    } else {
        Ok(())
    }
}
pub(crate) fn count(reader: &mut Reader<'_>, min: usize, max: usize) -> io::Result<usize> {
    let n = usize::from(reader.u16()?);
    if n > max || n > reader.remaining() / min {
        return Err(invalid("响应数量超过请求或正文容量"));
    }
    Ok(n)
}
pub(crate) fn add(base: i64, delta: i64) -> io::Result<i64> {
    base.checked_add(delta)
        .ok_or_else(|| invalid("价格整数溢出"))
}

#[derive(Debug, Clone, Serialize)]
pub struct CommandSource {
    pub opcode: u16,
    pub endpoint: Option<SocketAddr>,
    pub fetched_at: Option<DateTime<Utc>>,
    pub connection_id: Option<u64>,
    pub metadata_generation: Option<u64>,
}
impl CommandSource {
    pub(crate) fn offline(opcode: u16) -> Self {
        Self {
            opcode,
            endpoint: None,
            fetched_at: None,
            connection_id: None,
            metadata_generation: None,
        }
    }
    pub(crate) fn attach(&mut self, tcp: &Tcp) -> io::Result<()> {
        self.endpoint = Some(tcp.get_ref().0.peer_addr()?);
        self.fetched_at = Some(Utc::now());
        self.connection_id = Some(tcp.connection_id().get());
        crate::tcp::ip::capabilities::observe(
            self.endpoint.expect("peer address set"),
            self.opcode,
            tcp.connection_id().get(),
            crate::tcp::ip::CapabilityStatus::DecodedResponse,
        );
        Ok(())
    }
}
/// Raw payload is shared once per page, without copying it into every record.
#[derive(Debug, Clone, Serialize)]
pub struct CommandPage<T> {
    pub records: Vec<T>,
    pub source: CommandSource,
    #[serde(skip)]
    pub raw_payload: Arc<[u8]>,
}
impl<T> CommandPage<T> {
    pub(crate) fn new(opcode: u16, records: Vec<T>, bytes: &[u8]) -> Self {
        Self {
            records,
            source: CommandSource::offline(opcode),
            raw_payload: bytes.into(),
        }
    }
}
#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
pub enum CommandTermination {
    EmptyPage,
    ShortPage,
    PageBudget,
    RecordBudget,
    OffsetLimit,
}
#[derive(Debug, Clone, Copy)]
pub struct CommandScanOptions {
    pub max_pages: usize,
    pub max_records: usize,
    pub timeout: Duration,
}
impl Default for CommandScanOptions {
    fn default() -> Self {
        Self {
            max_pages: 256,
            max_records: 10_000,
            timeout: crate::tcp::network_policy().pagination_timeout,
        }
    }
}
impl CommandScanOptions {
    pub(crate) fn validate(self) -> io::Result<()> {
        if self.max_pages == 0
            || self.max_records == 0
            || self.timeout.is_zero()
            || self.max_pages > 65_536
            || self.max_records > 1_000_000
        {
            return Err(input("分页预算须为正且不超过65536页/100万条"));
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct CommandCollection<T> {
    pub pages: Vec<CommandPage<T>>,
    pub record_count: usize,
    pub termination: CommandTermination,
    pub complete: bool,
    /// Ranked pages can change while scanning; this is never an atomic snapshot.
    pub atomic: bool,
}

pub(crate) fn check_scan_source(
    baseline: &mut Option<CommandSource>,
    source: &CommandSource,
) -> io::Result<()> {
    if let Some(first) = baseline {
        let zone = chrono::FixedOffset::east_opt(8 * 3600).ok_or_else(|| invalid("交易日时区"))?;
        let day = |time: Option<DateTime<Utc>>| time.map(|t| t.with_timezone(&zone).date_naive());
        if first.endpoint != source.endpoint
            || first.connection_id != source.connection_id
            || day(first.fetched_at) != day(source.fetched_at)
        {
            return Err(invalid("分页连接或观察日期改变，请重新扫描"));
        }
    } else {
        *baseline = Some(source.clone());
    }
    Ok(())
}

#[cfg(test)]
mod scan_source_tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn scans_reject_endpoint_connection_and_shanghai_day_changes() {
        let source = CommandSource {
            opcode: 0x052b,
            endpoint: Some("127.0.0.1:7709".parse().unwrap()),
            fetched_at: Some(Utc.with_ymd_and_hms(2026, 10, 24, 15, 59, 59).unwrap()),
            connection_id: Some(1),
            metadata_generation: None,
        };
        let mut baseline = None;
        check_scan_source(&mut baseline, &source).unwrap();
        check_scan_source(&mut baseline, &source).unwrap();
        for change in 0..3 {
            let mut next = source.clone();
            match change {
                0 => next.endpoint = Some("127.0.0.2:7709".parse().unwrap()),
                1 => next.connection_id = Some(2),
                _ => next.fetched_at = Some(Utc.with_ymd_and_hms(2026, 10, 24, 16, 0, 0).unwrap()),
            }
            assert_eq!(
                check_scan_source(&mut baseline, &next).unwrap_err().kind(),
                io::ErrorKind::InvalidData
            );
        }
    }
}
