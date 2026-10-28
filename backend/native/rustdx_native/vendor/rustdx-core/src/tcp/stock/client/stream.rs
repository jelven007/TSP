//! Ordered callback delivery with one outstanding item per worker.
use std::{
    collections::BTreeMap,
    io::{self, Write},
    sync::mpsc,
    time::{Duration, Instant},
};

use super::{BarRangeCollection, BarRangeOptions, Client, KlineData, bar_range};
use crate::{
    operation::OperationContext,
    tcp::{
        governance::{deadline_after, remaining},
        network_policy,
    },
};

#[derive(Debug, Clone, Copy)]
pub struct BatchStreamOptions {
    pub max_parallel: usize,
    /// Total successfully delivered rows.
    pub max_rows: usize,
    /// Total JSON bytes of successfully delivered values; errors are excluded.
    pub max_bytes: usize,
    pub max_item_rows: usize,
    pub max_item_bytes: usize,
    pub timeout: Duration,
}

#[derive(Debug, Clone, Copy, Default)]
pub struct BarBatchStreamOptions {
    pub range: BarRangeOptions,
    pub delivery: BatchStreamOptions,
}

impl Default for BatchStreamOptions {
    fn default() -> Self {
        Self {
            max_parallel: 0,
            max_rows: 1_000_000,
            max_bytes: 256 * 1024 * 1024,
            max_item_rows: 65_536,
            max_item_bytes: 32 * 1024 * 1024,
            timeout: Duration::from_secs(300),
        }
    }
}

#[derive(Debug, Default, Clone, Copy, serde::Serialize)]
pub struct BatchStreamSummary {
    pub delivered_items: usize,
    pub successful_items: usize,
    pub failed_items: usize,
    pub rows: usize,
    pub json_bytes: usize,
}

impl BatchStreamOptions {
    pub(super) fn validate(self) -> io::Result<()> {
        if self.max_rows == 0
            || self.max_bytes == 0
            || self.max_item_rows == 0
            || self.max_item_bytes == 0
            || self.timeout.is_zero()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "流式批量预算须大于0",
            ));
        }
        Ok(())
    }
}

/// An item keeps the input identity; a successful collection may be incomplete.
#[derive(Debug)]
pub struct BatchBarRange<'a> {
    pub input_index: usize,
    pub market: u16,
    pub code: &'a str,
    pub result: io::Result<BarRangeCollection<KlineData<'a>>>,
}

