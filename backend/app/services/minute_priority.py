"""Build private scheduling hints; they never determine signal membership."""
import time

from app.services.daily_selections import DailySelections
from app.strategy.daily_pool import build_pool


def build_plan(engine, service, storage, ids, day, *, overrides, current):
    symbols, selected = set(), set()
    context = service.build_daily_pool_context(day, current=current) if ids else None
    for sid in ids:
        pool = build_pool(engine, sid, context, overrides=overrides.get(sid))
        symbols.update(pool["symbols"])
        frozen = DailySelections(storage).read(sid, day)
        if frozen:
            selected.update(row["symbol"] for row in frozen["rows"])
    return {"day": str(day), "sequence": time.time_ns(),
            "symbols": sorted(symbols), "selected": sorted(selected & symbols)}
