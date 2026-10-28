use super::Client;
use crate::tcp::{
    governance::deadline_after,
    network_policy,
    stock::{command_request::check_scan_source, *},
};
use std::io;

impl Client {
    pub fn block_metadata(&mut self, filename: &str) -> io::Result<BlockMetadata> {
        BlockMetaRequest::new(filename)?.recv(&mut self.tcp)
    }
    pub fn block_metadata_batch(
        &self,
        filenames: &[&str],
        max_parallel: usize,
    ) -> io::Result<Vec<io::Result<BlockMetadata>>> {
        super::extended_queries::validate_batch_len(filenames.len())?;
        let requests = filenames
            .iter()
            .map(|name| BlockMetaRequest::new(name))
            .collect::<io::Result<Vec<_>>>()?;
        self.command_batch(
            &requests,
            max_parallel,
            deadline_after(network_policy().batch_timeout)?,
            |r, tcp| r.recv(tcp),
        )
    }
    pub fn company_info_directory(
        &mut self,
        market: u16,
        code: &str,
    ) -> io::Result<CompanyInfoDirectory> {
        CompanyInfoDirectoryRequest::new(market, code)?.recv(&mut self.tcp)
    }
    pub fn company_info_directory_batch(
        &self,
        stocks: &[(u16, &str)],
        max_parallel: usize,
    ) -> io::Result<Vec<io::Result<CompanyInfoDirectory>>> {
        super::extended_queries::validate_batch_len(stocks.len())?;
        let requests = stocks
            .iter()
            .map(|&(m, c)| CompanyInfoDirectoryRequest::new(m, c))
            .collect::<io::Result<Vec<_>>>()?;
        self.command_batch(
            &requests,
            max_parallel,
            deadline_after(network_policy().batch_timeout)?,
            |r, tcp| r.recv(tcp),
        )
    }
    pub fn company_info_chunk(
        &mut self,
        request: &CompanyInfoTextRequest,
    ) -> io::Result<CompanyInfoTextChunk> {
        request.recv(&mut self.tcp)
    }
    pub fn company_info_text(
        &mut self,
        request: CompanyInfoTextRequest,
        options: CompanyInfoReadOptions,
    ) -> io::Result<CompanyInfoText> {
        company_info_text(&mut self.tcp, request, options)
    }
    /// Ordered per-request results; all parameters are checked before pool I/O.
    pub fn company_info_text_batch(
        &self,
        requests: &[CompanyInfoTextRequest],
        options: CompanyInfoReadOptions,
        max_parallel: usize,
    ) -> io::Result<Vec<io::Result<CompanyInfoText>>> {
        super::extended_queries::validate_batch_len(requests.len())?;
        options.validate(0)?;
        for request in requests {
            options.validate(request.length())?;
        }
        self.command_batch(
            requests,
            max_parallel,
            deadline_after(network_policy().batch_timeout)?,
            |r, tcp| company_info_text(tcp, r.clone(), options),
        )
    }
    /// One directory and all sections share the deadline and total byte/chunk budgets.
    pub fn f10_with_options(
        &mut self,
        market: u16,
        code: &str,
        options: CompanyInfoReadOptions,
    ) -> io::Result<Vec<(String, String)>> {
        let request = CompanyInfoDirectoryRequest::new(market, code)?;
        options.validate(0)?;
        self.tcp.with_timeout(options.timeout, |tcp| {
            let directory = request.recv(tcp)?;
            let total = directory.entries.iter().try_fold(0u32, |total, item| {
                total
                    .checked_add(item.length)
                    .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "F10目录总长度溢出"))
            })?;
            options.validate(total)?;
            let mut baseline = Some(directory.source.clone());
            let mut remaining_chunks = options.max_chunks;
            let mut sections = Vec::with_capacity(directory.entries.len());
            for item in directory.entries {
                if item.length == 0 {
                    sections.push((item.name, String::new()));
                    continue;
                }
                let content = company_info_text(
                    tcp,
                    CompanyInfoTextRequest::new(
                        market,
                        request.code(),
                        &item.filename,
                        item.start,
                        item.length,
                    )?,
                    CompanyInfoReadOptions {
                        max_chunks: remaining_chunks,
                        ..options
                    },
                )?;
                for chunk in &content.chunks {
                    check_scan_source(&mut baseline, &chunk.source)?;
                }
                remaining_chunks -= content.chunks.len();
                sections.push((item.name, content.text));
            }
            Ok(sections)
        })
    }
    pub fn delisted_bars(
        &mut self,
        market: u16,
        code: &str,
        start: u16,
        count: u16,
    ) -> io::Result<DelistedBarsPage> {
        DelistedBarsRequest::new(market, code, start, count)?.recv(&mut self.tcp)
    }
    pub fn delisted_bars_scan(
        &mut self,
        request: DelistedBarsRequest,
        options: CommandScanOptions,
    ) -> io::Result<DelistedBarsCollection> {
        delisted_bars_all(&mut self.tcp, request, options)
    }
    pub fn delisted_bars_all(
        &mut self,
        market: u16,
        code: &str,
        options: CommandScanOptions,
    ) -> io::Result<DelistedBarsCollection> {
        self.delisted_bars_scan(
            DelistedBarsRequest::new(market, code, 0, MAX_DELISTED_BARS_COUNT)?,
            options,
        )
    }
    pub fn delisted_bars_batch(
        &self,
        requests: &[DelistedBarsRequest],
        max_parallel: usize,
    ) -> io::Result<Vec<io::Result<DelistedBarsPage>>> {
        self.command_batch(
            requests,
            max_parallel,
            deadline_after(network_policy().batch_timeout)?,
            |r, tcp| r.recv(tcp),
        )
    }
    pub fn delisted_bars_all_batch(
        &self,
        stocks: &[(u16, &str)],
        options: CommandScanOptions,
        max_parallel: usize,
    ) -> io::Result<Vec<io::Result<DelistedBarsCollection>>> {
        super::extended_queries::validate_batch_len(stocks.len())?;
        options.validate()?;
        let requests = stocks
            .iter()
            .map(|&(m, c)| DelistedBarsRequest::new(m, c, 0, MAX_DELISTED_BARS_COUNT))
            .collect::<io::Result<Vec<_>>>()?;
        self.command_batch(
            &requests,
            max_parallel,
            deadline_after(network_policy().batch_timeout)?,
            |r, tcp| delisted_bars_all(tcp, r.clone(), options),
        )
    }
}
