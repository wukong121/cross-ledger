"""Collector tasks for ticket intake and the destination registry (run every collector cycle).

* reconcile    — re-queue every recent instance whose current status the connector has not
                 handled yet (missed events, connector downtime, records deleted in the ticketing
                 system). The connector decides what it means; this only makes sure it is asked.
* catalog      — the form's dataset / destination options must equal the Azure-side registries.
                 Drift is recorded and alerted (once a day per distinct drift); the form is never
                 overwritten automatically (the definition update API replaces the whole flow).
* destinations — every change to a `dest-*` Key Vault secret (portal, CLI or ARM) becomes a
                 `destination.changed` event: who, when, which destination — never the value.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from . import tickets as tk
from .config import Settings, settings
from .events import LedgerEvent, event_id, iso, utcnow
from .feishu import now_ms

log = logging.getLogger(__name__)

HANDLED = (tk.PENDING, tk.APPROVED, tk.REJECTED, *tk.WITHDRAWN)


def reconcile(fs, markers, send: Callable[[dict[str, Any], str], None], s: Settings | None = None) -> int:
    s = s or settings()
    end = now_ms()
    start = end - s.reconcile_days * 86400 * 1000
    codes = fs.list_instances(s.feishu_approval_code, start, end)
    # tickets approved earlier may have been deleted since — those no longer show up in the list
    # (only those of the current approval definition: after switching app / definition, tickets of the
    # old one are no longer visible and must not be reported as deleted)
    for k in markers.keys("intake/"):
        if k.endswith(f"/{tk.APPROVED}") and (markers.get(k) or {}).get("approvalCode") == s.feishu_approval_code:
            codes.append(k.split("/")[1])
    queued = 0
    for code in dict.fromkeys(codes):
        doc = fs.get_instance(code)
        status = (doc or {}).get("status") or "DELETED"
        if status not in HANDLED or markers.exists(f"intake/{code}/{status}"):
            continue
        mid = f"reconcile-{code}-{status}"
        send({"instanceCode": code, "status": status, "eventId": mid, "source": "reconcile"}, mid)
        queued += 1
    if queued:
        log.info("reconcile: %d instance(s) re-queued", queued)
    return queued


def catalog_event(fs, destinations: list[str], s: Settings | None = None) -> LedgerEvent | None:
    s = s or settings()
    definition = fs.get_definition(s.feishu_approval_code)
    opts = tk.form_options(definition, s.feishu_fields)
    diff = tk.catalog_diff(opts, s.datasets, destinations)
    if diff is None:
        return None
    day = utcnow().strftime("%Y-%m-%d")
    parts = [
        f"{sem}: form lacks {v['missing_in_form']} / form has unregistered {v['unknown_in_form']}"
        for sem, v in diff.items()
        if v["missing_in_form"] or v["unknown_in_form"]
    ]
    return LedgerEvent(
        EventType="catalog.drift",
        Stage="operate",
        TicketId="CATALOG",
        TimeGenerated=iso(utcnow()),
        EventId=event_id("catalog", day, json.dumps(diff, sort_keys=True)),
        ActorType="System",
        ActorName="collector",
        Status="Warning",
        Message="approval form options differ from the registries — " + "; ".join(parts),
        Details={
            "diff": diff,
            "formOptions": opts,
            "formWidgets": tk.form_widgets(definition),
            "fieldMapping": s.feishu_fields,
            "approvalCode": s.feishu_approval_code,
        },
    )


DEST_CHANGES_KQL = """
union isfuzzy=true
  (AzureDiagnostics
   | where ResourceProvider == "MICROSOFT.KEYVAULT" and Category == "AuditEvent"
   | where OperationName in ("SecretSet", "SecretDelete", "SecretUpdate", "SecretRestore", "SecretPurge")
   | extend Secret = tostring(split(column_ifexists("id_s", ""), "/")[4])
   | extend Caller = coalesce(
        column_ifexists("identity_claim_upn_s", ""),
        column_ifexists("identity_claim_http_schemas_xmlsoap_org_ws_2005_05_identity_claims_upn_s", ""),
        column_ifexists("identity_claim_oid_g", ""),
        column_ifexists("identity_claim_appid_g", ""))
   | project TimeGenerated, Op = OperationName, Secret, Caller, Result = ResultSignature, Ref = CorrelationId),
  (AzureActivity
   | where OperationNameValue startswith "MICROSOFT.KEYVAULT/VAULTS/SECRETS/" and ActivityStatusValue == "Success"
   | extend Secret = tostring(split(_ResourceId, "/")[10])
   | project TimeGenerated, Op = OperationNameValue, Secret, Caller, Result = ActivityStatusValue, Ref = CorrelationId)
| where Secret startswith "dest-"
"""


def destination_name(secret: str) -> str:
    name = secret.removeprefix("dest-")
    return name.removesuffix("-ca")


def destination_change_events(rows: list[dict[str, Any]]) -> list[LedgerEvent]:
    out = []
    for r in rows:
        name = destination_name(str(r.get("Secret", "")))
        if not name:
            continue
        what = "private CA" if str(r.get("Secret", "")).endswith("-ca") else "registration"
        out.append(
            LedgerEvent(
                EventType="destination.changed",
                Stage="operate",
                TicketId=f"DEST-{name}",
                TimeGenerated=iso(r.get("TimeGenerated")),
                EventId=event_id("dest-change", str(r.get("Ref", "")), str(r.get("Secret", "")), str(r.get("Op", ""))),
                ActorName=str(r.get("Caller", "")),
                ActorType="User",
                Route=f"-> {name}:",
                Status="Changed",
                Message=f"{r.get('Op')} on the {what} of destination '{name}' (value not recorded)",
                Details={"secret": r.get("Secret"), "operation": r.get("Op"), "result": r.get("Result")},
            )
        )
    return out


def run(client, query: Callable[..., list[dict]]) -> list[LedgerEvent]:
    """All ticket / registry tasks of one collector cycle. Each part is independent and non-fatal."""
    from azure.servicebus import ServiceBusClient, ServiceBusMessage

    from . import destinations as dst
    from .identity import credential
    from .markers import Markers

    s = settings()
    events: list[LedgerEvent] = []
    end = utcnow()
    try:
        events += destination_change_events(query(client, DEST_CHANGES_KQL, end - timedelta(hours=6), end))
    except Exception:
        log.exception("destination change scan failed (non-fatal)")
    if not s.feishu_app_id:
        return events

    from .feishu import client as feishu_client

    try:
        fs = feishu_client()
    except Exception:
        log.exception("ticket intake configured but the Feishu client is unavailable")
        return events
    try:
        enabled = [d["name"] for d in dst.list_all() if d["enabled"]]
        ev = catalog_event(fs, enabled, s)
        if ev:
            events.append(ev)
    except Exception:
        log.exception("catalog check failed (non-fatal)")
    try:
        with (
            ServiceBusClient(s.bus_namespace, credential=credential()) as sb,
            sb.get_queue_sender(s.ticket_queue) as tx,
        ):
            reconcile(
                fs,
                Markers(),
                lambda body, mid: tx.send_messages(
                    ServiceBusMessage(json.dumps(body), message_id=mid, subject=body["instanceCode"])
                ),
                s,
            )
    except Exception:
        log.exception("ticket reconcile failed (non-fatal)")
    return events
