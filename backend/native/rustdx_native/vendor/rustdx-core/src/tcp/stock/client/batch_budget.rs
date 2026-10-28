//! Accounting for owned result storage, independent of serialized JSON size.
use crate::tcp::stock::*;
use std::{io, mem::size_of, sync::Arc};

/// Limits all Vec-returning batch methods on one Client. Exhaustion rejects the
/// whole collection; use streaming methods when partial delivery is required.
///
/// Bytes include the output Vec's capacity, nested Vec/String capacities and
/// raw Arc payloads (charged per reference, conservatively). They exclude the
/// allocator, thread stacks, metadata, sockets and temporary decoder storage.
/// This is a retained-result budget, not a process RSS limit. Each in-flight
/// query remains subject to its own protocol/page limits.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BatchResultBudget {
    pub max_rows: usize,
    pub max_retained_bytes: usize,
    pub max_item_rows: usize,
    pub max_item_retained_bytes: usize,
}

impl Default for BatchResultBudget {
    fn default() -> Self {
        Self {
            max_rows: 1_000_000,
            max_retained_bytes: 256 * 1024 * 1024,
            max_item_rows: 65_536,
            max_item_retained_bytes: 32 * 1024 * 1024,
        }
    }
}

impl BatchResultBudget {
    pub(super) fn validate(self) -> io::Result<()> {
        if self.max_rows == 0
            || self.max_retained_bytes == 0
            || self.max_item_rows == 0
            || self.max_item_retained_bytes == 0
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "聚合批量预算须大于0",
            ));
        }
        Ok(())
    }
}

/// Heap only; inline fields are charged by the enclosing Vec or value.
pub(crate) trait Retained {
    fn heap_bytes(&self) -> usize;
    fn rows(&self) -> usize {
        1
    }
}

impl Retained for String {
    fn heap_bytes(&self) -> usize {
        self.capacity()
    }
}
impl<T: Retained> Retained for Vec<T> {
    fn heap_bytes(&self) -> usize {
        self.iter().fold(
            self.capacity().saturating_mul(size_of::<T>()),
            |bytes, value| bytes.saturating_add(value.heap_bytes()),
        )
    }
    fn rows(&self) -> usize {
        self.len()
    }
}
impl<T: Retained> Retained for Option<T> {
    fn heap_bytes(&self) -> usize {
        self.as_ref().map_or(0, Retained::heap_bytes)
    }
}
impl Retained for Arc<[u8]> {
    fn heap_bytes(&self) -> usize {
        self.len().saturating_add(3 * size_of::<usize>())
    }
}

macro_rules! impl_heap {
    ($ty:ty; $($field:ident),* $(,)?) => {
        impl $crate::tcp::stock::client::batch_budget::Retained for $ty {
            fn heap_bytes(&self) -> usize {
                let bytes = 0usize;
                $(let bytes = bytes.saturating_add(
                    $crate::tcp::stock::client::batch_budget::Retained::heap_bytes(&self.$field)
                );)*
                bytes
            }
        }
    };
}
pub(crate) use impl_heap;

