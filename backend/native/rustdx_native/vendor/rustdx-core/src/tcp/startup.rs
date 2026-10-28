//! Process startup: measure all seeds, publish today's pool, and warm one client.
//!
//! The shipped binaries call [`initialize_from_env`] before dispatching work.
//! Embedding applications call [`initialize`] or [`initialize_with_config`] in
//! their own main, after configuring the network policy.
use super::{
    HeartbeatAck, Tcp, TcpConfig,
    governance::{deadline_after, lock_until, remaining},
    ip::{DailyIpPool, IpPool, default_pool},
    network_policy,
    stock::Client,
};
use std::{
    io,
    net::SocketAddr,
    num::NonZeroU64,
    sync::{Mutex, OnceLock},
    time::{Duration, Instant},
};

/// Evidence of a successfully completed startup. The connection may later be
/// handed to a caller or released to make room in the shared network budget.
#[derive(Debug, Clone, serde::Serialize)]
pub struct StartupReport {
    pub daily: DailyIpPool,
    pub endpoint: SocketAddr,
    pub connection_id: NonZeroU64,
    pub heartbeat: HeartbeatAck,
}

struct Completed {
    config: TcpConfig,
    initial: Vec<SocketAddr>,
    report: StartupReport,
}

#[derive(Default)]
struct State {
    completed: Option<Completed>,
    warm: Option<Tcp>,
}

static INITIALIZING: Mutex<()> = Mutex::new(());
static STATE: OnceLock<Mutex<State>> = OnceLock::new();

fn state() -> &'static Mutex<State> {
    STATE.get_or_init(|| Mutex::new(State::default()))
}

fn normalized(config: &TcpConfig) -> TcpConfig {
    TcpConfig {
        ip_pool: Some(selected_pool(config).clone()),
        ..config.clone()
    }
}

fn selected_pool(config: &TcpConfig) -> &IpPool {
    config.ip_pool.as_ref().unwrap_or_else(|| default_pool())
}

/// Initialize the process default pool and TCP client once per current snapshot.
///
/// ```no_run
/// use rustdx_complete::tcp::{startup, stock::Client};
/// fn main() -> std::io::Result<()> {
///     let report = startup::initialize()?;
///     println!("{} valid nodes", report.daily.valid_nodes.len());
///     let mut client = Client::new()?; // takes the warmed connection
///     let quotes = client.quotes(&[(1, "600000")])?;
///     Ok(())
/// }
/// ```
pub fn initialize() -> io::Result<StartupReport> {
    initialize_with_config(&TcpConfig::default())
}

/// Use an embedding application's pool/configuration. Success is idempotent for
/// the same configuration, initial list, and current daily measurement. Failures
/// can be retried. A changed list/configuration triggers full measurement.
pub fn initialize_with_config(config: &TcpConfig) -> io::Result<StartupReport> {
    let budget = selected_pool(config)
        .probe_budget()?
        .saturating_add(network_policy().connect_timeout)
        .saturating_add(network_policy().request_timeout)
        .saturating_add(Duration::from_secs(5));
    initialize_until(config, deadline_after(budget)?)
}

