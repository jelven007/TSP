//! Empty-body 0004 heartbeat and one bounded idle worker per managed connection.
use super::{
    FrameEvent, FrameObserver, Tdx,
    governance::{deadline_after, global, remaining},
};
use std::{
    io::{self, Read, Write},
    net::{Shutdown, SocketAddr, TcpStream},
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

pub const HEARTBEAT_REQUEST: &[u8] = &[0x0c, 1, 0, 0, 0, 1, 2, 0, 2, 0, 4, 0];

#[derive(Debug, Default, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct HeartbeatAck {
    pub reserved_raw: [u8; 6],
    pub server_date_raw: u32,
    /// Invalid/zero date is preserved in server_date_raw and represented as None.
    pub server_date: Option<chrono::NaiveDate>,
    pub raw_payload: Vec<u8>,
}

pub fn decode_heartbeat(bytes: &[u8]) -> io::Result<HeartbeatAck> {
    let mut reader = super::reader::Reader::new(bytes);
    let mut reserved_raw = [0; 6];
    reserved_raw.copy_from_slice(reader.take(6)?);
    let server_date_raw = reader.u32()?;
    Ok(HeartbeatAck {
        reserved_raw,
        server_date_raw,
        server_date: crate::data::date_from_u32(server_date_raw).ok(),
        raw_payload: bytes.to_vec(),
    })
}

#[derive(Debug, Default)]
pub struct Heartbeat {
    ack: HeartbeatAck,
}

impl Tdx for Heartbeat {
    type Item = HeartbeatAck;
    const SEND: &'static [u8] = HEARTBEAT_REQUEST;
    const TAG: &'static str = "心跳0004";
    fn send(&mut self) -> &[u8] {
        Self::SEND
    }
    fn parse(&mut self, bytes: Vec<u8>) {
        let _ = self.try_parse(bytes);
    }
    fn try_parse(&mut self, bytes: Vec<u8>) -> io::Result<()> {
        self.ack = HeartbeatAck::default();
        self.ack = decode_heartbeat(&bytes)?;
        Ok(())
    }
    fn result(&self) -> &Self::Item {
        &self.ack
    }
}

#[derive(Debug)]
pub(super) struct FrameGate {
    pub last_activity: Mutex<Instant>,
    pub failed: AtomicBool,
}

impl Default for FrameGate {
    fn default() -> Self {
        Self {
            last_activity: Mutex::new(Instant::now()),
            failed: AtomicBool::new(false),
        }
    }
}

#[derive(Debug, Default)]
struct Stop {
    stopped: AtomicBool,
    abort_io: AtomicBool,
    wait: Mutex<()>,
    wake: Condvar,
}

#[derive(Debug)]
pub(super) struct IdleHeartbeat {
    stop: Arc<Stop>,
    socket: TcpStream,
    handle: Option<JoinHandle<()>>,
}

impl IdleHeartbeat {
    pub fn start(
        socket: &TcpStream,
        gate: Arc<FrameGate>,
        interval: Duration,
        timeout: Duration,
        observer: Option<Arc<dyn FrameObserver>>,
        pool: super::ip::IpPool,
    ) -> io::Result<Self> {
        // Preserve the established connection's identity before duplicating
        // handles. On Windows, peer_addr on a clone can fail despite successful
        // frame I/O; pool revocation and bounded I/O still detect invalidation.
        let endpoint = socket.peer_addr()?;
        let stop = Arc::new(Stop::default());
        let worker_stop = Arc::clone(&stop);
        let mut worker_socket = socket.try_clone()?;
        let socket = socket.try_clone()?;
        let handle = thread::Builder::new()
            .name("rustdx-heartbeat".into())
            .spawn(move || {
                loop {
                    if worker_stop.stopped.load(Ordering::Acquire) {
                        break;
                    }
                    let wait = match gate.last_activity.try_lock() {
                        Ok(mut last) => {
                            if gate.failed.load(Ordering::Acquire) {
                                break;
                            }
                            let elapsed = last.elapsed();
                            if elapsed < interval {
                                interval - elapsed
                            } else {
                                // Same socket, with complete frame ownership. The managed
                                // BufReader has capacity 1, so it never prefetches bytes
                                // for a response this worker will consume.
                                if worker_stop.stopped.load(Ordering::Acquire) {
                                    break;
                                }
                                if !pool.contains_current(endpoint) {
                                    gate.failed.store(true, Ordering::Release);
                                    let _ = worker_socket.shutdown(Shutdown::Both);
                                    break;
                                }
                                let started = Instant::now();
                                let result = exchange(
                                    &mut worker_socket,
                                    endpoint,
                                    timeout,
                                    observer.as_deref(),
                                    &worker_stop.abort_io,
                                );
                                global().request_result(result.is_ok());
                                global().observation(started.elapsed(), false);
                                global().endpoint_result(endpoint, result.is_ok());
                                if result.is_err() {
                                    gate.failed.store(true, Ordering::Release);
                                    let _ = worker_socket.shutdown(Shutdown::Both);
                                    break;
                                }
                                *last = Instant::now();
                                interval
                            }
                        }
                        Err(std::sync::TryLockError::WouldBlock) => Duration::from_millis(10),
                        Err(std::sync::TryLockError::Poisoned(_)) => {
                            gate.failed.store(true, Ordering::Release);
                            let _ = worker_socket.shutdown(Shutdown::Both);
                            break;
                        }
                    };
                    let Ok(guard) = worker_stop.wait.lock() else {
                        break;
                    };
                    if worker_stop.stopped.load(Ordering::Acquire) {
                        break;
                    }
                    if worker_stop
                        .wake
                        .wait_timeout(guard, wait.min(Duration::from_secs(1)))
                        .is_err()
                    {
                        break;
                    }
                }
            })?;
        Ok(Self {
            stop,
            socket,
            handle: Some(handle),
        })
    }

