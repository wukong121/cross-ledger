"""PoC only: generate a sample dataset in the source account.

Runs as a dedicated managed identity inside the private network and records the
load itself in the ledger (ticket SAMPLE-DATA-<yyyymmdd>), so the dataset's
provenance starts at the moment it lands.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random

from azure.storage.blob import ContainerClient, ContentSettings

from .config import settings
from .events import Ledger, LedgerEvent, event_id, iso, utcnow
from .identity import credential

log = logging.getLogger(__name__)


def main() -> int:
    s = settings()
    batches = [b for b in os.environ.get("XL_SEED_BATCHES", "batch-001").split(",") if b]
    files = int(os.environ.get("XL_SEED_FILES", "20"))
    ticket = os.environ.get("XL_SEED_TICKET") or f"SAMPLE-DATA-{utcnow():%Y%m%d}"
    cc = ContainerClient.from_container_url(f"{s.source_account_url}/{s.source_container}", credential=credential())
    rnd = random.Random(42)
    total_bytes = total_objs = 0
    for b in batches:
        for i in range(1, files + 1):
            data = rnd.randbytes(rnd.randint(64, 4096) * 1024)
            md5 = hashlib.md5(data).digest()
            cc.upload_blob(
                f"{b}/shards/part-{i:04d}.bin",
                data,
                overwrite=True,
                content_settings=ContentSettings(content_md5=bytearray(md5)),
            )
            total_bytes += len(data)
            total_objs += 1
        meta = json.dumps({"batch": b, "source": "sample-generator", "classification": "synthetic"}).encode()
        cc.upload_blob(
            f"{b}/metadata.json",
            meta,
            overwrite=True,
            content_settings=ContentSettings(
                content_type="application/json", content_md5=bytearray(hashlib.md5(meta).digest())
            ),
        )
        total_bytes += len(meta)
        total_objs += 1
        log.info("loaded %s", b)
    Ledger("seed").emit(
        LedgerEvent(
            EventType="dataset.loaded",
            Stage="operate",
            TicketId=ticket,
            TimeGenerated=iso(utcnow()),
            EventId=event_id(ticket, ",".join(batches), str(total_bytes)),
            ActorId=os.environ.get("XL_SEED_IDENTITY", ""),
            ActorName="sample-data-generator",
            ActorType="ManagedIdentity",
            Scope=s.source_scope,
            Route=f"sample generator -> source/{s.source_container}",
            Status="Loaded",
            Message=f"sample data loaded: {len(batches)} batches, {total_objs} objects, {total_bytes} bytes",
            Details={
                "batches": batches,
                "objects": total_objs,
                "bytes": total_bytes,
                "md5Sample": base64.b64encode(md5).decode(),
            },
        )
    )
    return 0
