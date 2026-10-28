//! 02cf/02d0 owned results. Assemble GBK bytes before decoding a whole section.
use super::command_request::*;
use crate::tcp::{Tcp, reader::Reader};
use encoding_rs::GBK;
use serde::Serialize;
use std::{io, sync::Arc, time::Duration};

super::client::batch_budget::impl_heap!(CompanyInfoDirectoryRequest; code);
super::client::batch_budget::impl_heap!(CompanyInfoTextRequest; code, filename_raw);

pub const COMPANY_INFO_DIRECTORY_COMMAND: u16 = 0x02cf;
pub const COMPANY_INFO_TEXT_COMMAND: u16 = 0x02d0;
pub const MAX_COMPANY_INFO_CHUNK: u32 = 60_000;

pub(crate) fn resource_symbol(market: u16, code: &str) -> io::Result<String> {
    let code = code.trim();
    if market > 2 || code.len() != 6 || !code.bytes().all(|b| b.is_ascii_digit()) {
        return Err(input("市场须为0/1/2，代码须为六位ASCII数字"));
    }
    Ok(code.into())
}
fn filename_bytes(filename: &str) -> io::Result<Vec<u8>> {
    let filename = filename.trim();
    let (bytes, _, errors) = GBK.encode(filename);
    if errors || bytes.is_empty() || bytes.len() > 80 || bytes.contains(&0) {
        return Err(input("F10文件名须为1..80字节可编码GBK且不含NUL"));
    }
    Ok(bytes.into_owned())
}
fn text(bytes: &[u8], trim_null: bool) -> io::Result<String> {
    let bytes = if trim_null {
        &bytes[..bytes.iter().position(|b| *b == 0).unwrap_or(bytes.len())]
    } else {
        bytes
    };
    GBK.decode_without_bom_handling_and_without_replacement(bytes)
        .map(|s| s.into_owned())
        .ok_or_else(|| invalid("F10响应包含无效GBK"))
}