    /// Drop/reconnect also aborts polled I/O: Windows shutdown does not reliably
    /// wake a read on a cloned socket. Raw extraction waits for the current
    /// bounded heartbeat and leaves a successful socket open.
    pub fn stop(&mut self, shutdown: bool) {
        let Some(handle) = self.handle.take() else {
            return;
        };
        if shutdown {
            self.stop.abort_io.store(true, Ordering::Release);
        }
        self.stop.stopped.store(true, Ordering::Release);
        self.stop.wake.notify_all();
        if shutdown {
            let _ = self.socket.shutdown(Shutdown::Both);
        }
        let _ = handle.join();
    }
}

impl Drop for IdleHeartbeat {
    fn drop(&mut self) {
        self.stop(true);
    }
}

fn exchange(
    socket: &mut TcpStream,
    endpoint: SocketAddr,
    timeout: Duration,
    observer: Option<&dyn FrameObserver>,
    abort_io: &AtomicBool,
) -> io::Result<()> {
    if let Some(observer) = observer {
        observer.observe(FrameEvent::Request {
            endpoint,
            request: HEARTBEAT_REQUEST,
        })?;
    }
    let result: io::Result<()> = (|| {
        let deadline = deadline_after(timeout)?;
        write_until(socket, HEARTBEAT_REQUEST, deadline, abort_io)?;
        let mut header = [0; 16];
        read_until(socket, &mut header, deadline, abort_io)?;
        let mut body = vec![0; usize::from(u16::from_le_bytes([header[12], header[13]]))];
        read_until(socket, &mut body, deadline, abort_io)?;
        if let Some(observer) = observer {
            observer.observe(FrameEvent::Response {
                endpoint,
                request: HEARTBEAT_REQUEST,
                header: &header,
                body: &body,
            })?;
        }
        super::frame::matches(&header, HEARTBEAT_REQUEST)?;
        decode_heartbeat(&super::decode_frame(&header, &body, true)?)?;
        // Clones share socket options. Raw extraction must not inherit the
        // short polling timeout used only to stop this worker promptly.
        socket.set_read_timeout(Some(timeout))?;
        socket.set_write_timeout(Some(timeout))?;
        Ok(())
    })();
    if let (Err(error), Some(observer)) = (&result, observer) {
        observer.observe(FrameEvent::Failed {
            endpoint,
            request: HEARTBEAT_REQUEST,
            kind: error.kind(),
        })?;
    }
    result
}

fn io_poll_timeout(deadline: Instant, abort_io: &AtomicBool) -> io::Result<Duration> {
    if abort_io.load(Ordering::Acquire) {
        return Err(io::Error::new(
            io::ErrorKind::ConnectionAborted,
            "heartbeat stopped",
        ));
    }
    Ok(remaining(deadline)?.min(Duration::from_millis(20)))
}

fn write_until(
    socket: &mut TcpStream,
    bytes: &[u8],
    deadline: Instant,
    abort_io: &AtomicBool,
) -> io::Result<()> {
    let mut offset = 0;
    while offset < bytes.len() {
        socket.set_write_timeout(Some(io_poll_timeout(deadline, abort_io)?))?;
        #[cfg(windows)]
        super::socket_poll::writable(socket, || io_poll_timeout(deadline, abort_io))?;
        match socket.write(&bytes[offset..]) {
            Ok(0) => return Err(io::ErrorKind::WriteZero.into()),
            Ok(n) => offset += n,
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::Interrupted
                        | io::ErrorKind::TimedOut
                        | io::ErrorKind::WouldBlock
                ) =>
            {
                continue;
            }
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

fn read_until(
    socket: &mut TcpStream,
    bytes: &mut [u8],
    deadline: Instant,
    abort_io: &AtomicBool,
) -> io::Result<()> {
    let mut offset = 0;
    while offset < bytes.len() {
        socket.set_read_timeout(Some(io_poll_timeout(deadline, abort_io)?))?;
        #[cfg(windows)]
        super::socket_poll::readable(socket, || io_poll_timeout(deadline, abort_io))?;
        match socket.read(&mut bytes[offset..]) {
            Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
            Ok(n) => offset += n,
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::Interrupted
                        | io::ErrorKind::TimedOut
                        | io::ErrorKind::WouldBlock
                ) =>
            {
                continue;
            }
            Err(error) => return Err(error),
        }
    }
    Ok(())
}
