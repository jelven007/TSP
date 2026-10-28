//! Checked general 06b9 reads. This profile is separate from legacy board reads.
use super::command_request::{CommandSource, bounded, check_scan_source, input, invalid};
use crate::tcp::{Tcp, reader::Reader};
use serde::Serialize;
use std::{io, sync::Arc, time::Duration};

pub const FILE_CONTENT_COMMAND: u16 = 0x06b9;
pub const MAX_FILE_PATH_BYTES: usize = 300;
pub const MAX_FILE_CHUNK_SIZE: u32 = 60_000;
pub const DEFAULT_FILE_CHUNK_SIZE: u32 = 30_000;

#[derive(Debug, Clone, Serialize)]
pub struct FileContentRequest {
    path: String,
    offset: u32,
    size: u32,
}
impl FileContentRequest {
    pub fn new(path: &str, offset: u32, size: u32) -> io::Result<Self> {
        let path = path.trim();
        if path.is_empty()
            || path.len() > MAX_FILE_PATH_BYTES
            || !path.is_ascii()
            || path.as_bytes().contains(&0)
        {
            return Err(input("资源路径须为1..300字节ASCII且不含NUL"));
        }
        if size == 0 || size > MAX_FILE_CHUNK_SIZE || offset.checked_add(size).is_none() {
            return Err(input("资源块须为1..60000字节且范围不得溢出u32"));
        }
        Ok(Self {
            path: path.replace('\\', "/"),
            offset,
            size,
        })
    }
    pub fn path(&self) -> &str {
        &self.path
    }
    pub fn offset(&self) -> u32 {
        self.offset
    }
    pub fn size(&self) -> u32 {
        self.size
    }
    pub fn encode(&self, id: u32) -> io::Result<Vec<u8>> {
        let mut body = [0; 8 + MAX_FILE_PATH_BYTES];
        body[..4].copy_from_slice(&self.offset.to_le_bytes());
        body[4..8].copy_from_slice(&self.size.to_le_bytes());
        body[8..8 + self.path.len()].copy_from_slice(self.path.as_bytes());
        crate::tcp::frame::request(FILE_CONTENT_COMMAND, id, &body)
    }
    pub fn recv(&self, tcp: &mut Tcp) -> io::Result<FileContentChunk> {
        let bytes = tcp.exchange_checked(&self.encode(crate::tcp::frame::sequence())?)?;
        let mut result = decode_file_content(self, &bytes)?;
        result.source.attach(tcp)?;
        Ok(result)
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct FileContentChunk {
    pub request: FileContentRequest,
    pub chunk_len: u32,
    pub header_raw: [u8; 4],
    #[serde(skip)]
    pub content: Vec<u8>,
    /// Bytes beyond the declared chunk length, never appended to the file.
    pub trailing_payload: Vec<u8>,
    pub source: CommandSource,
    #[serde(skip)]
    pub raw_payload: Arc<[u8]>,
}
impl FileContentChunk {
    /// A short chunk marks only the observed end of this server resource.
    pub fn is_last(&self) -> bool {
        self.chunk_len < self.request.size
    }
}

pub fn decode_file_content(
    request: &FileContentRequest,
    bytes: &[u8],
) -> io::Result<FileContentChunk> {
    bounded(bytes)?;
    let mut reader = Reader::new(bytes);
    let chunk_len = reader.u32()?;
    if chunk_len > request.size {
        return Err(invalid("06b9声明块长超过请求"));
    }
    let content = reader.take(chunk_len as usize)?.to_vec();
    Ok(FileContentChunk {
        request: request.clone(),
        chunk_len,
        header_raw: chunk_len.to_le_bytes(),
        content,
        trailing_payload: reader.take(reader.remaining())?.to_vec(),
        source: CommandSource::offline(FILE_CONTENT_COMMAND),
        raw_payload: bytes.into(),
    })
}

#[derive(Debug, Clone, Copy)]
pub struct ResourceReadOptions {
    pub chunk_size: u32,
    pub max_bytes: u32,
    pub max_chunks: usize,
    /// Sum of decoded response sizes, including prefixes and unknown tails.
    pub max_response_bytes: usize,
    pub timeout: Duration,
    pub page_delay: Duration,
}
impl Default for ResourceReadOptions {
    fn default() -> Self {
        Self {
            chunk_size: DEFAULT_FILE_CHUNK_SIZE,
            max_bytes: 64 * 1024 * 1024,
            max_chunks: 4096,
            max_response_bytes: 128 * 1024 * 1024,
            timeout: crate::tcp::network_policy().pagination_timeout,
            page_delay: Duration::from_millis(20),
        }
    }
}
impl ResourceReadOptions {
    pub(crate) fn validate(self) -> io::Result<()> {
        if self.chunk_size == 0
            || self.chunk_size > MAX_FILE_CHUNK_SIZE
            || self.max_bytes == 0
            || self.max_bytes > 64 * 1024 * 1024
            || self.max_chunks == 0
            || self.max_chunks > 4096
            || self.max_response_bytes < 4
            || self.max_response_bytes > 128 * 1024 * 1024
            || self.timeout.is_zero()
            || std::time::Instant::now()
                .checked_add(self.timeout)
                .is_none()
            || self.page_delay > self.timeout
        {
            return Err(input("资源预算无效：最高64MiB文件/4096块/128MiB响应"));
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum ResourceTermination {
    EmptyChunk,
    ShortChunk,
    ByteBudget,
    ChunkBudget,
    ResponseBudget,
}
#[derive(Debug, Clone, Serialize)]
pub struct ResourceFileChunk {
    pub offset: u32,
    pub chunk_len: u32,
    pub requested_size: u32,
    pub header_raw: [u8; 4],
    pub trailing_payload: Vec<u8>,
    pub source: CommandSource,
    #[serde(skip)]
    pub raw_payload: Arc<[u8]>,
}
#[derive(Debug, Clone, Serialize)]
pub struct ResourceFile {
    pub path: String,
    #[serde(skip)]
    pub content: Vec<u8>,
    pub chunks: Vec<ResourceFileChunk>,
    pub response_bytes: usize,
    pub termination: ResourceTermination,
    /// Only ShortChunk/EmptyChunk qualify. This is not a vendor hash verification.
    pub complete: bool,
}
impl ResourceFile {
    pub fn require_complete(&self) -> io::Result<&[u8]> {
        if !self.complete
            || !matches!(
                self.termination,
                ResourceTermination::EmptyChunk | ResourceTermination::ShortChunk
            )
        {
            return Err(invalid("资源下载预算结束，尚未观察到文件末尾"));
        }
        Ok(&self.content)
    }
}

/// Download from offset zero on one connection and one UTC+8 observation day.
/// Budget exhaustion returns a marked partial file; transport/schema errors return Err.
/// A file exactly filling max_bytes needs a larger budget to prove its end.
pub fn download_resource_file(
    tcp: &mut Tcp,
    path: &str,
    options: ResourceReadOptions,
) -> io::Result<ResourceFile> {
    options.validate()?;
    let path = FileContentRequest::new(path, 0, options.chunk_size)?.path;
    tcp.with_timeout(options.timeout, |tcp| {
        let mut file = ResourceFile {
            path,
            content: Vec::new(),
            chunks: Vec::new(),
            response_bytes: 0,
            termination: ResourceTermination::ChunkBudget,
            complete: false,
        };
        let mut baseline = None;
        loop {
            crate::operation::check()?;
            let offset = file.content.len() as u32;
            if offset == options.max_bytes {
                file.termination = ResourceTermination::ByteBudget;
                break;
            }
            if file.chunks.len() == options.max_chunks {
                break;
            }
            let size = options.chunk_size.min(options.max_bytes - offset);
            // Reserve the largest possible single decoded frame before reading.
            if usize::from(u16::MAX)
                > options
                    .max_response_bytes
                    .saturating_sub(file.response_bytes)
            {
                file.termination = ResourceTermination::ResponseBudget;
                break;
            }
            let chunk = FileContentRequest::new(&file.path, offset, size)?.recv(tcp)?;
            check_scan_source(&mut baseline, &chunk.source)?;
            file.response_bytes += chunk.raw_payload.len();
            file.content.extend_from_slice(&chunk.content);
            let last = chunk.is_last();
            file.chunks.push(ResourceFileChunk {
                offset,
                chunk_len: chunk.chunk_len,
                requested_size: size,
                header_raw: chunk.header_raw,
                trailing_payload: chunk.trailing_payload,
                source: chunk.source,
                raw_payload: chunk.raw_payload,
            });
            if last {
                file.termination = if chunk.chunk_len == 0 {
                    ResourceTermination::EmptyChunk
                } else {
                    ResourceTermination::ShortChunk
                };
                file.complete = true;
                break;
            }
            if !options.page_delay.is_zero() {
                crate::operation::sleep(options.page_delay)?;
            }
        }
        Ok(file)
    })
}
