use crate::bytes_helper::u16_from_le_bytes;
use log::trace;
use std::{
    io::{BufReader, Read, Result, Write},
    net::{SocketAddr, TcpStream},
    num::NonZeroU64,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

static NEXT_CONNECTION_ID: AtomicU64 = AtomicU64::new(1);

// Retain the upstream MSRV 1.89 API; try_update is newer than that toolchain.
#[allow(deprecated)]
fn next_connection_id() -> Result<NonZeroU64> {
    let id = NEXT_CONNECTION_ID
        .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |id| id.checked_add(1))
        .map_err(|_| std::io::Error::other("TCP连接标识已耗尽"))?;
    NonZeroU64::new(id).ok_or_else(|| std::io::Error::other("TCP连接标识不能为0"))
}

#[macro_use]
#[cfg(test)]
pub(crate) mod tests;

mod basic;
pub use basic::*;
pub(crate) mod governance;
pub use governance::{
    EndpointStats, NetworkPolicy, NetworkStats, Permit as NetworkPermit, SharedNetworkPolicy,
    acquire_network, configure_network, network_policy, network_stats, observe_empty_response,
    observe_request,
};
use governance::{deadline_after, global, remaining};

mod heartbeat;
pub use heartbeat::{HEARTBEAT_REQUEST, Heartbeat, HeartbeatAck, decode_heartbeat};

pub mod evidence;
pub(crate) mod frame;
pub mod helper;
pub use evidence::{FrameEvent, FrameObserver, decode_frame, decode_response};
pub mod ip;
pub(crate) mod reader;
#[cfg(windows)]
mod socket_poll;

pub mod startup;
pub mod stock;

/// 用于缓冲读取 TcpStream 的数据。
pub type BufTcp = BufReader<TcpStream>;

/// 连接配置：超时与指定服务器。
///
/// - [`Tcp::new`] 从默认当日有效IP池选址；
/// - [`Tcp::with_config`] 可选择独立IP池、指定已通过当日测试的地址与超时。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TcpConfig {
    /// 连接与读写的超时时间。
    pub timeout: Duration,
    /// 指定服务器地址。该地址必须在所选IP池中通过当日三轮测速。
    /// `None` 表示从当日有效IP池自动选择。
    pub ip: Option<SocketAddr>,
    /// 集成项目的独立IP池。`None` 使用进程共享的 [`ip::default_pool`]。
    pub ip_pool: Option<ip::IpPool>,
    /// 请求失败时自动重连重试的次数（每次失败先重连、再重发请求）。
    /// 默认 `0` = 不自动重试（与旧版行为一致）；日常使用建议 `1~2`。
    pub auto_reconnect: usize,
    /// 重试前的退避间隔基数（毫秒）。第 `n` 次失败后等待
    /// `retry_delay_ms × n` 再重连重试（线性退避）。
    ///
    /// 代理/高并发环境下「立即重试」常连续失败（实测 TUN fake-IP 环境
    /// 首连易 `EAGAIN`），建议 200~400。默认 `0` = 立即重试（旧行为）。
    pub retry_delay_ms: u64,
    /// K 线区间拉取（[`crate::tcp::stock::Client::k`] /
    /// [`crate::tcp::stock::Client::bars_range`]）空结果二次确认。
    ///
    /// TDX 服务端高频请求下会**静默返回空响应**（不报错），与停牌股
    /// 「窗口内本就无 K 线」的真空无法从返回值区分。开启后：空结果时
    /// 等待 300ms 并重连，重拉一次，仍空才返回空——以停牌股约 300ms/只
    /// 的代价换取全市场扫描不被静默限流污染。默认 `false`（旧行为）。
    ///
    /// 成交明细全量查询（[`stock::Client::transaction_all`] /
    /// [`stock::Client::history_transaction_all`]）也使用此选项，但在同一连接上
    /// 复查空页；中途重连会终止该次枚举，以免跨会话拼接偏移。
    pub recheck_empty: bool,
}

impl Default for TcpConfig {
    fn default() -> Self {
        Self {
            timeout: TIMEOUT,
            ip: None,
            ip_pool: None,
            auto_reconnect: 0,
            retry_delay_ms: 0,
            recheck_empty: false,
        }
    }
}

impl TcpConfig {
    /// 使用列表中第 `index` 个服务器（越界时回退到第一个）。
    pub fn with_index(index: usize, timeout: Duration) -> Self {
        Self {
            timeout,
            ip: Some(ip::STOCK_IP.get(index).copied().unwrap_or(ip::STOCK_IP[0])),
            ..Default::default()
        }
    }
}

