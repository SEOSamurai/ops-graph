"""The questions an agent asks, answered on the demo data (test Neo4j)."""

import pytest
import yaml

from ops_graph import queries
from ops_graph.ingest import ingest
from ops_graph.reconcile import reconcile

pytestmark = pytest.mark.integration


@pytest.fixture
def db(neo4j_driver, data_copy):
    ingest(neo4j_driver, data_copy, "b1")
    reconcile(neo4j_driver)
    return neo4j_driver


def by_id(rows):
    return {r["id"]: r for r in rows}


# --- find --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("db01.par", ["CI00301"]),
        ("SRV-PAR-DB01", ["CI00301"]),
        ("claims-api", ["app:claims-api"]),
        ("CI00202", ["CI00202"]),
        ("app01", ["CI00201", "CI00203"]),  # two sites: the caller must choose
        ("mq01", ["UNREG-par/mq01"]),
        ("nothing-here", []),
    ],
)
def test_find_assets(db, term, expected):
    assert [a["id"] for a in queries.find_assets(db, term)] == expected


# --- impact ------------------------------------------------------------------------


def test_impact_of_stopping_db01(db):
    impacted = by_id(queries.impact_analysis(db, "CI00301"))
    assert set(impacted) == {
        "db:policy-db", "db:claims-db",
        "app:quote-engine", "app:policy-admin", "app:billing-batch", "app:old-reporting",
        "app:claims-api", "app:fraud-scoring",
        "app:customer-portal",
    }
    assert impacted["db:claims-db"]["distance"] == 1
    assert impacted["app:customer-portal"]["distance"] == 3
    assert impacted["app:customer-portal"]["path"][0] == "customer-portal"
    assert impacted["app:customer-portal"]["path"][-1] == "SRV-PAR-DB01"


def test_impact_through_an_ambiguous_placement_is_flagged_uncertain(db):
    impacted = by_id(queries.impact_analysis(db, "CI00203"))  # SRV-LYO-APP01
    assert impacted["app:policy-admin"]["uncertain"] is False
    assert impacted["app:fraud-scoring"]["uncertain"] is True
    assert impacted["app:fraud-scoring"]["confidence"] == 0.0


def test_impact_through_a_fuzzy_match_needs_review(db):
    impacted = by_id(queries.impact_analysis(db, "CI00401"))  # batch01, matched from "bacth01"
    assert impacted["app:billing-batch"]["needs_review"] is True
    assert impacted["app:billing-batch"]["confidence"] == 0.6


def test_impact_depth_is_bounded(db):
    assert {r["distance"] for r in queries.impact_analysis(db, "CI00301", max_depth=1)} == {1}
    assert queries.impact_analysis(db, "CI00301", max_depth=99) == queries.impact_analysis(db, "CI00301")


def test_impact_of_an_unknown_id_is_empty(db):
    assert queries.impact_analysis(db, "nope") == []


# --- root cause --------------------------------------------------------------------


def test_root_cause_for_the_slow_portal_ranks_db01_first(db):
    ranked = queries.root_cause_candidates(db, "app:customer-portal")
    assert [r["id"] for r in ranked] == ["CI00301", "UNREG-par/mq01", "app:claims-api", "app:customer-portal"]
    db01 = ranked[0]
    assert db01["deepest"] is True
    assert [a["id"] for a in db01["alerts"]] == ["ALR-9001", "ALR-9002"]
    # three equally short paths reach db01; the evidence is the one through the alerting claims-api
    assert db01["path"] == ["customer-portal", "claims-api", "claims-db", "SRV-PAR-DB01"]
    assert ranked[2]["deepest"] is False  # claims-api alerts sit on top of alerting dependencies


def test_root_cause_includes_the_asset_itself(db):
    ranked = queries.root_cause_candidates(db, "CI00301")
    assert [(r["id"], r["distance"]) for r in ranked] == [("CI00301", 0)]


def test_only_alerting_dependencies_are_candidates(db):
    # old-reporting -> policy-db -> db01: db01 is the only alerting node below it
    assert [r["id"] for r in queries.root_cause_candidates(db, "app:old-reporting")] == ["CI00301"]
    # web01 depends on nothing and raises nothing
    assert queries.root_cause_candidates(db, "CI00101") == []


# --- decommission ------------------------------------------------------------------


def test_decommission_candidates_and_reasons(db):
    result = queries.decommission_candidates(db)
    found = {r["id"]: r["reasons"] for r in result["candidates"]}
    assert found == {
        "CI00303": ["only_unused_databases"],  # hosts crm-db, which nothing calls
        "CI00501": ["no_workload", "retired_in_cmdb_but_still_running"],  # legacy01 zombie
        "CI00601": ["no_workload", "not_seen_by_monitoring_for_165_days"],  # ftp01
        "CI00701": ["only_retired_applications"],  # report01 runs old-reporting only
    }
    assert result["blocked"] == []


def test_unknown_workload_blocks_decommissioning(neo4j_driver, data_copy):
    # move policy-admin away: SRV-LYO-APP01 is left with fraud-scoring's MAYBE_RUNS_ON only
    catalog = yaml.safe_load((data_copy / "services.yaml").read_text(encoding="utf-8"))
    for s in catalog["services"]:
        if s["name"] == "policy-admin":
            s["hosts"] = ["app02.par"]
    (data_copy / "services.yaml").write_text(yaml.safe_dump(catalog), encoding="utf-8")
    ingest(neo4j_driver, data_copy, "b1")
    reconcile(neo4j_driver)

    result = queries.decommission_candidates(neo4j_driver)
    assert "CI00203" not in {r["id"] for r in result["candidates"]}
    assert [r["id"] for r in result["blocked"]] == ["CI00203"]


# --- compliance --------------------------------------------------------------------


def test_compliance_findings(db):
    rows = {(r["finding"], r["asset"], r["server"]): r for r in queries.compliance_findings(db)}
    assert set(rows) == {
        ("personal_data_on_unregistered_server", "notification-service", "mq01.par.acme.local"),
        ("personal_data_on_unsupported_os", "billing-batch", "SRV-PAR-BATCH01"),
        ("personal_data_on_unsupported_os", "document-store", "SRV-PAR-DB02"),
        ("personal_data_on_unsupported_os", "fraud-scoring", "SRV-LYO-APP01"),
        ("personal_data_on_unsupported_os", "policy-admin", "SRV-LYO-APP01"),
    }
    assert rows[("personal_data_on_unsupported_os", "billing-batch", "SRV-PAR-BATCH01")]["needs_review"] is True
    assert rows[("personal_data_on_unsupported_os", "fraud-scoring", "SRV-LYO-APP01")]["uncertain"] is True
    assert rows[("personal_data_on_unsupported_os", "document-store", "SRV-PAR-DB02")]["needs_review"] is False


# --- quality and schema ------------------------------------------------------------


def test_reconciliation_report(db):
    report = queries.reconciliation_report(db)
    assert {i["problem"] for i in report["issues"]} == {"ambiguous_host", "not_in_cmdb"}
    assert {(r["asset"], r["rule"]) for r in report["needs_review"]} >= {("billing-batch", "fuzzy_stem")}
    rules = {r["rule"]: r["links"] for r in report["links_by_rule"]}
    assert rules["ip_exact"] >= 1 and rules["name_site"] >= 1


def test_describe_schema(db):
    schema = queries.describe_schema(db)
    assert {"label": "Server", "count": 13} in schema["nodes"]
    assert any("point from the thing that needs" in c for c in schema["conventions"])
