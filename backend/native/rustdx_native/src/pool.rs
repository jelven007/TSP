use std::sync::{Condvar, Mutex};
use std::time::Instant;

struct State {
    busy: Vec<bool>,
    background_active: usize,
    background_peak: usize,
    foreground_waiting: usize,
    waiting: usize,
    acquisitions: u64,
    wait_us: u64,
    max_wait_us: u64,
    closed: bool,
}

pub struct Scheduler {
    state: Mutex<State>,
    changed: Condvar,
    pub background_limit: usize,
}

pub struct Lease<'a> {
    scheduler: &'a Scheduler,
    pub index: usize,
    background: bool,
}

impl Drop for Lease<'_> {
    fn drop(&mut self) {
        let mut state = self.scheduler.state.lock().unwrap();
        state.busy[self.index] = false;
        if self.background {
            state.background_active -= 1;
        }
        self.scheduler.changed.notify_all();
    }
}

impl Scheduler {
    pub fn new(size: usize, background_limit: usize) -> Self {
        Self {
            state: Mutex::new(State {
                busy: vec![false; size],
                background_active: 0,
                background_peak: 0,
                foreground_waiting: 0,
                waiting: 0,
                acquisitions: 0,
                wait_us: 0,
                max_wait_us: 0,
                closed: false,
            }),
            changed: Condvar::new(),
            background_limit: background_limit.clamp(1, size),
        }
    }

    pub fn acquire(&self, preferred: usize, background: bool) -> Result<Lease<'_>, String> {
        let started = Instant::now();
        let mut state = self.state.lock().map_err(|error| error.to_string())?;
        state.waiting += 1;
        if !background {
            state.foreground_waiting += 1;
        }
        loop {
            if state.closed {
                state.waiting -= 1;
                if !background {
                    state.foreground_waiting -= 1;
                }
                return Err("rustdx connection pool is closed".into());
            }
            let allowed = !background
                || (state.foreground_waiting == 0
                    && state.background_active < self.background_limit);
            let size = state.busy.len();
            let idle = (0..size)
                .map(|offset| (preferred + offset) % size)
                .find(|index| !state.busy[*index]);
            if let Some(index) = idle.filter(|_| allowed) {
                state.waiting -= 1;
                state.busy[index] = true;
                if background {
                    state.background_active += 1;
                    state.background_peak = state.background_peak.max(state.background_active);
                } else {
                    state.foreground_waiting -= 1;
                }
                let elapsed = started.elapsed().as_micros().min(u64::MAX as u128) as u64;
                state.acquisitions += 1;
                state.wait_us = state.wait_us.saturating_add(elapsed);
                state.max_wait_us = state.max_wait_us.max(elapsed);
                return Ok(Lease {
                    scheduler: self,
                    index,
                    background,
                });
            }
            state = self
                .changed
                .wait(state)
                .map_err(|error| error.to_string())?;
        }
    }

    pub fn close(&self) {
        let mut state = self.state.lock().unwrap();
        state.closed = true;
        self.changed.notify_all();
    }

    pub fn stats(&self) -> serde_json::Value {
        let state = self.state.lock().unwrap();
        serde_json::json!({
            "background_limit": self.background_limit,
            "background_active": state.background_active,
            "background_peak": state.background_peak,
            "waiting_requests": state.waiting,
            "foreground_waiting": state.foreground_waiting,
            "slot_acquisitions": state.acquisitions,
            "slot_wait_ms_total": state.wait_us as f64 / 1000.0,
            "slot_wait_ms_max": state.max_wait_us as f64 / 1000.0,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::Scheduler;
    use std::sync::mpsc;
    use std::time::Duration;

    #[test]
    fn busy_preferred_slot_does_not_block_an_idle_slot() {
        let pool = Scheduler::new(2, 1);
        let first = pool.acquire(0, true).unwrap();
        let second = pool.acquire(0, false).unwrap();
        assert_eq!(first.index, 0);
        assert_eq!(second.index, 1);
    }

    #[test]
    fn foreground_precedes_waiting_background_and_reuses_slots() {
        let pool = Scheduler::new(1, 1);
        let occupied = pool.acquire(0, true).unwrap();
        let (sender, receiver) = mpsc::channel();
        std::thread::scope(|scope| {
            let sender_bg = sender.clone();
            let pool_ref = &pool;
            scope.spawn(move || {
                let _lease = pool_ref.acquire(0, true).unwrap();
                sender_bg.send("background").unwrap();
            });
            scope.spawn(|| {
                let _lease = pool.acquire(0, false).unwrap();
                sender.send("foreground").unwrap();
            });
            let deadline = std::time::Instant::now() + Duration::from_secs(2);
            while pool.stats()["waiting_requests"] != 2 {
                assert!(std::time::Instant::now() < deadline);
                std::thread::yield_now();
            }
            drop(occupied);
            assert_eq!(
                receiver.recv_timeout(Duration::from_secs(2)).unwrap(),
                "foreground"
            );
            assert_eq!(
                receiver.recv_timeout(Duration::from_secs(2)).unwrap(),
                "background"
            );
        });
    }

    #[test]
    fn closing_wakes_blocked_requests() {
        let pool = Scheduler::new(1, 1);
        let _occupied = pool.acquire(0, true).unwrap();
        std::thread::scope(|scope| {
            let waiter = scope.spawn(|| pool.acquire(0, true).err().unwrap());
            let deadline = std::time::Instant::now() + Duration::from_secs(2);
            while pool.stats()["waiting_requests"] != 1 {
                assert!(std::time::Instant::now() < deadline);
                std::thread::yield_now();
            }
            pool.close();
            assert!(waiter.join().unwrap().contains("closed"));
        });
    }
}
