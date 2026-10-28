mod kline;
pub use kline::{Kline, KlineData};

mod index_kline;
pub use index_kline::{IndexKline, IndexKlineData};

mod kline_request;
pub use kline_request::{KlineRequest, decode_index_bars, decode_stock_bars};

mod requests;
pub use requests::{
    FinanceRequest, HistoryTransactionRequest, QuotesRequest, TransactionRequest,
    decode_finance_legacy, decode_history_transactions, decode_quotes, decode_quotes_legacy,
    decode_quotes_with_metadata, decode_transactions,
};

mod trade_decode;
pub use trade_decode::decode_trade_page;
mod trade_models;
pub use trade_models::{
    TRADE_SEMANTIC_SCHEMA_VERSION, TradeCollection, TradeCounts, TradeCoverage, TradeDirection,
    TradeEventKind, TradePage, TradePhase, TradeProtocol, TradeQualification, TradeQuery,
    TradeRecord, TradeSource, TradeTermination, TradeWarning,
};

mod trade_amount;
pub use trade_amount::{
    AmountAvailability, AmountOrigin, AmountQualification, AmountUnavailableReason,
    TRADE_AMOUNT_SCHEMA_VERSION, TradeAmountContext, TradeAmountDateBasis, TradeAmountEstimate,
    TradeAmountOutcome, TradeEstimateComponent, TradeEstimateOptions, TradeEstimateSummary,
    TradeQuantityUnit,
};
mod reported_turnover;
pub use reported_turnover::{
    ReportedTurnover, ReportedTurnoverBar, ReportedTurnoverSeries, TurnoverAvailability,
    TurnoverDateBasis, TurnoverOptions, TurnoverOutcome, TurnoverProfile, TurnoverQuery,
    TurnoverSampling, TurnoverScope, TurnoverSource, TurnoverSummation, TurnoverUnavailableReason,
    decode_nonnegative_binary32, decode_reported_turnover,
};
mod trade_amount_reconcile;
pub use trade_amount_reconcile::{
    ReconciliationSourceStatus, TradeAmountAlignment, TradeAmountInterval,
    TradeAmountReconciliation, reconcile_trade_amounts,
};

mod finance_raw;
pub use finance_raw::{FINANCE_BODY_SIZE, FinanceRaw, decode_finance_raw};
mod finance_semantics;
pub use finance_semantics::{
    FINANCE_SEMANTIC_SCHEMA_VERSION, FinanceCandidateRejection, FinanceCashFlowReconciliation,
    FinanceField, FinanceFieldValue, FinanceProfile, FinanceSemanticStatus, FinanceSnapshot,
    FinanceUnit, decode_finance, normalize_finance,
};
mod price_scale;
pub use price_scale::PriceScale;
mod quote_metadata;
pub use quote_metadata::{
    MarketPrecision, PrecisionSnapshot, QuoteMetadataStore, QuotePage, QuotePrecision,
};

mod xdxr;
pub use xdxr::*;

mod quotes;
pub use quotes::{QuoteData, SecurityQuotes};

mod security_list;
pub use security_list::{SecurityList, SecurityListData};

mod security_directory;
pub use security_directory::{
    MAX_SECURITY_DIRECTORY_COUNT, SECURITY_DIRECTORY_COMMAND, SecurityBoard, SecurityCategory,
    SecurityDirectoryData, SecurityDirectoryPage, SecurityDirectoryRequest,
    decode_security_directory, security_directory,
};

mod minute_time;
pub use minute_time::{MinuteTime, MinuteTimeData};

mod today_minute;
pub use today_minute::{
    TodayMinutePoint, TodayMinuteRequest, TodayMinuteSeries, decode_today_minutes,
};

mod transaction;
pub use transaction::{Transaction, TransactionData};

mod transaction_pagination;
pub use transaction_pagination::{
    TransactionCollection, TransactionPageInfo, TransactionPaginationError,
    TransactionPaginationFailure, TransactionPaginationOptions, TransactionPaginationTermination,
};

mod finance_info;
pub use finance_info::{FinanceInfo, FinanceInfoData};

mod history_minute_time;
pub use history_minute_time::HistoryMinuteTime;

mod history_transaction;
pub use history_transaction::HistoryTransaction;

mod enhanced_history_transaction;
pub use enhanced_history_transaction::{
    ENHANCED_HISTORY_TRANSACTION_COMMAND, EnhancedHistoryTransactionData,
    EnhancedHistoryTransactionPage, EnhancedHistoryTransactionRequest, EnhancedTradeEventKind,
    EnhancedTradeSide, MAX_ENHANCED_HISTORY_TRANSACTION_COUNT,
    decode_enhanced_history_transactions,
};

mod block_info;
pub use block_info::{
    BlockFile, BlockFileChunk, BlockInfoChunk, BlockInfoMeta, BlockRecord, decode_block_content,
    download_block_file, get_block_info, parse_block_content,
};

