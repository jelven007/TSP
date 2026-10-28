//! Cooperative cancellation and one absolute deadline for synchronous work.
//! Worker threads must explicitly enter a clone of the parent's context.
use std::{
    cell::RefCell,
    io::{self, Read, Write},
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

#[derive(Debug, Clone)]
pub struct OperationContext {
    deadline: Instant,
    cancelled: Arc<AtomicBool>,
    cancel_file: Option<PathBuf>,
    parent: Option<Arc<OperationContext>>,
}

thread_local! {
    static CURRENT: RefCell<Option<OperationContext>> = const { RefCell::new(None) };
}

impl OperationContext {
    pub fn new(timeout: Duration, cancel_file: Option<PathBuf>) -> io::Result<Self> {
        if timeout.is_zero() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "任务时限必须大于0",
            ));
        }
        let deadline = Instant::now()
            .checked_add(timeout)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "任务时限过大"))?;
        Ok(Self {
            deadline,
            cancelled: Arc::new(AtomicBool::new(false)),
            cancel_file,
            parent: None,
        })
    }

    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::Release);
    }

    pub fn current() -> Option<Self> {
        CURRENT.with(|current| current.borrow().clone())
    }

    /// A nested scope cannot extend the parent's deadline. Drop restores it,
    /// including during unwinding. The guard stays on the entering thread.
    pub fn enter(&self) -> OperationGuard {
        let mut context = self.clone();
        let previous = CURRENT.with(|current| {
            let mut current = current.borrow_mut();
            if let Some(parent) = current.as_ref() {
                context.deadline = context.deadline.min(parent.deadline);
                context.parent = Some(Arc::new(parent.clone()));
            }
            current.replace(context)
        });
        OperationGuard {
            previous,
            _thread: std::marker::PhantomData,
        }
    }

    pub fn remaining(&self) -> io::Result<Duration> {
        if let Some(parent) = &self.parent {
            parent.remaining()?;
        }
        if self.cancelled.load(Ordering::Acquire)
            || self.cancel_file.as_ref().is_some_and(|p| p.exists())
        {
            return Err(io::Error::new(io::ErrorKind::Interrupted, "任务已取消"));
        }
        let duration = self.deadline.saturating_duration_since(Instant::now());
        if duration.is_zero() {
            Err(io::Error::new(io::ErrorKind::TimedOut, "任务总时限已耗尽"))
        } else {
            Ok(duration)
        }
    }
}

pub struct OperationGuard {
    previous: Option<OperationContext>,
    _thread: std::marker::PhantomData<std::rc::Rc<()>>,
}

impl Drop for OperationGuard {
    fn drop(&mut self) {
        CURRENT.with(|current| *current.borrow_mut() = self.previous.take());
    }
}

pub fn check() -> io::Result<()> {
    CURRENT.with(|current| {
        current
            .borrow()
            .as_ref()
            .map_or(Ok(()), |context| context.remaining().map(|_| ()))
    })
}

pub fn timeout(limit: Duration) -> io::Result<Duration> {
    CURRENT.with(|current| {
        current.borrow().as_ref().map_or(Ok(limit), |context| {
            context.remaining().map(|remaining| limit.min(remaining))
        })
    })
}

pub fn sleep(duration: Duration) -> io::Result<()> {
    let end = Instant::now()
        .checked_add(duration)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "等待时间过大"))?;
    loop {
        check()?;
        let remaining = end.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Ok(());
        }
        std::thread::sleep(timeout(remaining.min(Duration::from_millis(20)))?);
    }
}

/// Checks cancellation between bounded reads/writes. Blocking OS I/O itself is
/// not preempted; network clients must also set their transport timeouts.
pub struct CheckedIo<T>(pub T);

impl<T: Read> Read for CheckedIo<T> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        // Read::read_to_end and io::copy automatically retry Interrupted.
        // A cancelled operation must escape those loops instead.
        let terminal = |error: io::Error| {
            if error.kind() == io::ErrorKind::Interrupted {
                io::Error::other(error)
            } else {
                error
            }
        };
        check().map_err(terminal)?;
        let n = self.0.read(buffer).map_err(terminal)?;
        check().map_err(terminal)?;
        Ok(n)
    }
}

impl<T: Write> Write for CheckedIo<T> {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        check().map_err(io::Error::other)?;
        let n = self.0.write(buffer)?;
        check().map_err(io::Error::other)?;
        Ok(n)
    }
    fn flush(&mut self) -> io::Result<()> {
        check()?;
        self.0.flush()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn absolute_deadline_is_not_refreshed() {
        let mut task = OperationContext::new(Duration::from_secs(60), None).unwrap();
        let deadline = task.deadline;
        let _guard = task.enter();
        assert!(timeout(Duration::from_secs(120)).unwrap() <= Duration::from_secs(60));
        assert_eq!(OperationContext::current().unwrap().deadline, deadline);
        drop(_guard);
        // Inject an already expired deadline; no scheduler timing assumption.
        task.deadline = Instant::now();
        let _guard = task.enter();
        assert_eq!(check().unwrap_err().kind(), io::ErrorKind::TimedOut);
        assert_eq!(
            sleep(Duration::ZERO).unwrap_err().kind(),
            io::ErrorKind::TimedOut
        );
    }

    #[test]
    fn worker_cancellation_after_entering_context() {
        let task = OperationContext::new(Duration::from_secs(60), None).unwrap();
        let (ready_tx, ready_rx) = std::sync::mpsc::channel();
        let (cancel_tx, cancel_rx) = std::sync::mpsc::channel();
        let worker = task.clone();
        let thread = std::thread::spawn(move || {
            let _guard = worker.enter();
            check().unwrap();
            ready_tx.send(()).unwrap();
            cancel_rx.recv_timeout(Duration::from_secs(10)).unwrap();
            sleep(Duration::from_secs(10)).unwrap_err().kind()
        });
        ready_rx.recv_timeout(Duration::from_secs(10)).unwrap();
        task.cancel();
        cancel_tx.send(()).unwrap();
        assert_eq!(thread.join().unwrap(), io::ErrorKind::Interrupted);
    }

    #[test]
    fn scopes_restore_and_expiry_is_not_refreshed() {
        let mut task = OperationContext::new(Duration::from_secs(60), None).unwrap();
        task.deadline = Instant::now();
        let guard = task.enter();
        let nested = OperationContext::new(Duration::from_secs(120), None).unwrap();
        let inner = nested.enter();
        assert_eq!(OperationContext::current().unwrap().deadline, task.deadline);
        assert!(check().is_err());
        drop(inner);
        assert_eq!(check().unwrap_err().kind(), io::ErrorKind::TimedOut);
        drop(guard);
        check().unwrap();
    }
}
