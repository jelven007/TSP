"""Filesystem account deletion with bounded ownership and failure recovery."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from app.services import alert_store
from app.services.fs_utils import atomic_write_text
from app.strategy import paper


def delete_files(data_dir: Path, account_id: str) -> bool:
    """Caller holds PAPER_LOCK; alerts share the same lock ordering as fills."""
    root = paper._root(data_dir, account_id)
    base = paper.accounts_base(data_dir)
    if (base.is_symlink() or base.parent.is_symlink() or root.is_symlink()
            or root.resolve().parent != base.resolve()):
        raise ValueError("模拟账户删除路径不安全")
    if not root.exists():
        return False
    if not (root / "account.json").is_file():
        raise ValueError("模拟账户记录缺失, 拒绝删除")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("模拟账户包含符号链接, 拒绝删除")
    alert_path = Path(data_dir) / "user_data" / "alerts.jsonl"
    if alert_path.is_symlink() or alert_path.parent.is_symlink():
        raise ValueError("提醒记录路径不安全")
    with alert_store._lock:
        original = alert_path.read_text(encoding="utf-8") if alert_path.exists() else None
        kept = []
        for line in (original or "").splitlines(keepends=True):
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                kept.append(line)
                continue
            if event.get("source") != "paper" or event.get("account_id") != account_id:
                kept.append(line)
        # Recovery copy is outside the live account directory. No destructive
        # step starts until both scope validation and the complete copy succeed.
        with tempfile.TemporaryDirectory(prefix="tsp-paper-delete-") as directory:
            backup = Path(directory) / "account"
            shutil.copytree(root, backup)
            try:
                shutil.rmtree(root)
                if original is not None and "".join(kept) != original:
                    atomic_write_text(alert_path, "".join(kept))
            except Exception:
                shutil.copytree(backup, root, dirs_exist_ok=True)
                raise
    return True