mod company_info;
pub use company_info::{CompanyInfoCategory, CompanyInfoCategoryItem, CompanyInfoContent};

mod resource_metadata;
pub use resource_metadata::{
    BLOCK_META_COMMAND, BlockMetaRequest, BlockMetadata, decode_block_metadata,
};
mod resource_file;
pub use resource_file::{
    DEFAULT_FILE_CHUNK_SIZE, FILE_CONTENT_COMMAND, FileContentChunk, FileContentRequest,
    MAX_FILE_CHUNK_SIZE, MAX_FILE_PATH_BYTES, ResourceFile, ResourceFileChunk, ResourceReadOptions,
    ResourceTermination, decode_file_content, download_resource_file,
};
mod resource_stats;
pub use resource_stats::{
    MAX_STATS_ZIP_BYTES, StatsCandidateValues, StatsMember, StatsRow, TdxStat2Values,
    TdxStatValues, TdxStatsArchive, TdxStatsResource, parse_tdx_stats_archive, read_tdx_stats,
};
mod company_info_request;
pub use company_info_request::{
    COMPANY_INFO_DIRECTORY_COMMAND, COMPANY_INFO_TEXT_COMMAND, CompanyInfoDirectory,
    CompanyInfoDirectoryRequest, CompanyInfoEntry, CompanyInfoReadOptions, CompanyInfoText,
    CompanyInfoTextChunk, CompanyInfoTextRequest, MAX_COMPANY_INFO_CHUNK, company_info_text,
    decode_company_info_directory, decode_company_info_text,
};
mod daily_reference;
mod delisted_bars;
pub use daily_reference::{
    DailyReferenceComparison, DailyReferenceField, DailyReferenceRecord, DayFileReference,
};
pub use delisted_bars::{
    DELISTED_BARS_COMMAND, DailyBar, DailyBarsPage, DailyBarsRequest, DelistedBar,
    DelistedBarsCollection, DelistedBarsPage, DelistedBarsRequest, MAX_DELISTED_BARS_COUNT,
    decode_daily_bars, decode_delisted_amount, decode_delisted_bars, delisted_bars_all,
};

mod client;
pub use client::{
    Adj, BarBatchStreamOptions, BarRangeCollection, BarRangeCoverage, BarRangeOptions,
    BarRangePage, BarRangeSource, BarRangeTermination, BatchBarRange, BatchBars, BatchKline,
    BatchResultBudget, BatchStreamOptions, BatchStreamSummary, Client, ex_right_reference,
};

mod command_request;
pub use command_request::{
    CommandCollection, CommandPage, CommandScanOptions, CommandSource, CommandSymbol,
    CommandTermination,
};
mod special_limits;
pub use special_limits::{
    SPECIAL_LIMITS_COMMAND, SpecialLimitCollection, SpecialLimitPage, SpecialLimitRecord,
    SpecialLimitsRequest, decode_special_limits,
};
mod intraday_aux;
pub use intraday_aux::*;
mod sparkline;
pub use sparkline::*;
mod recent_minutes;
pub use recent_minutes::*;
mod auctions;
pub use auctions::{
    AUCTION_SERIES_COMMAND, AuctionCollection, AuctionOptions, AuctionPoint, AuctionSeries,
    AuctionSeriesRequest, decode_auction_series,
};
mod money_flow;
pub use money_flow::*;
mod extended_quote;
pub use extended_quote::{BasicQuote, QuoteLevel};
mod snapshots;
pub use snapshots::*;
mod category_quotes;
pub use category_quotes::{
    CATEGORY_QUOTES_COMMAND, CategoryQuote, CategoryQuoteCollection, CategoryQuoteOptions,
    CategoryQuotePage, CategoryQuotesRequest, QuoteCategory, QuoteSortField,
    decode_category_quotes,
};
mod quote_refresh;
pub use quote_refresh::*;
mod quote_subscription;
pub use quote_subscription::*;

/// 根据六位股票代码推断市场代码。
///
/// - `6` 开头 → 沪市（`1`），如 600xxx、688xxx
/// - `0` / `3` 开头 → 深市（`0`），如 000xxx、300xxx
/// - 其他（4/8 开头的北交所等）暂不支持，返回 `None`
///
/// ## 示例
/// ```
/// use rustdx_complete::tcp::stock::market_of;
///
/// assert_eq!(market_of("600519"), Some(1));
/// assert_eq!(market_of("300750"), Some(0));
/// assert_eq!(market_of("830000"), None);
/// ```
pub fn market_of(code: &str) -> Option<u16> {
    if code.len() != 6 || !code.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    match code.as_bytes().first()? {
        b'6' => Some(1),
        b'0' | b'3' => Some(0),
        _ => None,
    }
}

