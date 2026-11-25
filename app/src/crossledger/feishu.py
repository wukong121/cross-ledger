"""Minimal Feishu (Lark) Open Platform client — approval + contact + bot message.

Authentication: a self-built app's tenant_access_token (App ID + App Secret). The App Secret
lives only in Key Vault (`feishu-app-secret`) and in memory; it is never logged.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from functools import cache
from typing import Any

import httpx

from .config import settings
from .identity import credential

log = logging.getLogger(__name__)

SECRET_NAME = "feishu-app-secret"
NOT_FOUND = {1390003}  # instance code not found
CHUNK_MS = 10 * 3600 * 1000  # instance list: keep each query window <= 10 h


class FeishuError(RuntimeError):
    def __init__(self, code: int, msg: str, path: str = "") -> None:
        super().__init__(f"feishu {path}: {code} {msg}")
        self.code = code


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, domain: str = "https://open.feishu.cn", http=None) -> None:
        self.app_id, self._secret, self.domain = app_id, app_secret, domain.rstrip("/")
        self.http = http or httpx.Client(timeout=30)
        self._token, self._exp = "", 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ transport
    def _tenant_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._exp - 300:
                return self._token
            r = self.http.post(
                f"{self.domain}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self._secret},
            )
            body = r.json()
            if body.get("code") != 0:
                raise FeishuError(body.get("code", -1), body.get("msg", ""), "tenant_access_token")
            self._token, self._exp = body["tenant_access_token"], time.time() + int(body.get("expire", 7200))
            return self._token

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        for attempt in range(4):
            r = self.http.request(
                method,
                f"{self.domain}/open-apis/{path}",
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {self._tenant_token()}"},
            )
            try:
                data = r.json()
            except ValueError:
                data = {"code": r.status_code, "msg": r.text[:200]}
            code = data.get("code", -1)
            if code == 0:
                return data.get("data") or {}
            if code in (99991663, 99991661):  # token invalid / expired: refresh once
                self._token = ""
                continue
            if r.status_code == 429 or code in (1395001, 99991400):  # throttled / transient
                time.sleep(1 + attempt * 2)
                continue
            raise FeishuError(code, data.get("msg", ""), path)
        raise FeishuError(-1, "retries exhausted", path)

    # ------------------------------------------------------------------- approval
    def get_instance(self, code: str) -> dict | None:
        try:
            return self.call("GET", f"approval/v4/instances/{code}", {"user_id_type": "open_id"})
        except FeishuError as e:
            if e.code in NOT_FOUND:
                return None
            raise

    def list_instances(self, approval_code: str, start_ms: int, end_ms: int) -> list[str]:
        out: list[str] = []
        lo = start_ms
        while lo < end_ms:
            hi = min(lo + CHUNK_MS, end_ms)
            token = ""
            while True:
                params = {"approval_code": approval_code, "start_time": str(lo), "end_time": str(hi), "page_size": 100}
                if token:
                    params["page_token"] = token
                data = self.call("GET", "approval/v4/instances", params)
                out += data.get("instance_code_list") or []
                if not data.get("has_more"):
                    break
                token = data.get("page_token", "")
            lo = hi
        return list(dict.fromkeys(out))

    def get_definition(self, approval_code: str) -> dict:
        return self.call("GET", f"approval/v4/approvals/{approval_code}", {"locale": "zh-CN"})

    def subscribe(self, approval_code: str) -> None:
        try:
            self.call("POST", f"approval/v4/approvals/{approval_code}/subscribe")
            log.info("subscribed to approval events of %s", approval_code)
        except FeishuError as e:  # already subscribed is reported as an error code; harmless
            log.info("subscribe %s: %s", approval_code, e)

    # ------------------------------------------------------------- contact / bot
    def user_email(self, open_id: str) -> str:
        data = self.call("GET", f"contact/v3/users/{open_id}", {"user_id_type": "open_id"})
        u = data.get("user") or {}
        return (u.get("enterprise_email") or u.get("email") or "").strip().lower()

    def notify(self, open_id: str, text: str) -> None:
        """Bot message to a user (status updates to the applicant). Best effort."""
        try:
            self.call(
                "POST",
                "im/v1/messages",
                {"receive_id_type": "open_id"},
                {"receive_id": open_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
            )
        except Exception as e:  # never fail a transfer because a chat message could not be sent
            log.warning("feishu notify failed: %s", e)


def read_app_secret() -> str:
    from azure.keyvault.secrets import SecretClient

    s = settings()
    return SecretClient(s.key_vault_uri, credential()).get_secret(SECRET_NAME).value or ""


@cache
def client() -> FeishuClient:
    s = settings()
    if not s.feishu_app_id:
        raise RuntimeError("Feishu intake is not configured (XL_FEISHU_APP_ID)")
    secret = read_app_secret()
    if not secret or secret == "unset":
        raise RuntimeError(f"Key Vault secret '{SECRET_NAME}' is not set yet (`xl feishu set-secret`)")
    return FeishuClient(s.feishu_app_id, secret, s.feishu_domain)


class EmailCache:
    """open_id -> email, refreshed per intake batch (approver lists are tiny)."""

    def __init__(self, fs: FeishuClient) -> None:
        self.fs, self.cache = fs, {}

    def __call__(self, open_id: str) -> str:
        if open_id not in self.cache:
            try:
                self.cache[open_id] = self.fs.user_email(open_id)
            except FeishuError as e:
                log.warning("email lookup for %s failed: %s", open_id, e)
                self.cache[open_id] = ""
        return self.cache[open_id]


def now_ms() -> int:
    return int(time.time() * 1000)


def as_ms(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
