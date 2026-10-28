//! Checked 0x044d directory. Classifications are code-prefix hints, not wire fields.
use std::{collections::HashSet, io, net::SocketAddr};

use serde::Serialize;

use crate::tcp::{SecurityCount, Tcp, Tdx, reader::Reader};

pub const SECURITY_DIRECTORY_COMMAND: u16 = 0x044d;
pub const MAX_SECURITY_DIRECTORY_COUNT: u16 = 1600;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SecurityDirectoryRequest {
    market: u16,
    start: u32,
    count: u16,
}

impl SecurityDirectoryRequest {
    /// Count is 1–1600. The wire stores count and offset as u32.
    pub fn new(market: u16, start: u32, count: u16) -> io::Result<Self> {
        if market > 1 || !(1..=MAX_SECURITY_DIRECTORY_COUNT).contains(&count) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "目录市场须为0/1，数量须为1–1600",
            ));
        }
        Ok(Self {
            market,
            start,
            count,
        })
    }

    pub fn encode(self) -> [u8; 26] {
        let mut request = [0; 26];
        request[..12].copy_from_slice(&[0x0c, 1, 0, 0, 0, 1, 16, 0, 16, 0, 0x4d, 4]);
        request[12..14].copy_from_slice(&self.market.to_le_bytes());
        request[14..18].copy_from_slice(&self.start.to_le_bytes());
        request[18..22].copy_from_slice(&u32::from(self.count).to_le_bytes());
        request
    }

    pub fn recv(self, tcp: &mut Tcp) -> io::Result<SecurityDirectoryPage> {
        let bytes = crate::tcp::send_recv_decompress(tcp, &self.encode(), "证券目录044d")?;
        let mut page = decode_security_directory(&self, &bytes)?;
        page.source_endpoint = Some(tcp.get_ref().0.peer_addr()?);
        Ok(page)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum SecurityCategory {
    Index,
    Etf,
    AShare,
    BShare,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum SecurityBoard {
    ShanghaiMain,
    Star,
    ShenzhenMain,
    Chinext,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SecurityDirectoryData {
    pub market: u16,
    pub code: String,
    /// Full 16-byte GBK name, without NUL padding.
    pub name: String,
    pub volume_unit: u16,
    pub decimal: u8,
    pub previous_close: f32,
    /// Community profile interpretation of the first unknown slot.
    pub volume_ratio_base: f32,
    pub unknown0_raw: [u8; 4],
    pub previous_close_raw: [u8; 4],
    pub unknown3_raw: [u8; 4],
    pub category: SecurityCategory,
    pub board: SecurityBoard,
}

impl SecurityDirectoryData {
    pub fn to_legacy(&self) -> super::SecurityListData {
        super::SecurityListData {
            code: self.code.clone(),
            name: self.name.clone(),
            volunit: u32::from(self.volume_unit),
            decimal_point: self.decimal,
            pre_close: f64::from(self.previous_close),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct SecurityDirectoryPage {
    pub market: u16,
    pub start: u32,
    pub request_count: u16,
    pub records: Vec<SecurityDirectoryData>,
    pub raw_payload: Vec<u8>,
    pub source_endpoint: Option<SocketAddr>,
}

pub fn decode_security_directory(
    request: &SecurityDirectoryRequest,
    bytes: &[u8],
) -> io::Result<SecurityDirectoryPage> {
    let mut reader = Reader::new(bytes);
    let count = reader.u16()?;
    if count > request.count || bytes.len() != 2 + usize::from(count) * 37 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "目录044d条数或37字节记录长度不符",
        ));
    }
    let mut records = Vec::with_capacity(usize::from(count));
    for _ in 0..count {
        crate::operation::check()?;
        let code = reader.code()?;
        let volume_unit = reader.u16()?;
        let name = crate::tcp::helper::gbk_to_string_trim_null(reader.take(16)?);
        let unknown0_raw = reader.u32()?.to_le_bytes();
        let decimal = reader.u8()?;
        let previous_close_raw = reader.u32()?.to_le_bytes();
        let unknown3_raw = reader.u32()?.to_le_bytes();
        let previous_close = f32::from_le_bytes(previous_close_raw);
        let volume_ratio_base = f32::from_le_bytes(unknown0_raw);
        if !previous_close.is_finite() || previous_close < 0.0 || !volume_ratio_base.is_finite() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "目录044d浮点字段非法",
            ));
        }
        let (category, board) = classify(request.market, &code);
        records.push(SecurityDirectoryData {
            market: request.market,
            code,
            name,
            volume_unit,
            decimal,
            previous_close,
            volume_ratio_base,
            unknown0_raw,
            previous_close_raw,
            unknown3_raw,
            category,
            board,
        });
    }
    Ok(SecurityDirectoryPage {
        market: request.market,
        start: request.start,
        request_count: request.count,
        records,
        raw_payload: bytes.to_vec(),
        source_endpoint: None,
    })
}

