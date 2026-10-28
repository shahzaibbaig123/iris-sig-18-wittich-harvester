"""Transports: the only place that touches the network (or the fixtures).

PoliteClient (http.py) adds robots, throttling and retries on top of a
transport. Tests and ``--offline`` runs use FixtureTransport instead of
RequestsTransport, so the whole pipeline runs without network access.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit


class TransportError(Exception):
    """Network-level failure (no HTTP response)."""


class TransportTimeout(TransportError):
    pass


class TransportConnectionError(TransportError):
    pass


@dataclass
class Response:
    url: str
    status: int
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def header(self, name: str) -> str | None:
        name = name.lower()
        for key, value in self.headers.items():
            if key.lower() == name:
                return value
        return None


class Transport(Protocol):
    def get(self, url: str, headers: dict[str, str], timeout: float) -> Response: ...


class RequestsTransport:
    def __init__(self):
        import requests

        self._requests = requests
        self.session = requests.Session()

    def get(self, url: str, headers: dict[str, str], timeout: float) -> Response:
        try:
            r = self.session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        except self._requests.Timeout as exc:
            raise TransportTimeout(str(exc)) from exc
        except self._requests.ConnectionError as exc:
            raise TransportConnectionError(str(exc)) from exc
        return Response(url=r.url, status=r.status_code, content=r.content, headers=dict(r.headers))


@dataclass
class FixtureRoute:
    status: int = 200
    file: str | None = None
    body: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None  # "timeout" or "connection"


class FixtureTransport:
    """Serves canned responses keyed by URL path (query included).

    A path may map to a list of routes; they are served in order and the last
    one repeats. That lets tests script "500, 500, 200" retry sequences.
    Unknown paths return 404, like a real server would.
    """

    def __init__(self, routes: dict[str, list[FixtureRoute]], root: Path | None = None):
        self.root = root
        self.routes = {k: deque(v) for k, v in routes.items()}
        self.calls: list[str] = []
        self.counts: dict[str, int] = defaultdict(int)

    @classmethod
    def from_manifest(cls, manifest: Path) -> "FixtureTransport":
        data = json.loads(manifest.read_text(encoding="utf-8"))
        routes = {}
        for path, spec in data["routes"].items():
            specs = spec if isinstance(spec, list) else [spec]
            routes[path] = [FixtureRoute(**s) for s in specs]
        return cls(routes, root=manifest.parent)

    @staticmethod
    def key(url: str) -> str:
        parts = urlsplit(url)
        return parts.path + (f"?{parts.query}" if parts.query else "")

    def get(self, url: str, headers: dict[str, str], timeout: float) -> Response:
        key = self.key(url)
        self.calls.append(key)
        self.counts[key] += 1
        queue = self.routes.get(key)
        if not queue:
            return Response(url=url, status=404, content=b"")
        route = queue[0] if len(queue) == 1 else queue.popleft()
        if route.error == "timeout":
            raise TransportTimeout(f"fixture timeout for {key}")
        if route.error == "connection":
            raise TransportConnectionError(f"fixture connection error for {key}")
        if route.file is not None:
            if self.root is None:
                raise ValueError("FixtureTransport needs a root to serve files")
            content = (self.root / route.file).read_bytes()
        else:
            content = (route.body or "").encode("utf-8")
        return Response(url=url, status=route.status, content=content, headers=dict(route.headers))
