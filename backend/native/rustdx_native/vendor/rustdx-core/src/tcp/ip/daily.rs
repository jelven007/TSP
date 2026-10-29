use super::{
    DAILY_PROBE_ROUNDS, DAILY_PROBE_TIMEOUT, NodeTestResult, ProbeMode, STOCK_IP,
    probe::probe_all_until,
};
use chrono::{DateTime, FixedOffset, NaiveDate, TimeZone, Utc};
use serde::{Deserialize, Serialize};
use std::{
    io,
    net::SocketAddr,
    sync::{
        Arc, Condvar, Mutex, MutexGuard, OnceLock,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

/// 按北京时间标记的测速快照。只有三轮全通过的地址进入 `valid_nodes`。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DailyIpPool {
    pub date: NaiveDate,
    pub tested_at: DateTime<FixedOffset>,
    pub completed_at: DateTime<FixedOffset>,
    pub results: Vec<NodeTestResult>,
    /// 按总耗时中位数升序排列。
    pub valid_nodes: Vec<SocketAddr>,
}

#[derive(Debug)]
struct State {
    initial: Vec<SocketAddr>,
    daily: Option<DailyIpPool>,
    last_error: Option<String>,
}

#[derive(Debug)]
struct Inner {
    mode: ProbeMode,
    group: Option<super::ProbeGroup>,
    state: Mutex<State>,
    changed: Condvar,
    refresh: Mutex<()>,
    started: AtomicBool,
    closed: AtomicBool,
    cancellation: Mutex<Option<crate::operation::OperationContext>>,
}

/// 可由集成项目独立创建或共享的初始池和当日有效池。
///
/// 初始池的增删改查不会把未经测试的新地址加入有效池。删除/修改立即撤下旧地址；
/// 新地址在下次09:00测速或显式 [`Self::refresh`] 后生效。状态为进程内存数据，
/// 集成项目可用 [`Self::list`] 导出并在启动时用 [`Self::new`] 恢复初始列表。
///
/// ```no_run
/// use rustdx_complete::tcp::{ip::IpPool, Tcp, TcpConfig};
/// let pool = IpPool::default();
/// let addr = "119.97.185.59:7709".parse().unwrap();
/// pool.add(addr)?;
/// let today = pool.refresh()?;
/// let config = TcpConfig { ip_pool: Some(pool), ..Default::default() };
/// let tcp = Tcp::with_config(&config)?;
/// # Ok::<(), std::io::Error>(())
/// ```
#[derive(Debug, Clone)]
pub struct IpPool {
    inner: Arc<Inner>,
}

impl PartialEq for IpPool {
    fn eq(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.inner, &other.inner)
    }
}
impl Eq for IpPool {}

impl Default for IpPool {
    fn default() -> Self {
        Self::from_nodes(STOCK_IP.to_vec())
    }
}

impl IpPool {
    pub fn new(addresses: Vec<SocketAddr>) -> io::Result<Self> {
        Self::with_probe_mode(addresses, ProbeMode::SecurityCount)
    }

    /// TSP adaptation: each pool keeps its mode for its entire lifetime.
    pub fn with_probe_mode(addresses: Vec<SocketAddr>, mode: ProbeMode) -> io::Result<Self> {
        validate(&addresses)?;
        Ok(Self::from_nodes_with_mode(addresses, mode))
    }

    pub fn with_probe_group(
        addresses: Vec<SocketAddr>,
        mode: ProbeMode,
        group: super::ProbeGroup,
    ) -> io::Result<Self> {
        validate(&addresses)?;
        Ok(Self::from_nodes_scoped(addresses, mode, Some(group)))
    }

    /// Stop this pool's pending and daily probes. A stopped pool cannot revive.
    pub fn stop(&self) {
        self.inner.closed.store(true, Ordering::Release);
        if let Some(context) = self
            .inner
            .cancellation
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
        {
            context.cancel();
        }
        self.inner.changed.notify_all();
    }

    pub fn probe_mode(&self) -> ProbeMode {
        self.inner.mode
    }

    fn from_nodes(initial: Vec<SocketAddr>) -> Self {
        Self::from_nodes_with_mode(initial, ProbeMode::SecurityCount)
    }

    fn from_nodes_with_mode(initial: Vec<SocketAddr>, mode: ProbeMode) -> Self {
        Self::from_nodes_scoped(initial, mode, None)
    }

