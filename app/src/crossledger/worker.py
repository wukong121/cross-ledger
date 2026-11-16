"""Managed transfer worker — one Container Apps Job execution handles one queued shard.

queue message  ->  verify the requester holds an *activated* transfer-operator
                   role for this very ticket (PIM)
               ->  read destination credentials from Key Vault (managed identity)
               ->  rclone copy  source (Azure Blob, managed identity, private endpoint)
                                -> destination (S3-compatible object storage)
               ->  rclone check (size + hash of every object) and a provenance
                   manifest written next to the data
               ->  ledger events at every step, then complete the message
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any

from azure.storage.blob import ContainerClient

from . import pim
from .config import settings
from .events import Ledger, LedgerEvent, event_id, iso, utcnow
from .identity import credential

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # keep in sync with the queue's max_delivery_count
SYSTEM_CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")
LOCK_RENEWAL_SECONDS = 24 * 3600 + 600


class RetryableError(RuntimeError):
    """Transient failure — the message is released for another attempt."""


@dataclass
class Destination:
    """A registered destination: rclone remote options + allowed scope (Key Vault secret ``dest-<name>``)."""

    name: str
    allow: list[str]
    env: dict[str, str]
    ca_cert_file: str | None = None

    @classmethod
    def from_key_vault(cls, name: str) -> Destination:
        from azure.core.exceptions import ResourceNotFoundError
        from azure.keyvault.secrets import SecretClient

        from .destinations import NAME_RE, SECRET_PREFIX, DestinationError

        if not NAME_RE.match(name):
            raise DestinationError(f"invalid destination name {name!r}")
        try:
            sec = SecretClient(settings().key_vault_uri, credential()).get_secret(f"{SECRET_PREFIX}{name}")
        except ResourceNotFoundError as e:
            raise DestinationError(f"destination '{name}' is not registered") from e
        if sec.properties.enabled is False:
            raise DestinationError(f"destination '{name}' is retired")
        return cls.from_dict(name, json.loads(sec.value))

    @classmethod
    def from_dict(cls, name: str, doc: dict[str, Any]) -> Destination:
        ca_pem = doc.get("ca_cert_pem")
        ca_file = None
        if ca_pem:
            # rclone's --ca-cert REPLACES the trust store for every TLS connection (including the
            # Azure source), so trust the system roots *plus* the destination's private CA.
            system_roots = ""
            for path in SYSTEM_CA_BUNDLES:
                if os.path.exists(path):
                    with open(path) as fh:
                        system_roots = fh.read()
                    break
            f = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
            f.write(system_roots.rstrip() + "\n" + ca_pem)
            f.close()
            ca_file = f.name
        # Every rclone option (type, provider, endpoint, keys, ...) becomes RCLONE_CONFIG_DST_<OPTION>.
        env = {f"RCLONE_CONFIG_DST_{k.upper()}": str(v) for k, v in (doc.get("rclone") or {}).items()}
        return cls(name=name, allow=list(doc.get("allow") or []), env=env, ca_cert_file=ca_file)


@dataclass
class TransferRequest:
    requestId: str
    ticketId: str
    requesterId: str
    requesterName: str
    srcPrefix: str
    target: str  # "<destination>:<bucket>/<path>/" — where this shard lands
    submittedAt: str = ""
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
        return f"source/{s.source_container}/{self.srcPrefix} -> {self.target}"


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


def _rclone_env(dest: Destination) -> dict[str, str]:
    s = settings()
    account = s.source_account_url.split("//", 1)[-1].split(".", 1)[0]
    return {
        **os.environ,
        # Source: Azure Blob via the worker's user-assigned managed identity (private endpoint in the VNet).
        "RCLONE_CONFIG_SRC_TYPE": "azureblob",
        "RCLONE_CONFIG_SRC_ACCOUNT": account,
        "RCLONE_CONFIG_SRC_USE_MSI": "true",
        "RCLONE_CONFIG_SRC_MSI_CLIENT_ID": os.environ.get("AZURE_CLIENT_ID", ""),
        **dest.env,
    }


def _rclone(args: list[str], dest: Destination, stdin: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["rclone", *args]
    if dest.ca_cert_file:
        cmd += ["--ca-cert", dest.ca_cert_file]
    log.info("exec: %s", " ".join(cmd))
    return subprocess.run(cmd, env=_rclone_env(dest), input=stdin, capture_output=True, text=True)


def run_copy(src: str, dst: str, dest: Destination) -> dict[str, Any]:
    transfers = os.environ.get("XL_RCLONE_TRANSFERS", "16")
    res = _rclone(
        [
            "copy",
            src,
            dst,
            "--checksum",
            "--transfers",
            transfers,
            "--checkers",
            transfers,
            "--use-json-log",
            "-v",
            "--stats",
            "60s",
            "--stats-log-level",
            "NOTICE",
        ],
        dest,
    )
    stats: dict[str, Any] = {}
    errors: list[str] = []
    for line in res.stderr.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec.get("stats"), dict):
            stats = rec["stats"]
        if rec.get("level") in ("error", "critical"):
            errors.append(rec.get("msg", "")[:300])
    if res.returncode != 0:
        for line in res.stderr.splitlines()[-10:]:
            log.error("rclone: %s", line[:500])
    return {"exitCode": res.returncode, "stats": stats, "errors": errors[:10]}


def run_check(src: str, dst: str, dest: Destination) -> dict[str, Any]:
    """rclone check --one-way: every source object must exist at the destination with the same size + hash."""
    res = _rclone(["check", src, dst, "--one-way", "--combined", "-"], dest)
    counts = {"=": 0, "-": 0, "*": 0, "!": 0}
    problems = []
    for line in res.stdout.splitlines():
        mark, _, name = line.partition(" ")
        if mark in counts:
            counts[mark] += 1
            if mark != "=":
                problems.append(f"{mark} {name}")
    return {
        "matched": counts["="],
        "missing": counts["-"],
        "differ": counts["*"],
        "errors": counts["!"],
        "problems": problems[:50],
        "exitCode": res.returncode,
    }


def source_manifest(req: TransferRequest) -> dict[str, Any]:
    s = settings()
    cc = ContainerClient.from_container_url(f"{s.source_account_url}/{s.source_container}", credential=credential())
    objs = {}
    for b in cc.list_blobs(name_starts_with=req.srcPrefix or None):
        md5 = b.content_settings.content_md5
        objs[b.name[len(req.srcPrefix) :]] = {"size": b.size, "md5": bytes(md5).hex() if md5 else ""}
    return objs


def process(req: TransferRequest, ledger: Ledger, attempt: int = 1, submitted_at=None) -> bool:
    from .destinations import DestinationError, Target, approved_targets, check_target

    s = settings()
    req.srcPrefix = _norm_prefix(req.srcPrefix)

    def reject(msg: str, **details) -> bool:
        ledger.emit(_ev(req, "transfer.rejected", "operate", "Rejected", msg, **details))
        return True  # handled: a rejection is a business outcome, not a job failure

    # 1) authorization at the moment the shard was SUBMITTED (Service Bus enqueue time — stamped by the
    #    service, not by the submitter): the requester must have held an activated transfer-operator role
    #    for THIS ticket. Shards queued behind others therefore still run after the role is handed back.
    role_guid = s.operator_role_id.rsplit("/", 1)[-1]
    activation = pim.active_activation_for_ticket(
        s.ctl_scope, req.requesterId, role_guid, req.ticketId, at=submitted_at
    )
    if not activation:
        return reject(
            f"{req.requesterName} held no transfer-operator activation for ticket {req.ticketId} "
            "when the shard was submitted",
            submittedAt=iso(submitted_at) if submitted_at else None,
        )

    # 2) destination: must be registered by IT AND inside the scope approved for this ticket
    approved = approved_targets(activation["properties"].get("justification"))
    try:
        target = Target.parse(req.target)
        dest = Destination.from_key_vault(target.name)
    except DestinationError as e:
        return reject(str(e), approvedTargets=[str(a) for a in approved])
    reason = check_target(target, dest.allow, approved)
    if reason:
        return reject(reason, approvedTargets=[str(a) for a in approved], registeredScope=dest.allow)

    ledger.emit(
        _ev(
            req,
            "transfer.started",
            "operate",
            "Running",
            f"copy {req.route}",
            key=str(attempt),
            activationRequestId=activation["name"],
            destination=target.name,
            approvedTargets=[str(a) for a in approved],
        )
    )

    src = f"src:{s.source_container}/{req.srcPrefix}"
    dst = f"dst:{target.scope}"

    # 3) copy
    copy = run_copy(src, dst, dest)
    st = copy["stats"]
    stats = {
        "exitCode": copy["exitCode"],
        "TransfersCompleted": st.get("transfers"),
        "TotalTransfers": st.get("totalTransfers"),
        "Checks": st.get("checks"),
        "Errors": st.get("errors"),
        "bytesTransferred": int(st.get("bytes") or 0),
        "ElapsedSeconds": round(float(st.get("elapsedTime") or 0), 1),
        "rcloneErrors": copy["errors"],
    }
    if copy["exitCode"] != 0:
        ledger.emit(
            _ev(
                req,
                "transfer.failed",
                "operate",
                "Failed",
                f"rclone copy exit={copy['exitCode']}: {(copy['errors'] or ['see job log'])[0][:200]} "
                f"(attempt {attempt})",
                key=str(attempt),
                **stats,
            )
        )
        raise RetryableError("rclone copy failed")

    # 4) verify + provenance manifest (next to the data, in the same bucket)
    if req.verify:
        v = run_check(src, dst, dest)
        manifest = {
            "ticketId": req.ticketId,
            "requestId": req.requestId,
            "requester": {"id": req.requesterId, "name": req.requesterName},
            "route": req.route,
            "approvedTargets": [str(a) for a in approved],
            "completedAt": iso(utcnow()),
            "copy": {k: stats[k] for k in ("TransfersCompleted", "bytesTransferred", "ElapsedSeconds")},
            "check": {k: v[k] for k in ("matched", "missing", "differ", "errors")},
            "objects": source_manifest(req),
        }
        body = json.dumps(manifest, indent=1)
        manifest_path = f"{target.bucket}/_manifests/{req.ticketId}/{req.requestId}.json"
        _rclone(["rcat", f"dst:{manifest_path}"], dest, stdin=body)
        ok = v["missing"] == 0 and v["differ"] == 0 and v["errors"] == 0 and v["matched"] > 0
        ledger.emit(
            _ev(
                req,
                "verify.passed" if ok else "verify.failed",
                "operate",
                "Passed" if ok else "Failed",
                f"{v['matched']} objects match (size + hash), missing={v['missing']} differ={v['differ']}",
                **{k: v[k] for k in ("matched", "missing", "differ", "errors", "problems")},
                manifestObject=f"{target.name}:{manifest_path}",
                manifestSha256=hashlib.sha256(body.encode()).hexdigest(),
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
            f"{stats['TransfersCompleted']} files, {stats['bytesTransferred']} bytes in {stats['ElapsedSeconds']}s",
            **stats,
        )
    )
    return True


def dest_test(name: str) -> int:
    """Connectivity check for a registered destination: list each allowed bucket, then write,
    read back and delete a tiny object under the first allowed scope. Result goes to the ledger."""
    import time

    dest = Destination.from_key_vault(name)
    checks: list[dict[str, Any]] = []

    def step(label: str, args: list[str], stdin: str | None = None) -> bool:
        r = _rclone(args, dest, stdin=stdin)
        err = next((ln for ln in reversed(r.stderr.splitlines()) if ln.strip()), "")
        checks.append({"step": label, "ok": r.returncode == 0, "error": "" if r.returncode == 0 else err[:300]})
        return r.returncode == 0

    for scope in dest.allow:
        step(f"list {scope}", ["lsf", f"dst:{scope}", "--max-depth", "1"])
    probe = f"{dest.allow[0].rstrip('/')}/_xl-connectivity-test/{int(time.time())}.txt"
    if step("write probe", ["rcat", f"dst:{probe}"], stdin="cross-ledger connectivity test\n"):
        step("read probe", ["cat", f"dst:{probe}"])
        step("delete probe", ["deletefile", f"dst:{probe}"])
    ok = all(c["ok"] for c in checks)
    s = settings()
    Ledger("transfer-worker").emit(
        LedgerEvent(
            EventType="destination.tested",
            Stage="operate",
            TicketId=f"DEST-{name}",
            TimeGenerated=iso(utcnow()),
            EventId=event_id("dest-test", name, os.environ.get("CONTAINER_APP_JOB_EXECUTION_NAME", iso(utcnow()))),
            ActorId=s.worker_identity,
            ActorName="transfer-worker",
            ActorType="ManagedIdentity",
            Route=f"-> {name}:",
            Status="Passed" if ok else "Failed",
            Message=f"destination '{name}': "
            + ", ".join(f"{c['step']} {'ok' if c['ok'] else 'FAILED'}" for c in checks),
            Details={"checks": checks, "allow": dest.allow},
        )
    )
    for c in checks:
        log.info("dest-test %s: %s %s", c["step"], "ok" if c["ok"] else "FAILED", c["error"])
    return 0 if ok else 1


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
            ok = process(req, ledger, attempt=attempt, submitted_at=msg.enqueued_time_utc)
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
                # Back on the queue for another execution (rclone copy skips objects that already match).
                rx.abandon_message(msg)
            return 1
        rx.complete_message(msg)
        return 0 if ok else 2
