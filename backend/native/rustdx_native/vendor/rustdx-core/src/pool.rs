//! 有界同步 TCP 连接池。借用对象持有连接，归还不依赖可移动的数组下标。
use crate::tcp::{Tcp, TcpConfig};
use std::collections::VecDeque;
use std::io;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

#[derive(Debug, Clone)]
pub struct PoolConfig {
    pub max_size: usize,
    /// 最大空闲秒数；0 表示归还后不保留。
    pub max_idle: u64,
    pub max_lifetime: u64,
    /// 借出空闲连接时的心跳超时；0 表示关闭探活。
    pub health_check_timeout: u64,
}

impl Default for PoolConfig {
    fn default() -> Self {
        Self {
            max_size: 3,
            max_idle: 600,
            max_lifetime: 3600,
            health_check_timeout: 5,
        }
    }
}

#[derive(Debug)]
struct IdleConnection {
    tcp: Tcp,
    created_at: Instant,
    returned_at: Instant,
}

#[derive(Debug, Default)]
struct State {
    idle: VecDeque<IdleConnection>,
    /// 包含正在建立、探活和借出的连接。
    active: usize,
    closed: bool,
}

#[derive(Debug)]
struct Inner {
    state: Mutex<State>,
    available: Condvar,
}

impl Inner {
    fn lock(&self) -> io::Result<MutexGuard<'_, State>> {
        self.state
            .lock()
            .map_err(|_| io::Error::other("连接池状态锁中毒"))
    }

    // Drop 和状态查询不能抛出错误；中毒时关闭池，不再复用连接。
    fn cleanup_lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|error| {
            let mut state = error.into_inner();
            state.closed = true;
            state.idle.clear();
            self.available.notify_all();
            state
        })
    }
}

/// 建连/探活失败或提前退出时释放预留槽位，包括 panic unwind。
struct Reservation {
    pool: ConnectionPool,
    armed: bool,
}

impl Drop for Reservation {
    fn drop(&mut self) {
        if self.armed {
            self.pool.release_reservation();
        }
    }
}

#[derive(Debug, Clone)]
pub struct ConnectionPool {
    inner: Arc<Inner>,
    config: PoolConfig,
    tcp_config: TcpConfig,
}

impl ConnectionPool {
    pub fn new(max_size: usize) -> io::Result<Self> {
        Self::with_config(PoolConfig {
            max_size,
            ..Default::default()
        })
    }

    pub fn with_config(config: PoolConfig) -> io::Result<Self> {
        Self::with_tcp_config(config, TcpConfig::default())
    }

