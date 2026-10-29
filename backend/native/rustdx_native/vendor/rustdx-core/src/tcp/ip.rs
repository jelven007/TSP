//! 服务器地址管理与连通性检测。对应于 pytdx/mootdx 的 `consts.py` / `best_ip`。
//!
//! 服务器列表主要来自 [mootdx](https://github.com/mootdx/mootdx) `consts.py` 的
//! `HQ_HOSTS`（云服务器，持续维护），并保留原先实测可用的电信主站。
//!
//! [`default_pool`] 保留可编辑的初始池，每天北京时间09:00以三轮、八并发、
//! 各阶段三秒测速生成当日池。首次业务连接无当日结果时后台补测；
//! 所有业务连接（含指定地址、重连及连接池复用）仅使用当日通过的节点。
//! [`check_alive`] 等函数仅用于诊断，不会发布或替换当日有效池。

use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::time::Duration;

pub(crate) mod capabilities;
mod coordinator;
mod daily;
mod probe;
pub use capabilities::{CapabilityObservation, CapabilityStatus, capability_observation};
pub use coordinator::{ProbeGroup, ProbePolicy, ProbeStats, configure_probes, probe_stats};
pub use daily::{DailyIpPool, IpPool, default_pool};
pub use probe::{
    DAILY_PROBE_ROUNDS, DAILY_PROBE_TIMEOUT, DAILY_PROBE_WORKERS, NodeTestResult, ProbeFailure,
    ProbeMode, ProbeRound, ProbeStage,
};

/// 保留的40个内置初始地址。运行时增删改查请使用 [`default_pool`] / [`IpPool`]。
pub static STOCK_IP: [SocketAddr; 40] = [
    // 原列表中实测长期可用的电信主站
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(115, 238, 56, 198)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(180, 153, 18, 170)), 7709),
    // mootdx HQ_HOSTS（深圳双线主站）
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(110, 41, 147, 114)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(8, 129, 13, 54)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(120, 24, 149, 49)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(47, 113, 94, 204)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(8, 129, 174, 169)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(110, 41, 154, 219)), 7709),
    // 上海双线主站
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(124, 70, 176, 52)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(47, 100, 236, 28)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(101, 133, 214, 242)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(47, 116, 21, 80)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(47, 116, 105, 28)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(124, 70, 199, 56)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(106, 14, 201, 131)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(106, 14, 190, 242)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(121, 36, 225, 169)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(123, 60, 70, 228)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(123, 60, 73, 44)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(124, 70, 133, 119)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(124, 71, 187, 72)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(124, 71, 187, 122)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(123, 60, 84, 66)), 7709),
    // 北京双线主站
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(121, 36, 54, 217)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(121, 36, 81, 195)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(123, 249, 15, 60)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(124, 70, 75, 113)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(120, 46, 186, 223)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(124, 70, 22, 210)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(139, 9, 133, 247)), 7709),
    // 广州双线主站
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(124, 71, 85, 110)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(139, 9, 51, 18)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(139, 159, 239, 163)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(124, 71, 9, 153)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(116, 205, 163, 254)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(116, 205, 171, 132)), 7709),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(116, 205, 183, 150)), 7709),
    // 其他
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(119, 97, 185, 59)), 7709), // 武汉电信主站
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(47, 107, 64, 168)), 7709), // 深圳双线主站
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(47, 107, 228, 47)), 7719), // 深圳双线主站（7719 端口）
];

/// 依次探测服务器列表，返回可以建立 TCP 连接的地址（保持列表顺序）。
///
/// 对应 mootdx 的 `check_server`。结果可用于挑选可用的服务器，
/// 传给 [`Tcp::with_config`](crate::tcp::Tcp::with_config)。
///
/// ## 示例
/// ```no_run
/// use rustdx_complete::tcp::{ip, Tcp, TcpConfig};
/// use std::time::Duration;
///
/// let today = ip::default_pool().refresh()?;
/// if let Some(fastest) = today.valid_nodes.first() {
///     let config = TcpConfig { timeout: Duration::from_secs(5), ip: Some(*fastest), ..Default::default() };
///     let mut tcp = Tcp::with_config(&config).unwrap();
/// }
/// # Ok::<(), std::io::Error>(())
/// ```
pub fn check_alive(timeout: Duration) -> Vec<SocketAddr> {
    let initial = default_pool().list();
    let mut alive = Vec::with_capacity(initial.len());
    for addr in &initial {
        if tcp_connect_ok(addr, timeout) {
            alive.push(*addr);
        }
    }
    alive
}

