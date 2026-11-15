"""Ledger collector — scheduled Container Apps Job.

Reconstructs the access lifecycle from first-party Azure records and writes
normalized, deduplicated ledger events:

  PIM roleAssignmentScheduleRequests  -> access.requested / .cancelled / .failed
  PIM roleAssignmentApprovals         -> access.approved / access.denied (who, when, why)
  request schedule + deactivations    -> access.activated / access.revoked / access.expired
  StorageBlobLogs / StorageQueueLogs  -> data.accessed   (what the person actually did in the window)
                                      -> data.accessed.outside-window (anomaly: human access w/o activation)
  AzureActivity                       -> rbac.changed    (anomaly: role assignment changed outside PIM)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from azure.monitor.query import LogsQueryClient, LogsQueryStatus
from azure.storage.blob import ContainerClient

from . import pim
from .config import settings
from .events import Ledger, LedgerEvent, event_id, iso, utcnow
from .identity import ArmError, credential

log = logging.getLogger(__name__)

LOOKBACK = timedelta(days=30)
LOG_LATENCY = timedelta(minutes=15)
DECISION_GRACE = timedelta(
    hours=1
)  # how long to wait for the approver's Activity Log record  # Azure Monitor ingestion latency budget before summarizing a window
STATE_BLOB = "collector-state.json"
OK_DEACTIVATION = {"Provisioned", "Revoked", "Granted", "PendingRevocation"}
ACTIVATED = {"Provisioned", "Revoked", "PendingRevocation", "Granted", "ProvisioningStarted", "AdminApproved"}


@dataclass
class Window:
    ticket: str
    principal_id: str
    principal_name: str
    role: str
    scope: str
    start: datetime
    end: datetime
    closed_by: str  # expired | revoked | open
    request_id: str


# --------------------------------------------------------------------- state --


class State:
    def __init__(self) -> None:
        s = settings()
        self.cc = ContainerClient.from_container_url(s.state_container_url, credential=credential())
        try:
            self.data = json.loads(self.cc.download_blob(STATE_BLOB).readall())
        except ResourceNotFoundError:
            self.data = {}
        self.emitted: dict[str, str] = self.data.setdefault("emitted", {})
        self.data.setdefault("anomalyCursor", iso(utcnow() - timedelta(hours=1)))

    def seen(self, eid: str) -> bool:
        return eid in self.emitted

    def mark(self, eid: str) -> None:
        self.emitted[eid] = iso(utcnow())

    def save(self) -> None:
        cutoff = utcnow() - LOOKBACK * 2
        self.data["emitted"] = {k: v for k, v in self.emitted.items() if datetime.fromisoformat(v) > cutoff}
        self.cc.upload_blob(STATE_BLOB, json.dumps(self.data).encode(), overwrite=True)


# ---------------------------------------------------------------- PIM -> events


def _principal(req: dict) -> tuple[str, str, str]:
    p = req["properties"]
    ep = (p.get("expandedProperties") or {}).get("principal") or {}
    return (
        p.get("principalId", ""),
        ep.get("email") or ep.get("displayName") or p.get("principalId", ""),
        ep.get("type") or p.get("principalType", "User"),
    )


def _ticket(req: dict) -> str:
    return ((req["properties"].get("ticketInfo") or {}).get("ticketNumber") or "").strip()


def _ev(etype: str, stage: str, ticket: str, when: datetime | str | None, key: str, **kw: Any) -> LedgerEvent:
    return LedgerEvent(
        EventType=etype,
        Stage=stage,
        TicketId=ticket,
        TimeGenerated=iso(when),
        EventId=event_id(key, etype),
        Origin="collector",
        **kw,
    )


def approval_decisions(client: LogsQueryClient) -> dict[str, dict[str, Any]]:
    """Approver identity from the Activity Log (ApproveRoleActivation / DenyRoleActivation).

    The PIM approval API is only readable by the approvers themselves, but every
    decision is also emitted to the Activity Log with the approver's UPN / object id,
    their comment and the RoleAssignmentRequestId it belongs to.
    """
    kql = """
AzureActivity
| where OperationNameValue startswith 'Add member to role request '
| extend p = Properties_d
| extend ev = tostring(p['Event Name']), rid = tostring(p['RoleAssignmentRequestId'])
| where ev in ('ApproveRoleActivation', 'DenyRoleActivation') and isnotempty(rid)
| extend ci = parse_json(tostring(p['CallerInfo']))
| mv-apply ci on (summarize upn = take_anyif(tostring(ci.CallerIdentityValue), ci.CallerIdentityType == 'UPN'),
                             oid = take_anyif(tostring(ci.CallerIdentityValue), ci.CallerIdentityType == 'ObjectID'))
