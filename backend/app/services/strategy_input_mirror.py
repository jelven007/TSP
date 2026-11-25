"""S02权威输入包传输: 受控打包、SHA/文件树校验, 再验语义及实时接收期限。

不传业务凭据或租户账本; 接收失败不能回退为本地独立结果。
跨平台门禁见docs/release-pipeline.md, 策略身份见docs/reference/s02.md。
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import tempfile
from pathlib import Path

import httpx

from app.services.strategy_input_manifest import canonical, common_key, validate_manifest

PACK_SHA256 = "2ee64171ab54cf9f7ea390ae96fba32db55cf9426dc4a366fb1df2c64a93e403"
# 受控入口字节摘要是安装契约, 更新入口时须同步核验, 不能跳过门禁。
MAX_PACKAGE_BYTES = 16 * 1024 * 1024


def pack_tool():
    """核对TSP_PACK_SCRIPT字节后加载统一打包工具, 拒绝任意替换脚本。"""
    path = Path(os.environ["TSP_PACK_SCRIPT"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != PACK_SHA256:
        raise ValueError("受控打包入口摘要不一致")
    spec = importlib.util.spec_from_file_location("tsp_controlled_package_transfer", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def package(store, key, phase):
    """只打包已封存且语义有效的公开阶段清单, 返回包SHA与输入SHA。"""
    value = store.read_key(key, phase)
    if value is None:
        return None
    with tempfile.TemporaryDirectory(prefix="tsp-s02-package-") as directory:
        root = Path(directory).resolve()
        source = root / "s02"
        source.mkdir()
        (source / "input.json").write_bytes(canonical(value))
        archive = root / "input.tar.gz"
        report = pack_tool().create(source, archive)
        if archive.stat().st_size > MAX_PACKAGE_BYTES:
            raise ValueError("S02输入包超出大小限制")
        return {"key": key, "phase": phase, "sha256": report["sha256"],
                "input_sha256": value["input_sha256"],
                "archive": base64.b64encode(archive.read_bytes()).decode("ascii")}


def install(store, bundle, key, phase, *, accept=None):
    """按身份→包SHA→解包树→清单语义→接收期限顺序校验, 之后才封存。"""
    if bundle["key"] != key or bundle["phase"] != phase:
        raise ValueError("镜像输入身份不一致")
    encoded = bundle["archive"]
    if not isinstance(encoded, str) or len(encoded) > MAX_PACKAGE_BYTES * 4 // 3 + 4:
        raise ValueError("S02输入包超出大小限制")
    with tempfile.TemporaryDirectory(prefix="tsp-s02-receive-") as directory:
        root = Path(directory).resolve()
        archive = root / "input.tar.gz"
        archive.write_bytes(base64.b64decode(encoded, validate=True))
        tool = pack_tool()
        tool.verify(archive, bundle["sha256"])
        tree = root / "received"
        tool.extract(archive, tree, bundle["sha256"])
        tool.verify_tree(tree, archive, bundle["sha256"])
        value = json.loads((tree / "s02/input.json").read_text())
        validate_manifest(value)
        if (value["input_sha256"] != bundle["input_sha256"]
                or common_key(value["input"]) != key or value["phase"] != phase):
            raise ValueError("镜像输入语义身份不一致")
        if accept is not None and not accept():
            # 传输期间可能过期或停止; 保留诊断证据, 不混入实时ready文件。
            from app.services.fs_utils import atomic_write_text
            path = store.path(key, phase).with_suffix(".diagnostic.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, canonical({**value, "diagnostic_only": True}).decode())
            return None
        return store.seal(value)


class RemoteMirror:
    """使用受控认证连接权威端; 只读阶段包, 不重新请求本地行情。"""
    def __init__(self):
        self.client = httpx.Client(
            base_url=os.environ["TSP_S02_AUTHORITY_URL"].rstrip("/"),
            headers={"Authorization": "Bearer " + os.environ["TSP_S02_AUTHORITY_TOKEN"]},
            timeout=15, follow_redirects=False, trust_env=False)

    def close(self):
        self.client.close()

    def receive(self, store, key, phase, *, accept=None):
        """缺权威包返回None; HTTP/大小/身份错误交输入服务保存等待原因。"""
        response = self.client.get("/internal/cell-market/s02-input/package",
                                   params={"key": key, "phase": phase})
        response.raise_for_status()
        if len(response.content) > MAX_PACKAGE_BYTES * 4 // 3 + 4096:
            raise ValueError("S02输入响应超出大小限制")
        bundle = response.json()
        if bundle.get("state") == "waiting_input":
            return None
        return install(store, bundle, key, phase, accept=accept)
