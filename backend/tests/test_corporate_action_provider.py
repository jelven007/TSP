from datetime import date

import polars as pl
import pytest

from app.data_providers.corporate_actions import CorporateAction
from app.plugins.rustdx.provider import RustdxProvider
from app.services.corporate_actions import load_actions, save_actions, sync_actions


class Client:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def xdxr(self, symbol):
        return [{
            "category": 1, "year": 2026, "month": 9, "day": 22,
            "fenhong": 3, "songzhuangu": 5, "peigu": 2, "peigujia": 6,
        }]


def test_rustdx_preserves_actual_units_and_does_not_invent_event_dates():
    row = RustdxProvider(Client).get_corporate_actions(["600519.SH"]).to_dicts()[0]
    event = CorporateAction.model_validate(row)
    assert (event.cash_per_share, event.bonus_per_share, event.rights_per_share, event.rights_price) == (.3, .5, .2, 6)
    assert event.ex_date == date(2026, 9, 22)
    assert event.record_date is event.cash_pay_date is event.bonus_listing_date is None
    assert event.dates_verified is False
    assert "verified_date_evidence" in event.missing_evidence()


def test_failed_symbol_rejects_whole_rustdx_batch():
    class Broken(Client):
        def xdxr(self, symbol):
            if symbol == "000001.SZ":
                raise OSError("upstream unavailable")
            return super().xdxr(symbol)

    with pytest.raises(OSError):
        RustdxProvider(Broken).get_corporate_actions(["600519.SH", "000001.SZ"])


def test_storage_identity_duplicate_and_cache_refresh(tmp_path):
    event = CorporateAction.model_validate(RustdxProvider(Client).get_corporate_actions(["600519.SH"]).to_dicts()[0])
    save_actions(tmp_path, [event])
    assert load_actions(tmp_path, {"000001.SZ"}) == []
    assert load_actions(tmp_path)[0].cash_per_share == .3
    save_actions(tmp_path, [event.model_copy(update={"cash_per_share": .4})])
    assert load_actions(tmp_path)[0].cash_per_share == .4
    with pytest.raises(ValueError, match="duplicate"):
        save_actions(tmp_path, [event, event])
    with pytest.raises(ValueError, match="conflicting"):
        save_actions(tmp_path, [event.model_copy(update={"id": "other", "source": "another"})])
    assert load_actions(tmp_path)[0].cash_per_share == .4


def test_missing_capability_never_falls_back(tmp_path, monkeypatch):
    from app.data_providers import custom
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_corporate_actions_provider", lambda: "missing")
    monkeypatch.setattr(custom, "provider_has_dataset", lambda *_: False)
    with pytest.raises(ValueError, match="不提供"):
        sync_actions(tmp_path, ["600519.SH"])
    assert load_actions(tmp_path) == []


def test_custom_http_normalizes_contract_and_failure_is_not_partial(monkeypatch):
    from app.data_providers.custom.config import config_from_dict
    from app.data_providers.custom.provider import GenericHTTPProvider

    config = config_from_dict({
        "name": "verified", "datasets": {"corporate_actions": {
            "url": "https://example.test/actions", "batch": 1,
            "field_map": {"id": "id", "symbol": "symbol", "ex_date": "ex_date"},
        }},
    })
    provider = GenericHTTPProvider(config)
    row = {"id": "one", "symbol": "600519.SH", "ex_date": "2026-09-22"}
    monkeypatch.setattr(provider, "_request_rows_retry", lambda *_a, **_kw: [row])
    result = provider.get_corporate_actions(["600519.SH"])
    assert isinstance(result, pl.DataFrame)
    assert result["source"][0] == "verified"
    assert result["dates_verified"][0] is False
