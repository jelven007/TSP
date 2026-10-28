//! Limits and provenance for raw transaction enumeration.

use std::{net::SocketAddr, ops::Range, time::Duration};

use super::TransactionData;

/// Limits for [`super::Client::transaction_all`] and
/// [`super::Client::history_transaction_all`].
///
/// ```rust
/// use rustdx_complete::tcp::stock::TransactionPaginationOptions;
/// let options = TransactionPaginationOptions {
///     page_size: 800,
///     max_pages: 100,
///     ..Default::default()
/// };
/// assert_eq!(options.page_size, 800);
/// ```
#[derive(Debug, Clone)]
pub struct TransactionPaginationOptions {
    /// Requested raw records per page: 1–1800 for 0fc6, 1–2000 for legacy/today.
    pub page_size: u16,
    /// Maximum logical pages, including the final empty page.
    /// An empty recheck uses the same page slot and at most one extra request.
    pub max_pages: usize,
    /// Maximum raw records retained. At the exact limit, one record is requested
    /// to distinguish an empty end from an incomplete collection.
    pub max_records: usize,
    /// Maximum decompressed response bytes, including empty rechecks.
    /// A bounded protocol frame is received before this limit is checked.
    pub max_bytes: usize,
    /// Total timeout, including all pages, pacing and transport retries.
    /// The process pagination timeout and any outer deadline can only shorten it.
    pub timeout: Duration,
    /// Delay after a response before the next request. This is client pacing,
    /// not a claim about the server's rate limit.
    pub page_delay: Duration,
}

impl Default for TransactionPaginationOptions {
    fn default() -> Self {
        Self {
            page_size: 1800,
            max_pages: 100,
            max_records: usize::from(u16::MAX) + 2000,
            max_bytes: 8 * 1024 * 1024,
            timeout: Duration::from_secs(120),
            page_delay: Duration::from_millis(100),
        }
    }
}

/// Metadata in request order: start=0 first, final empty page last.
#[derive(Debug, Clone, serde::Serialize)]
pub struct TransactionPageInfo {
    pub start: u16,
    pub request_count: u16,
    /// Count before any caller-side classification or filtering.
    pub raw_count: usize,
    /// Index range in [`TransactionCollection::records`].
    pub record_range: Range<usize>,
    /// Decompressed bytes, including the first empty response when rechecked.
    pub payload_bytes: usize,
    /// One, or two if an empty page was rechecked on the same connection.
    pub requests: usize,
}

/// Raw events enumerated on one connection until an empty page was observed.
///
/// This establishes the source's enumerable range during this query. It does
/// **not** verify the source's trading date, coverage, or a frozen intraday
/// snapshot. Prefer stable post-session data for daily archives.
#[derive(Debug, Clone, serde::Serialize)]
pub struct TransactionCollection {
    pub market: u16,
    pub code: String,
    /// 0x0fc5 today, 0x0fc6 history, or explicit legacy 0x0fb5.
    pub command: u16,
    /// Explicit history date; None for the date-less current-day command.
    pub requested_date: Option<u32>,
    /// Client-observed UTC+8 calendar date at the start, not a server date.
    pub observed_date: u32,
    pub source_endpoint: SocketAddr,
    /// Local Tcp generation; a reconnect, even to the same address, changes it.
    pub connection_generation: u64,
    pub started_at: chrono::DateTime<chrono::Utc>,
    pub fetched_at: chrono::DateTime<chrono::Utc>,
    /// Earlier pages first, each page's wire order preserved. Minute order is
    /// checked, including page boundaries; equal minutes/records are retained.
    /// Status 5/8 and unknown values are not filtered.
    pub records: Vec<TransactionData>,
    pub pages: Vec<TransactionPageInfo>,
    /// Lossless event pages in request order, including raw payload, count/status/
    /// tail slots, historical reference bytes and source connection identity.
    /// Compatibility `records` is flattened in chronological page order.
    pub event_pages: Vec<super::TradePage>,
    /// Includes optional same-connection empty rechecks, not transport retries.
    pub requests: usize,
    pub payload_bytes: usize,
    pub termination: TransactionPaginationTermination,
}

/// Completion observation, separate from independently verified day coverage.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub enum TransactionPaginationTermination {
    EmptyPageObserved,
}

/// Local reasons a strict enumeration cannot be returned as successful.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub enum TransactionPaginationFailure {
    PageLimit,
    RecordLimit,
    ByteLimit,
    OffsetLimit,
    ConnectionChanged,
    DateChanged,
    /// Identical complete pages at different offsets are ambiguous; no records
    /// are deduplicated, and this is not treated as a normal end.
    RepeatedPage,
    InvalidOrder,
}

/// Available via `io::Error::get_ref().downcast_ref()` for local pagination
/// failures. No partial record vector is returned as a successful collection.
/// Network, decoding, cancellation and deadline errors retain their I/O kind.
#[derive(Debug, thiserror::Error)]
#[error("成交明细分页未完成（{reason:?}，start={start}，已取得{pages}页/{records}条）")]
pub struct TransactionPaginationError {
    pub reason: TransactionPaginationFailure,
    pub start: u32,
    pub pages: usize,
    pub records: usize,
}