fn classify(market: u16, code: &str) -> (SecurityCategory, SecurityBoard) {
    use SecurityBoard::*;
    use SecurityCategory::*;
    let prefix = &code[..3];
    let board = match (market, prefix) {
        (1, "688" | "689") => Star,
        (1, "600" | "601" | "603" | "605") => ShanghaiMain,
        (0, "000" | "001" | "002" | "003" | "004") => ShenzhenMain,
        (0, _) if code.starts_with("30") => Chinext,
        _ => SecurityBoard::Unknown,
    };
    let category = if board != SecurityBoard::Unknown {
        AShare
    } else {
        match (market, prefix) {
            (0, "399") | (1, "000" | "880" | "881" | "999") => Index,
            (0, "158" | "159")
            | (
                1,
                "510" | "511" | "512" | "513" | "515" | "516" | "517" | "518" | "520" | "560"
                | "561" | "562" | "563" | "588",
            ) => Etf,
            (1, "900") => BShare,
            (0, _) if code.starts_with("20") => BShare,
            _ => SecurityCategory::Unknown,
        }
    };
    (category, board)
}

/// Complete directory against 044e's advertised count, on one connection.
/// Short pages advance by actual count; duplicates, early empty pages and
/// changes of connection or exchange calendar day fail atomically.
pub fn security_directory(tcp: &mut Tcp, market: u16) -> io::Result<Vec<SecurityDirectoryData>> {
    SecurityDirectoryRequest::new(market, 0, MAX_SECURITY_DIRECTORY_COUNT)?;
    tcp.with_timeout(crate::tcp::network_policy().pagination_timeout, |tcp| {
        let day = super::quote_metadata::local_date(chrono::Utc::now());
        let mut count_request = SecurityCount::new(market);
        let total = usize::from(*count_request.recv_parsed(tcp)?);
        let session = tcp.connection_id();
        let mut records = Vec::with_capacity(total);
        let mut codes = HashSet::with_capacity(total);
        while records.len() < total {
            if !records.is_empty() {
                tcp.delay(std::time::Duration::from_millis(100))?;
            }
            let request = SecurityDirectoryRequest::new(
                market,
                records.len() as u32,
                (total - records.len()).min(usize::from(MAX_SECURITY_DIRECTORY_COUNT)) as u16,
            )?;
            let page = request.recv(tcp)?;
            if tcp.connection_id() != session
                || super::quote_metadata::local_date(chrono::Utc::now()) != day
            {
                return Err(io::Error::new(
                    io::ErrorKind::ConnectionAborted,
                    "证券目录跨连接或跨日",
                ));
            }
            if page.records.is_empty() {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "证券目录未达到044e总数就返回空页",
                ));
            }
            for record in &page.records {
                if !codes.insert(record.code.clone()) {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "证券目录代码重复",
                    ));
                }
            }
            records.extend(page.records);
        }
        Ok(records)
    })
}
