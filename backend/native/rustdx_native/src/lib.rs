mod pool;
mod quota;
mod report;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use rustdx_complete::operation::{self, OperationContext};
use rustdx_complete::tcp::ip::{IpPool, ProbeGroup, ProbeMode, ProbePolicy, configure_probes};
use rustdx_complete::tcp::stock::{Client, PrecisionSnapshot, QuoteMetadataStore};
use rustdx_complete::tcp::{NetworkPolicy, TcpConfig, configure_network};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet, VecDeque};
use std::net::SocketAddr;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock, RwLock, TryLockError};
use std::time::Duration;

const QUOTE_BATCH_SIZE: usize = 60;
const MAX_CONNECTIONS: usize = 8;
const PROBE_CONNECTIONS: usize = 1;
const TOTAL_BUSINESS_CONNECTIONS: usize = 24;
const TOTAL_PROBE_CONNECTIONS: usize = 3;
const RUSTDX_VERSION: &str = "2.0.4";
const RUSTDX_SOURCE_REV: &str = "c4c0273456874898aed50447fd900521d9ffd3b8";
type NetworkSetup = (Option<std::path::PathBuf>, Result<(), String>);
static NETWORK_SETUP: OnceLock<NetworkSetup> = OnceLock::new();

fn configure_budget() -> Result<(), String> {
    let directory = std::env::var_os("RUSTDX_NETWORK_SHARED_DIR")
        .filter(|value| !value.is_empty())
        .map(std::path::PathBuf::from);
    let setup = NETWORK_SETUP.get_or_init(|| {
        let result = (|| {
            let shared =
                directory
                    .clone()
                    .map(|directory| rustdx_complete::tcp::SharedNetworkPolicy {
                        directory,
                        max_connections: TOTAL_BUSINESS_CONNECTIONS,
                    });
            configure_network(NetworkPolicy {
                max_connections: TOTAL_BUSINESS_CONNECTIONS,
                shared: shared.clone(),
                request_timeout: Duration::from_secs(60),
                ..Default::default()
            })
            .map_err(io_error)?;
            configure_probes(ProbePolicy {
                max_connections: TOTAL_PROBE_CONNECTIONS,
                shared: shared.map(|policy| rustdx_complete::tcp::SharedNetworkPolicy {
                    directory: policy.directory.join("probes"),
                    max_connections: TOTAL_PROBE_CONNECTIONS,
                }),
                ..Default::default()
            })
            .map_err(io_error)
        })();
        (directory.clone(), result)
    });
    if setup.0 != directory {
        return Err("[InvalidInput] RUSTDX_NETWORK_SHARED_DIR changed; restart the process to change the global connection budget".into());
    }
    setup.1.clone()
}

fn operation_lock<T>(mutex: &Mutex<T>) -> Result<MutexGuard<'_, T>, String> {
    loop {
        operation::check().map_err(io_error)?;
        match mutex.try_lock() {
            Ok(guard) => return Ok(guard),
            Err(TryLockError::Poisoned(error)) => return Err(error.to_string()),
            Err(TryLockError::WouldBlock) => {
                operation::sleep(Duration::from_millis(20)).map_err(io_error)?
            }
        }
    }
}

fn io_error(error: std::io::Error) -> String {
    format!("[{:?}] {error}", error.kind())
}

fn terminal_error(error: &str) -> bool {
    [
        "[InvalidInput]",
        "[InvalidData]",
        "[NotFound]",
        "[Interrupted]",
        "[SourceChanged]",
        "[Quota]",
    ]
    .iter()
    .any(|prefix| error.starts_with(prefix))
        || error.contains("MissingPrecision")
        || error.contains("UnsupportedPrecision")
}

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

fn bar_source(client: &Client, slot: usize, generation: usize) -> Result<Value, String> {
    Ok(json!({
        "slot": slot,
        "generation": generation,
        "endpoint": client.tcp.get_ref().0.peer_addr().map_err(io_error)?.to_string(),
        "connection_id": client.tcp.connection_id().get(),
        "observed_date": chrono::Utc::now()
            .with_timezone(&chrono::FixedOffset::east_opt(8 * 3600).unwrap())
            .format("%Y%m%d").to_string().parse::<u32>().map_err(|e| e.to_string())?,
    }))
}

