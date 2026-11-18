"""Stage 1: ingestion.

Each source file is parsed into raw records and written to Neo4j as
(:SourceRecord) nodes, untouched: no matching, no interpretation. That raw
layer is the provenance trail every canonical entity points back to, and it
lets reconciliation be replayed without reading the files again.

Publication rule (incremental sync): one source = one transaction.
Inside it we upsert every record of the batch, mark the source's records
that are absent from this batch as stale, and stamp a (:Batch) node as
completed. If anything fails, the transaction rolls back and nothing is
marked stale: a half-read file can never make servers "disappear".
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from neo4j import Driver, ManagedTransaction


@dataclass(frozen=True)
class Record:
    """One raw row from one source, as written to (:SourceRecord)."""

    source: str
    key: str
    kind: str
    payload: dict[str, Any]

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.payload, sort_keys=True).encode()).hexdigest()

    def to_row(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "payload": json.dumps(self.payload, sort_keys=True), "hash": self.content_hash}


@dataclass(frozen=True)
class SourceBatch:
    source: str
    records: list[Record]
    as_of: str | None = None  # when the source system produced the export, if it says


# --- parsers: one per source, file in, records out ------------------------------------


def read_cmdb(path: Path) -> SourceBatch:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return SourceBatch("cmdb", [Record("cmdb", row["ci_id"], "server", row) for row in rows])


def read_monitoring(path: Path) -> SourceBatch:
    data = json.loads(path.read_text(encoding="utf-8"))
    records = [Record("monitoring", f"host:{h['host']}", "host", h) for h in data["hosts"]]
    records += [Record("monitoring", f"alert:{a['id']}", "alert", a) for a in data["alerts"]]
    return SourceBatch("monitoring", records, as_of=data.get("exported_at"))


def read_catalog(path: Path) -> SourceBatch:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    records = [Record("catalog", f"team:{t['id']}", "team", t) for t in data.get("teams", [])]
    records += [Record("catalog", f"service:{s['name']}", "service", s) for s in data.get("services", [])]
    records += [Record("catalog", f"database:{d['name']}", "database", d) for d in data.get("databases", [])]
    return SourceBatch("catalog", records)


SOURCES = {
    "cmdb": ("cmdb.csv", read_cmdb),
    "monitoring": ("monitoring.json", read_monitoring),
    "catalog": ("services.yaml", read_catalog),
}


def read_sources(data_dir: Path) -> list[SourceBatch]:
    """Parse every source file. Parsing happens before any write."""
    return [reader(data_dir / filename) for filename, reader in SOURCES.values()]


# --- writes --------------------------------------------------------------------------

SCHEMA = [
    "CREATE CONSTRAINT source_record_key IF NOT EXISTS FOR (r:SourceRecord) REQUIRE (r.source, r.source_key) IS UNIQUE",
    "CREATE CONSTRAINT batch_key IF NOT EXISTS FOR (b:Batch) REQUIRE (b.source, b.id) IS UNIQUE",
    "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
    "CREATE INDEX asset_name IF NOT EXISTS FOR (a:Asset) ON (a.name)",
    "CREATE INDEX server_host_key IF NOT EXISTS FOR (s:Server) ON (s.host_key)",
]


def ensure_schema(driver: Driver) -> None:
    for statement in SCHEMA:
        driver.execute_query(statement)


# Upsert every record of the batch in one round trip (UNWIND), and remember
# whether its content changed since the previous batch.
_UPSERT_RECORDS = """
UNWIND $rows AS row
MERGE (r:SourceRecord {source: $source, source_key: row.key})
SET r.changed = coalesce(r.hash, '') <> row.hash,
    r.kind = row.kind,
    r.payload = row.payload,
    r.hash = row.hash,
    r.batch = $batch_id,
    r.stale = false,
    r.first_seen = coalesce(r.first_seen, datetime()),
    r.last_seen = datetime()
RETURN count(r) AS written, sum(CASE WHEN r.changed THEN 1 ELSE 0 END) AS changed
"""

# A record of this source that the batch did not mention is gone from the source.
_MARK_STALE = """
MATCH (r:SourceRecord {source: $source})
WHERE r.batch <> $batch_id AND NOT r.stale
SET r.stale = true
RETURN count(r) AS gone
"""

_COMPLETE_BATCH = """
MERGE (b:Batch {source: $source, id: $batch_id})
SET b.completed_at = datetime(),
    b.as_of = CASE WHEN $as_of IS NULL THEN null ELSE datetime($as_of) END,
    b.records = $written, b.changed = $changed, b.gone = $gone
"""


def _write_batch_tx(tx: ManagedTransaction, source: str, batch_id: str, rows: list[dict], as_of: str | None) -> dict:
    counts = tx.run(_UPSERT_RECORDS, source=source, batch_id=batch_id, rows=rows).single().data()
    counts["changed"] = counts["changed"] or 0
    counts["gone"] = tx.run(_MARK_STALE, source=source, batch_id=batch_id).single()["gone"]
    tx.run(_COMPLETE_BATCH, source=source, batch_id=batch_id, as_of=as_of, **counts)
    return counts


def write_batch(driver: Driver, batch: SourceBatch, batch_id: str, rows: list[dict] | None = None) -> dict:
    """Publish one source batch atomically. Returns written/changed/gone counts."""
    with driver.session() as session:
        return session.execute_write(
            _write_batch_tx, batch.source, batch_id, rows if rows is not None else [r.to_row() for r in batch.records], batch.as_of
        )


def new_batch_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def ingest(driver: Driver, data_dir: Path, batch_id: str | None = None) -> dict[str, dict]:
    batch_id = batch_id or new_batch_id()
    ensure_schema(driver)
    return {batch.source: write_batch(driver, batch, batch_id) for batch in read_sources(data_dir)}
