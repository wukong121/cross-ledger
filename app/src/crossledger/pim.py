"""Privileged Identity Management (PIM for Azure resources) over ARM REST.

Docs: https://learn.microsoft.com/rest/api/authorization/privileged-role-assignment-rest-sample
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .identity import arm, arm_list

API = "2020-10-01"
APPROVAL_API = "2021-01-01-preview"
AUTHZ = "providers/Microsoft.Authorization"


def _dur(s: str | None) -> timedelta:
    """ISO-8601 duration subset used by PIM (PnDTnHnMnS)."""
    if not s:
        return timedelta(0)
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", s)
    if not m:
        return timedelta(0)
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi, seconds=se)


def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00"))
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def window(req: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    """Start / end of an activation request (end may be computed from a duration)."""
    p = req["properties"]
    si = p.get("scheduleInfo") or {}
    start = parse_time(si.get("startDateTime")) or parse_time(p.get("createdOn"))
    exp = si.get("expiration") or {}
    end = parse_time(exp.get("endDateTime"))
    if not end and start and exp.get("duration"):
        end = start + _dur(exp["duration"])
    return start, end


# ------------------------------------------------------------------ reading --


def schedule_requests(scope: str, flt: str = "atScope()") -> list[dict]:
    return arm_list(f"{scope}/{AUTHZ}/roleAssignmentScheduleRequests", API, {"$filter": flt})


def my_eligibilities(scope: str) -> list[dict]:
    """Eligibilities that apply to the caller (directly or through a group).

    `asTarget()` resolves groups from the caller's *token*, so someone who was
    just added to the data-team group sees nothing until they sign in again.
    Fall back to the scope's eligibilities + a live Graph membership check.
    """
    mine = arm_list(f"{scope}/{AUTHZ}/roleEligibilityScheduleInstances", API, {"$filter": "asTarget()"})
    if mine:
        return mine
    from .identity import member_of

    every = arm_list(f"{scope}/{AUTHZ}/roleEligibilityScheduleInstances", API, {"$filter": "atScope()"})
    groups = {e["properties"]["principalId"] for e in every if e["properties"].get("principalType") == "Group"}
    in_groups = member_of(sorted(groups)) if groups else set()
    oid = me_oid()
    return [e for e in every if e["properties"]["principalId"] in in_groups | {oid}]


def me_oid() -> str:
    from .identity import me

    return me()["oid"]


def my_active(scope: str) -> list[dict]:
    return arm_list(f"{scope}/{AUTHZ}/roleAssignmentScheduleInstances", API, {"$filter": "asTarget()"})


def approval(approval_id: str) -> dict:
    return arm("GET", approval_id, APPROVAL_API)


def my_pending_approvals() -> list[dict]:
    return arm_list(f"/{AUTHZ}/roleAssignmentApprovals", APPROVAL_API, {"$filter": "asApprover()"})


# ------------------------------------------------------------------ writing --


def _request(scope: str, body: dict) -> dict:
    name = str(uuid.uuid4())
    return arm("PUT", f"{scope}/{AUTHZ}/roleAssignmentScheduleRequests/{name}", API, {"properties": body})


def activate(
    scope: str,
    role_definition_id: str,
    principal_id: str,
    ticket: str,
    justification: str,
    hours: float,
    eligibility_schedule_id: str | None = None,
) -> dict:
    minutes = max(5, int(hours * 60))
    body = {
        "principalId": principal_id,
        "roleDefinitionId": role_definition_id,
        "requestType": "SelfActivate",
        "justification": justification,
        "ticketInfo": {"ticketNumber": ticket, "ticketSystem": "cross-ledger"},
        "scheduleInfo": {
            "startDateTime": datetime.now(UTC).isoformat(),
            "expiration": {"type": "AfterDuration", "duration": f"PT{minutes}M"},
        },
    }
    if eligibility_schedule_id:
        body["linkedRoleEligibilityScheduleId"] = eligibility_schedule_id
    return _request(scope, body)


def deactivate(scope: str, role_definition_id: str, principal_id: str, ticket: str, justification: str) -> dict:
    return _request(
        scope,
        {
            "principalId": principal_id,
            "roleDefinitionId": role_definition_id,
            "requestType": "SelfDeactivate",
            "justification": justification,
            "ticketInfo": {"ticketNumber": ticket, "ticketSystem": "cross-ledger"},
        },
    )


def admin_remove(scope: str, role_definition_id: str, principal_id: str, ticket: str, justification: str) -> dict:
    return _request(
        scope,
        {
            "principalId": principal_id,
            "roleDefinitionId": role_definition_id,
            "requestType": "AdminRemove",
            "justification": justification,
            "ticketInfo": {"ticketNumber": ticket, "ticketSystem": "cross-ledger"},
        },
    )


def cancel(scope: str, request_name: str) -> None:
    arm("POST", f"{scope}/{AUTHZ}/roleAssignmentScheduleRequests/{request_name}/cancel", API)


def review(approval_id: str, approve: bool, justification: str) -> list[dict]:
    """Approve / deny every pending stage of an approval that the caller can review."""
    ap = approval(approval_id)
    done = []
    for stage in ap["properties"].get("stages", []):
        sp = stage["properties"]
        if sp.get("status") not in ("InProgress", "NotStarted") or sp.get("reviewResult") not in (None, "NotReviewed"):
            continue
        done.append(
            arm(
                "PUT",
                stage["id"],
                APPROVAL_API,
                {"properties": {"reviewResult": "Approve" if approve else "Deny", "justification": justification}},
            )
        )
    return done


def active_activation_for_ticket(scope: str, principal_id: str, role_guid: str, ticket: str) -> dict | None:
    """The *provisioned, unexpired* SelfActivate request of a principal for a role carrying this ticket."""
    now = datetime.now(UTC)
    reqs = schedule_requests(scope, f"principalId eq '{principal_id}'")
    for r in sorted(reqs, key=lambda r: r["properties"].get("createdOn", ""), reverse=True):
        p = r["properties"]
        if p.get("requestType") != "SelfActivate" or p.get("status") != "Provisioned":
            continue
        if not p.get("roleDefinitionId", "").lower().endswith(role_guid.lower()):
            continue
        if (p.get("ticketInfo") or {}).get("ticketNumber") != ticket:
            continue
        start, end = window(r)
        if start and end and start <= now <= end:
            # A later SelfDeactivate would revoke it; make sure none exists.
            for d in reqs:
                dp = d["properties"]
                if (
                    dp.get("requestType") in ("SelfDeactivate", "AdminRemove")
                    and dp.get("roleDefinitionId", "").lower() == p["roleDefinitionId"].lower()
                    and (parse_time(dp.get("createdOn")) or now) >= start
                    and dp.get("status") in ("Provisioned", "Revoked", "Granted")
                ):
                    return None
            return r
    return None
