use super::{Result, Tdx, u16_from_le_bytes};

/// 对应 pytdx 的 hq.security_count / GetSecurityCountCmd
/// （深沪市证券数量，包括指数、股票和大量债券）。心跳使用0004。
#[derive(Debug)]
pub struct SecurityCount {
    send: Box<[u8]>,
    /// 0 代表深市；1 代表沪市。
    market: u16,
    /// 响应的结果：证券数量
    count: u16,
}

impl SecurityCount {
    /// market = 0 或 1，表示深市或沪市。
    pub fn new(market: u16) -> Self {
        let mut send = [0; Self::LEN];
        send.copy_from_slice(Self::SEND);
        if market != 0 {
            send[12..14].copy_from_slice(&market.to_le_bytes());
        }
        Self {
            send: send.into(),
            market,
            count: 0,
        }
    }

    pub fn market(&mut self, market: u16) {
        self.market = market;
        self.send[12..14].copy_from_slice(&market.to_le_bytes());
    }
}

impl Tdx for SecurityCount {
    type Item = u16;

    /// 深市证券数量的请求字节。
    const SEND: &'static [u8] = &[
        0x0c, 0x0c, 0x18, 0x6c, 0x00, 0x01, 0x08, 0x00, 0x08, 0x00, 0x4e, 0x04, 0x00, 0x00, 0x75,
        0xc7, 0x33, 0x01,
    ];
    const TAG: &'static str = "heartbeat";

    fn send(&mut self) -> &[u8] {
        &self.send
    }

    fn parse(&mut self, response: Vec<u8>) {
        let _ = self.try_parse(response);
    }

    fn try_parse(&mut self, response: Vec<u8>) -> Result<()> {
        self.count = 0;
        super::reader::require_len(&response, 2)?;
        self.count = u16_from_le_bytes(&response, 0);
        Ok(())
    }

    fn result(&self) -> &Self::Item {
        &self.count
    }
}

/// 查询证券列表。对应于 pytdx 中的 GetSecurityList。
///
/// ## 注意：
/// - 获取的数据可能同一类别内是有序的，不同类别间是顺序未知 （比如 A
///   股股票之后不是创业板/科创板股票）；
/// - 每次返回 1000 条结果。
#[derive(Debug, Clone)]
pub struct SecurityList {
    pub send: Box<[u8]>,
    pub market: u16,
    pub start: u16,
    /// 响应信息中的列表长度。
    pub count: usize,
    pub response: Vec<u8>,
    pub data: Box<[SecurityListData]>,
}

impl Default for SecurityList {
    fn default() -> Self {
        Self {
            send: {
                let mut arr = [0; Self::LEN];
                arr.copy_from_slice(Self::SEND);
                arr.into()
            },
            market: 0,
            start: 1,
            count: 0,
            response: Vec::new(),
            data: [].into(),
        }
    }
}

impl SecurityList {
    /// 参数说明：
    /// - market = 0 或 1，表示深市或沪市；
    /// - start 在 [0, n] 的范围内，其中 n 是 [`SecurityCount`] 得到的结果。 目前 market = 0
    ///   时，有 13471 条； market = 1 时，有 18065 条。
    pub fn new(market: u16, start: u16) -> Self {
        Self {
            send: {
                let mut arr = [0; Self::LEN];
                arr.copy_from_slice(Self::SEND);
                arr[12..14].copy_from_slice(&market.to_le_bytes());
                arr[14..16].copy_from_slice(&start.to_le_bytes());
                arr.into()
            },
            market,
            start,
            count: 0,
            response: Vec::new(),
            data: [].into(),
        }
    }
}

impl Tdx for SecurityList {
    type Item = [SecurityListData];

    /// market = 0, start = 0 表示深市所有证券从第 0 个开始的 1000 条证券; 共 16 字节。
    const SEND: &'static [u8] = &[
        0x0c, 0x01, 0x18, 0x64, 0x01, 0x01, 0x06, 0x00, 0x06, 0x00, 0x50, 0x04, 0x00, 0x00, 0x00,
        0x00,
    ];
    const TAG: &'static str = "股票、指数列表";

    fn send(&mut self) -> &[u8] {
        &self.send
    }

    /// 前 2 字节表示列表的长度，剩余字节中，每 29 字节使用 [`SecurityListData::parse`] 解析。
    fn parse(&mut self, v: Vec<u8>) {
        let _ = self.try_parse(v);
    }

    fn try_parse(&mut self, v: Vec<u8>) -> Result<()> {
        self.data = [].into();
        self.count = 0;
        self.response.clear();
        super::reader::require_len(&v, 2)?;
        let count = u16_from_le_bytes(&v, 0) as usize;
        super::reader::require_len(&v, 2 + count * 29)?;
        for c in v[2..2 + count * 29].as_chunks::<29>().0 {
            super::reader::Reader::new(c).code()?;
        }
        self.count = count;
        self.data = v[2..2 + count * 29]
            .as_chunks::<29>()
            .0
            .iter()
            .map(|c| SecurityListData::parse(c))
            .collect();
        debug_assert_eq!(self.count, self.data.len());
        self.response = v;
        Ok(())
    }

    fn result(&self) -> &Self::Item {
        &self.data
    }
}