#[derive(Debug, Clone, Serialize)]
pub struct CompanyInfoDirectoryRequest {
    market: u16,
    code: String,
}
impl CompanyInfoDirectoryRequest {
    pub fn new(market: u16, code: &str) -> io::Result<Self> {
        Ok(Self {
            market,
            code: resource_symbol(market, code)?,
        })
    }
    pub fn market(&self) -> u16 {
        self.market
    }
    pub fn code(&self) -> &str {
        &self.code
    }
    pub fn encode(&self, id: u32) -> io::Result<Vec<u8>> {
        let mut body = [0; 12];
        body[..2].copy_from_slice(&self.market.to_le_bytes());
        body[2..8].copy_from_slice(self.code.as_bytes());
        crate::tcp::frame::request(COMPANY_INFO_DIRECTORY_COMMAND, id, &body)
    }
    pub fn recv(&self, tcp: &mut Tcp) -> io::Result<CompanyInfoDirectory> {
        let bytes = tcp.exchange_checked(&self.encode(crate::tcp::frame::sequence())?)?;
        let mut result = decode_company_info_directory(self, &bytes)?;
        result.source.attach(tcp)?;
        Ok(result)
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct CompanyInfoEntry {
    pub name: String,
    pub filename: String,
    pub start: u32,
    pub length: u32,
    pub name_raw: Vec<u8>,
    pub filename_raw: Vec<u8>,
}
impl CompanyInfoEntry {
    pub fn to_legacy(&self) -> super::CompanyInfoCategoryItem {
        super::CompanyInfoCategoryItem {
            name: self.name.clone(),
            filename: self.filename.clone(),
            start: self.start,
            length: self.length,
        }
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct CompanyInfoDirectory {
    pub request: CompanyInfoDirectoryRequest,
    pub entries: Vec<CompanyInfoEntry>,
    pub trailing_payload: Vec<u8>,
    pub source: CommandSource,
    #[serde(skip)]
    pub raw_payload: Arc<[u8]>,
}
pub fn decode_company_info_directory(
    request: &CompanyInfoDirectoryRequest,
    bytes: &[u8],
) -> io::Result<CompanyInfoDirectory> {
    bounded(bytes)?;
    let mut reader = Reader::new(bytes);
    let n = count(&mut reader, 152, (u16::MAX as usize - 2) / 152)?;
    let mut entries = Vec::with_capacity(n);
    for _ in 0..n {
        let name_raw = reader.take(64)?.to_vec();
        let filename_raw = reader.take(80)?.to_vec();
        let start = reader.u32()?;
        let length = reader.u32()?;
        entries.push(CompanyInfoEntry {
            name: text(&name_raw, true)?,
            filename: text(&filename_raw, true)?,
            start,
            length,
            name_raw,
            filename_raw,
        });
    }
    Ok(CompanyInfoDirectory {
        request: request.clone(),
        entries,
        trailing_payload: reader.take(reader.remaining())?.to_vec(),
        source: CommandSource::offline(COMPANY_INFO_DIRECTORY_COMMAND),
        raw_payload: bytes.into(),
    })
}

#[derive(Debug, Clone, Serialize)]
pub struct CompanyInfoTextRequest {
    market: u16,
    code: String,
    filename_raw: Vec<u8>,
    offset: u32,
    length: u32,
}
impl CompanyInfoTextRequest {
    /// Single wire request; use company_info_text for ranges larger than a frame.
    pub fn new(
        market: u16,
        code: &str,
        filename: &str,
        offset: u32,
        length: u32,
    ) -> io::Result<Self> {
        let code = resource_symbol(market, code)?;
        let filename_raw = filename_bytes(filename)?;
        if length == 0 || offset.checked_add(length).is_none() {
            return Err(input("F10读取长度须为正且偏移加长度不得溢出u32"));
        }
        Ok(Self {
            market,
            code,
            filename_raw,
            offset,
            length,
        })
    }
    pub fn market(&self) -> u16 {
        self.market
    }
    pub fn code(&self) -> &str {
        &self.code
    }
    pub fn filename_raw(&self) -> &[u8] {
        &self.filename_raw
    }
    pub fn offset(&self) -> u32 {
        self.offset
    }
    pub fn length(&self) -> u32 {
        self.length
    }
    pub fn encode(&self, id: u32) -> io::Result<Vec<u8>> {
        let mut body = [0; 102];
        body[..2].copy_from_slice(&self.market.to_le_bytes());
        body[2..8].copy_from_slice(self.code.as_bytes());
        body[10..10 + self.filename_raw.len()].copy_from_slice(&self.filename_raw);
        body[90..94].copy_from_slice(&self.offset.to_le_bytes());
        body[94..98].copy_from_slice(&self.length.to_le_bytes());
        crate::tcp::frame::request(COMPANY_INFO_TEXT_COMMAND, id, &body)
    }
    pub fn recv(&self, tcp: &mut Tcp) -> io::Result<CompanyInfoTextChunk> {
        let bytes = tcp.exchange_checked(&self.encode(crate::tcp::frame::sequence())?)?;
        let mut result = decode_company_info_text(self, &bytes)?;
        result.source.attach(tcp)?;
        Ok(result)
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct CompanyInfoTextChunk {
    pub request: CompanyInfoTextRequest,
    pub header_raw: [u8; 10],
    pub declared_length: u16,
    /// Bytes can end halfway through a GBK character. Decode after assembly.
    pub content_raw: Vec<u8>,
    pub trailing_payload: Vec<u8>,
    pub source: CommandSource,
    #[serde(skip)]
    pub raw_payload: Arc<[u8]>,
}
impl CompanyInfoTextChunk {
    pub fn decode_text(&self) -> io::Result<String> {
        text(&self.content_raw, false)
    }
}
pub fn decode_company_info_text(
    request: &CompanyInfoTextRequest,
    bytes: &[u8],
) -> io::Result<CompanyInfoTextChunk> {
    bounded(bytes)?;
    let mut reader = Reader::new(bytes);
    let header_raw = reader.take(10)?.try_into().unwrap();
    let declared_length = reader.u16()?;
    if u32::from(declared_length) > request.length {
        return Err(invalid("02d0内容长度超过请求"));
    }
    let content_raw = reader.take(usize::from(declared_length))?.to_vec();
    Ok(CompanyInfoTextChunk {
        request: request.clone(),
        header_raw,
        declared_length,
        content_raw,
        trailing_payload: reader.take(reader.remaining())?.to_vec(),
        source: CommandSource::offline(COMPANY_INFO_TEXT_COMMAND),
        raw_payload: bytes.into(),
    })
}

#[derive(Debug, Clone, Copy)]
pub struct CompanyInfoReadOptions {
    pub chunk_size: u32,
    pub max_bytes: u32,
    pub max_chunks: usize,
    pub timeout: Duration,
}
impl Default for CompanyInfoReadOptions {
    fn default() -> Self {
        Self {
            chunk_size: 30_000,
            max_bytes: 64 * 1024 * 1024,
            max_chunks: 4096,
            timeout: crate::tcp::network_policy().pagination_timeout,
        }
    }
}
impl CompanyInfoReadOptions {
    pub(crate) fn validate(self, length: u32) -> io::Result<()> {
        if self.chunk_size == 0
            || self.chunk_size > MAX_COMPANY_INFO_CHUNK
            || self.max_bytes == 0
            || self.max_bytes > 64 * 1024 * 1024
            || length > self.max_bytes
            || self.max_chunks == 0
            || self.max_chunks > 65_536
            || self.timeout.is_zero()
        {
            return Err(input("F10读取超过预算或分块参数无效"));
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct CompanyInfoText {
    pub request: CompanyInfoTextRequest,
    pub text: String,
    pub content_raw: Vec<u8>,
    pub chunks: Vec<CompanyInfoTextChunk>,
    /// True only when the exact requested byte range has been read.
    pub complete: bool,
}
pub fn company_info_text(
    tcp: &mut Tcp,
    request: CompanyInfoTextRequest,
    options: CompanyInfoReadOptions,
) -> io::Result<CompanyInfoText> {
    options.validate(request.length)?;
    tcp.with_timeout(options.timeout, |tcp| {
        let mut content_raw = Vec::with_capacity(request.length as usize);
        let mut chunks = Vec::new();
        let mut baseline = None;
        while content_raw.len() < request.length as usize {
            if chunks.len() == options.max_chunks {
                return Err(io::Error::other("F10分块预算耗尽，内容不完整"));
            }
            let mut part = request.clone();
            part.offset += content_raw.len() as u32;
            part.length = options
                .chunk_size
                .min(request.length - content_raw.len() as u32);
            let chunk = part.recv(tcp)?;
            check_scan_source(&mut baseline, &chunk.source)?;
            if chunk.content_raw.is_empty() {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "F10在目录指定长度之前返回空块",
                ));
            }
            content_raw.extend_from_slice(&chunk.content_raw);
            chunks.push(chunk);
        }
        Ok(CompanyInfoText {
            request,
            text: text(&content_raw, false)?,
            content_raw,
            chunks,
            complete: true,
        })
    })
}
