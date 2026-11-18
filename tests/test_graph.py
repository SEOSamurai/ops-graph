"""Two-stage pipeline against a real Neo4j (test container on port 7688)."""

import pytest

from ops_graph.ingest import ingest, read_sources, write_batch
from ops_graph.reconcile import reconcile

pytestmark = pytest.mark.integration


def count(driver, query, **params):
    return driver.execute_query(query, **params).records[0][0]


def snapshot(driver):
    return (
        count(driver, "MATCH (n) RETURN count(n)"),
        count(driver, "MATCH ()-[r]->() RETURN count(r)"),
    )


def test_ingest_writes_raw_records_only(neo4j_driver, data_copy):
    ingest(neo4j_driver, data_copy, "b1")
    assert count(neo4j_driver, "MATCH (r:SourceRecord) RETURN count(r)") == 12 + 19 + 17
    assert count(neo4j_driver, "MATCH (e:Entity) RETURN count(e)") == 0


def test_run_twice_changes_nothing(neo4j_driver, data_copy):
    ingest(neo4j_driver, data_copy, "b1")
    reconcile(neo4j_driver)
    first = snapshot(neo4j_driver)
    ingest(neo4j_driver, data_copy, "b2")
    reconcile(neo4j_driver)
    # only the new Batch nodes differ
    assert snapshot(neo4j_driver) == (first[0] + 3, first[1])
    assert count(neo4j_driver, "MATCH (r:SourceRecord) WHERE r.changed RETURN count(r)") == 0


def test_reconcile_alone_replays_from_the_graph(neo4j_driver, data_copy):
    ingest(neo4j_driver, data_copy, "b1")
    reconcile(neo4j_driver)
    first = snapshot(neo4j_driver)
    reconcile(neo4j_driver)  # no file read at all
    assert snapshot(neo4j_driver) == first


def test_shadow_server_registered_later_becomes_one_node(neo4j_driver, data_copy):
    ingest(neo4j_driver, data_copy, "b1")
    reconcile(neo4j_driver)
    assert count(neo4j_driver, "MATCH (s:Server {id: 'UNREG-par/mq01'}) RETURN count(s)") == 1

    with (data_copy / "cmdb.csv").open("a", encoding="utf-8") as f:
        f.write("CI00801,SRV-PAR-MQ01,10.10.8.81,PROD,PAR,RHEL 9,2032-05-31,In Service,Middleware Ops,2026-11-16\n")
    ingest(neo4j_driver, data_copy, "b2")
    reconcile(neo4j_driver)

    rows = neo4j_driver.execute_query(
        """
        MATCH (s:Server) WHERE s.host_key = 'par/mq01'
        OPTIONAL MATCH (a:Alert)-[:RAISED_ON]->(s)
        OPTIONAL MATCH (app:Application)-[:RUNS_ON]->(s)
        RETURN s.id AS id, collect(DISTINCT a.id) AS alerts, collect(DISTINCT app.name) AS apps
        """
    ).records
    assert [(r["id"], r["alerts"], r["apps"]) for r in rows] == [("CI00801", ["ALR-9005"], ["notification-service"])]


def test_server_removed_from_cmdb_but_still_monitored_turns_shadow(neo4j_driver, data_copy):
    ingest(neo4j_driver, data_copy, "b1")
    reconcile(neo4j_driver)
    cmdb = data_copy / "cmdb.csv"
    cmdb.write_text("".join(l for l in cmdb.read_text(encoding="utf-8").splitlines(True) if "FTP01" not in l), encoding="utf-8")
    ingest(neo4j_driver, data_copy, "b2")
    reconcile(neo4j_driver)

    assert count(neo4j_driver, "MATCH (r:SourceRecord {source_key: 'CI00601'}) WHERE r.stale RETURN count(r)") == 1
    assert count(neo4j_driver, "MATCH (s:Server {id: 'CI00601'}) RETURN count(s)") == 0
    assert count(neo4j_driver, "MATCH (s:Server {id: 'UNREG-lyo/ftp01', in_cmdb: false}) RETURN count(s)") == 1


def test_failed_ingest_marks_nothing_stale(neo4j_driver, data_copy):
    ingest(neo4j_driver, data_copy, "b1")
    reconcile(neo4j_driver)
    before = snapshot(neo4j_driver)

    cmdb = next(b for b in read_sources(data_copy) if b.source == "cmdb")
    rows = [r.to_row() for r in cmdb.records][:3] + [{"key": None, "kind": "server", "payload": "{}", "hash": "x"}]
    with pytest.raises(Exception):
        write_batch(neo4j_driver, cmdb, "b2-broken", rows)

    assert count(neo4j_driver, "MATCH (r:SourceRecord) WHERE r.stale RETURN count(r)") == 0
    assert count(neo4j_driver, "MATCH (r:SourceRecord {batch: 'b2-broken'}) RETURN count(r)") == 0
    assert count(neo4j_driver, "MATCH (b:Batch {id: 'b2-broken'}) RETURN count(b)") == 0
    assert snapshot(neo4j_driver) == before


def test_provenance_answers_where_a_fact_comes_from(neo4j_driver, data_copy):
    ingest(neo4j_driver, data_copy, "b1")
    reconcile(neo4j_driver)
    rows = neo4j_driver.execute_query(
        """
        MATCH (r:SourceRecord)-[d:DESCRIBES]->(s:Server {id: 'CI00202'})
        RETURN r.source AS source, d.rule AS rule ORDER BY source
        """
    ).records
    assert [(r["source"], r["rule"]) for r in rows] == [("cmdb", "cmdb_master"), ("monitoring", "name_site")]