    /// 创建池时显式继承客户端服务器、超时与重试策略。
    pub fn with_tcp_config(config: PoolConfig, tcp_config: TcpConfig) -> io::Result<Self> {
        if config.max_size == 0 || config.max_lifetime == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "连接池大小和生命周期必须大于0",
            ));
        }
        Ok(Self {
            inner: Arc::new(Inner {
                state: Mutex::new(State::default()),
                available: Condvar::new(),
            }),
            config,
            tcp_config,
        })
    }

    pub fn get_connection(&self) -> io::Result<PooledConn> {
        self.acquire(None)
    }

    /// 等待槽位、建连及探活共享该总体时限。
    pub fn get_connection_timeout(&self, timeout: Duration) -> io::Result<PooledConn> {
        let deadline = Instant::now()
            .checked_add(timeout)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "等待时间过大"))?;
        self.acquire(Some(deadline))
    }

    fn acquire(&self, deadline: Option<Instant>) -> io::Result<PooledConn> {
        let connection_deadline = deadline.unwrap_or(crate::tcp::governance::deadline_after(
            crate::tcp::network_policy().connect_timeout,
        )?);
        {
            let candidate;
            {
                let mut state = self.inner.lock()?;
                loop {
                    crate::tcp::governance::remaining(connection_deadline)?;
                    if state.closed {
                        return Err(io::Error::new(io::ErrorKind::NotConnected, "连接池已关闭"));
                    }
                    state.idle.retain(|c| {
                        c.created_at.elapsed() < Duration::from_secs(self.config.max_lifetime)
                            && c.returned_at.elapsed() < Duration::from_secs(self.config.max_idle)
                            && c.tcp.is_reusable()
                    });
                    if let Some(conn) = state.idle.pop_front() {
                        candidate = Some(conn);
                        state.active += 1;
                        break;
                    }
                    if state.active < self.config.max_size {
                        candidate = None;
                        state.active += 1; // 先预留容量，随后在锁外建连。
                        break;
                    }
                    let Some(_) = deadline else {
                        return Err(io::Error::new(
                            io::ErrorKind::WouldBlock,
                            "Connection pool exhausted",
                        ));
                    };
                    let remaining = crate::tcp::governance::remaining(connection_deadline)?;
                    state = self
                        .inner
                        .available
                        .wait_timeout(state, remaining.min(Duration::from_millis(20)))
                        .map_err(|_| io::Error::other("连接池等待锁中毒"))?
                        .0;
                }
            }
            let mut reservation = Reservation {
                pool: self.clone(),
                armed: true,
            };
            let created_at = candidate
                .as_ref()
                .map_or_else(Instant::now, |c| c.created_at);
            let connection = match candidate {
                Some(mut conn) => {
                    if self.config.health_check_timeout > 0 {
                        conn.tcp
                            .with_deadline(connection_deadline, |tcp| {
                                tcp.check_health(Duration::from_secs(
                                    self.config.health_check_timeout,
                                ))
                            })
                            .map(|()| conn.tcp)
                    } else {
                        Ok(conn.tcp)
                    }
                }
                None => Tcp::with_config_until(&self.tcp_config, connection_deadline),
            };
            let tcp = connection?;
            crate::tcp::governance::remaining(connection_deadline)?;
            if self.inner.lock()?.closed {
                return Err(io::Error::new(io::ErrorKind::NotConnected, "连接池已关闭"));
            }
            let conn = PooledConn {
                pool: self.clone(),
                tcp: Some(tcp),
                created_at,
            };
            reservation.armed = false;
            Ok(conn)
        }
    }

    fn release_reservation(&self) {
        let mut state = self.inner.cleanup_lock();
        state.active = state.active.saturating_sub(1);
        self.inner.available.notify_one();
    }

    pub fn stats(&self) -> PoolStats {
        let state = self.inner.cleanup_lock();
        let active = if state.closed { 0 } else { state.active };
        PoolStats {
            total: active + state.idle.len(),
            active,
            idle: state.idle.len(),
            max_size: self.config.max_size,
        }
    }

    /// 永久关闭池；已经借出的连接在归还时丢弃。
    pub fn close(&self) {
        let mut state = self.inner.cleanup_lock();
        state.closed = true;
        state.idle.clear();
        self.inner.available.notify_all();
    }
}

pub struct PooledConn {
    pool: ConnectionPool,
    tcp: Option<Tcp>,
    created_at: Instant,
}

impl PooledConn {
    pub fn get_mut(&mut self) -> &mut Tcp {
        self.tcp.as_mut().expect("连接已被标记为不可复用")
    }

    pub fn execute<F, R>(&mut self, f: F) -> R
    where
        F: FnOnce(&mut Tcp) -> R,
    {
        f(self.get_mut())
    }

    /// 请求失败后调用；归还时释放容量而不保存失效连接。
    pub fn invalidate(&mut self) {
        self.tcp = None;
    }
}

impl Drop for PooledConn {
    fn drop(&mut self) {
        let mut state = self.pool.inner.cleanup_lock();
        state.active = state.active.saturating_sub(1);
        if !state.closed
            && self.pool.config.max_idle > 0
            && self.created_at.elapsed() < Duration::from_secs(self.pool.config.max_lifetime)
            && let Some(tcp) = self.tcp.take()
            && tcp.is_reusable()
        {
            state.idle.push_back(IdleConnection {
                tcp,
                created_at: self.created_at,
                returned_at: Instant::now(),
            });
        }
        self.pool.inner.available.notify_one();
    }
}

#[derive(Debug, Clone)]
pub struct PoolStats {
    pub total: usize,
    pub active: usize,
    pub idle: usize,
    pub max_size: usize,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tcp::tests::mock_server;

    #[test]
    fn cleanup_does_not_move_borrowed_connections() {
        let (config, server) = mock_server(3, |_| vec![0, 0]);
        let pool = ConnectionPool::with_tcp_config(
            PoolConfig {
                max_size: 2,
                max_idle: 0,
                health_check_timeout: 0,
                ..Default::default()
            },
            config,
        )
        .unwrap();
        let first = pool.get_connection().unwrap();
        let second = pool.get_connection().unwrap();
        assert_eq!(
            pool.get_connection().err().unwrap().kind(),
            io::ErrorKind::WouldBlock
        );
        drop(first);
        let third = pool.get_connection().unwrap();
        drop(second);
        drop(third);
        assert_eq!(pool.stats().active, 0);
        assert_eq!(pool.stats().total, 0);
        pool.close();
        server.join().unwrap();
    }

