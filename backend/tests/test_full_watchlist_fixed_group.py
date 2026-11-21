"""Fixed watchlist groups share protection across both SQL entry points."""
import pytest
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.cell.context import AccessError
from app.cell.watchlist import Watchlist as CellWatchlist
from app.multitenancy.watchlist import Watchlist


def test_s03_sql_order_and_deletion_are_protected(env):
    _, account = register(env)
    context = env["identity"].context(account, None, "fixed-group")
    watchlist = Watchlist(env["db"], context)
    _, first = watchlist.create_group("一")
    _, fixed = watchlist.create_group("s03", "orange")
    watchlist.add("600000.SH", "保留备注", group_id=fixed["id"])
    revision = watchlist.revision()
    before = watchlist.list_symbols()
    assert watchlist.list_groups()[0]["id"] == fixed["id"]
    assert watchlist.list_groups()[0]["protected"] is True
    with pytest.raises(ValueError, match="固定分组"):
        watchlist.delete_group(fixed["id"])
    with pytest.raises(ValueError, match="固定分组"):
        watchlist.rename_group(fixed["id"], "改名")
    cell = CellWatchlist(env["db"])
    with pytest.raises(AccessError, match="固定分组"):
        cell.delete_group(context, fixed["id"])
    with pytest.raises(AccessError, match="固定分组"):
        cell.group(context, fixed["id"], "改名", "sky")
    assert watchlist.revision() == revision
    assert watchlist.list_symbols() == before
    assert [g["id"] for g in watchlist.reorder_groups([first["id"], fixed["id"]])] == [
        fixed["id"], first["id"],
    ]
    assert [g["id"] for g in cell.reorder(context, [first["id"], fixed["id"]])["groups"]] == [
        fixed["id"], first["id"],
    ]
    with env["db"].business_transaction(context) as conn:
        positions = conn.execute(
            "SELECT id FROM business.watchlist_groups ORDER BY position,id"
        ).fetchall()
    assert [g["id"] for g in positions] == [fixed["id"], first["id"]]
