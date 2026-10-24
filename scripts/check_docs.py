"""Check maintained Markdown and requirement contracts using only local files."""

from __future__ import annotations

import argparse
import html
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

MODULES = (
    "01-access-administration", "02-data-management", "03-market-workspace",
    "04-strategy-signals", "05-factors-mining", "06-history-backtesting",
    "07-monitoring-paper", "08-ai-review",
)
BASELINE = "reports/production-system-consolidation-20261023/requirement-ids.json"
LINK = re.compile(r'!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)(?:\s+["\'][^)\n]*["\'])?\s*\)')
HTML_LINK = re.compile(r'<(?:a|img)\b[^>]*\b(?:href|src)=["\']([^"\']+)["\']', re.IGNORECASE)
DEFINITION = re.compile(r'^\s{0,3}\[([^\]]+)\]:\s*(<[^>]+>|\S+)', re.MULTILINE)
REFERENCE = re.compile(r'!?\[([^\]\n]+)\]\[([^\]\n]*)\]')
REQUIREMENT = re.compile(r'^\| `(FR-[A-Z-]+-\d+|NFR-\d+)` \|', re.MULTILINE)


def unfenced(text: str) -> tuple[str, bool]:
    """Preserve line numbers while excluding fenced examples from link checks."""
    output = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            output.append("")
        elif marker:
            fence = marker[1]
            output.append("")
        else:
            output.append(line)
    return "\n".join(output), fence is not None


def anchors(text: str) -> set[str]:
    clean, _ = unfenced(text)
    result = set(re.findall(r'\b(?:id|name)=["\']([^"\']+)["\']', clean))
    counts: Counter[str] = Counter()
    for line in clean.splitlines():
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?$", line)
        if not heading:
            continue
        value = re.sub(r"<[^>]*>", "", html.unescape(heading[1])).lower()
        value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
        value = "".join(c for c in value if unicodedata.category(c)[0] in "LNM" or c in " _-")
        slug = value.replace(" ", "-")
        duplicate = counts[slug]
        counts[slug] += 1
        result.add(slug if not duplicate else f"{slug}-{duplicate}")
    return result


def maintained_paths(root: Path, include_reports: bool = False) -> list[Path]:
    paths = set(root.glob("*.md"))
    folders = ["docs", "deploy", "examples", "scripts", "backend/scripts", "tests", "mcp-server"]
    if include_reports:
        folders.append("reports")
    for name in folders:
        paths.update(
            path for path in (root / name).rglob("*.md")
            if not any(part in {".venv", ".git", "node_modules", "__pycache__"}
                       for part in path.relative_to(root).parts)
        )
    return sorted(paths)


def check_links(root: Path, paths: list[Path]) -> list[str]:
    errors = []
    cached: dict[Path, set[str]] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        clean, incomplete = unfenced(text)
        name = path.relative_to(root)
        if incomplete:
            errors.append(f"{name}: unclosed code fence")
        # Inline code can contain sample Markdown, not navigable links.
        clean = re.sub(r"(`+)(.*?)\1", "", clean)
        definitions = {key.lower(): value for key, value in DEFINITION.findall(clean)}
        urls = [match[1] for match in LINK.finditer(clean)] + HTML_LINK.findall(clean)
        for label, reference in REFERENCE.findall(clean):
            key = (reference or label).lower()
            if label.isdigit() and key.isdigit() and key not in definitions:
                continue  # Adjacent numeric citations can be ordinary text.
            if key not in definitions:
                errors.append(f"{name}: missing reference definition: {key}")
        urls.extend(definitions.values())
        for raw in urls:
            raw = raw.strip("<>")
            parsed = urlsplit(raw)
            if parsed.scheme or parsed.netloc:
                continue  # No network requests or host-specific file URI checks.
            if parsed.path.startswith("/"):
                continue  # Site routes are not repository-relative file links.
            target = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path.resolve()
            if not target.exists():
                errors.append(f"{name}: missing link target: {raw}")
                continue
            if parsed.fragment and target.suffix == ".md":
                fragment = unquote(parsed.fragment)
                if re.fullmatch(r"L\d+(?:-L?\d+)?", fragment):
                    continue  # GitHub line links, distinct from heading anchors.
                if target not in cached:
                    cached[target] = anchors(target.read_text(encoding="utf-8"))
                if fragment not in cached[target]:
                    errors.append(f"{name}: missing anchor: {raw}")
    return errors