    fn from_nodes_scoped(
        initial: Vec<SocketAddr>,
        mode: ProbeMode,
        group: Option<super::ProbeGroup>,
    ) -> Self {
        Self {
            inner: Arc::new(Inner {
                mode,
                group,
                state: Mutex::new(State {
                    initial,
                    daily: None,
                    last_error: None,
                }),
                changed: Condvar::new(),
                refresh: Mutex::new(()),
                started: AtomicBool::new(false),
                closed: AtomicBool::new(false),
                cancellation: Mutex::new(None),
            }),
        }
    }

    fn state(&self) -> MutexGuard<'_, State> {
        self.inner.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// 查询完整初始池，返回独立副本。
    pub fn list(&self) -> Vec<SocketAddr> {
        self.state().initial.clone()
    }

    pub fn get(&self, address: SocketAddr) -> Option<SocketAddr> {
        self.state()
            .initial
            .iter()
            .find(|&&a| a == address)
            .copied()
    }

    /// 重复地址不重复插入，返回false；端口0返回InvalidInput。
    pub fn add(&self, address: SocketAddr) -> io::Result<bool> {
        validate_address(address)?;
        let mut state = self.state();
        if state.initial.contains(&address) {
            return Ok(false);
        }
        state.initial.push(address);
        Ok(true)
    }

    /// 删除不存在的地址返回false；已测通过的旧地址同时失效。
    pub fn remove(&self, address: SocketAddr) -> bool {
        let mut state = self.state();
        let len = state.initial.len();
        state.initial.retain(|&a| a != address);
        let removed = len != state.initial.len();
        prune(&mut state);
        self.inner.changed.notify_all();
        removed
    }

