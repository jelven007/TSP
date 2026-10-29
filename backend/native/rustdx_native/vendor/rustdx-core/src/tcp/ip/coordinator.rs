//! One separate probe quota for all pools and diagnostics in a process.
use crate::tcp::governance::{deadline_after, remaining};
use std::{
    collections::HashMap,
    fs::{File, OpenOptions},
    io,
    net::SocketAddr,
    sync::{Arc, Condvar, Mutex, OnceLock},
    time::{Duration, Instant},
};

/// Immutable quota identity shared by all pools assigned to a task.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ProbeGroup {
    Legacy,
    Realtime,
    Minute,
    History,
}

impl ProbeGroup {
    pub fn name(self) -> &'static str {
        match self {
            Self::Legacy => "legacy",
            Self::Realtime => "realtime",
            Self::Minute => "minute",
            Self::History => "history",
        }
    }
}

#[derive(Debug, Clone)]
pub struct ProbePolicy {
    pub max_connections: usize,
    /// Separate slots from business connections. All participants must agree.
    pub shared: Option<crate::tcp::SharedNetworkPolicy>,
    pub acquire_timeout: Duration,
}

impl Default for ProbePolicy {
    fn default() -> Self {
        Self {
            max_connections: super::DAILY_PROBE_WORKERS,
            shared: None,
            acquire_timeout: Duration::from_secs(30),
        }
    }
}

#[derive(Debug, Clone, Copy, Default)]
pub struct ProbeStats {
    pub active: usize,
    pub peak: usize,
    pub waits: u64,
    pub timeouts: u64,
}

#[derive(Debug)]
struct Coordinator {
    policy: ProbePolicy,
    state: Mutex<ProbeStats>,
    validated: Mutex<bool>,
    available: Condvar,
    scoped: Mutex<HashMap<String, Arc<crate::tcp::governance::Governor>>>,
}

static COORDINATOR: OnceLock<Arc<Coordinator>> = OnceLock::new();

/// Configure before the first probe. No sockets are opened by this function.
pub fn configure_probes(policy: ProbePolicy) -> io::Result<()> {
    validate(&policy)?;
    COORDINATOR
        .set(Arc::new(Coordinator::new(policy)))
        .map_err(|_| io::Error::new(io::ErrorKind::AlreadyExists, "探测策略已初始化"))
}

fn validate(policy: &ProbePolicy) -> io::Result<()> {
    if !(1..=1024).contains(&policy.max_connections) || policy.acquire_timeout.is_zero() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "探测配额须为1..1024且等待时限须为正",
        ));
    }
    deadline_after(policy.acquire_timeout)?;
    if let Some(shared) = &policy.shared {
        crate::tcp::governance::validate_shared(shared)?;
    }
    Ok(())
}

fn global() -> &'static Arc<Coordinator> {
    COORDINATOR.get_or_init(|| {
        // Applications using shared business governance automatically coordinate
        // probes in its own directory. Explicit configure_probes overrides this.
        let shared = crate::tcp::network_policy().shared.as_ref().map(|shared| {
            crate::tcp::SharedNetworkPolicy {
                directory: shared.directory.join("probes"),
                max_connections: super::DAILY_PROBE_WORKERS,
            }
        });
        Arc::new(Coordinator::new(ProbePolicy {
            shared,
            ..Default::default()
        }))
    })
}

pub fn probe_stats() -> ProbeStats {
    *global().state.lock().unwrap_or_else(|e| e.into_inner())
}

pub(super) fn workers() -> usize {
    let policy = &global().policy;
    policy
        .max_connections
        .min(
            policy
                .shared
                .as_ref()
                .map_or(super::DAILY_PROBE_WORKERS, |shared| shared.max_connections),
        )
        .min(super::DAILY_PROBE_WORKERS)
}

pub(super) fn workers_for(group: Option<ProbeGroup>) -> usize {
    if group.is_some() { 1 } else { workers() }
}

pub(super) fn acquire_at(
    deadline: Option<Instant>,
    group: Option<ProbeGroup>,
    address: SocketAddr,
) -> io::Result<ProbePermit> {
    let coordinator = global();
    let local = deadline_after(coordinator.policy.acquire_timeout)?;
    let deadline = deadline.map_or(local, |deadline| deadline.min(local));
    // Group precedes endpoint, which precedes total; no waiter retains the
    // scarce total quota while waiting behind probes of its own task.
    let mut scoped = Vec::new();
    if let Some(group) = group {
        scoped.push(
            coordinator
                .scope(format!("group-{}", group.name()), deadline)?
                .acquire(deadline)?,
        );
    }
    let encoded: String = address
        .to_string()
        .bytes()
        .map(|b| format!("{b:02x}"))
        .collect();
    scoped.push(
        coordinator
            .scope(format!("endpoint-{encoded}"), deadline)?
            .acquire(deadline)?,
    );
    let mut permit = coordinator.acquire(deadline)?;
    permit.scoped = scoped;
    Ok(permit)
}

