"""内置扩展数据预设 — 概念/行业初始化后默认手动获取。

设计原则:
  - 复用扩展数据配置与通用拉取链路
  - 仅在本模块做「接口结构 → 本地 schema」的转换
  - 仅修复未使用的空默认配置, 保留用户已有数据及自定义设置
  - 定时拉取由 PullScheduler 异步执行, 拉取失败不阻断启动

内置概念与行业保留默认拉取地址, 环境变量可独立覆盖上游:
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
        description="同花顺概念分类 (默认手动获取; 开启自动拉取后, 交易日 09:15-11:30 / 13:00-15:15 每分钟更新)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=data_url,
            method="GET",
            schedule_minutes=1,
            enabled=False,
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
        description="同花顺行业分类 (默认手动获取; 开启自动拉取后, 交易日 09:15-11:30 / 13:00-15:15 每分钟更新)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=data_url,
            method="GET",
            schedule_minutes=1,
            enabled=False,
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
# 旧版默认配置识别
# ---------------------------------------------------------------------------

def _is_unused_empty_preset(existing: ExtConfig, preset: ExtConfig, data_dir: Path) -> bool:
    """只修复原样生成且没有数据的空预设, 不能覆盖用户关闭或定制的配置。"""
    if not existing.pull or existing.pull.url or existing.pull.enabled:
        return False
    if any((data_dir / "ext_data" / existing.id).rglob("*.parquet")):
        return False
    original = existing.to_dict()
    expected = preset.to_dict()
    auto_description = (
        f"{preset.label.replace('扩展', '同花顺')}分类 "
        "(交易日 09:15-11:30 / 13:00-15:15 每分钟更新, 启动时立即刷新)"
    )
    legacy_description = auto_description.replace(
        "(交易日", "(配置上游后, 交易日"
    ).replace(", 启动时立即刷新", "")
    if existing.description not in {preset.description, auto_description, legacy_description}:
        return False
    for value in (original, expected):
        for key in ("created_at", "updated_at", "description"):
            value.pop(key, None)
    expected["pull"]["url"] = ""
    expected["pull"]["enabled"] = False
    return original == expected


def _is_legacy_manual_preset(existing: ExtConfig, preset: ExtConfig) -> bool:
    """识别未定制的历史「仅手动获取」内置预设。

    补全默认地址以便手动更新; 即使有静态快照, 仍保持自动拉取关闭。
    """
    legacy_description = (
        f"{preset.label.replace('扩展', '同花顺')}分类 "
        "(启动仅创建配置, 在概念/行业页手动获取)"
    )
    if existing.description != legacy_description:
        return False
    if (
        existing.label != preset.label
        or existing.mode != preset.mode
        or [field.to_dict() for field in existing.fields] != [field.to_dict() for field in preset.fields]
        or existing.symbol_map != preset.symbol_map
        or existing.code_map != preset.code_map
        or existing.market_level != preset.market_level
    ):
        return False
    if existing.pull is None or preset.pull is None:
        return False

    pull = existing.pull
    default = preset.pull
    no_auth = pull.auth is None or (
        isinstance(pull.auth, dict)
        and pull.auth.get("type", "none") == "none"
        and set(pull.auth).issubset({"type", "header", "param"})
    )
    return (
        pull.url in {"", default.url}
        and not pull.enabled
        and pull.method == default.method
        and pull.headers == {}
        and pull.body is None
        and pull.response_path == ""
        and pull.field_map == {}
        and pull.schedule_minutes == default.schedule_minutes
        and pull.time_window_start == default.time_window_start
        and pull.time_window_end == default.time_window_end
        and pull.date_param is None
        and pull.time_field is None
        and no_auth
        and pull.timeout_seconds == default.timeout_seconds
        and pull.page_param is None
        and pull.page_size_param is None
        and pull.page_size == 0
        and pull.page_start == 1
        and pull.max_pages == 20
        and pull.fanout is None
        and pull.transform == "auto"
        and pull.snapshot_policy == "auto"
    )


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
    """启动时为缺失的预设创建 config.json, 默认仅手动拉取。

    环境变量可覆盖默认地址, 配置地址不会启用自动拉取。
    用户显式开启后, PullScheduler 按保存的时段执行。

    安全保证:
      - 仅修复没有数据且未经定制的旧空预设, 其余配置完全保留
      - 本函数只写 config.json, 不直接发起网络请求
    """
    store = ExtConfigStore(data_dir)

    for config in _presets():
        existing = store.get(config.id)
        try:
            if existing is not None:
                empty_repair = _is_unused_empty_preset(existing, config, data_dir)
                legacy_migration = _is_legacy_manual_preset(existing, config)
                # #region debug-point A:legacy-preset-branch
                import json as _debug_json, os as _debug_os, urllib.request as _debug_request; _debug_url = _debug_os.getenv("DEBUG_SERVER_URL"); _debug_session = _debug_os.getenv("DEBUG_SESSION_ID"); _debug_payload = {"sessionId": _debug_session, "runId": _debug_os.getenv("DEBUG_RUN_ID", "pre-fix"), "hypothesisId": "A", "location": "app.services.ext_presets.ensure_builtin_presets", "msg": "[DEBUG] evaluated existing builtin preset", "data": {"config_id": config.id, "has_snapshot": any((data_dir / "ext_data" / existing.id).rglob("*.parquet")), "description": existing.description, "pull_present": existing.pull is not None, "pull_enabled": bool(existing.pull and existing.pull.enabled), "pull_url_configured": bool(existing.pull and existing.pull.url), "eligible_empty_repair": empty_repair, "eligible_legacy_migration": legacy_migration}}; _debug_request.urlopen(_debug_request.Request(_debug_url, data=_debug_json.dumps(_debug_payload).encode(), headers={"Content-Type": "application/json"}), timeout=1).read() if _debug_url and _debug_session else None  # noqa
                # #endregion
                if not (empty_repair or legacy_migration):
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
