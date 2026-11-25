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
    to: Annotated[
        list[str],
        typer.Option(
            "--to",
            help="transfer-operator: destination(s) this ticket may write to, "
            "<destination>:<bucket>/<path>/ (repeatable). Shown to the approver and enforced.",
        ),
    ] = [],  # noqa: B006
) -> None:
    """Ask for just-in-time access (PIM activation → IT ops approval)."""
    from . import destinations as dst

    r = _role(role)
    if to and role != "transfer-operator":
        _fail(RuntimeError("--to only applies to transfer-operator"))
    if role == "transfer-operator" and not to:
        _fail(
            RuntimeError("transfer-operator requests must name the destination(s): --to <destination>:<bucket>/<path>/")
        )
    targets = []
    for t in to:
        try:
            target = dst.Target.parse(t)
            if not dst.in_scope(target, dst.allowed_scope(target.name)):
                raise dst.DestinationError(f"{target} is outside the scope IT registered for '{target.name}'")
        except dst.DestinationError as e:
            _fail(e)
        targets.append(target)
    reason = dst.with_targets(reason, targets)
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


def _plan_shards(plan: Path) -> tuple[str | None, list[tuple[str, str]]]:
    """Read a transfer plan (YAML) -> (ticket, [(source prefix, target)])."""
    import yaml

    from . import destinations as dst

    doc = yaml.safe_load(plan.read_text()) or {}
    shards: list[tuple[str, str]] = []
    for n, route in enumerate(doc.get("routes") or [], 1):
        if "to" not in route:
            raise dst.DestinationError(f"route #{n}: missing 'to'")
        prefixes = list(route.get("prefixes") or [])
        if route.get("prefixes_file"):
            f = (plan.parent / route["prefixes_file"]).read_text().splitlines()
            prefixes += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        if not prefixes:
            raise dst.DestinationError(f"route #{n}: no prefixes")
        shards += [(p, route["to"]) for p in prefixes]
    return doc.get("ticket"), shards


