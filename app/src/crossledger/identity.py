"""Credential + tiny ARM / token helpers."""

from __future__ import annotations

import base64
import json
import os
from functools import cache
from typing import Any

import httpx
from azure.identity import AzureCliCredential, DefaultAzureCredential, ManagedIdentityCredential

ARM = "https://management.azure.com"


@cache
def credential():
    # In Container Apps: user-assigned managed identity (AZURE_CLIENT_ID is set).
    # On a laptop: the operator's own `az login` — so every action is attributable to a human.
    if os.environ.get("CONTAINER_APP_JOB_NAME") or os.environ.get("IDENTITY_ENDPOINT"):
        return ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID"))
    if os.environ.get("XL_USE_DEFAULT_CREDENTIAL"):
        return DefaultAzureCredential()
    return AzureCliCredential()


def token(scope: str = f"{ARM}/.default") -> str:
    return credential().get_token(scope).token


def me() -> dict[str, str]:
    """Object id / UPN of the caller, from the ARM token claims."""
    payload = token().split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return {
        "oid": claims.get("oid", ""),
        "upn": claims.get("upn") or claims.get("unique_name") or claims.get("appid", ""),
        "tid": claims.get("tid", ""),
    }


class ArmError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"ARM {status}: {body[:800]}")
        self.status = status
        self.body = body


def arm(method: str, path: str, api_version: str, body: Any = None, params: dict | None = None) -> Any:
    url = path if path.startswith("http") else f"{ARM}{path}"
    q = {"api-version": api_version, **(params or {})}
    with httpx.Client(timeout=60) as c:
        r = c.request(method, url, params=q, json=body, headers={"Authorization": f"Bearer {token()}"})
    if r.status_code >= 400:
        raise ArmError(r.status_code, r.text)
    return r.json() if r.content else None


def arm_list(path: str, api_version: str, params: dict | None = None) -> list[dict]:
    out: list[dict] = []
    page = arm("GET", path, api_version, params=params)
    while True:
        out.extend(page.get("value", []))
        nxt = page.get("nextLink")
        if not nxt:
            return out
        with httpx.Client(timeout=60) as c:
            r = c.get(nxt, headers={"Authorization": f"Bearer {token()}"})
        r.raise_for_status()
        page = r.json()


def member_of(group_ids: list[str]) -> set[str]:
    """Live (not token-cached) transitive membership check of the caller."""
    t = token("https://graph.microsoft.com/.default")
    with httpx.Client(timeout=30) as c:
        r = c.post(
            "https://graph.microsoft.com/v1.0/me/checkMemberGroups",
            json={"groupIds": group_ids},
            headers={"Authorization": f"Bearer {t}"},
        )
    r.raise_for_status()
    return set(r.json().get("value", []))
