"""Destination registry for the managed transfer component.

A *destination* is a named rclone remote (connection + credentials) plus the bucket/prefix
scope data may be written to. Only IT ops register destinations; users pick one per
transfer with an rclone-style target ``<destination>:<bucket>/<path>/``.

Storage: one Key Vault secret per destination, ``dest-<name>``, in one of two formats.
  * portal format (default; IT fills the "create secret" form in the Azure portal):
        value = S3 secret key; tags = provider, endpoint, access_key_id, root, region?, description?
        (optional private CA: a second secret ``dest-<name>-ca``)
  * legacy format — value (JSON, authoritative, readable by the worker only):
        {"rclone": {...rclone remote options...}, "allow": ["bucket/prefix/", ...],
         "description": "...", "ca_cert_pem": "(optional)"}
  * tags (non-secret, listable through ARM by anyone with Reader on the vault):
        xl-kind=destination, type, provider, allow, description
Secrets are written / listed through the ARM management plane, so neither IT ops nor
users need network access to the vault's data plane.

Approval binding: ``xl request transfer-operator --to <target>`` appends
``| to: <target>, <target>`` to the PIM justification. The approver sees it, and the
worker only accepts shards whose target is inside that approved scope.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .config import settings
from .identity import arm

KV_API = "2023-07-01"
SECRET_PREFIX = "dest-"
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
TO_MARKER = re.compile(r"\|\s*to:\s*(?P<targets>.+?)\s*$")


class DestinationError(ValueError):
    pass


# ----------------------------------------------------------------------------- targets


@dataclass(frozen=True)
class Target:
    """``name:bucket/path/`` — path always normalized with a trailing slash ('' = bucket root)."""

    name: str
    bucket: str
    path: str = ""

    @classmethod
    def parse(cls, text: str) -> Target:
        name, sep, rest = text.strip().partition(":")
        if not sep or not NAME_RE.match(name):
            raise DestinationError(f"target must look like <destination>:<bucket>/<path>/, got {text!r}")
        bucket, _, path = rest.strip("/").partition("/")
        if not bucket:
            raise DestinationError(f"target {text!r} has no bucket")
        if ".." in path.split("/"):
            raise DestinationError(f"target {text!r} must not contain '..'")
        path = path.strip("/")
        return cls(name, bucket, f"{path}/" if path else "")

    @property
    def scope(self) -> str:
        return f"{self.bucket}/{self.path}"

    def within(self, other: Target) -> bool:
        return self.name == other.name and self.scope.startswith(other.scope)

    def join(self, prefix: str) -> Target:
        prefix = prefix.strip("/")
        return Target(self.name, self.bucket, f"{self.path}{prefix}/" if prefix else self.path)

    def __str__(self) -> str:
        return f"{self.name}:{self.scope}"


def in_scope(target: Target, allow: list[str]) -> bool:
    """Is the target inside one of the registry's allowed ``bucket/prefix/`` scopes?"""
    for entry in allow:
        bucket, _, path = entry.strip("/").partition("/")
        scope = f"{bucket}/{path.strip('/') + '/' if path.strip('/') else ''}"
        if target.scope.startswith(scope):
            return True
    return False


# ------------------------------------------------------------------- approval binding


def with_targets(justification: str, targets: list[Target]) -> str:
    return f"{justification} | to: {', '.join(str(t) for t in targets)}" if targets else justification


def approved_targets(justification: str | None) -> list[Target]:
    m = TO_MARKER.search(justification or "")
    if not m:
        return []
    return [Target.parse(t) for t in m.group("targets").split(",") if t.strip()]


def check_target(target: Target, allow: list[str], approved: list[Target]) -> str | None:
    """None if allowed, otherwise the human-readable reason it is not."""
    if not in_scope(target, allow):
        return f"{target} is outside the scope registered for destination '{target.name}' ({', '.join(allow)})"
    if not approved:
        return "the approved activation does not name any destination (request with --to)"
    if not any(target.within(a) for a in approved):
        return f"{target} is not covered by the approved destinations ({', '.join(str(a) for a in approved)})"
    return None


# ---------------------------------------------------------------------- registry (ARM)


def _vault_id() -> str:
    vid = settings().key_vault_id
    if not vid:
        raise DestinationError("managed transfer is not enabled (no Key Vault configured)")
    return vid


# Tags of the portal-friendly format that become rclone S3 options.
S3_TAGS = ("provider", "endpoint", "access_key_id", "region")


def _root(text: str) -> str:
    text = (text or "").strip().strip("/")
    return f"{text}/" if text else ""