/// Tcp 链接的一层封装。
#[derive(Debug)]
pub struct Tcp {
    // Stop/join the worker before closing streams and returning connection permits.
    idle_heartbeat: Option<Box<heartbeat::IdleHeartbeat>>,
    stream: TcpStream,
    buffer: BufTcp,
    recv: [u8; RECV_SIZE],
    /// 创建本连接所用的配置，[`Tcp::reconnect`] 时使用。
    config: TcpConfig,
    reusable: bool,
    deadline: Option<Instant>,
    // 字段析构顺序：先释放流，再归还进程连接许可。
    permit: Option<governance::Permit>,
    observer: Option<Arc<dyn FrameObserver>>,
    ip_pool: ip::IpPool,
    connection_generation: u64,
    connection_id: NonZeroU64,
    frame_gate: Arc<heartbeat::FrameGate>,
    heartbeat_interval: Option<Duration>,
}

impl Tcp {
    /// Process-unique managed connection identity. A successful reconnect,
    /// including to the same endpoint, always receives a new identity.
    pub fn connection_id(&self) -> NonZeroU64 {
        self.connection_id
    }

    /// 连接到行情服务器并完成标准握手。
    ///
    /// 从当日有效IP池轮换选址，跳过冷却端点并故障转移。
    /// 初次无当日结果时等待后台补测；空池返回NotConnected，全部冷却返回WouldBlock。
    ///
    /// 超时默认 5 秒，可用 [`Tcp::with_config`] 自定义。
    pub fn new() -> Result<Self> {
        Self::with_config(&TcpConfig::default())
    }

    /// 以指定配置连接服务器并完成标准握手。
    ///
    /// - `config.ip` 为 `Some(addr)` 时只连接通过当日测试的该地址；
    /// - 为 `None` 时在当日有效IP池内轮换及冷却后故障转移。
    pub fn with_config(config: &TcpConfig) -> Result<Self> {
        Self::with_config_until(config, deadline_after(network_policy().connect_timeout)?)
    }

    pub(crate) fn with_config_until(config: &TcpConfig, deadline: Instant) -> Result<Self> {
        Self::with_observer_until(config, deadline, None)
    }

    /// Archive requests and complete compressed frames, including the first handshake.
    pub fn with_config_and_observer(
        config: &TcpConfig,
        observer: Arc<dyn FrameObserver>,
    ) -> Result<Self> {
        Self::with_observer_until(
            config,
            deadline_after(network_policy().connect_timeout)?,
            Some(observer),
        )
    }

