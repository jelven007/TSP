use super::command_request::{CommandSource, check_scan_source, invalid};
use crate::bytes_helper::u16_from_le_bytes;
use crate::tcp::Tdx;
use std::io;

/// 单条板块成分记录。
#[derive(Debug, Clone, serde::Serialize)]
pub struct BlockRecord {
    /// 板块名称（GBK 解码，如 "锂电池"、"沪深300"）
    pub blockname: String,
    /// 板块类型（0=板块 1=风格 等，随文件类型而异）
    pub block_type: u16,
    /// 成分股代码（6位）
    pub code: String,
}

/// 板块文件的单块下载请求（内部使用）。
///
/// 通达信服务器上保存着板块定义文件（`block_zs.dat` 指数板块、
/// `block_gn.dat` 概念板块、`block_fg.dat` 风格板块、`block.dat` 通用），
/// 通过两步协议下载：先 [`BlockInfoMeta`] 拿文件大小，再分块下载内容。
///
/// 高层 API 见 [`get_block_info`]。
#[derive(Debug, Clone)]
pub struct BlockInfoMeta {
    pub send: Box<[u8]>,
    pub response: Vec<u8>,
    /// 文件大小（字节）
    pub size: u32,
}

impl BlockInfoMeta {
    /// 查询板块文件大小。
    pub fn new(block_file: &str) -> Self {
        let mut send = [0u8; Self::LEN];
        send[0..12].copy_from_slice(Self::SEND);
        // 文件名（40 字节，\0 填充）
        let name = block_file.as_bytes();
        let n = name.len().min(40);
        send[12..12 + n].copy_from_slice(&name[..n]);
        Self {
            send: send.into(),
            response: Vec::new(),
            size: 0,
        }
    }
}

impl Tdx for BlockInfoMeta {
    type Item = u32;

    /// 包头 12 字节 + 文件名 40 字节 = 52。
    const SEND: &'static [u8] = &[
        0x0c, 0x39, 0x18, 0x69, 0x00, 0x01, 0x2a, 0x00, 0x2a, 0x00, 0xc5, 0x02,
    ];
    const TAG: &'static str = "板块文件大小";
    const LEN: usize = 12 + 40;

    fn send(&mut self) -> &[u8] {
        &self.send
    }

    /// 响应通常为 size(u32 LE) + 保留字节 + hash(32字节) + 终止字节。
    /// 旧适配器只解释 size；完整字段请使用 [`super::BlockMetaRequest`]。
    fn parse(&mut self, v: Vec<u8>) {
        if v.len() >= 4 {
            self.size = u32::from_le_bytes([v[0], v[1], v[2], v[3]]);
        }
        self.response = v;
    }

    fn result(&self) -> &Self::Item {
        &self.size
    }

    fn try_parse(&mut self, v: Vec<u8>) -> std::io::Result<()> {
        self.size = 0;
        crate::tcp::reader::require_len(&v, 4)?;
        self.parse(v);
        Ok(())
    }
}

/// 板块文件内容块下载请求（内部使用）。
#[derive(Debug, Clone)]
pub struct BlockInfoChunk {
    pub send: Box<[u8]>,
    pub response: Vec<u8>,
    /// 文件内容（跳过前 4 字节后的部分）
    pub data: Vec<u8>,
}

impl BlockInfoChunk {
    /// 下载板块文件的 [start, start+size) 字节段。
    pub fn new(block_file: &str, start: u32, size: u32) -> Self {
        let mut send = [0u8; Self::LEN];
        send[0..12].copy_from_slice(Self::SEND);
        send[12..16].copy_from_slice(&start.to_le_bytes());
        send[16..20].copy_from_slice(&size.to_le_bytes());
        let name = block_file.as_bytes();
        let n = name.len().min(100);
        send[20..20 + n].copy_from_slice(&name[..n]);
        Self {
            send: send.into(),
            response: Vec::new(),
            data: Vec::new(),
        }
    }
}

impl Tdx for BlockInfoChunk {
    type Item = [u8];

