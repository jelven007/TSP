use super::super::{PACK1, SecurityCount, Tdx, decode_frame};
use serde::{Deserialize, Serialize};
use std::{
    io::{self, Read, Write},
    net::{SocketAddr, TcpStream},
    sync::atomic::{AtomicUsize, Ordering},
    thread,
    time::{Duration, Instant},
};

/// 每天固定测三轮；所有池和诊断共享最多八个测速许可。
pub const DAILY_PROBE_ROUNDS: usize = 3;
pub const DAILY_PROBE_WORKERS: usize = 8;
pub const DAILY_PROBE_TIMEOUT: Duration = Duration::from_secs(3);

/// TSP adaptation: immutable per-pool qualification, with the upstream default preserved.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ProbeMode {
    #[default]
    SecurityCount,
    FinancialHeartbeat,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ProbeStage {
    Quota,
    Tcp,
    Handshake,
    Heartbeat,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProbeFailure {
    pub stage: ProbeStage,
    /// `std::io::ErrorKind` 的调试名称。
    pub kind: String,
    pub message: String,
}

/// 一轮独立连接的结果。失败耗时不参与可用节点排名。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProbeRound {
    #[serde(default)]
    pub mode: ProbeMode,
    pub tcp: Option<Duration>,
    pub handshake: Option<Duration>,
    pub heartbeat: Option<Duration>,
    pub elapsed: Duration,
    pub security_count: Option<u16>,
    #[serde(default)]
    pub heartbeat_ack: Option<crate::tcp::HeartbeatAck>,
    pub failure: Option<ProbeFailure>,
}

impl ProbeRound {
    pub fn passed(&self) -> bool {
        self.failure.is_none()
            && match self.mode {
                ProbeMode::SecurityCount => self.security_count.is_some(),
                ProbeMode::FinancialHeartbeat => self.heartbeat_ack.is_some(),
            }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NodeTestResult {
    pub address: SocketAddr,
    pub rounds: Vec<ProbeRound>,
}

impl NodeTestResult {
    pub fn passed(&self) -> bool {
        self.rounds.len() == DAILY_PROBE_ROUNDS && self.rounds.iter().all(ProbeRound::passed)
    }

    /// 成功样本的建连、握手和心跳总耗时中位数；不是单次网络 RTT。
    pub fn median_elapsed(&self) -> Option<Duration> {
        let mut times: Vec<_> = self
            .rounds
            .iter()
            .filter(|round| round.passed())
            .map(|round| round.elapsed)
            .collect();
        times.sort_unstable();
        times.get(times.len() / 2).copied()
    }
}

pub(super) fn probe_all(
    addresses: &[SocketAddr],
    rounds: usize,
    timeout: Duration,
) -> io::Result<Vec<NodeTestResult>> {
    probe_all_until(addresses, rounds, timeout, None, ProbeMode::SecurityCount)
}

pub(super) fn probe_all_until(
    addresses: &[SocketAddr],
    rounds: usize,
    timeout: Duration,
    deadline: Option<Instant>,
    mode: ProbeMode,
) -> io::Result<Vec<NodeTestResult>> {
    if timeout.is_zero() || rounds == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "测速参数必须大于0",
        ));
    }
    check_batch(deadline)?;
    let next = AtomicUsize::new(0);
    let context = crate::operation::OperationContext::current();
    thread::scope(|scope| {
        let mut handles = Vec::new();
        for _ in 0..super::coordinator::workers().min(addresses.len()) {
            let next = &next;
            let context = context.clone();
            handles.push(
                thread::Builder::new()
                    .name("rustdx-ip-probe".into())
                    .spawn_scoped(scope, move || {
                        let _scope = context.as_ref().map(|context| context.enter());
                        let mut results = Vec::new();
                        loop {
                            check_batch(deadline)?;
                            let index = next.fetch_add(1, Ordering::Relaxed);
                            let Some(&address) = addresses.get(index) else {
                                break;
                            };
                            let mut samples = Vec::with_capacity(rounds);
                            for _ in 0..rounds {
                                check_batch(deadline)?;
                                let round = probe_one_until(address, timeout, deadline, mode);
                                if let Some(failure) = &round.failure
                                    && failure.stage == ProbeStage::Quota
                                {
                                    // Local resource failure is not a failed node.
                                    // Do not replace a daily pool with quota-starved results.
                                    return Err(io::Error::other(format!(
                                        "探测配额失败（{}）: {}",
                                        failure.kind, failure.message
                                    )));
                                }
                                samples.push(round);
                            }
                            results.push((
                                index,
                                NodeTestResult {
                                    address,
                                    rounds: samples,
                                },
                            ));
                        }
                        Ok::<_, io::Error>(results)
                    })?,
            );
        }
        let mut results = Vec::with_capacity(addresses.len());
        let mut failure = None;
        for handle in handles {
            match handle.join() {
                Ok(Ok(nodes)) => results.extend(nodes),
                Ok(Err(error)) => failure = Some(error),
                Err(_) => failure = Some(io::Error::other("IP测速线程异常退出")),
            }
        }
        check_batch(deadline)?;
        if let Some(error) = failure {
            return Err(error);
        }
        results.sort_by_key(|(index, _)| *index);
        Ok(results.into_iter().map(|(_, result)| result).collect())
    })
}

