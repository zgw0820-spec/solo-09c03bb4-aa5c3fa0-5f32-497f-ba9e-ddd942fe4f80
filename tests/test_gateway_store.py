"""Gateway store: boot generations and simulated device reports."""
from __future__ import annotations

import pytest

from gateway.app.store import GatewayStore


@pytest.fixture()
def store(tmp_path):
    s = GatewayStore(tmp_path / "gateway.db")
    s.init()
    return s


def execute(store, command_id="c1", device_id="d1", status="succeeded"):
    action, _ = store.accept(command_id, device_id, False)
    assert action == "start"
    return store.commit_execution(command_id, device_id, status=status)


def test_successful_execution_issues_new_generation(store):
    row = execute(store, "c1", "d1")
    assert row["boot_generation"] == 1
    row = execute(store, "c2", "d1")
    assert row["boot_generation"] == 2
    assert store.get_device("d1")["generation"] == 2


def test_generations_are_per_device(store):
    assert execute(store, "c1", "d1")["boot_generation"] == 1
    assert execute(store, "c2", "d2")["boot_generation"] == 1
    assert execute(store, "c3", "d1")["boot_generation"] == 2


def test_failed_execution_issues_no_generation(store):
    row = execute(store, "c1", "d1", status="failed")
    assert row["boot_generation"] is None
    assert store.get_device("d1") is None
    assert execute(store, "c2", "d1")["boot_generation"] == 1


def test_reports_are_stamped_by_the_gateway(store):
    execute(store, "c1", "d1")  # generation 1
    r1 = store.add_report("d1", online=True)
    r2 = store.add_report("d1", online=False)
    assert (r1["generation"], r1["seq"]) == (1, 1)
    assert (r2["generation"], r2["seq"]) == (1, 2)
    assert r1["arrived"] == 1 and r1["arrived_at"] is not None

    execute(store, "c2", "d1")  # generation 2, sequence restarts
    r3 = store.add_report("d1", online=True)
    assert (r3["generation"], r3["seq"]) == (2, 1)


def test_report_before_any_reboot_uses_generation_zero(store):
    r = store.add_report("d9", online=True)
    assert (r["generation"], r["seq"]) == (0, 1)
    assert store.get_device("d9") is None  # reporting does not fake a reboot


def test_held_report_keeps_old_stamp_when_released_late(store):
    execute(store, "c1", "d1")  # generation 1
    held = store.add_report("d1", online=True, arrive=False)
    assert (held["generation"], held["seq"]) == (1, 1)
    assert held["arrived"] == 0
    # Held reports are invisible as "arrived" evidence.
    assert [r["arrived"] for r in store.list_reports("d1")] == [0]

    execute(store, "c2", "d1")  # generation 2
    fresh = store.add_report("d1", online=False)
    assert (fresh["generation"], fresh["seq"]) == (2, 1)

    assert store.release_reports("d1") == 1
    rows = {r["seq"]: r for r in store.list_reports("d1") if r["generation"] == 1}
    assert rows[1]["arrived"] == 1
    assert rows[1]["arrived_at"] is not None
    # ...but its stamp is still the old generation: it can never pass as new.
    assert rows[1]["generation"] == 1


def test_held_seq_is_reserved_at_emission_time(store):
    execute(store, "c1", "d1")
    held = store.add_report("d1", online=False, arrive=False)  # seq 1 held
    now = store.add_report("d1", online=True)  # seq 2 arrives first
    assert (held["seq"], now["seq"]) == (1, 2)
    store.release_reports("d1")
    rows = sorted(store.list_reports("d1"), key=lambda r: r["seq"])
    # Arrival order is seq2-then-seq1, but stamps stay (1, 2): the late seq 1
    # can never override seq 2.
    assert [r["seq"] for r in rows] == [1, 2]
    assert rows[0]["online"] == 0 and rows[1]["online"] == 1


def test_generation_and_reports_survive_reopen(store, tmp_path):
    execute(store, "c1", "d1")
    store.add_report("d1", online=True)
    reopened = GatewayStore(tmp_path / "gateway.db")
    reopened.init()
    assert reopened.get_device("d1")["generation"] == 1
    assert len(reopened.list_reports("d1")) == 1
    assert reopened.get("c1")["boot_generation"] == 1


def test_init_migrates_pre_generation_database(tmp_path):
    """An executions table created before boot generations existed gets the
    column added without losing data."""
    import sqlite3

    db = tmp_path / "gateway.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE executions ("
        "command_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, "
        "state TEXT NOT NULL, receipt_status TEXT, "
        "executions INTEGER NOT NULL DEFAULT 0, received_at TEXT NOT NULL, "
        "executed_at TEXT, drop_receipt INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO executions (command_id, device_id, state, receipt_status, "
        "executions, received_at) VALUES ('old-1', 'd1', 'executed', "
        "'succeeded', 1, 't')"
    )
    conn.commit()
    conn.close()

    s = GatewayStore(db)
    s.init()
    row = s.get("old-1")
    assert row["state"] == "executed"
    assert row["boot_generation"] is None
    # New work on the migrated database gets generations as usual.
    assert execute(s, "c2", "d1")["boot_generation"] == 1
