/// 多个值一起比较格式化的字符串值，当遇到第一对不相等的值比较时，直接 panic 。
/// 这主要用于比较不实现 PartialEq trait 类型的值，且只用于内部测试代码。
macro_rules! compare {
    ($a:expr, $($b:expr),+) => {
       {
            let v = format!("{:?}", $a);
            $(
                assert_eq!(v, format!("{:?}", $b), "{} ≠ {}", stringify!($a), stringify!($b));
            )+
        }
    };
}

use super::{Result, Tcp, Tdx};

/// 本地协议服务器：验证握手、池复用及请求行为，无外部网络依赖。
pub(crate) fn mock_server(
    connections: usize,
    response: impl Fn(&[u8]) -> Vec<u8> + Send + Sync + 'static,
) -> (super::TcpConfig, std::thread::JoinHandle<()>) {
    use std::io::{Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let config = super::TcpConfig {
        ip: Some(listener.local_addr().unwrap()),
        ip_pool: Some(super::ip::IpPool::for_test(listener.local_addr().unwrap())),
        timeout: std::time::Duration::from_secs(2),
        ..Default::default()
    };
    let response = std::sync::Arc::new(response);
    let handle = std::thread::spawn(move || {
        std::thread::scope(|scope| {
            for _ in 0..connections {
                let (mut stream, _) = listener.accept().unwrap();
                let response = response.clone();
                scope.spawn(move || {
                    stream
                        .set_read_timeout(Some(std::time::Duration::from_secs(3)))
                        .unwrap();
                    loop {
                        let mut header = [0u8; 10];
                        if stream.read_exact(&mut header).is_err() {
                            break;
                        }
                        let n = u16::from_le_bytes([header[6], header[7]]) as usize + 10;
                        if n < 10 {
                            break;
                        }
                        let mut request = header.to_vec();
                        request.resize(n, 0);
                        if stream.read_exact(&mut request[10..]).is_err() {
                            break;
                        }
                        let body = response(&request);
                        let mut reply = [0u8; 16];
                        reply[..4].copy_from_slice(&[0xb1, 0xcb, 0x74, 0]);
                        reply[5..9].copy_from_slice(&request[1..5]);
                        reply[10..12].copy_from_slice(&request[10..12]);
                        reply[12..14].copy_from_slice(&(body.len() as u16).to_le_bytes());
                        reply[14..16].copy_from_slice(&(body.len() as u16).to_le_bytes());
                        if stream
                            .write_all(&reply)
                            .and_then(|_| stream.write_all(&body))
                            .is_err()
                        {
                            break;
                        }
                    }
                });
            }
        });
    });
    (config, handle)
}

/// 连接测试辅助函数。
///
/// 注意：这是一个集成测试，需要实际的网络连接。
/// 如果设置了环境变量 `RUSTDX_SKIP_INTEGRATION_TESTS=1`，则会跳过测试。
pub fn connection<T: Tdx>(mut tdx: T) -> Result<()>
where
    <T as Tdx>::Item: std::fmt::Debug,
{
    if std::env::var("RUSTDX_SKIP_INTEGRATION_TESTS").is_ok() {
        println!("⚠️  跳过集成测试 (RUSTDX_SKIP_INTEGRATION_TESTS 已设置)");
        return Ok(());
    }

    println!("send: {:?}", tdx.send());
    println!("recv: {:?}", tdx.recv_parsed(&mut Tcp::new()?)?);
    Ok(())
}

#[allow(dead_code)]
pub fn connection_mut<T: Tdx>(tdx: &mut T) -> Result<()>
where
    <T as Tdx>::Item: std::fmt::Debug,
{
    if std::env::var("RUSTDX_SKIP_INTEGRATION_TESTS").is_ok() {
        println!("⚠️  跳过集成测试 (RUSTDX_SKIP_INTEGRATION_TESTS 已设置)");
        return Ok(());
    }

    println!("send: {:?}", tdx.send());
    let res = tdx.recv_parsed(&mut Tcp::new()?)?;
    println!("recv: {res:?}");
    Ok(())
}
