"""Runtime configuration.

Jobs get everything through environment variables (set by Terraform). The CLI
additionally reads a `.xl.env` file (written by scripts/deploy.sh) from the
current directory or any parent, so operators don't have to export anything.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path


def _load_dotenv() -> None:
    explicit = os.environ.get("XL_ENV_FILE")
    candidates = [Path(explicit)] if explicit else [p / ".xl.env" for p in [Path.cwd(), *Path.cwd().parents]]
    for path in candidates:
        if path.is_file():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
            return


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    subscription_id: str = ""
    gov_rg: str = ""
    workspace_id: str = ""
    source_account_url: str = ""
    landing_account_url: str = ""
    source_container: str = "datasets"
    landing_container: str = "datasets"
    bus_namespace: str = ""
    bus_queue: str = "transfer-requests"
    state_container_url: str = ""
    audit_container_url: str = ""
    dce_endpoint: str = ""
    dcr_id: str = ""
    dcr_stream: str = "Custom-XLedger"
    ledger_endpoint: str = ""
    scopes: list[str] = field(default_factory=list)
    source_scope: str = ""
    landing_scope: str = ""
    ctl_scope: str = ""
    operator_role_id: str = ""
    worker_identity: str = ""
    service_identities: list[str] = field(default_factory=list)
    roles: dict[str, dict[str, str]] = field(default_factory=dict)

    def role_by_scope(self, scope: str, role_definition_id: str) -> str:
        """Short role name (CLI vocabulary) for a scope/role pair, or the role GUID."""
        for name, r in self.roles.items():
            if r["scope"].lower() == scope.lower() and r["role"].lower().endswith(
                role_definition_id.lower().rsplit("/", 1)[-1]
            ):
                return name
        return role_definition_id.rsplit("/", 1)[-1]

    @property
    def data_scopes(self) -> list[str]:
        return [s for s in (self.source_scope, self.ctl_scope, self.landing_scope) if s]


@cache
def settings() -> Settings:
    _load_dotenv()
    roles = json.loads(_get("XL_ROLES") or "{}")
    if not roles and _get("XL_SOURCE_SCOPE"):
        # Jobs don't get XL_ROLES; derive the same map from scopes.
        roles = {
            "source-reader": {"scope": _get("XL_SOURCE_SCOPE"), "role": "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"},
            "transfer-operator": {"scope": _get("XL_CTL_SCOPE"), "role": _get("XL_OPERATOR_ROLE_ID")},
            "landing-writer": {"scope": _get("XL_LANDING_SCOPE"), "role": "ba92f5b4-2d11-453d-a403-e96b0029c9fe"},
        }
    ctl_scope = _get("XL_CTL_SCOPE") or roles.get("transfer-operator", {}).get("scope", "")
    return Settings(
        subscription_id=_get("XL_SUBSCRIPTION_ID"),
        gov_rg=_get("XL_GOV_RG"),
        workspace_id=_get("XL_WORKSPACE_ID"),
        source_account_url=_get("XL_SOURCE_ACCOUNT_URL").rstrip("/"),
        landing_account_url=_get("XL_LANDING_ACCOUNT_URL").rstrip("/"),
        source_container=_get("XL_SOURCE_CONTAINER", "datasets"),
        landing_container=_get("XL_LANDING_CONTAINER", "datasets"),
        bus_namespace=_get("XL_BUS_NAMESPACE"),
        bus_queue=_get("XL_BUS_QUEUE", "transfer-requests"),
        state_container_url=_get("XL_STATE_CONTAINER_URL"),
        audit_container_url=_get("XL_AUDIT_CONTAINER_URL"),
        dce_endpoint=_get("XL_DCE_ENDPOINT"),
        dcr_id=_get("XL_DCR_ID"),
        dcr_stream=_get("XL_DCR_STREAM", "Custom-XLedger"),
        ledger_endpoint=_get("XL_LEDGER_ENDPOINT"),
        scopes=[s for s in _get("XL_SCOPES").split(",") if s],
        source_scope=_get("XL_SOURCE_SCOPE") or roles.get("source-reader", {}).get("scope", ""),
        landing_scope=_get("XL_LANDING_SCOPE") or roles.get("landing-writer", {}).get("scope", ""),
        ctl_scope=ctl_scope,
        operator_role_id=_get("XL_OPERATOR_ROLE_ID") or roles.get("transfer-operator", {}).get("role", ""),
        worker_identity=_get("XL_WORKER_IDENTITY"),
        service_identities=[x for x in _get("XL_SERVICE_IDENTITIES").split(",") if x],
        roles=roles,
    )
