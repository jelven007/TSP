//! Keep one native lock request queued instead of repeatedly racing the owner.
use std::{
    cell::UnsafeCell,
    fs::File,
    io,
    os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
    ptr,
    time::{Duration, Instant},
};
use windows_sys::Win32::{
    Foundation::{ERROR_IO_PENDING, WAIT_TIMEOUT},
    Storage::FileSystem::{LOCKFILE_EXCLUSIVE_LOCK, LockFileEx},
    System::{
        IO::{CancelIoEx, GetOverlappedResult, GetOverlappedResultEx, OVERLAPPED},
        Threading::CreateEventW,
    },
};

struct Request<'a> {
    file: &'a File,
    // The allocation never moves, and UnsafeCell permits native completion writes.
    overlapped: Box<UnsafeCell<OVERLAPPED>>,
    _event: OwnedHandle,
    pending: bool,
    acquired: bool,
}

impl<'a> Request<'a> {
    fn new(file: &'a File) -> io::Result<Self> {
        // SAFETY: null security/name pointers request an unnamed manual-reset event.
        let event = unsafe { CreateEventW(ptr::null(), 1, 0, ptr::null()) };
        if event.is_null() {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: CreateEventW returned a fresh valid handle; this is its sole owner.
        let event = unsafe { OwnedHandle::from_raw_handle(event) };
        let overlapped = OVERLAPPED {
            hEvent: event.as_raw_handle(),
            ..OVERLAPPED::default()
        };
        let mut request = Self {
            file,
            overlapped: Box::new(UnsafeCell::new(overlapped)),
            _event: event,
            pending: false,
            acquired: false,
        };
        // SAFETY: the borrowed file, event and stable OVERLAPPED allocation stay
        // alive until completion, including cancellation cleanup in Drop.
        let result = unsafe {
            LockFileEx(
                file.as_raw_handle(),
                LOCKFILE_EXCLUSIVE_LOCK,
                0,
                u32::MAX,
                u32::MAX,
                request.overlapped.get(),
            )
        };
        if result != 0 {
            request.acquired = true;
        } else {
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(ERROR_IO_PENDING as i32) {
                return Err(error);
            }
            request.pending = true;
        }
        Ok(request)
    }

    fn finish(mut self, deadline: Instant) -> io::Result<()> {
        loop {
            crate::operation::check()?;
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "文件缓存锁等待超时",
                ));
            }
            if self.acquired {
                // The enclosing FileCacheLock takes responsibility for unlocking.
                self.acquired = false;
                return Ok(());
            }
            let wait = crate::operation::timeout(remaining.min(Duration::from_millis(20)))?;
            let milliseconds = wait.as_millis().max(1) as u32;
            let mut transferred = 0;
            // SAFETY: this request owns the event and OVERLAPPED allocation;
            // the file borrow remains live and the output pointer is writable.
            let result = unsafe {
                GetOverlappedResultEx(
                    self.file.as_raw_handle(),
                    self.overlapped.get(),
                    &mut transferred,
                    milliseconds,
                    0,
                )
            };
            if result != 0 {
                self.pending = false;
                self.acquired = true;
            } else {
                let error = io::Error::last_os_error();
                if error.raw_os_error() != Some(WAIT_TIMEOUT as i32) {
                    return Err(error);
                }
            }
        }
    }
}

impl Drop for Request<'_> {
    fn drop(&mut self) {
        if self.pending {
            let mut transferred = 0;
            // SAFETY: cancel only this request, then acknowledge completion before
            // freeing its OVERLAPPED/event. Cancellation can race normal completion:
            // a completed successful lock must also be explicitly released.
            unsafe {
                CancelIoEx(self.file.as_raw_handle(), self.overlapped.get());
                self.acquired = GetOverlappedResult(
                    self.file.as_raw_handle(),
                    self.overlapped.get(),
                    &mut transferred,
                    1,
                ) != 0;
            }
        }
        if self.acquired {
            let _ = self.file.unlock();
        }
    }
}

pub(super) fn lock(file: &File, deadline: Instant) -> io::Result<()> {
    crate::operation::check()?;
    Request::new(file)?.finish(deadline)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::windows::fs::OpenOptionsExt};
    use windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OVERLAPPED;

    fn files(label: &str) -> (std::path::PathBuf, File, File) {
        let path =
            std::env::temp_dir().join(format!("rustdx-queued-lock-{label}-{}", std::process::id()));
        let open = || {
            fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .custom_flags(FILE_FLAG_OVERLAPPED)
                .open(&path)
                .unwrap()
        };
        let owner = open();
        let waiter = open();
        owner.try_lock().unwrap();
        (path, owner, waiter)
    }

    fn cleanup(path: std::path::PathBuf, owner: File, waiter: File) {
        owner.unlock().unwrap();
        drop((owner, waiter));
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn queued_request_precedes_owner_reacquisition() {
        let (path, owner, waiter) = files("handoff");
        let request = Request::new(&waiter).unwrap();
        assert!(request.pending);
        owner.unlock().unwrap();
        assert!(matches!(
            owner.try_lock(),
            Err(fs::TryLockError::WouldBlock)
        ));
        request
            .finish(Instant::now() + Duration::from_secs(5))
            .unwrap();
        waiter.unlock().unwrap();
        owner.try_lock().unwrap();
        cleanup(path, owner, waiter);
    }

    #[test]
    fn expired_request_is_cancelled_while_owner_remains_locked() {
        let (path, owner, waiter) = files("deadline");
        let error = Request::new(&waiter)
            .unwrap()
            .finish(Instant::now())
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        owner.unlock().unwrap();
        owner.try_lock().unwrap();
        cleanup(path, owner, waiter);
    }

    #[test]
    fn cancellation_releases_a_request_that_completed_during_cancellation() {
        let (path, owner, waiter) = files("cancel-race");
        let request = Request::new(&waiter).unwrap();
        owner.unlock().unwrap();
        let context =
            crate::operation::OperationContext::new(Duration::from_secs(5), None).unwrap();
        context.cancel();
        let _scope = context.enter();
        let error = request
            .finish(Instant::now() + Duration::from_secs(5))
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::Interrupted);
        owner.try_lock().unwrap();
        cleanup(path, owner, waiter);
    }
}