    fn with_observer_until(
        config: &TcpConfig,
        deadline: Instant,
        observer: Option<Arc<dyn FrameObserver>>,
    ) -> Result<Self> {
        if config.timeout.is_zero() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "超时必须大于0",
            ));
        }
        let deadline = deadline.min(deadline_after(network_policy().connect_timeout)?);
        if observer.is_none()
            && let Some(tcp) = startup::take_warm_client(config, deadline)?
        {
            return Ok(tcp);
        }
        // 等待补测时不预占行情连接许可，否则满额时可能无法完成初始化。
        config
            .ip_pool
            .as_ref()
            .unwrap_or_else(|| ip::default_pool())
            .addresses_until(config.ip, deadline)?;
        let permit = global().acquire(deadline)?;
        Self::connect_selected(config, deadline, Some(permit), observer)
    }

    fn connect_selected(
        config: &TcpConfig,
        deadline: Instant,
        mut permit: Option<governance::Permit>,
        observer: Option<Arc<dyn FrameObserver>>,
    ) -> Result<Self> {
        let ip_pool = config
            .ip_pool
            .clone()
            .unwrap_or_else(|| ip::default_pool().clone());
        let valid = ip_pool.addresses_until(config.ip, deadline)?;
        let addresses = if config.ip.is_some() {
            valid
        } else {
            global().candidates(&valid)
        };
        let mut last_error =
            std::io::Error::new(std::io::ErrorKind::WouldBlock, "行情服务器均在冷却期");
        for address in addresses {
            let attempt_deadline = deadline.min(deadline_after(config.timeout)?);
            let result = (|| {
                let timeout = remaining(attempt_deadline)?;
                let timeout = if crate::operation::OperationContext::current().is_some() {
                    timeout.min(Duration::from_secs(1))
                } else {
                    timeout
                };
                if !ip_pool.contains_current(address) {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::NotConnected,
                        "地址已退出当日IP池",
                    ));
                }
                let (stream, buffer, recv) = open_tcpstream(&address, timeout)?;
                let mut tcp = Self {
                    stream,
                    buffer,
                    recv,
                    config: config.clone(),
                    reusable: false,
                    deadline: Some(attempt_deadline),
                    permit: None,
                    observer: observer.clone(),
                    ip_pool: ip_pool.clone(),
                    connection_generation: 0,
                    connection_id: next_connection_id()?,
                    frame_gate: Arc::new(heartbeat::FrameGate::default()),
                    heartbeat_interval: Some(Duration::from_secs(30)),
                    idle_heartbeat: None,
                };
                // 握手失败由服务器选择处理，禁止在握手内部递归重连。
                tcp.read_frame_once(PACK1)?;
                tcp.deadline = None;
                Ok(tcp)
            })();
            global().endpoint_result(address, result.is_ok());
            match result {
                Ok(mut tcp) => {
                    tcp.permit = permit.take();
                    tcp.set_heartbeat_interval(Some(Duration::from_secs(30)))?;
                    return Ok(tcp);
                }
                Err(error) => last_error = error,
            }
            remaining(deadline)?;
        }
        Err(last_error)
    }

    /// 为一组请求设置总体时限。嵌套调用仅能收紧外层截止时间。
    /// 同步闭包中的用户计算本身不能被抢占；所有受管网络 I/O 会检查截止时间。
    pub fn with_timeout<T>(
        &mut self,
        timeout: Duration,
        run: impl FnOnce(&mut Self) -> Result<T>,
    ) -> Result<T> {
        self.with_deadline(deadline_after(timeout)?, run)
    }

    pub(crate) fn with_deadline<T>(
        &mut self,
        deadline: Instant,
        run: impl FnOnce(&mut Self) -> Result<T>,
    ) -> Result<T> {
        let previous = self.deadline;
        self.deadline = Some(previous.map_or(deadline, |old| old.min(deadline)));
        let deadline = self.deadline.unwrap();
        let result = remaining(deadline)
            .and_then(|_| run(self))
            .and_then(|value| remaining(deadline).map(|_| value));
        self.deadline = previous;
        result
    }

    fn io_timeout(&self) -> Result<Duration> {
        self.deadline.map_or(Ok(self.config.timeout), |deadline| {
            remaining(deadline).map(|remaining| remaining.min(self.config.timeout))
        })
    }

    fn poll_timeout(&self) -> Result<Duration> {
        let timeout = self.io_timeout()?;
        Ok(if crate::operation::OperationContext::current().is_some() {
            timeout.min(Duration::from_millis(200))
        } else {
            timeout
        })
    }

    fn retry_poll(&self, error: &std::io::Error) -> Result<bool> {
        if crate::operation::OperationContext::current().is_some()
            && matches!(
                error.kind(),
                std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
            )
        {
            // A poll waking after cancellation/expiry must return the task's
            // terminal error, rather than leaking the socket's timeout kind.
            self.io_timeout()?;
            Ok(true)
        } else {
            Ok(false)
        }
    }

    fn read_exact_deadline(&mut self, body: &mut [u8]) -> Result<()> {
        let mut offset = 0;
        while offset < body.len() {
            self.buffer
                .get_ref()
                .set_read_timeout(Some(self.poll_timeout()?))?;
            #[cfg(windows)]
            socket_poll::readable(self.buffer.get_ref(), || self.poll_timeout())?;
            match self.buffer.read(&mut body[offset..]) {
                Ok(0) => {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::UnexpectedEof,
                        "响应提前结束",
                    ));
                }
                Ok(n) => offset += n,
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error) if self.retry_poll(&error)? => continue,
                Err(error) => return Err(error),
            }
        }
        Ok(())
    }

    /// 当前连接配置（[`TcpConfig::recheck_empty`] 等高级选项由此读取）。
    pub fn config(&self) -> &TcpConfig {
        &self.config
    }

    pub(crate) fn connection_generation(&self) -> u64 {
        self.connection_generation
    }

    pub fn is_reusable(&self) -> bool {
        self.reusable
            && !self.frame_gate.failed.load(Ordering::Acquire)
            && self
                .stream
                .peer_addr()
                .is_ok_and(|address| self.ip_pool.contains_current(address))
    }

    /// 跨日、09:00刷新或初始池删除后，下一次请求必须重新检查池。
    fn ensure_daily_endpoint(&mut self) -> Result<()> {
        let address = match self.stream.peer_addr() {
            Ok(address) => address,
            // macOS can return EINVAL after shutdown. A failed heartbeat has
            // already invalidated this connection, so obtain a new session.
            Err(_) if !self.is_reusable() => {
                self.reconnect()?;
                self.stream.peer_addr()?
            }
            Err(error) => return Err(error),
        };
        if self.ip_pool.contains_current(address) {
            return Ok(());
        }
        let deadline = self
            .deadline
            .unwrap_or(deadline_after(network_policy().connect_timeout)?);
        let valid = self.ip_pool.addresses_until(self.config.ip, deadline)?;
        if !valid.contains(&address) {
            self.reconnect()?;
        }
        Ok(())
    }

    pub(crate) fn check_health(&mut self, timeout: Duration) -> Result<()> {
        self.with_timeout(timeout, |tcp| tcp.heartbeat().map(|_| ()))
    }

    /// 重新建立连接（按创建时的配置），并重发握手包。原连接被丢弃。
    ///
    /// ## 示例
    /// ```ignore
    /// tcp.reconnect()?; // 断线后重新连接
    /// ```
    pub fn reconnect(&mut self) -> Result<()> {
        let generation = self
            .connection_generation
            .checked_add(1)
            .ok_or_else(|| std::io::Error::other("TCP连接代次已耗尽"))?;
        self.reusable = false;
        if let Some(mut worker) = self.idle_heartbeat.take() {
            worker.stop(true);
        }
        // 两个句柄引用同一 socket；shutdown 使旧连接立即停止占用服务器资源。
        let _ = self.stream.shutdown(std::net::Shutdown::Both);
        let deadline = self
            .deadline
            .unwrap_or(deadline_after(network_policy().connect_timeout)?);
        // 许可留在 self 中；不再申请第二个槽位，也不会在饱和时自锁。
        let mut replacement =
            Self::connect_selected(&self.config, deadline, None, self.observer.clone())?;
        replacement.permit = self.permit.take();
        replacement.deadline = self.deadline;
        replacement.connection_generation = generation;
        replacement.set_heartbeat_interval(self.heartbeat_interval)?;
        *self = replacement;
        Ok(())
    }

    /// 发送心跳包保持连接活跃。
    ///
    /// 通达信服务器会关闭长时间空闲的连接；在空闲场景下定期调用即可。
    /// 返回0004确认帧中的服务端日期及原始字段。默认空闲30秒自动发送。
    pub fn heartbeat(&mut self) -> Result<HeartbeatAck> {
        let mut hb = Heartbeat::default();
        let result = hb.recv_parsed(self).cloned();
        if result.is_err() {
            self.frame_gate.failed.store(true, Ordering::Release);
        }
        result
    }

    /// Explicit legacy count probe (044e). Prefer heartbeat() for keepalive.
    pub fn heartbeat_legacy(&mut self) -> Result<u16> {
        let mut count = SecurityCount::new(0);
        count.recv_parsed(self).copied()
    }

    /// Configure idle-only keepalive; None disables it. Each managed connection
    /// has at most one synchronous worker. Business frames and heartbeat frames
    /// share one gate; failures invalidate the socket instead of reconnecting
    /// behind a caller's pagination session.
    pub fn set_heartbeat_interval(&mut self, interval: Option<Duration>) -> Result<()> {
        if interval.is_some_and(|value| value.is_zero()) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "心跳间隔必须大于0",
            ));
        }
        if let Some(mut worker) = self.idle_heartbeat.take() {
            worker.stop(false);
        }
        self.heartbeat_interval = interval;
        if let Some(interval) = interval {
            self.idle_heartbeat = Some(Box::new(heartbeat::IdleHeartbeat::start(
                &self.stream,
                Arc::clone(&self.frame_gate),
                interval,
                self.config.timeout.min(network_policy().request_timeout),
                self.observer.clone(),
                self.ip_pool.clone(),
            )?));
        }
        Ok(())
    }

    /// 执行请求，失败时自动重连并重试。重连失败也占用一次尝试，
    /// 仍有预算时继续；`attempts` 包含首试。
    ///
    /// ## 示例
    /// ```ignore
    /// let quotes = tcp.retry(|tcp| quotes.recv_parsed(tcp).copied(), 3)?;
    /// ```
    pub fn retry<T>(
        &mut self,
        mut f: impl FnMut(&mut Tcp) -> Result<T>,
        attempts: usize,
    ) -> Result<T> {
        self.with_timeout(network_policy().request_timeout, |tcp| {
            tcp.retry_until(&mut f, attempts)
        })
    }

    fn retry_until<T>(
        &mut self,
        mut f: impl FnMut(&mut Tcp) -> Result<T>,
        attempts: usize,
    ) -> Result<T> {
        if attempts == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "尝试次数必须大于0",
            ));
        }
        self.ensure_daily_endpoint()?;
        let mut last_err = None;
        for attempt in 0..attempts {
            if let Some(deadline) = self.deadline {
                remaining(deadline)?;
            }
            let ready = if attempt > 0 || !self.is_reusable() {
                self.reconnect()
            } else {
                Ok(())
            };
            match ready.and_then(|_| f(self)) {
                Ok(v) => return Ok(v),
                Err(e) => {
                    self.reusable = false;
                    if frame::is_identity_error(&e) {
                        return Err(e);
                    }
                    if attempt + 1 < attempts {
                        self.backoff(attempt as u64 + 1)?;
                    }
                    last_err = Some(e);
                }
            }
        }
        Err(last_err.unwrap())
    }

    /// 重试退避：第 `n` 次失败后按 `retry_delay_ms × n` 等待（配置为 0 时不等待）。
    fn backoff(&self, failed: u64) -> Result<()> {
        if self.config.retry_delay_ms > 0 {
            self.delay(Duration::from_millis(
                self.config.retry_delay_ms.saturating_mul(failed),
            ))?;
        }
        Ok(())
    }

    pub(crate) fn delay(&self, duration: Duration) -> Result<()> {
        let duration = if let Some(deadline) = self.deadline {
            duration.min(remaining(deadline)?)
        } else {
            duration
        };
        crate::operation::sleep(duration)?;
        if let Some(deadline) = self.deadline {
            remaining(deadline)?;
        }
        Ok(())
    }

    /// 发送并接收字节。需要对接收的字节进行解析（参考 [`Tdx::parse`] 的实现）。
    ///
    /// 方法返回发送和读取的字节数。
    ///
    /// 注意是读取而不是接收的字节数。
    /// 由于每次接收先读取 16 字节，所以返回的元组中，第二个数字应为 16。
    ///
    /// 配置了 [`TcpConfig::auto_reconnect`] 时，请求失败会自动重连并按原请求
    /// 字节重发（行情查询请求幂等，可安全重试）。
    /// 响应前缀、命令或消息号错配返回InvalidData，连接失效且不自动重试。
    pub fn send_recv(&mut self, send: &[u8]) -> Result<(usize, usize)> {
        self.with_timeout(network_policy().request_timeout, |tcp| {
            tcp.send_recv_until(send)
        })
    }

    fn send_recv_until(&mut self, send: &[u8]) -> Result<(usize, usize)> {
        self.ensure_daily_endpoint()?;
        let attempts = self.config.auto_reconnect.saturating_add(1);
        let mut last_err: Option<std::io::Error> = None;
        for attempt in 0..attempts {
            match self.send_recv_once(send) {
                Ok(v) => return Ok(v),
                Err(e) => {
                    if frame::is_identity_error(&e) {
                        return Err(e);
                    }
                    last_err = Some(e);
                    if attempt + 1 < attempts {
                        log::warn!(
                            "行情请求失败（第 {} 次），自动重连后重发: {:?}",
                            attempt + 1,
                            last_err.as_ref().unwrap().kind()
                        );
                        self.backoff(attempt as u64 + 1)?;
                        match self.reconnect() {
                            Ok(()) => continue,
                            Err(re) => {
                                log::error!("自动重连失败: {re}");
                                return Err(re);
                            }
                        }
                    }
                }
            }
        }
        Err(last_err.unwrap_or_else(|| std::io::Error::other("行情请求失败")))
    }

    /// 单次发送并接收（不重试）。
    fn send_recv_once(&mut self, send: &[u8]) -> Result<(usize, usize)> {
        let gate = Arc::clone(&self.frame_gate);
        let _guard = self.lock_frame(&gate)?;
        // Header-only API leaves an outstanding body owned by its caller.
        // Idle heartbeat stays disabled until a complete managed frame succeeds.
        gate.failed.store(true, Ordering::Release);
        self.send_recv_header(send)
    }

    fn send_recv_header(&mut self, send: &[u8]) -> Result<(usize, usize)> {
        let counts = self.send_recv_header_unchecked(send)?;
        frame::matches(&self.recv, send)?;
        Ok(counts)
    }

    fn send_recv_header_unchecked(&mut self, send: &[u8]) -> Result<(usize, usize)> {
        self.reusable = false;
        let mut offset = 0;
        while offset < send.len() {
            self.stream.set_write_timeout(Some(self.poll_timeout()?))?;
            #[cfg(windows)]
            socket_poll::writable(&self.stream, || self.poll_timeout())?;
            match self.stream.write(&send[offset..]) {
                Ok(0) => return Err(std::io::ErrorKind::WriteZero.into()),
                Ok(n) => offset += n,
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error) if self.retry_poll(&error)? => continue,
                Err(error) => return Err(error),
            }
        }
        // 一次性读满 16 字节响应头：单次 read 在 TCP 分段时可能只返回部分字节，
        // 导致后续从错误偏移解析响应长度。
        let mut header = [0; RECV_SIZE];
        self.read_exact_deadline(&mut header)?;
        self.recv = header;
        trace!("send: {:?}\nrecv[16B]: {:?}", send, self);
        Ok((send.len(), RECV_SIZE))
    }

    fn read_frame_once(&mut self, send: &[u8]) -> Result<(Vec<u8>, u16, u16)> {
        let gate = Arc::clone(&self.frame_gate);
        let mut activity = self.lock_frame(&gate)?;
        gate.failed.store(true, Ordering::Release);
        let endpoint = self.stream.peer_addr()?;
        if let Some(observer) = &self.observer {
            if send.len() > u16::MAX as usize {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    "observed request exceeds 65535 bytes",
                ));
            }
            observer.observe(FrameEvent::Request {
                endpoint,
                request: send,
            })?;
        }
        let result: Result<(Vec<u8>, u16, u16)> = (|| {
            self.send_recv_header_unchecked(send)?;
            let identity_error = match frame::matches(&self.recv, send) {
                Ok(()) => None,
                Err(error) if self.observer.is_none() => return Err(error),
                // Keep a complete rejected wire frame when observation is enabled.
                Err(error) => Some(error),
            };
            let deflate = u16_from_le_bytes(&self.recv, 12);
            let inflate = u16_from_le_bytes(&self.recv, 14);
            let mut body = vec![0; deflate as usize];
            if let Err(error) = self.read_exact_deadline(&mut body) {
                // An incomplete bad frame must not become a retryable EOF/timeout.
                return Err(identity_error.unwrap_or(error));
            }
            if let Some(observer) = &self.observer {
                observer.observe(FrameEvent::Response {
                    endpoint,
                    request: send,
                    header: &self.recv,
                    body: &body,
                })?;
            }
            if let Some(error) = identity_error {
                return Err(error);
            }
            self.reusable = true;
            gate.failed.store(false, Ordering::Release);
            *activity = Instant::now();
            Ok((body, deflate, inflate))
        })();
        if let Err(error) = &result {
            self.reusable = false;
            if let Some(observer) = &self.observer {
                observer.observe(FrameEvent::Failed {
                    endpoint,
                    request: send,
                    kind: error.kind(),
                })?;
            }
        }
        result
    }

    fn request_frame(&mut self, send: &[u8], decompress: bool) -> Result<(Vec<u8>, u16, u16)> {
        let start = Instant::now();
        let result = self.with_timeout(network_policy().request_timeout, |tcp| {
            tcp.request_frame_until(send, decompress)
        });
        global().request_result(result.is_ok());
        global().observation(
            start.elapsed(),
            result.as_ref().is_ok_and(|(body, _, _)| body == &[0, 0]),
        );
        if let Ok(address) = self.stream.peer_addr() {
            global().endpoint_result(address, result.is_ok());
            if let Some(opcode) = send.get(10..12) {
                ip::capabilities::observe(
                    address,
                    u16::from_le_bytes([opcode[0], opcode[1]]),
                    self.connection_id().get(),
                    if result.is_ok() {
                        ip::CapabilityStatus::TransportValidated
                    } else {
                        ip::CapabilityStatus::TransportFailed
                    },
                );
            }
        }
        result
    }

    /// All managed complete-frame exchanges validate identity before exposing data.
    pub(crate) fn exchange_checked(&mut self, send: &[u8]) -> Result<Vec<u8>> {
        let (body, _, _) = self.request_frame(send, true)?;
        Ok(body)
    }

    fn request_frame_until(
        &mut self,
        send: &[u8],
        decompress: bool,
    ) -> Result<(Vec<u8>, u16, u16)> {
        self.ensure_daily_endpoint()?;
        for attempt in 0..=self.config.auto_reconnect {
            let ready = if !self.is_reusable() {
                self.reconnect()
            } else {
                Ok(())
            };
            let result = ready.and_then(|_| self.read_frame_once(send)).and_then(
                |(body, deflate, inflate)| {
                    let body = if decompress && deflate != inflate {
                        decode_frame(&self.recv, &body, true)?
                    } else {
                        body
                    };
                    Ok((body, deflate, inflate))
                },
            );
            match result {
                Ok(frame) => return Ok(frame),
                Err(err) => {
                    self.reusable = false;
                    self.frame_gate.failed.store(true, Ordering::Release);
                    if frame::is_identity_error(&err) || attempt == self.config.auto_reconnect {
                        return Err(err);
                    }
                    self.backoff(attempt as u64 + 1)?;
                }
            }
        }
        unreachable!()
    }

    /// 取出裸 socket 后退出受管预算。裸 tcpstream_* 和 get_ref().try_clone()
    /// 也由调用方自行负责连接计数及总体时限。
    pub fn into_inner(mut self) -> (TcpStream, BufTcp, [u8; RECV_SIZE]) {
        if let Some(mut worker) = self.idle_heartbeat.take() {
            worker.stop(false);
        }
        (self.stream, self.buffer, self.recv)
    }

    /// Raw I/O through cloned handles is caller-managed. Disable idle heartbeat
    /// first, or use into_inner(), to transfer exclusive frame ownership.
    pub fn get_ref(&self) -> (&TcpStream, &BufTcp, &[u8]) {
        (&self.stream, &self.buffer, &self.recv)
    }

    pub fn get_ref_recv(&self) -> &[u8] {
        &self.recv
    }

    fn lock_frame<'a>(
        &self,
        gate: &'a heartbeat::FrameGate,
    ) -> Result<std::sync::MutexGuard<'a, Instant>> {
        loop {
            self.io_timeout()?;
            match gate.last_activity.try_lock() {
                Ok(guard) => return Ok(guard),
                Err(std::sync::TryLockError::WouldBlock) => self.delay(Duration::from_millis(1))?,
                Err(std::sync::TryLockError::Poisoned(_)) => {
                    return Err(std::io::Error::other("TCP帧锁已损坏"));
                }
            }
        }
    }
}