| summarize arg_max(TimeGenerated, ev, upn, oid, justification = tostring(p['Justification']), correlationId = CorrelationId) by rid
"""
    now = utcnow()
    out = {}
    for row in _query(client, kql, now - LOOKBACK, now):
        out[row["rid"].lower()] = row
    return out


def pim_events(
    scope: str, decisions: dict[str, dict[str, Any]] | None = None
) -> tuple[list[LedgerEvent], list[Window]]:
    s = settings()
    now = utcnow()
    since = now - LOOKBACK
    reqs = [
        r
        for r in pim.schedule_requests(scope)
        if r["properties"].get("scope", "").lower() == scope.lower()
        and (pim.parse_time(r["properties"].get("createdOn")) or now) >= since
    ]
    deacts = [r for r in reqs if r["properties"].get("requestType") in ("SelfDeactivate", "AdminRemove")]
    events: list[LedgerEvent] = []
    windows: list[Window] = []

    for r in reqs:
        p = r["properties"]
        if p.get("requestType") != "SelfActivate":
            continue
        rid, ticket = r["name"], _ticket(r)
        pid, pname, ptype = _principal(r)
        role = s.role_by_scope(scope, p["roleDefinitionId"])
        start, end = pim.window(r)
        status = p.get("status", "")
        common = dict(Role=role, Scope=scope, Status=status)
        events.append(
            _ev(
                "access.requested",
                "request",
                ticket,
                p.get("createdOn"),
                rid,
                ActorId=pid,
                ActorName=pname,
                ActorType=ptype,
                Message=f"{pname} requested {role} for {p.get('scheduleInfo', {}).get('expiration', {}).get('duration')}: "
                f"{p.get('justification', '')}",
                Details={
                    "requestId": rid,
                    "justification": p.get("justification"),
                    "ticketInfo": p.get("ticketInfo"),
                    "scheduleInfo": p.get("scheduleInfo"),
                    "requestType": p.get("requestType"),
                },
                **common,
            )
        )

        # -- approval
        approved_at: datetime | None = None
        decided = False
        if p.get("approvalId"):
            try:
                ap = pim.approval(p["approvalId"])
                for st in ap["properties"].get("stages", []):
                    sp = st["properties"]
                    if sp.get("reviewResult") not in ("Approve", "Deny"):
                        continue
                    by = sp.get("reviewedBy") or {}
                    ok = sp["reviewResult"] == "Approve"
                    decided = True
                    when = pim.parse_time(sp.get("reviewedDateTime"))
                    approved_at = when if ok else None
                    events.append(
                        _ev(
                            "access.approved" if ok else "access.denied",
                            "approve",
                            ticket,
                            when,
                            f"{rid}/decision",
                            ActorId=by.get("id", ""),
                            ActorName=by.get("principalName") or by.get("displayName", ""),
                            ActorType=by.get("principalType", "User"),
                            Message=f"{'approved' if ok else 'denied'} by {by.get('principalName') or by.get('displayName')}"
                            f": {sp.get('justification', '')}",
                            Details={"requestId": rid, "approvalId": p["approvalId"], "stage": sp},
                            **common,
                        )
                    )
            except ArmError as e:
                log.warning("approval %s unreadable (%s) — falling back to request status", p["approvalId"], e.status)
        d = (decisions or {}).get(rid.lower())
        if not decided and d:
            ok = d["ev"] == "ApproveRoleActivation"
            decided = True
            when = (
                d["TimeGenerated"]
                if isinstance(d["TimeGenerated"], datetime)
                else pim.parse_time(str(d["TimeGenerated"]))
            )
            approved_at = when if ok else None
            events.append(
                _ev(
                    "access.approved" if ok else "access.denied",
                    "approve",
                    ticket,
                    when,
                    f"{rid}/decision",
                    ActorId=d.get("oid") or "",
                    ActorName=d.get("upn") or "",
                    ActorType="User",
                    Message=f"{'approved' if ok else 'denied'} by {d.get('upn')}: {d.get('justification') or ''}",
                    Details={
                        "requestId": rid,
                        "approvalId": p.get("approvalId"),
                        "source": "ActivityLog",
                        "correlationId": d.get("correlationId"),
                    },
                    **common,
                )
            )
        # Without an approver record, wait for Activity Log ingestion before settling for "unknown".
        settle = (pim.parse_time(p.get("createdOn")) or now) < now - DECISION_GRACE
        if not decided and settle:
            if status in ACTIVATED:
                events.append(
                    _ev(
                        "access.approved",
                        "approve",
                        ticket,
                        start,
                        f"{rid}/decision",
                        ActorType="System",
                        ActorName="PIM",
                        Message="approved (approver record not found in Activity Log)",
                        Details={"requestId": rid, "approvalId": p.get("approvalId")},
                        **common,
                    )
                )
            elif status in ("Denied", "AdminDenied"):
                events.append(
                    _ev(
                        "access.denied",
                        "approve",
                        ticket,
                        p.get("createdOn"),
                        f"{rid}/decision",
                        ActorType="System",
                        ActorName="PIM",
                        Message="denied",
                        Details={"requestId": rid},
                        **common,
                    )
                )
        if status in ("Canceled", "TimedOut", "Failed", "Invalid"):
            events.append(
                _ev(
                    f"access.{status.lower()}",
                    "revoke",
                    ticket,
                    p.get("createdOn"),
                    f"{rid}/end",
                    ActorId=pid,
                    ActorName=pname,
                    ActorType=ptype,
                    Message=f"request {status}",
                    Details={"requestId": rid},
                    **common,
                )
            )

        if status not in ACTIVATED or not start or not end:
            continue

        # -- activation + end of window
        act_at = max(start, approved_at) if approved_at else start
        events.append(
            _ev(
                "access.activated",
                "approve",
                ticket,
                act_at,
                rid,
                ActorId=pid,
                ActorName=pname,
                ActorType=ptype,
                Message=f"{role} active for {pname} until {iso(end)}",
                Details={"requestId": rid, "start": iso(act_at), "end": iso(end)},
                **common,
            )
        )
        closer = None
        for d in sorted(deacts, key=lambda d: d["properties"].get("createdOn", "")):
            dp = d["properties"]
            dt = pim.parse_time(dp.get("createdOn"))
            if (
                dp.get("principalId") == pid
                and dp.get("roleDefinitionId", "").lower() == p["roleDefinitionId"].lower()
                and dt
                and act_at <= dt <= end
                and dp.get("status") in OK_DEACTIVATION
            ):
                closer = d
                break
        if closer:
            dp = closer["properties"]
            cid, cname, ctype = _principal(closer)
            requestor = dp.get("requestorId", cid)
            kind = "self" if dp["requestType"] == "SelfDeactivate" else "admin"
            end = pim.parse_time(dp["createdOn"]) or end
            events.append(
                _ev(
                    "access.revoked",
                    "revoke",
                    ticket,
                    end,
                    rid,
                    ActorId=requestor,
                    ActorName=cname if kind == "self" else requestor,
                    ActorType=ctype,
                    Message=f"{role} revoked early ({kind}): {dp.get('justification', '')}",
                    Details={"requestId": rid, "deactivationRequestId": closer["name"], "kind": kind},
                    **common,
                )
            )
            windows.append(Window(ticket, pid, pname, role, scope, act_at, end, "revoked", rid))
        elif now >= end:
            events.append(
                _ev(
                    "access.expired",
                    "revoke",
                    ticket,
                    end,
                    rid,
                    ActorType="System",
                    ActorName="PIM",
                    Message=f"{role} for {pname} expired automatically",
                    Details={"requestId": rid},
                    **common,
                )
            )
            windows.append(Window(ticket, pid, pname, role, scope, act_at, end, "expired", rid))
        else:
            windows.append(Window(ticket, pid, pname, role, scope, act_at, end, "open", rid))
    return events, windows


# ---------------------------------------------------------- logs -> events


def _query(client: LogsQueryClient, kql: str, start: datetime, end: datetime) -> list[dict]:
    s = settings()
    res = client.query_workspace(s.workspace_id, kql, timespan=(start, end))
    if res.status != LogsQueryStatus.SUCCESS:
        log.warning("partial query result: %s", getattr(res, "partial_error", None))
        tables = res.partial_data
    else:
        tables = res.tables
    rows: list[dict] = []
    for t in tables:
        rows.extend(dict(zip(t.columns, r, strict=False)) for r in t.rows)
    return rows


def _account(scope: str) -> str:
    return scope.rstrip("/").rsplit("/", 1)[-1].lower()


def usage_events(client: LogsQueryClient, windows: list[Window], state: State) -> list[LedgerEvent]:
    """For closed windows: what did the human actually do with the access?"""
    now = utcnow()
    out = []
    for w in windows:
        if w.closed_by == "open" or now < w.end + LOG_LATENCY:
            continue
        eid = event_id(w.request_id, "data.accessed")
        if state.seen(eid):
            continue
        is_bus = w.role == "transfer-operator"
        if is_bus:
            # Service Bus (Standard) has no data-plane audit log; the worker's own ledger
            # events carry the authenticated requester, so summarize those instead.
            kql = f"""
