"""The ledger event model and its three sinks.

One event is written, in this order, to:
  1. Azure Confidential Ledger  (tamper-evident; returns a transaction id)
  2. Log Analytics `XLedger_CL` (queryable; row carries the ledger tx id + hash)
  3. WORM blob archive          (append-only JSONL, time-based immutability)

EventIds are deterministic (derived from the source object + event type), so
re-processing the same PIM request / transfer never produces a second fact.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import settings
from .identity import credential

log = logging.getLogger(__name__)

STAGES = ("request", "approve", "operate", "close", "revoke")  # revoke: PIM self-service only


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | str | None) -> str:
    if dt is None:
        return utcnow().isoformat()
    if isinstance(dt, str):
        return dt
    return dt.astimezone(UTC).isoformat()


def event_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


@dataclass
class LedgerEvent:
    EventType: str
    Stage: str
    TicketId: str
    TimeGenerated: str
    EventId: str
    ActorId: str = ""
    ActorName: str = ""
    ActorType: str = ""
    Role: str = ""
    Scope: str = ""
    Route: str = ""
    Status: str = ""
    Message: str = ""
    Origin: str = ""
    Details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.Stage not in STAGES:
            raise ValueError(f"unknown stage {self.Stage}")
        self.TicketId = self.TicketId or "UNTICKETED"

    def canonical(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=str)


# --------------------------------------------------------------------- sinks --


class _ConfidentialLedgerSink:
    def __init__(self, endpoint: str) -> None:
        from azure.confidentialledger import ConfidentialLedgerClient
        from azure.confidentialledger.certificate import ConfidentialLedgerCertificateClient

        ledger_id = endpoint.split("//", 1)[-1].split(".", 1)[0]
        ident = ConfidentialLedgerCertificateClient().get_ledger_identity(ledger_id=ledger_id)
        self.service_cert = ident["ledgerTlsCertificate"]
        cert = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
        cert.write(ident["ledgerTlsCertificate"])
        cert.close()
        self.client = ConfidentialLedgerClient(
            endpoint=endpoint, credential=credential(), ledger_certificate_path=cert.name
        )

    @staticmethod
    def collection(ticket: str) -> str:
        return re.sub(r"[^A-Za-z0-9_\-]", "_", ticket)[:64] or "UNTICKETED"

    def verify_receipt(self, transaction_id: str) -> tuple[bool, str]:
        """Fetch the write receipt of a transaction and verify it offline: the node's signature over
        the Merkle root, the endorsement chain to the ledger's service identity, and the proof path
        from this transaction's leaf to the signed root."""
        from azure.confidentialledger.receipt import verify_receipt

        res = self.client.begin_get_receipt(transaction_id).result()
        try:
            verify_receipt(res["receipt"], self.service_cert, application_claims=res.get("applicationClaims"))
            return True, f"signed by node {res['receipt'].get('nodeId', '')[:12]}…"
        except Exception as e:  # tampered / unverifiable
            return False, f"{type(e).__name__}: {e}"[:120]

    def write(self, ev: LedgerEvent, digest: str) -> str:
        entry = {"contents": json.dumps({"sha256": digest, "event": json.loads(ev.canonical())})}
        res = self.client.create_ledger_entry(entry=entry, collection_id=self.collection(ev.TicketId))
        return res["transactionId"]


class _WormSink:
    def __init__(self, container_url: str) -> None:
        from azure.storage.blob import ContainerClient

        self.container = ContainerClient.from_container_url(container_url, credential=credential())
        self._ready: set[str] = set()

    def write(self, records: list[dict[str, Any]], origin: str) -> None:
        from azure.core.exceptions import ResourceExistsError

        day = utcnow().strftime("%Y/%m/%d")
        name = f"events/{day}/{origin}.jsonl"
        blob = self.container.get_blob_client(name)
        if name not in self._ready:
            try:
                blob.create_append_blob(if_none_match="*")
            except ResourceExistsError:
                pass
            self._ready.add(name)
        payload = "".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in records).encode()
        # Append blocks are limited to 4 MiB; batches here are tiny, but be safe.
        for i in range(0, len(payload), 4 * 1024 * 1024):
            blob.append_block(payload[i : i + 4 * 1024 * 1024])


class _LogAnalyticsSink:
    def __init__(self, endpoint: str, rule_id: str, stream: str) -> None:
        from azure.monitor.ingestion import LogsIngestionClient

        self.client = LogsIngestionClient(endpoint=endpoint, credential=credential())
        self.rule_id, self.stream = rule_id, stream

    def write(self, records: list[dict[str, Any]]) -> None:
        self.client.upload(rule_id=self.rule_id, stream_name=self.stream, logs=records)


class Ledger:
    """Fan-out writer. Instantiate once per process."""

    def __init__(self, origin: str) -> None:
        s = settings()
        self.origin = origin
        self.acl = _ConfidentialLedgerSink(s.ledger_endpoint) if s.ledger_endpoint else None
        self.worm = _WormSink(s.audit_container_url) if s.audit_container_url else None
        self.la = _LogAnalyticsSink(s.dce_endpoint, s.dcr_id, s.dcr_stream) if s.dce_endpoint else None

    def emit(self, *events: LedgerEvent) -> list[dict[str, Any]]:
        records = []
        for ev in events:
            ev.Origin = ev.Origin or self.origin
            digest = hashlib.sha256(ev.canonical().encode()).hexdigest()
            tx = self.acl.write(ev, digest) if self.acl else ""
            rec = asdict(ev)
            rec["Details"] = {**ev.Details, "sha256": digest, "ledgerTransactionId": tx}
            records.append(rec)
            log.info("ledger %s %s %s %s", ev.TicketId, ev.Stage, ev.EventType, ev.Message)
        if not records:
            return records
        if self.la:
            self.la.write(records)
        if self.worm:
            self.worm.write(records, self.origin)
        return records
