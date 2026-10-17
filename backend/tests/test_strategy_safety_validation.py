"""GH #475 回归: 只解析载荷, 不执行危险代码或访问用户策略目录。"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.strategy import router
from app.strategy import ai_generator
from app.strategy.ai_generator import AIStrategyGenerator
from app.strategy.engine import StrategyEngine

ESCAPES = {
    "getattr_alias": "g = getattr",
    "eval_alias": "run = eval\nrun('1')",
    "globals_alias": "namespace = globals",
    "open_in_container": "functions = [open]",
    "builtins_name": "namespace = __builtins__",
    "import_concat": 'imp = __builtins__["__imp" + "ort__"]',
    "class_subscript": 'value = row["__class__"]',
    "class_concat": 'value = row["__cla" + "ss__"]',
    "dunder_argument": 'row.get("__globals__")',
    "dunder_format": '"{0.__class__}".format(value)',
    "dunder_attribute": "value = obj.__getattribute__",
    "subclasses": "value = ().__class__.__base__.__subclasses__()",
    "memoryview_alias": "view = memoryview",
    "exit_alias": "stop = exit",
    "polars_os": "import polars as pl\nmodule = pl.os",
    "polars_contextlib": "import polars as pl\nmodule = pl.contextlib",
    "numpy_lib": "import numpy as np\nmodule = np.lib",
    "numpy_ctypes": "import numpy as np\nmodule = np.ctypeslib",
    "namespace_from_import": "from polars import os as module",
    "file_from_import": "from polars import read_csv as reader",
    "dunder_from_import": "from polars import __builtins__ as namespace",
    "relative_import": "from .polars import DataFrame",
    "star_import": "from app.backtest.matrix import *",
    "parent_package": "import app.strategy",
    "command_method": "run = obj.system",
    "popen_method": "run = obj.popen",
    "write_method": "writer = df.write_csv",
    "read_method": "import polars as pl\nreader = pl.read_parquet",
    "numpy_save": "import numpy as np\nwriter = np.save",
    "numpy_fromfile": "import numpy as np\nreader = np.fromfile",
    "module_constructor": "constructor = obj.ModuleType",
    "function_constructor": "constructor = obj.FunctionType",
}

LEGITIMATE = {
    "polars": """
        import polars as pl
        def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
            return (pl.col("close") > pl.col("open")) & (pl.col("volume") > 0)
    """,
    "matrix": """
        import numpy as np
        from app.backtest.matrix import MarketDataMatrix, SignalMatrix, valid_shift
        values = np.arange(10)
        entry = market.open > market.close
    """,
    "factor_research_local_path": """
        from app.strategy.builtin.factor_rank_research import FactorRankResearchStrategy
    """,
    "market_data": """
        from app.strategy.market_data import get_index_daily, get_daily
    """,
    "dotted_framework_import": """
        import app.backtest.matrix
        cls = app.backtest.matrix.MarketDataMatrix
    """,
    "main_guard": 'if __name__ == "__main__":\n    pass',
    "docstrings": """
        \"\"\"说明 __class__ 遍历被禁止。\"\"\"
        class Strategy:
            \"\"\"说明 __builtins__ 不能使用。\"\"\"
            def __init__(self):
                \"\"\"说明 __globals__ 不能使用。\"\"\"
                self.value = 1
    """,
    "normal_python": """
        def values(row: dict) -> tuple[str, float]:
            if isinstance(row, dict):
                return ("{}".format(row.get("name", "")), row[str("close")])
            return ("", 0.0)
        for i in range(3):
            value = arr[i]
    """,
}


@pytest.mark.parametrize("code", ESCAPES.values(), ids=ESCAPES.keys())
def test_known_escape_references_are_rejected(code):
    with pytest.raises(ValueError, match="禁止"):
        AIStrategyGenerator._validate_safety(code)


@pytest.mark.parametrize("code", LEGITIMATE.values(), ids=LEGITIMATE.keys())
def test_legitimate_strategy_code_passes(code):
    AIStrategyGenerator._validate_safety(textwrap.dedent(code))


def test_all_shipped_builtin_strategies_pass():
    root = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"
    dependency_names = frozenset(
        path.stem for path in root.glob("_*.py") if path.name != "__init__.py"
    )
    paths = sorted(path for path in root.glob("*.py") if not path.name.startswith("_"))
    assert paths, "兼容性校验必须覆盖实际内置策略"
    for path in paths:
        AIStrategyGenerator._validate_safety(
            path.read_text(encoding="utf-8"),
            extra_allowed_import_modules=dependency_names,
        )


@pytest.mark.parametrize("code", [
    "import polars as pl\nhandle = pl._tsp_probe_module",
    "from polars import _tsp_probe_module as handle",
])
def test_module_handles_follow_loaded_library_namespace(monkeypatch, code):
    monkeypatch.setattr(pl, "_tsp_probe_module", ModuleType("probe"), raising=False)
    monkeypatch.setattr(ai_generator, "_MODULE_ATTR_CACHE", {}, raising=False)
    with pytest.raises(ValueError, match="模块对象"):
        AIStrategyGenerator._validate_safety(code)


def test_dependency_probe_does_not_import_user_named_module(monkeypatch):
    module_name = "_tsp_unloaded_strategy_helper"
    assert module_name not in sys.modules
    import_module = Mock(side_effect=AssertionError("校验不得执行用户模块"))
    monkeypatch.setattr(ai_generator, "importlib", SimpleNamespace(import_module=import_module),
                        raising=False)
    AIStrategyGenerator._validate_safety(
        f"from {module_name} import helper",
        extra_allowed_import_modules=frozenset({module_name}),
    )
    import_module.assert_not_called()


def _code(extra: str = "") -> str:
    return (
        "import polars as pl\n"
        'META = {"id": "custom_safe", "name": "safe", "scoring": {}}\n'
        "def filter(df, params):\n"
        "    return pl.lit(True)\n"
        + extra
    )


@pytest.mark.parametrize("code", ["run = eval", 'imp = __builtins__["__imp" + "ort__"]'])
def test_generator_rejects_before_extracting_metadata(monkeypatch, code):
    extract = Mock(side_effect=AssertionError("危险代码不能进入后续处理"))
    monkeypatch.setattr(AIStrategyGenerator, "_extract_meta", extract)
    result = AIStrategyGenerator().validate_code(_code(code))
    assert result["valid"] is False
    assert "禁止" in result["error"]
    extract.assert_not_called()


@pytest.mark.parametrize("dependency_code", ["run = eval", "def f(obj):\n    return vars(obj)\n"])
def test_engine_rejects_unsafe_dependency_before_import(tmp_path, monkeypatch, dependency_code):
    main = tmp_path / "custom_safe.py"
    main.write_text(_code("import _helpers\n"), encoding="utf-8")
    (tmp_path / "_helpers.py").write_text(dependency_code, encoding="utf-8")
    _assert_load_rejected_before_import(main, monkeypatch, ValueError, "禁止")


def _assert_load_rejected_before_import(path, monkeypatch, error_type, message):
    spec = Mock(side_effect=AssertionError("未通过校验的文件不能进入模块加载"))
    monkeypatch.setattr("app.strategy.engine.importlib.util.spec_from_file_location", spec)
    with pytest.raises(error_type, match=message):
        StrategyEngine._load_file(path)
    spec.assert_not_called()


def test_engine_rejects_directly_placed_payload_before_import(tmp_path, monkeypatch):
    path = tmp_path / "custom_safe.py"
    path.write_text(_code("run = eval\n"), encoding="utf-8")
    _assert_load_rejected_before_import(path, monkeypatch, ValueError, "禁止")


@pytest.mark.parametrize("target", ["main", "dependency"])
@pytest.mark.parametrize("failure", ["directory", "permission", "encoding", "syntax", "unexpected"])
def test_engine_fails_closed_on_validation_errors(tmp_path, monkeypatch, target, failure):
    main = tmp_path / "custom_safe.py"
    main.write_text(_code(), encoding="utf-8")
    path = main if target == "main" else tmp_path / "_helpers.py"
    if target == "dependency":
        path.write_text("VALUE = 1\n", encoding="utf-8")
    error_type, message = ValueError, "安全校验失败"
    if failure == "directory":
        path.unlink()
        path.mkdir()
    elif failure == "permission":
        read_text = Path.read_text

        def unreadable(self, *args, **kwargs):
            if self == path:
                raise PermissionError("permission denied")
            return read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", unreadable)
    elif failure == "encoding":
        path.write_bytes(b"\xff")
    elif failure == "syntax":
        path.write_text("def broken(:\n", encoding="utf-8")
        message = "策略语法错误"
    else:
        validate = AIStrategyGenerator._validate_safety
        failing_code = path.read_text(encoding="utf-8")

        def unavailable(code, **kwargs):
            if code == failing_code:
                raise RuntimeError("validator unavailable")
            return validate(code, **kwargs)

        monkeypatch.setattr(AIStrategyGenerator, "_validate_safety", unavailable)
        error_type, message = RuntimeError, "validator unavailable"
    _assert_load_rejected_before_import(main, monkeypatch, error_type, message)


def test_safe_typed_dependency_still_loads_and_filters(tmp_path):
    (tmp_path / "_tsp_safe_helper.py").write_text(
        "from collections.abc import Mapping\n"
        "from types import SimpleNamespace\n"
        "from typing import Any\n"
        "def threshold(params: Mapping[str, Any]) -> float:\n"
        "    return SimpleNamespace(value=params.get('min', 1)).value\n",
        encoding="utf-8",
    )
    path = tmp_path / "custom_safe.py"
    path.write_text(
        _code("from _tsp_safe_helper import threshold\n")
        .replace("return pl.lit(True)", 'return pl.col("close") > threshold(params)'),
        encoding="utf-8",
    )
    try:
        strategy = StrategyEngine._load_file(path)
        df = pl.DataFrame({"close": [1.0, 3.0]})
        assert df.filter(strategy.filter_fn(df, {"min": 2}))["close"].to_list() == [3.0]
    finally:
        sys.modules.pop("_tsp_safe_helper", None)
        sys.modules.pop("custom_safe", None)


@pytest.mark.parametrize("escape", ["run = eval", 'imp = __builtins__["__imp" + "ort__"]'])
def test_http_validate_and_save_reject_without_writing_or_loading(tmp_path, monkeypatch, escape):
    engine = StrategyEngine([tmp_path / "strategies" / "custom"])
    app = FastAPI()
    app.state.strategy_engine = engine
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(router)
    load = Mock(side_effect=AssertionError("危险策略不得加载"))
    monkeypatch.setattr(StrategyEngine, "_load_file", load)
    body = {"strategy_id": "custom_safe", "code": _code(escape)}
    with TestClient(app) as client:
        validated = client.post("/api/strategies/code/validate", json=body)
        assert validated.status_code == 200
        assert validated.json()["valid"] is False
        assert "禁止" in validated.json()["error"]
        saved = client.post("/api/strategies/code/save", json={
            **body, "target_source": "custom", "mode": "create",
        })
        assert saved.status_code == 400
        assert "禁止" in saved.json()["detail"]
    assert not list(tmp_path.rglob("*.py"))
    assert not engine.has("custom_safe")
    load.assert_not_called()


def test_http_rejected_update_keeps_previous_file_and_registry(tmp_path, monkeypatch):
    path = tmp_path / "custom_safe.py"
    previous_code = _code()
    path.write_text(previous_code, encoding="utf-8")
    engine = StrategyEngine([tmp_path])
    previous = engine.get("custom_safe")
    app = FastAPI()
    app.state.strategy_engine = engine
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(router)
    reload_engine = Mock(side_effect=AssertionError("拒绝的更新不得刷新策略缓存"))
    monkeypatch.setattr(engine, "reload", reload_engine)
    with TestClient(app) as client:
        response = client.post("/api/strategies/code/save", json={
            "strategy_id": "custom_safe", "code": _code("run = eval"),
            "target_source": "custom", "mode": "update",
        })
    assert response.status_code == 400
    assert "禁止" in response.json()["detail"]
    assert path.read_text(encoding="utf-8") == previous_code
    assert engine.get("custom_safe") is previous
    reload_engine.assert_not_called()


def test_tenant_save_rejects_before_compute_or_private_document_write(tmp_path, monkeypatch):
    from app.api.strategy import StrategyCodeSaveRequest, _save_strategy_code
    from app.multitenancy.strategies import TenantStrategyEngine

    documents = SimpleNamespace(
        context=SimpleNamespace(tenant_id="tenant-a"),
        get=lambda collection, key, default=None: default,
        records=lambda collection: [],
        values=lambda collection: [],
        put=Mock(side_effect=AssertionError("危险策略不得写入租户存储")),
    )
    executor = SimpleNamespace(execute=Mock(side_effect=AssertionError("危险策略不得送入计算服务")))
    engine = TenantStrategyEngine(documents, StrategyEngine([]), executor)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(strategy_engine=engine)))
    monkeypatch.setattr("app.api.strategy._get_engine", lambda request: engine)
    monkeypatch.setattr("app.api.strategy._data_dir", lambda request: documents)
    req = StrategyCodeSaveRequest(
        strategy_id="custom_safe", target_source="custom", mode="create", code=_code("run = eval"),
    )
    with pytest.raises(ValueError, match="禁止"):
        _save_strategy_code(req, request)
    executor.execute.assert_not_called()
    documents.put.assert_not_called()
    assert not engine.has("custom_safe")