@app.command()
def transfer(
    ticket: Annotated[str | None, typer.Option("--ticket", "-t", help="Ticket (or set it in the plan)")] = None,
    prefix: Annotated[
        list[str], typer.Option("--prefix", "-p", help="Source prefix (repeatable). One shard each.")
    ] = [],  # noqa: B006
    to: Annotated[
        str | None, typer.Option("--to", help="<destination>:<bucket>/<path>/ for the --prefix shards")
    ] = None,
    plan: Annotated[Path | None, typer.Option(help="Transfer plan (YAML): routes of prefixes -> destinations")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate and show the shards, submit nothing")] = False,
    no_verify: Annotated[bool, typer.Option("--no-verify", help="Skip size/hash verification")] = False,
    no_precheck: Annotated[bool, typer.Option("--no-precheck", hidden=True)] = False,
) -> None:
    """Submit a managed transfer. Every shard's destination must be registered by IT and approved for the ticket."""
    from . import destinations as dst

    shards: list[tuple[str, str]] = []
    if plan:
        try:
            plan_ticket, shards = _plan_shards(plan)
        except (dst.DestinationError, OSError) as e:
            _fail(e)
        ticket = ticket or plan_ticket
    if prefix:
        if not to:
            _fail(RuntimeError("--prefix needs --to <destination>:<bucket>/<path>/"))
        shards += [(p, to) for p in prefix]
    if not ticket or not shards:
        _fail(RuntimeError("need a ticket and at least one shard (--prefix ... --to ..., or --plan)"))

    who = me()
    s = settings()
    # resolve + pre-check (the worker enforces the same rules again — this is for fast feedback)
    resolved = []
    scopes: dict[str, list[str]] = {}
    approved: list = []
    if not no_precheck:
        guid = s.operator_role_id.rsplit("/", 1)[-1]
        act = pim.active_activation_for_ticket(s.ctl_scope, who["oid"], guid, ticket)
        if not act:
            _fail(
                RuntimeError(
                    f"no active transfer-operator activation for ticket {ticket} — "
                    f"`xl request transfer-operator -t {ticket} --to ...` first"
                )
            )
        approved = dst.approved_targets(act["properties"].get("justification"))
    for p, t in shards:
        p = p.strip("/") + "/"
        try:
            target = dst.Target.parse(t).join(p)
            if not no_precheck:
                if target.name not in scopes:
                    scopes[target.name] = dst.allowed_scope(target.name)
                reason = dst.check_target(target, scopes[target.name], approved)
                if reason:
                    raise dst.DestinationError(reason)
        except dst.DestinationError as e:
            _fail(e)
        resolved.append((p, target))

    t = Table("shard", "request id", "source prefix", "target")
    bodies = []
    for i, (p, target) in enumerate(resolved, 1):
        body = {
            "requestId": str(uuid.uuid4()),
            "ticketId": ticket,
            "requesterId": who["oid"],
            "requesterName": who["upn"],
            "srcPrefix": p,
            "target": str(target),
            "submittedAt": iso(utcnow()),
            "verify": not no_verify,
        }
        bodies.append(body)
        t.add_row(str(i), "(dry run)" if dry_run else body["requestId"], p, str(target))
    if dry_run:
        con.print(t)
        con.print(f"[yellow]dry run[/] — {len(bodies)} shard(s) valid, nothing submitted")
        return

    from azure.servicebus import ServiceBusClient, ServiceBusMessage

    from .identity import credential

    with ServiceBusClient(s.bus_namespace, credential=credential()) as sb, sb.get_queue_sender(s.bus_queue) as sender:
        for body in bodies:
            try:
                sender.send_messages(
                    ServiceBusMessage(
                        json.dumps(body),
                        message_id=body["requestId"],
                        subject=ticket,
                        application_properties={"ticketId": ticket, "requester": who["upn"], "target": body["target"]},
                    )
                )
            except Exception as e:  # most likely 403: role not activated yet / RBAC still propagating
                _fail(RuntimeError(f"queue rejected the request ({type(e).__name__}) — is transfer-operator active?"))
    con.print(t)
    con.print(f"[green]{len(bodies)} shard(s) queued[/] — follow with `xl timeline {ticket}`")


# ------------------------------------------------------------------ destinations --

dest_app = typer.Typer(no_args_is_help=True, help="Destination registry for managed transfers (IT ops register).")
app.add_typer(dest_app, name="dest")


@dest_app.command("list")
def dest_list() -> None:
    """Registered destinations and the bucket/prefix scope each may receive (no credentials shown)."""
    from . import destinations as dst

    t = Table("name", "type / provider", "allowed scope", "description", "updated")
    for d in dst.list_all():
        if d["enabled"]:
            t.add_row(
                d["name"],
                f"{d['type']} / {d['provider'] or '-'}",
                ", ".join(d["allow"]),
                d["description"],
                str(d["updated"] or "")[:19],
            )
    con.print(t)


@dest_app.command("add")
def dest_add(
    name: Annotated[str, typer.Argument(help="Destination name, e.g. cloud-x (shown in the approval form)")],
    root: Annotated[str | None, typer.Option(help="Landing root bucket/prefix/ (S3 form)")] = None,
    endpoint: Annotated[str | None, typer.Option(help="S3-compatible endpoint URL (S3 form)")] = None,
    access_key_id: Annotated[
        str | None, typer.Option(help="Access key id (S3 form; the secret key is prompted)")
    ] = None,
    provider: Annotated[str, typer.Option(help="rclone S3 provider name (Other = any S3-compatible service)")] = "Other",
    region: Annotated[str, typer.Option()] = "",
    allow: Annotated[
        list[str] | None, typer.Option("--allow", help="(rclone form) bucket/prefix/ data may be written to")
    ] = None,
    from_rclone: Annotated[
        str | None, typer.Option(help="(rclone form) take connection + credentials from this local rclone remote")
    ] = None,
    rclone_config: Annotated[Path | None, typer.Option(help="rclone config file (default: rclone's own)")] = None,
    from_json: Annotated[Path | None, typer.Option(help="(rclone form) JSON file of rclone remote options")] = None,
    ca_cert: Annotated[Path | None, typer.Option(help="PEM of a private CA the destination uses")] = None,
    description: Annotated[str, typer.Option()] = "",
) -> None:
    """(IT ops) Register or update a destination — the same thing IT can do in the Azure portal
    (Key Vault > Secrets > dest-<name>: value = secret key, tags = provider/endpoint/access_key_id/root).
    Written through ARM — no Key Vault network access needed."""
    import subprocess

    from . import destinations as dst

    if root or endpoint or access_key_id:
        if not (root and endpoint and access_key_id):
            _fail(RuntimeError("S3 form needs --root, --endpoint and --access-key-id"))
        secret_key = typer.prompt("secret access key", hide_input=True)
        try:
            dst.put_s3(
                name,
                provider=provider,
                endpoint=endpoint,
                access_key_id=access_key_id,
                secret_key=secret_key,
                root=root,
                region=region,
                description=description,
            )
            if ca_cert:
                from .identity import arm

                arm(
                    "PUT",
                    f"{dst._vault_id()}/secrets/{dst.SECRET_PREFIX}{name}-ca",
                    dst.KV_API,
                    {"properties": {"value": ca_cert.read_text(), "contentType": "application/x-pem-file"}},
                )
        except (dst.DestinationError, ArmError) as e:
            _fail(e)
        con.print(f"[green]registered[/] {name}: {provider} {endpoint} root={root.strip('/')}/ (secret key not shown)")
        con.print(f"next: `xl dest test {name}` and add '{name}' to the approval form's destination options")
        return

    if not allow:
        _fail(RuntimeError("give --root/--endpoint/--access-key-id, or --allow with --from-rclone/--from-json"))
    if bool(from_rclone) == bool(from_json):
        _fail(RuntimeError("use exactly one of --from-rclone / --from-json"))
    if from_rclone:
        cmd = ["rclone", "config", "dump"] + (["--config", str(rclone_config)] if rclone_config else [])
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            _fail(RuntimeError(f"rclone config dump failed: {r.stderr.strip()[:300]}"))
        remotes = json.loads(r.stdout or "{}")
        if from_rclone not in remotes:
            _fail(RuntimeError(f"rclone remote '{from_rclone}' not found ({', '.join(remotes) or 'none'})"))
        options = remotes[from_rclone]
    else:
        options = json.loads(from_json.read_text())
    try:
        dst.put(
            name, options, [a.strip("/") + "/" for a in allow], description, ca_cert.read_text() if ca_cert else None
        )
    except (dst.DestinationError, ArmError) as e:
        _fail(e)
    con.print(
        f"[green]registered[/] {name}: {options.get('type')}/{options.get('provider', '-')} "
        f"allow={', '.join(a.strip('/') + '/' for a in allow)}  (credentials stored in Key Vault, not shown)"
    )
    con.print(f"next: `xl dest test {name}`")


@dest_app.command("remove")
def dest_remove(name: Annotated[str, typer.Argument()]) -> None:
    """(IT ops) Retire a destination (disabled in Key Vault; new shards to it are rejected)."""
    from . import destinations as dst

    try:
        dst.delete(name)
    except (dst.DestinationError, ArmError) as e:
        _fail(e)
    con.print(f"[green]retired[/] {name}")


@dest_app.command("test")
def dest_test(name: Annotated[str, typer.Argument()]) -> None:
    """(IT ops) Run a connectivity test from the transfer worker (list, write, read, delete a probe)."""
    import time

    from .identity import arm

    s = settings()
    job = f"/subscriptions/{s.subscription_id}/resourceGroups/{s.gov_rg}/providers/Microsoft.App/jobs/{s.transfer_job}"
    try:
        tpl = arm("GET", job, "2025-01-01")["properties"]["template"]
        c = tpl["containers"][0]
        c["command"] = ["crossledger", "dest-test", name]
        c.pop("args", None)
        ex = arm("POST", f"{job}/start", "2025-01-01", {"containers": [c]})
    except ArmError as e:
        _fail(e)
    con.print(f"running {ex['name']} from the transfer worker (same identity, network and egress IP as real shards)…")
    while True:
        st = arm("GET", f"{job}/executions/{ex['name']}", "2025-01-01")["properties"]["status"]
        if st in ("Succeeded", "Failed", "Stopped", "Degraded"):
            break
        time.sleep(10)
    ok = st == "Succeeded"
    con.print(
        f"[{'green' if ok else 'red'}]{name}: {'PASSED' if ok else 'FAILED'}[/] — details in the ledger: "
        f"`xl timeline DEST-{name}` (a few minutes)"
    )
    raise typer.Exit(0 if ok else 1)


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
    Requester=take_anyif(ActorName, EventType in ('access.requested', 'ticket.submitted')),
    Approver=take_anyif(ActorName, EventType in ('access.approved', 'ticket.approved') and ActorType != 'System')
    by TicketId
| order by Last desc""",
        days,
    )
    t = Table("ticket", "requester", "approver", "stages", "events", "last event")
    order = ["request", "approve", "operate", "close", "revoke"]
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
def ledger_verify(
    ticket: Annotated[str, typer.Argument()],
    days: int = 30,
    receipts: Annotated[
        bool, typer.Option("--receipts", help="Also fetch and verify each entry's signed receipt (offline proof)")
    ] = False,
) -> None:
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
    t = Table("event", "ledger tx", "result", *(["receipt"] if receipts else []))
    bad = 0
    for r in rows:
        tx, sha = entries.get(r["EventId"], (None, None))
        ok = tx == r["Tx"] and sha == r["Sha"]
        bad += not ok
        cells = [r["EventType"], r["Tx"], "[green]match" if ok else "[red]MISMATCH / missing in ledger"]
        if receipts:
            good, note = acl.verify_receipt(tx) if tx else (False, "no ledger entry")
            bad += not good
            cells.append(f"[green]verified[/] ({note})" if good else f"[red]FAILED[/] {note}")
        t.add_row(*cells)
    con.print(t)
    con.print(f"{len(rows)} rows, {len(entries)} ledger entries, {bad} problems")
    raise typer.Exit(1 if bad else 0)


# ------------------------------------------------------------- ticket intake --

feishu_app = typer.Typer(no_args_is_help=True, help="Feishu approval intake (IT ops).")
app.add_typer(feishu_app, name="feishu")


@feishu_app.command("set-secret")
def feishu_set_secret() -> None:
    """Store the Feishu App Secret in Key Vault (prompted, never echoed). Written through ARM."""
    from . import destinations as dst
    from .feishu import SECRET_NAME
    from .identity import arm

    value = typer.prompt("Feishu App Secret", hide_input=True).strip()
    if not value:
        _fail(RuntimeError("empty secret"))
    try:
        arm(
            "PUT",
            f"{dst._vault_id()}/secrets/{SECRET_NAME}",
            dst.KV_API,
            {"tags": {"xl-kind": "feishu"}, "properties": {"value": value, "contentType": "text/plain"}},
        )
    except (dst.DestinationError, ArmError) as e:
        _fail(e)
    con.print(f"[green]stored[/] Key Vault secret {SECRET_NAME} (value not shown)")
    con.print("restart the connector so it picks it up: `xl feishu restart`")


@feishu_app.command("restart")
def feishu_restart() -> None:
    """Restart the connector (e.g. after rotating the App Secret)."""
    from .identity import arm

    s = settings()
    if not s.connector_app:
        _fail(RuntimeError("no connector configured (XL_CONNECTOR_APP)"))
    app_id = f"/subscriptions/{s.subscription_id}/resourceGroups/{s.gov_rg}/providers/Microsoft.App/containerApps/{s.connector_app}"
    rev = arm("GET", app_id, "2025-01-01")["properties"]["latestRevisionName"]
    arm("POST", f"{app_id}/revisions/{rev}/restart", "2025-01-01")
    con.print(f"[green]restarted[/] {s.connector_app} ({rev})")


@feishu_app.command("check")
def feishu_check() -> None:
    """Show the approval form as the connector sees it, and compare its options with the registries."""
    from . import destinations as dst
    from . import tickets as tk
    from .feishu import FeishuClient

    s = settings()
    secret = typer.prompt("Feishu App Secret (only used locally for this check)", hide_input=True)
    fs = FeishuClient(s.feishu_app_id, secret, s.feishu_domain)
    d = fs.get_definition(s.feishu_approval_code)
    t = Table("widget id", "custom id", "name", "type", "→ meaning")
    for w in tk._widgets(d.get("form")):
        t.add_row(
            w.get("id", ""),
            w.get("custom_id", ""),
            w.get("name", ""),
            w.get("type", ""),
            tk._semantic(w, s.feishu_fields) or "",
        )
    con.print(f"approval [bold]{d.get('approval_name')}[/] status={d.get('status')}")
    con.print(t)
    opts = tk.form_options(d, s.feishu_fields)
    enabled = [x["name"] for x in dst.list_all() if x["enabled"]]
    diff = tk.catalog_diff(opts, s.datasets, enabled)
    con.print(f"form options: {opts}")
    con.print("[green]options match the registries" if diff is None else f"[yellow]drift: {diff}")


# ----------------------------------------------------------------------- jobs --


@app.command(hidden=True)
def connector() -> None:
    """(container app) Feishu long connection + ticket intake."""
    from . import connector as c

    raise typer.Exit(c.main())


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
    """(job) PoC: generate a sample dataset in the source account."""
    from . import seed as sd

    raise typer.Exit(sd.main())


@app.command("dest-test", hidden=True)
def dest_test_job(name: Annotated[str, typer.Argument()]) -> None:
    """(job) connectivity test for a destination, run inside the transfer worker."""
    from . import worker as w

    raise typer.Exit(w.dest_test(name))


@app.callback()
def _setup(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for noisy in ("azure.core.pipeline", "azure.identity", "httpx", "azure.monitor", "azure.servicebus", "uamqp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
