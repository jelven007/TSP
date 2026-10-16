"""内置扩展数据预设 — 概念/行业可在交易日双时段内每分钟自动更新。

设计原则:
  - 复用扩展数据配置与通用拉取链路
  - 仅在本模块做「接口结构 → 本地 schema」的转换
  - 仅修复未使用的空默认配置, 保留用户已有数据及自定义设置
  - 定时拉取由 PullScheduler 异步执行, 拉取失败不阻断启动

内置概念与行业默认可拉取, 环境变量可独立覆盖上游:
  - EXT_CONCEPT_DATA_URL
  - EXT_INDUSTRY_DATA_URL
未配置或 URL 为空时沿用默认接口。

接入点: app.main.lifespan → ensure_builtin_presets(store.data_dir)
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

from app.config import settings
from app.services.ext_data import (
    ExtConfig,
    ExtConfigStore,
    ExtField,
    PullConfig,
)

logger = logging.getLogger(__name__)

_CONCEPT_DATA_URL = "https://shy313.com/api/plugins/market_flow/exports/ths-concepts"
_INDUSTRY_DATA_URL = "https://shy313.com/api/plugins/market_flow/exports/ths-industries"


# ---------------------------------------------------------------------------
# 预设定义: 字段结构 + 拉取配方
# ---------------------------------------------------------------------------

def _concept_preset() -> ExtConfig:
    """扩展概念 (ext_gn_ths)。

    接口结构: [{symbol, name, concepts: [概念1, 概念2, ...]}]
    本地 schema: 股票代码 / 股票简称 / 所属概念(分号拼接) / symbol / code
    """
    data_url = settings.ext_concept_data_url.strip() or _CONCEPT_DATA_URL
    return ExtConfig(
        id="ext_gn_ths",
        label="扩展概念",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
            ExtField("股票代码", "string", "股票代码"),
            ExtField("股票简称", "string", "股票简称"),
            ExtField("所属概念", "string", "所属概念"),
        ],
        description="同花顺概念分类 (交易日 09:15-11:30 / 13:00-15:15 每分钟更新, 启动时立即刷新)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=data_url,
            method="GET",
            schedule_minutes=1,
            enabled=bool(data_url),
            time_window_start="09:15",
            time_window_end="15:15",
        ),
    )


def _industry_preset() -> ExtConfig:
    """扩展行业 (ext_hy_ths)。

    接口结构: [{symbol, name, industries: [一级行业, 二级行业, 三级行业]}]
    本地 schema: 股票代码 / 股票简称 / 所属同花顺行业(横杠拼接) / symbol / code
    """
    data_url = settings.ext_industry_data_url.strip() or _INDUSTRY_DATA_URL
    return ExtConfig(
        id="ext_hy_ths",
        label="扩展行业",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
            ExtField("股票代码", "string", "股票代码"),
            ExtField("股票简称", "string", "股票简称"),
            ExtField("所属同花顺行业", "string", "所属同花顺行业"),
        ],
        description="同花顺行业分类 (交易日 09:15-11:30 / 13:00-15:15 每分钟更新, 启动时立即刷新)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=data_url,
            method="GET",
            schedule_minutes=1,
            enabled=bool(data_url),
            time_window_start="09:15",
            time_window_end="15:15",
        ),
    )


def _presets() -> list[ExtConfig]:
    return [_concept_preset(), _industry_preset()]


# ---------------------------------------------------------------------------
# 接口结构 → 本地 schema 转换 (仅预设使用)
# ---------------------------------------------------------------------------

def _symbol_to_code(symbol: str) -> str:
    """symbol (000001.SZ) → code (000001)。"""
    return symbol.split(".", 1)[0] if "." in symbol else symbol


def _dimension_label(value: object) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"nan", "none", "null"} else text


def _flatten_concept_rows(raw_rows: list[dict]) -> list[dict]:
    """概念: concepts 数组 → 分号拼接成「所属概念」字符串。

    [{symbol, name, concepts:[...]}] → [{股票代码, 股票简称, 所属概念, symbol, code}]
    注: code 由 symbol 派生 (000001.SZ → 000001), 因 rows_to_parquet 不执行 code_map。
    """
    out: list[dict] = []
    for r in raw_rows:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        concepts = r.get("concepts") or []
        labels = [label for c in concepts if (label := _dimension_label(c))]
        out.append({
            "股票代码": sym,
            "股票简称": r.get("name") or "",
            "所属概念": ";".join(labels),
            "symbol": sym,
            "code": _symbol_to_code(sym),
        })
    return out


def _flatten_industry_rows(raw_rows: list[dict]) -> list[dict]:
    """行业: industries 数组 → 横杠拼接成「所属同花顺行业」字符串。

    [{symbol, name, industries:[...]}] → [{股票代码, 股票简称, 所属同花顺行业, symbol, code}]
    """
    out: list[dict] = []
    for r in raw_rows:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        inds = r.get("industries") or []
        labels = [label for i in inds if (label := _dimension_label(i))]
        out.append({
            "股票代码": sym,
            "股票简称": r.get("name") or "",
            "所属同花顺行业": "-".join(labels),
            "symbol": sym,
            "code": _symbol_to_code(sym),
        })
    return out


# ---------------------------------------------------------------------------
# 旧版空默认配置识别
# ---------------------------------------------------------------------------

def _is_unused_empty_preset(existing: ExtConfig, preset: ExtConfig, data_dir: Path) -> bool:
    """只修复原样生成且没有数据的空预设, 不能覆盖用户关闭或定制的配置。"""
    if not existing.pull or existing.pull.url or existing.pull.enabled:
        return False
    if any((data_dir / "ext_data" / existing.id).rglob("*.parquet")):
        return False
    original = existing.to_dict()
    expected = preset.to_dict()
    legacy_description = preset.description.replace(
        "(交易日", "(配置上游后, 交易日"
    ).replace(", 启动时立即刷新", "")
    if existing.description not in {preset.description, legacy_description}:
        return False
    for value in (original, expected):
        for key in ("created_at", "updated_at", "description"):
            value.pop(key, None)
    expected["pull"]["url"] = ""
    expected["pull"]["enabled"] = False
    return original == expected


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def get_preset(config_id: str) -> ExtConfig | None:
    """按 id 取预设定义 (供 API 层校验 id 合法性)。"""
    for c in _presets():
        if c.id == config_id:
            return c
    return None


async def ensure_builtin_presets(data_dir: Path) -> None:
    """启动时为缺失的预设创建 config.json, 由调度器异步拉取。

    新建预设默认启用交易日
    09:15-11:30 / 13:00-15:15 每分钟拉取; PullScheduler 在启动后
    无视时段立即执行首轮。环境变量可覆盖默认地址。

    安全保证:
      - 仅修复没有数据且未经定制的旧空预设, 其余配置完全保留
      - 本函数只写 config.json, 不直接发起网络请求
    """
    store = ExtConfigStore(data_dir)

    for config in _presets():
        existing = store.get(config.id)
        try:
            if existing is not None:
                if not _is_unused_empty_preset(existing, config, data_dir):
                    continue
                existing.pull = config.pull
                existing.description = config.description
                config = existing
            store.upsert(config)
            logger.info(
                "内置扩展表 %s 配置已就绪 "
                "(自动拉取=%s, 交易日 09:15-11:30 / 13:00-15:15 每分钟更新)",
                config.id,
                bool(config.pull and config.pull.enabled),
            )
        except Exception as e:
            logger.warning("内置扩展表 %s 配置写入失败 (不影响启动): %s", config.id, e)


async def fetch_preset(config_id: str, data_dir: Path) -> int:
    """手动触发某个预设的数据拉取 (供 API 调用)。

    Raises:
        ValueError: config_id 不是内置预设
        Exception: 网络请求/解析/写入失败 (由 API 层转 HTTP 错误)
    """
    config = get_preset(config_id)
    if config is None:
        raise ValueError(f"未知的内置预设: {config_id}")

    # 与定时及扩展数据页手动拉取共用用户保存的请求、转换和北京时间落盘链路。
    from app.services.ext_pull import fetch_and_ingest

    await ensure_builtin_presets(data_dir)
    store = ExtConfigStore(data_dir)
    config = store.get(config_id)
    if config is None:
        raise ValueError(f"内置预设配置不可用: {config_id}")
    n, _ = await fetch_and_ingest(config, data_dir)
    logger.info("内置扩展表 %s 手动拉取成功: %d 行", config_id, n)
    return n
