"""Transfer worker — one Container Apps Job execution handles one queued shard.

queue message  ->  verify requester holds an *activated* transfer-operator
                   role for this very ticket (PIM)
               ->  AzCopy server-to-server copy (source -> landing)
               ->  verify (name + size + Content-MD5 of every object)
               ->  write a provenance manifest next to the data
               ->  ledger events at every step, delete message
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

from azure.storage.blob import ContainerClient

from . import pim
from .config import settings
from .events import Ledger, LedgerEvent, event_id, iso, utcnow
from .identity import credential

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # keep in sync with the queue's max_delivery_count
LOCK_RENEWAL_SECONDS = 24 * 3600 + 600


class RetryableError(RuntimeError):
    """Transient failure — the message is released for another attempt."""


@dataclass
class TransferRequest:
    requestId: str
    ticketId: str
    requesterId: str
    requesterName: str
    srcPrefix: str
    dstPrefix: str
    submittedAt: str
    verify: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: str) -> TransferRequest:
        d = json.loads(raw)
        known = {k: d.pop(k) for k in list(d) if k in cls.__dataclass_fields__ and k != "extra"}
        return cls(**known, extra=d)

    @property
    def route(self) -> str:
        s = settings()
        return f"source/{s.source_container}/{self.srcPrefix} -> landing/{s.landing_container}/{self.dstPrefix}"


def _norm_prefix(p: str) -> str:
    p = p.strip().lstrip("/")
    return p if not p or p.endswith("/") else p + "/"


def _ev(req: TransferRequest, etype: str, stage: str, status: str, msg: str, key: str = "", **details) -> LedgerEvent:
    s = settings()
    return LedgerEvent(
        EventType=etype,
        Stage=stage,
        TicketId=req.ticketId,
        TimeGenerated=iso(utcnow()),
        EventId=event_id(req.requestId, etype, key),
        ActorId=s.worker_identity,
        ActorName="transfer-worker",
        ActorType="ManagedIdentity",
        Role="transfer-operator",
        Scope=s.ctl_scope,
        Route=req.route,
        Status=status,
        Message=msg,
        Details={
            "requestId": req.requestId,
            "requesterId": req.requesterId,
            "requester": req.requesterName,
            "execution": os.environ.get("CONTAINER_APP_JOB_EXECUTION_NAME", "local"),
            **details,
        },
    )


def run_azcopy(src: str, dst: str) -> dict[str, Any]:
    cmd = [
        "azcopy",
        "copy",
        src,
        dst,
        "--recursive",
        "--overwrite=ifSourceNewer",  # idempotent: a retried shard skips what already landed
        "--s2s-preserve-properties=true",  # keeps Content-MD5 for end-to-end verification
        "--s2s-preserve-access-tier=false",
        "--check-length=true",
        "--output-type=json",
        "--log-level=WARNING",
    ]
    log.info("exec: %s", " ".join(cmd))
    summary: dict[str, Any] = {}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout
    for line in proc.stdout:
        line = line.strip()
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log.info("azcopy: %s", line)
            continue
        mtype = msg.get("MessageType")
        content = msg.get("MessageContent", "")
        if mtype in ("Progress", "EndOfJob"):
            try:
                summary = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                pass
            if mtype == "Progress":
                log.info(
                    "progress: %s/%s files, %s bytes",
                    summary.get("TransfersCompleted"),
                    summary.get("TotalTransfers"),
                    summary.get("TotalBytesTransferred"),
                )
        elif mtype in ("Error", "Info", "Init"):
            log.info("azcopy %s: %s", mtype, content)
    summary["exitCode"] = proc.wait()
    return summary


def verify(req: TransferRequest) -> dict[str, Any]:
    s = settings()
    src = ContainerClient.from_container_url(f"{s.source_account_url}/{s.source_container}", credential=credential())
    dst = ContainerClient.from_container_url(f"{s.landing_account_url}/{s.landing_container}", credential=credential())

    def index(cc: ContainerClient, prefix: str) -> dict[str, tuple[int, str]]:
        out = {}
        for b in cc.list_blobs(name_starts_with=prefix or None):
            md5 = b.content_settings.content_md5
            out[b.name[len(prefix) :]] = (b.size, bytes(md5).hex() if md5 else "")
        return out

    a, b = index(src, req.srcPrefix), index(dst, req.dstPrefix)
    missing = sorted(set(a) - set(b))
    mismatched = sorted(k for k in set(a) & set(b) if a[k][0] != b[k][0] or (a[k][1] and a[k][1] != b[k][1]))
    no_md5 = sum(1 for v in a.values() if not v[1])
    return {
        "objects": len(a),
        "bytes": sum(v[0] for v in a.values()),
        "missing": missing[:50],
        "missingCount": len(missing),
        "mismatched": mismatched[:50],
        "mismatchedCount": len(mismatched),
        "objectsWithoutMd5": no_md5,
        "manifest": {k: {"size": v[0], "md5": v[1]} for k, v in a.items()},
    }


def write_manifest(req: TransferRequest, result: dict[str, Any], azcopy: dict[str, Any]) -> str:
    s = settings()
    dst = ContainerClient.from_container_url(f"{s.landing_account_url}/{s.landing_container}", credential=credential())
    name = f"_manifests/{req.ticketId}/{req.requestId}.json"
    doc = {
        "ticketId": req.ticketId,
        "requestId": req.requestId,
        "requester": {"id": req.requesterId, "name": req.requesterName},
        "route": req.route,
        "completedAt": iso(utcnow()),
        "azcopy": {
            k: azcopy.get(k)
            for k in (
                "JobID",
                "TotalBytesTransferred",
                "TransfersCompleted",
                "TransfersFailed",
                "TransfersSkipped",
                "JobStatus",
            )
        },
        "objects": result["manifest"],
    }
    dst.upload_blob(name, json.dumps(doc, indent=1).encode(), overwrite=True)
    return name


def process(req: TransferRequest, ledger: Ledger, attempt: int = 1) -> bool:
    s = settings()
    req.srcPrefix, req.dstPrefix = _norm_prefix(req.srcPrefix), _norm_prefix(req.dstPrefix or req.srcPrefix)

    # 1) authorization: requester must have an activated, unexpired operator role for THIS ticket.
    role_guid = s.operator_role_id.rsplit("/", 1)[-1]
    activation = pim.active_activation_for_ticket(s.ctl_scope, req.requesterId, role_guid, req.ticketId)
    if not activation:
        ledger.emit(
            _ev(
                req,
                "transfer.rejected",
                "operate",
                "Rejected",
                f"{req.requesterName} has no active transfer-operator activation for ticket {req.ticketId}",
            )
        )
        return True  # handled: a rejection is a business outcome, not a job failure
    ledger.emit(
        _ev(
            req,
            "transfer.started",
            "operate",
            "Running",
            f"copy {req.route}",
            key=str(attempt),
            activationRequestId=activation["name"],
        )
    )

    # 2) copy
    src = f"{s.source_account_url}/{s.source_container}/{req.srcPrefix}*"
    dst = f"{s.landing_account_url}/{s.landing_container}/{req.dstPrefix}"
    summary = run_azcopy(src, dst)
    stats = {
        k: summary.get(k)
        for k in (
            "JobID",
            "JobStatus",
            "TotalTransfers",
            "TransfersCompleted",
            "TransfersFailed",
            "TransfersSkipped",
            "TotalBytesTransferred",
            "TotalBytesEnumerated",
            "exitCode",
        )
    }
    stats["bytesTransferred"] = int(summary.get("TotalBytesTransferred") or 0)
    if summary["exitCode"] != 0 or summary.get("JobStatus") not in ("Completed", "CompletedWithSkipped"):
        ledger.emit(
            _ev(
                req,
                "transfer.failed",
                "operate",
                summary.get("JobStatus", "Failed"),
                f"azcopy exit={summary['exitCode']} status={summary.get('JobStatus')} (attempt {attempt})",
                key=str(attempt),
                **stats,
            )
        )
        raise RetryableError("azcopy failed")

    # 3) verify
    if req.verify:
        v = verify(req)
        manifest = write_manifest(req, v, summary)
        brief = {k: v[k] for k in v if k != "manifest"} | {"manifestBlob": manifest}
        ok = v["missingCount"] == 0 and v["mismatchedCount"] == 0
        ledger.emit(
            _ev(
                req,
                "verify.passed" if ok else "verify.failed",
                "operate",
                "Passed" if ok else "Failed",
                f"{v['objects']} objects / {v['bytes']} bytes, missing={v['missingCount']} "
                f"mismatched={v['mismatchedCount']}",
                **brief,
            )
        )
        if not ok:
            return False

    ledger.emit(
        _ev(
            req,
            "transfer.completed",
            "operate",
            "Completed",
            f"{stats['TransfersCompleted']} files, {stats['bytesTransferred']} bytes "
            f"(skipped {stats['TransfersSkipped']})",
            **stats,
        )
    )
    return True


def main() -> int:
    from azure.servicebus import AutoLockRenewer, ServiceBusClient

    s = settings()
    with (
        ServiceBusClient(s.bus_namespace, credential=credential()) as sb,
        AutoLockRenewer(max_lock_renewal_duration=LOCK_RENEWAL_SECONDS) as renewer,
        sb.get_queue_receiver(s.bus_queue, max_wait_time=15, auto_lock_renewer=renewer) as rx,
    ):
        msgs = rx.receive_messages(max_message_count=1, max_wait_time=15)
        if not msgs:
            log.info("queue empty — nothing to do")
            return 0
        msg = msgs[0]
        attempt = msg.delivery_count + 1  # delivery_count counts *previous* deliveries
        ledger = Ledger("transfer-worker")
        try:
            req = TransferRequest.parse(str(msg))
        except Exception as e:  # malformed request: park it, never retry
            rx.dead_letter_message(msg, reason="malformed", error_description=str(e)[:1000])
            log.error("malformed message %s: %s", msg.message_id, e)
            return 1

        try:
            ok = process(req, ledger, attempt=attempt)
        except Exception as e:
            if not isinstance(e, RetryableError):
                log.exception("transfer crashed")
                ledger.emit(
                    _ev(req, "transfer.failed", "operate", "Error", f"{type(e).__name__}: {e}", key=str(attempt))
                )
            if attempt >= MAX_ATTEMPTS:
                ledger.emit(
                    _ev(
                        req,
                        "transfer.failed",
                        "operate",
                        "DeadLettered",
                        f"gave up after {attempt} attempts",
                        key="final",
                    )
                )
                rx.dead_letter_message(msg, reason="max-attempts", error_description=str(e)[:1000])
            else:
                # Back on the queue for another execution (AzCopy is idempotent with ifSourceNewer).
                rx.abandon_message(msg)
            return 1
        rx.complete_message(msg)
        return 0 if ok else 2
