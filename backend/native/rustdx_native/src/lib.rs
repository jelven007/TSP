mod pool;
mod report;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use rustdx_complete::tcp::TcpConfig;
use rustdx_complete::tcp::stock::Client;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet, VecDeque};
use std::net::SocketAddr;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

const QUOTE_BATCH_SIZE: usize = 60;
const MAX_CONNECTIONS: usize = 35;
const RUSTDX_VERSION: &str = "1.12.2";
const RUSTDX_SOURCE_REV: &str = "9dfc024c9662fa5091099c1f17610569ee2eca8b";

fn validate_security(market: u16, code: &str) -> PyResult<()> {
    if !matches!(market, 0 | 1) || code.len() != 6 || !code.bytes().all(|b| b.is_ascii_digit()) {
        return Err(PyValueError::new_err(
            "rustdx requires market 0/1 and six digit code",
        ));
    }
    Ok(())
}

struct ActiveRequest<'a>(&'a AtomicUsize);

impl Drop for ActiveRequest<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::Relaxed);
    }
}

fn protocol_call<T>(operation: impl FnOnce() -> Result<T, String>) -> Result<T, String> {
    catch_unwind(AssertUnwindSafe(operation))
        .unwrap_or_else(|_| Err("rustdx protocol decoder panicked".to_string()))
}

fn runtime_error(error: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(error.to_string())
}

fn quote_value(market: u16, quote: &impl serde::Serialize) -> Result<Value, String> {
    let mut value = serde_json::to_value(quote).map_err(|error| error.to_string())?;
    value
        .as_object_mut()
        .ok_or_else(|| "rustdx quote did not serialize to an object".to_string())?
        .insert("market".to_string(), json!(market));
    Ok(value)
}

#[pyclass]
struct RustdxClient {
    clients: Vec<Mutex<Option<Client>>>,
    scheduler: pool::Scheduler,
    config: TcpConfig,
    servers: Vec<SocketAddr>,
    active_server: AtomicUsize,
    retries: AtomicUsize,
    failures: AtomicUsize,
    minute_requests: AtomicUsize,
    max_connections: usize,
    next_slot: AtomicUsize,
    total_connections: AtomicUsize,
    active_connections: AtomicUsize,
    closed: AtomicBool,
}

impl RustdxClient {
    fn next_slot(&self) -> usize {
        self.next_slot.fetch_add(1, Ordering::Relaxed) % self.max_connections
    }

    fn with_client<T>(
        &self,
        index: usize,
        background: bool,
        operation: impl FnMut(&mut Client) -> Result<T, String>,
    ) -> Result<T, String> {
        self.with_client_at(index, &self.config, background, true, operation)
    }

    fn with_client_at<T>(
        &self,
        index: usize,
        config: &TcpConfig,
        background: bool,
        failover: bool,
        mut operation: impl FnMut(&mut Client) -> Result<T, String>,
    ) -> Result<T, String> {
        let lease = self.scheduler.acquire(index, background)?;
        let index = lease.index;
        let slot = &self.clients[index % self.clients.len()];
        let mut guard = slot.lock().map_err(|error| error.to_string())?;
        if self.closed.load(Ordering::Acquire) {
            return Err("rustdx connection pool is closed".to_string());
        }
        self.active_connections.fetch_add(1, Ordering::Relaxed);
        let _active = ActiveRequest(&self.active_connections);
        let mut last_error = None;
        for attempt in 0..3 {
            if attempt > 0 {
                self.retries.fetch_add(1, Ordering::Relaxed);
            }
            if self.closed.load(Ordering::Acquire) {
                return Err("rustdx connection pool is closed".to_string());
            }
            let mut selected_config = config.clone();
            let server_index =
                self.active_server.load(Ordering::Relaxed) % self.servers.len().max(1);
            if failover && !self.servers.is_empty() {
                selected_config.ip = Some(self.servers[server_index]);
            }
            if guard
                .as_ref()
                .is_some_and(|client| client.tcp.config().ip != selected_config.ip)
            {
                *guard = None;
                self.total_connections.fetch_sub(1, Ordering::Relaxed);
            }
            if guard.is_none() {
                // Stagger only cold handshakes; established sockets run immediately.
                if attempt == 0 {
                    std::thread::sleep(Duration::from_millis(index as u64 * 20));
                }
                match protocol_call(|| {
                    Client::with_config(&selected_config).map_err(|e| e.to_string())
                }) {
                    Ok(client) => {
                        *guard = Some(client);
                        self.total_connections.fetch_add(1, Ordering::Relaxed);
                    }
                    Err(error) => {
                        self.failures.fetch_add(1, Ordering::Relaxed);
                        if failover && self.servers.len() > 1 {
                            let _ = self.active_server.compare_exchange(
                                server_index,
                                (server_index + 1) % self.servers.len(),
                                Ordering::Relaxed,
                                Ordering::Relaxed,
                            );
                        }
                        last_error = Some(error);
                        if attempt < 2 {
                            std::thread::sleep(Duration::from_millis(100 * (attempt + 1) as u64));
                        }
                        continue;
                    }
                }
            }
            match protocol_call(|| operation(guard.as_mut().unwrap())) {
                Ok(value) => return Ok(value),
                Err(error) => {
                    self.failures.fetch_add(1, Ordering::Relaxed);
                    if failover && self.servers.len() > 1 {
                        let _ = self.active_server.compare_exchange(
                            server_index,
                            (server_index + 1) % self.servers.len(),
                            Ordering::Relaxed,
                            Ordering::Relaxed,
                        );
                    }
                    last_error = Some(error);
                    *guard = None;
                    self.total_connections.fetch_sub(1, Ordering::Relaxed);
                    if attempt < 2 {
                        std::thread::sleep(Duration::from_millis(100 * (attempt + 1) as u64));
                    }
                }
            }
        }
        Err(last_error.unwrap_or_else(|| "rustdx operation failed".to_string()))
    }