XLedger_CL
| where TimeGenerated between (datetime({iso(w.start)}) .. datetime({iso(w.end + LOG_LATENCY)}))
| where EventType in ('transfer.started', 'transfer.rejected') and tostring(Details.requesterId) == '{w.principal_id}'
| summarize Operations = dcount(tostring(Details.requestId)), Writes = 0, Deletes = 0,
    Rejected = dcountif(tostring(Details.requestId), EventType == 'transfer.rejected'),
    Routes = make_set(Route, 30), Tickets = make_set(TicketId, 10)
"""
        else:
            kql = f"""
StorageBlobLogs
| where TimeGenerated between (datetime({iso(w.start)}) .. datetime({iso(w.end)}))
| where RequesterObjectId == '{w.principal_id}' and tolower(AccountName) == '{_account(w.scope)}'
| summarize Operations = count(), Reads = countif(Category == 'StorageRead'),
    Writes = countif(Category == 'StorageWrite'), Deletes = countif(Category == 'StorageDelete'),
    Failed = countif(toint(StatusCode) >= 400), BytesOut = sum(ResponseBodySize), BytesIn = sum(RequestBodySize),
    Objects = dcount(ObjectKey), OperationNames = make_set(OperationName, 30), SampleObjects = make_set(ObjectKey, 20),
    ClientIps = make_set(CallerIpAddress, 10)