/// 获取市场全部证券列表（044d，最多1600条/页，映射兼容字段）。
///
/// 数量不一致、重复代码或未达到总数就返回空页时，返回错误。
///
/// ## 示例
/// ```ignore
/// use rustdx_complete::tcp::{Tcp, stock::stocks};
///
/// let mut tcp = Tcp::new()?;
/// let all = stocks(&mut tcp, 0)?; // 深市全部证券
/// println!("深市共 {} 只证券", all.len());
/// ```
pub fn stocks(tcp: &mut crate::tcp::Tcp, market: u16) -> crate::Result<Vec<SecurityListData>> {
    Ok(security_directory(tcp, market)?
        .iter()
        .map(SecurityDirectoryData::to_legacy)
        .collect())
}

/// 显式使用旧0450目录，1000条/页。
pub fn stocks_legacy(
    tcp: &mut crate::tcp::Tcp,
    market: u16,
) -> crate::Result<Vec<SecurityListData>> {
    Ok(
        tcp.with_timeout(crate::tcp::network_policy().pagination_timeout, |tcp| {
            stocks_until(tcp, market)
        })?,
    )
}

fn stocks_until(tcp: &mut crate::tcp::Tcp, market: u16) -> std::io::Result<Vec<SecurityListData>> {
    use crate::tcp::Tdx;
    use std::io::{Error, ErrorKind};
    if market > 1 {
        return Err(Error::new(ErrorKind::InvalidInput, "market必须为0或1"));
    }
    let count = {
        let mut sc = crate::tcp::basic::SecurityCount::new(market);
        *sc.recv_parsed(tcp)?
    };
    let mut all = Vec::with_capacity(count as usize);
    let mut codes = std::collections::HashSet::with_capacity(count as usize);
    while all.len() < count as usize {
        let mut list = SecurityList::new(market, all.len() as u16);
        list.recv_parsed(tcp)?;
        let n = list.result().len();
        if n == 0 {
            return Err(Error::new(
                ErrorKind::UnexpectedEof,
                format!("证券列表未完成: 已获取{}，预期{count}", all.len()),
            ));
        }
        if n > 1000 || all.len() + n > count as usize {
            return Err(Error::new(
                ErrorKind::InvalidData,
                "证券列表数量超出预期，请重新获取",
            ));
        }
        for stock in list.result() {
            if !codes.insert(stock.code.clone()) {
                return Err(Error::new(
                    ErrorKind::InvalidData,
                    format!("证券列表代码重复: {}", stock.code),
                ));
            }
        }
        all.extend(list.result().iter().cloned());
    }
    Ok(all)
}

mod industry_mapping;
#[allow(deprecated)]
pub use industry_mapping::{get_industry_info, get_industry_name, get_province_name};

mod concept_mapping;
#[allow(deprecated)]
pub use concept_mapping::{ConceptStock, get_concept_info, get_concept_names, get_concept_stocks};

// 数据验证模块
pub mod validator;
pub use validator::{
    DataLocation, Validatable, ValidationLevel, ValidationResult, detect_anomalies,
    validate_finance_consistency, validate_finance_snapshot, validate_kline_continuity,
};

#[cfg(test)]
mod tests {
    use super::*;

    fn page(codes: &[&str]) -> Vec<u8> {
        let mut bytes = (codes.len() as u16).to_le_bytes().to_vec();
        for code in codes {
            let mut record = [0u8; 29];
            record[..6].copy_from_slice(code.as_bytes());
            bytes.extend_from_slice(&record);
        }
        bytes
    }

    #[test]
    fn stocks_rejects_incomplete_duplicate_and_excess_pages() {
        for last in [page(&[]), page(&["000001"]), page(&["000002", "000003"])] {
            let (config, server) = crate::tcp::tests::mock_server(1, move |req| {
                if req[10] == 0x0d {
                    vec![0, 0]
                } else if req[10] == 0x4e {
                    vec![2, 0]
                } else if req[14..16] == [0, 0] {
                    page(&["000001"])
                } else {
                    last.clone()
                }
            });
            let mut tcp = crate::tcp::Tcp::with_config(&config).unwrap();
            assert!(stocks_legacy(&mut tcp, 0).is_err());
            drop(tcp);
            server.join().unwrap();
        }
    }

    #[test]
    fn stocks_collects_pages_in_order() {
        let (config, server) = crate::tcp::tests::mock_server(1, |req| {
            if req[10] == 0x0d {
                vec![0, 0]
            } else if req[10] == 0x4e {
                vec![2, 0]
            } else if req[14..16] == [0, 0] {
                page(&["000001"])
            } else {
                page(&["000002"])
            }
        });
        let mut tcp = crate::tcp::Tcp::with_config(&config).unwrap();
        let rows = stocks_legacy(&mut tcp, 0).unwrap();
        assert_eq!(
            rows.iter().map(|r| r.code.as_str()).collect::<Vec<_>>(),
            ["000001", "000002"]
        );
        assert!(stocks(&mut tcp, 2).is_err());
        drop(tcp);
        server.join().unwrap();
        for invalid in ["6", "60000x", "0000010", ""] {
            assert_eq!(market_of(invalid), None);
        }
    }
}