impl Coordinator {
    fn new(policy: ProbePolicy) -> Self {
        Self {
            policy,
            state: Mutex::new(ProbeStats::default()),
            validated: Mutex::new(false),
            available: Condvar::new(),
            scoped: Mutex::new(HashMap::new()),
        }
    }

    fn scope(
        &self,
        key: String,
        deadline: Instant,
    ) -> io::Result<Arc<crate::tcp::governance::Governor>> {
        let mut scoped = crate::tcp::governance::lock_until(&self.scoped, deadline)?;
        if let Some(governor) = scoped.get(&key) {
            return Ok(Arc::clone(governor));
        }
        let shared = self
            .policy
            .shared
            .as_ref()
            .map(|shared| crate::tcp::SharedNetworkPolicy {
                directory: shared.directory.join("scopes").join(&key),
                max_connections: 1,
            });
        if let Some(shared) = &shared {
            crate::tcp::governance::validate_shared_until(shared, deadline)?;
        }
        let governor = Arc::new(crate::tcp::governance::Governor::new(
            crate::tcp::NetworkPolicy {
                max_connections: 1,
                acquire_timeout: self.policy.acquire_timeout,
                shared,
                ..Default::default()
            },
        ));
        scoped.insert(key, Arc::clone(&governor));
        Ok(governor)
    }

    fn acquire(self: &Arc<Self>, deadline: Instant) -> io::Result<ProbePermit> {
        // Default inherited shared policy is lazily validated, without hiding I/O failures.
        if let Some(shared) = &self.policy.shared {
            let mut validated = crate::tcp::governance::lock_until(&self.validated, deadline)?;
            if !*validated {
                crate::tcp::governance::validate_shared_until(shared, deadline)?;
                *validated = true;
            }
        }
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let mut waited = false;
        loop {
            let wait = match remaining(deadline) {
                Ok(wait) => wait,
                Err(error) => {
                    state.timeouts += u64::from(error.kind() == io::ErrorKind::TimedOut);
                    return Err(error);
                }
            };
            if state.active < self.policy.max_connections {
                let mut file = None;
                if let Some(shared) = &self.policy.shared {
                    for index in 0..shared.max_connections {
                        let slot = OpenOptions::new()
                            .read(true)
                            .write(true)
                            .create(true)
                            .truncate(false)
                            .open(shared.directory.join(format!("permit-{index}.lock")))?;
                        match slot.try_lock() {
                            Ok(()) => {
                                file = Some(slot);
                                break;
                            }
                            Err(std::fs::TryLockError::WouldBlock) => {}
                            Err(std::fs::TryLockError::Error(error)) => return Err(error),
                        }
                    }
                }
                if self.policy.shared.is_none() || file.is_some() {
                    state.active += 1;
                    state.peak = state.peak.max(state.active);
                    return Ok(ProbePermit {
                        coordinator: self.clone(),
                        shared: file,
                        scoped: Vec::new(),
                    });
                }
            }
            if !waited {
                state.waits += 1;
                waited = true;
            }
            state = self
                .available
                .wait_timeout(state, wait.min(Duration::from_millis(20)))
                .unwrap_or_else(|e| e.into_inner())
                .0;
        }
    }
}

pub(super) struct ProbePermit {
    coordinator: Arc<Coordinator>,
    shared: Option<File>,
    scoped: Vec<crate::tcp::governance::Permit>,
}

impl Drop for ProbePermit {
    fn drop(&mut self) {
        self.shared.take();
        self.scoped.clear();
        let mut state = self
            .coordinator
            .state
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        state.active -= 1;
        self.coordinator.available.notify_one();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn independent_pools_share_slots_and_waiting_inherits_cancellation() {
        let path = std::env::temp_dir().join(format!("rustdx-probe-quota-{}", std::process::id()));
        let policy = ProbePolicy {
            max_connections: 1,
            shared: Some(crate::tcp::SharedNetworkPolicy {
                directory: path.clone(),
                max_connections: 1,
            }),
            ..Default::default()
        };
        validate(&policy).unwrap();
        let first = Arc::new(Coordinator::new(policy.clone()));
        let second = Arc::new(Coordinator::new(policy));
        let permit = first
            .acquire(deadline_after(Duration::from_secs(1)).unwrap())
            .unwrap();
        assert!(
            matches!(second.acquire(deadline_after(Duration::from_millis(10)).unwrap()), Err(e) if e.kind() == io::ErrorKind::TimedOut)
        );
        let context =
            crate::operation::OperationContext::new(Duration::from_secs(1), None).unwrap();
        let guard = context.enter();
        context.cancel();
        assert!(
            matches!(second.acquire(Instant::now() + Duration::from_secs(1)), Err(e) if e.kind() == io::ErrorKind::Interrupted)
        );
        drop(guard);
        drop(permit);
        drop(
            second
                .acquire(deadline_after(Duration::from_secs(1)).unwrap())
                .unwrap(),
        );
        assert_eq!(first.state.lock().unwrap().peak, 1);
        assert_eq!(second.state.lock().unwrap().active, 0);
        std::fs::remove_dir_all(path).unwrap();
    }
}
