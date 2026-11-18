"""build_plan on the demo data: every trap in the sources lands where it should."""


def rels(plan, rel_type):
    return {(r["start"], r["end"]): r["props"] for r in plan.rels[rel_type]}


def describes(plan, source, key):
    return [d for d in plan.describes if (d["source"], d["key"]) == (source, key)]


def issues(plan, kind):
    return {(i["key"], i["value"]): i["candidates"] for i in plan.issues if i["kind"] == kind}


def test_servers_are_the_cmdb_plus_one_shadow_host(plan):
    servers = plan.nodes["Server"]
    assert len(servers) == 13
    assert servers["UNREG-par/mq01"]["in_cmdb"] is False
    assert sum(s["in_cmdb"] for s in servers.values()) == 12


def test_monitoring_hosts_resolve_by_ip_or_by_name(plan):
    assert describes(plan, "monitoring", "host:web01.par.acme.local")[0]["rule"] == "ip_exact"
    for host, ci in [("web02.par.acme.local", "CI00102"), ("app-02.par.acme.local", "CI00202"), ("db1.lyo.acme.local", "CI00303")]:
        (d,) = describes(plan, "monitoring", f"host:{host}")
        assert (d["entity"], d["rule"]) == (ci, "name_site")


def test_monitoring_enriches_the_cmdb_server(plan):
    ftp = plan.nodes["Server"]["CI00601"]
    assert ftp["monitored"] is True
    assert ftp["last_seen"].date().isoformat() == "2026-06-02"


def test_service_on_a_shadow_host_is_still_placed(plan):
    # catalog says notification-service runs on mq01, which only monitoring knows
    assert ("app:notification-service", "UNREG-par/mq01") in rels(plan, "RUNS_ON")
    assert ("ALR-9005", "UNREG-par/mq01") in rels(plan, "RAISED_ON")
    assert ("host:mq01.par.acme.local", "mq01.par.acme.local") in issues(plan, "not_in_cmdb")


def test_ambiguous_placement_becomes_maybe_runs_on_on_both_candidates(plan):
    maybe = rels(plan, "MAYBE_RUNS_ON")
    assert set(maybe) == {("app:fraud-scoring", "CI00201"), ("app:fraud-scoring", "CI00203")}
    assert all(p["needs_review"] for p in maybe.values())
    assert not [k for k in rels(plan, "RUNS_ON") if k[0] == "app:fraud-scoring"]
    assert issues(plan, "ambiguous_host")[("service:fraud-scoring", "app01")] == ["CI00201", "CI00203"]


def test_typo_placement_carries_its_confidence(plan):
    props = rels(plan, "RUNS_ON")[("app:billing-batch", "CI00401")]
    assert props == {"rule": "fuzzy_stem", "confidence": 0.6, "needs_review": True}


def test_every_database_lands_on_its_server(plan):
    hosted = {start: end for start, end in rels(plan, "HOSTED_ON")}
    assert hosted == {
        "db:policy-db": "CI00301",
        "db:claims-db": "CI00301",
        "db:document-store": "CI00302",
        "db:crm-db": "CI00303",
    }


def test_alerts_attach_to_hosts_and_services(plan):
    raised = rels(plan, "RAISED_ON")
    assert ("ALR-9002", "CI00301") in raised
    assert ("ALR-9003", "app:claims-api") in raised
    assert len(raised) == 6


def test_cmdb_support_groups_join_catalog_teams_by_name(plan):
    teams = plan.nodes["Team"]
    assert ("team:digital", "CI00101") in rels(plan, "SUPPORTS")  # "Digital Channels" in both sources
    assert "team:middleware-ops" in teams  # only the CMDB knows this one


def test_every_relationship_points_at_a_planned_entity(plan):
    ids = plan.ids()
    for rel_list in plan.rels.values():
        for r in rel_list:
            assert r["start"] in ids and r["end"] in ids
    assert all(d["entity"] in ids for d in plan.describes)


def test_clean_data_raises_only_the_planted_issues(plan):
    kinds = sorted(i["kind"] for i in plan.issues)
    assert kinds == ["ambiguous_host", "not_in_cmdb"]
