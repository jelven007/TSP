mod key;
pub use key::KEY;
mod fq;
pub use fq::*;

use crate::{Result, bytes_helper::*};

use std::collections::HashMap;
/// gbbq 按**含市场前缀的完整代码**（如 `sh600000`）分组的 HashMap，
/// 与 [`crate::file::day::Day::code`] 的格式保持一致。
pub type StockGbbq<'a> = HashMap<String, Vec<Gbbq<'a>>>;

/// 股本变迁 (gbbq) 文件。
///
/// ## 注意
/// 开启 `serde` feature 时，此结构体的序列化 (serialize) 时：
/// `date` 为 `年-月-日` 格式。
#[derive(Debug, Clone, serde::Serialize)]
pub struct Gbbq<'a> {
    pub market: u8,
    /// 6 位股票代码
    pub code: &'a str,
    /// 日期
    #[serde(serialize_with = "ser_date_string")]
    pub date: u32,
    /// 信息类型
    ///
    /// |   | 类别                           |    | 类别                                    |
    /// | - | ------------------------------ | -- | --------------------------------------- |
    /// | 1 | 除权除息                       | 8  | 增发新股上市                            |
    /// | 2 | 送配股上市                     | 9  | 转配股上市                              |
    /// | 3 | 非流通股上市股                 | 10 | 可转债上市                              |
    /// | 4 | 未知股本变动                   | 11 | 扩缩股 songgu:比例                      |
    /// | 5 | 股本变化                       | 12 | 非流通股缩股 songgu: 比例               |
    /// | 6 | 增发新股 peigujia; songgu:未知 | 13 | 送认购权证 songgu: 份数; hongli: 行权价 |
    /// | 7 | 股份回购                       | 14 | 送认沽权证 songgu: 份数; hongli: 行权价 |
    ///
    /// 例子：查找第一次出现各种类别的数据，运行 `cargo run --example gbbq_category_unique`
    ///
    /// ```ignore
    /// use itertools::Itertools;
    /// use rustdx_complete::file::gbbq::{Gbbq, Gbbqs};
    ///
    /// fn main() {
    ///     let mut gbbqs = Gbbqs::from_file("tests/fixtures/tdx/gbbq").unwrap();
    ///     let v: Vec<_> = gbbqs.to_vec().into_iter()
    ///         .unique_by(|Gbbq { category, .. }| *category).collect();
    ///     println!("len: {}\n{:#?}", v.len(), v);
    /// }
    /// ```
    pub category: u8,
    /// 分红（每 10 股派现金 x 元）| 前流通盘：
    pub fh_qltp: f32,
    /// 配股价（每股配股价 x 元）| 前总股本
    pub pgj_qzgb: f32,
    /// 送转股（每 10 股送转股比例 x 股） | 后流通盘
    pub sg_hltp: f32,
    /// 配股（每 10 股配股比例 x 股）| 后总股本：
    pub pg_hzgb: f32,
}

