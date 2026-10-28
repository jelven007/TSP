//! Bounded ZIP/GBK parsing for the known zhb.zip statistics layout.
//! Named slots follow eltdx 3.3.0; they are candidates, not independently verified units.
use super::command_request::invalid;
use super::{ResourceFile, ResourceReadOptions, download_resource_file};
use serde::Serialize;
use std::{
    collections::BTreeMap,
    io::{self, Cursor, Read},
    sync::Arc,
};

pub const MAX_STATS_ZIP_BYTES: usize = 32 * 1024 * 1024;
const MAX_ENTRY_BYTES: u64 = 32 * 1024 * 1024;
const MAX_UNCOMPRESSED_BYTES: u64 = 64 * 1024 * 1024;
const MAX_ENTRIES: usize = 128;
const MAX_ROWS: usize = 100_000;
const MAX_LINE_BYTES: usize = 16 * 1024;
const MEMBERS: [&str; 2] = ["tdxstat.cfg", "tdxstat2.cfg"];

#[derive(Debug, Clone, Serialize)]
pub struct TdxStatValues {
    pub stats_date_raw: Option<String>,
    pub beta_60d: Option<f64>,
    pub pe_ttm: Option<f64>,
    pub free_float_shares_10k: Option<f64>,
    pub year_limit_up_days: Option<i64>,
    pub limit_stat_days: Option<i64>,
    pub limit_up_count_in_stat_days: Option<i64>,
    pub limit_up_streak_days: Option<i64>,
}
#[derive(Debug, Clone, Serialize)]
pub struct TdxStat2Values {
    pub stats_date_raw: Option<String>,
    pub amount_10k: Option<f64>,
    pub seal_amount_10k: Option<f64>,
    pub prev_amount_10k: Option<f64>,
    pub prev_seal_amount_10k: Option<f64>,
    pub prev2_amount_10k: Option<f64>,
    pub prev2_seal_amount_10k: Option<f64>,
    pub open_volume_hand: Option<f64>,
    pub prev_open_volume_hand: Option<f64>,
    pub open_amount_10k: Option<f64>,
    pub prev_open_amount_10k: Option<f64>,
}
#[derive(Debug, Clone, Serialize)]
pub enum StatsCandidateValues {
    Stat(TdxStatValues),
    Stat2(TdxStat2Values),
}
#[derive(Debug, Clone, Serialize)]
pub struct StatsRow {
    /// Vendor key; this is not automatically the quote market identifier.
    pub market_id_raw: i32,
    pub code: String,
    pub line_number: usize,
    /// Every slot, including unknown columns, remains available.
    pub fields_raw: Vec<String>,
    pub candidate_values: StatsCandidateValues,
}
#[derive(Debug, Clone, Serialize)]
pub struct StatsMember {
    pub name: String,
    pub crc32: u32,
    pub compressed_size: u64,
    pub rows: Vec<StatsRow>,
    /// Retain nonempty headers/non-record lines rather than silently dropping them.
    pub unparsed_lines: Vec<(usize, String)>,
    #[serde(skip)]
    pub content: Arc<[u8]>,
}
#[derive(Debug, Clone, Serialize)]
pub struct TdxStatsArchive {
    pub source_path: String,
    pub layout_profile: &'static str,
    pub members: Vec<StatsMember>,
    #[serde(skip)]
    pub raw_archive: Arc<[u8]>,
}
impl TdxStatsArchive {
    pub fn row(&self, member: &str, market_id: i32, code: &str) -> Option<&StatsRow> {
        self.members
            .iter()
            .find(|m| m.name == member)?
            .rows
            .iter()
            .find(|r| r.market_id_raw == market_id && r.code == code)
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct TdxStatsResource {
    pub file: ResourceFile,
    pub archive: TdxStatsArchive,
}

/// Parse only ZIP stored/deflated members, without extracting anything to disk.
/// Validate every entry's size/encryption and the two required members' CRC.
pub fn parse_tdx_stats_archive(payload: &[u8], source_path: &str) -> io::Result<TdxStatsArchive> {
    if payload.is_empty() || payload.len() > MAX_STATS_ZIP_BYTES {
        return Err(invalid("统计ZIP为空或超过32MiB"));
    }
    validate_directory(payload)?;
    let mut archive = zip::ZipArchive::new(Cursor::new(payload)).map_err(zip_error)?;
    if archive.len() > MAX_ENTRIES {
        return Err(invalid("统计ZIP超过128个成员"));
    }
    let mut indices = BTreeMap::new();
    let mut total = 0u64;
    for index in 0..archive.len() {
        let entry = archive.by_index_raw(index).map_err(zip_error)?;
        total = total
            .checked_add(entry.size())
            .ok_or_else(|| invalid("ZIP大小溢出"))?;
        if entry.encrypted() || entry.size() > MAX_ENTRY_BYTES || total > MAX_UNCOMPRESSED_BYTES {
            return Err(invalid("统计ZIP包含加密成员或解压大小超限"));
        }
        if MEMBERS.contains(&entry.name())
            && indices.insert(entry.name().to_owned(), index).is_some()
        {
            return Err(invalid("统计ZIP包含重复的必需成员"));
        }
    }
    let mut members = Vec::new();
    for name in MEMBERS {
        let index = *indices
            .get(name)
            .ok_or_else(|| invalid("统计ZIP缺少必需CFG"))?;
        let mut entry = archive.by_index(index).map_err(zip_error)?;
        let size = entry.size();
        let crc32 = entry.crc32();
        let compressed_size = entry.compressed_size();
        let mut bytes = Vec::new();
        (&mut entry)
            .take(MAX_ENTRY_BYTES + 1)
            .read_to_end(&mut bytes)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        if bytes.len() as u64 != size {
            return Err(invalid("统计CFG解压字节数与ZIP声明不一致"));
        }
        members.push(parse_member(name, crc32, compressed_size, &bytes)?);
    }
    Ok(TdxStatsArchive {
        source_path: source_path.into(),
        layout_profile: "eltdx-3.3.0/zhb-cfg-candidate-v1",
        members,
        raw_archive: payload.into(),
    })
}
fn zip_error(error: zip::result::ZipError) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error)
}
// zip 2.x indexes entries by name and can hide duplicate central directory
// records. Validate/count the original directory before constructing its map.
fn validate_directory(bytes: &[u8]) -> io::Result<()> {
    let end = (bytes.len().saturating_sub(65_557)..bytes.len().saturating_sub(21))
        .rev()
        .find(|&i| {
            bytes.get(i..i + 4) == Some(b"PK\x05\x06")
                && bytes.get(i + 20..i + 22).is_some_and(|raw| {
                    i + 22 + usize::from(u16::from_le_bytes([raw[0], raw[1]])) == bytes.len()
                })
        })
        .ok_or_else(|| invalid("统计ZIP没有完整EOCD"))?;
    let u16_at = |i| u16::from_le_bytes([bytes[i], bytes[i + 1]]);
    let u32_at = |i| u32::from_le_bytes([bytes[i], bytes[i + 1], bytes[i + 2], bytes[i + 3]]);
    let count = usize::from(u16_at(end + 10));
    if u16_at(end + 4) != 0
        || u16_at(end + 6) != 0
        || usize::from(u16_at(end + 8)) != count
        || count > MAX_ENTRIES
    {
        return Err(invalid("统计ZIP多卷/ZIP64或原始成员数超限"));
    }
    let start = u32_at(end + 16) as usize;
    let length = u32_at(end + 12) as usize;
    if start.checked_add(length) != Some(end) {
        return Err(invalid("统计ZIP中央目录范围无效"));
    }
    let mut offset = start;
    let mut names = std::collections::BTreeSet::new();
    let mut total = 0u64;
    for _ in 0..count {
        if offset.checked_add(46).is_none_or(|n| n > end)
            || bytes.get(offset..offset + 4) != Some(b"PK\x01\x02")
        {
            return Err(invalid("统计ZIP中央目录截断"));
        }
        let size = u64::from(u32_at(offset + 24));
        total += size;
        if u16_at(offset + 8) & 1 != 0 || size > MAX_ENTRY_BYTES || total > MAX_UNCOMPRESSED_BYTES {
            return Err(invalid("统计ZIP原始目录加密或大小超限"));
        }
        let name_len = usize::from(u16_at(offset + 28));
        let extra_len = usize::from(u16_at(offset + 30));
        let comment_len = usize::from(u16_at(offset + 32));
        let next = offset + 46 + name_len + extra_len + comment_len;
        if next > end || !names.insert(&bytes[offset + 46..offset + 46 + name_len]) {
            return Err(invalid("统计ZIP重复成员或目录越界"));
        }
        offset = next;
    }
    if offset != end {
        return Err(invalid("统计ZIP原始成员数不一致"));
    }
    Ok(())
}
fn text(value: &str) -> Option<String> {
    let value = value.trim();
    (!value.is_empty()).then(|| value.to_owned())
}
fn number(value: &str) -> Option<f64> {
    value.trim().parse::<f64>().ok().filter(|n| n.is_finite())
}
fn integer(value: &str) -> Option<i64> {
    let value = number(value)?;
    // Reject fractional/overflowing slots instead of truncating or saturating.
    (value.fract() == 0.0 && value >= i64::MIN as f64 && value < -(i64::MIN as f64))
        .then_some(value as i64)
}
fn parse_member(
    name: &str,
    crc32: u32,
    compressed_size: u64,
    bytes: &[u8],
) -> io::Result<StatsMember> {
    let decoded = encoding_rs::GBK
        .decode_without_bom_handling_and_without_replacement(bytes)
        .ok_or_else(|| invalid("统计CFG包含非法GBK"))?;
    let stat = name == MEMBERS[0];
    let mut rows = Vec::new();
    let mut keys = std::collections::BTreeSet::new();
    let mut unparsed_lines = Vec::new();
    for (index, line) in decoded.lines().enumerate() {
        crate::operation::check()?;
        if index >= MAX_ROWS || line.len() > MAX_LINE_BYTES {
            return Err(invalid("统计CFG行数或单行字节超限"));
        }
        if line.trim().is_empty() {
            continue;
        }
        let fields: Vec<_> = line.split('|').map(str::to_owned).collect();
        let market = fields[0].trim().parse::<i32>();
        if fields.len() < if stat { 35 } else { 21 } || market.is_err() {
            unparsed_lines.push((index + 1, line.to_owned()));
            continue;
        }
        let market_id_raw = market.map_err(|_| invalid("统计市场键无效"))?;
        let code = fields[1].trim();
        if code.is_empty() || code.len() > 32 || !code.is_ascii() || code.as_bytes().contains(&0) {
            return Err(invalid("统计证券键为空或无效"));
        }
        if !keys.insert((market_id_raw, code.to_owned())) {
            return Err(invalid("统计CFG存在重复市场/代码键"));
        }
        let candidate_values = if stat {
            StatsCandidateValues::Stat(TdxStatValues {
                stats_date_raw: text(&fields[4]),
                beta_60d: number(&fields[2]),
                pe_ttm: number(&fields[3]),
                free_float_shares_10k: number(&fields[11]),
                year_limit_up_days: integer(&fields[26]),
                limit_stat_days: integer(&fields[31]),
                limit_up_count_in_stat_days: integer(&fields[32]),
                limit_up_streak_days: integer(&fields[33]),
            })
        } else {
            StatsCandidateValues::Stat2(TdxStat2Values {
                stats_date_raw: text(&fields[2]),
                amount_10k: number(&fields[3]),
                seal_amount_10k: number(&fields[4]),
                prev_amount_10k: number(&fields[5]),
                prev_seal_amount_10k: number(&fields[6]),
                prev2_amount_10k: number(&fields[7]),
                prev2_seal_amount_10k: number(&fields[8]),
                open_volume_hand: number(&fields[9]),
                prev_open_volume_hand: number(&fields[10]),
                open_amount_10k: number(&fields[14]),
                prev_open_amount_10k: number(&fields[15]),
            })
        };
        rows.push(StatsRow {
            market_id_raw,
            code: code.into(),
            line_number: index + 1,
            fields_raw: fields,
            candidate_values,
        });
    }
    if rows.is_empty() {
        return Err(invalid("统计CFG没有可用记录"));
    }
    Ok(StatsMember {
        name: name.into(),
        crc32,
        compressed_size,
        rows,
        unparsed_lines,
        content: bytes.into(),
    })
}
pub fn read_tdx_stats(
    tcp: &mut crate::tcp::Tcp,
    path: &str,
    mut options: ResourceReadOptions,
) -> io::Result<TdxStatsResource> {
    options.validate()?;
    if options.max_bytes > MAX_STATS_ZIP_BYTES as u32 + 1 {
        options.max_bytes = MAX_STATS_ZIP_BYTES as u32 + 1;
    }
    tcp.with_timeout(options.timeout, |tcp| {
        let file = download_resource_file(tcp, path, options)?;
        let archive = parse_tdx_stats_archive(file.require_complete()?, &file.path)?;
        Ok(TdxStatsResource { file, archive })
    })
}