pub trait Tdx {
    /// 待发送的字节。所有发送请求的字节由两部分组成：
    /// 1. 固定的默认字节（基本为前半段字节）
    /// 2. 查询所需的变化的字节（基本为后半段字节）
    ///
    /// [`Tdx::SEND`] 的作用为默认有效的字节，即发送字节之后会正常响应且数据可以解析，
    /// 但是并不保证每次响应的数据完全一致。
    /// 比如请求日线时发送字节在当天收盘后是不变的，次日交易日请求得到的数据则可能改变。
    ///
    /// 如果发送请求的字节有误，则无法得到响应
    /// （比如设置了读取超时，无响应情况下会得到：
    /// [`WouldBlock` error][`std::io::ErrorKind::WouldBlock`]）。
    ///
    /// 字节具体的含义见 Implementor 的 Tdx trait 部分的 `SEND` 文档。
    const SEND: &'static [u8];
    /// 描述此次 tcp 连接的用途，目前用于记录日志。
    const TAG: &'static str;
    /// 发送的字节的长度。每种请求所发送的字节长度是已知的。默认为 [`Tdx::SEND`] 的长度。
    const LEN: usize = Self::SEND.len();
    type Item: ?Sized;

    /// 真正发送的字节。
    fn send(&mut self) -> &[u8];