impl<'a> Gbbq<'a> {
    #[inline]
    pub fn from_chunk(chunk: &'a [u8]) -> Gbbq<'a> {
        Self {
            market: u8_from_le_bytes(chunk, 0),
            code: std::str::from_utf8(&chunk[1..7]).unwrap_or(""),
            date: u32_from_le_bytes(chunk, 8),
            category: u8_from_le_bytes(chunk, 12),
            fh_qltp: f32_from_le_bytes(chunk, 13),
            pgj_qzgb: f32_from_le_bytes(chunk, 17),
            sg_hltp: f32_from_le_bytes(chunk, 21),
            pg_hzgb: f32_from_le_bytes(chunk, 25),
        }
    }

    pub fn try_from_chunk(chunk: &'a [u8]) -> Result<Gbbq<'a>> {
        if chunk.len() != 29 {
            return Err(crate::Error::Custom("gbbq记录必须为29字节"));
        }
        let code = std::str::from_utf8(&chunk[1..7])
            .map_err(|_| crate::Error::Custom("gbbq代码编码非法"))?;
        if !code.bytes().all(|b| b.is_ascii_digit()) || chunk[0] > 2 {
            return Err(crate::Error::Custom("gbbq市场或代码非法"));
        }
        let row = Self::from_chunk(chunk);
        crate::data::date_from_u32(row.date)?;
        if !(1..=14).contains(&row.category)
            || ![row.fh_qltp, row.pgj_qzgb, row.sg_hltp, row.pg_hzgb]
                .iter()
                .all(|v| v.is_finite())
        {
            return Err(crate::Error::Custom("gbbq类别或数值非法"));
        }
        Ok(row)
    }

    // 未解密二进制数据转化成 [`Gbbq`]
    pub fn iter(bytes: &mut [u8]) -> impl Iterator<Item = Gbbq<'_>> {
        bytes
            .as_chunks_mut::<29>()
            .0
            .iter_mut()
            .map(|c| parse(c))
            .map(Gbbq::from_chunk)
        // bytes.chunks_exact_mut(29).map(parse).map(Gbbq::from_chunk_mut)
    }

    // 解密二进制数据转化成 [`Gbbq`]
    pub fn iter_deciphered(bytes: &'a [u8]) -> impl Iterator<Item = Gbbq<'a>> {
        bytes
            .as_chunks::<29>()
            .0
            .iter()
            .map(|c| Self::from_chunk(c))
    }

    #[inline]
    pub fn compute_pre_pct(&self, close: f64, mut preclose: f64, flag: bool) -> [f64; 3] {
        if flag {
            preclose = (preclose * 10. - self.fh_qltp as f64
                + self.pg_hzgb as f64 * self.pgj_qzgb as f64)
                / (10. + self.pg_hzgb as f64 + self.sg_hltp as f64)
        }

        [preclose, close, close / preclose]
    }

    /// 把 `gbbq` 文件的分红送股信息（category = 1）全部提取出来变成 HashMap 数据类型：
    /// key 为**含市场前缀的完整代码**（如 `sh600000`），与 [`crate::file::day::Day::code`] 一致。
    ///
    /// 说明：gbbq 中 `market` 字段 0 = 深圳、1 = 上海（与通达信一致），
    /// 拼前缀后可直接与 day 文件的完整代码匹配，不再经过 `u32` 转换。
    pub fn filter_hashmap(gbbq: impl Iterator<Item = Self>) -> StockGbbq<'a> {
        let mut hm: StockGbbq<'a> = HashMap::with_capacity(6000);
        for g in gbbq.filter(|g| {
            g.category == 1 && g.code.len() == 6 && g.code.bytes().all(|b| b.is_ascii_digit())
        }) {
            let prefix = match g.market {
                0 => "sz",
                1 => "sh",
                2 => "bj",
                _ => continue,
            };
            hm.entry(format!("{prefix}{}", g.code)).or_default().push(g);
        }
        for events in hm.values_mut() {
            events.sort_by_key(|g| g.date);
        }
        hm
    }
}

pub struct Gbbqs {
    data: Vec<u8>,
    /// 股本变迁的记录条数。这个数据在读取 `gbbq` 文件时就已经被解析了。
    /// 因为该文件前 4 个字节的含义就是记录条数。
    ///
    /// 注：`gbbq` 的二进制字节长度 = 4 + count * 29
    pub count: usize,
    /// 是否已经解密。
    pub parsed: bool,
}

impl Gbbqs {
    pub fn from_file(p: impl AsRef<std::path::Path>) -> Result<Self> {
        Self::from_bytes(std::fs::read(p)?)
    }

    pub fn from_bytes(vec: Vec<u8>) -> Result<Self> {
        if vec.len() < 4 || !(vec.len() - 4).is_multiple_of(29) {
            return Err(crate::Error::Custom("gbbq文件头或记录截断"));
        }
        let count = u32_from_le_bytes(&vec, 0) as usize;
        if count != (vec.len() - 4) / 29 {
            return Err(crate::Error::Custom("gbbq记录数与文件长度不符"));
        }
        Ok(Self {
            data: vec,
            count,
            parsed: false,
        })
    }

    /// 产生 [`Gbbq`] 的 `Vec` 。
    ///
    /// 注意：
    /// 1. 未调用此方法之前，[`Gbbqs::get_data`] 或 [`Gbbqs::get_data_mut`]
    ///    的结果为原始的、未解密的二进制数据。
    /// 2. 当第一次调用这个方法之后，[`Gbbqs::get_data`] 或 [`Gbbqs::get_data_mut`]
    ///    的结果为解密后的二进制数据。
    pub fn to_vec(&mut self) -> Vec<Gbbq<'_>> {
        if self.parsed {
            self.data[4..]
                .as_chunks::<29>()
                .0
                .iter()
                .map(|c| Gbbq::from_chunk(c))
                .collect()
        } else {
            let res = self.data[4..]
                .as_chunks_mut::<29>()
                .0
                .iter_mut()
                .map(|c| parse(c))
                .map(Gbbq::from_chunk)
                // .map(Gbbq::from_chunk_mut)
                .collect();
            self.parsed = true;
            res
        }
    }

