import os
from pathlib import Path

import pytest

from ops_graph.ingest import ingest
from ops_graph.rag import HashingEmbedder, chunk_markdown, embed_documents, find_mentions, search_docs
from ops_graph.reconcile import reconcile

RUNBOOKS = Path(__file__).resolve().parent.parent / "data" / "runbooks"


@pytest.fixture(scope="module")
def known_assets(request):
    plan = request.getfixturevalue("plan")
    named = {p["name"]: i for label in ("Application", "Database") for i, p in plan.nodes[label].items()}
    servers = [{"id": i, "host_key": p["host_key"]} for i, p in plan.nodes["Server"].items()]
    return named, servers


# --- chunking and linking (no database) --------------------------------------------


def test_runbook_is_chunked_by_heading_with_title_context():
    title, chunks = chunk_markdown(RUNBOOKS / "db01-postgresql.md", "db01-postgresql.md")
    assert title == "Runbook: PostgreSQL primary SRV-PAR-DB01"
    assert [c.heading for c in chunks] == [
        "Overview", "What runs here", "Replication lag above 60 seconds", "Filesystem above 90%", "Failover",
    ]
    assert chunks[2].id == "db01-postgresql.md#2"
    assert chunks[2].text.startswith("Runbook: PostgreSQL primary SRV-PAR-DB01 / Replication lag")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SRV-PAR-DB01 (db01.par) hosts policy-db.", {"CI00301", "db:policy-db"}),
        ("errors come from document-store, check SRV-PAR-DB02.", {"db:document-store", "CI00302"}),
        ("queue on mq01.par.acme.local", {"UNREG-par/mq01"}),
        ("claims-api writes claims-db", {"app:claims-api", "db:claims-db"}),
        ("app01 is slow", set()),  # PAR or LYO: ambiguous, link nothing
        ("Windows Server 2012 R2", set()),  # digits, but not a host
        ("the claims team", set()),  # "claims" alone is not claims-api
    ],
)
def test_find_mentions(known_assets, text, expected):
    named, servers = known_assets
    assert find_mentions(text, named, servers) == expected


# --- against the database ----------------------------------------------------------


@pytest.fixture
def indexed(neo4j_driver, data_copy):
    ingest(neo4j_driver, data_copy, "b1")
    reconcile(neo4j_driver)
    stats = embed_documents(neo4j_driver, data_copy / "runbooks", HashingEmbedder())
    return neo4j_driver, stats


@pytest.mark.integration
def test_embedding_is_incremental(indexed, data_copy):
    driver, first = indexed
    assert first["documents"] == 5 and first["embedded"] == first["chunks"] > 0
    again = embed_documents(driver, data_copy / "runbooks", HashingEmbedder())
    assert again["embedded"] == 0
    (data_copy / "runbooks" / "claims-api.md").write_text("# Runbook: claims-api\n\n## Note\n\nRetired.\n", encoding="utf-8")
    changed = embed_documents(driver, data_copy / "runbooks", HashingEmbedder())
    assert changed["embedded"] == 1
    assert driver.execute_query(
        "MATCH (:Document {path: 'claims-api.md'})-[:HAS_CHUNK]->(c) RETURN count(c)"
    ).records[0][0] == 1


@pytest.mark.integration
def test_search_returns_the_passage_and_the_live_graph_around_it(indexed):
    driver, _ = indexed
    hits = search_docs(driver, "postgresql replication lag primary", HashingEmbedder(), k=3)
    top = hits[0]
    assert (top["source"], top["heading"]) == ("db01-postgresql.md", "Replication lag above 60 seconds")
    db01 = next(a for a in hits[0]["assets"] + hits[1]["assets"] + hits[2]["assets"] if a["id"] == "CI00301")
    assert {a["id"] for a in db01["alerts"]} == {"ALR-9001", "ALR-9002"}
    assert {"policy-db", "claims-db"} <= set(db01["used_by"])


@pytest.mark.integration
def test_mentions_follow_a_server_when_it_is_registered(indexed, data_copy):
    driver, _ = indexed
    query = "MATCH (:Document {path: 'notification-queue.md'})-[:HAS_CHUNK]->()-[:MENTIONS]->(s:Server) RETURN DISTINCT s.id"
    assert [r[0] for r in driver.execute_query(query).records] == ["UNREG-par/mq01"]

    with (data_copy / "cmdb.csv").open("a", encoding="utf-8") as f:
        f.write("CI00801,SRV-PAR-MQ01,10.10.8.81,PROD,PAR,RHEL 9,2032-05-31,In Service,Middleware Ops,2026-11-16\n")
    ingest(driver, data_copy, "b2")
    reconcile(driver)
    assert [r[0] for r in driver.execute_query(query).records] == ["CI00801"]


@pytest.mark.integration
def test_real_ollama_embedder_when_available():
    from ops_graph.rag import make_embedder

    try:
        vector = make_embedder().embed_query("replication lag on the primary database")
    except Exception as exc:
        pytest.skip(f"Ollama or {os.environ.get('EMBED_MODEL', 'nomic-embed-text')} not available: {exc}")
    assert len(vector) == 768
