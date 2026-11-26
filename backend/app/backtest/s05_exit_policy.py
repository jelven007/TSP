"""Freeze the local S05 backtest policy without changing saved strategy rules."""
import os

S05_ID = "custom_mvxxcs05"
CARRY_NEXT_BRANCH = "carry_next_branch_v1"
CARRY_ENTRY_BRANCH = "carry_entry_branch_v2"


def local_policy(strategy_id: str | None) -> str | None:
    if strategy_id == S05_ID and os.environ.get("TSP_LOCAL_S05_EXIT_CARRY") == "1":
        return CARRY_ENTRY_BRANCH
    return None


def freeze_manual_policy(payload: dict, source: str) -> None:
    if payload["kind"] != "backtest" or source not in {"manual", "new_inputs"} or payload.get("expected_runtime"):
        return
    config = payload.get("config")
    if config is None:
        return
    config.pop("s05_exit_policy", None)
    if (policy := local_policy(config.get("strategy_id"))) is not None:
        config["s05_exit_policy"] = policy