struct ManagedClient {
    // Rust drops fields in declaration order: close TCP before releasing quota.
    client: Client,
    _permit: quota::Permit,
}

impl std::ops::Deref for ManagedClient {
    type Target = Client;
    fn deref(&self) -> &Self::Target {
        &self.client
    }
}

impl std::ops::DerefMut for ManagedClient {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.client
    }
}

struct ActiveOperation<'a> {
    requests: &'a Mutex<HashMap<usize, OperationContext>>,
    id: usize,
}

impl Drop for ActiveOperation<'_> {
    fn drop(&mut self) {
        self.requests
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .remove(&self.id);
    }
}

#[pyclass]
struct RustdxClient {
    clients: Vec<Mutex<Option<ManagedClient>>>,
    task_group: String,
    requests: Mutex<HashMap<usize, OperationContext>>,
    next_request: AtomicUsize,
    scheduler: pool::Scheduler,
    config: TcpConfig,
    initialization: Mutex<()>,
    financial_configs: Mutex<HashMap<SocketAddr, TcpConfig>>,
    reference_config: Mutex<Option<TcpConfig>>,
    quote_metadata: Arc<QuoteMetadataStore>,
    metadata_warmup: Mutex<()>,
    servers: Vec<SocketAddr>,
    active_server: AtomicUsize,
    generation: AtomicUsize,
    transition: RwLock<()>,
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
    fn switch_server(&self, expected: usize) -> Result<(), String> {
        let _transition = loop {
            operation::check().map_err(io_error)?;
            match self.transition.try_write() {
                Ok(guard) => break guard,
                Err(TryLockError::Poisoned(error)) => return Err(error.to_string()),
                Err(TryLockError::WouldBlock) => {
                    operation::sleep(Duration::from_millis(20)).map_err(io_error)?
                }
            }
        };
        if self.active_server.load(Ordering::Acquire) != expected || self.servers.len() < 2 {
            return Ok(());
        }
        let valid = self
            .config
            .ip_pool
            .as_ref()
            .and_then(IpPool::snapshot)
            .ok_or("[NotFound] missing daily qualified nodes")?
            .valid_nodes;
        let next = (1..self.servers.len())
            .map(|offset| (expected + offset) % self.servers.len())
            .find(|index| valid.contains(&self.servers[*index]))
            .ok_or("[NotFound] no qualified backup in task group")?;
        // No new operation enters while old readers finish and every socket retires.
        for slot in &self.clients {
            if operation_lock(slot)?.take().is_some() {
                self.total_connections.fetch_sub(1, Ordering::Relaxed);
            }
        }
        self.quote_metadata
            .replace(
                PrecisionSnapshot::from_decimals(&[], chrono::Utc::now(), None)
                    .map_err(io_error)?,
            )
            .map_err(io_error)?;
        self.generation.fetch_add(1, Ordering::AcqRel);
        self.active_server.store(next, Ordering::Release);
        Ok(())
    }

    fn probe_group(&self) -> ProbeGroup {
        match self.task_group.as_str() {
            "realtime" => ProbeGroup::Realtime,
            "minute" => ProbeGroup::Minute,
            "history" => ProbeGroup::History,
            _ => ProbeGroup::Legacy,
        }
    }

    fn ensure_pool(&self, config: &TcpConfig) -> Result<(), String> {
        let _initialization = operation_lock(&self.initialization)?;
        if self.closed.load(Ordering::Acquire) {
            return Err("rustdx connection pool is closed".into());
        }
        let pool = config.ip_pool.as_ref().ok_or("missing rustdx IP pool")?;
        if pool.snapshot().is_none() {
            // The core computes this whole-pool deadline using the actual single probe quota.
            pool.refresh().map_err(io_error)?;
        }
        Ok(())
    }

    fn financial_config(&self, server: Option<&str>) -> Result<TcpConfig, String> {
        let Some(server) = server else {
            return Ok(self.config.clone());
        };
        let address = server
            .parse::<SocketAddr>()
            .map_err(|e| format!("[InvalidInput] {e}"))?;
        let mut configs = self.financial_configs.lock().map_err(|e| e.to_string())?;
        if let Some(config) = configs.get(&address) {
            return Ok(config.clone());
        }
        let mut config = self.config.clone();
        config.ip = Some(address);
        config.ip_pool = Some(
            IpPool::with_probe_group(
                vec![address],
                ProbeMode::FinancialHeartbeat,
                self.probe_group(),
            )
            .map_err(io_error)?,
        );
        // Only the currently configured financial endpoint is retained.
        for old in configs.values() {
            if let Some(pool) = &old.ip_pool {
                pool.stop();
            }
        }
        configs.clear();
        configs.insert(address, config.clone());
        Ok(config)
    }

