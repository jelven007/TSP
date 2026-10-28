//! 所有受管 Tcp（包括 Client 主连接及各连接池）共享的进程预算。
use std::{
    collections::HashMap,
    fs::{File, OpenOptions},
    io::{self, Read, Write},
    net::SocketAddr,
    path::PathBuf,
    sync::{Arc, Condvar, Mutex, MutexGuard, OnceLock, TryLockError},
    time::{Duration, Instant},
};

/// 在首次建立 Tcp 前调用 [`configure_network`] 可调整进程策略。
#[derive(Debug, Clone)]
pub struct NetworkPolicy {
    pub max_connections: usize,
    /// Shared files must remain in place. A crashed process releases OS locks.
    pub shared: Option<SharedNetworkPolicy>,
    pub acquire_timeout: Duration,
    pub connect_timeout: Duration,
    pub request_timeout: Duration,
    pub pagination_timeout: Duration,
    pub batch_timeout: Duration,
    pub endpoint_cooldown: Duration,
}

impl Default for NetworkPolicy {
    fn default() -> Self {
        Self {
            max_connections: 8,
            shared: None,
            acquire_timeout: Duration::from_secs(5),
            connect_timeout: Duration::from_secs(30),
            request_timeout: Duration::from_secs(15),
            pagination_timeout: Duration::from_secs(120),
            batch_timeout: Duration::from_secs(300),
            endpoint_cooldown: Duration::from_secs(10),
        }
    }
}

#[derive(Debug, Clone)]
pub struct SharedNetworkPolicy {
    pub directory: PathBuf,
    pub max_connections: usize,
}

#[derive(Debug, Clone, Default)]
pub struct EndpointStats {
    pub attempts: u64,
    pub failures: u64,
    pub cooldown_remaining: Duration,
}

#[derive(Debug, Clone)]
pub struct NetworkStats {
    pub active: usize,
    pub peak: usize,
    pub max_connections: usize,
    pub acquisition_timeouts: u64,
    pub requests: u64,
    pub request_failures: u64,
    pub queue_wait_micros: u64,
    pub request_p95_micros: u64,
    pub latency_samples: usize,
    pub empty_responses: u64,
    pub shared_max_connections: Option<usize>,
    pub endpoints: HashMap<SocketAddr, EndpointStats>,
}

#[derive(Debug, Default)]
struct Endpoint {
    attempts: u64,
    failures: u64,
    cooling_until: Option<Instant>,
}

#[derive(Debug, Default)]
struct State {
    active: usize,
    peak: usize,
    cursor: usize,
    acquisition_timeouts: u64,
    requests: u64,
    request_failures: u64,
    queue_wait_micros: u64,
    latencies: std::collections::VecDeque<u64>,
    empty_responses: u64,
    endpoints: HashMap<SocketAddr, Endpoint>,
}

#[derive(Debug)]
pub(crate) struct Governor {
    pub policy: NetworkPolicy,
    state: Mutex<State>,
    available: Condvar,
}

static GLOBAL: OnceLock<Arc<Governor>> = OnceLock::new();

/// 仅允许在进程首次使用网络预算前设置一次；已初始化时返回 AlreadyExists。
pub fn configure_network(policy: NetworkPolicy) -> io::Result<()> {
    if policy.max_connections == 0
        || [
            policy.acquire_timeout,
            policy.connect_timeout,
            policy.request_timeout,
            policy.pagination_timeout,
            policy.batch_timeout,
        ]
        .iter()
        .any(Duration::is_zero)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "网络预算必须大于0",
        ));
    }
    for timeout in [
        policy.acquire_timeout,
        policy.connect_timeout,
        policy.request_timeout,
        policy.pagination_timeout,
        policy.batch_timeout,
        policy.endpoint_cooldown,
    ] {
        deadline_after(timeout)?;
    }
    if let Some(shared) = &policy.shared {
        validate_shared(shared)?;
    }
    GLOBAL
        .set(Arc::new(Governor::new(policy)))
        .map_err(|_| io::Error::new(io::ErrorKind::AlreadyExists, "网络策略已初始化"))
}