#[test]
fn connection() -> Result<()> {
    // 跳过集成测试，如果设置了环境变量
    if std::env::var("RUSTDX_SKIP_INTEGRATION_TESTS").is_ok() {
        println!("⚠️  跳过集成测试 (RUSTDX_SKIP_INTEGRATION_TESTS 已设置)");
        return Ok(());
    }

    SecurityList::default().recv_parsed(&mut crate::tcp::Tcp::new()?)?;
    Ok(())
}

/// [`SecurityList`] 的解析结果。具体为指数、股票、债券等证券的代码、名称。
///
/// ## 注意
/// 有些响应的字节没有被解析，具体查看 [`SecurityListData::parse`] 的说明。
#[derive(Debug, Clone, serde::Serialize)]
pub struct SecurityListData {
    pub code: String,
    /// GBK名称，去掉首个空字节之后的填充。
    pub name: String,
}

impl SecurityListData {
    /// 解析 [`SecurityList`] 的响应字节。传入长度为 29 字节序列。
    /// ```python
    /// (
    ///     code,            # UTF-8 编码
    ///     volunit,         # 100
    ///     name_bytes,      # GBK 编码
    ///     reversed_bytes1,
    ///     decimal_point,   # 2
    ///     pre_close_raw,   # 少许结果与实际数据有出入，故不解析
    ///     reversed_bytes2,
    /// ) = struct.unpack("<6sH8s4sBI4s", bytes) # python 表示方式
    /// ```
    pub fn parse(bytes: &[u8]) -> Self {
        let code = std::str::from_utf8(bytes.get(..6).unwrap_or_default())
            .unwrap_or("")
            .into();
        let name = super::helper::gbk_to_string_trim_null(bytes.get(8..16).unwrap_or_default());
        Self { code, name }
    }
}

#[test]
fn malformed_count_clears_previous_result_without_panicking() {
    let mut count = SecurityCount::new(0);
    count.try_parse(vec![42, 0]).unwrap();
    assert_eq!(*count.result(), 42);
    assert!(count.try_parse(vec![1]).is_err());
    assert_eq!(*count.result(), 0);
    count.parse(vec![]);
    assert_eq!(*count.result(), 0);
    assert_eq!(SecurityListData::parse(&[]).name, "");
    let mut record = [0; 29];
    record[..6].copy_from_slice(b"000001");
    record[8] = 0x81;
    assert!(SecurityListData::parse(&record).name.contains('\u{fffd}'));
}

pub const PACK1: &[u8] = &[
    0x0c, 0x02, 0x18, 0x93, 0x00, 0x01, 0x03, 0x00, 0x03, 0x00, 0x0d, 0x00, 0x01,
];
#[deprecated(note = "当前行情服务器只需要 PACK1；继续发送旧登录包会使K线请求被拒绝")]
pub const PACK2: &[u8] = &[
    0x0c, 0x02, 0x18, 0x94, 0x00, 0x01, 0x03, 0x00, 0x03, 0x00, 0x0d, 0x00, 0x02,
];
#[deprecated(note = "当前行情服务器只需要 PACK1；继续发送旧登录包会使K线请求被拒绝")]
pub const PACK3: &[u8] = &[
    0x0c, 0x03, 0x18, 0x99, 0x00, 0x01, 0x20, 0x00, 0x20, 0x00, 0xdb, 0x0f, 0xd5, 0xd0, 0xc9, 0xcc,
    0xd6, 0xa4, 0xa8, 0xaf, 0x00, 0x00, 0x00, 0x8f, 0xc2, 0x25, 0x40, 0x13, 0x00, 0x00, 0xd5, 0x00,
    0xc9, 0xcc, 0xbd, 0xf0, 0xd7, 0xea, 0x00, 0x00, 0x00, 0x02,
];
pub const RECV_SIZE: usize = 16;

pub fn send_packs(tcp: &mut super::Tcp, decompress: bool) -> Result<()> {
    use super::{send_recv, send_recv_decompress};
    if decompress {
        send_recv_decompress(tcp, PACK1, "PACK1")?;
    } else {
        send_recv(tcp, PACK1, "PACK1")?;
    }
    Ok(())
}