    pub fn update(&self, old: SocketAddr, new: SocketAddr) -> io::Result<()> {
        validate_address(new)?;
        let mut state = self.state();
        let index = state
            .initial
            .iter()
            .position(|&a| a == old)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "初始IP池中没有该地址"))?;
        if old != new && state.initial.contains(&new) {
            return Err(io::Error::new(io::ErrorKind::AlreadyExists, "新地址已存在"));
        }
        state.initial[index] = new;
        prune(&mut state);
        self.inner.changed.notify_all();
        Ok(())
    }

    /// 整体替换初始池，用于恢复集成项目保存的配置。校验失败时保持原池。
    pub fn replace(&self, addresses: Vec<SocketAddr>) -> io::Result<()> {
        validate(&addresses)?;
        let mut state = self.state();
        state.initial = addresses;
        prune(&mut state);
        self.inner.changed.notify_all();
        Ok(())
    }

    /// 读取当前北京时间日期的快照；尚未测速或只有昨天的结果时返回None。
    pub fn snapshot(&self) -> Option<DailyIpPool> {
        if self.inner.closed.load(Ordering::Acquire) {
            return None;
        }
        let today = beijing_now().date_naive();
        self.state()
            .daily
            .as_ref()
            .filter(|d| d.date == today)
            .cloned()
    }

    /// 立即测试当前初始池：三轮、最多八并发、TCP/握手/心跳各3秒，
    /// 并启动后续每天09:00的后台刷新。可在应用启动时主动调用。
    pub fn refresh(&self) -> io::Result<DailyIpPool> {
        self.refresh_until(crate::tcp::governance::deadline_after(
            self.probe_budget()?.saturating_add(Duration::from_secs(5)),
        )?)
    }

    /// 等待刷新锁和全池测速共用绝对截止时间；继承当前任务的取消信号。
    /// 超时或取消不会发布本批次的部分结果。成功后仍启动每日09:00刷新。
    pub fn refresh_until(&self, deadline: Instant) -> io::Result<DailyIpPool> {
        let daily = self
            .refresh_inner(true, Some(deadline))?
            .ok_or_else(|| io::Error::other("未生成当日IP池"))?;
        self.start_daily_refresh()?;
        Ok(daily)
    }

    pub(crate) fn probe_budget(&self) -> io::Result<Duration> {
        let batches = self
            .list()
            .len()
            .div_ceil(super::coordinator::workers_for(self.inner.group));
        let phases = batches
            .checked_mul(DAILY_PROBE_ROUNDS * 3)
            .and_then(|phases| u32::try_from(phases).ok())
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "初始IP池过大"))?;
        DAILY_PROBE_TIMEOUT
            .checked_mul(phases)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "测速预算过大"))
    }

    fn refresh_inner(
        &self,
        force: bool,
        deadline: Option<Instant>,
    ) -> io::Result<Option<DailyIpPool>> {
        let _refresh = match deadline {
            Some(deadline) => crate::tcp::governance::lock_until(&self.inner.refresh, deadline)?,
            None => self.inner.refresh.lock().unwrap_or_else(|e| e.into_inner()),
        };
        let context = crate::operation::OperationContext::new(
            self.probe_budget()?.saturating_add(Duration::from_secs(5)),
            None,
        )?;
        {
            let mut cancellation = crate::tcp::governance::lock_until(
                &self.inner.cancellation,
                deadline.unwrap_or(Instant::now() + Duration::from_secs(5)),
            )?;
            if self.inner.closed.load(Ordering::Acquire) {
                return Err(io::Error::new(io::ErrorKind::Interrupted, "IP池已关闭"));
            }
            *cancellation = Some(context.clone());
        }
        let _scope = context.enter();
        let now = beijing_now();
        let initial = {
            let state = self.state();
            if !force && !needs_refresh(&state, now) {
                return Ok(None);
            }
            state.initial.clone()
        };
        let results = probe_all_until(
            &initial,
            DAILY_PROBE_ROUNDS,
            DAILY_PROBE_TIMEOUT,
            deadline,
            self.inner.mode,
            self.inner.group,
        );
        match results {
            Ok(results) => {
                crate::operation::check()?;
                if let Some(deadline) = deadline {
                    crate::tcp::governance::remaining(deadline)?;
                }
                let completed_at = beijing_now();
                if now.date_naive() != completed_at.date_naive() {
                    return Err(io::Error::new(
                        io::ErrorKind::Interrupted,
                        "测速跨日，需重新测速",
                    ));
                }
                let mut ranked: Vec<_> = results.iter().filter(|result| result.passed()).collect();
                ranked.sort_by_key(|result| result.median_elapsed());
                let valid_nodes = ranked.iter().map(|result| result.address).collect();
                let daily = DailyIpPool {
                    date: now.date_naive(),
                    tested_at: now,
                    completed_at,
                    results,
                    valid_nodes,
                };
                Ok(Some(self.publish(daily)))
            }
            Err(error) => {
                self.state().last_error = Some(error.to_string());
                self.inner.changed.notify_all();
                Err(error)
            }
        }
    }

    fn publish(&self, daily: DailyIpPool) -> DailyIpPool {
        let mut state = self.state();
        state.daily = Some(daily);
        state.last_error = None;
        // 测速期间的删除和修改不得被旧批次结果覆盖。
        prune(&mut state);
        let daily = state.daily.as_ref().expect("刚发布的IP池").clone();
        self.inner.changed.notify_all();
        daily
    }

    /// 启动一个进程内后台线程，每天北京时间09:00刷新；重复调用不会重复启动。
    /// 无当日结果时立即补测。最后一个池引用释放后，线程至多60秒内退出。
    pub fn start_daily_refresh(&self) -> io::Result<()> {
        if self.inner.closed.load(Ordering::Acquire) {
            return Err(io::Error::new(io::ErrorKind::Interrupted, "IP池已关闭"));
        }
        if self.inner.started.swap(true, Ordering::AcqRel) {
            return Ok(());
        }
        let weak = Arc::downgrade(&self.inner);
        let spawned = thread::Builder::new()
            .name("rustdx-daily-ip".into())
            .spawn(move || {
                while let Some(inner) = weak.upgrade() {
                    let pool = Self { inner };
                    if pool.inner.closed.load(Ordering::Acquire) {
                        break;
                    }
                    if let Err(error) = pool.refresh_inner(false, None) {
                        if pool.inner.closed.load(Ordering::Acquire) {
                            break;
                        }
                        log::warn!("当日IP池刷新失败: {error}");
                        let state = pool.state();
                        let _wait = pool
                            .inner
                            .changed
                            .wait_timeout(state, Duration::from_secs(60));
                        continue;
                    }
                    let now = beijing_now();
                    let delay = (next_refresh(now) - now)
                        .to_std()
                        .unwrap_or(Duration::ZERO)
                        .min(Duration::from_secs(60));
                    let state = pool.state();
                    let _wait = pool.inner.changed.wait_timeout(state, delay);
                }
            });
        if let Err(error) = spawned {
            self.inner.started.store(false, Ordering::Release);
            return Err(error);
        }
        Ok(())
    }

    pub(crate) fn addresses_until(
        &self,
        preferred: Option<SocketAddr>,
        deadline: Instant,
    ) -> io::Result<Vec<SocketAddr>> {
        if let Some(address) = preferred
            && self.get(address).is_none()
        {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "指定地址不在初始IP池中",
            ));
        }
        self.start_daily_refresh()?;
        let mut state = self.state();
        loop {
            let remaining = crate::tcp::governance::remaining(deadline)?;
            if !needs_refresh(&state, beijing_now()) {
                let daily = state.daily.as_ref().expect("当日测速结果");
                if let Some(address) = preferred {
                    return if daily.valid_nodes.contains(&address) {
                        Ok(vec![address])
                    } else {
                        Err(io::Error::new(
                            io::ErrorKind::NotConnected,
                            "指定地址未通过当日三轮测速",
                        ))
                    };
                }
                return if daily.valid_nodes.is_empty() {
                    Err(io::Error::new(
                        io::ErrorKind::NotConnected,
                        "当日有效IP池为空",
                    ))
                } else {
                    Ok(daily.valid_nodes.clone())
                };
            }
            if let Some(error) = &state.last_error {
                return Err(io::Error::other(format!("当日IP池测速失败: {error}")));
            }
            self.inner.changed.notify_all();
            state = self
                .inner
                .changed
                .wait_timeout(state, remaining.min(Duration::from_millis(100)))
                .unwrap_or_else(|e| e.into_inner())
                .0;
        }
    }

    pub(crate) fn contains_current(&self, address: SocketAddr) -> bool {
        let state = self.state();
        !self.inner.closed.load(Ordering::Acquire)
            && !needs_refresh(&state, beijing_now())
            && state
                .daily
                .as_ref()
                .is_some_and(|d| d.valid_nodes.contains(&address))
    }

    #[cfg(test)]
    pub(crate) fn for_test(address: SocketAddr) -> Self {
        let pool = Self::from_nodes(vec![address]);
        let now = beijing_now();
        pool.publish(DailyIpPool {
            date: now.date_naive(),
            tested_at: now,
            completed_at: now,
            results: vec![],
            valid_nodes: vec![address],
        });
        pool
    }
}

