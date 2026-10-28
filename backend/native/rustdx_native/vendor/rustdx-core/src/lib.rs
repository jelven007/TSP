#![cfg_attr(docsrs, feature(doc_auto_cfg))]

pub mod bytes_helper;

pub mod file;

pub mod tcp;

pub mod indicators;

pub mod cache;

pub mod error;

pub mod calendar;

pub mod data;

pub mod atomic_file;

pub mod operation;

/// A 股涨跌停规则：板块判定、涨跌停价、封板/炸板判型、连板高度。
pub mod limit;

pub mod builder;

pub mod pool;

// 重新导出常用错误类型
pub use error::{CacheError, Error, IndicatorError, Result, TcpError, ValidationError};

/// Error used by the legacy general `Error::Indicator` wrapper.
/// The original `IndicatorError` path remains available.
pub use error::IndicatorError as LegacyIndicatorError;
/// Parameter errors returned by checked EMA/MACD APIs and incremental states.
/// The original `indicators::IndicatorError` path remains available.
pub use indicators::IndicatorError as IncrementalIndicatorError;

// 重新导出Builder
pub use builder::KlineBuilder;

// 重新导出连接池
pub use pool::{ConnectionPool, PoolConfig, PoolStats};
