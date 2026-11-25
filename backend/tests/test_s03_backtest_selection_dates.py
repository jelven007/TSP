"""Typed research dates must have the same entry identity as persisted dates."""
from copy import deepcopy
from datetime import date, datetime

import pytest
from test_entry_groups import run, snapshot


def test_typed_atr_reference_date_in_s03_selection_does_not_break_entry():
    frozen=snapshot()
    frozen["rows"][0]["atr_reference_date"]=date(2026,11,19)
    frozen["scoring_provenance"]={"observed_cutoff":datetime(2026,11,19,21,0)}
    result=run(selection=frozen)
    assert not result.diagnostics
    assert result.symbols==["A","AB","B"]


def test_typed_and_persisted_dates_share_s03_task_identity():
    typed=snapshot()
    typed["rows"][0]["atr_reference_date"]=date(2026,11,19)
    state={}
    first=run(selection=typed,task_state=state)
    identity=state["identity"]
    persisted=deepcopy(typed)
    persisted["rows"][0]["atr_reference_date"]="2026-11-19"
    second=run(selection=persisted,task_state=state)
    assert state["identity"]==identity
    assert first.symbols==second.symbols==["A","AB","B"]


def test_non_date_objects_are_not_silently_encoded_as_selection_evidence():
    frozen = snapshot()
    frozen["rows"][0]["atr_reference_date"] = object()
    with pytest.raises(TypeError, match="object is not JSON serializable"):
        run(selection=frozen)