/// 探测一台服务器是否可以在超时时间内建立 TCP 连接。
pub fn tcp_connect_ok(addr: &SocketAddr, timeout: Duration) -> bool {
    let result = (|| {
        let deadline = crate::tcp::governance::deadline_after(timeout)?;
        let _permit = coordinator::acquire_at(Some(deadline), None, *addr)?;
        let stream = std::net::TcpStream::connect_timeout(
            addr,
            crate::tcp::governance::remaining(deadline)?,
        )?;
        crate::tcp::governance::remaining(deadline)?;
        drop(stream);
        Ok::<_, std::io::Error>(())
    })();
    result.is_ok()
}

/// 协议级探测：TCP 连接、协议握手、心跳全部成功才算可用。
///
/// 比 [`check_alive`] 慢（部分服务器 TCP 可连但不响应行情协议），
/// 但结果可靠。最多八并发，每个节点测一轮，结果保持初始池顺序。
pub fn check_alive_protocol(timeout: Duration) -> Vec<SocketAddr> {
    check_alive_protocol_inner(timeout)
        .into_iter()
        .map(|(addr, _)| addr)
        .collect()
}

/// 协议级探测全部服务器，**按往返耗时升序**返回可用服务器及其 RTT。
///
/// 在 [`check_alive_protocol`]（保持列表顺序）基础上的选优变体：高频轮询
/// 场景建议取 RTT 最小的一台建立长驻连接（对应 mootdx `bestip` 的
/// 「自动测速选优」）。
///
/// ## 示例
/// ```no_run
/// use rustdx_complete::tcp::{ip, Tcp, TcpConfig};
/// use std::time::Duration;
///
/// let today = ip::default_pool().refresh()?;
/// if let Some(fastest) = today.valid_nodes.first() {
///     let config = TcpConfig { timeout: Duration::from_secs(5), ip: Some(*fastest), ..Default::default() };
///     let mut tcp = Tcp::with_config(&config).unwrap();
/// }
/// # Ok::<(), std::io::Error>(())
/// ```
pub fn check_alive_by_rtt(timeout: Duration) -> Vec<(SocketAddr, Duration)> {
    let mut ranked = check_alive_protocol_inner(timeout);
    ranked.sort_by_key(|(_, rtt)| *rtt);
    ranked
}

/// 并发协议级探测的公共实现：`(地址, RTT)`，按列表顺序。
fn check_alive_protocol_inner(timeout: Duration) -> Vec<(SocketAddr, Duration)> {
    match probe::probe_all(&default_pool().list(), 1, timeout) {
        Ok(results) => results
            .into_iter()
            .filter_map(|result| {
                result
                    .median_elapsed()
                    .map(|elapsed| (result.address, elapsed))
            })
            .collect(),
        Err(error) => {
            log::warn!("协议探测失败: {error}");
            Vec::new()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{STOCK_IP, check_alive};
    use crate::tcp::tcpstream_ip;
    use std::time::Duration;

    #[test]
    fn check_all_stock_ips() {
        if std::env::var("RUSTDX_SKIP_INTEGRATION_TESTS").is_ok() {
            println!("⚠️  跳过集成测试 (RUSTDX_SKIP_INTEGRATION_TESTS 已设置)");
            return;
        }
        // 服务器随时间会有增减，要求绝大多数可用（2026-09 实测 40/40 可连）
        let alive = check_alive(Duration::from_secs(3));
        println!(
            "✅ 检测到 {} 个可用服务器 (总共 {} 个):",
            alive.len(),
            STOCK_IP.len()
        );
        for addr in &alive {
            println!("  - {addr}");
        }
        assert!(
            alive.len() >= 10,
            "可用服务器数量不足: 至少需要 10 个，当前有 {} 个。可用: {alive:?}",
            alive.len()
        );
    }

    #[test]
    fn first_server_is_connectable() {
        if std::env::var("RUSTDX_SKIP_INTEGRATION_TESTS").is_ok() {
            println!("⚠️  跳过集成测试 (RUSTDX_SKIP_INTEGRATION_TESTS 已设置)");
            return;
        }
        // 列表首位服务器必须可用——它是 `Tcp::new()` 无故障转移时的首选
        assert!(
            tcpstream_ip(&STOCK_IP[0]).is_ok(),
            "首选服务器 {} 不可用",
            STOCK_IP[0]
        );
    }
}
