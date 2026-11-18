"""Operations web: submit reboot commands, watch progress, simulate loss,
and verify whether a device actually came back online afterwards."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import httpx

from .dispatcher import Dispatcher
from .recovery import GatewayUnreachable, compute_conclusion
from .store import DISPLAY, CommandIdConflict, Store

DB_PATH = os.environ.get("OPS_DB", "/data/ops.db")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8001")

store = Store(DB_PATH)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
dispatcher: Dispatcher | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global dispatcher
    store.init()
    dispatcher = Dispatcher(store, GATEWAY_URL)
    dispatcher.start()
    try:
        yield
    finally:
        await dispatcher.stop()


app = FastAPI(title="Reboot Control", lifespan=lifespan)


def row_to_dict(row) -> dict:
    return {
        "command_id": row["command_id"],
        "device_id": row["device_id"],
        "deadline": row["deadline"],
        "state": row["state"],
        "state_label": DISPLAY.get(row["state"], row["state"]),
        "receipt_status": row["receipt_status"],
        "detail": row["detail"],
        "attempts": row["attempts"],
        "receipt_lost": bool(row["receipt_lost"]),
        "boot_generation": row["boot_generation"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ------------------------------------------------------------ gateway client
def _gw_get(path: str) -> dict:
    try:
        resp = httpx.get(f"{GATEWAY_URL.rstrip('/')}{path}", timeout=2.0)
    except httpx.HTTPError as exc:
        raise GatewayUnreachable(str(exc)) from exc
    if resp.status_code == 404:
        return {}
    if resp.status_code != 200:
        raise GatewayUnreachable(f"HTTP {resp.status_code}")
    return resp.json()


def _gw_post(path: str, payload: dict) -> dict:
    try:
        resp = httpx.post(f"{GATEWAY_URL.rstrip('/')}{path}", json=payload, timeout=2.0)
    except httpx.HTTPError as exc:
        raise GatewayUnreachable(str(exc)) from exc
    if resp.status_code >= 400:
        raise GatewayUnreachable(f"HTTP {resp.status_code}")
    return resp.json()


def fetch_reports(device_id: str) -> dict:
    return _gw_get(f"/api/devices/{device_id}/reports")


def fetch_command(command_id: str) -> dict:
    return _gw_get(f"/api/commands/{command_id}")


def recovery_conclusion(device_id: str) -> dict:
    return compute_conclusion(
        device_id,
        store=store,
        fetch_reports=fetch_reports,
        fetch_command=fetch_command,
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    rows = [row_to_dict(r) for r in store.list()]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "rows": rows,
            "counts": store.counts(),
            "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "gateway_url": GATEWAY_URL,
        },
    )


@app.post("/commands")
def submit_form(
    device_id: str = Form(...),
    command_id: str = Form(...),
    deadline_minutes: int = Form(30),
    drop_receipt: bool = Form(False),
):
    device_id = device_id.strip()
    command_id = command_id.strip()
    if not device_id or not command_id:
        raise HTTPException(status_code=400, detail="设备号和指令号不能为空")
    if deadline_minutes < 1 or deadline_minutes > 1440:
        raise HTTPException(status_code=400, detail="截止时间需在 1..1440 分钟之间")
    deadline = datetime.now(timezone.utc) + timedelta(minutes=deadline_minutes)
    try:
        row, created = store.submit(
            device_id=device_id,
            command_id=command_id,
            deadline=deadline,
            drop_receipt=drop_receipt,
        )
    except CommandIdConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Duplicates deliberately redirect to the original record; nothing is
    # re-queued and `created` is False.
    suffix = "" if created else "?duplicate=1"
    return RedirectResponse(url=f"/commands/{row['command_id']}{suffix}", status_code=303)


@app.get("/commands/{command_id}", response_class=HTMLResponse)
def detail(request: Request, command_id: str, duplicate: int = 0):
    row = store.get(command_id)
    if row is None:
        raise HTTPException(status_code=404, detail="指令不存在")
    return templates.TemplateResponse(
        request,
        "detail.html",
        {"cmd": row_to_dict(row), "duplicate": bool(duplicate)},
    )


@app.get("/api/commands/{command_id}")
def api_detail(command_id: str):
    row = store.get(command_id)
    if row is None:
        raise HTTPException(status_code=404, detail="指令不存在")
    return row_to_dict(row)


@app.get("/api/commands")
def api_list():
    return [row_to_dict(r) for r in store.list()]


@app.post("/commands/{command_id}/drop-receipt")
def drop_receipt(command_id: str):
    """Simulate losing the receipt for this command.

    * Not yet delivered: arm the flag; the next delivery asks the gateway to
      execute but swallow its receipt.
    * Already delivered: ask the gateway to drop the response (works while it
      is still inside the simulated device delay), keeping the execution.
    """
    row = store.get(command_id)
    if row is None:
        raise HTTPException(status_code=404, detail="指令不存在")
    if row["state"] not in ("pending", "inflight"):
        raise HTTPException(status_code=409, detail="指令已结案，无法触发丢回执")

    if row["state"] == "inflight":
        try:
            resp = httpx.post(
                f"{GATEWAY_URL.rstrip('/')}/api/commands/{command_id}/drop-receipt",
                timeout=2.0,
            )
            delivered = resp.status_code == 200
        except httpx.HTTPError:
            delivered = False
        if not delivered:
            # The gateway has not seen it yet; next delivery will drop.
            store.arm_drop_receipt(command_id)
    else:
        store.arm_drop_receipt(command_id)
    return RedirectResponse(url=f"/commands/{command_id}", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ------------------------------------------------------- recovery verification
@app.get("/devices/{device_id}", response_class=HTMLResponse)
def device_recovery(request: Request, device_id: str, error: str = ""):
    conclusion = recovery_conclusion(device_id)
    try:
        gw = fetch_reports(device_id)
        gateway_ok = True
    except GatewayUnreachable:
        gw = {"generation": None, "reports": []}
        gateway_ok = False
    reports = sorted(
        gw.get("reports", []),
        key=lambda r: (r["generation"], r["seq"]),
        reverse=True,
    )
    return templates.TemplateResponse(
        request,
        "recovery.html",
        {
            "device_id": device_id,
            "conclusion": conclusion,
            "gateway_ok": gateway_ok,
            "gateway_generation": gw.get("generation"),
            "reports": reports,
            "held": [r for r in reports if not r["arrived"]],
            "error": error,
            "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )


@app.get("/api/devices/{device_id}/recovery")
def api_device_recovery(device_id: str):
    return recovery_conclusion(device_id)


@app.post("/devices/{device_id}/report")
def device_report(
    device_id: str,
    online: bool = Form(...),
    mode: str = Form("current"),
):
    """Ask the gateway's simulated device to emit a report.

    The generation and sequence number are assigned by the gateway; this
    endpoint deliberately accepts no such fields.
    """
    if mode not in ("current", "hold"):
        raise HTTPException(status_code=400, detail="非法的回报方式")
    try:
        _gw_post(
            f"/api/devices/{device_id}/reports",
            {"online": online, "mode": mode},
        )
    except GatewayUnreachable as exc:
        return RedirectResponse(
            url=f"/devices/{device_id}?error={quote(f'网关不可达，回报未记录（{exc}）')}",
            status_code=303,
        )
    return RedirectResponse(url=f"/devices/{device_id}", status_code=303)


@app.post("/devices/{device_id}/release")
def device_release(device_id: str):
    """Release the device's held reports: they arrive late, keeping their
    original gateway-assigned (generation, seq)."""
    try:
        _gw_post(f"/api/devices/{device_id}/reports/release", {})
    except GatewayUnreachable as exc:
        return RedirectResponse(
            url=f"/devices/{device_id}?error={quote(f'网关不可达，无法释放挂起回报（{exc}）')}",
            status_code=303,
        )
    return RedirectResponse(url=f"/devices/{device_id}", status_code=303)
