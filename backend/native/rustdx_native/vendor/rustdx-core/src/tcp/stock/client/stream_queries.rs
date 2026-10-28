//! Streaming counterparts of extended command batches.
use super::{BatchStreamOptions, BatchStreamSummary, Client};
use crate::{
    operation::OperationContext,
    tcp::{network_policy, stock::*},
};
use std::io;

fn requests<R>(
    stocks: &[(u16, &str)],
    make: impl Fn(u16, &str) -> io::Result<R>,
) -> io::Result<Vec<R>> {
    super::extended_queries::validate_batch_len(stocks.len())?;
    stocks
        .iter()
        .map(|&(market, code)| make(market, code))
        .collect()
}

impl Client {
    /// Callback indices refer to input requests, including failed items.
    pub fn intraday_aux_batch_stream(
        &self,
        stocks: &[(u16, &str)],
        kind: IntradayAuxKind,
        options: BatchStreamOptions,
        consume: impl FnMut(usize, io::Result<IntradayAuxSeries>) -> io::Result<()>,
    ) -> io::Result<BatchStreamSummary> {
        let requests = requests(stocks, |m, c| IntradayAuxRequest::new(m, c, kind))?;
        self.command_stream(
            &requests,
            options,
            |s| s.page.records.len(),
            |r, tcp| r.recv(tcp),
            consume,
        )
    }

    pub fn sparkline_batch_stream(
        &self,
        stocks: &[(u16, &str)],
        page: SparklineOptions,
        options: BatchStreamOptions,
        consume: impl FnMut(usize, io::Result<SparklineSeries>) -> io::Result<()>,
    ) -> io::Result<BatchStreamSummary> {
        let requests = requests(stocks, |m, c| SparklineRequest::new(m, c, page))?;
        self.command_stream(
            &requests,
            options,
            |s| s.page.records.len(),
            |r, tcp| r.recv(tcp),
            consume,
        )
    }

    pub fn money_flow_daily_batch_stream(
        &self,
        stocks: &[(u16, &str)],
        options: BatchStreamOptions,
        consume: impl FnMut(usize, io::Result<MoneyFlowBlock>) -> io::Result<()>,
    ) -> io::Result<BatchStreamSummary> {
        let requests = requests(stocks, MoneyFlowRequest::new)?;
        self.command_stream(
            &requests,
            options,
            |s| s.page.records.len(),
            |r, tcp| r.recv(tcp),
            consume,
        )
    }

    pub fn auction_series_batch_stream(
        &self,
        stocks: &[(u16, &str)],
        date: Option<u32>,
        page: AuctionOptions,
        options: BatchStreamOptions,
        consume: impl FnMut(usize, io::Result<AuctionSeries>) -> io::Result<()>,
    ) -> io::Result<BatchStreamSummary> {
        let requests = requests(stocks, |m, c| AuctionSeriesRequest::new(m, c, date, page))?;
        self.command_stream(
            &requests,
            options,
            |s| s.page.records.len(),
            |r, tcp| r.recv(tcp),
            consume,
        )
    }

    pub fn recent_minutes_batch_stream(
        &mut self,
        stocks: &[(u16, &str)],
        date: u32,
        options: BatchStreamOptions,
        consume: impl FnMut(usize, io::Result<MinuteSeries>) -> io::Result<()>,
    ) -> io::Result<BatchStreamSummary> {
        let requests = requests(stocks, |m, c| RecentMinutesRequest::new(m, c, date))?;
        options.validate()?;
        if requests.is_empty() {
            return Ok(BatchStreamSummary::default());
        }
        let context =
            OperationContext::new(options.timeout.min(network_policy().batch_timeout), None)?;
        let _guard = context.enter();
        let metadata = self.tcp.with_timeout(options.timeout, |tcp| {
            super::super::quote_metadata::ensure_metadata(
                tcp,
                &self.quote_metadata,
                &stocks.iter().map(|s| s.0).collect::<Vec<_>>(),
                false,
            )
        })?;
        self.command_stream(
            &requests,
            options,
            |s| s.page.records.len(),
            |r, tcp| r.recv(tcp, &metadata),
            consume,
        )
    }

