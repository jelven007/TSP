//! Checked 02c5 metadata, including the hash and uninterpreted response bytes.
use super::command_request::{CommandSource, bounded, input, invalid};
use crate::tcp::{Tcp, reader::Reader};
use md5::{Digest, Md5};
use serde::Serialize;
use std::{io, sync::Arc};

pub const BLOCK_META_COMMAND: u16 = 0x02c5;

#[derive(Debug, Clone, Serialize)]
pub struct BlockMetaRequest {
    filename: String,
}
impl BlockMetaRequest {
    pub fn new(filename: &str) -> io::Result<Self> {
        let filename = filename.trim();
        if filename.is_empty()
            || filename.len() > 40
            || !filename.is_ascii()
            || filename.as_bytes().contains(&0)
        {
            return Err(input("板块文件名须为1..40字节ASCII且不含NUL"));
        }
        Ok(Self {
            filename: filename.into(),
        })
    }
    pub fn filename(&self) -> &str {
        &self.filename
    }
    pub fn encode(&self, id: u32) -> io::Result<Vec<u8>> {
        let mut body = [0; 40];
        body[..self.filename.len()].copy_from_slice(self.filename.as_bytes());
        crate::tcp::frame::request(BLOCK_META_COMMAND, id, &body)
    }
    pub fn recv(&self, tcp: &mut Tcp) -> io::Result<BlockMetadata> {
        let bytes = tcp.exchange_checked(&self.encode(crate::tcp::frame::sequence())?)?;
        let mut result = decode_block_metadata(&bytes)?;
        result.source.attach(tcp)?;
        Ok(result)
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct BlockMetadata {
    pub size: u32,
    pub md5: String,
    pub reserved_raw: u8,
    pub md5_raw: [u8; 32],
    pub terminator_raw: u8,
    pub trailing_payload: Vec<u8>,
    pub source: CommandSource,
    #[serde(skip)]
    pub raw_payload: Arc<[u8]>,
}

impl BlockMetadata {
    pub(crate) fn validate_md5(&self) -> io::Result<()> {
        if !self.md5_raw.iter().all(u8::is_ascii_hexdigit) {
            return Err(invalid("02c5 MD5必须为32位十六进制ASCII"));
        }
        Ok(())
    }

    /// 验算完整文件的字节数及服务器声明的MD5，返回小写计算值。
    ///
    /// 空文件也必须声明正确MD5。仅验证下载一致性，不证明服务器文件的权威性。
    /// 原始MD5字段及未知元数据字节保持不变。
    pub fn verify_content(&self, content: &[u8]) -> io::Result<String> {
        self.validate_md5()?;
        if content.len() != self.size as usize {
            return Err(invalid("板块文件总长度与02c5声明不一致"));
        }
        let computed = format!("{:x}", Md5::digest(content));
        if !computed.as_bytes().eq_ignore_ascii_case(&self.md5_raw) {
            return Err(invalid("板块文件MD5与02c5声明不一致"));
        }
        Ok(computed)
    }
}

pub fn decode_block_metadata(bytes: &[u8]) -> io::Result<BlockMetadata> {
    bounded(bytes)?;
    let mut reader = Reader::new(bytes);
    let size = reader.u32()?;
    let reserved_raw = reader.u8()?;
    let md5_raw: [u8; 32] = reader.take(32)?.try_into().unwrap();
    if !md5_raw.is_ascii() {
        return Err(invalid("02c5 MD5字段不是ASCII"));
    }
    let end = md5_raw.iter().position(|b| *b == 0).unwrap_or(32);
    let md5 = std::str::from_utf8(&md5_raw[..end])
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?
        .trim()
        .to_owned();
    let terminator_raw = reader.u8()?;
    Ok(BlockMetadata {
        size,
        md5,
        reserved_raw,
        md5_raw,
        terminator_raw,
        trailing_payload: reader.take(reader.remaining())?.to_vec(),
        source: CommandSource::offline(BLOCK_META_COMMAND),
        raw_payload: bytes.into(),
    })
}