    fn next_slot(&self) -> usize {
        self.next_slot.fetch_add(1, Ordering::Relaxed) % self.max_connections
    }

    fn reference_config(&self, server: &str) -> Result<TcpConfig, String> {
        let address = server
            .parse::<SocketAddr>()
            .map_err(|error| format!("[InvalidInput] {error}"))?;
        if self.config.ip == Some(address) {
            return Ok(self.config.clone());
        }
        let mut current = operation_lock(&self.reference_config)?;
        if let Some(config) = current.as_ref().filter(|config| config.ip == Some(address)) {
            return Ok(config.clone());
        }
        let mut config = self.config.clone();
        config.ip = Some(address);
        config.ip_pool = Some(
            IpPool::with_probe_group(vec![address], ProbeMode::SecurityCount, self.probe_group())
                .map_err(io_error)?,
        );
        if let Some(old) = current.as_ref().and_then(|config| config.ip_pool.as_ref()) {
            old.stop();
        }
        *current = Some(config.clone());
        Ok(config)
    }

    fn with_client<T>(
        &self,
        index: usize,
        background: bool,
        mut operation: impl FnMut(&mut Client) -> Result<T, String>,
    ) -> Result<T, String> {
        self.with_client_at(
            index,
            &self.config,
            background,
            true,
            false,
            false,
            |client, _slot| operation(client),
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn with_client_at<T>(
        &self,
        index: usize,
        config: &TcpConfig,
        background: bool,
        failover: bool,
        pinned: bool,
        priority: bool,
        mut operation: impl FnMut(&mut Client, usize) -> Result<T, String>,
    ) -> Result<T, String> {
        // A file contains many bounded chunk requests. Its total budget must
        // not be the timeout of one TCP request.
        let timeout = if failover {
            rustdx_complete::tcp::network_policy().request_timeout
        } else {
            rustdx_complete::tcp::network_policy().batch_timeout
        };
        let task = OperationContext::current()
            .is_none()
            .then(|| OperationContext::new(timeout, None))
            .transpose()
            .map_err(io_error)?;
        let _scope = task.as_ref().map(OperationContext::enter);
        let id = self.next_request.fetch_add(1, Ordering::Relaxed);
        {
            let mut requests = self.requests.lock().map_err(|error| error.to_string())?;
            if self.closed.load(Ordering::Acquire) {
                return Err("rustdx connection pool is closed".into());
            }
            requests.insert(
                id,
                OperationContext::current().ok_or("missing operation context")?,
            );
        }
        let _request = ActiveOperation {
            requests: &self.requests,
            id,
        };
        self.ensure_pool(config)?;
        let mut last_error = None;
        for attempt in 0..3 {
            operation::check().map_err(io_error)?;
            if attempt > 0 {
                self.retries.fetch_add(1, Ordering::Relaxed);
            }
            if self.closed.load(Ordering::Acquire) {
                return Err("rustdx connection pool is closed".to_string());
            }
            let mut server_index = 0;
            let result = (|| {
                let lease = if priority {
                    self.scheduler
                        .acquire_priority(index, background, pinned, true)?
                } else if pinned {
                    self.scheduler.acquire_at(index, background, true)?
                } else {
                    self.scheduler.acquire(index, background)?
                };
                let index = lease.index;
                let _transition = loop {
                    operation::check().map_err(io_error)?;
                    match self.transition.try_read() {
                        Ok(guard) => break guard,
                        Err(TryLockError::Poisoned(error)) => return Err(error.to_string()),
                        Err(TryLockError::WouldBlock) => {
                            operation::sleep(Duration::from_millis(20)).map_err(io_error)?
                        }
                    }
                };
                let mut guard = operation_lock(&self.clients[index])?;
                self.active_connections.fetch_add(1, Ordering::Relaxed);
                let _active = ActiveRequest(&self.active_connections);
                let mut selected_config = config.clone();
                server_index = self.active_server.load(Ordering::Acquire);
                if failover {
                    selected_config.ip = Some(self.servers[server_index]);
                }
                if guard.as_ref().is_some_and(|client| {
                    client.tcp.config() != &selected_config || !client.tcp.is_reusable()
                }) {
                    *guard = None;
                    self.total_connections.fetch_sub(1, Ordering::Relaxed);
                }
                if guard.is_none() {
                    if attempt == 0 {
                        operation::sleep(Duration::from_millis(index as u64 * 20))
                            .map_err(io_error)?;
                    }
                    let client = protocol_call(|| {
                        let endpoint = selected_config
                            .ip
                            .ok_or("[InvalidInput] missing endpoint")?;
                        let permit = quota::acquire(&self.task_group, endpoint)?;
                        let client = Client::with_quote_metadata(
                            &selected_config,
                            Arc::clone(&self.quote_metadata),
                        )
                        .map_err(io_error)?;
                        Ok(ManagedClient {
                            client,
                            _permit: permit,
                        })
                    })?;
                    *guard = Some(client);
                    self.total_connections.fetch_add(1, Ordering::Relaxed);
                }
                let client = guard
                    .as_mut()
                    .ok_or("rustdx client initialization failed")?;
                let result = protocol_call(|| {
                    // Bare callers perform one business request. Give admission
                    // its own budget and start the original timeout at transport.
                    // Explicit pagination/file scopes retain their own deadlines.
                    let transport = task
                        .as_ref()
                        .filter(|_| failover)
                        .map(|_| OperationContext::new(config.timeout, None))
                        .transpose()
                        .map_err(io_error)?;
                    let _transport_scope = transport.as_ref().map(OperationContext::enter);
                    operation(client, index)
                });
                if result
                    .as_ref()
                    .is_err_and(|error| !error.starts_with("[SourceChanged]"))
                {
                    *guard = None;
                    self.total_connections.fetch_sub(1, Ordering::Relaxed);
                }
                result
            })();
            match result {
                Ok(value) => return Ok(value),
                Err(error) => {
                    if error.starts_with("[SourceChanged]") {
                        return Err(error);
                    }
                    self.failures.fetch_add(1, Ordering::Relaxed);
                    if !terminal_error(&error) && failover && self.servers.len() > 1 {
                        self.switch_server(server_index)?;
                    }
                    last_error = Some(error);
                    if terminal_error(last_error.as_deref().unwrap_or_default()) {
                        break;
                    }
                    if attempt < 2 {
                        operation::sleep(Duration::from_millis(100 * (attempt + 1) as u64))
                            .map_err(io_error)?;
                    }
                }
            }
        }
        Err(last_error.unwrap_or_else(|| "rustdx operation failed".to_string()))
    }

    fn fetch_quotes(
        &self,
        securities: Vec<(u16, String)>,
        priority: bool,
    ) -> Result<Vec<Value>, String> {
        let task =
            OperationContext::new(rustdx_complete::tcp::network_policy().batch_timeout, None)
                .map_err(io_error)?;
        let _scope = task.enter();
        let context = OperationContext::current().ok_or("missing quote operation context")?;
        if securities.is_empty() {
            return Ok(Vec::new());
        }
        {
            let _warmup = operation_lock(&self.metadata_warmup)?;
            let markets = [0, 1]
                .into_iter()
                .filter(|market| securities.iter().any(|(m, _)| m == market))
                .collect::<Vec<_>>();
            let snapshot = self.quote_metadata.snapshot().map_err(io_error)?;
            let stale = markets
                .into_iter()
                .filter(|market| !snapshot.is_fresh(*market, chrono::Utc::now()))
                .collect::<Vec<_>>();
            if !stale.is_empty() {
                self.with_client_at(
                    0,
                    &self.config,
                    false,
                    true,
                    false,
                    priority,
                    |client, _| client.refresh_quote_metadata(&stale).map_err(io_error),
                )?;
            }
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
                let context = context.clone();
                scope.spawn(move || {
                    let _scope = context.enter();
                    loop {
                        let task = queue.lock().ok().and_then(|mut queue| queue.pop_front());
                        let Some((batch_index, batch)) = task else {
                            break;
                        };
                        let converted = self.with_client_at(
                            worker_index,
                            &self.config,
                            false,
                            true,
                            false,
                            priority,
                            |client, _| {
                                let references = batch
                                    .iter()
                                    .map(|(market, code)| (*market, code.as_str()))
                                    .collect::<Vec<_>>();
                                let quotes = client.quotes(&references).map_err(io_error)?;
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
                                        let quote =
                                            by_code.remove(code.as_str()).ok_or_else(|| {
                                                format!("missing or duplicate rustdx quote: {code}")
                                            })?;
                                        quote_value(*market, quote)
                                    })
                                    .collect()
                            },
                        );
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
    #[pyo3(signature = (max_connections = MAX_CONNECTIONS, server = None, timeout_seconds = 8, background_connections = None, fallback_servers = None, task_group = "legacy"))]
    fn new(
        max_connections: usize,
        server: Option<String>,
        timeout_seconds: u64,
        background_connections: Option<usize>,
        fallback_servers: Option<Vec<String>>,
        task_group: &str,
    ) -> PyResult<Self> {
        let max_connections = max_connections.clamp(1, MAX_CONNECTIONS);
        quota::validate_group(task_group).map_err(runtime_error)?;
        configure_budget().map_err(runtime_error)?;
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
            ip_pool: Some(
                IpPool::with_probe_group(
                    servers.clone(),
                    ProbeMode::SecurityCount,
                    match task_group {
                        "realtime" => ProbeGroup::Realtime,
                        "minute" => ProbeGroup::Minute,
                        "history" => ProbeGroup::History,
                        _ => ProbeGroup::Legacy,
                    },
                )
                .map_err(runtime_error)?,
            ),
            auto_reconnect: 0,
            retry_delay_ms: 200,
            // Retry here after dropping the socket; upstream reconnect opens
            // its replacement before dropping the old TCP connection.
            recheck_empty: false,
        };
        Ok(Self {
            clients: (0..max_connections).map(|_| Mutex::new(None)).collect(),
            task_group: task_group.to_string(),
            requests: Mutex::new(HashMap::new()),
            next_request: AtomicUsize::new(0),
            scheduler: pool::Scheduler::new(
                max_connections,
                background_connections.unwrap_or(max_connections),
            ),
            config,
            initialization: Mutex::new(()),
            financial_configs: Mutex::new(HashMap::new()),
            reference_config: Mutex::new(None),
            quote_metadata: Arc::new(QuoteMetadataStore::default()),
            metadata_warmup: Mutex::new(()),
            servers,
            active_server: AtomicUsize::new(0),
            generation: AtomicUsize::new(0),
            transition: RwLock::new(()),
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

    #[pyo3(signature = (securities, priority=false))]
    fn quotes_json(
        &self,
        py: Python<'_>,
        securities: Vec<(u16, String)>,
        priority: bool,
    ) -> PyResult<String> {
        if securities.len() > 20_000 {
            return Err(PyValueError::new_err(
                "rustdx quote batch exceeds 20000 securities",
            ));
        }
        for (market, code) in &securities {
            validate_security(*market, code)?;
        }
        let rows = py.allow_threads(|| self.fetch_quotes(securities, priority));
        serde_json::to_string(&rows.map_err(runtime_error)?).map_err(runtime_error)
    }

    fn set_background_connections(&self, limit: usize) {
        self.scheduler.set_background_limit(limit);
    }

    fn node_pool_json(&self, py: Python<'_>, refresh: bool) -> PyResult<String> {
        py.allow_threads(|| {
            if refresh {
                let pool = self
                    .config
                    .ip_pool
                    .as_ref()
                    .ok_or_else(|| runtime_error("missing IP pool"))?;
                pool.refresh()
                    .map_err(|error| runtime_error(io_error(error)))?;
            } else {
                self.ensure_pool(&self.config).map_err(runtime_error)?;
            }
            serde_json::to_string(&self.config.ip_pool.as_ref().and_then(IpPool::snapshot))
                .map_err(runtime_error)
        })
    }

    #[pyo3(signature = (market, code, category, start=0, count=800, index=false, with_request_count=false, with_source=false, expected_source=None, background=true, reference_server=None))]
    #[allow(clippy::too_many_arguments)] // Keep the existing Python positional API.
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
        with_source: bool,
        expected_source: Option<String>,
        background: bool,
        reference_server: Option<String>,
    ) -> PyResult<String> {
        validate_security(market, &code)?;
        if !matches!(category, 0..=9) || count == 0 {
            return Err(PyValueError::new_err("invalid rustdx bar category/count"));
        }
        let expected = expected_source
            .map(|value| serde_json::from_str::<Value>(&value))
            .transpose()
            .map_err(runtime_error)?;
        let config = reference_server
            .as_deref()
            .map(|server| self.reference_config(server))
            .transpose()
            .map_err(runtime_error)?
            .unwrap_or_else(|| self.config.clone());
        let slot = if let Some(source) = &expected {
            source["slot"]
                .as_u64()
                .filter(|slot| *slot < self.max_connections as u64)
                .ok_or_else(|| PyValueError::new_err("invalid bar source slot"))?
                as usize
        } else {
            self.next_slot()
        };
        py.allow_threads(|| {
            // Queueing, pool qualification and bounded reconnects need their
            // own budget; the transport deadline starts only after admission.
            let task = OperationContext::new(
                rustdx_complete::tcp::network_policy().request_timeout,
                None,
            )
            .map_err(runtime_error)?;
            let _scope = task.enter();
            let mut requests = 0;
            let mut source = None;
            let result = self.with_client_at(
                slot, &config, background, reference_server.is_none(), expected.is_some(), false, |client, actual_slot| {
                let observed = bar_source(client, actual_slot, self.generation.load(Ordering::Acquire))?;
                if expected.as_ref().is_some_and(|value| *value != observed) {
                    return Err("[SourceChanged] K线来源变化，须从头重采".into());
                }
                let transport = OperationContext::new(self.config.timeout, None).map_err(io_error)?;
                let _transport_scope = transport.enter();
                requests += 1;
                if category == 8 {
                    self.minute_requests.fetch_add(1, Ordering::Relaxed);
                }
                let rows = if index {
                    serde_json::to_string(
                        &client
                            .index_bars(market, &code, category, start, count.min(800))
                            .map_err(io_error)?,
                    )
                } else {
                    serde_json::to_string(
                        &client
                            .bars(market, &code, category, start, count.min(800))
                            .map_err(io_error)?,
                    )
                };
                let rows = rows.map_err(|error| error.to_string())?;
                if bar_source(client, actual_slot, self.generation.load(Ordering::Acquire))? != observed {
                    return Err("[SourceChanged] K线来源变化，须从头重采".into());
                }
                source = Some(observed);
                Ok(rows)
            });
            if with_request_count || with_source {
                serde_json::to_string(&json!({
                    "rows": result.as_ref().ok().and_then(|value| serde_json::from_str::<Value>(value).ok())
                        .unwrap_or_else(|| json!([])),
                    "requests": requests,
                    "error": result.err(),
                    "source": source,
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
            let task = OperationContext::new(
                rustdx_complete::tcp::network_policy().pagination_timeout,
                None,
            )
            .map_err(runtime_error)?;
            let _scope = task.enter();
            self.with_client(slot, true, |client| {
                serde_json::to_string(&client.stocks(market).map_err(|error| match error {
                    rustdx_complete::Error::Io(error) => io_error(error),
                    error => format!("[InvalidData] {error}"),
                })?)
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
                    .map_err(io_error)?
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
                    client.finance_legacy(market, &code).map_err(io_error)?,
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
            let task = OperationContext::new(
                rustdx_complete::tcp::network_policy().pagination_timeout,
                None,
            )
            .map_err(runtime_error)?;
            let _scope = task.enter();
            self.with_client(slot, true, |client| {
                let rows = client.f10(market, &code).map_err(io_error)?;
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
        let config = self
            .financial_config(server.as_deref())
            .map_err(runtime_error)?;
        let slot = self.next_slot();
        let data = py
            .allow_threads(|| {
                self.with_client_at(slot, &config, true, false, false, false, |client, _slot| {
                    if filename != "tdxfin/gpcw.txt" {
                        let listing =
                            report::download(&mut client.tcp, "tdxfin/gpcw.txt", 512_000)?;
                        report::validate_listing(&listing)?;
                    }
                    let bytes = report::download(&mut client.tcp, &filename, max_bytes)?;
                    if filename == "tdxfin/gpcw.txt" {
                        report::validate_listing(&bytes)?;
                    }
                    Ok(bytes)
                })
            })
            .map_err(runtime_error)?;
        Ok(PyBytes::new(py, &data))
    }

    fn stats_json(&self) -> PyResult<String> {
        let total = self.total_connections.load(Ordering::Relaxed);
        let active = self.active_connections.load(Ordering::Relaxed);
        let network = rustdx_complete::tcp::network_stats();
        let probes = rustdx_complete::tcp::ip::probe_stats();
        serde_json::to_string(&json!({
            "max_connections": self.max_connections,
            "business_limit": MAX_CONNECTIONS,
            "probe_limit": PROBE_CONNECTIONS,
            "total_connection_limit": MAX_CONNECTIONS + PROBE_CONNECTIONS,
            "global_business_limit": TOTAL_BUSINESS_CONNECTIONS,
            "global_probe_limit": TOTAL_PROBE_CONNECTIONS,
            "task_quota": quota::stats(&self.task_group),
            "network": {
                "active": network.active, "peak": network.peak,
                "acquisition_timeouts": network.acquisition_timeouts,
                "requests": network.requests, "request_failures": network.request_failures,
                "shared_max_connections": network.shared_max_connections,
            },
            "probes": {
                "active": probes.active, "peak": probes.peak,
                "waits": probes.waits, "timeouts": probes.timeouts,
            },
            "market_pool": self.config.ip_pool.as_ref().and_then(IpPool::snapshot),
            "financial_pools": self.financial_configs.lock().map_err(runtime_error)?
                .values().filter_map(|config| config.ip_pool.as_ref().and_then(IpPool::snapshot))
                .collect::<Vec<_>>(),
            "quote_metadata": self.quote_metadata.snapshot().map_err(runtime_error)?.status(),
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
            "generation": self.generation.load(Ordering::Acquire),
        }))
        .map_err(runtime_error)
    }

    fn close(&self, py: Python<'_>) {
        self.closed.store(true, Ordering::Release);
        self.scheduler.close();
        for context in self
            .requests
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .values()
        {
            context.cancel();
        }
        if let Some(pool) = &self.config.ip_pool {
            pool.stop();
        }
        if let Some(pool) = self
            .reference_config
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
            .and_then(|config| config.ip_pool.as_ref())
        {
            // When the saved reference is the main pool it is already stopped.
            pool.stop();
        }
        for config in self
            .financial_configs
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .values()
        {
            if let Some(pool) = &config.ip_pool {
                pool.stop();
            }
        }
        py.allow_threads(|| {
            for slot in &self.clients {
                if let Ok(mut client) = slot.lock()
                    && client.take().is_some()
                {
                    self.total_connections.fetch_sub(1, Ordering::Relaxed);
                }
            }
        });
    }
}

impl Drop for RustdxClient {
    fn drop(&mut self) {
        self.closed.store(true, Ordering::Release);
        self.scheduler.close();
        if let Some(pool) = &self.config.ip_pool {
            pool.stop();
        }
        if let Some(pool) = self
            .reference_config
            .get_mut()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
            .and_then(|config| config.ip_pool.as_ref())
        {
            pool.stop();
        }
        for config in self
            .financial_configs
            .get_mut()
            .unwrap_or_else(|e| e.into_inner())
            .values()
        {
            if let Some(pool) = &config.ip_pool {
                pool.stop();
            }
        }
    }
}

#[pymodule]
fn tsp_rustdx_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<RustdxClient>()?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add("RUSTDX_VERSION", RUSTDX_VERSION)?;
    module.add("RUSTDX_SOURCE_REV", RUSTDX_SOURCE_REV)?;
    module.add("RUSTDX_CORE_SHA256", env!("RUSTDX_CORE_SHA256"))?;
    module.add("RUSTDX_PATCH_SHA256", env!("RUSTDX_PATCH_SHA256"))?;
    module.add("MAX_CONNECTIONS", MAX_CONNECTIONS)?;
    module.add("PROBE_CONNECTIONS", PROBE_CONNECTIONS)?;
    module.add("TOTAL_BUSINESS_CONNECTIONS", TOTAL_BUSINESS_CONNECTIONS)?;
    module.add("TOTAL_PROBE_CONNECTIONS", TOTAL_PROBE_CONNECTIONS)?;
    module.add("QUOTE_BATCH_SIZE", QUOTE_BATCH_SIZE)?;
    module.add("POOL_API_VERSION", 4)?;
    module.add(
        "SEED_SERVERS",
        rustdx_complete::tcp::ip::STOCK_IP
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>(),
    )?;
    Ok(())
}
