"""Named multi-stock batches: numbering, atomicity and stock-only editing."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import lots as api
from app.strategy import lots as domain
from app.strategy import momentum_selection, monitor_rules


def stock(symbol="600519.SH", **changes):
    return {"symbol": symbol, "qty": 100, "cost_price": 10, "target_pct": 6,
            "stop_pct": 5, **changes}


@pytest.fixture
def lot_request(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_reload_engine", lambda _: None)
    monkeypatch.setattr("app.services.preferences.get_webhook_default_channels", lambda: [])
    monkeypatch.setattr(domain, "_batch_day", lambda: "20261101", raising=False)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                             resolve_asset_type=lambda _: "stock"))))


def create(request, key="request_1", **changes):
    body = {"name": "自定义组合", "request_id": key,
            "stocks": [stock(), stock("000001.SZ")], **changes}
    return api.create_batch(api.BatchModel(**body), request)


def test_prioritized_selection_keeps_all_rows_and_matches_buy_head():
    rows = pl.DataFrame({"symbol": ["600003.SH", "600001.SH", "600002.SH"],
                         "return_1430": [.02, .02, .03], "prev_d_amount20": [3e8, 5e8, 4e8]})
    for limit, expected in ((2, ["600002.SH", "600001.SH", "600003.SH"]),
                            (3, ["600001.SH", "600002.SH", "600003.SH"])):
        ordered = momentum_selection.prioritized_selection(rows, {"position_limit": limit})
        assert ordered["symbol"].to_list() == expected
        assert momentum_selection.ranked_candidates(rows, {"position_limit": limit}).equals(ordered.head(limit))


def test_multi_batch_persist_retry_edit_and_delete(lot_request, tmp_path):
    request = lot_request
    result = create(request)
    assert result["batch"]["number"] == "2026110101"
    assert result["batch"]["name"] == "自定义组合"
    assert len(result["lots"]) == 2
    assert len(monitor_rules.load_all(tmp_path)) == 2
    assert create(request)["batch"]["id"] == result["batch"]["id"]
    assert len(domain.load_all(tmp_path)) == 2
    lot = result["lots"][0]
    # Old single-stock clients omit batch_id; editing must retain grouping.
    assert api.upsert_lot(api.LotModel(**{k: v for k, v in {**lot, "qty": 200}.items()
                                       if k != "batch_id"}), request)["lot"]["batch_id"] == result["batch"]["id"]
    api.delete_lot(lot["id"], request)
    assert len(domain.load_all(tmp_path)) == 1
    assert len(monitor_rules.load_all(tmp_path)) == 1
    assert create(request, "request_2")["batch"]["number"] == "2026110102"
    assert len(api.list_batches(request)["batches"]) == 2


def test_batch_rejects_duplicate_symbols_and_changed_retry(lot_request, tmp_path):
    request = lot_request
    with pytest.raises(HTTPException) as exc:
        create(request, stocks=[stock(), stock()])
    assert exc.value.status_code == 400
    assert domain.load_all(tmp_path) == []
    create(request)
    with pytest.raises(HTTPException) as exc:
        create(request, name="different")
    assert exc.value.status_code == 409


def test_batch_validates_every_stock_before_write(lot_request, tmp_path):
    request = lot_request
    with pytest.raises(HTTPException):
        create(request, stocks=[stock(), stock("000001.SZ", cost_price=0)])
    assert domain.load_all(tmp_path) == []
    assert monitor_rules.load_all(tmp_path) == []
    assert api.list_batches(request)["next_number"] == "2026110101"


def test_batch_failure_rolls_back_stocks_rules_and_counter(lot_request, tmp_path, monkeypatch):
    request = lot_request
    original = monitor_rules.save_one
    calls = []

    def fail_second(store, rule):
        calls.append(rule["id"])
        if len(calls) == 2:
            raise RuntimeError("second rule failed")
        original(store, rule)

    monkeypatch.setattr(monitor_rules, "save_one", fail_second)
    with pytest.raises(RuntimeError, match="second rule"):
        create(request)
    assert domain.load_all(tmp_path) == []
    assert monitor_rules.load_all(tmp_path) == []
    assert api.list_batches(request)["batches"] == []
    assert api.list_batches(request)["next_number"] == "2026110101"


def test_numbering_99_daily_reset_and_concurrent_requests(lot_request, tmp_path, monkeypatch):
    request = lot_request
    def submit(n):
        return create(request, f"request_{n}", stocks=[stock()])["batch"]["number"]

    with ThreadPoolExecutor(max_workers=5) as pool:
        numbers = list(pool.map(submit, range(10)))
    assert set(numbers) == {f"20261101{i:02d}" for i in range(1, 11)}
    for n in range(10, 99):
        submit(n)
    assert api.list_batches(request)["next_number"] is None
    with pytest.raises(HTTPException) as exc:
        submit(100)
    assert exc.value.status_code == 409
    # A retry of an existing successful request still works at the limit.
    assert submit(0) in numbers
    monkeypatch.setattr(domain, "_batch_day", lambda: "20261102")
    assert submit(101) == "2026110201"


def test_nonfinite_stock_values_are_rejected(lot_request):
    request = lot_request
    for field in ("cost_price", "qty", "target_pct", "stop_pct"):
        with pytest.raises(HTTPException):
            create(request, stocks=[stock(**{field: float("nan")})])


def test_delete_selected_batch_cascades_both_rules_and_preserves_other_data(lot_request, tmp_path, monkeypatch):
    selected = create(lot_request, stocks=[stock(remind_date="2026-12-01"),
                                         stock("000001.SZ", remind_date="2026-12-01")])
    other = create(lot_request, "other")
    legacy = api.upsert_lot(api.LotModel(**stock("600000.SH")), lot_request)["lot"]
    before_rules = monitor_rules.load_all(tmp_path)
    reloads = []
    monkeypatch.setattr(api, "_reload_engine", lambda _: reloads.append(domain.load_batches(tmp_path)))
    result = api.delete_batch(selected["batch"]["id"], lot_request)
    assert result == {"ok": True, "deleted_lots": 2}
    assert domain.load_batches(tmp_path) == [other["batch"]]
    assert {lot["id"] for lot in domain.load_all(tmp_path)} == {legacy["id"], *(lot["id"] for lot in other["lots"])}
    selected_ids = {lot["id"] for lot in selected["lots"]}
    assert monitor_rules.load_all(tmp_path) == [r for r in before_rules if r.get("lot_id") not in selected_ids]
    assert reloads == [[other["batch"]]]
    assert domain.next_batch_number(tmp_path) == "2026110103"


def test_delete_empty_batch_keeps_numbering_and_unknown_delete_is_rejected(lot_request, tmp_path):
    selected = create(lot_request)
    for lot in selected["lots"]:
        api.delete_lot(lot["id"], lot_request)
    assert api.delete_batch(selected["batch"]["id"], lot_request) == {"ok": True, "deleted_lots": 0}
    assert domain.load_batches(tmp_path) == []
    assert create(lot_request, "next")["batch"]["number"] == "2026110102"
    for batch_id, status in (("2026110101", 404), ("legacy", 400), ("2026110102\n", 400)):
        with pytest.raises(HTTPException) as exc:
            api.delete_batch(batch_id, lot_request)
        assert exc.value.status_code == status


def test_batch_delete_failure_rolls_back_lots_rules_and_metadata(lot_request, tmp_path, monkeypatch):
    selected = create(lot_request, stocks=[stock(remind_date="2026-12-01"), stock("000001.SZ")])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    original = monitor_rules.delete_one
    calls = []
    def fail_second(store, rule_id, **kwargs):
        calls.append(rule_id)
        if len(calls) == 2:
            raise RuntimeError("delete failed")
        return original(store, rule_id, **kwargs)
    monkeypatch.setattr(monitor_rules, "delete_one", fail_second)
    reloads = []
    monkeypatch.setattr(api, "_reload_engine", lambda _: reloads.append(True))
    with pytest.raises(RuntimeError, match="delete failed"):
        api.delete_batch(selected["batch"]["id"], lot_request)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.json")} == before
    assert reloads == []


def test_batch_delete_rejects_unsafe_stored_member_ids_before_any_write(lot_request, tmp_path):
    import json

    selected = create(lot_request)
    path = tmp_path / "user_data" / "lots" / f"{selected['lots'][1]['id']}.json"
    value = json.loads(path.read_text())
    value["id"] = "../outside"
    path.write_text(json.dumps(value))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    with pytest.raises(HTTPException) as exc:
        api.delete_batch(selected["batch"]["id"], lot_request)
    assert exc.value.status_code == 400
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.json")} == before
