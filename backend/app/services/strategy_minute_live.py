"""Short-lived completed bars, independent of the Parquet partition lock."""
from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from functools import lru_cache
from threading import BoundedSemaphore, Lock

import polars as pl

from app.market_time import as_cn_datetime, cn_now
from app.minute_clock import closed_minute_stamps, normalize_minute_clock
from app.services.minute_write_order import VERSION_COLUMN

MAX_SCOPE_SYMBOLS = 384  # even a full session fits the existing Arrow reader's 100k limit
KEYS = ["symbol", "datetime"]
VALUES = ["open", "high", "low", "close", "volume", "amount"]
SCHEMA = {"symbol": pl.String, "datetime": pl.Datetime("us"),
          **dict.fromkeys(VALUES, pl.Float64), VERSION_COLUMN: pl.UInt64}
logger = logging.getLogger(__name__)


def live_mode():
    return os.environ.get("TSP_STRATEGY_MINUTE_LIVE", "off")


def wall(value):
    return as_cn_datetime(value).replace(tzinfo=None)


def live_candidates(frame, now, minutes=24):
    """Keep the recent session suffix before normalization/IPC."""
    now = wall(now)
    dtype = frame.schema.get("datetime")
    if frame.is_empty() or not isinstance(dtype, pl.Datetime):
        return pl.DataFrame()
    if dtype.time_zone:
        frame = frame.with_columns(pl.col("datetime").dt.convert_time_zone("Asia/Shanghai")
                                   .dt.replace_time_zone(None))
    stamps = [stamp for stamp in closed_minute_stamps(now.date()) if stamp <= now][-minutes:]
    return frame.filter(pl.col("datetime").is_between(stamps[0], stamps[-1])) if stamps else pl.DataFrame()


@lru_cache(maxsize=4)
def _suspension_history(data_dir, fingerprint):
    from app.instrument_suspension import load_suspension_history

    return load_suspension_history(data_dir)


def prepare_live_rows(frame, minute_dir, now):
    """Reuse the archive's dated suspension gate without reading the minute partition."""
    from app.instrument_suspension import RELATIVE_PATH
    from app.services.kline_sync import _prepare_minute_rows

    frame = live_candidates(frame, now)
    if frame.is_empty():
        return frame
    try:
        stat = (minute_dir.parent / RELATIVE_PATH).stat()
        fingerprint = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        fingerprint = None
    history = _suspension_history(minute_dir.parent, fingerprint)
    return _prepare_minute_rows(frame, minute_dir, isolate_suspension_conflicts=True, history=history)


class LiveMinutes:
    """Current-day suffix of verified bars; bucket locks never cover disk or provider IO."""

    def __init__(self, *, max_rows=96_000, max_minutes=24):
        self.max_rows, self.max_minutes = max_rows, max_minutes
        self.lock = Lock()
        self.identity = None
        self.buckets = {}
        self.latest_seq = self.size = 0
        self.capacity_evicted_rows = self.expired_rows = 0

    def offer(self, source, sequence, frame):
        if frame.is_empty():
            return
        if type(sequence) is not int or not 0 < sequence < 2**64:
            raise ValueError("invalid live minute request sequence")
        now = wall(cn_now())
        stamps = [stamp for stamp in closed_minute_stamps(now.date()) if stamp <= now][-self.max_minutes:]
        if not stamps:
            return
        frame = normalize_minute_clock(frame.filter(pl.col("datetime").is_between(stamps[0], stamps[-1])))
        if frame.is_empty():
            return
        if not {"symbol", "datetime", "close"} <= set(frame.columns):
            raise ValueError("live minutes lack symbol/datetime/close")
        # Canonical numeric columns; invalid values remain visible to the input guard.
        rows = frame.select(
            *KEYS, *[pl.col(key).cast(pl.Float64) if key in frame.columns
                      else pl.lit(None, dtype=pl.Float64).alias(key) for key in VALUES],
        ).unique(KEYS, keep="last", maintain_order=True).iter_rows()
        identity = (source, now.date())
        with self.lock:
            if self.identity != identity:
                if (self.identity is not None and self.identity[1] == now.date()
                        and sequence <= self.latest_seq):
                    return
                self.identity = identity
                self.buckets.clear()
                self.latest_seq = self.size = 0
                self.capacity_evicted_rows = self.expired_rows = 0
            self.latest_seq = max(self.latest_seq, sequence)
            for stamp in list(self.buckets):
                if stamp < stamps[0]:
                    expired = len(self.buckets.pop(stamp))
                    self.expired_rows += expired
                    self.size -= expired
            for symbol, stamp, *values in rows:
                bucket = self.buckets.setdefault(stamp, {})
                prior = bucket.get(symbol)
                if prior is None or sequence > prior[-1]:
                    bucket[symbol] = (*values, sequence)
                    self.size += prior is None
            for stamp in sorted(self.buckets):
                if self.size <= self.max_rows:
                    break
                bucket = self.buckets[stamp]
                excess = self.size - self.max_rows
                self.capacity_evicted_rows += min(excess, len(bucket))
                if excess >= len(bucket):
                    self.size -= len(self.buckets.pop(stamp))
                else:
                    for symbol in list(bucket)[:excess]:
                        del bucket[symbol]
                    self.size -= excess

    def status(self):
        with self.lock:
            return {"day": self.identity[1].isoformat() if self.identity else None,
                    "rows": self.size, "max_rows": self.max_rows, "max_minutes": self.max_minutes,
                    "capacity_evicted_rows": self.capacity_evicted_rows, "expired_rows": self.expired_rows}

    def read(self, source, symbols, start, end):
        now = wall(cn_now())
        if start.date() != now.date() or end.date() != now.date() or start > end:
            return pl.DataFrame(schema=SCHEMA)
        valid = [stamp for stamp in closed_minute_stamps(now.date()) if stamp <= now][-self.max_minutes:]
        wanted = set(symbols)
        with self.lock:
            if not valid or self.identity != (source, now.date()):
                return pl.DataFrame(schema=SCHEMA)
            rows = [(symbol, stamp, *bucket[symbol])
                    for stamp, bucket in self.buckets.items() if max(start, valid[0]) <= stamp <= min(end, now)
                    for symbol in wanted.intersection(bucket)]
        return pl.DataFrame(rows, schema=SCHEMA, orient="row")


