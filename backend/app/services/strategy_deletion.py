"""Local result cleanup with compensation across SQLite and user files."""
import json
from contextlib import contextmanager
from pathlib import Path

from app.backtest import candidates
from app.services.strategy_results import StrategyResults


def _owned_path(root: Path, path: Path):
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("strategy result path is outside the data directory")
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError("symbolic strategy result path cannot be deleted")
        current = current.parent


@contextmanager
def delete_local_results(root: Path, strategy_id: str):
    """Hold the candidate writer lock and roll files back if SQL/source deletion fails."""
    root = Path(root).absolute()
    store = candidates.CandidateStore(root)
    backups = {}
    with candidates._lock:
        _owned_path(root, store.path)
        items = store._load()
        remaining = [item for item in items if item.get("config", {}).get("strategy_id") != strategy_id]
        if remaining != items:
            backups[store.path] = store.path.read_bytes()
        try:
            with StrategyResults(root).deleting_strategy(strategy_id):
                directory = root / "backtest_results" / "manifests"
                _owned_path(root, directory)
                manifests = []
                for path in directory.glob("*.json"):
                    _owned_path(root, path)
                    data = path.read_bytes()
                    value = json.loads(data)
                    if not isinstance(value, dict) or not isinstance(value.get("config"), dict):
                        raise ValueError("invalid backtest manifest")
                    if value["config"].get("strategy_id") == strategy_id:
                        manifests.append(path)
                        backups[path] = data
                if remaining != items:
                    store._write(remaining)
                for path in manifests:
                    path.unlink()
                yield
        except Exception:
            for path, content in backups.items():
                if not path.exists() or path.read_bytes() != content:
                    path.write_bytes(content)
            raise
