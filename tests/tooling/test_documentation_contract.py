"""Failure-oriented checks for moved docs and preserved product contracts."""

from pathlib import Path

import pytest
from check_docs import MODULES, anchors, check, check_links, check_requirements

ROOT = Path(__file__).resolve().parents[2]


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_repository_documentation_contract():
    assert check(ROOT) == []


def test_moved_relative_link_requires_correct_parent_and_anchor(tmp_path):
    write(tmp_path, "docs/target.md", "# 原价 / 时间\n")
    path = write(tmp_path, "docs/architecture/contract.md", "[规则](../target.md#原价--时间)\n")
    assert check_links(tmp_path, [path]) == []
    path.write_text("[规则](target.md#原价--时间)\n")
    assert "missing link target" in check_links(tmp_path, [path])[0]
    path.write_text("[规则](../target.md#不存在)\n")
    assert "missing anchor" in check_links(tmp_path, [path])[0]


def test_unicode_url_explicit_anchor_duplicates_and_fenced_example(tmp_path):
    path = write(tmp_path, "中文.md", """# 标题
# 标题
<a id="cite-1"></a>
[第二节](#%E6%A0%87%E9%A2%98-1)
[证据](#cite-1)
```markdown
[示例](不存在.md)
```
`[代码](不存在.md)`
[定义][出处]
[出处]: 中文.md#标题
[1][2] 为正文中的相邻数字引用。
""")
    assert check_links(tmp_path, [path]) == []
    assert {"标题", "标题-1", "cite-1"} <= anchors(path.read_text())


def test_unclosed_fence_and_missing_reference_are_errors(tmp_path):
    path = write(tmp_path, "README.md", "[正文][来源]\n```python\npass\n")
    errors = check_links(tmp_path, [path])
    assert any("unclosed code fence" in error for error in errors)
    assert any("missing reference definition" in error for error in errors)


def test_remote_links_are_not_requested_and_local_assets_must_exist(tmp_path):
    path = write(tmp_path, "README.md", "[外部](https://example.invalid/)\n![原图](assets/source.png)\n")
    assert check_links(tmp_path, [path]) == ["README.md: missing link target: assets/source.png"]


@pytest.fixture
def requirement_repo(tmp_path):
    links = []
    sections = []
    ids = {"NFR-01"}
    for i, slug in enumerate(MODULES, 1):
        ident = f"FR-TEST-{i:02}"
        ids.add(ident)
        links.append(f"[模块](#req-{slug})")
        sections.append(f'<a id="req-{slug}"></a>\n### 模块{i}\n'
                        f"| `{ident}` | 已实现 S0 | P0 | 输入 | 结果 | 异常 |\n")
    write(tmp_path, "docs/requirements.md",
          "# 需求\n状态依据：源码，不等于生产签收。\n"  # noqa: RUF001 - Preserve Chinese document punctuation.
          + "\n".join(links) + "\n## 功能需求\n" + "\n".join(sections)
          + "\n## 非功能需求\n| `NFR-01` | 已实现 | 兼容 |\n")
    return tmp_path, ids


def test_requirement_baseline_is_preserved_once(requirement_repo):
    root, ids = requirement_repo
    assert check_requirements(root, ids) == []
    path = root / "docs/requirements.md"
    path.write_text(path.read_text() + "| `FR-TEST-01` | 已实现 S0 | P0 | 输入 | 结果 | 异常 |\n")
    assert "duplicate requirement: FR-TEST-01 (2)" in check_requirements(root, ids)
    path.write_text(path.read_text().replace("FR-TEST-01", "FR-TEST-99"))
    assert "missing preserved requirement: FR-TEST-01" in check_requirements(root, ids)


@pytest.mark.parametrize("name", ["docs/requirements/extra.md", "docs/reference/duplicate.md"])
def test_parallel_document_cannot_hide_additional_definitions(requirement_repo, name):
    root, ids = requirement_repo
    write(root, name, "| `FR-TEST-01` | 已实现 S0 | P0 | 输入 | 结果 | 异常 |\n")
    assert f"parallel requirement definitions: {name}" in check_requirements(root, ids)


@pytest.mark.parametrize("kind", ["missing_anchor", "unindexed", "undeclared", "missing_state",
                                 "empty_module", "duplicate_anchor", "outside_module", "bad_columns"])
def test_requirement_module_and_state_fail_closed(requirement_repo, kind):
    root, ids = requirement_repo
    path = root / "docs/requirements.md"
    text = path.read_text()
    if kind == "missing_anchor":
        path.write_text(text.replace(f'<a id="req-{MODULES[0]}"></a>', ""))
        expected = "missing requirement module"
    elif kind == "unindexed":
        path.write_text(text.replace(f"[模块](#req-{MODULES[0]})", ""))
        expected = "unindexed requirement module"
    elif kind == "undeclared":
        path.write_text(text.replace("状态依据：", "说明："))  # noqa: RUF001 - Test the exact Chinese labels.
        expected = "missing implementation/acceptance status boundary"
    elif kind == "missing_state":
        path.write_text(text.replace("已实现 S0", "P0"))
        expected = "missing requirement status"
    elif kind == "empty_module":
        path.write_text(text.replace("| `FR-TEST-01` | 已实现 S0 | P0 | 输入 | 结果 | 异常 |\n", ""))
        expected = "no functional requirement definitions"
    elif kind == "duplicate_anchor":
        path.write_text(text + f'\n<a id="req-{MODULES[0]}"></a>\n')
        expected = "duplicate requirement module"
    elif kind == "outside_module":
        row = "| `FR-TEST-01` | 已实现 S0 | P0 | 输入 | 结果 | 异常 |\n"
        path.write_text(row + text.replace(row, ""))
        expected = "functional requirement outside module"
    else:
        path.write_text(text.replace("| P0 | 输入 |", "| P2 | 输入 |"))
        expected = "invalid functional contract columns"
    assert any(expected in error for error in check_requirements(root, ids))
