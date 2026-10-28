//! Passive command observations, separate from daily protocol liveness.
//! A decoded response proves only that this command answered on this connection;
//! it cannot qualify a trading date, freshness, full coverage or active push.
use chrono::{DateTime, Utc};
use serde::Serialize;
use std::{
    collections::BTreeMap,
    net::SocketAddr,
    sync::{Mutex, OnceLock},
    time::Duration,
};

const MAX_OBSERVATIONS: usize = 4096;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum CapabilityStatus {
    TransportValidated,
    DecodedResponse,
    TransportFailed,
}

#[derive(Debug, Clone, Serialize)]
pub struct CapabilityObservation {
    pub endpoint: SocketAddr,
    pub opcode: u16,
    pub connection_id: u64,
    pub status: CapabilityStatus,
    pub observed_at: DateTime<Utc>,
    pub transport_successes: u64,
    pub transport_failures: u64,
    pub decoded_responses: u64,
}

#[derive(Default)]
struct Registry {
    observations: BTreeMap<(SocketAddr, u16), CapabilityObservation>,
}

static REGISTRY: OnceLock<Mutex<Registry>> = OnceLock::new();

fn registry() -> &'static Mutex<Registry> {
    REGISTRY.get_or_init(Default::default)
}

pub(crate) fn observe(
    endpoint: SocketAddr,
    opcode: u16,
    connection_id: u64,
    status: CapabilityStatus,
) {
    registry()
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .observe(endpoint, opcode, connection_id, status);
}

impl Registry {
    fn observe(
        &mut self,
        endpoint: SocketAddr,
        opcode: u16,
        connection_id: u64,
        status: CapabilityStatus,
    ) {
        let key = (endpoint, opcode);
        if self.observations.len() == MAX_OBSERVATIONS
            && !self.observations.contains_key(&key)
            && let Some(oldest) = self
                .observations
                .iter()
                .min_by_key(|(_, v)| v.observed_at)
                .map(|(k, _)| *k)
        {
            self.observations.remove(&oldest);
        }
        let entry = self
            .observations
            .entry(key)
            .or_insert(CapabilityObservation {
                endpoint,
                opcode,
                connection_id,
                status,
                observed_at: Utc::now(),
                transport_successes: 0,
                transport_failures: 0,
                decoded_responses: 0,
            });
        entry.connection_id = connection_id;
        entry.status = status;
        entry.observed_at = Utc::now();
        match status {
            CapabilityStatus::TransportValidated => {
                entry.transport_successes = entry.transport_successes.saturating_add(1)
            }
            CapabilityStatus::TransportFailed => {
                entry.transport_failures = entry.transport_failures.saturating_add(1)
            }
            CapabilityStatus::DecodedResponse => {
                entry.decoded_responses = entry.decoded_responses.saturating_add(1)
            }
        }
    }
}

pub fn capability_observation(endpoint: SocketAddr, opcode: u16) -> Option<CapabilityObservation> {
    registry()
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .observations
        .get(&(endpoint, opcode))
        .cloned()
}

impl super::IpPool {
    /// Current daily nodes with a latest decoded response for this exact opcode.
    /// No observation, expiry, a subsequent transport error or an undecoded
    /// exchange excludes a node. Does not issue network requests or fall back.
    /// Caller chooses a sample request and validates its business qualification.
    pub fn capable_nodes(&self, opcode: u16, max_age: Duration) -> Vec<SocketAddr> {
        let Some(daily) = self.snapshot() else {
            return Vec::new();
        };
        let now = Utc::now();
        let registry = registry().lock().unwrap_or_else(|e| e.into_inner());
        daily
            .valid_nodes
            .into_iter()
            .filter(|address| {
                registry
                    .observations
                    .get(&(*address, opcode))
                    .is_some_and(|entry| {
                        self.contains_current(*address)
                            && entry.status == CapabilityStatus::DecodedResponse
                            && !max_age.is_zero()
                            && now
                                .signed_duration_since(entry.observed_at)
                                .to_std()
                                .is_ok_and(|age| age <= max_age)
                    })
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heartbeat_does_not_qualify_special_commands_and_failure_revokes_observation() {
        let endpoint = "127.0.0.1:17999".parse().unwrap();
        let pool = super::super::IpPool::for_test(endpoint);
        observe(endpoint, 0x044e, 1, CapabilityStatus::DecodedResponse);
        assert!(
            pool.capable_nodes(0x0ffc, Duration::from_secs(60))
                .is_empty()
        );
        observe(endpoint, 0x0ffc, 1, CapabilityStatus::TransportValidated);
        assert!(
            pool.capable_nodes(0x0ffc, Duration::from_secs(60))
                .is_empty()
        );
        observe(endpoint, 0x0ffc, 1, CapabilityStatus::DecodedResponse);
        assert_eq!(
            pool.capable_nodes(0x0ffc, Duration::from_secs(60)),
            [endpoint]
        );
        assert!(pool.capable_nodes(0x0ffc, Duration::ZERO).is_empty());
        observe(endpoint, 0x0ffc, 2, CapabilityStatus::TransportFailed);
        assert!(
            pool.capable_nodes(0x0ffc, Duration::from_secs(60))
                .is_empty()
        );
        let observation = capability_observation(endpoint, 0x0ffc).unwrap();
        assert_eq!(
            (
                observation.transport_successes,
                observation.transport_failures,
                observation.decoded_responses
            ),
            (1, 1, 1)
        );
    }
}
