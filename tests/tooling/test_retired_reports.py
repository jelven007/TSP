"""Public report removal keeps the maintained requirement contract mandatory."""
import json
from pathlib import Path

from check_docs import REQUIREMENT, check_requirements
from check_repository import REQUIRED_PATHS, check

ROOT = Path(__file__).resolve().parents[2]


def test_repository_layout_does_not_require_retired_reports(tmp_path):
    for name in (*REQUIRED_PATHS, "docs/evidence.md", "docs/requirement-ids.json"):
        if name.startswith("reports/"):
            continue
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert check(tmp_path) == []


def test_requirement_baseline_remains_required_in_docs(tmp_path):
    target = tmp_path / "docs"
    target.mkdir()
    text = (ROOT / "docs/requirements.md").read_text()
    (target / "requirements.md").write_text(text)
    identifiers = REQUIREMENT.findall(text)
    baseline = target / "requirement-ids.json"
    baseline.write_text(json.dumps(identifiers))
    assert check_requirements(tmp_path) == []
    baseline.unlink()
    assert any("missing preserved requirement baseline" in error
               for error in check_requirements(tmp_path))
