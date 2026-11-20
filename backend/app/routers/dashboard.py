from __future__ import annotations
from fastapi import APIRouter
from app.db import connect
from app.services.gate import is_sealed

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
def dashboard():
    conn = connect()
    try:
        conn.execute("BEGIN")
        day = conn.execute("SELECT value FROM settings WHERE key='dock_day'").fetchone()["value"]
        legs = {}
        for leg, table in (("expect", "expect_lines"), ("receive", "receive_lines"), ("confirm", "confirm_lines")):
            r = conn.execute(
                f"SELECT COUNT(*) n, COALESCE(SUM(qty), 0) t FROM {table} WHERE dock_day=?", (day,)
            ).fetchone()
            legs[leg] = {"count": int(r["n"]), "total": float(r["t"])}
        batch = conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? ORDER BY id DESC LIMIT 1",
            (day,),
        ).fetchone()
        pending = 0
        if batch:
            pending = conn.execute(
                "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'",
                (batch["id"],),
            ).fetchone()["c"]
        current_rows = sum(legs[leg]["count"] for leg in legs)
        open_batch = batch if batch and batch["status"] == "open" else None
        scanned = int(open_batch["scanned_rows"]) if open_batch else (int(batch["scanned_rows"]) if batch else 0)
        result = {
            "dock_day": day,
            "sealed": is_sealed(conn, day),
            "expect_count": legs["expect"]["count"],
            "receive_count": legs["receive"]["count"],
            "confirm_count": legs["confirm"]["count"],
            "expect_total": legs["expect"]["total"],
            "receive_total": legs["receive"]["total"],
            "confirm_total": legs["confirm"]["total"],
            "current_rows": current_rows,
            "scanned_rows": scanned,
            "unscanned_rows": (current_rows - scanned) if open_batch else 0,
            "latest_batch": dict(batch) if batch else None,
            "pending_variance": pending,
        }
        conn.commit()
        return result
    finally:
        conn.close()