pub fn network_policy() -> &'static NetworkPolicy {
    &global().policy
}

pub fn network_stats() -> NetworkStats {
    global().stats()
}

pub(crate) fn global() -> &'static Arc<Governor> {
    GLOBAL.get_or_init(|| Arc::new(Governor::new(NetworkPolicy::default())))
}

pub(crate) fn deadline_after(timeout: Duration) -> io::Result<Instant> {
    let timeout = crate::operation::timeout(timeout)?;
    Instant::now()
        .checked_add(timeout)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "超时时间过大"))
}

pub(crate) fn remaining(deadline: Instant) -> io::Result<Duration> {
    let duration = deadline.saturating_duration_since(Instant::now());
    if duration.is_zero() {
        Err(io::Error::new(
            io::ErrorKind::TimedOut,
            "操作总体时限已耗尽",
        ))
    } else {
        crate::operation::timeout(duration)
    }
}

/// Waiting for initialization must obey the same deadline and cancellation as I/O.
pub(crate) fn lock_until<T>(mutex: &Mutex<T>, deadline: Instant) -> io::Result<MutexGuard<'_, T>> {
    loop {
        let wait = remaining(deadline)?;
        match mutex.try_lock() {
            Ok(guard) => return Ok(guard),
            Err(TryLockError::Poisoned(error)) => return Ok(error.into_inner()),
            Err(TryLockError::WouldBlock) => {
                crate::operation::sleep(wait.min(Duration::from_millis(10)))?;
            }
        }
    }
}

impl Governor {
    fn new(policy: NetworkPolicy) -> Self {
        Self {
            policy,
            state: Mutex::new(State::default()),
            available: Condvar::new(),
        }
    }

    pub fn acquire(self: &Arc<Self>, deadline: Instant) -> io::Result<Permit> {
        let started = Instant::now();
        let deadline = deadline.min(deadline_after(self.policy.acquire_timeout)?);
        // A parked startup client yields its permit to unrelated TCP/HTTP work.
        // Drop outside the governor lock: Permit::drop locks this same state.
        super::startup::release_warm_client();
        let mut state = self.state.lock().unwrap();
        loop {
            let wait = match remaining(deadline) {
                Ok(wait) => wait,
                Err(error) => {
                    state.queue_wait_micros += started.elapsed().as_micros() as u64;
                    state.acquisition_timeouts +=
                        u64::from(error.kind() == io::ErrorKind::TimedOut);
                    return Err(error);
                }
            };
            if state.active < self.policy.max_connections {
                let shared = self.try_shared()?;
                if self.policy.shared.is_none() || shared.is_some() {
                    state.active += 1;
                    state.peak = state.peak.max(state.active);
                    state.queue_wait_micros += started.elapsed().as_micros() as u64;
                    return Ok(Permit {
                        governor: self.clone(),
                        _shared: shared,
                    });
                }
            }
            state = self
                .available
                .wait_timeout(state, wait.min(Duration::from_millis(20)))
                .unwrap()
                .0;
            // An initializer can publish a parked client while this waiter is
            // asleep. Yield that newly parked permit without holding our lock.
            drop(state);
            super::startup::release_warm_client();
            state = self.state.lock().unwrap();
        }
    }

    fn try_shared(&self) -> io::Result<Option<File>> {
        let Some(shared) = &self.policy.shared else {
            return Ok(None);
        };
        for i in 0..shared.max_connections {
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(shared.directory.join(format!("permit-{i}.lock")))?;
            match file.try_lock() {
                Ok(()) => return Ok(Some(file)),
                Err(std::fs::TryLockError::WouldBlock) => continue,
                Err(std::fs::TryLockError::Error(e)) => return Err(e),
            }
        }
        Ok(None)
    }