fn check_batch(deadline: Option<Instant>) -> io::Result<()> {
    match deadline {
        Some(deadline) => crate::tcp::governance::remaining(deadline).map(|_| ()),
        None => crate::operation::check(),
    }
}

#[cfg(test)]
fn probe_one(address: SocketAddr, timeout: Duration) -> ProbeRound {
    probe_one_until(address, timeout, None, ProbeMode::SecurityCount)
}

fn probe_one_until(
    address: SocketAddr,
    timeout: Duration,
    deadline: Option<Instant>,
    mode: ProbeMode,
) -> ProbeRound {
    let started = Instant::now();
    let mut round = ProbeRound {
        mode,
        tcp: None,
        handshake: None,
        heartbeat: None,
        elapsed: Duration::ZERO,
        security_count: None,
        heartbeat_ack: None,
        failure: None,
    };
    let mut stage = ProbeStage::Quota;
    let result: io::Result<()> = (|| {
        let _permit = super::coordinator::acquire(deadline)?;
        stage = ProbeStage::Tcp;
        let tcp_deadline = phase_deadline(timeout, deadline)?;
        let mut stream = TcpStream::connect_timeout(&address, left(tcp_deadline)?)?;
        left(tcp_deadline)?;
        round.tcp = Some(started.elapsed());
        stage = ProbeStage::Handshake;
        let phase = Instant::now();
        exchange(&mut stream, PACK1, phase_deadline(timeout, deadline)?)?;
        round.handshake = Some(phase.elapsed());
        stage = ProbeStage::Heartbeat;
        let phase = Instant::now();
        let (header, body) = exchange(
            &mut stream,
            match mode {
                ProbeMode::SecurityCount => SecurityCount::SEND,
                ProbeMode::FinancialHeartbeat => crate::tcp::HEARTBEAT_REQUEST,
            },
            phase_deadline(timeout, deadline)?,
        )?;
        let body = decode_frame(&header, &body, true)?;
        match mode {
            ProbeMode::SecurityCount => {
                let mut heartbeat = SecurityCount::new(0);
                heartbeat.try_parse(body)?;
                round.security_count = Some(*heartbeat.result());
            }
            ProbeMode::FinancialHeartbeat => {
                round.heartbeat_ack = Some(crate::tcp::decode_heartbeat(&body)?);
            }
        }
        round.heartbeat = Some(phase.elapsed());
        Ok(())
    })();
    if let Err(error) = result {
        // macOS的阻塞socket超时通常表现为WouldBlock。
        let kind = if error.kind() == io::ErrorKind::WouldBlock {
            io::ErrorKind::TimedOut
        } else {
            error.kind()
        };
        round.failure = Some(ProbeFailure {
            stage,
            kind: format!("{kind:?}"),
            message: error.to_string(),
        });
    }
    round.elapsed = started.elapsed();
    round
}

fn phase_deadline(timeout: Duration, deadline: Option<Instant>) -> io::Result<Instant> {
    let phase = crate::tcp::governance::deadline_after(timeout)?;
    Ok(deadline.map_or(phase, |deadline| deadline.min(phase)))
}

fn left(deadline: Instant) -> io::Result<Duration> {
    crate::tcp::governance::remaining(deadline)
}