    #[test]
    fn reuse_wait_and_close() {
        let (config, server) = mock_server(1, |_| vec![0, 0]);
        let pool = ConnectionPool::with_tcp_config(
            PoolConfig {
                max_size: 1,
                health_check_timeout: 0,
                ..Default::default()
            },
            config,
        )
        .unwrap();
        let conn = pool.get_connection().unwrap();
        assert_eq!(
            pool.get_connection_timeout(Duration::from_millis(5))
                .err()
                .unwrap()
                .kind(),
            io::ErrorKind::TimedOut
        );
        drop(conn);
        assert_eq!(pool.stats().idle, 1);
        let conn = pool.get_connection().unwrap();
        pool.close();
        assert_eq!(
            pool.get_connection().err().unwrap().kind(),
            io::ErrorKind::NotConnected
        );
        drop(conn);
        assert_eq!(pool.stats().total, 0);
        server.join().unwrap();
    }

    #[test]
    fn invalid_configuration() {
        assert!(ConnectionPool::new(0).is_err());
    }

    #[test]
    fn occupied_pool_observes_cancellation_without_a_returned_connection() {
        let (config, server) = mock_server(1, |_| vec![0, 0]);
        let pool = ConnectionPool::with_tcp_config(
            PoolConfig {
                max_size: 1,
                health_check_timeout: 0,
                ..Default::default()
            },
            config,
        )
        .unwrap();
        let holder = pool.get_connection().unwrap();
        let task = crate::operation::OperationContext::new(Duration::from_secs(5), None).unwrap();
        let worker_task = task.clone();
        let worker_pool = pool.clone();
        let (ready_tx, ready_rx) = std::sync::mpsc::channel();
        let (done_tx, done_rx) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            let _guard = worker_task.enter();
            ready_tx.send(()).unwrap();
            done_tx
                .send(
                    worker_pool
                        .get_connection_timeout(Duration::from_secs(3))
                        .err()
                        .unwrap()
                        .kind(),
                )
                .unwrap();
        });
        ready_rx.recv().unwrap();
        assert!(done_rx.recv_timeout(Duration::from_millis(30)).is_err());
        task.cancel();
        assert_eq!(
            done_rx.recv_timeout(Duration::from_millis(250)).unwrap(),
            io::ErrorKind::Interrupted
        );
        assert_eq!(pool.stats().active, 1);
        worker.join().unwrap();
        drop(holder);
        assert_eq!(pool.stats().active, 0);
        pool.close();
        server.join().unwrap();
    }

    #[test]
    fn waiting_uses_nested_parent_deadline_and_close_wakes_all() {
        let pool = ConnectionPool::new(1).unwrap();
        // 一个正在建立或借出的连接占满槽位，无需远端来触发等待路径。
        pool.inner.state.lock().unwrap().active = 1;
        let parent =
            crate::operation::OperationContext::new(Duration::from_millis(60), None).unwrap();
        let worker_pool = pool.clone();
        let (tx, rx) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            let _parent = parent.enter();
            let child =
                crate::operation::OperationContext::new(Duration::from_secs(10), None).unwrap();
            let _child = child.enter();
            tx.send(
                worker_pool
                    .get_connection_timeout(Duration::from_secs(3))
                    .err()
                    .unwrap()
                    .kind(),
            )
            .unwrap();
        });
        assert_eq!(
            rx.recv_timeout(Duration::from_millis(300)).unwrap(),
            io::ErrorKind::TimedOut
        );
        worker.join().unwrap();
        assert_eq!(pool.stats().active, 1);
        let waiters: Vec<_> = (0..3)
            .map(|_| {
                let pool = pool.clone();
                std::thread::spawn(move || {
                    pool.get_connection_timeout(Duration::from_secs(3))
                        .err()
                        .unwrap()
                        .kind()
                })
            })
            .collect();
        pool.close();
        for waiter in waiters {
            assert_eq!(waiter.join().unwrap(), io::ErrorKind::NotConnected);
        }
        assert_eq!(pool.stats().total, 0);
    }

    #[test]
    fn reservation_releases_on_unwind_and_poisoned_cleanup_does_not_panic() {
        let pool = ConnectionPool::new(1).unwrap();
        pool.inner.state.lock().unwrap().active = 1;
        let _ = std::panic::catch_unwind(|| {
            let _reservation = Reservation {
                pool: pool.clone(),
                armed: true,
            };
            panic!("test build failure");
        });
        assert_eq!(pool.stats().active, 0);
        let _ = std::panic::catch_unwind(|| {
            let _state = pool.inner.state.lock().unwrap();
            panic!("test poison");
        });
        assert_eq!(
            pool.get_connection().err().unwrap().kind(),
            io::ErrorKind::Other
        );
        pool.close();
        assert_eq!(pool.stats().total, 0);
    }
}
