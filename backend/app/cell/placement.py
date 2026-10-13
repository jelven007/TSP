"""Capacity-weighted placement. Existing tenant placements never change implicitly."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CellLoad:
    id: str
    status: str
    tenants: int
    tenant_limit: int
    realtime_units: int
    realtime_limit: int


def select_cell(cells: list[CellLoad], realtime_units: int = 1) -> str:
    if realtime_units < 0:
        raise ValueError("negative realtime reservation")
    candidates = [
        c for c in cells if c.status == "active" and c.tenants < c.tenant_limit
        and c.realtime_units + realtime_units <= c.realtime_limit
    ]
    if not candidates:
        raise ValueError("no Cell has available capacity")
    # Minimize projected bottleneck utilization, rather than distributing heavy and
    # light tenants solely by tenant count or hash.
    return min(candidates, key=lambda c: (
        max((c.tenants + 1) / c.tenant_limit, (c.realtime_units + realtime_units) / c.realtime_limit),
        c.realtime_units, c.tenants, c.id,
    )).id


def cell_loads(control) -> list[CellLoad]:
    return [CellLoad(**row) for row in control.execute(
        """SELECT c.id,c.status,c.tenant_limit,c.realtime_limit,count(t.id)::integer AS tenants,
                  coalesce(sum(t.realtime_reservation),0)::integer AS realtime_units
           FROM control.cells c LEFT JOIN control.tenants t ON t.cell_id=c.id
           GROUP BY c.id ORDER BY c.id"""
    ).fetchall()]


def planning_loads(control, tenants: list[dict]) -> list[CellLoad]:
    """Remove the input tenants' current reservations before repacking them."""
    from dataclasses import replace

    loads = {cell.id: cell for cell in cell_loads(control)}
    existing = control.execute(
        "SELECT cell_id,realtime_reservation FROM control.tenants WHERE id=ANY(%s)",
        ([row["tenant_id"] for row in tenants],),
    ).fetchall()
    for row in existing:
        cell = loads[row["cell_id"]]
        loads[cell.id] = replace(cell, tenants=cell.tenants - 1,
                                 realtime_units=cell.realtime_units - row["realtime_reservation"])
    return list(loads.values())


def split_plan(tenants: list[dict], cells: list[CellLoad]) -> dict:
    """Read-only packing plan; raises instead of silently overfilling cells."""
    from dataclasses import replace

    if len({row["tenant_id"] for row in tenants}) != len(tenants):
        raise ValueError("duplicate tenant in split plan")
    current = {cell.id: cell for cell in cells}
    assignments = []
    for tenant in sorted(tenants, key=lambda row: (-row["realtime_units"], row["tenant_id"])):
        units = tenant["realtime_units"]
        cell_id = select_cell(list(current.values()), units)
        load = current[cell_id]
        current[cell_id] = replace(load, tenants=load.tenants + 1, realtime_units=load.realtime_units + units)
        assignments.append({**tenant, "cell_id": cell_id})
    return {"assignments": assignments, "cells": [
        {"cell_id": c.id, "tenants": c.tenants, "realtime_units": c.realtime_units,
         "tenant_limit": c.tenant_limit, "realtime_limit": c.realtime_limit}
        for c in sorted(current.values(), key=lambda c: c.id)
    ]}