    fn fetch_quotes(&self, securities: Vec<(u16, String)>) -> Result<Vec<Value>, String> {
        if securities.is_empty() {
            return Ok(Vec::new());
        }
        // Upstream QuoteData omits market. Never infer it from response position:
        // servers can reorder rows, and SH/SZ can share the same six-digit code.
        let mut batches = Vec::new();
        for market in [0, 1] {
            let mut seen = HashSet::new();
            let group = securities
                .iter()
                .filter(|(m, code)| *m == market && seen.insert(code.clone()))
                .cloned()
                .collect::<Vec<_>>();
            batches.extend(group.chunks(QUOTE_BATCH_SIZE).map(|chunk| chunk.to_vec()));
        }
        let worker_count = self.max_connections.min(batches.len()).max(1);
        let queue = Arc::new(Mutex::new(
            batches
                .into_iter()
                .enumerate()
                .collect::<VecDeque<(usize, Vec<(u16, String)>)>>(),
        ));
        let result_count = queue.lock().map_err(|error| error.to_string())?.len();
        let results = Arc::new(Mutex::new(
            (0..result_count)
                .map(|_| None)
                .collect::<Vec<Option<Result<Vec<Value>, String>>>>(),
        ));

        std::thread::scope(|scope| {
            for worker_index in 0..worker_count {
                let queue = Arc::clone(&queue);
                let results = Arc::clone(&results);
                scope.spawn(move || {
                    loop {
                        let task = queue.lock().ok().and_then(|mut queue| queue.pop_front());
                        let Some((batch_index, batch)) = task else {
                            break;
                        };
                        let converted = self.with_client(worker_index, false, |client| {
                            let references = batch
                                .iter()
                                .map(|(market, code)| (*market, code.as_str()))
                                .collect::<Vec<_>>();
                            let quotes = client
                                .quotes(&references)
                                .map_err(|error| error.to_string())?;
                            if quotes.len() != batch.len() {
                                return Err(format!(
                                    "rustdx quote batch incomplete: {}/{}",
                                    quotes.len(),
                                    batch.len()
                                ));
                            }
                            let mut by_code = quotes
                                .iter()
                                .map(|q| (q.code.as_str(), q))
                                .collect::<HashMap<_, _>>();
                            batch
                                .iter()
                                .map(|(market, code)| {
                                    let quote = by_code.remove(code.as_str()).ok_or_else(|| {
                                        format!("missing or duplicate rustdx quote: {code}")
                                    })?;
                                    quote_value(*market, quote)
                                })
                                .collect()
                        });
                        if let Ok(mut slots) = results.lock() {
                            slots[batch_index] = Some(converted);
                        }
                    }
                });
            }
        });

        let mut output = Vec::with_capacity(securities.len());
        let mut slots = results.lock().map_err(|error| error.to_string())?;
        for slot in slots.iter_mut() {
            match slot.take() {
                Some(Ok(mut rows)) => output.append(&mut rows),
                Some(Err(error)) => return Err(error),
                None => return Err("rustdx worker did not complete a quote batch".to_string()),
            }
        }
        let by_security = output
            .into_iter()
            .map(|row| {
                let key = (
                    row["market"].as_u64().unwrap() as u16,
                    row["code"].as_str().unwrap().to_string(),
                );
                (key, row)
            })
            .collect::<HashMap<_, _>>();
        securities
            .iter()
            .map(|key| {
                by_security
                    .get(key)
                    .cloned()
                    .ok_or_else(|| "missing rustdx quote result".to_string())
            })
            .collect()
    }
}