    pub fn candidates(&self, addresses: &[SocketAddr]) -> Vec<SocketAddr> {
        let mut state = self.state.lock().unwrap();
        let start = state.cursor;
        state.cursor = state.cursor.wrapping_add(1);
        (0..addresses.len())
            .map(|offset| addresses[(start.wrapping_add(offset)) % addresses.len()])
            .filter(|address| {
                state
                    .endpoints
                    .get(address)
                    .and_then(|endpoint| endpoint.cooling_until)
                    .is_none_or(|until| until <= Instant::now())
            })
            .collect()
    }

    pub fn endpoint_result(&self, address: SocketAddr, success: bool) {
        let mut state = self.state.lock().unwrap();
        let endpoint = state.endpoints.entry(address).or_default();
        endpoint.attempts += 1;
        if success {
            endpoint.cooling_until = None;
        } else {
            endpoint.failures += 1;
            endpoint.cooling_until = Instant::now().checked_add(self.policy.endpoint_cooldown);
        }
    }

    pub fn request_result(&self, success: bool) {
        let mut state = self.state.lock().unwrap();
        state.requests += 1;
        state.request_failures += u64::from(!success);
    }

    pub fn observation(&self, elapsed: Duration, empty: bool) {
        let mut state = self.state.lock().unwrap();
        if state.latencies.len() == 1024 {
            state.latencies.pop_front();
        }
        state.latencies.push_back(elapsed.as_micros() as u64);
        state.empty_responses += u64::from(empty);
    }

    fn stats(&self) -> NetworkStats {
        let state = self.state.lock().unwrap();
        let mut latencies: Vec<_> = state.latencies.iter().copied().collect();
        latencies.sort_unstable();
        let p95 = latencies
            .get(
                latencies
                    .len()
                    .saturating_mul(95)
                    .div_ceil(100)
                    .saturating_sub(1),
            )
            .copied()
            .unwrap_or(0);
        NetworkStats {
            active: state.active,
            peak: state.peak,
            max_connections: self.policy.max_connections,
            acquisition_timeouts: state.acquisition_timeouts,
            requests: state.requests,
            request_failures: state.request_failures,
            queue_wait_micros: state.queue_wait_micros,
            request_p95_micros: p95,
            latency_samples: latencies.len(),
            empty_responses: state.empty_responses,
            shared_max_connections: self.policy.shared.as_ref().map(|p| p.max_connections),
            endpoints: state
                .endpoints
                .iter()
                .map(|(&address, endpoint)| {
                    (
                        address,
                        EndpointStats {
                            attempts: endpoint.attempts,
                            failures: endpoint.failures,
                            cooldown_remaining: endpoint
                                .cooling_until
                                .map_or(Duration::ZERO, |until| {
                                    until.saturating_duration_since(Instant::now())
                                }),
                        },
                    )
                })
                .collect(),
        }
    }
}

/// 先关闭 Tcp 的两个句柄再释放预算；重连在原许可内进行。
#[derive(Debug)]
pub struct Permit {
    governor: Arc<Governor>,
    _shared: Option<File>,
}

impl Drop for Permit {
    fn drop(&mut self) {
        // Close the shared lock before waking a process-local waiter.
        self._shared.take();
        let mut state = self.governor.state.lock().unwrap();
        state.active -= 1;
        self.governor.available.notify_one();
    }
}

/// Reserve a connection for HTTP or another synchronous network transport.
pub fn acquire_network(timeout: Duration) -> io::Result<Permit> {
    global().acquire(deadline_after(timeout)?)
}

/// Record one complete HTTP operation. TCP records its own frames.
pub fn observe_request(elapsed: Duration, success: bool, empty: bool) {
    global().request_result(success);
    global().observation(elapsed, empty);
}

/// Count a successfully decoded empty business page without adding a second
/// HTTP request or an artificial zero-duration latency sample.
pub fn observe_empty_response() {
    global().state.lock().unwrap().empty_responses += 1;
}

