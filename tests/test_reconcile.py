import pytest

from ops_graph.normalize import parse_host
from ops_graph.reconcile import Match, ServerCandidate, match_host


def server(id_: str, hostname: str, ip: str | None = None) -> ServerCandidate:
    return ServerCandidate(id_, parse_host(hostname), ip)


CMDB = [
    server("CI00101", "SRV-PAR-WEB01", "10.10.1.11"),
    server("CI00102", "SRV-PAR-WEB02", "10.10.1.12"),
    server("CI00201", "SRV-PAR-APP01", "10.10.2.21"),
    server("CI00202", "SRV-PAR-APP02", "10.10.2.22"),
    server("CI00203", "SRV-LYO-APP01", "10.20.2.21"),
    server("CI00301", "SRV-PAR-DB01", "10.10.3.31"),
    server("CI00303", "SRV-LYO-DB01", "10.20.3.31"),
    server("CI00401", "SRV-PAR-BATCH01", "10.10.4.41"),
]


def test_ip_wins_even_when_the_name_disagrees():
    m = match_host("totally-renamed.par.acme.local", CMDB, ip="10.10.1.11")
    assert m == Match("CI00101", "ip_exact", 1.0, False)


def test_unknown_ip_falls_through_to_name_rules():
    m = match_host("web02.par.acme.local", CMDB, ip="10.99.99.99")
    assert (m.server_id, m.rule) == ("CI00102", "name_site")


@pytest.mark.parametrize(
    ("raw", "site_hint", "expected_id"),
    [
        ("web02.par.acme.local", None, "CI00102"),
        ("app-02.par.acme.local", None, "CI00202"),
        ("db1.lyo.acme.local", None, "CI00303"),
        ("srv-par-app02", None, "CI00202"),
        ("db01.par", None, "CI00301"),
        ("db1-lyo", None, "CI00303"),
        ("app01", "par", "CI00201"),  # catalog entry declares site: par
    ],
)
def test_name_and_site_match(raw, site_hint, expected_id):
    m = match_host(raw, CMDB, site_hint=site_hint)
    assert m == Match(expected_id, "name_site", 0.9, False)


def test_name_without_site_resolves_when_only_one_server_fits():
    m = match_host("web01", CMDB)
    assert m == Match("CI00101", "name_unique", 0.8, False)


def test_name_without_site_is_ambiguous_when_two_sites_have_it():
    # fraud-scoring declares hosts: [app01] and no site
    m = match_host("app01", CMDB)
    assert m.rule == "ambiguous"
    assert m.server_id is None
    assert m.needs_review
    assert m.candidates == ("CI00201", "CI00203")


def test_fuzzy_catches_a_typo_in_the_letters():
    # billing-batch declares hosts: [bacth01], site: par
    m = match_host("bacth01", CMDB, site_hint="par")
    assert m == Match("CI00401", "fuzzy_stem", 0.6, True)


def test_fuzzy_never_links_numbered_siblings():
    # web01 vs web11: 89% similar as whole strings, different machines
    only_web11 = [server("CI09911", "SRV-PAR-WEB11")]
    assert match_host("web01", only_web11, site_hint="par").rule == "unresolved"


def test_fuzzy_never_links_a_different_number():
    only_app02 = [server("CI00202", "SRV-PAR-APP02")]
    assert match_host("app01", only_app02, site_hint="par").rule == "unresolved"


def test_fuzzy_needs_a_known_site():
    assert match_host("bacth01", CMDB).rule == "unresolved"


def test_fuzzy_does_not_cross_sites():
    assert match_host("bacth01", CMDB, site_hint="lyo").rule == "unresolved"


def test_nothing_matches():
    m = match_host("mq01.par.acme.local", CMDB, ip="10.10.8.81")
    assert m == Match(None, "unresolved", 0.0, True)
    assert not m.resolved


def test_duplicate_ip_is_ambiguous_not_a_guess():
    clash = CMDB + [server("CI09999", "SRV-PAR-OLDWEB01", "10.10.1.11")]
    m = match_host("whatever", clash, ip="10.10.1.11")
    assert m.rule == "ambiguous"
    assert m.candidates == ("CI00101", "CI09999")