def doc_from_secret(value: str, tags: dict[str, str]) -> dict:
    """Registry entry from a Key Vault secret, in either format.

    * portal format (IT fills a form in the Azure portal): value = the S3 secret key;
      tags = provider, endpoint, access_key_id, root (bucket/prefix/), region?, description?
    * legacy format: value = JSON {"rclone": {...}, "allow": [...], "description", "ca_cert_pem"?}
    """
    try:
        doc = json.loads(value)
    except (TypeError, ValueError):
        doc = None
    if isinstance(doc, dict) and "rclone" in doc:
        return doc
    tags = tags or {}
    rclone = {"type": tags.get("type") or "s3"}
    rclone.update({k: tags[k] for k in S3_TAGS if tags.get(k)})
    rclone["secret_access_key"] = value
    root = _root(tags.get("root", ""))
    return {"rclone": rclone, "allow": [root] if root else [], "description": tags.get("description", "")}


def put_s3(
    name: str,
    *,
    provider: str,
    endpoint: str,
    access_key_id: str,
    secret_key: str,
    root: str,
    region: str = "",
    description: str = "",
) -> dict:
    """Register / update a destination in the portal-friendly format (what IT would type in the portal)."""
    if not NAME_RE.match(name):
        raise DestinationError("destination names: lowercase letters, digits and '-' (max 32)")
    if not _root(root):
        raise DestinationError("a landing root bucket/prefix/ is required (--root)")
    tags = {
        "xl-kind": "destination",
        "type": "s3",
        "provider": provider,
        "endpoint": endpoint,
        "access_key_id": access_key_id,
        "root": _root(root),
        "allow": _root(root),
        "description": description[:250],
    }
    if region:
        tags["region"] = region
    body = {"tags": tags, "properties": {"value": secret_key, "contentType": "text/plain"}}
    return arm("PUT", f"{_vault_id()}/secrets/{SECRET_PREFIX}{name}", KV_API, body)


def put(name: str, rclone: dict, allow: list[str], description: str = "", ca_cert_pem: str | None = None) -> dict:
    if not NAME_RE.match(name):
        raise DestinationError("destination names: lowercase letters, digits and '-' (max 32)")
    if not allow:
        raise DestinationError("register at least one allowed bucket/prefix (--allow)")
    if "type" not in rclone:
        raise DestinationError("rclone remote options must include 'type'")
    value = {"rclone": rclone, "allow": allow, "description": description}
    if ca_cert_pem:
        value["ca_cert_pem"] = ca_cert_pem
    tags = {
        "xl-kind": "destination",
        "type": str(rclone.get("type", ""))[:64],
        "provider": str(rclone.get("provider", ""))[:64],
        "allow": ",".join(allow)[:250],
        "description": description[:250],
    }
    body = {"tags": tags, "properties": {"value": json.dumps(value), "contentType": "application/json"}}
    return arm("PUT", f"{_vault_id()}/secrets/{SECRET_PREFIX}{name}", KV_API, body)


def delete(name: str) -> None:
    # The ARM provider cannot delete secrets; disable instead so the worker refuses it.
    sec = arm("GET", f"{_vault_id()}/secrets/{SECRET_PREFIX}{name}", KV_API)
    tags = {**(sec.get("tags") or {}), "xl-kind": "destination-retired"}
    arm(
        "PATCH",
        f"{_vault_id()}/secrets/{SECRET_PREFIX}{name}",
        KV_API,
        {"tags": tags, "properties": {"attributes": {"enabled": False}}},
    )


def list_all() -> list[dict]:
    out = []
    page = arm("GET", f"{_vault_id()}/secrets", KV_API)
    for sec in page.get("value", []):
        tags = sec.get("tags") or {}
        if tags.get("xl-kind") != "destination" or not sec["name"].startswith(SECRET_PREFIX):
            continue
        allow = [a for a in (tags.get("root") or tags.get("allow", "")).split(",") if a]
        attrs = (sec.get("properties") or {}).get("attributes") or {}
        out.append(
            {
                "name": sec["name"][len(SECRET_PREFIX) :],
                "type": tags.get("type", ""),
                "provider": tags.get("provider", ""),
                "allow": allow,
                "root": allow[0] if allow else "",
                "description": tags.get("description", ""),
                "enabled": attrs.get("enabled", True),
                "updated": _ts(attrs.get("updated")),
            }
        )
    return sorted(out, key=lambda d: d["name"])


def _ts(v) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(v, UTC).strftime("%Y-%m-%d %H:%M") if isinstance(v, int | float) else str(v or "")


def allowed_scope(name: str) -> list[str]:
    for d in list_all():
        if d["name"] == name and d["enabled"]:
            return d["allow"]
    raise DestinationError(f"destination '{name}' is not registered (see `xl dest list`)")