"""
        rows = _query(client, kql, w.start - timedelta(minutes=5), w.end + LOG_LATENCY + timedelta(minutes=5))
        stats = rows[0] if rows else {}
        stats = {k: (v if not isinstance(v, str) or not v.startswith("[") else json.loads(v)) for k, v in stats.items()}
        n = int(stats.get("Operations") or 0)
        what = "transfer submissions" if is_bus else "storage operations"
        out.append(
            LedgerEvent(
                EventType="data.accessed",
                Stage="operate",
                TicketId=w.ticket,
                TimeGenerated=iso(w.end),
                EventId=eid,
                Origin="collector",
                ActorId=w.principal_id,
                ActorName=w.principal_name,
                ActorType="User",
                Role=w.role,
                Scope=w.scope,
                Status="Summarized",
                Message=f"{w.principal_name} performed {n} {what} during the {w.role} window "
                f"({int(stats.get('Writes') or 0)} writes, {int(stats.get('Deletes') or 0)} deletes)",
                Details={
                    "requestId": w.request_id,
                    "windowStart": iso(w.start),
                    "windowEnd": iso(w.end),
                    "closedBy": w.closed_by,
                    **stats,
                },
            )
        )
    return out


def anomaly_events(client: LogsQueryClient, windows: list[Window], state: State) -> list[LedgerEvent]:
    s = settings()
    now = utcnow()
    since = datetime.fromisoformat(state.data["anomalyCursor"])
    until = now - LOG_LATENCY
    if until <= since:
        return []
    accounts = {_account(sc): sc for sc in (s.source_scope, s.landing_scope) if sc}
    acct_list = ", ".join(f"'{a}'" for a in accounts)
    svc = ", ".join(f"'{x}'" for x in {*s.service_identities, s.worker_identity} if x) or "''"
    kql = f"""
StorageBlobLogs
| where TimeGenerated >= datetime({iso(since)}) and TimeGenerated < datetime({iso(until)})
| where tolower(AccountName) in ({acct_list}) and AuthenticationType == 'OAuth'
| where RequesterObjectId !in ({svc})
// service-level configuration reads (e.g. IaC refresh) never touch data
| where OperationName !in ('GetBlobServiceProperties', 'GetBlobServiceStats', 'GetAccountInformation')
| summarize First = min(TimeGenerated), Last = max(TimeGenerated), Operations = count(),
    Succeeded = countif(toint(StatusCode) < 400), Denied = countif(toint(StatusCode) in (401, 403)),
    OperationNames = make_set(OperationName, 20), SampleObjects = make_set(ObjectKey, 10),
    UserAgents = make_set(substring(UserAgentHeader, 0, 60), 5), ClientIps = make_set(CallerIpAddress, 5)
    by RequesterObjectId, RequesterUpn, AccountName = tolower(AccountName), bin(TimeGenerated, 5m)
