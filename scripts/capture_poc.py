"""Regenerate the PoC report screenshots (docs/images/poc/*.svg, then PNG) from a live deployment.

    cd app && uv run python ../scripts/capture_poc.py [TICKET]

One-off captures (need a live pending request / the simulation host, run by hand):
    40, 42-48 (destination registry, plan file, enforcement — see docs/poc-report.md §5.3)
    14-approvals, 15-self-approve, 16-timeline-cancel, 20..26 (scripts/sim_run.py)
    e.g. CAPTURE_PROMPT="xl approvals" uv run python ../scripts/capture.py 14-approvals "IT ops: pending approvals" -- uv run xl approvals

Identities are redacted with the git-ignored .redact.json map (see capture.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from capture import ROOT, render, run  # noqa: E402

TICKET = sys.argv[1] if len(sys.argv) > 1 else "DT-POC-0005"
ENV = dict(ln.split("=", 1) for ln in (ROOT / ".xl.env").read_text().splitlines() if "=" in ln)
TF = json.loads(
    subprocess.run(
        ["terraform", f"-chdir={ROOT / 'infra'}", "output", "-json"], capture_output=True, text=True, check=True
    ).stdout
)
SUB, WS = ENV["XL_SUBSCRIPTION_ID"], ENV["XL_WORKSPACE_ID"]
RG = TF["resource_groups"]["value"]["gov"]
JOB = TF["jobs"]["value"]["transfer"]


def az_json(*args: str):
    r = subprocess.run(["az", *args, "-o", "json"], capture_output=True, text=True)
    if r.returncode != 0:  # fail loudly: an empty screenshot is worse than none
        raise SystemExit(f"az {' '.join(args[:4])} failed:\n{r.stderr}")
    return json.loads(r.stdout or "null")


def kql(query: str) -> list[dict]:
    rows = az_json("monitor", "log-analytics", "query", "-w", WS, "--analytics-query", query)
    return [{k: v for k, v in r.items() if k != "TableName"} for r in rows or []]


def xl(name: str, title: str, *args: str, width: int = 170) -> None:
    render(name, title, "xl " + " ".join(args), run(["uv", "run", "xl", *args], width), width=width)


def table(name: str, title: str, prompt: str, rows, width: int = 170) -> None:
    render(name, title, prompt, json.dumps(rows), width=width, table=True)


# --- CLI -----------------------------------------------------------------------------------------
xl("01-roles", "xl roles — data roles are eligible, never standing", "roles")
xl("02-tickets", "xl tickets — lifecycle coverage per ticket", "tickets")
xl("03-timeline", "xl timeline — request → approve → operate → revoke", "timeline", TICKET, width=210)
xl("04-ledger-verify", "Tamper evidence: Log Analytics vs Azure Confidential Ledger", "ledger-verify", TICKET)

# --- PIM policy ----------------------------------------------------------------------------------
SRC_ID = f"/subscriptions/{SUB}/resourceGroups/{TF['resource_groups']['value']['source']}/providers/Microsoft.Storage/storageAccounts/{TF['source_account']['value']}"
role = f"{SRC_ID}/providers/Microsoft.Authorization/roleDefinitions/2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"
pol = az_json(
    "rest",
    "--url",
    f"https://management.azure.com{SRC_ID}/providers/Microsoft.Authorization/"
    f"roleManagementPolicies?api-version=2020-10-01&$filter=roleDefinitionId eq '{role}'",
)
rules = {r["id"]: r for r in pol["value"][0]["properties"]["rules"]}
appr = rules["Approval_EndUser_Assignment"]["setting"]
table(
    "06-pim-policy",
    "PIM activation policy — source-reader",
    "PIM role settings: source-reader (Storage Blob Data Reader @ source account)",
    [
        {"setting": "Approval required", "value": appr["isApprovalRequired"]},
        {
            "setting": "Approvers",
            "value": ", ".join(a.get("description", "") for a in appr["approvalStages"][0]["primaryApprovers"]),
        },
        {"setting": "Required on activation", "value": rules["Enablement_EndUser_Assignment"]["enabledRules"]},
        {"setting": "Max activation duration", "value": rules["Expiration_EndUser_Assignment"]["maximumDuration"]},
        {"setting": "Eligibility must expire", "value": rules["Expiration_Admin_Eligibility"]["isExpirationRequired"]},
        {"setting": "Max eligibility", "value": rules["Expiration_Admin_Eligibility"]["maximumDuration"]},
    ],
)

# --- network -------------------------------------------------------------------------------------
table(
    "07a-storage-private",
    "Storage: Entra-only; source behind the perimeter, everything else private",
    "az storage account list (cross-ledger)",
    az_json(
        "storage",
        "account",
        "list",
        "--query",
        "[?tags.solution=='cross-ledger'].{account:name, role:tags.role, publicNetworkAccess:publicNetworkAccess, "
        "sharedKeyAccess:allowSharedKeyAccess, minTls:minimumTlsVersion}",
    ),
)


# --- optional managed transfer component ---------------------------------------------------------
MT = os.environ.get("MT_TICKET", "DT-POC-0007")  # a clean managed-transfer ticket
MT_REJECT = os.environ.get("MT_REJECT_TICKET", "DT-POC-9998")  # a submission with a mismatching ticket
if JOB:  # optional managed transfer component
    xl("41-dest-list", "Users: registered destinations and their allowed scope (no credentials)", "dest", "list")
    ev = kql(
        f"XLedger_CL | where Origin == 'transfer-worker' and TicketId in ('{MT}','{MT_REJECT}') "
        "| project at=format_datetime(TimeGenerated,'HH:mm:ss'), ticket=TicketId, event=EventType, message=Message "
        "| order by at asc"
    )
    table(
        "33-worker-events",
        "Managed transfer — worker events in the ledger",
        f"KQL: XLedger_CL (transfer-worker, {MT} / {MT_REJECT})",
        ev,
        width=210,
    )
    first = kql(
        f"XLedger_CL | where Origin == 'transfer-worker' and TicketId in ('{MT}','{MT_REJECT}') | summarize t=min(TimeGenerated)"
    )
    since = (first[0]["t"] or "")[:16] if first else ""
    table(
        "32-executions",
        "Managed transfer — one Container Apps Job execution per shard (KEDA)",
        f"az containerapp job execution list -n {JOB}",
        az_json(
            "containerapp",
            "job",
            "execution",
            "list",
            "-n",
            JOB,
            "-g",
            RG,
            "--query",
            f"reverse([?properties.startTime >= '{since[:15]}0'].{{execution:name, status:properties.status, "
            "started:properties.startTime, ended:properties.endTime})",
        ),
        width=150,
    )
    xl("35-timeline-managed", "Managed transfer — ticket timeline", "timeline", MT, width=210)
    alerts = az_json(
        "rest",
        "--url",
        f"https://management.azure.com/subscriptions/{SUB}/providers/Microsoft.AlertsManagement/alerts"
        f"?api-version=2019-05-05-preview&targetResourceGroup={RG}&timeRange=1d",
    )
    rej = kql(
        f"XLedger_CL | where TicketId == '{MT_REJECT}' and EventType == 'transfer.rejected' | summarize t=min(TimeGenerated)"
    )
    rej_t = (rej[0]["t"] or "")[:19] if rej else ""
    rows = [
        {
            "alert": a["properties"]["essentials"]["alertRule"].rsplit("/", 1)[-1],
            "severity": a["properties"]["essentials"]["severity"],
            "condition": a["properties"]["essentials"]["monitorCondition"],
            "fired": a["properties"]["essentials"]["startDateTime"][:19],
        }
        for a in alerts["value"]
        if "ledger" in a["name"] and a["properties"]["essentials"]["startDateTime"][:19] >= rej_t
    ]
    table(
        "36-alert",
        f"Ops alert — fired after the rejected submission ({rej_t[11:]} UTC)",
        "Azure Monitor: fired alerts",
        sorted(rows, key=lambda r: r["fired"])[:3],
        width=150,
    )
    xfer_oid = az_json("identity", "show", "-n", JOB.replace("job-", "id-"), "-g", RG, "--query", "principalId")
    kv = kql(
        f"AzureDiagnostics | where ResourceProvider == 'MICROSOFT.KEYVAULT' and OperationName == 'SecretGet' "
        f"| where TimeGenerated >= datetime({since}:00Z) - 5m "
        f"| extend caller = iif(tostring(pack_all()) has '{xfer_oid}', 'transfer-worker (managed identity)', 'other') "
        "| project at=format_datetime(TimeGenerated,'HH:mm:ss'), operation=OperationName, secret=tostring(split(id_s,'/')[4]), "
        "caller, result=ResultSignature | order by at asc"
    )
    table(
        "37-kv-audit",
        "Destination credentials: every read is audited (Key Vault)",
        "KQL: AzureDiagnostics (Key Vault, SecretGet)",
        kv,
        width=150,
    )

# --- ledger --------------------------------------------------------------------------------------
acc = kql(f"XLedger_CL | where TicketId == '{TICKET}' and EventType == 'data.accessed' | project Details | take 1")
details = json.loads(acc[0]["Details"]) if acc else {}
details.pop("sha256", None)
# presentation only: collapse ip:port to unique IPs, keep the object sample short
if details.get("ClientIps"):
    details["ClientIps"] = sorted({ip.rsplit(":", 1)[0] for ip in details["ClientIps"]})
if details.get("SampleObjects"):
    n = len(details["SampleObjects"])
    details["SampleObjects"] = [o.split("/datasets/", 1)[-1] for o in details["SampleObjects"][:4]] + (
        [f"... ({n} sampled)"] if n > 4 else []
    )
render(
    "10-data-accessed",
    "What the requester actually did inside the access window",
    f"KQL: XLedger_CL | where TicketId == '{TICKET}' and EventType == 'data.accessed' | project Details",
    json.dumps(details, indent=2, ensure_ascii=False),
    width=200,
)
table(
    "11-anomalies",
    "Detections: access without activation, RBAC changes outside PIM, audit gaps, rejections",
    "KQL: XLedger_CL — anomalies and rejections",
    kql(
        "XLedger_CL | where Status in ('Anomaly','Denied','Rejected') "
        "| summarize Events=count(), First=format_datetime(min(TimeGenerated),'HH:mm'), Last=format_datetime(max(TimeGenerated),'HH:mm') "
        "by EventType, Status, Ticket=TicketId, Actor=ActorName, Target=tostring(split(Scope,'/')[-1]) | order by EventType asc, First asc"
    ),
    width=200,
)
table(
    "27-nsp-logs",
    "Network Security Perimeter: public inbound to the source account",
    "KQL: NSPAccessLogs (source account, public inbound)",
    kql(
        f"NSPAccessLogs | where ServiceResourceId =~ '{SRC_ID}' and Category startswith 'NspPublicInbound' "
        "| summarize Requests=sum(Count), First=format_datetime(min(TimeGenerated),'HH:mm'), "
        "Last=format_datetime(max(TimeGenerated),'HH:mm') by ResultAction, SourceIpAddress, Category | order by ResultAction asc"
    ),
)
audit = az_json("storage", "account", "list", "-g", RG, "--query", "[?tags.role=='audit'].name")[0]
pol = az_json(
    "rest",
    "--url",
    f"https://management.azure.com/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Storage/"
    f"storageAccounts/{audit}/blobServices/default/containers/ledger/immutabilityPolicies/default?api-version=2023-05-01",
)
table(
    "12-worm",
    "WORM archive: time-based retention, append-only",
    f"immutability policy: {audit}/ledger",
    [
        {
            "state": pol["properties"]["state"],
            "retentionDays": pol["properties"]["immutabilityPeriodSinceCreationInDays"],
            "allowProtectedAppendWritesAll": pol["properties"].get("allowProtectedAppendWritesAll"),
        }
    ],
)

subprocess.run([str(ROOT / "scripts" / "svg2png.sh")], check=True)