#[pymethods]
impl RustdxClient {
    #[new]
    #[pyo3(signature = (max_connections = MAX_CONNECTIONS, server = None, timeout_seconds = 8, background_connections = None, fallback_servers = None))]
    fn new(
        max_connections: usize,
        server: Option<String>,
        timeout_seconds: u64,
        background_connections: Option<usize>,
        fallback_servers: Option<Vec<String>>,
    ) -> PyResult<Self> {
        let max_connections = max_connections.clamp(1, MAX_CONNECTIONS);
        let ip = server
            .map(|value| value.parse::<SocketAddr>())
            .transpose()
            .map_err(|error| PyValueError::new_err(format!("invalid rustdx server: {error}")))?;
        let mut servers = ip.into_iter().collect::<Vec<_>>();
        for value in fallback_servers.unwrap_or_default() {
            let address = value.parse::<SocketAddr>().map_err(|error| {
                PyValueError::new_err(format!("invalid rustdx server: {error}"))
            })?;
            if !servers.contains(&address) {
                servers.push(address);
            }
        }
        if servers.is_empty() {
            // Bound default node selection to the same three attempts; do not
            // let upstream scan its entire host list inside one attempt.
            servers.extend(rustdx_complete::tcp::ip::STOCK_IP.iter().take(3).copied());
        }
        let config = TcpConfig {
            timeout: Duration::from_secs(timeout_seconds.clamp(2, 60)),
            ip: servers.first().copied(),
            auto_reconnect: 0,
            retry_delay_ms: 200,
            // Retry here after dropping the socket; upstream reconnect opens
            // its replacement before dropping the old TCP connection.
            recheck_empty: false,
        };
        Ok(Self {
            clients: (0..max_connections).map(|_| Mutex::new(None)).collect(),
            scheduler: pool::Scheduler::new(
                max_connections,
                background_connections.unwrap_or(max_connections),
            ),
            config,
            servers,
            active_server: AtomicUsize::new(0),
            retries: AtomicUsize::new(0),
            failures: AtomicUsize::new(0),
            minute_requests: AtomicUsize::new(0),
            max_connections,
            next_slot: AtomicUsize::new(0),
            total_connections: AtomicUsize::new(0),
            active_connections: AtomicUsize::new(0),
            closed: AtomicBool::new(false),
        })
    }

    #[getter]
    fn max_connections(&self) -> usize {
        self.max_connections
    }

    fn quotes_json(&self, py: Python<'_>, securities: Vec<(u16, String)>) -> PyResult<String> {
        for (market, code) in &securities {
            validate_security(*market, code)?;
        }
        let rows = py.allow_threads(|| self.fetch_quotes(securities));
        serde_json::to_string(&rows.map_err(runtime_error)?).map_err(runtime_error)
    }

    #[pyo3(signature = (market, code, category, start=0, count=800, index=false, with_request_count=false))]
    fn bars_json(
        &self,
        py: Python<'_>,
        market: u16,
        code: String,
        category: u16,
        start: u16,
        count: u16,
        index: bool,
        with_request_count: bool,
    ) -> PyResult<String> {
        validate_security(market, &code)?;
        if !matches!(category, 0..=9) || count == 0 {
            return Err(PyValueError::new_err("invalid rustdx bar category/count"));
        }
        let slot = self.next_slot();
        py.allow_threads(|| {
            let mut requests = 0;
            let result = self.with_client(slot, true, |client| {
                requests += 1;
                if category == 8 {
                    self.minute_requests.fetch_add(1, Ordering::Relaxed);
                }
                let rows = if index {
                    serde_json::to_string(
                        &client
                            .index_bars(market, &code, category, start, count.min(800))
                            .map_err(|error| error.to_string())?,
                    )
                } else {
                    serde_json::to_string(
                        &client
                            .bars(market, &code, category, start, count.min(800))
                            .map_err(|error| error.to_string())?,
                    )
                };
                rows.map_err(|error| error.to_string())
            });
            if with_request_count {
                serde_json::to_string(&json!({
                    "rows": result.as_ref().ok().and_then(|value| serde_json::from_str::<Value>(value).ok())
                        .unwrap_or_else(|| json!([])),
                    "requests": requests,
                    "error": result.err(),
                })).map_err(runtime_error)
            } else {
                result.map_err(runtime_error)
            }
        })
    }

    fn stocks_json(&self, py: Python<'_>, market: u16) -> PyResult<String> {
        validate_security(market, "000001")?;
        let slot = self.next_slot();
        py.allow_threads(|| {
            self.with_client(slot, true, |client| {
                serde_json::to_string(&client.stocks(market).map_err(|error| error.to_string())?)
                    .map_err(|error| error.to_string())
            })
            .map_err(runtime_error)
        })
    }

