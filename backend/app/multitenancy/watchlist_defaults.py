"""Controlled column catalog defaults; never initialize watchlist business data."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["builtin"]
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")


class _Column(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=96, pattern=r"^builtin:[a-zA-Z0-9_]+$")
    label: str = Field(min_length=1, max_length=64)
    source: _Source
    align: Literal["left", "center", "right"]
    visible: bool = Field(strict=True)


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: Literal[2]
    columns: list[_Column] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_columns(self):
        if len({row.id for row in self.columns}) != len(self.columns):
            raise ValueError("duplicate default columns")
        return self


def load_snapshot(path: Path) -> Snapshot:
    raw = path.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("watchlist column defaults too large")
    return Snapshot.model_validate_json(raw)


def seed_columns(conn, user_id: str, columns: list[dict]) -> None:
    """Preserve explicit user settings and merge only an absent default."""
    conn.execute(
        """INSERT INTO control.user_preferences(user_id,value) VALUES(%s,%s)
           ON CONFLICT(user_id) DO UPDATE SET
             value=control.user_preferences.value || excluded.value,
             revision=control.user_preferences.revision+1
           WHERE control.user_preferences.value->'watchlist_columns' IS NULL
              OR control.user_preferences.value->'watchlist_columns'='null'::jsonb""",
        (user_id, Jsonb({"watchlist_columns": columns})),
    )