    /// 包头 12 字节 + start(4) + size(4) + 文件名(100) = 120。
    const SEND: &'static [u8] = &[
        0x0c, 0x37, 0x18, 0x6a, 0x00, 0x01, 0x6e, 0x00, 0x6e, 0x00, 0xb9, 0x06,
    ];
    const TAG: &'static str = "板块文件内容";
    const LEN: usize = 12 + 4 + 4 + 100;

    fn send(&mut self) -> &[u8] {
        &self.send
    }

    /// 响应：前 4 字节跳过，其余为文件内容。
    fn parse(&mut self, v: Vec<u8>) {
        self.data = v.iter().skip(4).copied().collect();
        self.response = v;
    }

    fn result(&self) -> &Self::Item {
        &self.data
    }

    fn try_parse(&mut self, v: Vec<u8>) -> std::io::Result<()> {
        self.data.clear();
        crate::tcp::reader::require_len(&v, 4)?;
        self.parse(v);
        Ok(())
    }
}

/// 通过02c5大小/MD5校验的完整板块文件。
#[derive(Debug, Clone, serde::Serialize)]
pub struct BlockFile {
    pub filename: String,
    pub metadata: super::BlockMetadata,
    /// 完整文件字节，不包含06b9正文的前4字节。
    #[serde(skip)]
    pub content: Vec<u8>,
    /// 完整content的实际MD5，小写十六进制。
    pub computed_md5: String,
    pub chunks: Vec<BlockFileChunk>,
}

/// 完整下载中每个06b9块的范围、未知头部及来源。
#[derive(Debug, Clone, serde::Serialize)]
pub struct BlockFileChunk {
    pub start: u32,
    pub length: u32,
    /// 保留06b9正文前4字节；不推断其含义。
    pub header_raw: [u8; 4],
    pub source: CommandSource,
}

impl BlockFile {
    /// 严格解析成分；畸形文件不会作为空表或部分结果返回。
    pub fn decode_records(&self) -> io::Result<Vec<BlockRecord>> {
        decode_block_content(&self.content)
    }
}

/// 有界下载完整板块文件，验证所有块来源连续、总长度及02c5声明MD5。
///
/// 最多64MiB，每块30000字节，共享pagination_timeout总预算。
/// 此入口仅校验文件传输；成分布局由[`BlockFile::decode_records`]验证。
pub fn download_block_file(tcp: &mut crate::tcp::Tcp, block_file: &str) -> io::Result<BlockFile> {
    tcp.with_timeout(crate::tcp::network_policy().pagination_timeout, |tcp| {
        download_block_file_until(tcp, block_file)
    })
}

/// 下载并解析板块文件，返回全部板块成分记录。
///
/// 文件大小、完整MD5及槽位/GBK/代码格式均须通过校验；异常返回错误。
///
/// ## 参数
/// - `block_file`: 板块文件名。常用：
///   - `"block_gn.dat"` 概念板块
///   - `"block_zs.dat"` 指数板块
///   - `"block_fg.dat"` 风格板块
///   - `"block.dat"` 通用板块
///
/// ## 示例
/// ```ignore
/// use rustdx_complete::tcp::{Tcp, Tdx};
/// use rustdx_complete::tcp::stock::get_block_info;
///
/// let mut tcp = Tcp::new()?;
/// let records = get_block_info(&mut tcp, "block_gn.dat")?;
/// for r in records.iter().filter(|r| r.blockname == "锂电池") {
///     println!("{} 属于锂电池板块", r.code);
/// }
/// ```
pub fn get_block_info(
    tcp: &mut crate::tcp::Tcp,
    block_file: &str,
) -> crate::Result<Vec<BlockRecord>> {
    Ok(download_block_file(tcp, block_file)?.decode_records()?)
}

