//! Explicit, checked price scale for the 0x053e wire layout only.
use serde::Serialize;
use std::io;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct PriceScale {
    decimal: u8,
    divisor: u32,
}

impl PriceScale {
    pub fn from_metadata(decimal: u8) -> io::Result<Self> {
        let divisor = match decimal {
            2 => 100,
            3 => 1_000,
            4 => 10_000,
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::Unsupported,
                    format!("UnsupportedPrecision: 053e尚未验证decimal={decimal}"),
                ));
            }
        };
        Ok(Self { decimal, divisor })
    }
    pub fn decimal(self) -> u8 {
        self.decimal
    }
    pub fn divisor(self) -> u32 {
        self.divisor
    }
    pub(super) fn price(self, base: i32, delta: i32) -> f64 {
        (i64::from(base) + i64::from(delta)) as f64 / f64::from(self.divisor)
    }
}
