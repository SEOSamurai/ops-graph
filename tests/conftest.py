import os
import shutil
from pathlib import Path

import pytest
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable

from ops_graph.ingest import read_sources
from ops_graph.reconcile import StoredRecord, build_plan

DATA = Path(__file__).resolve().parent.parent / "data"


def records_from(data_dir: Path) -> list[StoredRecord]:
    """The records ingest would write, as reconcile would read them back."""
    return [StoredRecord(r.source, r.key, r.kind, r.payload) for b in read_sources(data_dir) for r in b.records]


@pytest.fixture(scope="session")
def plan():
    return build_plan(records_from(DATA))


@pytest.fixture
def data_copy(tmp_path: Path) -> Path:
    """A writable copy of the demo sources, to simulate a later batch."""
    target = tmp_path / "data"
    shutil.copytree(DATA, target)
    return target


@pytest.fixture
def neo4j_driver():
    """Empty test database (docker compose --profile test up -d neo4j-test)."""
    driver = GraphDatabase.driver(
        os.environ.get("NEO4J_TEST_URI", "bolt://localhost:7688"),
        auth=("neo4j", os.environ.get("NEO4J_TEST_PASSWORD", "ops-graph-test")),
    )
    try:
        driver.verify_connectivity()
    except (ServiceUnavailable, OSError) as exc:
        driver.close()
        pytest.skip(f"test Neo4j not reachable: {exc}")
    driver.execute_query("MATCH (n) DETACH DELETE n")
    yield driver
    driver.close()