fn download_block_file_until(tcp: &mut crate::tcp::Tcp, block_file: &str) -> io::Result<BlockFile> {
    let request = super::BlockMetaRequest::new(block_file)?;
    let meta = request.recv(tcp)?;
    let size = meta.size;
    if size > 64 * 1024 * 1024 {
        return Err(invalid("板块文件超过64MiB限制"));
    }
    meta.validate_md5()?;
    let mut baseline = Some(meta.source.clone());

    const ONE_CHUNK: u32 = 0x7530;
    let chunk_count = size.div_ceil(ONE_CHUNK);
    let mut chunks = Vec::with_capacity(chunk_count as usize);
    let mut content = Vec::with_capacity(size as usize);
    for seg in 0..chunk_count {
        let start = seg * ONE_CHUNK;
        let take = ONE_CHUNK.min(size - start);
        let mut chunk = BlockInfoChunk::new(request.filename(), start, take);
        chunk.recv_parsed(tcp)?;
        let mut source = CommandSource::offline(0x06b9);
        source.attach(tcp)?;
        check_scan_source(&mut baseline, &source)?;
        if chunk.result().len() != take as usize {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "板块文件块长度不完整",
            ));
        }
        let mut header_raw = [0; 4];
        header_raw.copy_from_slice(&chunk.response[..4]);
        chunks.push(BlockFileChunk {
            start,
            length: take,
            header_raw,
            source,
        });
        content.extend_from_slice(chunk.result());
    }

    let computed_md5 = meta.verify_content(&content)?;
    Ok(BlockFile {
        filename: request.filename().to_owned(),
        metadata: meta,
        content,
        computed_md5,
        chunks,
    })
}

/// 严格解析固定槽位板块文件。
///
/// 接受空字节文件或386字节零板块文件。拒绝截断头部、不能整除的槽位、
/// 成分数越过本槽、非法GBK名称和非6位ASCII数字代码。
/// 名称尾部NUL、代码第7字节及未使用槽位字节不作市场/填充语义推断。
pub fn decode_block_content(data: &[u8]) -> io::Result<Vec<BlockRecord>> {
    if data.is_empty() {
        return Ok(Vec::new());
    }
    crate::tcp::reader::require_len(data, 386)?;
    let num = usize::from(u16_from_le_bytes(data, 384));
    let body = &data[386..];
    if num == 0 {
        if !body.is_empty() {
            return Err(invalid("零板块文件包含多余槽位字节"));
        }
        return Ok(Vec::new());
    }
    if body.len() % num != 0 || body.len() / num < 13 {
        return Err(invalid("板块文件槽位长度无效"));
    }
    let slot_size = body.len() / num;
    let mut records = Vec::new();
    for (index, slot) in body.chunks_exact(slot_size).enumerate() {
        let count = usize::from(u16_from_le_bytes(slot, 9));
        if count > (slot_size - 13) / 7 {
            return Err(invalid("板块成分数量超出本槽位容量"));
        }
        let name_raw = &slot[..9];
        let end = name_raw.iter().position(|&b| b == 0).unwrap_or(9);
        let name = encoding_rs::GBK
            .decode_without_bom_handling_and_without_replacement(&name_raw[..end])
            .ok_or_else(|| invalid("板块名称包含非法GBK"))?;
        if name.is_empty() && count != 0 {
            return Err(invalid("有成分的板块名称为空"));
        }
        let block_type = u16_from_le_bytes(slot, 11);
        for member in slot[13..13 + count * 7].chunks_exact(7) {
            let code_raw = &member[..6];
            if !code_raw.iter().all(u8::is_ascii_digit) {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("板块槽位{index}成分代码不是6位ASCII数字"),
                ));
            }
            let code = std::str::from_utf8(code_raw)
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
            records.push(BlockRecord {
                blockname: name.to_string(),
                block_type,
                code: code.to_owned(),
            });
        }
    }
    Ok(records)
}