    /// 得到响应的字节。响应的字节长度无法预测。
    fn recv(&mut self, tcp: &mut Tcp) -> Result<Vec<u8>> {
        send_recv_decompress(tcp, self.send(), Self::TAG)
    }

    /// 解析响应的字节。
    fn parse(&mut self, response: Vec<u8>);

    /// 带错误返回的解析入口。旧的 parse 保留用于兼容；内置协议应重写此方法。
    fn try_parse(&mut self, response: Vec<u8>) -> Result<()> {
        self.parse(response);
        Ok(())
    }

    /// 得到和解析响应的字节，并返回解析的数据。
    fn recv_parsed(&mut self, tcp: &mut Tcp) -> Result<&Self::Item> {
        let response = self.recv(tcp)?;
        self.try_parse(response)
            .map_err(|e| std::io::Error::new(e.kind(), format!("{}: {e}", Self::TAG)))?;
        Ok(self.result())
    }

    /// 解析后的结果。可以是引用，也可以是 owned type 。
    fn result(&self) -> &Self::Item;
}

/// 此函数完成以下事情：
/// 1. 接收完整响应，校验前缀、命令和消息号；错配不自动重试
/// 2. 根据响应信息的解压前后长度，进行数据解压
/// 3. 消耗缓冲区的字节（否则下次 read 的内容是上次未读/未消耗的字节），返回有效数据
///
/// 有效数据：包含实际有用信息的数据。
pub fn send_recv_decompress(tcp: &mut Tcp, send: &[u8], tag: &str) -> Result<Vec<u8>> {
    let (buf, _, _) = tcp.request_frame(send, true)?;
    trace!("{tag}: {} 字节", buf.len());
    Ok(buf)
}