pub(crate) fn validate_shared(policy: &SharedNetworkPolicy) -> io::Result<()> {
    validate_shared_until(policy, deadline_after(Duration::from_secs(5))?)
}

pub(crate) fn validate_shared_until(
    policy: &SharedNetworkPolicy,
    deadline: Instant,
) -> io::Result<()> {
    if policy.max_connections == 0 || policy.max_connections > 1024 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "共享连接数必须为1–1024",
        ));
    }
    std::fs::create_dir_all(&policy.directory)?;
    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(policy.directory.join("capacity.lock"))?;
    // Only policy initialization is serialized here; slots are independent.
    loop {
        match file.try_lock() {
            Ok(()) => break,
            Err(std::fs::TryLockError::WouldBlock) => {
                remaining(deadline)?;
                crate::operation::sleep(Duration::from_millis(20))?;
            }
            Err(std::fs::TryLockError::Error(e)) => return Err(e),
        }
    }
    let mut capacity = String::new();
    file.read_to_string(&mut capacity)?;
    if capacity.is_empty() {
        write!(file, "{}", policy.max_connections)?;
        file.sync_all()?;
    } else if capacity.trim() != policy.max_connections.to_string() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "共享许可目录的连接数不一致，请使用相同配置或独立许可目录",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn observations_use_a_bounded_nearest_rank_window_and_shared_policy_agrees() {
        let governor = Governor::new(NetworkPolicy::default());
        for i in 1..=2000 {
            governor.observation(Duration::from_micros(i), i == 2000);
        }
        let stats = governor.stats();
        assert_eq!(
            (
                stats.latency_samples,
                stats.request_p95_micros,
                stats.empty_responses
            ),
            (1024, 1949, 1)
        );
        let dir = std::env::temp_dir().join(format!("rustdx-capacity-test-{}", std::process::id()));
        let policy = SharedNetworkPolicy {
            directory: dir.clone(),
            max_connections: 2,
        };
        validate_shared(&policy).unwrap();
        validate_shared(&policy).unwrap();
        assert!(
            validate_shared(&SharedNetworkPolicy {
                max_connections: 3,
                ..policy
            })
            .is_err()
        );
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn bounded_budget_releases_and_wakes_waiters() {
        let governor = Arc::new(Governor::new(NetworkPolicy {
            max_connections: 1,
            ..Default::default()
        }));
        let permit = governor
            .acquire(deadline_after(Duration::from_secs(1)).unwrap())
            .unwrap();
        assert_eq!(
            governor
                .acquire(deadline_after(Duration::from_millis(5)).unwrap())
                .unwrap_err()
                .kind(),
            io::ErrorKind::TimedOut
        );
        std::thread::scope(|scope| {
            let governor = &governor;
            let waiter = scope.spawn(|| {
                governor
                    .acquire(deadline_after(Duration::from_secs(1)).unwrap())
                    .unwrap()
            });
            drop(permit);
            drop(waiter.join().unwrap());
        });
        let stats = governor.stats();
        assert_eq!(
            (stats.active, stats.peak, stats.acquisition_timeouts),
            (0, 1, 1)
        );
    }

    #[test]
    fn endpoint_rotation_and_failure_cooldown() {
        let governor = Governor::new(NetworkPolicy::default());
        let addresses = [
            "127.0.0.1:1".parse().unwrap(),
            "127.0.0.1:2".parse().unwrap(),
        ];
        assert_eq!(governor.candidates(&addresses), addresses);
        assert_eq!(
            governor.candidates(&addresses),
            [addresses[1], addresses[0]]
        );
        governor.endpoint_result(addresses[0], false);
        assert_eq!(governor.candidates(&addresses), [addresses[1]]);
        governor.endpoint_result(addresses[0], true);
        assert_eq!(governor.candidates(&addresses).len(), 2);
        assert_eq!(governor.stats().endpoints[&addresses[0]].failures, 1);
    }
}