/// All startup phases, including waiting for another initializer, share this
/// absolute deadline and inherit the current OperationContext's cancellation.
pub fn initialize_until(config: &TcpConfig, deadline: Instant) -> io::Result<StartupReport> {
    if config.timeout.is_zero() {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "超时必须大于0"));
    }
    let _initializing = lock_until(&INITIALIZING, deadline)?;
    let config = normalized(config);
    let pool = selected_pool(&config);
    let initial = pool.list();
    {
        let state = state().lock().unwrap_or_else(|error| error.into_inner());
        if let Some(completed) = &state.completed
            && completed.config == config
            && completed.initial == initial
            && pool.contains_current(completed.report.endpoint)
            && pool
                .snapshot()
                .is_some_and(|daily| daily.tested_at == completed.report.daily.tested_at)
        {
            remaining(deadline)?;
            return Ok(completed.report.clone());
        }
    }
    release_warm_client();
    state()
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .completed = None;
    log::info!(
        "启动TCP初始化：{}个初始节点，三轮、8并发、各阶段3秒",
        initial.len()
    );
    let daily = pool.refresh_until(deadline)?;
    if daily.valid_nodes.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::NotConnected,
            "当日有效IP池为空",
        ));
    }
    remaining(deadline)?;
    // Build the real high-level client; no cache is held while it acquires a permit.
    let attempts = if config.ip.is_some() {
        1
    } else {
        daily.valid_nodes.len()
    };
    let mut last_error = io::Error::new(io::ErrorKind::NotConnected, "启动TCP客户端不可用");
    let mut ready = None;
    for _ in 0..attempts {
        remaining(deadline)?;
        match Client::with_config_until(&config, deadline).and_then(|mut client| {
            let heartbeat = client.tcp.with_deadline(deadline, |tcp| tcp.heartbeat())?;
            Ok((client, heartbeat))
        }) {
            Ok(client) => {
                ready = Some(client);
                break;
            }
            Err(error) => last_error = error,
        }
    }
    let (client, heartbeat) = ready.ok_or(last_error)?;
    remaining(deadline)?;
    let report = StartupReport {
        daily,
        endpoint: client.tcp.get_ref().0.peer_addr()?,
        connection_id: client.tcp.connection_id(),
        heartbeat,
    };
    let old = {
        let mut state = state().lock().unwrap_or_else(|error| error.into_inner());
        state.completed = Some(Completed {
            config,
            initial,
            report: report.clone(),
        });
        state.warm.replace(client.tcp)
    };
    drop(old);
    log::info!(
        "TCP启动完成：{}，有效节点{}/{}，客户端{}，连接#{}",
        report.daily.date,
        report.daily.valid_nodes.len(),
        report.daily.results.len(),
        report.endpoint,
        report.connection_id,
    );
    Ok(report)
}

/// Binary entry point. RUSTDX_TCP_INITIAL_IPS replaces the default seed list
/// with comma-separated SocketAddr values (IPv6 uses `[address]:port`).
/// Unset means the existing editable default pool, initially the 40 built-in seeds.
pub fn initialize_from_env() -> io::Result<StartupReport> {
    match std::env::var("RUSTDX_TCP_INITIAL_IPS") {
        Ok(value) => {
            let addresses = value
                .split(',')
                .map(|value| {
                    value.trim().parse::<SocketAddr>().map_err(|error| {
                        io::Error::new(
                            io::ErrorKind::InvalidInput,
                            format!("RUSTDX_TCP_INITIAL_IPS地址无效: {error}"),
                        )
                    })
                })
                .collect::<io::Result<Vec<_>>>()?;
            if default_pool().list() != addresses {
                default_pool().replace(addresses)?;
            }
        }
        Err(std::env::VarError::NotPresent) => {}
        Err(error) => return Err(io::Error::new(io::ErrorKind::InvalidInput, error)),
    }
    initialize()
}

/// Latest successful initialization evidence; returns None if its endpoint or
/// daily measurement has expired. This does not reserve a client connection.
pub fn snapshot() -> Option<StartupReport> {
    let state = state().lock().unwrap_or_else(|error| error.into_inner());
    let completed = state.completed.as_ref()?;
    let pool = selected_pool(&completed.config);
    if !pool.contains_current(completed.report.endpoint)
        || pool.list() != completed.initial
        || pool.snapshot()?.tested_at != completed.report.daily.tested_at
    {
        return None;
    }
    Some(completed.report.clone())
}

/// Release an unclaimed warmed connection before spawning another process.
/// Measurement and the daily scheduler remain available.
pub fn release_client() {
    release_warm_client();
}

pub(super) fn take_warm_client(config: &TcpConfig, deadline: Instant) -> io::Result<Option<Tcp>> {
    remaining(deadline)?;
    let warm = {
        let mut state = state().lock().unwrap_or_else(|error| error.into_inner());
        if state
            .warm
            .as_ref()
            .is_some_and(|tcp| *tcp.config() == normalized(config))
        {
            state.warm.take()
        } else {
            None
        }
    };
    if let Some(mut tcp) = warm {
        if tcp.is_reusable() && tcp.with_deadline(deadline, |tcp| tcp.heartbeat()).is_ok() {
            tcp.config = config.clone();
            return Ok(Some(tcp));
        }
        drop(tcp);
        remaining(deadline)?;
    }
    Ok(None)
}

pub(super) fn release_warm_client() {
    if let Some(state) = STATE.get() {
        let warm = state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .warm
            .take();
        // Stop/join heartbeat and release the permit outside all state locks.
        drop(warm);
    }
}
