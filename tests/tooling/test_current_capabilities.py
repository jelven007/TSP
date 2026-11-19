"""Machine-check current facts while preserving the meaning of dated history."""
import ast
import json
from pathlib import Path


def test_current_capabilities_match_schema_pool_and_deployment_docs():
    root = Path(__file__).resolve().parents[2]
    path = root / "docs/current-capabilities.json"
    assert path.exists()
    facts = json.loads(path.read_text())
    assert facts["application_version_source"] == "VERSION"
    for kind in ("control", "data"):
        versions = {
            int(path.stem.split("_v")[1])
            for path in (root / "backend/app/cell/sql").glob(f"{kind}_v*.sql")
        }
        assert facts["schema_target"] == max(versions)
        assert versions == set(range(1, facts["schema_target"] + 1))
    tree = ast.parse((root / "backend/app/strategy/daily_pool.py").read_text())
    definition = next(
        ast.literal_eval(node.value) for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "DEFINITION_VERSION" for target in node.targets)
    )
    assert facts["strategy_pool_definition"] == definition
    architecture = (root / "docs/architecture.md").read_text().split('id="current-1-架构摘要"')[1]
    assert f"schema v{facts['schema_target']}" in architecture.split("### 2.", 1)[0]
    deploy = (root / "deploy/full/README.md").read_text()
    assert f"迁移目标均为 schema v{facts['schema_target']}" in deploy