fn prune(state: &mut State) {
    if let Some(daily) = &mut state.daily {
        daily
            .valid_nodes
            .retain(|address| state.initial.contains(address));
    }
}

fn validate_address(address: SocketAddr) -> io::Result<()> {
    if address.port() == 0 || address.ip().is_unspecified() || address.ip().is_multicast() {
        Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "IP地址或端口无效",
        ))
    } else {
        Ok(())
    }
}

fn validate(addresses: &[SocketAddr]) -> io::Result<()> {
    let mut seen = std::collections::HashSet::new();
    for &address in addresses {
        validate_address(address)?;
        if !seen.insert(address) {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "初始IP池有重复地址",
            ));
        }
    }
    Ok(())
}

pub(super) fn beijing_now() -> DateTime<FixedOffset> {
    Utc::now().with_timezone(&FixedOffset::east_opt(8 * 3600).expect("UTC+8"))
}

fn nine(now: DateTime<FixedOffset>) -> DateTime<FixedOffset> {
    now.timezone()
        .from_local_datetime(&now.date_naive().and_hms_opt(9, 0, 0).expect("09:00"))
        .single()
        .expect("固定UTC+8没有夏令时")
}

fn next_refresh(now: DateTime<FixedOffset>) -> DateTime<FixedOffset> {
    let today = nine(now);
    if now < today {
        today
    } else {
        today + chrono::Duration::days(1)
    }
}

fn needs_refresh(state: &State, now: DateTime<FixedOffset>) -> bool {
    state.daily.as_ref().is_none_or(|daily| {
        daily.date != now.date_naive() || (now >= nine(now) && daily.tested_at < nine(now))
    })
}

static DEFAULT_POOL: OnceLock<IpPool> = OnceLock::new();

