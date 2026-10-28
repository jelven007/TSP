//! Shared request/response identity checks for managed 7709 exchanges.
use std::{
    io,
    sync::atomic::{AtomicU32, Ordering},
};

static SEQUENCE: AtomicU32 = AtomicU32::new(1);

pub(crate) fn sequence() -> u32 {
    SEQUENCE.fetch_add(1, Ordering::Relaxed)
}

pub(crate) fn request(opcode: u16, id: u32, body: &[u8]) -> io::Result<Vec<u8>> {
    let length = u16::try_from(body.len().saturating_add(2))
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "请求体过长"))?;
    let mut bytes = Vec::with_capacity(12 + body.len());
    bytes.push(0x0c);
    bytes.extend_from_slice(&id.to_le_bytes());
    bytes.push(1);
    bytes.extend_from_slice(&length.to_le_bytes());
    bytes.extend_from_slice(&length.to_le_bytes());
    bytes.extend_from_slice(&opcode.to_le_bytes());
    bytes.extend_from_slice(body);
    Ok(bytes)
}

pub(crate) fn identity(header: &[u8; 16]) -> io::Result<(u16, u32)> {
    if header[..4] != [0xb1, 0xcb, 0x74, 0] {
        return Err(identity_error("7709响应前缀错误"));
    }
    Ok((
        u16::from_le_bytes([header[10], header[11]]),
        u32::from_le_bytes([header[5], header[6], header[7], header[8]]),
    ))
}

pub(crate) fn matches(header: &[u8; 16], send: &[u8]) -> io::Result<()> {
    let (opcode, id) = identity(header)?;
    if send.len() < 12
        || opcode != u16::from_le_bytes([send[10], send[11]])
        || id != u32::from_le_bytes([send[1], send[2], send[3], send[4]])
    {
        return Err(identity_error("7709响应命令/消息号错配"));
    }
    Ok(())
}

#[derive(Debug)]
struct ResponseIdentityError(&'static str);

impl std::fmt::Display for ResponseIdentityError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.0)
    }
}

impl std::error::Error for ResponseIdentityError {}

fn identity_error(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, ResponseIdentityError(message))
}

pub(crate) fn is_identity_error(error: &io::Error) -> bool {
    error
        .get_ref()
        .is_some_and(|cause| cause.is::<ResponseIdentityError>())
}
