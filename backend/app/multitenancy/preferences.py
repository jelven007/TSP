"""User-owned display preferences, independent of administrator configuration."""
from __future__ import annotations

import copy
import json

from psycopg.types.json import Jsonb

from app.cell.context import AccessError
from app.cell.database import Databases
from app.services.preferences import normalize_dashboard_layout

DEFAULTS = {
    "dashboard_layout": None, "watchlist_columns": None, "screener_result_columns": None,
    "watchlist_groups_in_nav": False, "sse_refresh_pages": {}, "nav_order": [], "nav_hidden": [],
    "screener_auto_run": False,
}
EDITABLE = frozenset({"dashboard_layout", "watchlist_columns", "screener_result_columns"})


class UserPreferences:
    def __init__(self, db: Databases):
        self.db = db

    def load(self, user_id: str) -> dict:
        with self.db.control.connection() as conn:
            row = conn.execute(
                "SELECT value FROM control.user_preferences WHERE user_id=%s", (user_id,),
            ).fetchone()
        value = row["value"] if row else {}
        return {**copy.deepcopy(DEFAULTS), **{key: value[key] for key in DEFAULTS if key in value}}

    def save_display(self, user_id: str, key: str, value) -> dict:
        if key not in EDITABLE:
            raise AccessError(403, "无权修改该设置")
        if key == "dashboard_layout":
            try:
                value = normalize_dashboard_layout(value)
            except ValueError as exc:
                raise AccessError(400, str(exc)) from exc
        else:
            if not isinstance(value, list) or len(value) > 100 or any(not isinstance(row, dict) for row in value):
                raise AccessError(400, "列表列配置格式错误")
            if len(json.dumps(value, ensure_ascii=False)) > 64 * 1024:
                raise AccessError(400, "列表列配置过大")
        with self.db.control.connection() as conn:
            conn.execute(
                """INSERT INTO control.user_preferences(user_id,value) VALUES(%s,%s)
                   ON CONFLICT(user_id) DO UPDATE SET
                     value=control.user_preferences.value || excluded.value,
                     revision=control.user_preferences.revision+1""", (user_id, Jsonb({key: value})),
            )
        return {key: value}
