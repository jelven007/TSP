"""TDX financial archives downloaded by rustdx and parsed without Python TDX clients."""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path

from app.market_time import cn_now

_ARCHIVE_LOCK = threading.Lock()
_REPORT_FILE = re.compile(r"gpcw(\d{8})\.zip")
_HEADER = struct.Struct("<hIH3L")
_SECURITY = struct.Struct("<6scL")
_MAX_ARCHIVE_BYTES = 128_000_000
_MAX_DATA_BYTES = 256_000_000


def _verified(path: Path, size: int, checksum: str) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    with path.open("rb") as source:
        return hashlib.file_digest(source, "md5").hexdigest() == checksum


def _retain_revision(path: Path, cache_dir: Path, report_date: str, observed_at: str) -> Path:
    """Keep immutable observed bytes without assigning a historical publication date."""
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    revision_dir = cache_dir / "revisions" / report_date
    revision_dir.mkdir(parents=True, exist_ok=True)
    revision = revision_dir / f"{digest}.zip"
    if not revision.exists():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=revision_dir, delete=False) as out:
                temporary = Path(out.name)
                with path.open("rb") as source:
                    while block := source.read(64_000):
                        out.write(block)
            os.replace(temporary, revision)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    metadata = revision.with_suffix(".json")
    if not metadata.exists():
        payload = {
            "report_date": report_date,
            "sha256": digest,
            "first_observed_at": observed_at,
            "publication_time_verified": False,
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=revision_dir, delete=False) as out:
                temporary = Path(out.name)
                json.dump(payload, out, sort_keys=True)
            os.replace(temporary, metadata)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return revision


def parse_archive(path: Path, codes: set[str], columns: set[str], report_date: str) -> list[dict]:
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if (
            len(members) != 1
            or not members[0].filename.endswith(".dat")
            or members[0].file_size > _MAX_DATA_BYTES
        ):
            raise ValueError("财务压缩包结构异常")
        # Spool bounds RAM; no archive paths are extracted to the filesystem.
        with tempfile.SpooledTemporaryFile(max_size=8_000_000) as data:
            with archive.open(members[0]) as source:
                while block := source.read(64_000):
                    data.write(block)
                    if data.tell() > _MAX_DATA_BYTES:
                        raise ValueError("财务数据文件过大")
            length = data.tell()
            data.seek(0)
            header = data.read(_HEADER.size)
            if len(header) != _HEADER.size:
                raise ValueError("财务数据头不完整")
            _, wire_date, count, _, row_size, _ = _HEADER.unpack(header)
            if str(wire_date) != report_date or not row_size or row_size % 4:
                raise ValueError("财务报告日期或字段长度异常")
            index_end = _HEADER.size + count * _SECURITY.size
            if index_end > length:
                raise ValueError("财务证券目录不完整")
            selected_columns = sorted(
                (int(column[3:]) - 1, column)
                for column in columns
                if re.fullmatch(r"col[1-9]\d*", column) and int(column[3:]) * 4 <= row_size
            )
            records = []
            for index in range(count):
                data.seek(_HEADER.size + index * _SECURITY.size)
                code_bytes, _, offset = _SECURITY.unpack(data.read(_SECURITY.size))
                code = code_bytes.decode("ascii")
                if offset < index_end or offset + row_size > length:
                    raise ValueError("财务字段偏移越界")
                if code not in codes:
                    continue
                data.seek(offset)
                raw = data.read(row_size)
                record = {"code": code, "report_date": report_date}
                record.update(
                    (column, struct.unpack_from("<f", raw, position * 4)[0])
                    for position, column in selected_columns
                )
                records.append(record)
            return records


def financial_history(native, symbols, *, periods, columns, cache_dir: Path) -> list[dict]:
    if not symbols or periods <= 0:
        return []
    listing = bytes(native.report_file("tdxfin/gpcw.txt", 512_000)).decode("utf-8")
    today = cn_now().date()
    selected = {}
    for line in listing.splitlines():
        parts = line.strip().split(",")
        if len(parts) != 3:
            continue
        filename, checksum, size_text = parts
        match = _REPORT_FILE.fullmatch(filename)
        if not match or not re.fullmatch("[0-9a-f]{32}", checksum):
            continue
        try:
            day = datetime.strptime(match[1], "%Y%m%d").date()
            size = int(size_text)
        except ValueError:
            continue
        if day <= today and 200 < size <= _MAX_ARCHIVE_BYTES:
            selected[match[1]] = (filename, checksum, size)
    if not selected:
        raise ValueError("未获取到有效历史财务目录")
    cache_dir.mkdir(parents=True, exist_ok=True)
    codes = {symbol.split(".", 1)[0] for symbol in symbols}
    records = []
    for report_date in sorted(selected, reverse=True)[:periods]:
        filename, checksum, size = selected[report_date]
        path = cache_dir / filename
        with _ARCHIVE_LOCK:
            if not _verified(path, size, checksum):
                temporary = None
                try:
                    payload = bytes(native.report_file(f"tdxfin/{filename}", size))
                    with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False) as out:
                        temporary = Path(out.name)
                        out.write(payload)
                    if not _verified(temporary, size, checksum):
                        raise ValueError(f"财务文件校验失败: {filename}")
                    # Validate before advancing the mutable current cache.
                    parse_archive(temporary, codes, set(columns), report_date)
                    if path.is_file():
                        try:
                            parse_archive(path, codes, set(columns), report_date)
                        except (ValueError, OSError, zipfile.BadZipFile):
                            pass
                        else:
                            _retain_revision(path, cache_dir, report_date, cn_now().isoformat())
                    _retain_revision(temporary, cache_dir, report_date, cn_now().isoformat())
                    os.replace(temporary, path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            _retain_revision(path, cache_dir, report_date, cn_now().isoformat())
            records.extend(parse_archive(path, codes, set(columns), report_date))
    return records
