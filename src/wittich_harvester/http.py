"""Polite HTTP client: robots.txt, per-host throttling, retries with backoff.

The retry policy follows the iris-sample HttpClient (timeouts, connection
errors, 429 with Retry-After, 5xx with exponential backoff plus jitter), but
sleep, clock and randomness are injectable so tests never wait.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from wittich_harvester.config import HttpSettings
from wittich_harvester.transport import (
    Response,
    Transport,
    TransportConnectionError,
    TransportTimeout,
)

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, code: str, url: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.url = url
        self.status = status


class RobotsDisallowed(FetchError):
    pass


# ----------------------------------------------------------------------------
# robots.txt (RFC 9309 group selection, longest-match rule, * and $ wildcards)
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RobotsRule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


def _compile_rule(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(regex + ("$" if anchored else ""))


class RobotsPolicy:
    def __init__(self, groups: list[tuple[list[str], list[RobotsRule]]], crawl_delays: dict[str, float]):
        self.groups = groups
        self.crawl_delays = crawl_delays

    @classmethod
    def allow_all(cls) -> "RobotsPolicy":
        return cls([], {})

    @classmethod
    def disallow_all(cls) -> "RobotsPolicy":
        return cls([(["*"], [RobotsRule(False, "/", _compile_rule("/"))])], {})

    @classmethod
    def parse(cls, text: str) -> "RobotsPolicy":
        groups: list[tuple[list[str], list[RobotsRule]]] = []
        delays: dict[str, float] = {}
        agents: list[str] = []
        rules: list[RobotsRule] = []
        in_rules = False

        def flush():
            if agents:
                groups.append((list(agents), list(rules)))

        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if in_rules:
                    flush()
                    agents, rules, in_rules = [], [], False
                agents.append(value.lower())
            elif key in ("allow", "disallow"):
                in_rules = True
                if value:  # an empty Disallow allows everything
                    rules.append(RobotsRule(key == "allow", value, _compile_rule(value)))
            elif key == "crawl-delay":
                in_rules = True
                try:
                    for agent in agents:
                        delays[agent] = float(value)
                except ValueError:
                    pass
        flush()
        return cls(groups, delays)

    @staticmethod
    def product_token(user_agent: str) -> str:
        return re.split(r"[/\s]", user_agent.strip(), maxsplit=1)[0].lower()

    def _rules_for(self, user_agent: str) -> list[RobotsRule]:
        # A group applies when its agent name occurs anywhere in our product
        # token: the original robots.txt convention, also used by Python's
        # urllib.robotparser. It is stricter than RFC 9309's exact token match,
        # so a named block such as "harvest" is honoured for any
        # "...Harvester" crawler.
        product = self.product_token(user_agent)
        specific = [r for agents, r in self.groups if any(a != "*" and a in product for a in agents)]
        if specific:
            return [rule for group in specific for rule in group]
        return [rule for agents, r in self.groups if "*" in agents for rule in r]

    def allowed(self, user_agent: str, url: str) -> bool:
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best: RobotsRule | None = None
        for rule in self._rules_for(user_agent):
            if rule.regex.match(target):
                if (
                    best is None
                    or len(rule.pattern) > len(best.pattern)
                    or (len(rule.pattern) == len(best.pattern) and rule.allow)
                ):
                    best = rule
        return best is None or best.allow

    def crawl_delay(self, user_agent: str) -> float | None:
        product = self.product_token(user_agent)
        for agent, delay in self.crawl_delays.items():
            if agent != "*" and agent in product:
                return delay
        return self.crawl_delays.get("*")


# ----------------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------------


class PoliteClient:
    def __init__(
        self,
        settings: HttpSettings,
        transport: Transport,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ):
        self.settings = settings
        self.transport = transport
        self.sleep = sleep
        self.clock = clock
        self.rng = rng
        self._robots: dict[str, RobotsPolicy] = {}
        self._last_request: dict[str, float] = {}
        self.request_count = 0

    @property
    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.5",
            "Accept-Language": "de-DE,de;q=0.9",
        }

    # -- robots ---------------------------------------------------------------

    def robots_for(self, url: str) -> RobotsPolicy:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots:
            try:
                response = self._send(f"{host}/robots.txt")
            except FetchError as exc:
                log.warning("robots.txt unreachable for %s (%s); treating host as disallowed", host, exc)
                self._robots[host] = RobotsPolicy.disallow_all()
            else:
                if response.ok:
                    self._robots[host] = RobotsPolicy.parse(response.text)
                elif 400 <= response.status < 500:
                    self._robots[host] = RobotsPolicy.allow_all()
                else:
                    self._robots[host] = RobotsPolicy.disallow_all()
        return self._robots[host]

    # -- requests -------------------------------------------------------------

    def get(self, url: str) -> Response:
        if self.settings.respect_robots:
            policy = self.robots_for(url)
            if not policy.allowed(self.settings.user_agent, url):
                raise RobotsDisallowed("robots_disallowed", url, f"robots.txt disallows {url}")
        return self._send(url)

    def _throttle(self, host: str) -> None:
        interval = self.settings.min_interval_seconds
        policy = self._robots.get(host)
        if policy is not None:
            delay = policy.crawl_delay(self.settings.user_agent)
            if delay:
                interval = max(interval, delay)
        last = self._last_request.get(host)
        if last is not None:
            wait = interval - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_request[host] = self.clock()

    def _backoff(self, attempt: int, base: float) -> float:
        delay = base * (2**attempt)
        return delay + self.rng() * min(30.0, delay * 0.25)

    def _send(self, url: str) -> Response:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        retries = self.settings.max_retries
        for attempt in range(retries + 1):
            self._throttle(host)
            self.request_count += 1
            try:
                response = self.transport.get(url, self.headers, self.settings.timeout_seconds)
            except (TransportTimeout, TransportConnectionError) as exc:
                code = "timeout" if isinstance(exc, TransportTimeout) else "connection_error"
                if attempt >= retries:
                    raise FetchError(code, url, f"{code} after {attempt + 1} attempts: {exc}") from exc
                self.sleep(self._backoff(attempt, self.settings.backoff_base_seconds))
                continue

            if response.ok or 300 <= response.status < 400:
                return response
            if response.status == 429:
                if attempt >= retries:
                    raise FetchError("rate_limited", url, f"HTTP 429 after {attempt + 1} attempts", 429)
                self.sleep(self._retry_after(response, attempt))
                continue
            if response.status in RETRYABLE_STATUS:
                if attempt >= retries:
                    raise FetchError(
                        "server_error", url, f"HTTP {response.status} after {attempt + 1} attempts", response.status
                    )
                self.sleep(self._backoff(attempt, self.settings.backoff_base_seconds))
                continue
            # 4xx other than 429: not retryable, but robots.txt 4xx is handled by the caller.
            if url.endswith("/robots.txt"):
                return response
            raise FetchError(f"http_{response.status}", url, f"HTTP {response.status} for {url}", response.status)
        raise FetchError("retries_exhausted", url, f"gave up on {url}")  # pragma: no cover

    def _retry_after(self, response: Response, attempt: int) -> float:
        value = response.header("Retry-After")
        if value:
            try:
                seconds = float(value)
                if seconds >= 0:
                    return seconds
            except ValueError:
                pass
        return self._backoff(attempt, self.settings.rate_limit_base_seconds)