fn read_exact_until(stream: &mut TcpStream, body: &mut [u8], deadline: Instant) -> io::Result<()> {
    let mut offset = 0;
    while offset < body.len() {
        stream.set_read_timeout(Some(left(deadline)?.min(Duration::from_millis(200))))?;
        match stream.read(&mut body[offset..]) {
            Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
            Ok(n) => offset += n,
            Err(error) if polling_error(&error) => continue,
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

fn exchange(
    stream: &mut TcpStream,
    request: &[u8],
    deadline: Instant,
) -> io::Result<([u8; 16], Vec<u8>)> {
    let mut offset = 0;
    while offset < request.len() {
        stream.set_write_timeout(Some(left(deadline)?.min(Duration::from_millis(200))))?;
        match stream.write(&request[offset..]) {
            Ok(0) => return Err(io::ErrorKind::WriteZero.into()),
            Ok(n) => offset += n,
            Err(error) if polling_error(&error) => continue,
            Err(error) => return Err(error),
        }
    }
    let mut header = [0; 16];
    read_exact_until(stream, &mut header, deadline)?;
    let size = u16::from_le_bytes([header[12], header[13]]) as usize;
    let mut body = vec![0; size];
    read_exact_until(stream, &mut body, deadline)?;
    crate::tcp::frame::matches(&header, request)?;
    left(deadline)?;
    Ok((header, body))
}

fn polling_error(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn financial_mode_accepts_real_heartbeat_without_security_count() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(1)))
                .unwrap();
            for (request, payload) in [
                (PACK1, vec![0, 0]),
                (crate::tcp::HEARTBEAT_REQUEST, vec![0; 10]),
            ] {
                let mut received = vec![0; request.len()];
                stream.read_exact(&mut received).unwrap();
                assert_eq!(received, request);
                let mut reply = vec![0; 16];
                reply[..4].copy_from_slice(&[0xb1, 0xcb, 0x74, 0]);
                reply[5..9].copy_from_slice(&request[1..5]);
                reply[10..12].copy_from_slice(&request[10..12]);
                reply[12..14].copy_from_slice(&(payload.len() as u16).to_le_bytes());
                reply[14..16].copy_from_slice(&(payload.len() as u16).to_le_bytes());
                reply.extend(payload);
                stream.write_all(&reply).unwrap();
            }
        });
        let round = probe_one_until(
            address,
            Duration::from_secs(1),
            None,
            ProbeMode::FinancialHeartbeat,
        );
        assert!(round.passed(), "{:?}", round.failure);
        assert_eq!(round.security_count, None);
        assert_eq!(round.heartbeat_ack.unwrap().raw_payload.len(), 10);
        server.join().unwrap();
    }

    #[test]
    fn slow_headers_and_silent_heartbeat_obey_the_whole_stage_deadline() {
        for heartbeat_stage in [false, true] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let address = listener.local_addr().unwrap();
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(1)))
                    .unwrap();
                stream.read_exact(&mut [0; 13]).unwrap();
                if heartbeat_stage {
                    let mut reply = [0; 18];
                    reply[..4].copy_from_slice(&[0xb1, 0xcb, 0x74, 0]);
                    reply[5..9].copy_from_slice(&PACK1[1..5]);
                    reply[10..12].copy_from_slice(&PACK1[10..12]);
                    reply[12..14].copy_from_slice(&2u16.to_le_bytes());
                    reply[14..16].copy_from_slice(&2u16.to_le_bytes());
                    stream.write_all(&reply).unwrap();
                    stream.read_exact(&mut [0; 18]).unwrap();
                    thread::sleep(Duration::from_millis(90));
                } else {
                    for _ in 0..16 {
                        if stream.write_all(&[0]).is_err() {
                            break;
                        }
                        thread::sleep(Duration::from_millis(15));
                    }
                }
            });
            let round = probe_one(address, Duration::from_millis(30));
            let failure = round.failure.as_ref().unwrap();
            assert_eq!(failure.kind, "TimedOut");
            assert_eq!(
                failure.stage,
                if heartbeat_stage {
                    ProbeStage::Heartbeat
                } else {
                    ProbeStage::Handshake
                }
            );
            assert!(!round.passed());
            assert!(round.tcp.is_some());
            assert!(round.elapsed < Duration::from_millis(200));
            server.join().unwrap();
        }
    }
}
