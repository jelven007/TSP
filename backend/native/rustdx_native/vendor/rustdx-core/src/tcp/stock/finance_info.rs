use crate::tcp::Tdx;

/// 获取股票财务信息。对应于 pytdx 中的 hq.get_finance_info、GetFinanceInfoCmd。
///
/// ## 注意
/// - 返回股票的基本面财务数据
/// - 包含股本结构、资产负债、利润表等信息
/// - market: 0=深市, 1=沪市
///
/// ## 示例
/// ```ignore
/// use rustdx_complete::tcp::{Tcp, Tdx};
/// use rustdx_complete::tcp::stock::{FinanceInfo, FinanceField};
///
/// let mut tcp = Tcp::new()?;
/// let mut finance = FinanceInfo::new(0, "000001");
/// finance.recv_parsed(&mut tcp)?;
/// let info = &finance.result()[0];
/// println!("总股本（已验证）: {:?}", info.total_shares());
/// println!("候选总资产: {:?}", info.field(FinanceField::TotalAssets).candidate_value());
/// ```
#[derive(Debug, Clone)]
pub struct FinanceInfo<'d> {
    pub send: Box<[u8]>,
    pub market: u8,
    pub code: &'d str,
    pub response: Vec<u8>,
    pub data: Vec<super::FinanceSnapshot>,
}

impl<'d> FinanceInfo<'d> {
    /// 创建一个新的财务信息请求。
    ///
    /// ## 参数
    /// - `market`: 市场代码（0=深市, 1=沪市）
    /// - `code`: 6位股票代码
    pub fn new(market: u8, code: &'d str) -> Self {
        assert_eq!(code.len(), 6, "股票代码必须是6位");

        let mut send = [0u8; Self::LEN];
        // 复制整个包头（14字节）
        send[0..14].copy_from_slice(Self::SEND);

        // 设置market（字节14）
        send[14] = market;
        // 设置code（字节15-20）
        send[15..21].copy_from_slice(code.as_bytes());

        Self {
            send: send.into(),
            market,
            code,
            response: Vec::new(),
            data: Vec::with_capacity(1),
        }
    }
}

impl<'a> Tdx for FinanceInfo<'a> {
    type Item = [super::FinanceSnapshot];

    /// 获取财务信息的请求字节。
    ///
    /// ## 协议格式（基于pytdx源码分析）
    /// - 前14字节：固定包头
    /// - 字节14：market（市场代码）
    /// - 字节15-20：code（股票代码，6字节）
    const SEND: &'static [u8] = &[
        0x0c, 0x1f, 0x18, 0x76, 0x00, 0x01, 0x0b, 0x00, 0x0b, 0x00, 0x10, 0x00, 0x01,
        0x00, // 固定包头（14字节）
    ];

    const TAG: &'static str = "财务信息";
    const LEN: usize = 14 + 1 + 6; // 固定长度：包头14字节 + market(1) + code(6)

    fn send(&mut self) -> &[u8] {
        &self.send
    }

    /// 解析响应的字节。
    ///
    /// ## 响应格式（基于pytdx源码分析）
    /// - 前2字节：证券数量（0或1）
    /// - 7字节：market(1) + code(6)
    /// - 之后：136字节主体（31个float32值与地区、行业、日期）
    fn parse(&mut self, v: Vec<u8>) {
        let _ = self.try_parse(v);
    }

    fn try_parse(&mut self, v: Vec<u8>) -> std::io::Result<()> {
        self.data.clear();
        self.response = v;
        let request = super::FinanceRequest::new(u16::from(self.market), self.code)?;
        self.data = super::decode_finance(&request, &self.response)?;
        Ok(())
    }

    fn result(&self) -> &Self::Item {
        &self.data
    }

    fn recv_parsed(&mut self, tcp: &mut crate::tcp::Tcp) -> std::io::Result<&Self::Item> {
        self.data.clear();
        let response = self.recv(tcp)?;
        self.try_parse(response)?;
        let fetched_at = chrono::Utc::now();
        let endpoint = tcp.get_ref().0.peer_addr()?;
        for row in &mut self.data {
            row.fetched_at = Some(fetched_at);
            row.source_endpoint = Some(endpoint);
        }
        Ok(&self.data)
    }
}