/// 解析板块文件内容。
///
/// 此历史兼容入口可能返回空/部分结果；高层下载使用[`decode_block_content`]。
///
/// 文件布局（2026 实测）：384 字节文件头（"Registry ver: 1.0 ..."）+ 板块数(u16)，
/// 之后为 **固定大小的板块槽位**（`槽位大小 = (总长 - 386) / 板块数`，
/// block_gn.dat 为 2813 字节 = 板块头 13 + 最多 400 只代码 × 7）。
/// 每个槽位内：板块名(9, GBK) + 成分数(u16) + 类型(u16) + 成分数×代码(7)，
/// 剩余部分以 `\0` 填充。
pub fn parse_block_content(data: &[u8]) -> Vec<BlockRecord> {
    let mut result = Vec::new();
    if data.len() < 386 {
        return result;
    }
    let num = u16_from_le_bytes(data, 384) as usize;
    if num == 0 {
        return result;
    }
    // 固定槽位大小（总长 - 头部 / 板块数）
    let slot = (data.len() - 386).div_ceil(num);

    for i in 0..num {
        let mut pos = 386 + i * slot;
        if pos + 13 > data.len() {
            break;
        }
        let name_raw = &data[pos..pos + 9];
        let blockname = crate::tcp::helper::gbk_to_string_trim_null(name_raw);
        let stock_count = u16_from_le_bytes(data, pos + 9) as usize;
        let block_type = u16_from_le_bytes(data, pos + 11);
        pos += 13;
        if blockname.is_empty() || stock_count == 0 {
            continue; // 空槽位
        }
        for _ in 0..stock_count {
            if pos + 7 > data.len() {
                break;
            }
            // 7 字节 = 6 位代码 + 1 字节（市场/填充）
            let code_raw = &data[pos..pos + 6];
            pos += 7;
            let code: String = code_raw
                .iter()
                .map(|&b| b as char)
                .filter(|c| c.is_ascii_digit())
                .collect();
            if code.len() == 6 {
                result.push(BlockRecord {
                    blockname: blockname.clone(),
                    block_type,
                    code,
                });
            }
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 构造的板块文件内容：384 字节头 + 数量 + 2 个固定槽位（槽位间填充）。
    #[test]
    fn parse_block_content_two_blocks() {
        // 槽位大小 = 13 + 2×7 = 27（2 个代码位）
        let slot: usize = 13 + 14;
        let mut data = vec![0u8; 384];
        data.extend_from_slice(&2u16.to_le_bytes()); // num = 2

        // 槽位0: "锂电池"（GBK: ef ae b5 e7 b3 d8）2 只成分股 类型1
        let mut b0 = vec![0u8; slot];
        b0[..6].copy_from_slice(&[0xef, 0xae, 0xb5, 0xe7, 0xb3, 0xd8]);
        b0[9..11].copy_from_slice(&2u16.to_le_bytes());
        b0[11..13].copy_from_slice(&1u16.to_le_bytes());
        b0[13..19].copy_from_slice(b"300750");
        b0[20..26].copy_from_slice(b"600519");
        data.extend_from_slice(&b0);

        // 槽位1: "HS300" 1 只成分股 类型0
        let mut b1 = vec![0u8; slot];
        b1[..5].copy_from_slice(b"HS300");
        b1[9..11].copy_from_slice(&1u16.to_le_bytes());
        b1[11..13].copy_from_slice(&0u16.to_le_bytes());
        b1[13..19].copy_from_slice(b"000001");
        data.extend_from_slice(&b1);

        let records = parse_block_content(&data);
        assert_eq!(records.len(), 3);
        assert_eq!(records[0].blockname, "锂电池");
        assert_eq!(records[0].code, "300750");
        assert_eq!(records[0].block_type, 1);
        assert_eq!(records[1].blockname, "锂电池");
        assert_eq!(records[1].code, "600519");
        assert_eq!(records[2].blockname, "HS300");
        assert_eq!(records[2].code, "000001");
    }

    #[test]
    fn parse_truncated_returns_empty() {
        assert!(parse_block_content(&[0u8; 100]).is_empty());
    }

    #[test]
    fn test_meta_send_bytes() {
        let meta = BlockInfoMeta::new("block_gn.dat");
        assert_eq!(meta.send.len(), 52);
        assert_eq!(&meta.send[12..24], b"block_gn.dat");
        assert_eq!(&meta.send[24..52], &[0u8; 28]);
    }
}