def merge_minutes(published, live):
    """Published wins ties; otherwise use the same write order as the archive."""
    if live.is_empty():
        return published
    if published.is_empty():
        return live.sort(KEYS)
    if VERSION_COLUMN not in published.columns:
        published = published.with_columns(pl.lit(0, dtype=pl.UInt64).alias(VERSION_COLUMN))
    return pl.concat([live, published], how="diagonal_relaxed").sort(
        *KEYS, VERSION_COLUMN, maintain_order=True,
    ).unique(KEYS, keep="last", maintain_order=True)


def shadow_report(published, live, symbols, required):
    """Compare exact keys, minute values and versions; keep rows out of the receipt."""
    members = sorted(set(symbols))
    stamps = sorted(set(required))

    def canonical(frame):
        if frame.is_empty():
            return pl.DataFrame(schema=SCHEMA)
        return frame.select(
            pl.col("symbol").cast(pl.String), pl.col("datetime").cast(pl.Datetime("us")),
            *[pl.col(name).cast(dtype) if name in frame.columns else
              pl.lit(0 if name == VERSION_COLUMN else None, dtype=dtype).alias(name)
              for name, dtype in SCHEMA.items() if name not in KEYS],
        ).filter(pl.col("symbol").is_in(members)).sort(KEYS).rechunk()

    base, incoming = canonical(published), canonical(live)
    candidate = merge_minutes(base, incoming).rechunk()
    overlap = incoming.join(base, on=KEYS, how="inner", suffix="_published")
    seq, prior = pl.col(VERSION_COLUMN), pl.col(VERSION_COLUMN + "_published")
    aligned = pl.all_horizontal(pl.col(name).eq_missing(pl.col(name + "_published")) for name in VALUES)
    buckets = overlap.select(
        ((seq == prior) & aligned).sum().alias("matching_keys"),
        ((seq == prior) & ~aligned).sum().alias("same_sequence_conflicts"),
        (seq > prior).sum().alias("newer_keys"), (seq < prior).sum().alias("older_keys"),
    ).row(0, named=True)

    def missing(frame):
        available = frame.filter(pl.col("datetime").is_in(stamps)
                                 & pl.col("close").is_finite() & (pl.col("close") > 0))
        return len(members) * len(stamps) - available.unique(KEYS).height

    def digest(frame):
        return hashlib.sha256(frame.write_ipc(None).getvalue()).hexdigest()

    return {**buckets, "scope_sha256": hashlib.sha256(
                ("\n".join(members + [stamp.isoformat() for stamp in stamps])).encode()).hexdigest(),
            "published_sha256": digest(base), "candidate_sha256": digest(candidate),
            "published_rows": base.height, "live_rows": incoming.height,
            "live_only_keys": incoming.height - overlap.height,
            "published_missing_required": missing(base), "candidate_missing_required": missing(candidate)}


