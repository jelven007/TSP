//! A permit lives as long as its business TCP, across all native pools.
use std::{
    collections::{HashMap, HashSet},
    fs::{File, OpenOptions},
    io::{Read, Write},
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::{Arc, Condvar, Mutex, OnceLock},
    time::{Duration, Instant},
};

#[derive(Default)]
struct State {
    active: HashMap<String, usize>,
    peak: HashMap<String, usize>,
    validated: HashSet<String>,
}

#[derive(Default)]
struct Quota {
    state: Mutex<State>,
    available: Condvar,
}

static QUOTA: OnceLock<Arc<Quota>> = OnceLock::new();

pub struct Permit {
    quota: Arc<Quota>,
    keys: [String; 2],
    files: Vec<File>,
}

pub fn validate_group(group: &str) -> Result<(), String> {
    if matches!(group, "legacy" | "realtime" | "minute" | "history") {
        Ok(())
    } else {
        Err("[InvalidInput] unknown rustdx task group".into())
    }
}

fn endpoint_key(endpoint: SocketAddr) -> String {
    let encoded: String = endpoint
        .to_string()
        .bytes()
        .map(|b| format!("{b:02x}"))
        .collect();
    format!("endpoint-{encoded}")
}

fn lock_file(path: &Path) -> Result<File, String> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)
        .map_err(super::io_error)
}

fn validate_capacity(path: &Path, capacity: usize, deadline: Instant) -> Result<(), String> {
    std::fs::create_dir_all(path).map_err(super::io_error)?;
    let mut file = lock_file(&path.join("capacity.lock"))?;
    loop {
        rustdx_complete::operation::check().map_err(super::io_error)?;
        match file.try_lock() {
            Ok(()) => break,
            Err(std::fs::TryLockError::WouldBlock) if Instant::now() < deadline => {
                rustdx_complete::operation::sleep(Duration::from_millis(20))
                    .map_err(super::io_error)?;
            }
            Err(std::fs::TryLockError::WouldBlock) => {
                return Err("[TimedOut] quota initialization".into());
            }
            Err(std::fs::TryLockError::Error(error)) => return Err(super::io_error(error)),
        }
    }
    let mut configured = String::new();
    file.read_to_string(&mut configured)
        .map_err(super::io_error)?;
    if configured.is_empty() {
        write!(file, "{capacity}").map_err(super::io_error)?;
        file.sync_all().map_err(super::io_error)?;
    } else if configured.trim() != capacity.to_string() {
        return Err(
            "[InvalidInput] shared rustdx quota capacity differs; restart with the same protocol"
                .into(),
        );
    }
    Ok(())
}

pub fn acquire(group: &str, endpoint: SocketAddr) -> Result<Permit, String> {
    validate_group(group)?;
    let quota = QUOTA.get_or_init(|| Arc::new(Quota::default()));
    let keys = [format!("group-{group}"), endpoint_key(endpoint)];
    let directory = std::env::var_os("RUSTDX_NETWORK_SHARED_DIR")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from);
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut state = super::operation_lock(&quota.state)?;
    if let Some(directory) = &directory {
        for key in &keys {
            if !state.validated.contains(key) {
                validate_capacity(
                    &directory.join("business").join(key),
                    super::MAX_CONNECTIONS,
                    deadline,
                )?;
                state.validated.insert(key.clone());
            }
        }
    }
    loop {
        rustdx_complete::operation::check().map_err(super::io_error)?;
        if Instant::now() >= deadline {
            return Err("[Quota] rustdx task/endpoint quota exhausted".into());
        }
        if keys
            .iter()
            .all(|key| state.active.get(key).copied().unwrap_or(0) < super::MAX_CONNECTIONS)
        {
            let mut files = Vec::new();
            if let Some(directory) = &directory {
                for key in &keys {
                    let mut acquired = None;
                    for index in 0..super::MAX_CONNECTIONS {
                        let file = lock_file(
                            &directory
                                .join("business")
                                .join(key)
                                .join(format!("permit-{index}.lock")),
                        )?;
                        match file.try_lock() {
                            Ok(()) => {
                                acquired = Some(file);
                                break;
                            }
                            Err(std::fs::TryLockError::WouldBlock) => {}
                            Err(std::fs::TryLockError::Error(error)) => {
                                return Err(super::io_error(error));
                            }
                        }
                    }
                    if let Some(file) = acquired {
                        files.push(file);
                    } else {
                        break;
                    }
                }
            }
            if directory.is_none() || files.len() == keys.len() {
                for key in &keys {
                    let active = state.active.entry(key.clone()).or_default();
                    *active += 1;
                    let active = *active;
                    let peak = state.peak.entry(key.clone()).or_default();
                    *peak = (*peak).max(active);
                }
                return Ok(Permit {
                    quota: Arc::clone(quota),
                    keys,
                    files,
                });
            }
            // Never wait while retaining only part of the shared dimensions.
            drop(files);
        }
        state = quota
            .available
            .wait_timeout(state, Duration::from_millis(20))
            .map_err(|error| error.to_string())?
            .0;
    }
}

pub fn stats(group: &str) -> serde_json::Value {
    let quota = QUOTA.get_or_init(|| Arc::new(Quota::default()));
    let state = quota
        .state
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    let key = format!("group-{group}");
    serde_json::json!({
        "group": group,
        "limit": super::MAX_CONNECTIONS,
        "active": state.active.get(&key).copied().unwrap_or(0),
        "peak": state.peak.get(&key).copied().unwrap_or(0),
        "endpoints": state.active.iter().filter(|(key, _)| key.starts_with("endpoint-"))
            .map(|(key, active)| (key.clone(), *active)).collect::<HashMap<_, _>>(),
    })
}

impl Drop for Permit {
    fn drop(&mut self) {
        self.files.clear();
        let mut state = self
            .quota
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        for key in &self.keys {
            *state.active.get_mut(key).expect("live quota permit") -= 1;
        }
        self.quota.available.notify_all();
    }
}
