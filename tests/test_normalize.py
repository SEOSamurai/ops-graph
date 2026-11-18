import pytest

from ops_graph.normalize import HostKey, parse_host


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # CMDB style: prefix, site infix, zero padding
        ("SRV-PAR-APP02", HostKey("app", 2, "par")),
        ("SRV-LYO-DB01", HostKey("db", 1, "lyo")),
        ("SRV-PAR-LEGACY01", HostKey("legacy", 1, "par")),
        # monitoring style: FQDN, site as a label, hyphen inside the host
        ("app01.par.acme.local", HostKey("app", 1, "par")),
        ("app-02.par.acme.local", HostKey("app", 2, "par")),
        ("db1.lyo.acme.local", HostKey("db", 1, "lyo")),
        # catalog style: whatever the team typed
        ("srv-par-app02", HostKey("app", 2, "par")),
        ("db01.par", HostKey("db", 1, "par")),
        ("db1-lyo", HostKey("db", 1, "lyo")),
        ("app01", HostKey("app", 1, None)),
        ("  Web02 ", HostKey("web", 2, None)),
        # no trailing number
        ("gateway.par", HostKey("gateway", None, "par")),
    ],
)
def test_every_spelling_maps_to_one_key(raw, expected):
    assert parse_host(raw) == expected


def test_zero_padding_does_not_change_identity():
    assert parse_host("app01") == parse_host("app1") == parse_host("APP-001")


def test_sibling_numbers_stay_distinct():
    assert parse_host("web01") != parse_host("web11")


@pytest.mark.parametrize("raw", ["", "srv", "par", "SRV-PAR", ".acme.local"])
def test_spellings_with_no_host_part_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_host(raw)


def test_two_sites_in_one_name_is_rejected():
    with pytest.raises(ValueError, match="several sites"):
        parse_host("app01-par-lyo")


def test_site_hint_fills_missing_site_only():
    assert parse_host("app01").with_site("PAR") == HostKey("app", 1, "par")
    assert parse_host("app01.lyo").with_site("par") == HostKey("app", 1, "lyo")


def test_display_name():
    assert str(parse_host("SRV-PAR-APP02")) == "par/app02"
    assert str(parse_host("app1")) == "app01"
