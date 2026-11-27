"""为API和计算容器发布不可变公共行情, 不混入租户定义、预选或业务凭据。"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from datetime import time as clock_time
from pathlib import Path

from app.services.observability import measure_runtime_lock

logger = logging.getLogger(__name__)
PUBLIC_DIRS = frozenset({
    "kline_daily", "kline_daily_enriched", "kline_index_daily", "kline_index_enriched",
    "kline_index_weekly", "kline_index_monthly",
    "kline_minute",
    "kline_5minute", "kline_10minute", "kline_15minute", "kline_weekly",
    "adj_factor", "financials", "instruments", "instruments_index",
    "instruments_ext", "kline_ext", "depth5", "auction_snapshot",
    "quote_second", "regime_history", "mainline_history", "ext_data", "analysis_menus",
    "instrument_status", "data_releases",
    "market_snapshot", "corporate_actions",
})
PUBLIC_FILES = frozenset({
    ".matrix_generation_stock.json", ".matrix_generation_index.json",
    "settlement_ready.json", "capabilities.json",
})
DAILY_COVERAGE_DIRS = (
    ("stock", "kline_daily", "kline_daily_enriched"),
    ("index", "kline_index_daily", "kline_index_enriched"),
)
MINUTE_FAST_WINDOWS = (
    (clock_time(10), clock_time(10, 30)),  # S03 fixed minute and repair window.
    (clock_time(14, 25), clock_time(14, 55)),
)


def _minute_fast_window(wall: datetime) -> bool:
    clock = wall.time()
    return wall.weekday() < 5 and any(start <= clock <= end for start, end in MINUTE_FAST_WINDOWS)


def _strip_volatile(config: dict) -> None:
    """Drop ext config fields the scheduler rewrites on every pull tick."""
    config.pop("pull", None)
    config.pop("updated_at", None)


def release_path(root: Path, release_id: str | None = None) -> Path:
    """解析受控发布目录, 指定身份不可回退current; 不验证正文, 校验由租约负责。"""
    root = root.resolve()
    if release_id is not None:
        if len(release_id) != 32 or any(char not in "0123456789abcdef" for char in release_id):
            raise ValueError("invalid public release")
        target = root / "releases" / release_id
    else:
        target = root / "current" if (root / "current").exists() else root
    target = target.resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_dir():
        raise ValueError("public release is unavailable")
    return target


class PublicPublisher:
    def __init__(self, source, destination, write_lock=None):
        self.source, self.root = Path(source).resolve(), Path(destination).resolve()
        if self.source == self.root or self.root.is_relative_to(self.source):
            raise ValueError("public releases must be outside the source data directory")
        self.stopped = threading.Event()
        self.changed = threading.Event()
        self.minute_changed = threading.Event()
        self.metadata_lock = threading.Lock()
        self.minute_completed_at = None
        self.minute_priority_completed_at = None
        self.thread = threading.Thread(target=self._loop, daemon=True, name="public-file-publisher")
        self.minute_thread = threading.Thread(
            target=self._minute_loop, daemon=True, name="public-minute-publisher")
        self.error = None
        self.write_lock = write_lock or threading.Lock()
        self.minute_lock = threading.Lock()
        self.commit_lock = threading.Lock()

    def request_minute_publish(self, completed_at):
        """记录普通分钟采集完成时钟, 关键窗口唤醒独立发布线程。"""
        from app.market_time import CN_TZ

        wall = (completed_at.astimezone(CN_TZ).replace(tzinfo=None)
                if completed_at.tzinfo else completed_at)
        with self.metadata_lock:
            self.minute_completed_at = wall.replace(tzinfo=CN_TZ).timestamp()
        if _minute_fast_window(wall):
            self.minute_changed.set()

    def publish_priority(self, completed_at):
        """记录预选采集完成时钟, 在关键窗口同步发布已提交分钟。

        返回实际发布manifest, 窗口外返回None; 不将预选采集时钟冒充全市场
        完成。异常仍唤醒分钟发布线程, 调用方保留失败回执继续重试。
        """
        from app.market_time import CN_TZ

        wall = (completed_at.astimezone(CN_TZ).replace(tzinfo=None)
                if completed_at.tzinfo else completed_at)
        with self.metadata_lock:
            self.minute_priority_completed_at = wall.replace(tzinfo=CN_TZ).timestamp()
        if _minute_fast_window(wall):
            try:
                return self.publish_minute(wall.date())
            except Exception:
                self.minute_changed.set()
                raise
        return None

    def publish_minute(self, day):
        """冻结目标日已原子提交分钟, 不等待全量历史复制或无关长写锁。

        先取得已验证发布租约, 复制变化分钟、硬链未变分钟并引用固定历史基线;
        复制前后重查源签名和清单, 最后经指针世代守卫原子切换。失败清理暂存,
        保持旧 current; 分钟完成时钟不能冒充日线或财务完成时钟。
        minute_completed_at 与 minute_priority_completed_at 分别记录普通/优先
        采集时钟, 随本份清单固定; 下游只使用其绑定清单, 不借读之后的可变元数据。
        """
        from app.multitenancy.public_reader import PublicReleaseLease
        from app.services.parquet_profile import PROFILE, row_symbol_stats

        if not (self.root / "current").exists():
            return self.publish()
        with measure_runtime_lock(self.minute_lock, "public.minute"):
            lease = PublicReleaseLease.acquire(release_path(self.root))
            try:
                previous, prior = lease.release_dir, lease.validate()
                if not prior.get("files") or not prior.get("checksums"):
                    raise ValueError("public minute baseline is not verified")
                base = lease.dependencies[0] if lease.dependencies else lease
                base_manifest = base.validate()
                prefix = f"kline_minute/date={day.isoformat()}/"
                partition = self.source / prefix

                def inventory():
                    return {
                        path.relative_to(self.source).as_posix(): self._signature(path)
                        for path in partition.rglob("*")
                        if not path.is_symlink() and path.is_file()
                        and path.suffix in {".parquet", ".json"}
                    }

                captured = inventory()
                if not any(name.endswith(".parquet") for name in captured):
                    raise ValueError("public minute partition is unavailable")
                old = {name: signature for name, signature in prior["files"].items()
                       if name.startswith(prefix)}
                if old == captured:
                    return prior
                files, checksums = dict(prior["files"]), dict(prior["checksums"])
                for name in old:
                    files.pop(name)
                    checksums.pop(name, None)
                files.update(captured)
                self._check_capacity(captured, extra_inodes=sum(
                    name.startswith("kline_minute/") for name in files))
                release_id = uuid.uuid4().hex
                releases = self.root / "releases"
                stage = releases / ("." + release_id)
                stage.mkdir()
                try:
                    inherited = []
                    for directory in sorted(PUBLIC_DIRS - {"kline_minute"}):
                        if (base.release_dir / directory).is_dir():
                            (stage / directory).symlink_to(
                                Path("..") / base.release_dir.name / directory,
                                target_is_directory=True,
                            )
                            inherited.append(directory)
                    for name in PUBLIC_FILES:
                        if (base.release_dir / name).is_file():
                            os.link(base.release_dir / name, stage / name)
                    for name, signature in files.items():
                        if not name.startswith("kline_minute/") or name.endswith("/" + PROFILE):
                            continue
                        target = stage / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if name in captured and old.get(name) != signature:
                            source = self.source / name
                            if self._signature(source) != signature:
                                raise ValueError("public minute changed before capture; retry")
                            shutil.copy2(source, target)
                            if self._signature(source) != signature:
                                raise ValueError("public minute changed during capture; retry")
                            with target.open("rb") as stream:
                                checksums[name] = hashlib.file_digest(stream, "sha256").hexdigest()
                        else:
                            os.link(previous / name, target)
                            checksums[name] = prior["checksums"][name]
                    profile = "kline_minute/" + PROFILE
                    if (previous / profile).is_file():
                        shutil.copy2(previous / profile, stage / profile)
                    row_symbol_stats(stage / "kline_minute")
                    if (stage / profile).is_file():
                        files[profile] = self._signature(stage / profile)
                        with (stage / profile).open("rb") as stream:
                            checksums[profile] = hashlib.file_digest(stream, "sha256").hexdigest()
                    if inventory() != captured:
                        raise ValueError("public minute changed during publication; retry")
                    lease.release_id()
                    with self.metadata_lock:
                        metadata = {
                            "minute_completed_at": self.minute_completed_at,
                            "minute_priority_completed_at": self.minute_priority_completed_at,
                        }
                    manifest = {
                        "id": release_id, "base_release_id": base.release_dir.name,
                        "base_release_digest": base_manifest["digest"],
                        "inherited_dirs": inherited, "minute_day": day.isoformat(),
                        "digest": hashlib.sha256(json.dumps(
                            [base_manifest["digest"], files], sort_keys=True,
                        ).encode()).hexdigest(),
                        "generations": base_manifest["generations"],
                        "files": files, "checksums": checksums, "published_at": time.time(),
                        **{key: value for key, value in metadata.items() if value is not None},
                    }
                    self._commit(stage, manifest, previous.name)
                    self.error = None
                    return manifest
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
            finally:
                lease.close()

    def _check_capacity(self, files, *, extra_inodes=0):
        if not hasattr(os, "statvfs"):
            return
        capacity = os.statvfs(self.root)
        directories = {parent for name in files for parent in Path(name).parents
                       if parent != Path(".")}
        needed = len(files) + len(directories) + extra_inodes + len(PUBLIC_DIRS) + 3
        reserve = max(100_000, capacity.f_files // 10)
        if (
            (capacity.f_files and capacity.f_ffree - needed < reserve)
            or capacity.f_bavail * capacity.f_frsize
            - sum(signature[0] for signature in files.values()) < 1024**3
        ):
            raise OSError(errno.ENOSPC, "public release capacity reserve")

    def _commit(self, stage, manifest, expected_id):
        """仅捕获时的 current 身份仍有效才提交; 慢复制不能回滚较新分钟发布。"""
        with self.commit_lock:
            current_id = release_path(self.root).name if (self.root / "current").exists() else None
            if current_id != expected_id:
                raise ValueError("public release superseded during publication; retry")
            release_id = manifest["id"]
            (stage / ".public_release.json").write_text(json.dumps(manifest, sort_keys=True))
            os.rename(stage, stage.parent / release_id)
            link = self.root / ("." + release_id)
            link.symlink_to(Path("releases") / release_id, target_is_directory=True)
            self._swap_current(link)
            logger.info("public release available release=%s minute_completed_at=%s available_at=%.6f",
                        release_id, manifest.get("minute_completed_at"), time.time())

    def generations(self):
        from app.enriched_generation import get_enriched_generation

        return {asset: get_enriched_generation(self.source, asset, initialize=False)
                for asset in ("stock", "index")}

    def inventory(self):
        files = {}
        for directory in PUBLIC_DIRS:
            for path in (self.source / directory).rglob("*"):
                if path.is_symlink() or not path.is_file() or path.suffix not in {".parquet", ".json"}:
                    continue
                # Session JSON is mutable writer diagnostics, not replay evidence.
                if directory == "quote_second" and path.suffix == ".json":
                    continue
                files[path.relative_to(self.source).as_posix()] = self._signature(path)
        for name in PUBLIC_FILES:
            path = self.source / name
            if path.is_file() and not path.is_symlink():
                stat = path.stat()
                files[name] = [stat.st_size, stat.st_mtime_ns]
        return files

    @staticmethod
    def _signature(path: Path):
        if path.parent.parent.name == "ext_data" and path.name == "config.json":
            # The ext scheduler rewrites pull progress and updated_at into
            # config.json every minute. publish() strips those blocks, so the
            # volatile mtime must not invalidate a release still copying files.
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            else:
                _strip_volatile(config)
                digest = hashlib.sha256(
                    json.dumps(config, sort_keys=True, ensure_ascii=False).encode(),
                ).hexdigest()
                return [0, int(digest[:15], 16)]
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns]

    def publish(self):
        # Serialize the short copy boundary with the platform repository's
        # writers. Otherwise a one-second quote cycle could starve publication.
        with measure_runtime_lock(self.write_lock, "public.publish"):
            return self._publish()

    def validate_daily_coverage(self):
        import polars as pl

        for asset, raw_name, enriched_name in DAILY_COVERAGE_DIRS:
            raw_root = self.source / raw_name
            raw_partition = max(
                (path for path in raw_root.glob("date=????-??-??") if path.is_dir()),
                default=None,
            )
            if raw_partition is None:
                continue
            enriched_partition = self.source / enriched_name / raw_partition.name
            raw_files = sorted(raw_partition.rglob("*.parquet"))
            enriched_files = (
                sorted(enriched_partition.rglob("*.parquet"))
                if enriched_partition.is_dir() else []
            )
            if not raw_files or not enriched_files:
                raise ValueError(
                    f"public enriched coverage missing for {asset} {raw_partition.name}",
                )
            try:
                raw_symbols = set(
                    pl.read_parquet(raw_files, columns=["symbol"])["symbol"].drop_nulls().to_list(),
                )
                enriched_symbols = set(
                    pl.read_parquet(enriched_files, columns=["symbol"])["symbol"].drop_nulls().to_list(),
                )
            except Exception as exc:
                raise ValueError(
                    f"public enriched coverage unreadable for {asset} {raw_partition.name}",
                ) from exc
            missing = raw_symbols - enriched_symbols
            if missing:
                sample = ",".join(sorted(missing)[:5])
                raise ValueError(
                    f"public enriched coverage incomplete for {asset} "
                    f"{raw_partition.name}: {len(missing)} missing ({sample})",
                )

    def _publish(self):
        from app.services.parquet_profile import prepare_profiles, rebind_profiles

        with self.metadata_lock:
            minute_completed_at = self.minute_completed_at
            minute_priority_completed_at = self.minute_priority_completed_at
        self.validate_daily_coverage()
        prepare_profiles(self.source)
        generations, files = self.generations(), self.inventory()
        digest = hashlib.sha256(json.dumps([generations, files], sort_keys=True).encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        previous = None
        try:
            previous = release_path(self.root)
            previous_manifest = json.loads((previous / ".public_release.json").read_text())
        except (OSError, ValueError):
            previous_manifest = {}
        if previous_manifest.get("digest") == digest and "checksums" in previous_manifest:
            return previous_manifest
        if hasattr(os, "statvfs"):
            capacity = os.statvfs(self.root)
            # A release needs fresh directory inodes even for hard-linked files.
            # Budget every file as a copy, retaining 10%/100k inodes and 1 GiB
            # for the source writer and the rest of the host.
            directories = {
                parent.as_posix()
                for name in files
                for parent in Path(name).parents
                if parent != Path(".")
            }
            needed = len(files) + len(directories) + 3
            reserve = max(100_000, capacity.f_files // 10)
            bytes_needed = sum(
                signature[0] for name, signature in files.items()
                if previous_manifest.get("files", {}).get(name) != signature
            )
            if (
                (capacity.f_files and capacity.f_ffree - needed < reserve)
                or capacity.f_bavail * capacity.f_frsize - bytes_needed < 1024**3
            ):
                raise OSError(errno.ENOSPC, "public release capacity reserve")
        release_id = uuid.uuid4().hex
        releases = self.root / "releases"
        releases.mkdir(exist_ok=True)
        stage = releases / ("." + release_id)
        stage.mkdir()
        try:
            checksums = {}
            # Minute writers use their own partition locks, not write_lock.
            # Freeze this small input first; later atomic refreshes belong to
            # the next release rather than invalidating a long historical copy.
            for name in sorted(files, key=lambda name: (not name.startswith("kline_minute/"), name)):
                signature = files[name]
                source, target = self.source / name, stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                minute_input = name.startswith("kline_minute/")
                if minute_input and self._signature(source) != signature:
                    raise ValueError("public minute changed before capture; retry")
                if previous and previous_manifest.get("files", {}).get(name) == signature:
                    # Only link a previous immutable COPY, never mutable source data.
                    os.link(previous / name, target)
                elif name.startswith("ext_data/") and name.endswith("/config.json"):
                    config = json.loads(source.read_text(encoding="utf-8"))
                    _strip_volatile(config)
                    target.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
                    os.utime(target, ns=(signature[1], signature[1]))
                else:
                    shutil.copy2(source, target)
                if minute_input and self._signature(source) != signature:
                    raise ValueError("public minute changed during capture; retry")
                retained_hash = previous_manifest.get("checksums", {}).get(name)
                if retained_hash and previous_manifest.get("files", {}).get(name) == signature:
                    checksums[name] = retained_hash
                else:
                    with target.open("rb") as stream:
                        checksums[name] = hashlib.file_digest(stream, "sha256").hexdigest()
            current_files = {
                name: signature for name, signature in self.inventory().items()
                if name in files or not (
                    name.startswith("quote_second/") and name.endswith(".parquet")
                )
            }
            # Captured minutes have already passed their copy boundary checks.
            # Require their continued existence, and reject newly added minute
            # files. Daily inputs and existing second shards stay fully strict.
            comparable_files = {
                name: files[name] if name in files and name.startswith("kline_minute/") else signature
                for name, signature in current_files.items()
            }
            if self.generations() != generations or comparable_files != files:
                raise ValueError("public files changed during publication; retry")
            rebind_profiles(stage)
            # Profiles describe copied inode identities, so their checksums differ
            # from mutable source profiles. Keep release checksums exact.
            for name in checksums:
                if name.endswith("/.query_profile.json"):
                    with (stage / name).open("rb") as stream:
                        checksums[name] = hashlib.file_digest(stream, "sha256").hexdigest()
            manifest = {"id": release_id, "digest": digest, "generations": generations,
                        "files": files, "checksums": checksums, "published_at": time.time()}
            if minute_completed_at is not None:
                manifest["minute_completed_at"] = minute_completed_at
            if minute_priority_completed_at is not None:
                manifest["minute_priority_completed_at"] = minute_priority_completed_at
            self._commit(stage, manifest, previous.name if previous_manifest else None)
            self.error = None
            return manifest
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    @staticmethod
    def _swap_current(link):
        current = link.parent / "current"
        try:
            os.replace(link, current)
        except OSError:
            # Windows MoveFileEx cannot swap a directory entry for a link, so the
            # stale link is dropped first. POSIX keeps the atomic rename; only the
            # publisher's own link is removed, never a real directory holding data.
            if os.name != "nt" or not current.is_symlink():
                raise
            current.unlink()
            os.replace(link, current)

    def start(self):
        # The first complete public release is a deployment readiness gate.
        try:
            self.publish()
        except OSError as exc:
            if exc.errno != errno.ENOSPC:
                raise
            previous = release_path(self.root)
            manifest = json.loads((previous / ".public_release.json").read_text())
            if manifest.get("id") != previous.name or not manifest.get("checksums"):
                raise
            self.error = type(exc).__name__
            logger.warning("public file publication deferred: %s", self.error)
        self.thread.start()
        self.minute_thread.start()

    def close(self):
        self.stopped.set()
        self.changed.set()
        self.minute_changed.set()
        self.thread.join(timeout=20)
        self.minute_thread.join(timeout=20)

    def _minute_loop(self):
        """独立线程发布同日完成分钟; 失败在窗口内重试, 不排在全量复制之后。"""
        from app.market_time import CN_TZ, cn_now

        while not self.stopped.is_set():
            self.minute_changed.wait()
            if self.stopped.is_set():
                break
            self.minute_changed.clear()
            with self.metadata_lock:
                minute_at = max(self.minute_completed_at or 0, self.minute_priority_completed_at or 0)
            wall = datetime.fromtimestamp(minute_at, CN_TZ) if minute_at else None
            now = cn_now()
            if not wall or wall.date() != now.date() or not _minute_fast_window(now):
                continue
            try:
                self.publish_minute(wall.date())
            except Exception as exc:
                self.error = type(exc).__name__
                logger.warning("public minute publication deferred: %s", self.error)
                if not self.stopped.wait(1):
                    self.minute_changed.set()

    def _loop(self):
        # File releases feed immutable research inputs; live quotes/depth use the
        # independent market bridge. Frequent copies duplicate every directory
        # inode even when unchanged files are hard-linked to the prior release.
        delay = 300
        last_attempt = 0.0
        capacity_deferred = False
        while not self.stopped.is_set():
            if capacity_deferred:
                if self.stopped.wait(delay):
                    break
            else:
                self.changed.wait(delay)
            if self.stopped.is_set():
                break
            # Merge bursts; a notification during publication remains pending.
            remaining = max(0.0, 5 - (time.monotonic() - last_attempt))
            if self.stopped.wait(remaining):
                break
            self.changed.clear()
            last_attempt = time.monotonic()
            try:
                self.publish()
                delay = 300
                capacity_deferred = False
            except Exception as exc:
                self.error = type(exc).__name__
                logger.warning("public file publication deferred: %s", self.error)
                capacity_deferred = isinstance(exc, OSError) and exc.errno == errno.ENOSPC
                delay = 300 if capacity_deferred else 30
