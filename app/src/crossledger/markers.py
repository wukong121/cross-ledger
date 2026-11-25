"""Tiny idempotency markers in the control account (``collector-state`` container).

intake/<instance>/<STATUS>    the connector finished handling that ticket status
transfer/<instance>/started   the worker started copying for that ticket
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .config import settings
from .identity import credential

PREFIX = "markers/"


class Markers:
    def __init__(self, container_url: str | None = None) -> None:
        from azure.storage.blob import ContainerClient

        self.cc = ContainerClient.from_container_url(
            container_url or settings().state_container_url, credential=credential()
        )

    def exists(self, key: str) -> bool:
        return self.cc.get_blob_client(PREFIX + key).exists()

    def put(self, key: str, body: dict[str, Any] | None = None) -> None:
        self.cc.get_blob_client(PREFIX + key).upload_blob(json.dumps(body or {}), overwrite=True)

    def get(self, key: str) -> dict[str, Any] | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return json.loads(self.cc.get_blob_client(PREFIX + key).download_blob().readall())
        except ResourceNotFoundError:
            return None

    def keys(self, prefix: str) -> list[str]:
        return [b.name[len(PREFIX) :] for b in self.cc.list_blobs(name_starts_with=PREFIX + prefix)]

    @contextmanager
    def lock(self, key: str, wait_seconds: int = 120) -> Iterator[None]:
        """Mutual exclusion across processes / replicas (blob lease, 60 s, renewed while held).
        Overlapping revisions during a rollout must not handle the same ticket concurrently."""
        from azure.core.exceptions import HttpResponseError, ResourceExistsError
        from azure.storage.blob import BlobLeaseClient

        blob = self.cc.get_blob_client(f"locks/{key}")
        try:
            blob.upload_blob(b"", overwrite=False)
        except ResourceExistsError:
            pass
        lease = BlobLeaseClient(blob)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                lease.acquire(lease_duration=60)
                break
            except HttpResponseError as e:
                if e.status_code != 409 or time.monotonic() > deadline:
                    raise
                time.sleep(2)
        stop = threading.Event()

        def renew() -> None:
            while not stop.wait(30):
                try:
                    lease.renew()
                except Exception:
                    return

        t = threading.Thread(target=renew, daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()
            try:
                lease.release()
            except Exception:
                pass


class MemoryMarkers:
    """In-memory stand-in (tests)."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @contextmanager
    def lock(self, key: str, wait_seconds: int = 120) -> Iterator[None]:
        with self._guard:
            lk = self._locks.setdefault(key, threading.Lock())
        with lk:
            yield

    def exists(self, key: str) -> bool:
        return key in self.data

    def put(self, key: str, body: dict[str, Any] | None = None) -> None:
        self.data[key] = body or {}

    def get(self, key: str) -> dict[str, Any] | None:
        return self.data.get(key)

    def keys(self, prefix: str) -> list[str]:
        return [k for k in self.data if k.startswith(prefix)]
