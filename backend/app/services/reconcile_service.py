from __future__ import annotations
from datetime import datetime, timezone
import sqlite3

# 全量重扫架构：每次对账按 dock_day 重聚合三腿正式表全集，生成新 open 批并作废旧 open。
# 不依赖任何入湖水位字段；批上快照 scanned_rows 与三腿行数/合计，
# 结案前以「当前全量重算 vs 批快照」互证，分叉即挡结案。
LEGS = (("expect", "expect_lines"), ("receive", "receive_lines"), ("confirm", "confirm_lines"))
TOTAL_EPS = 1e-6


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _leg_stats(conn: sqlite3.Connection, table: str, dock_day: str):
    """单腿全集聚合：键值合计映射 + 行数 + 总量。"""
    rows = conn.execute(
        f"SELECT supplier_code, sku, SUM(qty) q FROM {table} WHERE dock_day=? GROUP BY supplier_code, sku",
        (dock_day,),
    ).fetchall()
    m = {(r["supplier_code"], r["sku"]): float(r["q"]) for r in rows}
    agg = conn.execute(
        f"SELECT COUNT(*) n, COALESCE(SUM(qty), 0) t FROM {table} WHERE dock_day=?",
        (dock_day,),
    ).fetchone()
    return m, int(agg["n"]), float(agg["t"])


def _snapshot(conn: sqlite3.Connection, dock_day: str) -> dict:
    """对三腿正式表做当前全量重算，得到水位快照（须在事务内调用，保证一致切点）。"""
    snap = {}
    for leg, table in LEGS:
        m, n, t = _leg_stats(conn, table, dock_day)
        snap[leg] = {"map": m, "rows": n, "total": t}
    snap["scanned_rows"] = sum(snap[leg]["rows"] for leg, _ in LEGS)
    return snap


def _snapshot_diff(batch: sqlite3.Row, snap: dict) -> list[str]:
    """批快照与当前全量重算的分叉明细；空列表 = 一致。"""
    diff = []
    for leg, _ in LEGS:
        b_rows, n_rows = int(batch[f"{leg}_rows"]), snap[leg]["rows"]
        if b_rows != n_rows:
            diff.append(f"{leg} 行数 批={b_rows} 湖={n_rows}")
        b_total, n_total = float(batch[f"{leg}_total"]), snap[leg]["total"]
        if abs(b_total - n_total) > TOTAL_EPS:
            diff.append(f"{leg} 合计 批={b_total:g} 湖={n_total:g}")
    return diff


def _latest_open(conn: sqlite3.Connection, dock_day: str):
    return conn.execute(
        "SELECT * FROM reconcile_batches WHERE dock_day=? AND status='open' ORDER BY id DESC LIMIT 1",
        (dock_day,),
    ).fetchone()