    fn xdxr_json(&self, py: Python<'_>, market: u16, code: String) -> PyResult<String> {
        validate_security(market, &code)?;
        let slot = self.next_slot();
        py.allow_threads(|| {
            self.with_client(slot, true, |client| {
                let rows = client
                    .xdxr(market, &code)
                    .map_err(|error| error.to_string())?
                    .into_iter()
                    .map(|row| {
                        json!({
                            "market": row.market,
                            "code": row.code,
                            "year": row.date / 10000,
                            "month": row.date % 10000 / 100,
                            "day": row.date % 100,
                            "category": row.category,
                            "fenhong": row.fh_qltp,
                            "peigujia": row.pgj_qzgb,
                            "songzhuangu": row.sg_hltp,
                            "peigu": row.pg_hzgb,
                        })
                    })
                    .collect::<Vec<_>>();
                serde_json::to_string(&rows).map_err(|error| error.to_string())
            })
            .map_err(runtime_error)
        })
    }

    fn finance_json(&self, py: Python<'_>, market: u16, code: String) -> PyResult<String> {
        validate_security(market, &code)?;
        let slot = self.next_slot();
        py.allow_threads(|| {
            self.with_client(slot, true, |client| {
                serde_json::to_string(&vec![
                    client
                        .finance(market, &code)
                        .map_err(|error| error.to_string())?,
                ])
                .map_err(|error| error.to_string())
            })
            .map_err(runtime_error)
        })
    }

    fn company_info(
        &self,
        py: Python<'_>,
        market: u16,
        code: String,
        category_name: String,
    ) -> PyResult<Option<String>> {
        validate_security(market, &code)?;
        let slot = self.next_slot();
        py.allow_threads(|| {
            self.with_client(slot, true, |client| {
                let rows = client
                    .f10(market, &code)
                    .map_err(|error| error.to_string())?;
                Ok(rows
                    .into_iter()
                    .find(|(name, _)| name.trim() == category_name.trim())
                    .map(|(_, content)| content))
            })
            .map_err(runtime_error)
        })
    }

    #[pyo3(signature = (filename, max_bytes, server=None))]
    fn report_file<'py>(
        &self,
        py: Python<'py>,
        filename: String,
        max_bytes: usize,
        server: Option<String>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let mut config = self.config.clone();
        if let Some(server) = server {
            config.ip = Some(
                server
                    .parse::<SocketAddr>()
                    .map_err(|e| PyValueError::new_err(e.to_string()))?,
            );
        }
        let slot = self.next_slot();
        let data = py
            .allow_threads(|| {
                self.with_client_at(slot, &config, true, false, |client| {
                    report::download(&mut client.tcp, &filename, max_bytes)
                })
            })
            .map_err(runtime_error)?;
        Ok(PyBytes::new(py, &data))
    }

    fn stats_json(&self) -> PyResult<String> {
        let total = self.total_connections.load(Ordering::Relaxed);
        let active = self.active_connections.load(Ordering::Relaxed);
        serde_json::to_string(&json!({
            "max_connections": self.max_connections,
            "total_connections": total,
            "active_connections": active,
            "idle_connections": total.saturating_sub(active),
            "closed": self.closed.load(Ordering::Acquire),
            "quote_batch_size": QUOTE_BATCH_SIZE,
            "scheduler": self.scheduler.stats(),
            "retries": self.retries.load(Ordering::Relaxed),
            "failures": self.failures.load(Ordering::Relaxed),
            "minute_requests": self.minute_requests.load(Ordering::Relaxed),
            "active_server": self.servers.get(self.active_server.load(Ordering::Relaxed))
                .map(|address| address.to_string()),
        }))
        .map_err(runtime_error)
    }

    fn close(&self, py: Python<'_>) {
        self.closed.store(true, Ordering::Release);
        self.scheduler.close();
        py.allow_threads(|| {
            for slot in &self.clients {
                if let Ok(mut client) = slot.lock() {
                    if client.take().is_some() {
                        self.total_connections.fetch_sub(1, Ordering::Relaxed);
                    }
                }
            }
        });
    }
}

#[pymodule]
fn tsp_rustdx_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<RustdxClient>()?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add("RUSTDX_VERSION", RUSTDX_VERSION)?;
    module.add("RUSTDX_SOURCE_REV", RUSTDX_SOURCE_REV)?;
    module.add("MAX_CONNECTIONS", MAX_CONNECTIONS)?;
    module.add("QUOTE_BATCH_SIZE", QUOTE_BATCH_SIZE)?;
    module.add("POOL_API_VERSION", 2)?;
    Ok(())
}