    /// Decode at most once and validate every borrowed record.
    pub fn try_to_vec(&mut self) -> Result<Vec<Gbbq<'_>>> {
        if !self.parsed {
            for chunk in self.data[4..].as_chunks_mut::<29>().0 {
                try_parse(chunk)?;
            }
            self.parsed = true;
        }
        self.data[4..]
            .as_chunks::<29>()
            .0
            .iter()
            .map(|chunk| Gbbq::try_from_chunk(chunk))
            .collect()
    }

    /// 获取 `gbbq` 文件的二进制数据的共享引用，注意：
    /// 1. 未调用 [`Gbbqs::to_vec`] 之前，[`Gbbqs::get_data`] 返回的是原始的、未解密的数据；
    /// 2. 调用一次 [`Gbbqs::to_vec`] 之后，[`Gbbqs::get_data`] 返回的是加密后的二进制数据；
    /// 3. 此二进制数据的引用已剔除前 4 个字节，
    ///    因为这四个字节只表示该 `gbbq` 文件所含的记录条数，
    ///    如果需要知道这个条数，请调用 `.count` 。
    pub fn get_data(&self) -> &[u8] {
        &self.data[4..]
    }

    /// 获取 `gbbq` 文件的二进制数据的独占引用，
    /// 除了引用类型的区别外，与 [`Gbbqs::get_data`] 无区别。
    pub fn get_data_mut(&mut self) -> &mut [u8] {
        &mut self.data[4..]
    }
}

/// 加密数据必须分成 29 个 u8 为一组，每次解析一组。
///
/// 参考资料：
/// 1. [【pytdx】 `pytdx.reader.gbbq_reader` ]
/// 2. [【CSDN】通达信股本变迁文件（gbbq）解密方法]
/// 3. [【新浪博客】通达信权息文件]
///
///
/// [【新浪博客】通达信权息文件]: http://blog.sina.com.cn/s/blog_6b2f87db0102uxo3.html
/// [【CSDN】通达信股本变迁文件（gbbq）解密方法]: https://blog.csdn.net/fangle6688/article/details/50956609
/// [【pytdx】 `pytdx.reader.gbbq_reader` ]: https://github.com/rainx/pytdx/blob/2857fdad08534533610bd2aeca387f760c4baa42/pytdx/reader/gbbq_reader.py#L29-L77
///
/// TODO: item 改写成 &mut [u8] ，`GbbqRaw` 需要增加 item 字段
pub fn parse(encrypt: &mut [u8]) -> &[u8] {
    try_parse(encrypt).expect("gbbq记录必须为29字节")
}

/// Safe deciphering entrypoint. A rejected length leaves the input untouched.
pub fn try_parse(encrypt: &mut [u8]) -> Result<&[u8]> {
    if encrypt.len() != 29 {
        return Err(crate::Error::Custom("gbbq记录必须为29字节"));
    }
    let mut pos = 0usize;
    for i in (0usize..24).step_by(8) {
        let mut eax = u32_from_le_bytes(KEY, 0x44);
        let mut ebx = u32_from_le_bytes(encrypt, pos);
        let mut num = eax ^ ebx;
        let mut numold = u32_from_le_bytes(encrypt, pos + 4);
        for j in (4usize..68).step_by(4).rev() {
            ebx = (num & 0xff0000) >> 16;
            eax = u32_from_le_bytes(KEY, ebx as usize * 4 + 0x448);
            ebx = num >> 24;
            let mut eax_add = u32_from_le_bytes(KEY, ebx as usize * 4 + 0x48);
            eax = eax.overflowing_add(eax_add).0;
            ebx = (num & 0xff00) >> 8;
            let mut eax_xor = u32_from_le_bytes(KEY, ebx as usize * 4 + 0x848);
            eax ^= eax_xor;
            ebx = num & 0xff;
            eax_add = u32_from_le_bytes(KEY, ebx as usize * 4 + 0xc48);
            eax = eax.overflowing_add(eax_add).0;
            eax_xor = u32_from_le_bytes(KEY, j);
            eax ^= eax_xor;
            ebx = num;
            num = numold ^ eax;
            numold = ebx;
        }
        numold ^= u32_from_le_bytes(KEY, 0);
        encrypt[i..i + 4].copy_from_slice(&numold.to_le_bytes());
        encrypt[i + 4..i + 8].copy_from_slice(&num.to_le_bytes());
        pos += 8;
    }
    Ok(encrypt)
}
