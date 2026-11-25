"""Ticket connector — an always-on Container App, no ingress.

thread A  Feishu long connection (outbound websocket). An approval-instance event is only
          *recorded*: it is put on the `ticket-events` queue and acknowledged (Feishu wants an
          answer within 3 s). If the queue cannot be reached the event is not acknowledged and
          Feishu redelivers it.
thread B  Intake: consumes `ticket-events`, re-reads the instance through the API (the event is
          never trusted), re-checks it against the Azure-side registries and the IT approver list,
          writes the ledger, and queues exactly one transfer shard per approved ticket.

The collector reconciles against the instance list every few minutes, so a missed event
(connector down, websocket gap) is still picked up.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from . import tickets as tk
from .config import Settings, settings
from .events import Ledger, LedgerEvent, event_id, iso, utcnow

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------- events --


def ticket_event(
    t: tk.Ticket,
    etype: str,
    stage: str,
    status: str,
    message: str,
    *,
    actor_id: str = "",
    actor_name: str = "",
    actor_type: str = "TicketUser",
    key: str = "",
    **details: Any,
) -> LedgerEvent:
    return LedgerEvent(
        EventType=etype,
        Stage=stage,
        TicketId=t.instance_code,
        TimeGenerated=iso(utcnow()),
        EventId=event_id(t.instance_code, etype, key or t.status),
        ActorId=actor_id,
        ActorName=actor_name,
        ActorType=actor_type,
        Role="ticket",
        Scope=t.approval_code,
        Route=f"{t.dataset} -> {t.destination}" if t.dataset or t.destination else "",
        Status=status,
        Message=message,
        Details={
            "serial": t.serial,
            "dataset": t.dataset,
            "destination": t.destination,
            "purpose": t.purpose,
            "ticketStatus": t.status,
            **details,
        },
    )


class SnapshotStore:
    """Raw instance JSON into the WORM archive (new blob per snapshot; the container is immutable)."""

    def __init__(self, container_url: str) -> None:
        from azure.storage.blob import ContainerClient

        from .identity import credential

        self.cc = ContainerClient.from_container_url(container_url, credential=credential())

    def put(self, instance_code: str, doc: dict[str, Any]) -> tuple[str, str]:
        from azure.core.exceptions import ResourceExistsError

        body = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode()
        sha = hashlib.sha256(body).hexdigest()
        name = f"tickets/{instance_code}/{sha[:16]}.json"
        try:
            self.cc.get_blob_client(name).upload_blob(body, overwrite=False)
        except ResourceExistsError:
            pass  # identical snapshot already archived
        return name, sha


# ---------------------------------------------------------------------- intake --


def _ms_iso(v: Any) -> str:
    from datetime import UTC, datetime

    try:
        ms = int(v)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat() if ms > 0 else ""


def registry_lookup() -> Callable[[str], dict[str, Any] | None]:
    """Destination lookup from the registry's *tags* (ARM listing) — the connector never reads
    destination credentials; only the worker does."""
    from . import destinations as dst

    cache: dict[str, dict[str, Any]] | None = None

    def lookup(name: str) -> dict[str, Any] | None:
        nonlocal cache
        if cache is None:
            cache = {d["name"]: d for d in dst.list_all()}
        d = cache.get(name)
        return {"enabled": d["enabled"], "root": d.get("root", "")} if d else None

    return lookup


def handle(
    instance_code: str,
    *,
    fs,
    ledger,
    markers,
    send_transfer: Callable[[dict[str, Any], str], None],
    snapshots=None,
    s: Settings | None = None,
    destination: Callable[[str], dict[str, Any] | None] | None = None,
) -> str:
    """Process the *current* state of one instance. Idempotent and serialized per instance
    (several connector replicas may briefly run side by side during a rollout)."""
    with markers.lock(f"intake/{instance_code}"):
        return _handle(
            instance_code,
            fs=fs,
            ledger=ledger,
            markers=markers,
            send_transfer=send_transfer,
            snapshots=snapshots,
            s=s,
            destination=destination,
        )


def _handle(
    instance_code: str,
    *,
    fs,
    ledger,
    markers,
    send_transfer: Callable[[dict[str, Any], str], None],
    snapshots=None,
    s: Settings | None = None,
    destination: Callable[[str], dict[str, Any] | None] | None = None,
) -> str:
    from .feishu import EmailCache

    s = s or settings()
    destination = destination or registry_lookup()
    doc = fs.get_instance(instance_code)
    source_missing = doc is None  # the record was removed from the ticketing system
    if doc is None:
        doc = {"instance_code": instance_code, "status": "DELETED", "approval_code": s.feishu_approval_code}
    if doc.get("approval_code") and doc["approval_code"] != s.feishu_approval_code:
        log.info("ignore %s: other approval definition %s", instance_code, doc["approval_code"])
        return "ignored"
    t = tk.parse_instance(doc, s.feishu_fields)
    t.instance_code = t.instance_code or instance_code
    key = f"intake/{instance_code}/{t.status}"
    if markers.exists(key):
        return "duplicate"
    emails = EmailCache(fs)
    applicant = {"actor_id": t.applicant, "actor_name": emails(t.applicant) if t.applicant else ""}
    outcome = t.status.lower()

    submitted_key = f"intake/{instance_code}/{tk.PENDING}"
    if t.status == tk.PENDING or (not source_missing and not markers.exists(submitted_key)):
        # Approved within seconds, the ticket may already be past PENDING when it is first handled:
        # the submission is still recorded (at the ticket's real creation time) so the lifecycle is complete.
        ev = ticket_event(
            t, "ticket.submitted", "request", "Submitted", f"ticket {t.serial} submitted", key=tk.PENDING, **applicant
        )
        created = _ms_iso(doc.get("start_time"))
        if created:
            ev.TimeGenerated = created
        ledger.emit(ev)
        if t.status != tk.PENDING:
            markers.put(
                submitted_key, {"at": iso(utcnow()), "outcome": "pending", "approvalCode": s.feishu_approval_code}
            )

    if t.status == tk.PENDING:
        pass  # the submission was recorded above

    elif t.status == tk.APPROVED:
        if snapshots is not None:
            blob, sha = snapshots.put(instance_code, doc)
            ledger.emit(
                ticket_event(
                    t,
                    "ticket.snapshot",
                    "approve",
                    "Archived",
                    f"instance snapshot archived ({sha[:12]}…)",
                    actor_type="System",
                    actor_name="connector",
                    snapshotBlob=blob,
                    snapshotSha256=sha,
                )
            )
        d = tk.evaluate(
            t,
            approver_emails=set(s.approver_emails),
            email_of=emails,
            datasets=s.datasets,
            destination=destination,
        )
        if not d.ok:
            ledger.emit(
                ticket_event(
                    t,
                    "transfer.rejected",
                    "approve",
                    "Rejected",
                    d.reason,
                    actor_type="System",
                    actor_name="connector",
                    approvers=t.approvers,
                )
            )
            fs.notify(t.applicant, f"数据回传申请 {t.serial} 未执行：{d.reason}")
            outcome = "rejected"
        else:
            rid = tk.request_id(t)
            ledger.emit(
                ticket_event(
                    t,
                    "ticket.approved",
                    "approve",
                    "Approved",
                    f"approved by IT ({d.approver_email})",
                    actor_id=d.approver,
                    actor_name=d.approver_email,
                    actor_type="TicketUser",
                )
            )
            send_transfer({"kind": "ticket", "instanceCode": instance_code}, rid)
            ledger.emit(
                ticket_event(
                    t,
                    "transfer.queued",
                    "operate",
                    "Queued",
                    f"shard {rid} queued",
                    actor_type="System",
                    actor_name="connector",
                    requestId=rid,
                )
            )
            fs.notify(t.applicant, f"数据回传申请 {t.serial} 已审批，开始传输：{t.dataset} → {t.destination}")
            outcome = "queued"

    elif t.status == tk.REJECTED:
        rejecter = next((x.get("open_id", "") for x in doc.get("task_list") or [] if x.get("status") == "REJECTED"), "")
        ledger.emit(
            ticket_event(
                t,
                "ticket.rejected",
                "approve",
                "Rejected",
                f"ticket {t.serial} rejected",
                actor_id=rejecter,
                actor_name=emails(rejecter) if rejecter else "",
            )
        )

    elif t.status in tk.WITHDRAWN and source_missing:
        started = markers.exists(f"transfer/{instance_code}/started")
        ledger.emit(
            ticket_event(
                t,
                "ticket.source.deleted",
                "close",
                "Anomaly" if started else "Warning",
                "the ticket no longer exists in the ticketing system"
                + (" — its transfer had already started" if started else ""),
                actor_type="System",
                actor_name="connector",
                transferStarted=started,
            )
        )
        outcome = "source-deleted"
    elif t.status in tk.WITHDRAWN:
        if markers.exists(f"transfer/{instance_code}/started"):
            ledger.emit(
                ticket_event(
                    t,
                    "ticket.withdrawn.after-start",
                    "close",
                    "Anomaly",
                    f"ticket became {t.status} after the transfer started",
                    **applicant,
                )
            )
            outcome = "after-start"
        else:
            ledger.emit(ticket_event(t, "ticket.withdrawn", "close", "Withdrawn", f"ticket {t.status}", **applicant))
    else:
        log.info("instance %s status %s: nothing to do", instance_code, t.status)
        return "noop"

    markers.put(key, {"at": iso(utcnow()), "outcome": outcome, "approvalCode": s.feishu_approval_code})
    return outcome


# ------------------------------------------------------------------- threads --


def event_message(ev: dict[str, Any], envelope_uuid: str | None, approval_code: str) -> tuple[dict, str] | None:
    """Queue message for an approval-instance event of our definition (None = not ours)."""
    if ev.get("type") != "approval_instance" or ev.get("approval_code") != approval_code:
        return None
    code, status = ev.get("instance_code", ""), ev.get("status", "")
    if not code:
        return None
    mid = envelope_uuid or f"{code}-{status}-{ev.get('operate_time', '')}"
    return {"instanceCode": code, "status": status, "eventId": mid, "source": "event"}, mid


def _intake_loop(stop: threading.Event) -> None:
    from azure.servicebus import ServiceBusClient, ServiceBusMessage

    from .feishu import client as feishu_client
    from .identity import credential
    from .markers import Markers

    s = settings()
    ledger, markers = Ledger("connector"), Markers()
    snapshots = SnapshotStore(s.audit_container_url) if s.audit_container_url else None
    while not stop.is_set():
        try:
            with (
                ServiceBusClient(s.bus_namespace, credential=credential()) as sb,
                sb.get_queue_receiver(s.ticket_queue, max_wait_time=30) as rx,
                sb.get_queue_sender(s.bus_queue) as tx,
            ):

                def send_transfer(body: dict, message_id: str) -> None:
                    tx.send_messages(
                        ServiceBusMessage(json.dumps(body), message_id=message_id, subject=body["instanceCode"])
                    )

                while not stop.is_set():
                    for msg in rx.receive_messages(max_message_count=10, max_wait_time=30):
                        try:
                            code = json.loads(str(msg))["instanceCode"]
                            res = handle(
                                code,
                                fs=feishu_client(),
                                ledger=ledger,
                                markers=markers,
                                send_transfer=send_transfer,
                                snapshots=snapshots,
                                s=s,
                            )
                            log.info("intake %s -> %s", code, res)
                            rx.complete_message(msg)
                        except Exception:
                            log.exception("intake failed (delivery %s)", msg.delivery_count)
                            rx.abandon_message(msg)
        except Exception:
            log.exception("intake loop error — reconnecting in 10s")
            time.sleep(10)


def main() -> int:
    import lark_oapi as lark
    from azure.servicebus import ServiceBusClient, ServiceBusMessage

    from .feishu import client as feishu_client
    from .identity import credential

    s = settings()
    while True:
        try:
            fs = feishu_client()
            fs.subscribe(s.feishu_approval_code)
            break
        except Exception as e:  # secret not set yet / wrong secret: keep the container up, retry
            log.warning("Feishu client not ready (%s) — retrying in 60s", e)
            feishu_client.cache_clear()
            time.sleep(60)

    stop = threading.Event()
    threading.Thread(target=_intake_loop, args=(stop,), daemon=True, name="intake").start()

    sb = ServiceBusClient(s.bus_namespace, credential=credential())
    sender = sb.get_queue_sender(s.ticket_queue)
    lock = threading.Lock()

    def on_instance(data) -> None:
        m = event_message(data.event or {}, getattr(data, "uuid", None), s.feishu_approval_code)
        if not m:
            return
        body, mid = m
        with lock:  # raises -> not acknowledged -> Feishu redelivers
            sender.send_messages(ServiceBusMessage(json.dumps(body), message_id=mid, subject=body["instanceCode"]))
        log.info("event %s %s queued", body["instanceCode"], body["status"])

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p1_customized_event("approval_instance", on_instance)
        .build()
    )
    log.info("connecting to Feishu (long connection) for approval %s", s.feishu_approval_code)
    lark.ws.Client(
        fs.app_id, fs._secret, event_handler=handler, domain=s.feishu_domain, log_level=lark.LogLevel.INFO
    ).start()  # blocks; reconnects automatically
    return 0