impl_heap!(u8;);
impl_heap!(IntradayAuxPoint;);
impl_heap!(SparklinePrice;);
impl_heap!(MoneyFlowDaily;);
impl_heap!(QuoteLevel;);
impl_heap!(BarRangePage;);
impl_heap!(KlineData<'_>;);
impl_heap!(DelistedBar; record_raw);
impl_heap!(AuctionPoint; time_label);
impl_heap!(MinutePoint; time_label);
impl_heap!(BasicQuote; code, bids, asks, tail_raw);
impl_heap!(CompanyInfoEntry; name, filename, name_raw, filename_raw);
impl_heap!(CompanyInfoTextChunk; request, content_raw, trailing_payload, raw_payload);

impl<T: Retained> Retained for CommandPage<T> {
    fn heap_bytes(&self) -> usize {
        self.records
            .heap_bytes()
            .saturating_add(self.raw_payload.heap_bytes())
    }
    fn rows(&self) -> usize {
        self.records.len()
    }
}

macro_rules! page_result {
    ($ty:ty; $($extra:ident),* $(,)?) => {
        impl Retained for $ty {
            fn heap_bytes(&self) -> usize {
                let bytes = self.page.heap_bytes().saturating_add(self.request.heap_bytes());
                $(let bytes = bytes.saturating_add(self.$extra.heap_bytes());)*
                bytes
            }
            fn rows(&self) -> usize { self.page.records.len() }
        }
    };
}
page_result!(IntradayAuxSeries;);
page_result!(SparklineSeries; code);
page_result!(MoneyFlowBlock; code, header_raw);
page_result!(AuctionSeries;);
page_result!(SnapshotPage; missing_codes);
impl_heap!(BlockMetadata; md5, trailing_payload, raw_payload);
impl_heap!(CompanyInfoText; request, text, content_raw, chunks);

impl Retained for CompanyInfoDirectory {
    fn heap_bytes(&self) -> usize {
        self.request
            .heap_bytes()
            .saturating_add(self.entries.heap_bytes())
            .saturating_add(self.trailing_payload.heap_bytes())
            .saturating_add(self.raw_payload.heap_bytes())
    }
    fn rows(&self) -> usize {
        self.entries.len()
    }
}
impl Retained for DelistedBarsPage {
    fn heap_bytes(&self) -> usize {
        self.request
            .heap_bytes()
            .saturating_add(self.records.heap_bytes())
            .saturating_add(self.raw_payload.heap_bytes())
    }
    fn rows(&self) -> usize {
        self.records.len()
    }
}
impl Retained for DelistedBarsCollection {
    fn heap_bytes(&self) -> usize {
        self.records
            .heap_bytes()
            .saturating_add(self.pages.heap_bytes())
    }
    fn rows(&self) -> usize {
        // Both copies consume row storage.
        self.pages
            .iter()
            .fold(self.records.len(), |n, page| n.saturating_add(page.rows()))
    }
}
impl<T: Retained> Retained for BarRangeCollection<T> {
    fn heap_bytes(&self) -> usize {
        self.code
            .heap_bytes()
            .saturating_add(self.records.heap_bytes())
            .saturating_add(self.pages.heap_bytes())
    }
    fn rows(&self) -> usize {
        self.records.len()
    }
}

pub(super) struct Collector<T> {
    values: Vec<io::Result<T>>,
    budget: BatchResultBudget,
    bytes: usize,
    rows: usize,
}

impl<T: Retained> Collector<T> {
    pub(super) fn new(len: usize, budget: BatchResultBudget) -> io::Result<Self> {
        budget.validate()?;
        let bytes = len
            .checked_mul(size_of::<io::Result<T>>())
            .filter(|n| *n <= budget.max_retained_bytes)
            .ok_or_else(exhausted)?;
        let mut values = Vec::new();
        values.try_reserve_exact(len).map_err(io::Error::other)?;
        let actual = values.capacity().saturating_mul(size_of::<io::Result<T>>());
        if actual > budget.max_retained_bytes {
            return Err(exhausted());
        }
        Ok(Self {
            values,
            budget,
            bytes: bytes.max(actual),
            rows: 0,
        })
    }

    pub(super) fn push(&mut self, result: io::Result<T>) -> io::Result<()> {
        let (result, rows, bytes) = match result {
            Ok(value) => {
                let rows = value.rows();
                let bytes = value.heap_bytes();
                if rows > self.budget.max_item_rows
                    || bytes.saturating_add(size_of::<T>()) > self.budget.max_item_retained_bytes
                {
                    return Err(exhausted());
                }
                (Ok(value), rows, bytes)
            }
            Err(error) => {
                // Preserve kind and bounded diagnostic text, rather than retain
                // an opaque error with arbitrarily large owned context.
                let mut message = BoundedMessage(String::new());
                let _ = std::fmt::write(&mut message, format_args!("{error}"));
                let bytes = message.0.capacity().saturating_add(128);
                (Err(io::Error::new(error.kind(), message.0)), 0, bytes)
            }
        };
        let next_rows = self
            .rows
            .checked_add(rows)
            .filter(|n| *n <= self.budget.max_rows);
        let next_bytes = self
            .bytes
            .checked_add(bytes)
            .filter(|n| *n <= self.budget.max_retained_bytes);
        match (next_rows, next_bytes) {
            (Some(rows), Some(bytes)) => {
                self.values.push(result);
                self.rows = rows;
                self.bytes = bytes;
                Ok(())
            }
            _ => Err(exhausted()),
        }
    }

    pub(super) fn finish(self) -> Vec<io::Result<T>> {
        self.values
    }
}

fn exhausted() -> io::Error {
    io::Error::other("聚合批量结果预算耗尽；整批未完成，请缩小范围或使用流式入口")
}

struct BoundedMessage(String);
impl std::fmt::Write for BoundedMessage {
    fn write_str(&mut self, text: &str) -> std::fmt::Result {
        let mut end = text.len().min(2048usize.saturating_sub(self.0.len()));
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        self.0.push_str(&text[..end]);
        if end < text.len() {
            Err(std::fmt::Error)
        } else {
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capacity_raw_payload_and_error_storage_are_charged() {
        let mut data = Vec::<u8>::with_capacity(8192);
        data.push(1);
        let page = CommandPage::new(0x051b, data, &[0; 4096]);
        assert!(page.heap_bytes() >= 8192 + 4096);
        let mut collector = Collector::<CommandPage<u8>>::new(
            1,
            BatchResultBudget {
                max_item_retained_bytes: 10000,
                ..Default::default()
            },
        )
        .unwrap();
        assert!(collector.push(Ok(page)).is_err());
        let mut collector = Collector::<Vec<u8>>::new(
            2,
            BatchResultBudget {
                max_retained_bytes: 200,
                ..Default::default()
            },
        )
        .unwrap();
        assert!(
            collector
                .push(Err(io::Error::other("x".repeat(3000))))
                .is_err()
        );
    }

    #[test]
    fn slots_are_budgeted_before_allocation_and_rows_stop_collection() {
        assert!(
            Collector::<Vec<u8>>::new(
                100_000,
                BatchResultBudget {
                    max_retained_bytes: 1024,
                    ..Default::default()
                }
            )
            .is_err()
        );
        let mut collector = Collector::<Vec<u8>>::new(
            3,
            BatchResultBudget {
                max_rows: 3,
                ..Default::default()
            },
        )
        .unwrap();
        collector.push(Ok(vec![1, 2])).unwrap();
        collector
            .push(Err(io::Error::new(io::ErrorKind::NotFound, "missing")))
            .unwrap();
        assert!(collector.push(Ok(vec![3, 4])).is_err());
    }
}