class ShadowMinuteProbe:
    """One asynchronous sample in flight per role; never queue behind business work."""

    def __init__(self):
        self.slot = BoundedSemaphore()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="strategy-minute-shadow")

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)

    def submit(self, call, *args):
        if not self.slot.acquire(blocking=False):
            return False
        try:
            future = self.pool.submit(call, *args)
        except RuntimeError:
            self.slot.release()
            return False

        def done(completed):
            self.slot.release()
            if not completed.cancelled() and (error := completed.exception()) is not None:
                logger.warning("strategy minute shadow probe unavailable: %s", type(error).__name__)

        future.add_done_callback(done)
        return True

    def offer(self, payload, market, observed_at):
        read = getattr(market, "strategy_minute", None)
        release = payload.get("public_release_id")
        if live_mode() != "shadow" or not callable(read) or not release:
            return False
        scopes = [(sid, kind, scope)
                  for sid in payload.get("strategy_task_plans", {})
                  for kind, scope in scoped_windows(payload, sid, observed_at).items()][:8]
        return bool(scopes) and self.submit(self._read_scopes, read, release, scopes, observed_at)

    @staticmethod
    def _read_scopes(read, release, scopes, observed_at):
        from app.cell.context import AccessError

        for sid, kind, scope in scopes:
            try:
                packet = read(release, scope)
                logger.info("strategy minute shadow sid=%s phase=%s observed_at=%s release=%s state=%s comparison=%s",
                            sid, kind, observed_at.isoformat(), release, packet.get("state"),
                            packet.get("comparison"))
            except AccessError:
                logger.info("strategy minute shadow sid=%s phase=%s observed_at=%s state=unavailable",
                            sid, kind, observed_at.isoformat())


def scoped_windows(payload, sid, observed_at):
    """Derive transport scope from the frozen action, never from the full universe."""
    from app.services.selection_clock import S03_ID, S05_ID
    from app.services.strategy_input_manifest import S01_ID, S02_ID

    now = wall(observed_at)
    day = now.date()
    prefix = [stamp for stamp in closed_minute_stamps(day) if stamp <= now]
    if not prefix or sid not in (S01_ID, S02_ID, S03_ID, S05_ID):
        return {}
    actions = (payload.get("strategy_task_plans", {}).get(sid) or {}).get("actions", [])
    windows = payload.get("strategy_task_windows", {}).get(sid) or {}
    requests = {}

    def window(symbols, end):
        return {"symbols": sorted(symbols), "start": prefix[0].isoformat(), "end": end.isoformat()}

    if sid in (S03_ID, S05_ID):
        buy = {a["signal_id"] for a in actions if a["kind"] == "buy" and a["phase"] == "stocks"}
        targets = [datetime.combine(day, time.fromisoformat(w["start"])) for w in windows.get("buy", [])
                   if w["signal"]["id"] in buy]
        selection = payload.get("selection_snapshots", {}).get(sid) or {}
        members = {row["symbol"] for row in selection.get("rows", [])}
        if (targets and all(target in prefix for target in targets)
                and 0 < len(members) <= MAX_SCOPE_SYMBOLS):
            requests["buy"] = window(members, max(targets))
    sell = {a["signal_id"] for a in actions if a["kind"] == "sell" and a["phase"] == "observe"}
    roster = {row["symbol"] for row in (payload.get("sell_rosters", {}).get(sid) or {}).get("rows", [])}
    members = set()
    for w in windows.get("sell", []):
        if w["signal"]["id"] in sell:
            members.update(roster.intersection(w.get("symbols", roster)))
    if 0 < len(members) <= MAX_SCOPE_SYMBOLS:
        requests["sell"] = window(members, prefix[-1])
    return requests


def prioritize_collection(plan, payload, observed_at):
    """Bring due members forward while retaining the entire ordinary collection."""
    from app.services.selection_clock import S03_ID, S05_ID
    from app.services.strategy_input_manifest import S01_ID, S02_ID

    now = wall(observed_at)
    selected, lead, breadth = set(plan["selected"]), set(), set()
    day = now.date()

    def at(value):
        return datetime.combine(day, time.fromisoformat(value))

    for sid, windows in payload["strategy_task_windows"].items():
        if sid not in (S01_ID, S02_ID, S03_ID, S05_ID):
            continue
        for window in windows.get("buy", []):
            start = at(window["start"])
            if not start - timedelta(seconds=90) <= now < start + timedelta(seconds=120):
                continue
            selection = payload["selection_snapshots"].get(sid) or {}
            frozen = {row["symbol"] for row in selection.get("rows", []) if selection.get("as_of") == str(day)}
            selected.update(frozen)
            if sid == S02_ID:
                lead.update(frozen)
            if sid == S01_ID and now < start + timedelta(minutes=1):
                breadth.update((payload["daily_pool_snapshots"].get(sid) or {}).get("symbols", []))
        roster = {row["symbol"] for row in (payload["sell_rosters"].get(sid) or {}).get("rows", [])}
        for window in windows.get("sell", []):
            if at(window["start"]) - timedelta(seconds=90) <= now < at(window["end"]):
                selected.update(roster.intersection(window.get("symbols", roster)))
    return {**plan, "symbols": sorted(set(plan["symbols"]) | selected | breadth),
            "selected": sorted(selected), "lead": sorted(lead), "breadth": sorted(breadth)}


