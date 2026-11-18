"""Recovery conclusion logic: ordering, withdrawal, staleness rules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from web.app.recovery import (
    GatewayUnreachable,
    NOT_RECOVERED,
    NONE,
    RECOVERED,
    VERIFY,
    compute_conclusion,
)
from web.app.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "ops.db")
    s.init()
    return s


def deadline_in(seconds=300):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def submit_executed(store, device_id="d1", command_id="c1", generation=1):
    store.submit(device_id=device_id, command_id=command_id, deadline=deadline_in())
    store.claim_batch(retry_seconds=0)
    store.finish(
        command_id,
        state="executed",
        receipt_status="succeeded",
        boot_generation=generation,
    )


def report(generation, seq, online, arrived=True):
    return {
        "device_id": "d1",
        "generation": generation,
        "seq": seq,
        "online": online,
        "arrived": arrived,
        "emitted_at": "t",
        "arrived_at": "t" if arrived else None,
    }


def fetch_ok(reports, generation=1):
    def _fetch(device_id):
        return {"device_id": device_id, "generation": generation, "reports": reports}

    return _fetch


def fetch_down(device_id):
    raise GatewayUnreachable("down")


def fetch_command_ok(generation=1):
    def _fetch(command_id):
        return {
            "command_id": command_id,
            "state": "executed",
            "status": "succeeded",
            "boot_generation": generation,
        }

    return _fetch


def conclude(store, device_id="d1", reports=None, generation=1):
    return compute_conclusion(
        device_id,
        store=store,
        fetch_reports=fetch_ok(reports or [], generation),
        fetch_command=fetch_command_ok(generation),
    )


def test_no_commands_is_none(store):
    assert conclude(store)["conclusion"] == NONE


def test_pending_command_is_verify(store):
    store.submit(device_id="d1", command_id="c1", deadline=deadline_in())
    r = conclude(store)
    assert r["conclusion"] == VERIFY
    assert r["command_id"] == "c1"


def test_inflight_command_is_verify(store):
    store.submit(device_id="d1", command_id="c1", deadline=deadline_in())
    store.claim_batch(retry_seconds=0)
    assert conclude(store)["conclusion"] == VERIFY


@pytest.mark.parametrize("state", ["failed", "unexecuted", "rejected"])
def test_negative_terminal_states_are_not_recovered(store, state):
    store.submit(device_id="d1", command_id="c1", deadline=deadline_in())
    store.claim_batch(retry_seconds=0)
    store.finish("c1", state=state)
    r = conclude(store, reports=[report(1, 1, True)])
    # Even an online report cannot recover a command that never ran.
    assert r["conclusion"] == NOT_RECOVERED


def test_executed_and_new_generation_online_is_recovered(store):
    submit_executed(store, generation=1)
    r = conclude(store, reports=[report(1, 1, True)])
    assert r["conclusion"] == RECOVERED
    assert r["boot_generation"] == 1
    assert r["latest_report"]["seq"] == 1


def test_executed_without_any_report_is_verify(store):
    submit_executed(store, generation=1)
    assert conclude(store, reports=[])["conclusion"] == VERIFY


def test_old_generation_report_is_not_evidence(store):
    submit_executed(store, generation=2)
    # A late "online" from the previous boot must not count.
    r = conclude(store, reports=[report(1, 7, True)], generation=2)
    assert r["conclusion"] == VERIFY


def test_newer_offline_report_withdraws_recovery(store):
    submit_executed(store, generation=1)
    reports = [report(1, 1, True), report(1, 2, False)]
    r = conclude(store, reports=reports)
    assert r["conclusion"] == NOT_RECOVERED
    assert r["latest_report"]["seq"] == 2


def test_recovery_can_return_with_newer_online(store):
    submit_executed(store, generation=1)
    reports = [report(1, 1, True), report(1, 2, False), report(1, 3, True)]
    assert conclude(store, reports=reports)["conclusion"] == RECOVERED


def test_late_old_seq_cannot_override_newer_report(store):
    submit_executed(store, generation=1)
    # Arrival order: seq 2 (online) first, then the late seq 1 (offline).
    reports = [report(1, 2, True), report(1, 1, False)]
    r = conclude(store, reports=reports)
    assert r["conclusion"] == RECOVERED
    assert r["latest_report"]["seq"] == 2


def test_held_report_is_not_evidence(store):
    submit_executed(store, generation=1)
    r = conclude(store, reports=[report(1, 1, True, arrived=False)])
    assert r["conclusion"] == VERIFY


def test_gateway_unreachable_is_verify(store):
    submit_executed(store, generation=1)
    r = compute_conclusion(
        "d1",
        store=store,
        fetch_reports=fetch_down,
        fetch_command=fetch_command_ok(),
    )
    assert r["conclusion"] == VERIFY
    assert "不可达" in r["reason"]


def test_new_command_supersedes_old_conclusion(store):
    submit_executed(store, "d1", "c1", generation=1)
    assert conclude(store, reports=[report(1, 1, True)])["conclusion"] == RECOVERED
    # A newer command exists now: the old "recovered" must not be reused.
    store.submit(device_id="d1", command_id="c2", deadline=deadline_in())
    r = conclude(store, reports=[report(1, 1, True)])
    assert r["conclusion"] == VERIFY
    assert r["command_id"] == "c2"


def test_command_changing_mid_check_delivers_no_stale_conclusion(store):
    submit_executed(store, "d1", "c1", generation=1)

    def sneaky_fetch(device_id):
        # While we query the gateway, a new command gets submitted.
        store.submit(device_id="d1", command_id="c2", deadline=deadline_in())
        return {"device_id": device_id, "generation": 1, "reports": [report(1, 1, True)]}

    r = compute_conclusion(
        "d1",
        store=store,
        fetch_reports=sneaky_fetch,
        fetch_command=fetch_command_ok(),
    )
    assert r["conclusion"] == VERIFY
    assert "变化" in r["reason"]


def test_missing_generation_is_backfilled_from_gateway(store):
    store.submit(device_id="d1", command_id="c1", deadline=deadline_in())
    store.claim_batch(retry_seconds=0)
    store.finish("c1", state="executed", receipt_status="succeeded")
    assert store.get("c1")["boot_generation"] is None  # legacy record

    r = conclude(store, reports=[report(3, 1, True)], generation=3)
    assert r["conclusion"] == RECOVERED
    assert r["boot_generation"] == 3
    assert store.get("c1")["boot_generation"] == 3  # persisted


def test_missing_generation_and_gateway_down_is_verify(store):
    store.submit(device_id="d1", command_id="c1", deadline=deadline_in())
    store.claim_batch(retry_seconds=0)
    store.finish("c1", state="executed", receipt_status="succeeded")
    r = compute_conclusion(
        "d1", store=store, fetch_reports=fetch_ok([]), fetch_command=fetch_down
    )
    assert r["conclusion"] == VERIFY