def check_requirements(root: Path, expected_ids: set[str] | None = None) -> list[str]:
    errors = []
    entry = root / "docs/requirements.md"
    if not entry.exists():
        return ["missing requirement entry: docs/requirements.md"]
    index, _ = unfenced(entry.read_text(encoding="utf-8"))
    if "状态依据：" not in index or "不等于" not in index or "签收" not in index:  # noqa: RUF001 - Exact Chinese document label.
        errors.append("docs/requirements.md: missing implementation/acceptance status boundary")
    # Functional definitions live in one handbook; other maintained documents
    # link to them rather than carrying a second, independently editable copy.
    for path in sorted((root / "docs").rglob("*.md")):
        if path != entry and any(i.startswith("FR-") for i in REQUIREMENT.findall(
                unfenced(path.read_text(encoding="utf-8"))[0])):
            errors.append(f"parallel requirement definitions: {path.relative_to(root)}")
    urls = [m[1] for m in LINK.finditer(index)]
    ranges = []
    for slug in MODULES:
        anchor = f"req-{slug}"
        markers = list(re.finditer(r'<a\s+id=["\']' + re.escape(anchor) + r'["\']\s*></a>', index))
        if not markers:
            errors.append(f"missing requirement module: #{anchor}")
            continue
        if len(markers) != 1:
            errors.append(f"duplicate requirement module: #{anchor}")
        if f"#{anchor}" not in urls:
            errors.append(f"unindexed requirement module: #{anchor}")
        # Each module owns the following H3 section. H4 groups stay inside it;
        # a later H1/H2/H3, including NFR chapters, closes its scope.
        start = markers[0].end()
        heading = re.match(r"\s*###\s+[^\n]+\n", index[start:])
        if not heading:
            errors.append(f"#{anchor}: missing module heading")
            continue
        start += heading.end()
        end_heading = re.search(r"^#{1,3}\s+", index[start:], re.MULTILINE)
        end = start + end_heading.start() if end_heading else len(index)
        ranges.append((start, end))
        if not any(i.startswith("FR-") for i in REQUIREMENT.findall(index[start:end])):
            errors.append(f"#{anchor}: no functional requirement definitions")
    ids = REQUIREMENT.findall(index)
    for match in REQUIREMENT.finditer(index):
        if match[1].startswith("FR-") and not any(a <= match.start() < b for a, b in ranges):
            errors.append(f"functional requirement outside module: {match[1]}")
    for line in index.splitlines():
        if not REQUIREMENT.match(line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2 or not re.match(r"^(已实现|部分实现|目标|条件扩展)", cells[1]):
            errors.append(f"docs/requirements.md: missing requirement status: {cells[0]}")
        if cells[0].startswith("`FR-") and (len(cells) != 6 or cells[2] not in {"P0", "P1"}):
            errors.append(f"docs/requirements.md: invalid functional contract columns: {cells[0]}")
    for ident, count in Counter(ids).items():
        if count != 1:
            errors.append(f"duplicate requirement: {ident} ({count})")
    if expected_ids is None:
        baseline = root / BASELINE
        if not baseline.exists():
            errors.append(f"missing preserved requirement baseline: {BASELINE}")
            expected_ids = set()
        else:
            expected_ids = set(json.loads(baseline.read_text(encoding="utf-8")))
    for missing in sorted(expected_ids - set(ids)):
        errors.append(f"missing preserved requirement: {missing}")
    return errors


def check(root: Path, include_reports: bool = False) -> list[str]:
    return check_links(root, maintained_paths(root, include_reports)) + check_requirements(root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--include-reports", action="store_true", help="Also audit historical report links.")
    args = parser.parse_args()
    root = args.root.resolve()
    errors = check(root, args.include_reports)
    if errors:
        parser.exit(1, "\n".join(errors) + "\n")
    print(f"Documentation passed: {len(maintained_paths(root, args.include_reports))} Markdown files; "
          f"{len(MODULES)} in-document requirement modules; preserved baseline checked.")


if __name__ == "__main__":
    main()