impl Client {
    /// The callback runs on the calling thread, in input order. Only at most
    /// max_parallel items are being fetched or waiting for delivery. Range and
    /// stream budgets are independent; inspect each collection's complete flag.
    ///
    /// Callback errors, budget exhaustion, cancellation and deadline expiry stop
    /// delivery. Previously delivered items remain the consumer's responsibility.
    /// Callbacks are cooperative: a blocking callback cannot be preempted, but
    /// its elapsed time is checked before another item is delivered.
    pub fn bars_batch_stream<'a>(
        &self,
        stocks: &[(u16, &'a str)],
        category: u16,
        begin: Option<u32>,
        end: Option<u32>,
        options: BarBatchStreamOptions,
        mut consume: impl FnMut(BatchBarRange<'a>) -> io::Result<()>,
    ) -> io::Result<BatchStreamSummary> {
        let BarBatchStreamOptions {
            range,
            delivery: options,
        } = options;
        options.validate()?;
        range.validate()?;
        super::validate_request(0, "000001", category)?;
        super::validate_range(begin, end)?;
        let range = BarRangeOptions {
            max_rows: range.max_rows.min(options.max_item_rows),
            max_bytes: range.max_bytes.min(options.max_item_bytes),
            ..range
        };
        self.command_stream(
            stocks,
            options,
            |result: &BarRangeCollection<KlineData<'a>>| result.records.len(),
            |&(market, code), tcp| bar_range::stock(tcp, market, code, category, begin, end, range),
            |index, result| {
                let (market, code) = stocks[index];
                consume(BatchBarRange {
                    input_index: index,
                    market,
                    code,
                    result,
                })
            },
        )
    }

    pub fn k_batch_stream<'a>(
        &self,
        stocks: &[(u16, &'a str)],
        begin: Option<u32>,
        end: Option<u32>,
        options: BarBatchStreamOptions,
        consume: impl FnMut(BatchBarRange<'a>) -> io::Result<()>,
    ) -> io::Result<BatchStreamSummary> {
        self.bars_batch_stream(stocks, 9, begin, end, options, consume)
    }

    pub(super) fn command_stream<R: Sync, T: Send + serde::Serialize>(
        &self,
        requests: &[R],
        options: BatchStreamOptions,
        rows: impl Fn(&T) -> usize,
        run: impl Fn(&R, &mut crate::tcp::Tcp) -> io::Result<T> + Sync,
        mut consume: impl FnMut(usize, io::Result<T>) -> io::Result<()>,
    ) -> io::Result<BatchStreamSummary> {
        options.validate()?;
        super::extended_queries::validate_batch_len(requests.len())?;
        if requests.is_empty() {
            return Ok(BatchStreamSummary::default());
        }
        let deadline = deadline_after(options.timeout.min(network_policy().batch_timeout))?;
        let _gate = self.batch_lock.acquire(deadline)?;
        let budget = network_policy().max_connections.saturating_sub(1);
        if budget == 0 {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "批量查询需要至少两个连接许可",
            ));
        }
        let workers = if options.max_parallel == 0 {
            std::thread::available_parallelism().map_or(4, usize::from)
        } else {
            options.max_parallel
        }
        .max(1)
        .min(budget)
        .min(requests.len());
        let pool = self.batch_pool(workers)?;
        let mut summary = BatchStreamSummary::default();
        ordered(
            requests.len(),
            workers,
            deadline,
            || pool.get_connection_timeout(remaining(deadline)?),
            |connection, index| {
                connection
                    .execute(|tcp| tcp.with_deadline(deadline, |tcp| run(&requests[index], tcp)))
            },
            |index, mut result| {
                let mut item_rows = 0;
                let mut item_bytes = 0;
                if let Ok(value) = &result {
                    let measured = if rows(value) > options.max_item_rows {
                        Err(io::Error::other("流式批量单项行数超出预算"))
                    } else {
                        json_bytes(value, options.max_item_bytes).map(|bytes| (rows(value), bytes))
                    };
                    match measured {
                        Ok((count, bytes)) => {
                            item_rows = count;
                            item_bytes = bytes;
                        }
                        Err(error) => {
                            result = Err(error);
                        }
                    }
                    if summary
                        .rows
                        .checked_add(item_rows)
                        .is_none_or(|n| n > options.max_rows)
                        || summary
                            .json_bytes
                            .checked_add(item_bytes)
                            .is_none_or(|n| n > options.max_bytes)
                    {
                        return Err(io::Error::other("流式批量总行数或总字节预算耗尽"));
                    }
                }
                let success = result.is_ok();
                remaining(deadline)?;
                consume(index, result)?;
                summary.delivered_items += 1;
                summary.successful_items += usize::from(success);
                summary.failed_items += usize::from(!success);
                summary.rows += item_rows;
                summary.json_bytes += item_bytes;
                Ok(())
            },
        )?;
        Ok(summary)
    }
}

fn json_bytes(value: &impl serde::Serialize, max: usize) -> io::Result<usize> {
    struct Counter {
        bytes: usize,
        max: usize,
    }
    impl Write for Counter {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            crate::operation::check()?;
            self.bytes = self
                .bytes
                .checked_add(bytes.len())
                .filter(|n| *n <= self.max)
                .ok_or_else(|| io::Error::other("流式批量单项JSON字节超出预算"))?;
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    let mut count = Counter { bytes: 0, max };
    serde_json::to_writer(&mut count, value).map_err(io::Error::other)?;
    Ok(count.bytes)
}

/// Workers cannot move to another input until its predecessor has been
/// delivered. The result queue and reorder buffer never grow with input count.
pub(super) fn ordered<S: Send, T: Send>(
    len: usize,
    workers: usize,
    deadline: Instant,
    setup: impl Fn() -> io::Result<S> + Sync,
    run: impl Fn(&mut S, usize) -> io::Result<T> + Sync,
    mut consume: impl FnMut(usize, io::Result<T>) -> io::Result<()>,
) -> io::Result<()> {
    let context = OperationContext::new(remaining(deadline)?, None)?;
    let _context = context.enter();
    let inherited =
        OperationContext::current().ok_or_else(|| io::Error::other("流式批量缺少操作上下文"))?;
    std::thread::scope(|scope| {
        let (result_tx, result_rx) = mpsc::sync_channel(workers);
        let mut senders = Vec::with_capacity(workers);
        let mut handles = Vec::with_capacity(workers);
        let launched = (|| {
            for worker in 0..workers {
                let (task_tx, task_rx) = mpsc::sync_channel::<usize>(1);
                senders.push(task_tx);
                let result_tx = result_tx.clone();
                let inherited = inherited.clone();
                let setup = &setup;
                let run = &run;
                handles.push(
                    std::thread::Builder::new()
                        .name("rustdx-stream".into())
                        .spawn_scoped(scope, move || {
                            let _guard = inherited.enter();
                            let mut state = setup();
                            while let Ok(index) = task_rx.recv() {
                                let result = remaining(deadline).and_then(|_| {
                                    let state = state
                                        .as_mut()
                                        .map_err(|e| io::Error::new(e.kind(), e.to_string()))?;
                                    run(state, index)
                                });
                                if result_tx.send((worker, index, result)).is_err() {
                                    break;
                                }
                            }
                        })?,
                );
            }
            Ok::<_, io::Error>(())
        })();
        drop(result_tx);
        let outcome = launched.and_then(|_| {
            let mut scheduled = workers.min(len);
            for (index, sender) in senders.iter().take(scheduled).enumerate() {
                sender
                    .send(index)
                    .map_err(|_| io::Error::other("流式批量线程已退出"))?;
            }
            let mut pending = BTreeMap::new();
            let mut expected = 0;
            while expected < len {
                remaining(deadline)?;
                match result_rx.recv_timeout(remaining(deadline)?.min(Duration::from_millis(20))) {
                    Ok((worker, index, value)) => {
                        pending.insert(index, (worker, value));
                    }
                    Err(mpsc::RecvTimeoutError::Timeout) => continue,
                    Err(mpsc::RecvTimeoutError::Disconnected) => {
                        return Err(io::Error::other("流式批量线程提前退出"));
                    }
                }
                while let Some((worker, value)) = pending.remove(&expected) {
                    remaining(deadline)?;
                    consume(expected, value)?;
                    remaining(deadline)?;
                    expected += 1;
                    if scheduled < len {
                        senders[worker]
                            .send(scheduled)
                            .map_err(|_| io::Error::other("流式批量线程已退出"))?;
                        scheduled += 1;
                    }
                }
            }
            Ok(())
        });
        context.cancel();
        drop(senders);
        drop(result_rx);
        let mut joined = Ok(());
        for handle in handles {
            if handle.join().is_err() {
                joined = Err(io::Error::other("流式批量线程异常"));
            }
        }
        outcome.and(joined)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[test]
    fn slow_first_item_bounds_prefetch_and_preserves_errors_in_input_order() {
        let fetched = AtomicUsize::new(0);
        let mut delivered = Vec::new();
        ordered(
            1000,
            3,
            deadline_after(Duration::from_secs(5)).unwrap(),
            || Ok(()),
            |_, index| {
                fetched.fetch_add(1, Ordering::Relaxed);
                if index == 0 {
                    std::thread::sleep(Duration::from_millis(20));
                }
                if index == 1 {
                    Err(io::Error::other("item failed"))
                } else {
                    Ok(index)
                }
            },
            |index, value| {
                if index == 0 {
                    assert_eq!(fetched.load(Ordering::Relaxed), 3);
                }
                delivered.push((index, value.ok()));
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(delivered.len(), 1000);
        assert_eq!(delivered[1], (1, None));
        for (index, row) in delivered.iter().enumerate() {
            assert_eq!(row.0, index);
        }
    }

    #[test]
    fn callback_failure_deadline_and_cancellation_stop_delivery() {
        let mut delivered = 0;
        let error = ordered(
            100,
            2,
            deadline_after(Duration::from_secs(1)).unwrap(),
            || Ok(()),
            |_, i| Ok(i),
            |_, _| {
                delivered += 1;
                Err(io::Error::new(
                    io::ErrorKind::BrokenPipe,
                    "consumer stopped",
                ))
            },
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::BrokenPipe);
        assert_eq!(delivered, 1);
        let error = ordered(
            10,
            1,
            deadline_after(Duration::from_millis(10)).unwrap(),
            || Ok(()),
            |_, i| Ok(i),
            |_, _| {
                std::thread::sleep(Duration::from_millis(20));
                Ok(())
            },
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        let parent = OperationContext::new(Duration::from_secs(1), None).unwrap();
        let _guard = parent.enter();
        let error = ordered(
            10,
            1,
            deadline_after(Duration::from_secs(1)).unwrap(),
            || Ok(()),
            |_, i| Ok(i),
            |_, _| {
                parent.cancel();
                Ok(())
            },
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::Interrupted);
        drop(_guard);
        assert_eq!(json_bytes(&vec![1, 2], 5).unwrap(), 5);
        assert!(json_bytes(&vec![1, 2], 4).is_err());
    }

    #[test]
    fn total_budgets_stop_before_delivery_and_item_failures_are_retained() {
        for (max_rows, max_bytes, max_item_bytes, expected) in [
            (3, 100, 100, (1, 0, false)),
            (100, 9, 100, (1, 0, false)),
            (100, 100, 4, (0, 2, true)),
            (100, 100, 100, (2, 0, true)),
        ] {
            let (config, server) = crate::tcp::tests::mock_server(2, |_| vec![0, 0]);
            let client = Client::with_config(&config).unwrap();
            let mut successful = 0;
            let mut failed = 0;
            let result = client.command_stream(
                &[0, 1],
                BatchStreamOptions {
                    max_parallel: 1,
                    max_rows,
                    max_bytes,
                    max_item_bytes,
                    ..Default::default()
                },
                |value: &Vec<u8>| value.len(),
                |_, _| Ok(vec![1u8, 2]),
                |_, value| {
                    if value.is_ok() {
                        successful += 1;
                    } else {
                        failed += 1;
                    }
                    Ok(())
                },
            );
            assert_eq!((successful, failed, result.is_ok()), expected);
            if let Ok(summary) = result {
                assert_eq!(summary.successful_items, successful);
                assert_eq!(summary.failed_items, failed);
                assert_eq!(summary.rows, successful * 2);
                assert_eq!(summary.json_bytes, successful * 5);
            }
            drop(client);
            server.join().unwrap();
        }
    }
}
