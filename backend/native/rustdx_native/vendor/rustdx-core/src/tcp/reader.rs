//! 协议解码使用的有界字节读取器。
use std::io::{self, ErrorKind};

pub(crate) struct Reader<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    pub fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, pos: 0 }
    }
    pub fn remaining(&self) -> usize {
        self.bytes.len() - self.pos
    }
    pub fn take(&mut self, n: usize) -> io::Result<&'a [u8]> {
        let end = self
            .pos
            .checked_add(n)
            .filter(|end| *end <= self.bytes.len())
            .ok_or_else(|| {
                io::Error::new(
                    ErrorKind::UnexpectedEof,
                    format!("响应在偏移{}截断，需要{n}字节", self.pos),
                )
            })?;
        let data = &self.bytes[self.pos..end];
        self.pos = end;
        Ok(data)
    }
    pub fn skip(&mut self, n: usize) -> io::Result<()> {
        self.take(n).map(|_| ())
    }
    pub fn u8(&mut self) -> io::Result<u8> {
        Ok(self.take(1)?[0])
    }
    pub fn u16(&mut self) -> io::Result<u16> {
        Ok(u16::from_le_bytes(self.take(2)?.try_into().unwrap()))
    }
    pub fn u32(&mut self) -> io::Result<u32> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    pub fn f32(&mut self) -> io::Result<f32> {
        let value = f32::from_bits(self.u32()?);
        if !value.is_finite() {
            return Err(io::Error::new(ErrorKind::InvalidData, "响应浮点非有限数"));
        }
        Ok(value)
    }
    pub fn position(&self) -> usize {
        self.pos
    }
    /// TDX sign/magnitude encoding: six value bits in the first byte,
    /// followed by seven per continuation byte. Reject overflow before shifting.
    pub fn varint64(&mut self) -> io::Result<i64> {
        let first = self.u8()?;
        let negative = first & 0x40 != 0;
        let mut value = u64::from(first & 0x3f);
        let mut byte = first;
        let mut shift = 6;
        while byte & 0x80 != 0 {
            if shift > 62 {
                return Err(io::Error::new(ErrorKind::InvalidData, "变长整数过长"));
            }
            byte = self.u8()?;
            let part = u64::from(byte & 0x7f);
            if part > (u64::MAX >> shift) {
                return Err(io::Error::new(ErrorKind::InvalidData, "变长整数溢出"));
            }
            value |= part << shift;
            shift += 7;
        }
        if value > i64::MAX as u64 + u64::from(negative) {
            return Err(io::Error::new(ErrorKind::InvalidData, "变长整数溢出"));
        }
        Ok(if negative {
            (value as i64).wrapping_neg()
        } else {
            value as i64
        })
    }
    pub fn code(&mut self) -> io::Result<String> {
        let bytes = self.take(6)?;
        if !bytes.iter().all(u8::is_ascii_alphanumeric) {
            return Err(io::Error::new(
                ErrorKind::InvalidData,
                "证券代码包含非ASCII字符",
            ));
        }
        Ok(std::str::from_utf8(bytes)
            .map_err(|e| io::Error::new(ErrorKind::InvalidData, e))?
            .to_owned())
    }
    pub fn varint(&mut self) -> io::Result<i32> {
        let first = self.u8()?;
        let negative = first & 0x40 != 0;
        let mut bit = first;
        let mut value = (first & 0x3f) as u64;
        let mut shift = 6;
        for _ in 0..4 {
            if bit & 0x80 == 0 {
                break;
            }
            bit = self.u8()?;
            value |= ((bit & 0x7f) as u64) << shift;
            shift += 7;
        }
        if bit & 0x80 != 0 || value > i32::MAX as u64 + u64::from(negative) {
            return Err(io::Error::new(ErrorKind::InvalidData, "价格变长整数溢出"));
        }
        let signed = if negative {
            -(value as i64)
        } else {
            value as i64
        };
        Ok(signed as i32)
    }
}

pub(crate) fn require_len(bytes: &[u8], expected: usize) -> io::Result<()> {
    if bytes.len() < expected {
        Err(io::Error::new(
            ErrorKind::UnexpectedEof,
            format!("响应长度不足：需要{expected}，实际{}", bytes.len()),
        ))
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_truncated_and_overlong_values() {
        assert!(Reader::new(&[0x80]).varint().is_err());
        assert!(Reader::new(&[0xff; 6]).varint().is_err());
        assert!(Reader::new(&[0xff; 6]).code().is_err());
        assert_eq!(Reader::new(&[228, 6]).varint().unwrap(), -420);
    }
}
