import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.seed import SCHEMA, migrate  # noqa: E402
from app.services import reconcile_service as svc  # noqa: E402
from app.services.gate import assert_writable  # noqa: E402

DAY = "2026-11-19"


def fresh_conn(path, init_schema=True) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    if init_schema:
        conn.executescript(SCHEMA)
        migrate(conn)
    return conn


def add_line(conn, table, no, day, qty, supplier, sku):
    """模拟入湖端点：BEGIN IMMEDIATE + 结案闸 + 插入，同事务。"""
    no_col = {"expect_lines": "doc_no", "receive_lines": "receive_no", "confirm_lines": "confirm_no"}[table]
    extra = ", occurred_at" if table == "receive_lines" else ""
    ph = ",?" if table == "receive_lines" else ""
    vals = (no, supplier, sku, day, qty, "2026-11-19T10:00:00Z") if table == "receive_lines" else (no, supplier, sku, day, qty)
    conn.execute("BEGIN IMMEDIATE")
    try:
        assert_writable(conn, day)
        conn.execute(
            f"INSERT INTO {table}({no_col}, supplier_code, sku, dock_day, qty{extra}, created_at) VALUES (?,?,?,?,?{ph},?)",
            (*vals, "2026-11-19T10:00:00Z"),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def add_triple(conn, tag, day=DAY, qty=5.0, supplier="SUP-A", sku=None):
    sku = sku or f"SKU-{tag}"
    add_line(conn, "expect_lines", f"EX-{tag}", day, qty, supplier, sku)
    add_line(conn, "receive_lines", f"RC-{tag}", day, qty, supplier, sku)
    add_line(conn, "confirm_lines", f"CF-{tag}", day, qty, supplier, sku)


def lake_counts(conn, day=DAY):
    out = {}
    for leg, table in (("expect", "expect_lines"), ("receive", "receive_lines"), ("confirm", "confirm_lines")):
        r = conn.execute(f"SELECT COUNT(*) n, COALESCE(SUM(qty),0) t FROM {table} WHERE dock_day=?", (day,)).fetchone()
        out[leg] = (int(r["n"]), float(r["t"]))
    return out


def test_full_recompute_replaces_open_batch_and_covers_new_rows(tmp_path):
    conn = fresh_conn(tmp_path / "t.db")
    add_triple(conn, "a")
    b1 = svc.run_reconcile(conn, DAY)
    assert b1["scanned_rows"] == 3
    assert (b1["expect_rows"], b1["receive_rows"], b1["confirm_rows"]) == (1, 1, 1)

    # 新实收入湖后重跑：全集重扫必须含新行，旧 open 批作废（置 voided 留档，批号不复用）
    add_line(conn, "receive_lines", "RC-b", DAY, 7.0, "SUP-B", "SKU-b")
    b2 = svc.run_reconcile(conn, DAY)
    assert b2["batch_no"] != b1["batch_no"]
    assert conn.execute("SELECT COUNT(*) c FROM reconcile_batches WHERE dock_day=? AND status='open'", (DAY,)).fetchone()["c"] == 1
    old = conn.execute("SELECT * FROM reconcile_batches WHERE batch_no=?", (b1["batch_no"],)).fetchone()
    assert old["status"] == "voided"
    assert conn.execute("SELECT COUNT(*) c FROM match_rows WHERE batch_id=?", (old["id"],)).fetchone()["c"] == 0
    keys = {r["sku"] for r in conn.execute("SELECT sku FROM match_rows WHERE batch_id=?", (b2["batch_id"],))}
    assert keys == {"SKU-a", "SKU-b"}
    assert b2["scanned_rows"] == 4 and b2["receive_rows"] == 2 and b2["receive_total"] == 12.0
    counts = lake_counts(conn)
    assert b2["scanned_rows"] == sum(n for n, _ in counts.values())


def test_verify_divergence_blocks_seal_until_rerun(tmp_path):
    conn = fresh_conn(tmp_path / "t.db")
    add_triple(conn, "a")
    svc.run_reconcile(conn, DAY)

    # 对账后新实收入湖：互证必须报分叉，结案闸必须挡
    add_line(conn, "receive_lines", "RC-late", DAY, 3.0, "SUP-A", "SKU-a")
    v = svc.verify_day(conn, DAY)
    assert not v["consistent"]
    assert any("receive" in d for d in v["divergence"])
    assert v["snapshot"]["receive_rows"] == 1 and v["current"]["receive_rows"] == 2
    with pytest.raises(ValueError, match="分叉"):
        svc.seal_day(conn, DAY)

    # 重跑后互证一致；处置差额（实收 8 vs 预报/回执 5）后可结案
    svc.run_reconcile(conn, DAY)
    assert svc.verify_day(conn, DAY)["consistent"]
    pend = conn.execute("SELECT id FROM variance_rows WHERE fate='pending'").fetchall()
    assert pend, "实收改量后应产生差额"
    for r in pend:
        svc.set_variance_fate(conn, r["id"], "accept_over")
    sealed = svc.seal_day(conn, DAY)
    assert sealed["status"] == "sealed" and sealed["sealed_at"]
    # 封印批的快照 == 封印时刻全量（此刻即最终湖态，因已禁止再写）
    counts = lake_counts(conn)
    assert sealed["scanned_rows"] == sum(n for n, _ in counts.values())
    assert (sealed["receive_rows"], sealed["receive_total"]) == counts["receive"]


def test_sealed_day_rejects_all_writes(tmp_path):
    conn = fresh_conn(tmp_path / "t.db")
    add_triple(conn, "a")
    svc.run_reconcile(conn, DAY)
    svc.seal_day(conn, DAY)

    with pytest.raises(ValueError, match="已结案"):
        svc.run_reconcile(conn, DAY)
    with pytest.raises(ValueError, match="已结案"):
        add_line(conn, "receive_lines", "RC-x", DAY, 1.0, "SUP-A", "SKU-a")
    with pytest.raises(ValueError, match="已结案"):
        add_line(conn, "expect_lines", "EX-x", DAY, 1.0, "SUP-A", "SKU-a")
    with pytest.raises(ValueError, match="已结案"):
        add_line(conn, "confirm_lines", "CF-x", DAY, 1.0, "SUP-A", "SKU-a")
    # 已结案日的湖态不得再变
    assert lake_counts(conn) == {"expect": (1, 5.0), "receive": (1, 5.0), "confirm": (1, 5.0)}


def test_sealed_batch_variance_fate_locked(tmp_path):
    conn = fresh_conn(tmp_path / "t.db")
    add_triple(conn, "a", qty=5.0)
    add_line(conn, "receive_lines", "RC-a2", DAY, 2.0, "SUP-A", "SKU-a")  # 实收 7 ≠ 5
    svc.run_reconcile(conn, DAY)
    vid = conn.execute("SELECT id FROM variance_rows WHERE fate='pending'").fetchone()["id"]
    svc.set_variance_fate(conn, vid, "accept_short")
    svc.seal_day(conn, DAY)
    with pytest.raises(ValueError, match="已结案"):
        svc.set_variance_fate(conn, vid, "quarantine")


def test_snapshot_never_rolls_back(tmp_path):
    conn = fresh_conn(tmp_path / "t.db")
    scanned_seq = []
    for i in range(4):
        add_triple(conn, f"t{i}")
        b = svc.run_reconcile(conn, DAY)
        scanned_seq.append(b["scanned_rows"])
        counts = lake_counts(conn)
        # 每个批快照都必须等于其切点处的全量（此处为静止态，可直接比对）
        assert b["scanned_rows"] == sum(n for n, _ in counts.values())
        for leg in ("expect", "receive", "confirm"):
            assert (b[f"{leg}_rows"], b[f"{leg}_total"]) == counts[leg]
    assert scanned_seq == sorted(scanned_seq)  # 水位单调不回拨


def test_concurrent_ingest_and_reconcile_no_half_advance(tmp_path):
    db = tmp_path / "t.db"
    conn = fresh_conn(db)
    add_triple(conn, "base")
    conn.close()

    errors = []
    N_THREADS, N_PER = 4, 15

    def ingester(tid):
        c = fresh_conn(db, init_schema=False)
        try:
            for i in range(N_PER):
                add_triple(c, f"t{tid}-{i}", qty=1.0)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    def reconciler():
        c = fresh_conn(db, init_schema=False)
        try:
            for _ in range(12):
                svc.run_reconcile(c, DAY)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=ingester, args=(t,)) for t in range(N_THREADS)]
    threads.append(threading.Thread(target=reconciler))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"并发期间出错: {errors}"

    conn = fresh_conn(db)
    # 每个批都是一致切点：scanned == 三腿行数之和，且水位单调不回拨
    batches = conn.execute(
        "SELECT * FROM reconcile_batches WHERE dock_day=? ORDER BY id", (DAY,)
    ).fetchall()
    assert len(batches) >= 2
    scanned_seq = [b["scanned_rows"] for b in batches]
    assert scanned_seq == sorted(scanned_seq)
    for b in batches:
        assert b["scanned_rows"] == b["expect_rows"] + b["receive_rows"] + b["confirm_rows"]

    # 静止后终跑：快照 == 最终湖态全集（1 base + 4*15 三腿对齐三元组，全部对齐无差额）
    final = svc.run_reconcile(conn, DAY)
    total_rows = 3 * (1 + N_THREADS * N_PER)
    assert final["scanned_rows"] == total_rows
    assert final["variance_count"] == 0
    counts = lake_counts(conn)
    assert final["scanned_rows"] == sum(n for n, _ in counts.values())

    # 结案互证通过；封印后并发入湖一律被拒
    sealed = svc.seal_day(conn, DAY)
    assert sealed["scanned_rows"] == total_rows
    with pytest.raises(ValueError, match="已结案"):
        add_line(conn, "receive_lines", "RC-post-seal", DAY, 1.0, "SUP-A", "SKU-x")
    conn.close()


def test_migrate_adds_snapshot_columns_to_old_db(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE reconcile_batches (
             id INTEGER PRIMARY KEY, batch_no TEXT NOT NULL UNIQUE, dock_day TEXT NOT NULL,
             matched_count INTEGER NOT NULL, variance_count INTEGER NOT NULL,
             status TEXT NOT NULL, sealed_at TEXT, note TEXT)"""
    )
    conn.execute("INSERT INTO reconcile_batches(batch_no, dock_day, matched_count, variance_count, status) VALUES ('RB-1', ?, 0, 0, 'open')", (DAY,))
    conn.commit()
    migrate(conn)
    migrate(conn)  # 幂等
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reconcile_batches)")}
    for c in ("scanned_rows", "expect_rows", "receive_rows", "confirm_rows", "expect_total", "receive_total", "confirm_total", "created_at"):
        assert c in cols
    assert conn.execute("SELECT scanned_rows FROM reconcile_batches").fetchone()["scanned_rows"] == 0
    conn.close()
