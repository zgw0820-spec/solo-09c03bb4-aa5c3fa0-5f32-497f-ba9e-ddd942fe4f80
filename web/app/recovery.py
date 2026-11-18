"""Recovery verification for a device's latest reboot command.

The conclusion is *computed on demand* from two sources and never cached:

* the operations DB: the device's most recently submitted command and its
  verified execution outcome (including the boot generation the gateway
  issued for that execution);
* the gateway, queried live: the device's report log.  Only reports that
  the gateway itself stamped with a generation and a per-generation
  sequence number count as evidence -- nothing typed into a web page is
  ever evidence.

Conclusions
-----------
recovered       已恢复  command verified executed successfully AND the newest
                        report *of the new boot generation* is "online".
not_recovered   未恢复  the command verifiably did not run / failed / was
                        rejected, or the newest report of the new generation
                        is "offline" (a newer offline report withdraws an
                        earlier "recovered").
verify          待核实  outcome unknown so far, gateway unreachable, no
                        report of the new generation yet, or the command
                        changed while we were checking.
none            暂无指令  the device has no commands at all.

Ordering rules
--------------
Only reports with ``generation == boot_generation_of_the_command`` are
considered; among them the one with the highest ``seq`` decides.  A late
report from an old generation, or with an old sequence number, can
therefore never override a newer one -- and a "recovered" conclusion is
withdrawn as soon as a *newer* offline report exists.
"""
from __future__ import annotations

from typing import Callable, Optional

RECOVERED = "recovered"
NOT_RECOVERED = "not_recovered"
VERIFY = "verify"
NONE = "none"

LABELS = {
    RECOVERED: "已恢复",
    NOT_RECOVERED: "未恢复",
    VERIFY: "待核实",
    NONE: "暂无指令",
}

COMMAND_STATE_LABELS = {
    "pending": "等待下发",
    "inflight": "已发出，等待回执",
    "executed": "执行成功",
    "failed": "执行失败",
    "unexecuted": "未执行（已过期）",
    "rejected": "网关拒绝（指令号冲突）",
}


class GatewayUnreachable(Exception):
    """The gateway could not be queried; nothing can be concluded."""


# fetch_reports(device_id) -> {"generation": int, "reports": [ {...} ]}
# fetch_command(command_id) -> gateway execution record dict
FetchReports = Callable[[str], dict]
FetchCommand = Callable[[str], dict]


def _result(
    conclusion: str,
    reason: str,
    *,
    command=None,
    boot_generation: Optional[int] = None,
    gateway_generation: Optional[int] = None,
    latest_report: Optional[dict] = None,
) -> dict:
    return {
        "conclusion": conclusion,
        "label": LABELS[conclusion],
        "reason": reason,
        "command_id": command["command_id"] if command is not None else None,
        "command_state": command["state"] if command is not None else None,
        "command_state_label": (
            COMMAND_STATE_LABELS.get(command["state"], command["state"])
            if command is not None
            else None
        ),
        "boot_generation": boot_generation,
        "gateway_generation": gateway_generation,
        "latest_report": latest_report,
    }


def compute_conclusion(
    device_id: str,
    *,
    store,
    fetch_reports: FetchReports,
    fetch_command: FetchCommand,
) -> dict:
    """Recovery conclusion for ``device_id``'s latest command.

    ``store`` is the operations Store; ``fetch_reports`` / ``fetch_command``
    query the gateway and raise :class:`GatewayUnreachable` on failure.
    """
    cmd = store.latest_command_for_device(device_id)
    if cmd is None:
        return _result(NONE, "该设备还没有重启指令")

    state = cmd["state"]
    cid = cmd["command_id"]

    # Verifiably not executed / failed: never "recovered", regardless of
    # whatever the device reports (it never rebooted for this command).
    if state == "unexecuted":
        return _result(NOT_RECOVERED, f"指令 {cid} 已过截止时间，明确未执行", command=cmd)
    if state == "failed":
        return _result(NOT_RECOVERED, f"指令 {cid} 已确认执行失败", command=cmd)
    if state == "rejected":
        return _result(NOT_RECOVERED, f"指令 {cid} 被网关拒绝，未执行", command=cmd)

    # Outcome not yet known: no conclusion can be delivered.
    if state in ("pending", "inflight"):
        return _result(
            VERIFY,
            f"指令 {cid} 尚未确认执行成功（{COMMAND_STATE_LABELS.get(state, state)}），"
            "恢复情况待核实",
            command=cmd,
        )

    # state == "executed": the command ran.  Recovery additionally requires
    # the newest report *of the boot generation this execution issued* to be
    # "online".
    generation = cmd["boot_generation"]
    if generation is None:
        # Record predates boot generations (or the receipt lacked it):
        # backfill from the gateway's authoritative execution record.
        try:
            gw = fetch_command(cid)
        except GatewayUnreachable:
            return _result(
                VERIFY, "网关不可达，无法核实该指令的启动代次", command=cmd
            )
        gw_gen = gw.get("boot_generation") if gw else None
        if gw and gw.get("state") == "executed" and gw_gen is not None:
            store.set_boot_generation(cid, int(gw_gen))
            generation = int(gw_gen)
        else:
            return _result(
                VERIFY,
                "网关没有该指令的启动代次记录，恢复情况待核实",
                command=cmd,
            )

    try:
        data = fetch_reports(device_id)
    except GatewayUnreachable:
        return _result(
            VERIFY,
            "网关不可达，无法获取设备回报，恢复情况待核实",
            command=cmd,
            boot_generation=generation,
        )

    # The command must not have changed while we were querying the gateway;
    # otherwise this conclusion would already be stale.
    cmd2 = store.latest_command_for_device(device_id)
    if (
        cmd2 is None
        or cmd2["command_id"] != cid
        or cmd2["state"] != "executed"
        or cmd2["boot_generation"] != generation
    ):
        return _result(
            VERIFY,
            "核查期间指令状态发生变化，本次结论作废，稍候自动重核",
            command=cmd2 if cmd2 is not None else cmd,
        )

    gateway_generation = data.get("generation")
    reports = [
        r
        for r in data.get("reports", [])
        if r.get("arrived") and r.get("generation") == generation
    ]
    if not reports:
        return _result(
            VERIFY,
            f"指令已确认执行成功（新启动代次 {generation}），"
            "但新代次尚无设备回报，恢复情况待核实",
            command=cmd,
            boot_generation=generation,
            gateway_generation=gateway_generation,
        )

    latest = max(reports, key=lambda r: r["seq"])
    if latest["online"]:
        return _result(
            RECOVERED,
            f"指令已确认执行成功，新代次 {generation} 最新回报"
            f"（序号 {latest['seq']}）为在线",
            command=cmd,
            boot_generation=generation,
            gateway_generation=gateway_generation,
            latest_report=latest,
        )
    return _result(
        NOT_RECOVERED,
        f"指令已确认执行成功，但新代次 {generation} 最新回报"
        f"（序号 {latest['seq']}）为离线",
        command=cmd,
        boot_generation=generation,
        gateway_generation=gateway_generation,
        latest_report=latest,
    )