    /// One callback item is an at-most-80-symbol fragment, matching snapshots_batch.
    pub fn snapshots_batch_stream(
        &mut self,
        stocks: &[(u16, &str)],
        options: BatchStreamOptions,
        consume: impl FnMut(usize, io::Result<SnapshotPage>) -> io::Result<()>,
    ) -> io::Result<BatchStreamSummary> {
        super::extended_queries::validate_batch_len(stocks.len())?;
        options.validate()?;
        if stocks.is_empty() {
            return Ok(BatchStreamSummary::default());
        }
        let requests = stocks
            .chunks(SNAPSHOT_BATCH_SIZE)
            .map(SnapshotsRequest::new)
            .collect::<io::Result<Vec<_>>>()?;
        let context =
            OperationContext::new(options.timeout.min(network_policy().batch_timeout), None)?;
        let _guard = context.enter();
        let metadata = self.tcp.with_timeout(options.timeout, |tcp| {
            super::super::quote_metadata::ensure_metadata(
                tcp,
                &self.quote_metadata,
                &stocks.iter().map(|s| s.0).collect::<Vec<_>>(),
                false,
            )
        })?;
        self.command_stream(
            &requests,
            options,
            |s| s.page.records.len(),
            |r, tcp| r.recv(tcp, &metadata),
            consume,
        )
    }

    /// Metadata and F10 text each count as one output object; directory rows
    /// count entries, and data series count points.
    pub fn block_metadata_batch_stream(
        &self,
        filenames: &[&str],
        options: BatchStreamOptions,
        consume: impl FnMut(usize, io::Result<BlockMetadata>) -> io::Result<()>,
    ) -> io::Result<BatchStreamSummary> {
        super::extended_queries::validate_batch_len(filenames.len())?;
        let requests = filenames
            .iter()
            .map(|name| BlockMetaRequest::new(name))
            .collect::<io::Result<Vec<_>>>()?;
        self.command_stream(&requests, options, |_| 1, |r, tcp| r.recv(tcp), consume)
    }

    pub fn company_info_directory_batch_stream(
        &self,
        stocks: &[(u16, &str)],
        options: BatchStreamOptions,
        consume: impl FnMut(usize, io::Result<CompanyInfoDirectory>) -> io::Result<()>,
    ) -> io::Result<BatchStreamSummary> {
        let requests = requests(stocks, CompanyInfoDirectoryRequest::new)?;
        self.command_stream(
            &requests,
            options,
            |s| s.entries.len(),
            |r, tcp| r.recv(tcp),
            consume,
        )
    }

    pub fn company_info_text_batch_stream(
        &self,
        requests: &[CompanyInfoTextRequest],
        page: CompanyInfoReadOptions,
        options: BatchStreamOptions,
        consume: impl FnMut(usize, io::Result<CompanyInfoText>) -> io::Result<()>,
    ) -> io::Result<BatchStreamSummary> {
        super::extended_queries::validate_batch_len(requests.len())?;
        options.validate()?;
        let page = CompanyInfoReadOptions {
            max_bytes: page
                .max_bytes
                .min(u32::try_from(options.max_item_bytes).unwrap_or(u32::MAX)),
            ..page
        };
        page.validate(0)?;
        for request in requests {
            page.validate(request.length())?;
        }
        self.command_stream(
            requests,
            options,
            |_| 1,
            |r, tcp| company_info_text(tcp, r.clone(), page),
            consume,
        )
    }

    pub fn delisted_bars_batch_stream(
        &self,
        requests: &[DelistedBarsRequest],
        options: BatchStreamOptions,
        consume: impl FnMut(usize, io::Result<DelistedBarsPage>) -> io::Result<()>,
    ) -> io::Result<BatchStreamSummary> {
        self.command_stream(
            requests,
            options,
            |s| s.records.len(),
            |r, tcp| r.recv(tcp),
            consume,
        )
    }

    pub fn delisted_bars_all_batch_stream(
        &self,
        stocks: &[(u16, &str)],
        scan: CommandScanOptions,
        options: BatchStreamOptions,
        consume: impl FnMut(usize, io::Result<DelistedBarsCollection>) -> io::Result<()>,
    ) -> io::Result<BatchStreamSummary> {
        options.validate()?;
        let scan = CommandScanOptions {
            max_records: scan.max_records.min(options.max_item_rows),
            ..scan
        };
        scan.validate()?;
        let requests = requests(stocks, |m, c| {
            DelistedBarsRequest::new(m, c, 0, MAX_DELISTED_BARS_COUNT)
        })?;
        self.command_stream(
            &requests,
            options,
            |s| s.records.len(),
            |r, tcp| delisted_bars_all(tcp, r.clone(), scan),
            consume,
        )
    }
}
