"""`xl` — the operator CLI.

Everything a human does goes through their own Entra ID identity (`az login`),
so every request/approval/transfer is attributable in the ledger.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import pim
from .config import settings
from .events import iso, utcnow
from .identity import ArmError, me

app = typer.Typer(no_args_is_help=True, add_completion=False, help="cross-ledger: request, approve, move, audit.")
con = Console()

Ticket = Annotated[str, typer.Option("--ticket", "-t", help="Business ticket / change number tying the lifecycle")]
Reason = Annotated[str, typer.Option("--reason", "-r", help="Justification (recorded in the ledger)")]


def _role(name: str) -> dict[str, str]:
    roles = settings().roles
    if name not in roles:
        con.print(f"[red]unknown role {name!r}; choose from: {', '.join(roles)}")
        raise typer.Exit(2)
    return roles[name]


def _fail(e: Exception) -> None:
    con.print(f"[red]{e}")
    raise typer.Exit(1)


# ------------------------------------------------------------------ data team --


@app.command()
def roles() -> None:
    """Roles you are eligible for, and the ones currently active."""
    who = me()
    t = Table("role", "scope", "eligible", "active until")
    for name, r in settings().roles.items():
        guid = r["role"].rsplit("/", 1)[-1].lower()
        elig = [
            e for e in pim.my_eligibilities(r["scope"]) if e["properties"]["roleDefinitionId"].lower().endswith(guid)
        ]
        act = [
            a
            for a in pim.my_active(r["scope"])
            if a["properties"]["roleDefinitionId"].lower().endswith(guid)
            and a["properties"].get("principalId") == who["oid"]
            # Only JIT activations; PIM's instance view can briefly keep a stale "Assigned" row after revocation.
            and a["properties"].get("assignmentType") == "Activated"
        ]
        until = ", ".join(a["properties"].get("endDateTime") or "?" for a in act) or "-"
        t.add_row(name, r["scope"].rsplit("/", 1)[-1], "yes" if elig else "no", until)
    con.print(f"signed in as [bold]{who['upn']}[/] ({who['oid']})")
    con.print(t)


@app.command()
def request(
    role: Annotated[str, typer.Argument(help="source-reader | transfer-operator")],
    ticket: Ticket,
    reason: Reason,
    hours: Annotated[float, typer.Option(help="Activation length (capped by policy)")] = 2,
) -> None:
    """Ask for just-in-time access (PIM activation → IT ops approval)."""
    r = _role(role)
    who = me()
    guid = r["role"].rsplit("/", 1)[-1].lower()
    elig = [e for e in pim.my_eligibilities(r["scope"]) if e["properties"]["roleDefinitionId"].lower().endswith(guid)]
    if not elig:
        _fail(RuntimeError(f"you are not eligible for {role} — ask IT ops to add you to the data-team group"))
    try:
        res = pim.activate(
            r["scope"],
            r["role"],
            who["oid"],
            ticket,
            reason,
            hours,
            elig[0]["properties"].get("roleEligibilityScheduleId"),
        )
    except ArmError as e:
        _fail(e)
    p = res["properties"]
    con.print(f"[green]request {res['name']}[/] status=[bold]{p['status']}[/] ticket={ticket}")
    if p["status"].startswith("PendingApproval"):
        con.print("waiting for IT ops: `xl approve --ticket " + ticket + "`")


@app.command()
def revoke(
    role: Annotated[str, typer.Argument(help="Role to give back early")],
    ticket: Ticket,
    reason: Reason = "work finished",
) -> None:
    """Give an active role back before it expires."""
    r = _role(role)
    try:
        res = pim.deactivate(r["scope"], r["role"], me()["oid"], ticket, reason)
    except ArmError as e:
        _fail(e)
    con.print(f"[green]deactivation {res['name']}[/] status={res['properties']['status']}")


@app.command()
def cancel(
    role: Annotated[str, typer.Argument(help="Role whose pending request should be withdrawn")],
    ticket: Ticket,
) -> None:
    """Withdraw your own request that is still waiting for approval."""
    r = _role(role)
    reqs = [
        x
        for x in pim.schedule_requests(r["scope"], "asRequestor()")
        if x["properties"].get("status", "").startswith("PendingApproval")
        and (x["properties"].get("ticketInfo") or {}).get("ticketNumber") == ticket
    ]
    if not reqs:
        _fail(RuntimeError(f"no pending {role} request for ticket {ticket}"))
    for x in reqs:
        pim.cancel(r["scope"], x["name"])
        con.print(f"[green]cancelled[/] {x['name']}")


@app.command()
def transfer(
    ticket: Ticket,
    prefix: Annotated[
        list[str], typer.Option("--prefix", "-p", help="Source prefix (repeatable). One shard each.")
    ] = [],  # noqa: B006
    manifest: Annotated[Path | None, typer.Option(help="File with one source prefix per line")] = None,
    dest_root: Annotated[str, typer.Option(help="Prepended to each prefix at the destination")] = "",
    no_verify: Annotated[bool, typer.Option("--no-verify", help="Skip md5/size verification")] = False,
) -> None:
    """Submit a transfer (requires an activated transfer-operator role for the same ticket)."""
    from azure.servicebus import ServiceBusClient, ServiceBusMessage

    from .identity import credential

    prefixes = list(prefix)
    if manifest:
        prefixes += [ln.strip() for ln in manifest.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    if not prefixes:
        _fail(RuntimeError("give at least one --prefix or --manifest"))
    who = me()
    s = settings()
    sb = ServiceBusClient(s.bus_namespace, credential=credential())
    sender = sb.get_queue_sender(s.bus_queue)
    t = Table("shard", "request id", "source prefix", "destination prefix")
    for i, p in enumerate(prefixes, 1):
        p = p.strip("/") + "/"
        body = {
            "requestId": str(uuid.uuid4()),
            "ticketId": ticket,
            "requesterId": who["oid"],
            "requesterName": who["upn"],
            "srcPrefix": p,
            "dstPrefix": f"{dest_root.strip('/')}/{p}" if dest_root else p,
            "submittedAt": iso(utcnow()),
            "verify": not no_verify,
        }
        try:
            sender.send_messages(
                ServiceBusMessage(
                    json.dumps(body),
                    message_id=body["requestId"],
                    subject=ticket,
                    application_properties={"ticketId": ticket, "requester": who["upn"]},
                )
            )
        except Exception as e:  # most likely 403: role not activated
            _fail(
                RuntimeError(
                    f"queue rejected the request ({type(e).__name__}). "
                    f"Is transfer-operator active? `xl request transfer-operator -t {ticket} ...`"
                )
            )
        t.add_row(str(i), body["requestId"], p, body["dstPrefix"])
    sender.close()
    sb.close()
    con.print(t)
    con.print(f"[green]{len(prefixes)} shard(s) queued[/] — follow with `xl timeline {ticket}`")


@app.command("rclone-config")
def rclone_config(
    remote: Annotated[str, typer.Option(help="Name of the rclone remote")] = "overseas",
) -> None:
    """Print an rclone remote for the source dataset (your own Entra ID via `az`, no keys / SAS).

    Append it to ~/.config/rclone/rclone.conf (or `rclone config file`), then e.g.
    `rclone copy overseas:datasets/batch-001 ./batch-001 --checksum`.
    """
    s = settings()
    account = s.source_account_url.split("//", 1)[-1].split(".", 1)[0]
    print(f"[{remote}]\ntype = azureblob\naccount = {account}\nuse_az = true\n")
    r = s.roles.get("source-reader")
    if r:
        guid = r["role"].rsplit("/", 1)[-1].lower()
        active = [
            a
            for a in pim.my_active(r["scope"])
            if a["properties"]["roleDefinitionId"].lower().endswith(guid)
            and a["properties"].get("assignmentType") == "Activated"
            and a["properties"].get("principalId") == me()["oid"]
        ]
        msg = (
            f"# source-reader active until {active[0]['properties'].get('endDateTime')}"
            if active
            else "# source-reader NOT active: `xl request source-reader -t <ticket> -r <reason>` first"
        )
        con.print(f"[dim]{msg}[/]", highlight=False)


# --------------------------------------------------------------------- IT ops --


def _pending_for_ticket(ticket: str | None) -> list[dict]:
    out = []
    for scope in settings().data_scopes:
        for r in pim.schedule_requests(scope, "atScope()"):
            p = r["properties"]
            if not p.get("status", "").startswith("PendingApproval") or p.get("scope", "").lower() != scope.lower():
                continue
            if ticket and (p.get("ticketInfo") or {}).get("ticketNumber") != ticket:
                continue
            out.append(r)
    return out


@app.command()
def approvals() -> None:
    """Pending access requests you can approve."""
    t = Table("ticket", "requester", "role", "duration", "justification", "created")
    s = settings()
    for r in _pending_for_ticket(None):
        p = r["properties"]
        ep = (p.get("expandedProperties") or {}).get("principal") or {}
        t.add_row(
            (p.get("ticketInfo") or {}).get("ticketNumber", ""),
            ep.get("email") or ep.get("displayName", ""),
            s.role_by_scope(p["scope"], p["roleDefinitionId"]),
            p.get("scheduleInfo", {}).get("expiration", {}).get("duration", ""),
            p.get("justification", ""),
            p.get("createdOn", "")[:19],
        )
    con.print(t)


def _review(ticket: str, reason: str, ok: bool) -> None:
    reqs = _pending_for_ticket(ticket)
    if not reqs:
        _fail(RuntimeError(f"no pending request for ticket {ticket}"))
    for r in reqs:
        try:
            done = pim.review(r["properties"]["approvalId"], ok, reason)
        except ArmError as e:
            _fail(e)
        verb = "approved" if ok else "denied"
        con.print(
            f"[green]{verb}[/] {r['name']} ({len(done)} stage(s))"
            if done
            else f"[yellow]nothing to review on {r['name']} (you cannot approve your own request)"
        )


@app.command()
def approve(ticket: Ticket, reason: Reason) -> None:
    """Approve every pending request carrying this ticket."""
    _review(ticket, reason, True)


@app.command()
def deny(ticket: Ticket, reason: Reason) -> None:
    """Deny every pending request carrying this ticket."""
    _review(ticket, reason, False)


@app.command("force-revoke")
def force_revoke(
    role: Annotated[str, typer.Argument()],
    principal: Annotated[str, typer.Option(help="Object id of the user")],
    ticket: Ticket,
    reason: Reason,
) -> None:
    """IT ops: cut off someone's active role immediately."""
    r = _role(role)
    try:
        res = pim.admin_remove(r["scope"], r["role"], principal, ticket, reason)
    except ArmError as e:
        _fail(e)
    con.print(f"[green]removal {res['name']}[/] status={res['properties']['status']}")