def use_scoped_minutes(published, live, scope):
    """Only replace input when the whole due cohort has a valid prefix.

    A partially delivered packet must not create an early terminal zero-buy/sell
    or replace a published-only path that previously had more usable evidence.
    """
    if live.is_empty():
        return None
    if not published.is_empty() and VERSION_COLUMN in published.columns:
        published_keys = published.select(*KEYS, pl.col(VERSION_COLUMN).alias("_published_seq"))
        newer = live.join(published_keys, on=KEYS, how="left").filter(
            pl.col("_published_seq").is_null() | (pl.col(VERSION_COLUMN) > pl.col("_published_seq")))
        if newer.is_empty():
            return None
    start, end = (datetime.fromisoformat(scope[key]) for key in ("start", "end"))
    members = scope["symbols"]
    valid = pl.col("symbol").is_in(members) & pl.col("datetime").is_between(start, end)
    live = live.filter(valid)
    if live.is_empty():
        return None
    merged = merge_minutes(published, live)
    stamps = [stamp for stamp in closed_minute_stamps(start.date()) if start <= stamp <= end]
    usable = merged.filter(valid & pl.col("datetime").is_in(stamps)
                           & pl.col("close").is_finite() & (pl.col("close") > 0))
    if not stamps or usable.height != len(members) * len(stamps):
        return None
    return merged


def fetch_scoped_packets(payload, market, sid, observed_at):
    """The Worker holds no authorization or tenant transaction locks here."""
    from app.cell.context import AccessError

    release = payload.get("public_release_id")
    read = getattr(market, "strategy_minute", None)
    if not release or not callable(read) or live_mode() != "active":
        return {}
    packets = {}
    for kind, scope in scoped_windows(payload, sid, observed_at).items():
        try:
            packet = read(release, scope)
        except AccessError:
            continue
        if (live_mode() == "active" and packet.get("state") == "ready"
                and packet.get("release_id") == release and packet.get("scope") == scope):
            packets[kind] = packet
    return packets


class ScopedMinuteRepository:
    """Compute's strategy-specific adapter. Other reads use the pinned repository."""

    def __init__(self, repo, packets, release, expected):
        self.repo = repo
        self.packets = {kind: packet for kind, packet in packets.items()
                        if packet.get("release_id") == release
                        and packet.get("scope") == expected.get(kind)}

    def __getattr__(self, name):
        return getattr(self.repo, name)

    def _read(self, kind, symbols, day):
        from app.multitenancy.market import decode_frame

        packet = self.packets.get(kind)
        if not packet or day != wall(cn_now()).date():
            return None, None
        scope = packet["scope"]
        members = set(scope["symbols"])
        start, end = (datetime.fromisoformat(scope[key]) for key in ("start", "end"))
        if start.date() != day or not members.issubset(symbols) or (kind == "buy" and members != set(symbols)):
            return None, None
        try:
            frame = use_scoped_minutes(pl.DataFrame(), decode_frame(packet["frame"]), scope)
        except (ValueError, KeyError, OSError, pl.exceptions.PolarsError):
            return None, None
        if frame is None:
            return None, None
        frame = frame.drop(VERSION_COLUMN)
        return self.repo._maybe_adjust_minute(frame, "stock"), (members, end)

    def get_minute_by_dates(self, symbols, dates, asset_type="stock"):
        if asset_type == "stock" and len(dates) == 1:
            frame, _ = self._read("buy", symbols, dates[0])
            if frame is not None:
                return frame
        return self.repo.get_minute_by_dates(symbols, dates, asset_type=asset_type)

    def get_minute_batch(self, symbols, day, asset_type="stock"):
        if asset_type == "stock":
            frame, coverage = self._read("sell", symbols, day)
            if coverage is not None:
                members, end = coverage
                # Other branches' archived bars stay available, with no live input for them.
                published = self.repo.get_minute_batch(symbols, day, asset_type=asset_type)
                if published.is_empty():
                    return frame
                return pl.concat([published.filter(
                    ~pl.col("symbol").is_in(sorted(members)) | (pl.col("datetime") > end)),
                    frame], how="diagonal_relaxed").sort(KEYS)
        return self.repo.get_minute_batch(symbols, day, asset_type=asset_type)