/// Historical pytdx field names and scaling only. Not the default business
/// result. Use FinanceSnapshot; request this type via recv_legacy/decode_finance_legacy.
#[derive(Debug, Default, Clone, serde::Serialize)]
pub struct FinanceInfoData {
    /// 市场代码
    pub market: u8,
    /// 股票代码
    pub code: String,
    /// 流通股本（股）
    pub liutongguben: f64,
    /// 所属省份代码
    pub province: u16,
    /// 所属行业代码
    pub industry: u16,
    /// 财务更新日期（YYYYMMDD格式）
    pub updated_date: u32,
    /// 上市日期（YYYYMMDD格式）
    pub ipo_date: u32,
    /// 总股本（股）
    pub zongguben: f64,
    /// 国家股（股）
    pub guojiagu: f64,
    /// 发起人法人股（股）
    pub faqirenfarengu: f64,
    /// 法人股（股）
    pub farengu: f64,
    /// B股（股）
    pub bgu: f64,
    /// H股（股）
    pub hgu: f64,
    /// 职工股（股）
    pub zhigonggu: f64,
    /// 总资产（元）
    pub zongzichan: f64,
    /// 流动资产（元）
    pub liudongzichan: f64,
    /// 固定资产（元）
    pub gudingzichan: f64,
    /// 无形资产（元）
    pub wuxingzichan: f64,
    /// 股东人数
    pub gudongrenshu: f64,
    /// 流动负债（元）
    pub liudongfuzhai: f64,
    /// 长期负债（元）
    pub changqifuzhai: f64,
    /// 资本公积金（元）
    pub zibengongjijin: f64,
    /// 净资产（元）
    pub jingzichan: f64,
    /// 主营收入（元）
    pub zhuyingshouru: f64,
    /// 主营利润（元）
    pub zhuyinglirun: f64,
    /// 应收账款（元）
    pub yingshouzhangkuan: f64,
    /// 营业利润（元）
    pub yingyelirun: f64,
    /// 投资收益（元）
    pub touzishouyu: f64,
    /// 经营现金流（元）
    pub jingyingxianjinliu: f64,
    /// 总现金流（元）
    pub zongxianjinliu: f64,
    /// 存货（元）
    pub cunhuo: f64,
    /// 利润总额（元）
    pub lirunzonghe: f64,
    /// 税后利润（元）
    pub shuihoulirun: f64,
    /// 净利润（元）
    pub jinglirun: f64,
    /// 未分配利润（元）
    pub weifenpeilirun: f64,
    /// 每股净资产（元）
    pub meigujingzichan: f64,
    /// 保留字段
    pub baoliu2: f64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_finance_info_new() {
        let finance = FinanceInfo::new(0, "000001");
        assert_eq!(finance.market, 0);
        assert_eq!(finance.code, "000001");
        assert_eq!(finance.send.len(), 21);
    }

    #[test]
    fn test_finance_info_new_shanghai() {
        let finance = FinanceInfo::new(1, "600000");
        assert_eq!(finance.market, 1);
        assert_eq!(finance.code, "600000");
    }

    #[test]
    fn test_finance_info_send_bytes() {
        let finance = FinanceInfo::new(0, "000001");
        // 验证包头
        assert_eq!(
            &finance.send[0..14],
            &[
                0x0c, 0x1f, 0x18, 0x76, 0x00, 0x01, 0x0b, 0x00, 0x0b, 0x00, 0x10, 0x00, 0x01, 0x00
            ]
        );
        // 验证market
        assert_eq!(finance.send[14], 0);
        // 验证code
        assert_eq!(&finance.send[15..21], b"000001");
    }

    #[test]
    #[should_panic(expected = "股票代码必须是6位")]
    fn test_finance_info_invalid_code() {
        FinanceInfo::new(0, "00001");
    }

    #[test]
    fn test_connection() {
        // 跳过集成测试（需要实际网络连接）
        if std::env::var("RUSTDX_SKIP_INTEGRATION_TESTS").is_ok() {
            println!("⚠️  跳过集成测试 (RUSTDX_SKIP_INTEGRATION_TESTS 已设置)");
            return;
        }
        println!("⚠️  集成测试需要手动验证（需要实际TCP连接）");
    }
}