"""
    out: list[LedgerEvent] = []
    for row in _query(client, kql, since - timedelta(minutes=1), until):
        oid, acct = row["RequesterObjectId"], row["AccountName"]
        first = row["First"] if isinstance(row["First"], datetime) else pim.parse_time(str(row["First"]))
        covered = any(
            w.principal_id == oid and _account(w.scope) == acct and w.start - timedelta(minutes=2) <= first <= w.end
            for w in windows
        )
        if covered:
            continue
        # All attempts blocked by RBAC -> recorded, low severity. Any success -> data really left without a grant.
        leaked = int(row.get("Succeeded") or 0) > 0
        etype = "data.accessed.outside-window" if leaked else "data.access.denied"
        status = "Anomaly" if leaked else "Denied"
        out.append(
            LedgerEvent(
                EventType=etype,
                Stage="operate",
                TicketId="UNTICKETED",
                TimeGenerated=iso(first),
                EventId=event_id(oid, acct, iso(row["TimeGenerated"]), "outside"),
                Origin="collector",
                ActorId=oid,
                ActorName=row.get("RequesterUpn") or oid,
                ActorType="User",
                Scope=accounts.get(acct, acct),
                Status=status,
                Message=(
                    f"{row.get('RequesterUpn') or oid} accessed {acct} WITHOUT a PIM activation "
                    f"({row['Succeeded']}/{row['Operations']} ops succeeded) — check for standing role assignments"
                    if leaked
                    else f"{row.get('RequesterUpn') or oid} tried {acct} without a PIM activation — "
                    f"all {row['Operations']} ops denied"
                ),
                Details={
                    k: (json.loads(v) if isinstance(v, str) and v.startswith("[") else v)
                    for k, v in row.items()
                    if k not in ("First", "Last", "TimeGenerated")
                },
            )
        )

    rg_names = [sc.split("/")[4].lower() for sc in s.scopes if sc.count("/") >= 4]
    if rg_names:
        rgs = ", ".join(f"'{r}'" for r in rg_names)
        kql = f"""
AzureActivity
| where TimeGenerated >= datetime({iso(since)}) and TimeGenerated < datetime({iso(until)})
| where OperationNameValue in~ ('Microsoft.Authorization/roleAssignments/write', 'Microsoft.Authorization/roleAssignments/delete')
| where ActivityStatusValue =~ 'Success' and tolower(ResourceGroup) in ({rgs})
| where Caller !~ 'MS-PIM' and Claims_d.appid != '01fc33a7-78ba-4d2f-a4b7-768e336e890e'
| project TimeGenerated, Caller, OperationNameValue, _ResourceId, CorrelationId, Properties_d
"""
        for row in _query(client, kql, since - timedelta(minutes=1), until):
            out.append(
                LedgerEvent(
                    EventType="rbac.changed",
                    Stage="approve",
                    TicketId="UNTICKETED",
                    TimeGenerated=iso(row["TimeGenerated"]),
                    EventId=event_id(row["CorrelationId"], row["OperationNameValue"], "rbac"),
                    Origin="collector",
                    ActorName=row["Caller"],
                    ActorType="User",
                    Scope=row["_ResourceId"],
                    Status="Anomaly",
                    Message=f"{row['Caller']} changed a standing role assignment outside PIM ({row['OperationNameValue']})",
                    Details={"correlationId": row["CorrelationId"]},
                )
            )
    state.data["anomalyCursor"] = iso(until)
    return out


# --------------------------------------------------------------------- main --


def main() -> int:
    s = settings()
    state = State()
    ledger = Ledger("collector")
    events: list[LedgerEvent] = []
    windows: list[Window] = []
    client = LogsQueryClient(credential())
    try:
        decisions = approval_decisions(client)
    except Exception:
        log.exception("could not read approval decisions from the Activity Log (non-fatal)")
        decisions = {}
    for scope in s.data_scopes:
        ev, win = pim_events(scope, decisions)
        events += ev
        windows += win
    events += usage_events(client, windows, state)
    try:
        events += anomaly_events(client, windows, state)
    except Exception:
        log.exception("anomaly detection failed (non-fatal)")

    fresh = []
    for e in sorted(events, key=lambda e: e.TimeGenerated):
        if not state.seen(e.EventId) and e.EventId not in {f.EventId for f in fresh}:
            fresh.append(e)
    # Emit in small batches and checkpoint after each, so a crash never re-emits.
    for i in range(0, len(fresh), 20):
        batch = fresh[i : i + 20]
        ledger.emit(*batch)
        for e in batch:
            state.mark(e.EventId)
        state.save()
    state.save()
    log.info("collector: %d windows, %d candidate events, %d new", len(windows), len(events), len(fresh))
    return 0


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