/// Read a complete raw wire body after validating response identity.
/// Compression is preserved; identity mismatches invalidate the connection.
// 由于只读取了前 16 字节（TCP_RECV_SIZE），
// 剩下的未读字节需要使用内部缓冲区消耗掉，
// 否则下次 read 的内容是上次的未读字节。
// 如果不使用 BufReader ，那么需要手动 read 剩余字节。
// 对于 TcpStream ，Write::flush 没有做任何事情，所以无需调用。
pub fn send_recv(tcp: &mut Tcp, send: &[u8], tag: &str) -> Result<(Vec<u8>, u16, u16)> {
    let frame = tcp.request_frame(send, false)?;
    trace!("{tag}: {} 字节", frame.0.len());
    Ok(frame)
}

/// 默认的超时值。
pub const TIMEOUT: Duration = Duration::from_secs(5);

/// 快速引入裸tcpstream（从默认当日有效IP池故障转移），设置 [`TIMEOUT`] 超时。
pub fn tcpstream() -> Result<(TcpStream, BufTcp, [u8; RECV_SIZE])> {
    tcpstream_with_timeout(TIMEOUT)
}

/// 从默认当日有效IP池轮换选址，所有尝试共享网络策略的连接总体时限。
pub fn tcpstream_with_timeout(timeout: Duration) -> Result<(TcpStream, BufTcp, [u8; RECV_SIZE])> {
    if timeout.is_zero() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "超时必须大于0",
        ));
    }
    let deadline = deadline_after(network_policy().connect_timeout)?;
    let pool = ip::default_pool();
    let valid = pool.addresses_until(None, deadline)?;
    let mut last_err = None;
    for addr in global().candidates(&valid) {
        remaining(deadline)?;
        if !pool.contains_current(addr) {
            continue;
        }
        let result = open_tcpstream(&addr, timeout.min(remaining(deadline)?));
        global().endpoint_result(addr, result.is_ok());
        match result {
            Ok(x) => return Ok(x),
            Err(e) => last_err = Some((addr, e)),
        }
    }
    let Some((addr, err)) = last_err else {
        return Err(std::io::Error::new(
            std::io::ErrorKind::WouldBlock,
            "当日有效节点均在冷却期",
        ));
    };
    log::warn!("所有行情服务器连接失败，最后尝试: {addr}");
    Err(err)
}

