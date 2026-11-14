"""Preserve stage evidence at the archive boundary, independently of latest metrics."""
from copy import deepcopy


def _actual(step):
    return (step.get("state") in ("success", "rejected")
            and step.get("processed") is not None
            and bool(step.get("started_at")) and bool(step.get("finished_at")))


def preserve_first_receipts(current, previous=None):
    """Mutate only current. Missing legacy first evidence stays explicitly unknown."""
    previous = previous or {}
    for signal, quality in (current.get("entry_data_quality") or {}).items():
        progress = quality.get("progress") or {}
        if (progress.get("schema") != "late_session_progress_v1"
                or progress.get("as_of") != current.get("as_of")):
            continue
        prior = ((previous.get("entry_data_quality") or {}).get(signal) or {}).get("progress") or {}
        same_scope = (previous.get("as_of") == current.get("as_of")
                      and all(prior.get(key) == progress.get(key) for key in
                              ("schema", "as_of", "bar_at", "cohort", "params")))
        for key, step in progress.get("steps", {}).items():
            old = (prior.get("steps") or {}).get(key) or {}
            same_step = same_scope and old.get("scheduled_at") == step.get("scheduled_at")
            step.pop("first_receipt", None)
            if same_step and "first_receipt" in old:
                step["first_receipt"] = deepcopy(old["first_receipt"])
            elif same_step and _actual(old):
                # Legacy archives may already have been overwritten repeatedly.
                step["first_receipt"] = None
            elif _actual(step):
                step["first_receipt"] = deepcopy(step)