# ---------------------------------------------------------------------- audit --


def _logs(kql: str, days: int = 30) -> list[dict]:
    from datetime import timedelta

    from azure.monitor.query import LogsQueryClient

    from .identity import credential

    res = LogsQueryClient(credential()).query_workspace(settings().workspace_id, kql, timespan=timedelta(days=days))
    tables = res.tables if hasattr(res, "tables") else res.partial_data
    return [dict(zip(t.columns, row, strict=False)) for t in tables for row in t.rows]


@app.command()
def tickets(days: int = 30) -> None:
    """All tickets in the ledger with their lifecycle coverage."""
    rows = _logs(
        """
XLedger_CL
| summarize First=min(TimeGenerated), Last=max(TimeGenerated), Stages=make_set(Stage), Events=count(),
    Requester=take_anyif(ActorName, EventType=='access.requested'), Approver=take_anyif(ActorName, EventType=='access.approved' and ActorType != 'System')
    by TicketId
| order by Last desc""",
        days,
    )
    t = Table("ticket", "requester", "approver", "stages", "events", "last event")
    order = ["request", "approve", "operate", "revoke"]
    for r in rows:
        stages = json.loads(r["Stages"]) if isinstance(r["Stages"], str) else r["Stages"]
        t.add_row(
            r["TicketId"],
            r["Requester"] or "",
            r["Approver"] or "",
            " → ".join(s for s in order if s in stages),
            str(r["Events"]),
            str(r["Last"])[:19],
        )
    con.print(t)


