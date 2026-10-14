"""Atomic SQL candidates using the original validation and publication rules."""
from __future__ import annotations

import copy

import polars as pl

from app.backtest.candidates import CandidateStore
from app.backtest.mining import evaluate_candidate_gate
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.research import read_frame
from app.services.mining_candidates import (
    _ARTIFACT_SCHEMA,
    _MAX_ARTIFACT_BYTES,
    _MAX_ARTIFACT_ROWS,
    MiningCandidateService,
    _published_strategy_id,
)
from app.services.mining_jobs import SUCCESS_RUN_STATUSES


class _MemoryCandidates(CandidateStore):
    def __init__(self, values):
        self.items = copy.deepcopy(values)

    def _load(self):
        return self.items

    def _write(self, items):
        self.items = items


class SQLCandidateStore:
    def __init__(self, documents: TenantDocuments):
        self.documents = documents

    def list(self):
        return self.documents.get("research_candidates", "default", [])

    def _mutate(self, method, *args, **kwargs):
        result = None

        def change(values):
            nonlocal result
            memory = _MemoryCandidates(values or [])
            result = getattr(memory, method)(*args, **kwargs)
            return memory.items

        self.documents.mutate("research_candidates", "default", change)
        return copy.deepcopy(result)

    def create(self, **values):
        return self._mutate("create", **values)

    def create_or_get_by_provenance(self, **values):
        return self._mutate("create_or_get_by_provenance", **values)

    def update(self, candidate_id, **values):
        return self._mutate("update", candidate_id, **values)

    def delete(self, candidate_id):
        return self._mutate("delete", candidate_id)


class SQLMiningCandidates(MiningCandidateService):
    def __init__(self, documents, store, engine):
        self.documents = documents
        self.run_store = store
        self.candidate_store = SQLCandidateStore(documents)
        self.strategy_engine = engine

    def _load_candidate(self, run_id, signature):
        manifest = self.run_store.get(run_id)
        if not manifest:
            raise KeyError(run_id)
        if manifest["status"] not in SUCCESS_RUN_STATUSES:
            raise ValueError("mining candidates require a successful run")
        data = self.run_store.jobs.artifact(run_id, "candidates")
        if len(data) > _MAX_ARTIFACT_BYTES:
            raise ValueError("mining candidates artifact exceeds its size limit")
        frame = read_frame(data, max_rows=_MAX_ARTIFACT_ROWS, max_uncompressed=32 * 1024 * 1024)
        if frame.height > _MAX_ARTIFACT_ROWS or any(frame.schema.get(key) != value
                                                  for key, value in _ARTIFACT_SCHEMA.items()):
            raise ValueError("mining candidates artifact schema is invalid")
        matches = frame.filter(pl.col("signature") == signature)
        if matches.height == 0:
            raise KeyError(signature)
        if matches.height != 1:
            raise ValueError("mining candidates artifact contains a duplicate signature")
        row = matches.row(0, named=True)
        backlinks = self.documents.get("mining_backlinks", f"{run_id}:{signature}", {})
        row.update(backlinks)
        self._asset_type(manifest)
        definition = self._validate_definition(manifest, row, signature)
        summary = self._validated_summary(self.run_store.read_summary(run_id))
        return manifest, summary, run_id, frame, row, definition

    def _write_backlink(self, run_id, frame, signature, field, value):
        def change(current):
            values = current or {}
            if values.get(field) and values[field] != value:
                raise ValueError("mining candidate publication conflict")
            return {**values, field: value}

        self.documents.mutate("mining_backlinks", f"{run_id}:{signature}", change)

    def publish(self, run_id, signature):
        manifest, summary, _, frame, row, definition = self._load_candidate(run_id, signature)
        gate = evaluate_candidate_gate(
            confidence=row.get("confidence"), valid_folds=row.get("valid_folds"),
            positive_fold_ratio=row.get("oos_positive_fold_ratio"), sharpe=row.get("oos_sharpe"),
            max_drawdown=row.get("oos_max_drawdown"), n_trades=row.get("oos_n_trades"),
        )
        if not gate.qualified:
            raise ValueError("candidate does not meet the promotion gate: " + "; ".join(gate.reasons))
        asset_type = self._asset_type(manifest)
        if definition["kind"] == "existing_strategy":
            published_id = str(definition["strategy_id"])
            self._validate_publication_backlink(row, published_id)
            self._verify_public_strategy(published_id, asset_type)
        else:
            published_id = self._validate_published_id(_published_strategy_id(run_id, signature))
            self._validate_publication_backlink(row, published_id)
            code = self._render_factor_strategy(manifest, summary, row, definition, published_id)
            existing = self.documents.get("strategy_sources", published_id)
            if existing:
                if existing["source"] != "custom" or existing["code"] != code:
                    raise FileExistsError("strategy publication conflicts with an existing source")
            else:
                try:
                    self.strategy_engine.save_source(published_id, code, "custom")
                except Exception:
                    # Concurrent identical publish is idempotent. Other content
                    # and validation failures retain the original exception.
                    existing = self.documents.get("strategy_sources", published_id)
                    if not existing or existing["source"] != "custom" or existing["code"] != code:
                        raise
            self.strategy_engine.reload()
            self._verify_public_strategy(published_id, asset_type)
        self._write_backlink(run_id, frame, signature, "published_strategy_id", published_id)
        return {"ok": True, "strategy_id": published_id}