/// 进程共享的默认初始池。内置40节点在首次调用时复制到可编辑的池中。
pub fn default_pool() -> &'static IpPool {
    DEFAULT_POOL.get_or_init(IpPool::default)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(day: u32, hour: u32, minute: u32) -> DateTime<FixedOffset> {
        FixedOffset::east_opt(8 * 3600)
            .unwrap()
            .with_ymd_and_hms(2026, 10, day, hour, minute, 0)
            .unwrap()
    }

    fn report(now: DateTime<FixedOffset>, nodes: Vec<SocketAddr>) -> DailyIpPool {
        DailyIpPool {
            date: now.date_naive(),
            tested_at: now,
            completed_at: now,
            results: vec![],
            valid_nodes: nodes,
        }
    }

    #[test]
    fn crud_is_shared_atomic_and_never_admits_an_untested_address() {
        let a = "127.0.0.1:1001".parse().unwrap();
        let b = "127.0.0.1:1002".parse().unwrap();
        let c = "127.0.0.1:1003".parse().unwrap();
        let pool = IpPool::for_test(a);
        let shared = pool.clone();
        assert_eq!(shared.get(a), Some(a));
        assert!(!shared.add(a).unwrap());
        assert!(shared.add(b).unwrap());
        assert!(!pool.contains_current(b));
        assert_eq!(
            pool.update(a, b).unwrap_err().kind(),
            io::ErrorKind::AlreadyExists
        );
        assert_eq!(pool.list(), [a, b]);
        shared.update(a, c).unwrap();
        assert!(!pool.contains_current(a));
        assert!(!pool.contains_current(c));
        assert!(pool.remove(b));
        assert!(!pool.remove(b));
        assert_eq!(
            pool.update(a, b).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        assert!(pool.replace(vec![c, c]).is_err());
        assert!(pool.add("127.0.0.1:0".parse().unwrap()).is_err());
        assert_eq!(pool.list(), [c]);
        pool.replace(vec![]).unwrap();
        assert!(pool.list().is_empty());
        assert_eq!(IpPool::default().list(), STOCK_IP);
    }

    #[test]
    fn scheduler_uses_beijing_nine_and_expires_yesterdays_pool() {
        let mut state = State {
            initial: vec![],
            daily: None,
            last_error: None,
        };
        assert!(needs_refresh(&state, at(24, 8, 0)));
        state.daily = Some(report(at(24, 8, 0), vec![]));
        assert!(!needs_refresh(&state, at(24, 8, 59)));
        assert!(needs_refresh(&state, at(24, 9, 0)));
        state.daily = Some(report(at(24, 9, 0), vec![]));
        assert!(!needs_refresh(&state, at(24, 23, 59)));
        assert!(needs_refresh(&state, at(25, 0, 0)));
        assert_eq!(next_refresh(at(24, 8, 59)), at(24, 9, 0));
        assert_eq!(next_refresh(at(24, 9, 0)), at(25, 9, 0));
        let utc = Utc.with_ymd_and_hms(2026, 10, 23, 16, 0, 0).unwrap();
        assert_eq!(utc.with_timezone(&at(24, 0, 0).timezone()), at(24, 0, 0));
    }

    #[test]
    fn publishing_a_batch_cannot_restore_addresses_removed_during_probing() {
        let a = "127.0.0.1:1001".parse().unwrap();
        let b = "127.0.0.1:1002".parse().unwrap();
        let c = "127.0.0.1:1003".parse().unwrap();
        let pool = IpPool::new(vec![a, b]).unwrap();
        pool.remove(a);
        pool.add(c).unwrap();
        let daily = pool.publish(report(beijing_now(), vec![a, b]));
        assert_eq!(daily.valid_nodes, [b]);
        assert!(pool.contains_current(b));
        assert!(!pool.contains_current(c));
    }

    #[test]
    fn empty_pool_and_unapproved_preferences_fail_without_falling_back_to_seeds() {
        let pool = IpPool::new(vec![]).unwrap();
        pool.publish(report(beijing_now(), vec![]));
        let deadline = Instant::now() + Duration::from_secs(1);
        assert_eq!(
            pool.addresses_until(None, deadline).unwrap_err().kind(),
            io::ErrorKind::NotConnected
        );
        assert_eq!(
            pool.addresses_until(Some(STOCK_IP[0]), deadline)
                .unwrap_err()
                .kind(),
            io::ErrorKind::NotFound
        );
        let pool = IpPool::new(vec![STOCK_IP[0]]).unwrap();
        pool.publish(report(beijing_now(), vec![]));
        assert_eq!(
            pool.addresses_until(Some(STOCK_IP[0]), deadline)
                .unwrap_err()
                .kind(),
            io::ErrorKind::NotConnected
        );
        let pool = IpPool::new(vec![STOCK_IP[0]]).unwrap();
        pool.publish(report(
            beijing_now() - chrono::Duration::days(1),
            vec![STOCK_IP[0]],
        ));
        assert!(pool.snapshot().is_none());
        assert!(!pool.contains_current(STOCK_IP[0]));
    }
}