@app.command()
def timeline(ticket: Annotated[str, typer.Argument()], days: int = 30) -> None:
    """Full request → approve → operate → revoke history of one ticket."""
    safe = ticket.replace("'", "")
    rows = _logs(
        f"""
XLedger_CL | where TicketId == '{safe}'
| project TimeGenerated, Stage, EventType, ActorName, Role, Status, Message, Tx=tostring(Details.ledgerTransactionId)
| order by TimeGenerated asc""",
        days,
    )
    t = Table("time (UTC)", "stage", "event", "actor", "role", "message", "ledger tx")
    for r in rows:
        t.add_row(
            str(r["TimeGenerated"])[:19], r["Stage"], r["EventType"], r["ActorName"], r["Role"], r["Message"], r["Tx"]
        )
    con.print(t)
    if not rows:
        con.print("[yellow]no events yet (collector runs every few minutes; ingestion adds ~2-5 min)")


@app.command("ledger-verify")
def ledger_verify(ticket: Annotated[str, typer.Argument()], days: int = 30) -> None:
    """Cross-check Log Analytics rows against Azure Confidential Ledger entries (tamper evidence)."""
    from .events import _ConfidentialLedgerSink

    s = settings()
    if not s.ledger_endpoint:
        _fail(RuntimeError("Confidential Ledger not enabled"))
    acl = _ConfidentialLedgerSink(s.ledger_endpoint)
    entries = {}
    for e in acl.client.list_ledger_entries(collection_id=acl.collection(ticket)):
        c = json.loads(e["contents"])
        entries[c["event"]["EventId"]] = (e["transactionId"], c["sha256"])
    safe = ticket.replace("'", "")
    rows = _logs(
        f"XLedger_CL | where TicketId == '{safe}' | project EventId, EventType, "
        f"Tx=tostring(Details.ledgerTransactionId), Sha=tostring(Details.sha256)",
        days,
    )
    t = Table("event", "ledger tx", "result")
    bad = 0
    for r in rows:
        tx, sha = entries.get(r["EventId"], (None, None))
        ok = tx == r["Tx"] and sha == r["Sha"]
        bad += not ok
        t.add_row(r["EventType"], r["Tx"], "[green]match" if ok else "[red]MISMATCH / missing in ledger")
    con.print(t)
    con.print(f"{len(rows)} rows, {len(entries)} ledger entries, {bad} problems")
    raise typer.Exit(1 if bad else 0)


# ----------------------------------------------------------------------- jobs --


@app.command(hidden=True)
def worker() -> None:
    """(job) process one transfer request from the queue."""
    from . import worker as w

    raise typer.Exit(w.main())


@app.command(hidden=True)
def collect() -> None:
    """(job) collect lifecycle events into the ledger."""
    from . import collector

    raise typer.Exit(collector.main())


@app.command(hidden=True)
def seed() -> None:
    """(job) PoC: simulate the vendor delivering a dataset into the source account."""
    from . import seed as sd

    raise typer.Exit(sd.main())


@app.callback()
def _setup(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for noisy in ("azure.core.pipeline", "azure.identity", "httpx", "azure.monitor"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
