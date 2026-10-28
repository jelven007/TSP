//! A synchronous, single-owner 0547 session. No query pool owns this socket.
use super::{
    BasicQuote, PrecisionSnapshot, QuoteRefresh, RefreshCursor, RefreshRequest, command_request::*,
    decode_quote_refresh,
};
use crate::tcp::{
    FrameEvent, FrameObserver, Tcp, TcpConfig, decode_frame, frame,
    governance::{deadline_after, remaining},
};
use chrono::{DateTime, Utc};
use serde::Serialize;
use std::{
    collections::{BTreeMap, VecDeque},
    io::{self, Read, Write},
    net::{Shutdown, SocketAddr},
    sync::Arc,
    time::{Duration, Instant},
};

#[derive(Debug, Clone, Copy)]
pub struct SubscriptionOptions {
    pub max_queue_frames: usize,
    pub max_queue_bytes: usize,
    pub refresh_timeout: Duration,
}
impl Default for SubscriptionOptions {
    fn default() -> Self {
        Self {
            max_queue_frames: 256,
            max_queue_bytes: 8 * 1024 * 1024,
            refresh_timeout: crate::tcp::network_policy().request_timeout,
        }
    }
}
impl SubscriptionOptions {
    pub(crate) fn validate(self) -> io::Result<()> {
        if self.max_queue_frames == 0
            || self.max_queue_frames > 256
            || self.max_queue_bytes == 0
            || self.max_queue_bytes > 8 * 1024 * 1024
            || self.refresh_timeout.is_zero()
            || Instant::now().checked_add(self.refresh_timeout).is_none()
        {
            return Err(input("订阅预算须为1..256帧/1..8MiB且超时须为正并可表示"));
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum DepthSyncStatus {
    AwaitingBaseline,
    Synchronized,
    Invalid,
    Closed,
}
#[derive(Debug, Clone, Serialize)]
pub struct DepthState {
    pub symbol: CommandSymbol,
    pub quote: Option<BasicQuote>,
    pub cursor: u32,
    pub received_at: Option<DateTime<Utc>>,
}
#[derive(Debug, Clone, Serialize)]
pub struct DepthSnapshot {
    pub status: DepthSyncStatus,
    pub connection_id: Option<u64>,
    pub generation: u64,
    pub states: Vec<DepthState>,
    pub queued_frames: usize,
    pub queued_bytes: usize,
}
#[derive(Debug, Clone)]
pub struct QuotePush {
    pub message_id: u32,
    pub connection_id: u64,
    pub generation: u64,
    pub received_at: DateTime<Utc>,
    pub header: [u8; 16],
    pub wire_body: Arc<[u8]>,
    pub refresh: QuoteRefresh,
    resident_bytes: usize,
}

/// Explicit source distinction for nodes that only answer refresh requests.
/// Requested refreshes never count as evidence of passive server push.
#[derive(Debug, Clone)]
pub enum QuoteUpdate {
    PassivePush(QuotePush),
    RequestedRefresh(QuoteRefresh),
}

pub trait PushObserver: std::fmt::Debug + Send + Sync {
    fn observe(&self, endpoint: SocketAddr, push: &QuotePush) -> io::Result<()>;
}

#[derive(Debug)]
pub struct QuoteSubscription {
    tcp: Option<Tcp>,
    config: TcpConfig,
    observer: Option<Arc<dyn FrameObserver>>,
    push_observer: Option<Arc<dyn PushObserver>>,
    metadata: Arc<PrecisionSnapshot>,
    options: SubscriptionOptions,
    states: BTreeMap<CommandSymbol, DepthState>,
    staging: Vec<u8>,
    queue: VecDeque<QuotePush>,
    queued_bytes: usize,
    status: DepthSyncStatus,
    next_id: u32,
    generation: u64,
}
impl QuoteSubscription {
    pub fn with_config(
        config: &TcpConfig,
        stocks: &[(u16, &str)],
        metadata: Arc<PrecisionSnapshot>,
        options: SubscriptionOptions,
    ) -> io::Result<Self> {
        let request = RefreshRequest::from_stocks(stocks)?;
        options.validate()?;
        Self::from_tcp(Tcp::with_config(config)?, request, metadata, options)
    }
    pub fn from_tcp(
        mut tcp: Tcp,
        request: RefreshRequest,
        metadata: Arc<PrecisionSnapshot>,
        options: SubscriptionOptions,
    ) -> io::Result<Self> {
        options.validate()?;
        let mut states = BTreeMap::new();
        for item in request.items() {
            if !metadata.is_fresh(item.symbol.market(), Utc::now()) {
                return Err(input("订阅精度目录过期"));
            }
            metadata.scale(item.symbol.market(), item.symbol.code())?;
            states.insert(
                item.symbol.clone(),
                DepthState {
                    symbol: item.symbol.clone(),
                    quote: None,
                    cursor: item.cursor,
                    received_at: None,
                },
            );
        }
        tcp.set_heartbeat_interval(None)?;
        Ok(Self {
            config: tcp.config().clone(),
            observer: tcp.observer.clone(),
            push_observer: None,
            tcp: Some(tcp),
            metadata,
            options,
            states,
            staging: Vec::new(),
            queue: VecDeque::new(),
            queued_bytes: 0,
            status: DepthSyncStatus::AwaitingBaseline,
            next_id: 1,
            generation: 0,
        })
    }
    pub fn set_push_observer(&mut self, observer: Option<Arc<dyn PushObserver>>) {
        self.push_observer = observer;
    }
    fn is_healthy(&self) -> bool {
        self.tcp.as_ref().is_some_and(Tcp::is_reusable)
            && self
                .states
                .keys()
                .all(|s| self.metadata.is_fresh(s.market(), Utc::now()))
    }
    pub fn snapshot(&self) -> DepthSnapshot {
        DepthSnapshot {
            status: if !self.is_healthy() && self.status != DepthSyncStatus::Closed {
                DepthSyncStatus::Invalid
            } else {
                self.status
            },
            connection_id: self.tcp.as_ref().map(|t| t.connection_id().get()),
            generation: self.generation,
            states: self.states.values().cloned().collect(),
            queued_frames: self.queue.len(),
            queued_bytes: self.queued_bytes,
        }
    }
    /// Business consumers must use this gate after every refresh/push/rebuild.
    /// A successful rebuild can still have an incomplete baseline.
    pub fn synchronized_snapshot(&self) -> io::Result<DepthSnapshot> {
        self.healthy()?;
        if self.status != DepthSyncStatus::Synchronized
            || self.states.values().any(|state| state.quote.is_none())
        {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "0547全部订阅证券的完整基线尚未恢复",
            ));
        }
        Ok(self.snapshot())
    }
    fn healthy(&self) -> io::Result<&Tcp> {
        crate::operation::check()?;
        if self.status != DepthSyncStatus::Closed && !self.is_healthy() {
            return Err(io::Error::new(
                io::ErrorKind::NotConnected,
                "订阅会话失效，请rebuild",
            ));
        }
        self.tcp
            .as_ref()
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotConnected, "订阅已关闭"))
    }
    fn request(&self) -> io::Result<RefreshRequest> {
        RefreshRequest::new(
            self.states
                .values()
                .map(|s| RefreshCursor {
                    symbol: s.symbol.clone(),
                    cursor: s.cursor,
                })
                .collect(),
        )
    }
    pub fn refresh(&mut self) -> io::Result<QuoteRefresh> {
        let start = Instant::now();
        let result = self.refresh_inner();
        crate::tcp::observe_request(
            start.elapsed(),
            result.is_ok(),
            result.as_ref().is_ok_and(|r| r.page.records.is_empty()),
        );
        if result.is_err() {
            self.invalidate();
        }
        result
    }
    fn refresh_inner(&mut self) -> io::Result<QuoteRefresh> {
        let deadline = deadline_after(self.options.refresh_timeout)?;
        // Never reuse an id in one session. Rebuild before wraparound.
        let id = self.next_id;
        self.next_id = self
            .next_id
            .checked_add(1)
            .ok_or_else(|| invalid("订阅消息号耗尽，请重建"))?;
        let request = self.request()?;
        let send = request.encode(id)?;
        // A frame already started before this request cannot be its response,
        // even when the unsolicited message id coincides with the new id.
        let mut pre_request_frame = !self.staging.is_empty();
        let tcp = self.healthy()?;
        let endpoint = tcp.get_ref().0.peer_addr()?;
        if let Some(observer) = &self.observer {
            observer.observe(FrameEvent::Request {
                endpoint,
                request: &send,
            })?;
        }
        let mut offset = 0;
        while offset < send.len() {
            let tcp = self.healthy()?;
            let mut stream = tcp.get_ref().0;
            stream.set_write_timeout(Some(remaining(deadline)?.min(Duration::from_millis(200))))?;
            match stream.write(&send[offset..]) {
                Ok(0) => return Err(io::ErrorKind::WriteZero.into()),
                Ok(n) => offset += n,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
                    ) =>
                {
                    remaining(deadline)?;
                }
                Err(e) => return Err(e),
            }
        }
        loop {
            let Some((header, body)) = self.read_frame(deadline)? else {
                return Err(io::ErrorKind::TimedOut.into());
            };
            let (opcode, message) = frame::identity(&header)?;
            if opcode != super::QUOTE_REFRESH_COMMAND {
                return Err(invalid("订阅连接收到其他命令"));
            }
            let is_response = message == id && !pre_request_frame;
            pre_request_frame = false;
            if is_response {
                if let Some(observer) = &self.observer {
                    observer.observe(FrameEvent::Response {
                        endpoint,
                        request: &send,
                        header: &header,
                        body: &body,
                    })?;
                }
                let bytes = decode_frame(&header, &body, true)?;
                let mut refresh = decode_quote_refresh(&request, &bytes, &self.metadata)?;
                if resident_bytes(body.len(), bytes.len(), &refresh)
                    > self
                        .options
                        .max_queue_bytes
                        .saturating_sub(self.queued_bytes)
                {
                    return Err(io::Error::other("0547推送队列溢出，需重建基线"));
                }
                refresh.page.source.attach(self.healthy()?)?;
                self.merge(&refresh)?;
                return Ok(refresh);
            }
            self.offer(header, body, message)?;
        }
    }
    /// A local poll timeout returns None and retains any partially read frame.
    /// Cancellation or endpoint revocation invalidates and closes the session.
    pub fn poll_push(&mut self, timeout: Duration) -> io::Result<Option<QuotePush>> {
        let result = (|| {
            self.healthy()?;
            if let Some(push) = self.queue.pop_front() {
                self.queued_bytes -= push.resident_bytes;
                return Ok(Some(push));
            }
            let deadline = deadline_after(timeout)?;
            let Some((header, body)) = self.read_frame(deadline)? else {
                return Ok(None);
            };
            let (opcode, id) = frame::identity(&header)?;
            if opcode != super::QUOTE_REFRESH_COMMAND {
                return Err(invalid("推送命令错误"));
            }
            let push = self.decode_push(header, body, id)?;
            if let Some(observer) = &self.push_observer {
                observer.observe(self.healthy()?.get_ref().0.peer_addr()?, &push)?;
            }
            self.merge(&push.refresh)?;
            Ok(Some(push))
        })();
        if result.is_err() {
            self.invalidate();
        }
        result
    }

    /// Wait for a passive update, then explicitly refresh if the node is idle.
    /// Initial or rebuilt sessions request a baseline immediately. No background
    /// thread, heartbeat, reconnect or change to poll_push's passive contract.
    pub fn poll_update(&mut self, idle_timeout: Duration) -> io::Result<QuoteUpdate> {
        if idle_timeout.is_zero() {
            return Err(input("持续更新的空闲等待须大于0，避免忙循环"));
        }
        if self.status == DepthSyncStatus::AwaitingBaseline {
            return self.refresh().map(QuoteUpdate::RequestedRefresh);
        }
        match self.poll_push(idle_timeout)? {
            Some(push) => Ok(QuoteUpdate::PassivePush(push)),
            None => self.refresh().map(QuoteUpdate::RequestedRefresh),
        }
    }

    fn read_frame(&mut self, deadline: Instant) -> io::Result<Option<([u8; 16], Vec<u8>)>> {
        loop {
            self.healthy()?;
            let target = if self.staging.len() < 16 {
                16
            } else {
                let header: [u8; 16] = self.staging[..16]
                    .try_into()
                    .map_err(|_| invalid("订阅头长度"))?;
                frame::identity(&header)?;
                16 + usize::from(u16::from_le_bytes([header[12], header[13]]))
            };
            if target
                > self
                    .options
                    .max_queue_bytes
                    .saturating_sub(self.queued_bytes)
            {
                return Err(io::Error::other("0547推送队列溢出，需重建基线"));
            }
            if self.staging.len() == target && target >= 16 {
                let header = self.staging[..16]
                    .try_into()
                    .map_err(|_| invalid("订阅头长度"))?;
                let body = self.staging[16..].to_vec();
                self.staging.clear();
                return Ok(Some((header, body)));
            }
            if Instant::now() >= deadline {
                return Ok(None);
            }
            let timeout = remaining(deadline)?.min(Duration::from_millis(100));
            let mut buffer = [0; 4096];
            let need = (target - self.staging.len()).min(buffer.len());
            let mut stream = self.healthy()?.get_ref().0;
            stream.set_read_timeout(Some(timeout))?;
            match stream.read(&mut buffer[..need]) {
                Ok(0) => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "订阅连接关闭")),
                Ok(n) => self.staging.extend_from_slice(&buffer[..n]),
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::TimedOut
                            | io::ErrorKind::WouldBlock
                            | io::ErrorKind::Interrupted
                    ) =>
                {
                    continue;
                }
                Err(e) => return Err(e),
            }
        }
    }
    fn decode_push(
        &self,
        header: [u8; 16],
        body: Vec<u8>,
        message_id: u32,
    ) -> io::Result<QuotePush> {
        let bytes = decode_frame(&header, &body, true)?;
        let mut refresh = decode_quote_refresh(&self.request()?, &bytes, &self.metadata)?;
        let tcp = self.healthy()?;
        refresh.page.source.attach(tcp)?;
        let resident_bytes = resident_bytes(body.len(), bytes.len(), &refresh);
        if resident_bytes > self.options.max_queue_bytes {
            return Err(io::Error::other("0547推送队列溢出，需重建基线"));
        }
        Ok(QuotePush {
            message_id,
            connection_id: tcp.connection_id().get(),
            generation: self.generation,
            received_at: Utc::now(),
            header,
            wire_body: body.into(),
            refresh,
            resident_bytes,
        })
    }
    fn offer(&mut self, header: [u8; 16], body: Vec<u8>, id: u32) -> io::Result<()> {
        let push = self.decode_push(header, body, id)?;
        if self.queue.len() >= self.options.max_queue_frames
            || push.resident_bytes
                > self
                    .options
                    .max_queue_bytes
                    .saturating_sub(self.queued_bytes)
        {
            return Err(io::Error::other("0547推送队列溢出，需重建基线"));
        }
        if let Some(observer) = &self.push_observer {
            observer.observe(self.healthy()?.get_ref().0.peer_addr()?, &push)?;
        }
        self.merge(&push.refresh)?;
        self.queued_bytes += push.resident_bytes;
        self.queue.push_back(push);
        Ok(())
    }
    fn merge(&mut self, refresh: &QuoteRefresh) -> io::Result<()> {
        // Validate the whole page before publishing any state.
        for q in &refresh.page.records {
            let symbol = CommandSymbol::new(u16::from(q.market_raw), &q.code)?;
            let state = self
                .states
                .get(&symbol)
                .ok_or_else(|| invalid("推送包含未订阅证券"))?;
            let cursor = q.update_cursor.ok_or_else(|| invalid("推送缺少游标"))?;
            if cursor < state.cursor {
                return Err(invalid("0547游标回退，需重建"));
            }
            if q.bids.len() != 5 || q.asks.len() != 5 {
                return Err(invalid("0547缺少完整五档"));
            }
        }
        for q in &refresh.page.records {
            let symbol = CommandSymbol::new(u16::from(q.market_raw), &q.code)?;
            let state = self
                .states
                .get_mut(&symbol)
                .ok_or_else(|| invalid("订阅状态不存在"))?;
            state.cursor = q.update_cursor.ok_or_else(|| invalid("推送游标缺失"))?;
            state.quote = Some(q.clone());
            state.received_at = Some(Utc::now());
        }
        self.status = if self.states.values().all(|s| s.quote.is_some()) {
            DepthSyncStatus::Synchronized
        } else {
            DepthSyncStatus::AwaitingBaseline
        };
        Ok(())
    }
    fn invalidate(&mut self) {
        if let Some(tcp) = self.tcp.take() {
            let _ = tcp.get_ref().0.shutdown(Shutdown::Both);
        }
        self.status = DepthSyncStatus::Invalid;
        self.queue.clear();
        self.queued_bytes = 0;
        self.staging.clear();
    }
    /// Reopen through the configured daily pool, reset cursors, then fetch a
    /// baseline. Pass fresh metadata after midnight or precision changes.
    pub fn rebuild(&mut self, metadata: Arc<PrecisionSnapshot>) -> io::Result<QuoteRefresh> {
        self.invalidate();
        self.generation = self
            .generation
            .checked_add(1)
            .ok_or_else(|| invalid("订阅代次耗尽"))?;
        for state in self.states.values_mut() {
            state.quote = None;
            state.cursor = 0;
            state.received_at = None;
        }
        self.metadata = metadata;
        let result = (|| {
            for symbol in self.states.keys() {
                if !self.metadata.is_fresh(symbol.market(), Utc::now()) {
                    return Err(input("订阅精度目录过期"));
                }
                self.metadata.scale(symbol.market(), symbol.code())?;
            }
            let mut tcp = match &self.observer {
                Some(observer) => {
                    Tcp::with_config_and_observer(&self.config, Arc::clone(observer))?
                }
                None => Tcp::with_config(&self.config)?,
            };
            tcp.set_heartbeat_interval(None)?;
            self.tcp = Some(tcp);
            self.next_id = 1;
            self.status = DepthSyncStatus::AwaitingBaseline;
            self.refresh()
        })();
        if result.is_err() {
            self.invalidate();
        }
        result
    }
    pub fn close(&mut self) {
        self.invalidate();
        self.status = DepthSyncStatus::Closed;
    }
}
impl Drop for QuoteSubscription {
    fn drop(&mut self) {
        self.close();
    }
}

// Include raw/transformed payloads, owned record/tail/depth/symbol buffers
// and queue bookkeeping. Response decoding uses the same conservative bound.
fn resident_bytes(wire: usize, decoded: usize, refresh: &QuoteRefresh) -> usize {
    wire + decoded * 2
        + 16
        + std::mem::size_of::<QuotePush>()
        + refresh
            .page
            .records
            .iter()
            .map(|q| {
                std::mem::size_of::<BasicQuote>()
                    + q.tail_raw.len()
                    + q.code.len()
                    + (q.bids.capacity() + q.asks.capacity())
                        * std::mem::size_of::<super::QuoteLevel>()
            })
            .sum::<usize>()
        + refresh.requested.len() * (std::mem::size_of::<CommandSymbol>() + 6)
}
