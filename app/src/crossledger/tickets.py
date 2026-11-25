"""Approval tickets from the ticketing system (Feishu approval instances) — pure logic, no I/O.

The ticketing system is the *front end* (form + approval flow). Nothing in an event or a form is
trusted on its own: the instance is always re-read through the API, and every decision is made
here against the Azure-side registries (datasets, destinations) and the IT approver list.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

APPROVED = "APPROVED"
PENDING = "PENDING"
REJECTED = "REJECTED"
WITHDRAWN = ("CANCELED", "DELETED", "REVERTED")  # ended without (or no longer with) an approval
SEMANTICS = ("dataset", "destination", "purpose")


@dataclass
class Ticket:
    instance_code: str
    serial: str
    status: str
    approval_code: str
    applicant: str  # open_id
    dataset: str = ""
    destination: str = ""
    purpose: str = ""
    approvers: list[str] = field(default_factory=list)  # open_ids of approving tasks
    raw: dict[str, Any] = field(default_factory=dict)
    widgets: list[str] = field(default_factory=list)  # "name (type)" of every form widget, for diagnostics


@dataclass
class Decision:
    ok: bool
    reason: str = ""
    approver: str = ""
    approver_email: str = ""


def _widgets(form: Any) -> list[dict[str, Any]]:
    if isinstance(form, str):
        form = json.loads(form or "[]")
    return [w for w in form or [] if isinstance(w, dict)]


def _semantic(widget: dict[str, Any], fields: dict[str, str]) -> str | None:
    for key in (widget.get("custom_id"), widget.get("id"), widget.get("name")):
        if key and key in fields:
            return fields[key]
    return None


def _text(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("text") or value.get("value") or ""
    return str(value or "").strip()


def parse_instance(doc: dict[str, Any], fields: dict[str, str]) -> Ticket:
    """`fields` maps a widget custom_id / id / name to one of SEMANTICS."""
    t = Ticket(
        instance_code=doc.get("instance_code", ""),
        serial=str(doc.get("serial_number", "")),
        status=doc.get("status", ""),
        approval_code=doc.get("approval_code", ""),
        applicant=doc.get("open_id", ""),
        raw=doc,
    )
    for w in _widgets(doc.get("form")):
        t.widgets.append(f"{w.get('name') or w.get('custom_id') or w.get('id')} ({w.get('type', '?')})")
        sem = _semantic(w, fields)
        if sem in SEMANTICS:
            setattr(t, sem, _text(w.get("value")))
    seen: list[str] = []
    for task in doc.get("task_list") or []:
        if task.get("status") == APPROVED and task.get("open_id"):
            seen.append(task["open_id"])
    for ev in doc.get("timeline") or []:
        if ev.get("type") == "PASS" and ev.get("open_id"):
            seen.append(ev["open_id"])
    t.approvers = list(dict.fromkeys(seen))
    return t


def evaluate(
    t: Ticket,
    *,
    approver_emails: set[str],
    email_of: Callable[[str], str | None],
    datasets: dict[str, dict[str, Any]],
    destination: Callable[[str], dict[str, Any] | None],
) -> Decision:
    """Independent re-check of an approved ticket. `destination(name)` -> {"enabled", "root"} or None."""
    if t.status != APPROVED:
        return Decision(False, f"ticket is {t.status or 'unknown'}, not approved")
    for sem in ("dataset", "destination"):
        if not getattr(t, sem):
            return Decision(
                False,
                f"the form has no {sem} value — no widget is mapped to '{sem}' (form widgets: "
                f"{', '.join(t.widgets) or 'none'}; adjust feishu_fields)",
            )
    if t.dataset not in datasets:
        return Decision(False, f"dataset '{t.dataset}' is not registered")
    d = destination(t.destination) if t.destination else None
    if not d:
        return Decision(False, f"destination '{t.destination}' is not registered")
    if not d.get("enabled", True):
        return Decision(False, f"destination '{t.destination}' is disabled")
    if not d.get("root"):
        return Decision(False, f"destination '{t.destination}' has no landing root")
    it = {e.strip().lower() for e in approver_emails}
    self_only = True
    for oid in t.approvers:
        if oid == t.applicant:
            continue
        self_only = False
        email = (email_of(oid) or "").strip().lower()
        if email and email in it:
            return Decision(True, approver=oid, approver_email=email)
    if t.approvers and self_only:
        return Decision(False, "the applicant approved their own request; IT approval by someone else is required")
    return Decision(False, "no approver of this ticket belongs to IT")


def request_id(t: Ticket) -> str:
    return hashlib.sha256(f"{t.instance_code}|{t.dataset}|{t.destination}".encode()).hexdigest()[:16]


def target_for(t: Ticket, root: str) -> str:
    root = root.strip("/")
    return f"{t.destination}:{root}/{t.dataset}/" if root else f"{t.destination}:{t.dataset}/"


def form_options(definition: dict[str, Any], fields: dict[str, str]) -> dict[str, list[str]]:
    """Option texts of the dataset / destination widgets of an approval definition."""
    out: dict[str, list[str]] = {}
    for w in _widgets(definition.get("form")):
        sem = _semantic(w, fields)
        if sem not in ("dataset", "destination"):
            continue
        opts = w.get("option") or w.get("options") or []
        if isinstance(opts, dict):  # some shapes nest the list
            opts = opts.get("options") or opts.get("option") or []
        out[sem] = [_text(o) for o in opts if _text(o)]
    return out


def form_widgets(definition: dict[str, Any]) -> list[str]:
    return [
        f"{w.get('name') or w.get('custom_id') or w.get('id')} ({w.get('type', '?')})"
        for w in _widgets(definition.get("form"))
    ]


def catalog_diff(
    options: dict[str, list[str]], datasets: dict[str, Any], destinations: list[str]
) -> dict[str, dict[str, list[str]]] | None:
    """None when the form offers exactly the registered datasets / enabled destinations."""
    want = {"dataset": set(datasets), "destination": set(destinations)}
    diff = {}
    for sem, registered in want.items():
        offered = set(options.get(sem, []))
        diff[sem] = {
            "missing_in_form": sorted(registered - offered),
            "unknown_in_form": sorted(offered - registered),
        }
    if all(not v["missing_in_form"] and not v["unknown_in_form"] for v in diff.values()):
        return None
    return diff