/// 快速引入 tcpstream（指定服务器），设置 [`TIMEOUT`] 超时。
pub fn tcpstream_ip(ip: &SocketAddr) -> Result<(TcpStream, BufTcp, [u8; RECV_SIZE])> {
    tcpstream_ip_with_timeout(ip, TIMEOUT)
}

/// 快速引入裸tcpstream，指定地址也必须通过默认池的当日三轮测试。
pub fn tcpstream_ip_with_timeout(
    ip: &SocketAddr,
    timeout: Duration,
) -> Result<(TcpStream, BufTcp, [u8; RECV_SIZE])> {
    if timeout.is_zero() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "超时必须大于0",
        ));
    }
    ip::default_pool()
        .addresses_until(Some(*ip), deadline_after(network_policy().connect_timeout)?)?;
    open_tcpstream(ip, timeout)
}

fn open_tcpstream(
    ip: &SocketAddr,
    timeout: Duration,
) -> Result<(TcpStream, BufTcp, [u8; RECV_SIZE])> {
    let stream = TcpStream::connect_timeout(ip, timeout)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    let recv = [0; RECV_SIZE];
    // No prefetch beyond each requested header/body slice: the idle heartbeat
    // uses a cloned descriptor under the same complete-frame gate.
    let buffer = BufReader::with_capacity(1, stream.try_clone()?);
    Ok((stream, buffer, recv))
}