def run_reconcile(conn: sqlite3.Connection, dock_day: str) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        sealed = conn.execute(
            "SELECT 1 FROM reconcile_batches WHERE dock_day=? AND status='sealed' LIMIT 1",
            (dock_day,),
        ).fetchone()
        if sealed:
            raise ValueError(f"到货日 {dock_day} 已结案，不能重跑对账")

        # 作废旧 open 批：批行置 voided 留档（快照可审计、批号不复用），其工作行随新批重建
        old = conn.execute(
            "SELECT id FROM reconcile_batches WHERE dock_day=? AND status='open'",
            (dock_day,),
        ).fetchall()
        for r in old:
            conn.execute("DELETE FROM match_rows WHERE batch_id=?", (r["id"],))
            conn.execute("DELETE FROM variance_rows WHERE batch_id=?", (r["id"],))
            conn.execute("UPDATE reconcile_batches SET status='voided' WHERE id=?", (r["id"],))

        snap = _snapshot(conn, dock_day)
        keys: set[tuple[str, str]] = set()
        for leg, _ in LEGS:
            keys |= set(snap[leg]["map"])

        n = conn.execute("SELECT COUNT(*) c FROM reconcile_batches").fetchone()["c"] + 1
        batch_no = f"RB-{dock_day.replace('-', '')}-{n:03d}"
        now = _now()
        cur = conn.execute(
            '''INSERT INTO reconcile_batches(batch_no, dock_day, matched_count, variance_count, status, sealed_at, note,
                                             scanned_rows, expect_rows, receive_rows, confirm_rows,
                                             expect_total, receive_total, confirm_total, created_at)
               VALUES (?,?,0,0,'open',NULL,?,?,?,?,?,?,?,?,?)''',
            (
                batch_no,
                dock_day,
                "三联对齐",
                snap["scanned_rows"],
                snap["expect"]["rows"],
                snap["receive"]["rows"],
                snap["confirm"]["rows"],
                snap["expect"]["total"],
                snap["receive"]["total"],
                snap["confirm"]["total"],
                now,
            ),
        )
        batch_id = int(cur.lastrowid)

        matched = 0
        variance = 0
        for supplier_code, sku in sorted(keys):
            k = (supplier_code, sku)
            eq = snap["expect"]["map"].get(k, 0.0)
            rq = snap["receive"]["map"].get(k, 0.0)
            cq = snap["confirm"]["map"].get(k, 0.0)
            aligned = 1 if eq == rq == cq else 0
            conn.execute(
                '''INSERT INTO match_rows(batch_id, dock_day, supplier_code, sku, expect_qty, receive_qty, confirm_qty, aligned)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (batch_id, dock_day, supplier_code, sku, eq, rq, cq, aligned),
            )
            if aligned:
                matched += 1
            else:
                variance += 1
                conn.execute(
                    '''INSERT INTO variance_rows(batch_id, dock_day, supplier_code, sku, expect_qty, receive_qty, confirm_qty, fate, created_at)
                       VALUES (?,?,?,?,?,?,?,'pending',?)''',
                    (batch_id, dock_day, supplier_code, sku, eq, rq, cq, now),
                )

        conn.execute(
            "UPDATE reconcile_batches SET matched_count=?, variance_count=? WHERE id=?",
            (matched, variance, batch_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "batch_id": batch_id,
        "batch_no": batch_no,
        "dock_day": dock_day,
        "matched_count": matched,
        "variance_count": variance,
        "status": "open",
        "scanned_rows": snap["scanned_rows"],
        "expect_rows": snap["expect"]["rows"],
        "receive_rows": snap["receive"]["rows"],
        "confirm_rows": snap["confirm"]["rows"],
        "expect_total": snap["expect"]["total"],
        "receive_total": snap["receive"]["total"],
        "confirm_total": snap["confirm"]["total"],
    }


def verify_day(conn: sqlite3.Connection, dock_day: str) -> dict:
    """互证入口：当前 open 批快照 vs 此刻全量重算。分叉明细为空才允许结案。"""
    conn.execute("BEGIN")
    try:
        batch = _latest_open(conn, dock_day)
        if not batch:
            raise ValueError(f"到货日 {dock_day} 没有可互证的 open 批次")
        snap = _snapshot(conn, dock_day)
        diff = _snapshot_diff(batch, snap)
        result = {
            "batch_id": batch["id"],
            "batch_no": batch["batch_no"],
            "dock_day": dock_day,
            "consistent": not diff,
            "divergence": diff,
            "snapshot": {
                "scanned_rows": batch["scanned_rows"],
                "expect_rows": batch["expect_rows"],
                "receive_rows": batch["receive_rows"],
                "confirm_rows": batch["confirm_rows"],
                "expect_total": batch["expect_total"],
                "receive_total": batch["receive_total"],
                "confirm_total": batch["confirm_total"],
            },
            "current": {
                "scanned_rows": snap["scanned_rows"],
                "expect_rows": snap["expect"]["rows"],
                "receive_rows": snap["receive"]["rows"],
                "confirm_rows": snap["confirm"]["rows"],
                "expect_total": snap["expect"]["total"],
                "receive_total": snap["receive"]["total"],
                "confirm_total": snap["confirm"]["total"],
            },
        }
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def seal_day(conn: sqlite3.Connection, dock_day: str) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        batch = _latest_open(conn, dock_day)
        if not batch:
            raise ValueError("没有可结案的对账批次，请先跑三联对账")
        # 结案写闸互证：批快照必须等于此刻全量重算，否则说明批后仍有新行入湖未扫
        diff = _snapshot_diff(batch, _snapshot(conn, dock_day))
        if diff:
            raise ValueError(
                "对账批次与当前入湖数据分叉（" + "；".join(diff) + "），存在未入批的新行，请重跑三联对账后再结案"
            )
        pending = conn.execute(
            "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'",
            (batch["id"],),
        ).fetchone()["c"]
        if pending:
            raise ValueError(f"仍有 {pending} 条差额未处置，不能结案")
        now = _now()
        conn.execute(
            "UPDATE reconcile_batches SET status='sealed', sealed_at=? WHERE id=?",
            (now, batch["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch["id"],)).fetchone())


def set_variance_fate(conn: sqlite3.Connection, variance_id: int, fate: str) -> dict:
    allowed = {"pending", "accept_short", "accept_over", "quarantine"}
    if fate not in allowed:
        raise ValueError("非法差额命运")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone()
        if not row:
            raise ValueError("差额行不存在")
        batch = conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (row["batch_id"],)).fetchone()
        if batch and batch["status"] == "sealed":
            raise ValueError("已结案批次不可改差额命运")
        conn.execute("UPDATE variance_rows SET fate=? WHERE id=?", (fate, variance_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone())
