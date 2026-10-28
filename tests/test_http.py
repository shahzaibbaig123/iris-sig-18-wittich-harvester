import pytest

from conftest import fixture_text
from wittich_harvester.config import HttpSettings, user_agent
from wittich_harvester.http import FetchError, PoliteClient, RobotsDisallowed, RobotsPolicy
from wittich_harvester.transport import FixtureRoute, FixtureTransport

UA = "IRIS-GazetteBot/0.1 (+mailto:ops@example.org)"
OL_ROBOTS = RobotsPolicy.parse(fixture_text("robots/ol_robots.txt"))
WWW_ROBOTS = RobotsPolicy.parse(fixture_text("robots/www_robots.txt"))


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def client(routes, **http):
    clock = FakeClock()
    settings = HttpSettings(user_agent=UA, **{"min_interval_seconds": 1.0, "backoff_base_seconds": 2.0, **http})
    transport = FixtureTransport({"/robots.txt": [FixtureRoute(body="")], **routes})
    return PoliteClient(settings, transport, sleep=clock.sleep, clock=clock.clock, rng=lambda: 0.0), transport, clock


# -- robots.txt -----------------------------------------------------------------


def test_ol_allows_our_agent_but_blocks_named_bots():
    url = "https://ol.wittich.de/titel/130"
    assert OL_ROBOTS.allowed(UA, url)
    assert not OL_ROBOTS.allowed("Python-urllib/3.12", url)
    assert not OL_ROBOTS.allowed("Scrapy/2.11", url)


def test_harvest_and_es_groups_block_substring_matches():
    # ol.wittich.de blocks agents "harvest" and "es"; the conservative
    # substring rule therefore refuses these names (see README).
    url = "https://ol.wittich.de/titel/130"
    assert not OL_ROBOTS.allowed("IRIS-SignalHarvester/0.1", url)
    assert not OL_ROBOTS.allowed("python-requests/2.32", url)


def test_www_forbids_query_strings():
    assert not WWW_ROBOTS.allowed(UA, "https://www.wittich.de/suche?q=amtsblatt")
    assert WWW_ROBOTS.allowed(UA, "https://www.wittich.de/produkte/zeitungen/amtsblaetter")
    assert WWW_ROBOTS.allowed(UA, "https://www.wittich.de/typo3conf/ext/x.css")


def test_robots_longest_match_and_anchor():
    policy = RobotsPolicy.parse("User-agent: *\nDisallow: /a\nAllow: /a/b\nDisallow: /*.pdf$\n")
    assert not policy.allowed(UA, "https://h/a/x")
    assert policy.allowed(UA, "https://h/a/b/c")
    assert not policy.allowed(UA, "https://h/x/file.pdf")
    assert policy.allowed(UA, "https://h/x/file.pdf?download=1")


def test_empty_disallow_allows_everything():
    assert RobotsPolicy.parse("User-agent: *\nDisallow:\n").allowed(UA, "https://h/anything")


def test_robots_disallowed_url_is_never_requested():
    c, transport, _ = client({"/robots.txt": [FixtureRoute(body="User-agent: *\nDisallow: /private\n")]})
    with pytest.raises(RobotsDisallowed):
        c.get("https://h.example/private/page")
    assert transport.calls == ["/robots.txt"]


def test_unreachable_robots_means_disallow_all():
    c, transport, _ = client({"/robots.txt": [FixtureRoute(status=503)], "/page": [FixtureRoute(body="ok")]}, max_retries=1)
    with pytest.raises(RobotsDisallowed):
        c.get("https://h.example/page")
    assert "/page" not in transport.calls


def test_missing_robots_means_allow_all():
    c, _, _ = client({"/robots.txt": [FixtureRoute(status=404)], "/page": [FixtureRoute(body="ok")]})
    assert c.get("https://h.example/page").text == "ok"


def test_user_agent_contact_is_optional():
    assert user_agent("Bot/0.1 (+mailto:{contact})", "ops@example.org") == "Bot/0.1 (+mailto:ops@example.org)"
    assert user_agent("Bot/0.1 (+mailto:{contact})", "") == "Bot/0.1"


# -- retries and throttling -------------------------------------------------------


def test_server_errors_are_retried_with_backoff():
    c, transport, clock = client({"/p": [FixtureRoute(status=500), FixtureRoute(status=503), FixtureRoute(body="ok")]})
    assert c.get("https://h.example/p").text == "ok"
    assert transport.counts["/p"] == 3
    assert [s for s in clock.sleeps if s >= 2.0] == [2.0, 4.0]  # 2 * 2**attempt, no jitter (rng=0)


def test_rate_limit_honours_retry_after():
    c, _, clock = client({"/p": [FixtureRoute(status=429, headers={"Retry-After": "7"}), FixtureRoute(body="ok")]})
    assert c.get("https://h.example/p").text == "ok"
    assert 7.0 in clock.sleeps


def test_timeouts_give_up_after_max_retries():
    c, transport, _ = client({"/p": [FixtureRoute(error="timeout")]}, max_retries=2)
    with pytest.raises(FetchError) as exc:
        c.get("https://h.example/p")
    assert exc.value.code == "timeout"
    assert transport.counts["/p"] == 3


def test_not_found_is_not_retried():
    c, transport, _ = client({})
    with pytest.raises(FetchError) as exc:
        c.get("https://h.example/missing")
    assert exc.value.code == "http_404"
    assert transport.counts["/missing"] == 1


def test_requests_to_one_host_are_spaced():
    c, _, clock = client({"/a": [FixtureRoute(body="a")], "/b": [FixtureRoute(body="b")]}, min_interval_seconds=1.5)
    c.get("https://h.example/a")
    c.get("https://h.example/b")
    # robots.txt, /a and /b: two waits of 1.5 s on the fake clock
    assert clock.sleeps == [1.5, 1.5]


def test_crawl_delay_raises_the_interval():
    c, _, clock = client(
        {"/robots.txt": [FixtureRoute(body="User-agent: *\nCrawl-delay: 5\n")], "/a": [FixtureRoute(body="a")]}
    )
    c.get("https://h.example/a")
    assert clock.sleeps == [5.0]
