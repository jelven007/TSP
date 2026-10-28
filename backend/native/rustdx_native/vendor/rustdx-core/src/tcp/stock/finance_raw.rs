//! Lossless single-security 0x0010 decoding. No business-unit conversion here.
use super::FinanceRequest;
use crate::tcp::reader::Reader;
use chrono::NaiveDate;
use serde::{Serialize, Serializer};
use std::io::{self, ErrorKind};

pub const FINANCE_BODY_SIZE: usize = 136;

#[derive(Debug, Clone, Serialize)]
pub struct FinanceRaw {
    market: u16,
    code: String,
    #[serde(serialize_with = "serialize_body")]
    body: [u8; FINANCE_BODY_SIZE],
}

fn serialize_body<S: Serializer>(body: &[u8; FINANCE_BODY_SIZE], s: S) -> Result<S::Ok, S::Error> {
    body.as_slice().serialize(s)
}

impl FinanceRaw {
    pub fn market(&self) -> u16 {
        self.market
    }
    pub fn code(&self) -> &str {
        &self.code
    }
    pub fn body(&self) -> &[u8; FINANCE_BODY_SIZE] {
        &self.body
    }
    /// Only float slots (0x00 and aligned 0x10..0x84) can be read as f32.
    pub fn raw_bits(&self, offset: usize) -> io::Result<u32> {
        if offset != 0 && !((16..=132).contains(&offset) && offset.is_multiple_of(4)) {
            return Err(io::Error::new(
                ErrorKind::InvalidInput,
                "不是财务float32槽位",
            ));
        }
        Ok(self.u32(offset))
    }
    pub fn raw_f32(&self, offset: usize) -> io::Result<f32> {
        self.raw_bits(offset).map(f32::from_bits)
    }
    pub fn province(&self) -> u16 {
        u16::from_le_bytes([self.body[4], self.body[5]])
    }
    pub fn industry(&self) -> u16 {
        u16::from_le_bytes([self.body[6], self.body[7]])
    }
    pub fn updated_date_raw(&self) -> u32 {
        self.u32(8)
    }
    pub fn ipo_date_raw(&self) -> u32 {
        self.u32(12)
    }
    pub fn updated_date(&self) -> Option<NaiveDate> {
        date(self.updated_date_raw()).ok().flatten()
    }
    pub fn ipo_date(&self) -> Option<NaiveDate> {
        date(self.ipo_date_raw()).ok().flatten()
    }
    pub(super) fn u32(&self, offset: usize) -> u32 {
        u32::from_le_bytes([
            self.body[offset],
            self.body[offset + 1],
            self.body[offset + 2],
            self.body[offset + 3],
        ])
    }
}

fn date(value: u32) -> io::Result<Option<NaiveDate>> {
    if value == 0 {
        return Ok(None);
    }
    if !(1..=9999).contains(&(value / 10000)) {
        return Err(io::Error::new(ErrorKind::InvalidData, "财务日期非法"));
    }
    crate::data::date_from_u32(value)
        .map(Some)
        .map_err(|e| io::Error::new(ErrorKind::InvalidData, e))
}

pub fn decode_finance_raw(
    request: &FinanceRequest<'_>,
    bytes: &[u8],
) -> io::Result<Vec<FinanceRaw>> {
    let mut reader = Reader::new(bytes);
    let count = reader.u16()?;
    if count > 1 {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "单只财务数量须为0或1",
        ));
    }
    let mut result = Vec::with_capacity(usize::from(count));
    if count == 1 {
        crate::operation::check()?;
        let market = u16::from(reader.u8()?);
        let code = reader.code()?;
        if market != request.market() || code != request.code() {
            return Err(io::Error::new(
                ErrorKind::InvalidData,
                "财务证券与请求不一致",
            ));
        }
        let mut body = [0; FINANCE_BODY_SIZE];
        body.copy_from_slice(reader.take(FINANCE_BODY_SIZE)?);
        let raw = FinanceRaw { market, code, body };
        for offset in std::iter::once(0).chain((16..136).step_by(4)) {
            if !raw.raw_f32(offset)?.is_finite() {
                return Err(io::Error::new(
                    ErrorKind::InvalidData,
                    "财务字段包含非有限值",
                ));
            }
        }
        date(raw.updated_date_raw())?;
        date(raw.ipo_date_raw())?;
        result.push(raw);
    }
    if reader.remaining() != 0 {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "财务响应包含多余字节",
        ));
    }
    Ok(result)
}
