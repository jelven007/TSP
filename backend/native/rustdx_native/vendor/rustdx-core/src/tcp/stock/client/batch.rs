use super::{Client, KlineData, ctx_err, fetch_bars_range, fetch_k_checked};
use crate::tcp::governance::{deadline_after, remaining};
use crate::tcp::network_policy;
use std::{
    io,
    sync::{Condvar, Mutex},
    time::Instant,
};

#[derive(Debug, Default)]
pub(super) struct BatchGate {
    busy: Mutex<bool>,
    ready: Condvar,
}

impl BatchGate {
    pub(super) fn acquire(&self, deadline: Instant) -> io::Result<BatchGuard<'_>> {
        let mut busy = self
            .busy
            .lock()
            .map_err(|_| io::Error::other("批量查询锁已损坏"))?;
        loop {
            let wait = remaining(deadline)?;
            if !*busy {
                *busy = true;
                return Ok(BatchGuard(self));
            }
            busy = self
                .ready
                .wait_timeout(busy, wait.min(std::time::Duration::from_millis(20)))
                .map_err(|_| io::Error::other("批量查询锁已损坏"))?
                .0;
        }
    }
}

pub(super) struct BatchGuard<'a>(&'a BatchGate);

impl Drop for BatchGuard<'_> {
    fn drop(&mut self) {
        *self.0.busy.lock().unwrap_or_else(|e| e.into_inner()) = false;
        self.0.ready.notify_one();
    }
}

type RowResult<'a> = io::Result<Vec<KlineData<'a>>>;

pub(super) fn fetch<'a>(
    client: &Client,
    stocks: &[(u16, &'a str)],
    category: Option<u16>,
    begin: Option<u32>,
    end: Option<u32>,
    max_parallel: usize,
) -> io::Result<Vec<RowResult<'a>>> {
    let deadline = deadline_after(network_policy().batch_timeout)?;
    client.command_batch(stocks, max_parallel, deadline, |&(market, code), tcp| {
        match category {
            Some(category) => fetch_bars_range(tcp, market, code, category, begin, end),
            None => fetch_k_checked(tcp, market, code, begin, end),
        }
        .map_err(|error| ctx_err(error, format_args!("batch(market={market}, code={code})")))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn queued_batches_have_a_deadline() {
        let gate = BatchGate::default();
        let guard = gate
            .acquire(deadline_after(std::time::Duration::from_secs(1)).unwrap())
            .unwrap();
        assert_eq!(
            gate.acquire(deadline_after(std::time::Duration::from_millis(5)).unwrap())
                .err()
                .unwrap()
                .kind(),
            io::ErrorKind::TimedOut
        );
        drop(guard);
        assert!(
            gate.acquire(deadline_after(std::time::Duration::from_secs(1)).unwrap())
                .is_ok()
        );
    }
}
