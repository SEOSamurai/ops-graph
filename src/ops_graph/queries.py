"""Read-only Cypher behind every MCP tool.

Direction convention (docs/model.md): every dependency edge points from the
thing that needs to the thing it needs:

    (Application)-[:DEPENDS_ON]->(Application|Database)
    (Application)-[:RUNS_ON | :MAYBE_RUNS_ON]->(Server)
    (Database)-[:HOSTED_ON]->(Server)

So "what breaks if X stops" walks edges INTO X, and "why is X failing"
walks edges OUT of X. Every traversal is bounded (*..6) so a cycle or a
large graph cannot run away.

Uncertainty from reconciliation travels with the answers: a path through a
MAYBE_RUNS_ON edge is `uncertain`, a path through an edge flagged
needs_review is `needs_review`, and `confidence` is the weakest link.
"""

from __future__ import annotations

from typing import Any

from neo4j import Driver, RoutingControl
from neo4j.time import Date, DateTime

from ops_graph.normalize import parse_host

DEPENDENCY = "DEPENDS_ON|RUNS_ON|HOSTED_ON|MAYBE_RUNS_ON"
MAX_DEPTH = 6

# Shared Cypher fragments: how sure are we about a path?
_PATH_QUALITY = """
  any(r IN relationships(p) WHERE type(r) = 'MAYBE_RUNS_ON') AS uncertain,
  any(r IN relationships(p) WHERE coalesce(r.needs_review, false)) AS needs_review,
  reduce(c = 1.0, r IN relationships(p) | CASE WHEN coalesce(r.confidence, 1.0) < c THEN coalesce(r.confidence, 1.0) ELSE c END) AS confidence
"""


def _plain(value: Any) -> Any:
    """Neo4j temporal types -> ISO strings, so results are JSON-ready."""
    if isinstance(value, (Date, DateTime)):
        return value.iso_format()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _read(driver: Driver, query: str, **params: Any) -> list[dict[str, Any]]:
    records, _, _ = driver.execute_query(query, params, routing_=RoutingControl.READ)
    return [_plain(r.data()) for r in records]


# --- finding things --------------------------------------------------------------------


def find_assets(driver: Driver, term: str) -> list[dict[str, Any]]:
    """Resolve what a human typed ("db01.par", "claims-api", "CI00301") to entities."""
    try:
        host_key = str(parse_host(term))
    except ValueError:
        host_key = None
    return _read(
        driver,
        """
        MATCH (a:Asset)
        WHERE a.id = $term OR toLower(a.name) = toLower($term)
           OR a.host_key = $host_key
           OR ($host_key IS NOT NULL AND NOT $host_key CONTAINS '/' AND a.host_key ENDS WITH '/' + $host_key)
        RETURN a.id AS id, a.name AS name, [l IN labels(a) WHERE NOT l IN ['Entity', 'Asset']][0] AS type
        ORDER BY id
        """,
        term=term,
        host_key=host_key,
    )


# --- impact ----------------------------------------------------------------------------


def impact_analysis(driver: Driver, asset_id: str, max_depth: int = MAX_DEPTH) -> list[dict[str, Any]]:
    """Everything that stops working if `asset_id` stops, with the path that proves it.

    When several paths exist, the certain one wins over an uncertain one,
    then the shortest, then the smallest ids (so the answer is reproducible).
    """
    depth = max(1, min(int(max_depth), MAX_DEPTH))
    return _read(
        driver,
        f"""
        MATCH (target:Entity {{id: $id}})
        MATCH p = (impacted:Asset)-[:{DEPENDENCY}*1..{depth}]->(target)
        WITH impacted, p, {_PATH_QUALITY}
        ORDER BY uncertain, length(p), [n IN nodes(p) | n.id]
        WITH impacted, collect({{p: p, uncertain: uncertain, needs_review: needs_review, confidence: confidence}})[0] AS best
        RETURN impacted.id AS id,
               impacted.name AS name,
               [l IN labels(impacted) WHERE NOT l IN ['Entity', 'Asset']][0] AS type,
               impacted.criticality AS criticality,
               impacted.personal_data AS personal_data,
               length(best.p) AS distance,
               [n IN nodes(best.p) | n.name] AS path,
               best.uncertain AS uncertain,
               best.needs_review AS needs_review,
               best.confidence AS confidence
        ORDER BY distance, name
        """,
        id=asset_id,
    )


# --- root cause ------------------------------------------------------------------------


def root_cause_candidates(driver: Driver, asset_id: str) -> list[dict[str, Any]]:
    """Alerting things `asset_id` depends on (itself included), most likely cause first.

    Ranking:
      1. `deepest`: the node alerts and nothing it depends on alerts, so its
         alerts cannot be an echo of a deeper failure;
      2. larger distance first (further down the stack);
      3. worst severity first;
      4. id, so ties are stable.
    """
    return _read(
        driver,
        f"""
        MATCH (start:Entity {{id: $id}})
        MATCH p = (start)-[:{DEPENDENCY}*0..{MAX_DEPTH}]->(suspect:Entity)
        WHERE EXISTS {{ (:Alert)-[:RAISED_ON]->(suspect) }}
        WITH suspect, p, {_PATH_QUALITY},
             size([n IN nodes(p) WHERE EXISTS {{ (:Alert)-[:RAISED_ON]->(n) }}]) AS alerting_hops
        // evidence path: certain first, shortest, then the one the failure most visibly travelled
        ORDER BY uncertain, length(p), alerting_hops DESC, [n IN nodes(p) | n.id]
        WITH suspect, collect({{p: p, uncertain: uncertain, confidence: confidence}})[0] AS best
        WITH suspect, best,
             NOT EXISTS {{
               MATCH (suspect)-[:{DEPENDENCY}*1..{MAX_DEPTH}]->(deeper:Entity)
               WHERE EXISTS {{ (:Alert)-[:RAISED_ON]->(deeper) }}
             }} AS deepest
        MATCH (a:Alert)-[:RAISED_ON]->(suspect)
        WITH suspect, best, deepest, a
        ORDER BY a.raised_at
        WITH suspect, best, deepest,
             collect(a {{.id, .severity, .message, .raised_at}}) AS alerts,
             max(CASE a.severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END) AS worst
        RETURN suspect.id AS id,
               suspect.name AS name,
               [l IN labels(suspect) WHERE NOT l IN ['Entity', 'Asset']][0] AS type,
               length(best.p) AS distance,
               [n IN nodes(best.p) | n.name] AS path,
               deepest,
               best.uncertain AS uncertain,
               best.confidence AS confidence,
               alerts
        ORDER BY deepest DESC, distance DESC, worst DESC, id
        """,
        id=asset_id,
    )


# --- decommissioning -------------------------------------------------------------------

_AS_OF = """
OPTIONAL MATCH (b:Batch {source: 'monitoring'}) WHERE b.completed_at IS NOT NULL
WITH b ORDER BY b.completed_at DESC LIMIT 1
WITH coalesce(b.as_of, datetime()) AS as_of
"""


def decommission_candidates(driver: Driver, stale_days: int = 90) -> dict[str, list[dict[str, Any]]]:
    """Servers with no live workload, with the reasons, plus the ones we cannot judge.

    A workload is live when it is an application not retired, or a database
    that at least one non-retired application depends on. Dates are measured
    against the monitoring export date, not "now", so answers are reproducible.

    A server that an ambiguous placement might host (MAYBE_RUNS_ON) is never a
    candidate: its workload is unknown, not absent. It is listed under `blocked`.
    """
    rows = _read(
        driver,
        _AS_OF
        + """
        MATCH (s:Server)
        OPTIONAL MATCH (w)-[:RUNS_ON|HOSTED_ON]->(s)
        WITH as_of, s, collect(w) AS workloads
        WITH as_of, s, workloads,
             [w IN workloads WHERE (w:Application AND w.status <> 'retired')
                 OR (w:Database AND EXISTS { (c:Application)-[:DEPENDS_ON]->(w) WHERE c.status <> 'retired' })] AS live,
             EXISTS { (:Application)-[:MAYBE_RUNS_ON]->(s) } AS workload_unknown,
             CASE WHEN s.last_seen IS NULL THEN null ELSE duration.inDays(s.last_seen, as_of).days END AS days_unseen
        WHERE size(live) = 0
        WITH s, workloads, workload_unknown, days_unseen, [reason IN [
                 CASE WHEN size(workloads) = 0 THEN 'no_workload' END,
                 CASE WHEN any(w IN workloads WHERE w:Application) THEN 'only_retired_applications' END,
                 CASE WHEN any(w IN workloads WHERE w:Database) THEN 'only_unused_databases' END,
                 CASE WHEN s.status = 'retired' AND days_unseen <= 7 THEN 'retired_in_cmdb_but_still_running' END,
                 CASE WHEN days_unseen > $stale_days THEN 'not_seen_by_monitoring_for_' + toString(days_unseen) + '_days' END,
                 CASE WHEN NOT coalesce(s.monitored, false) THEN 'not_monitored' END
             ] WHERE reason IS NOT NULL] AS reasons
        RETURN s.id AS id, s.name AS name, s.status AS cmdb_status, s.in_cmdb AS in_cmdb,
               s.last_seen AS last_seen, days_unseen, workload_unknown, reasons,
               [w IN workloads | w.name] AS workloads
        ORDER BY id
        """,
        stale_days=stale_days,
    )
    return {
        "candidates": [r for r in rows if not r["workload_unknown"]],
        "blocked": [r for r in rows if r["workload_unknown"]],
    }


# --- compliance ------------------------------------------------------------------------


def compliance_findings(driver: Driver) -> list[dict[str, Any]]:
    """Personal data on an unsupported OS, or on a server nobody registered."""
    return _read(
        driver,
        _AS_OF
        + """
        MATCH (a:Asset)-[r:RUNS_ON|HOSTED_ON|MAYBE_RUNS_ON]->(s:Server)
        WHERE a.personal_data
          AND (NOT s.in_cmdb OR s.os_end_of_support < date(as_of))
        RETURN CASE WHEN NOT s.in_cmdb THEN 'personal_data_on_unregistered_server'
                    ELSE 'personal_data_on_unsupported_os' END AS finding,
               a.id AS asset_id, a.name AS asset,
               s.id AS server_id, s.name AS server, s.os AS os, s.os_end_of_support AS os_end_of_support,
               type(r) = 'MAYBE_RUNS_ON' AS uncertain,
               coalesce(r.needs_review, false) AS needs_review,
               r.rule AS matched_by, r.confidence AS confidence
        ORDER BY finding, asset, server
        """,
    )


# --- data quality ----------------------------------------------------------------------


def reconciliation_report(driver: Driver) -> dict[str, Any]:
    """What reconciliation could not settle, and how every link was made."""
    issues = _read(
        driver,
        """
        MATCH (r:SourceRecord)-[:HAS_ISSUE]->(i:Issue)
        RETURN r.source AS source, r.source_key AS record, i.kind AS problem, i.value AS value, i.candidates AS candidates
        ORDER BY source, record
        """,
    )
    to_review = _read(
        driver,
        """
        MATCH (a)-[x:RUNS_ON|HOSTED_ON]->(s:Server) WHERE x.needs_review
        RETURN a.name AS asset, type(x) AS link, s.name AS server, x.rule AS rule, x.confidence AS confidence
        UNION ALL
        MATCH (r:SourceRecord)-[d:DESCRIBES]->(e:Entity) WHERE d.needs_review
        RETURN r.source + ':' + r.source_key AS asset, 'DESCRIBES' AS link, e.name AS server, d.rule AS rule, d.confidence AS confidence
        """,
    )
    rules = _read(
        driver,
        """
        MATCH (:SourceRecord)-[d:DESCRIBES]->()
        RETURN d.rule AS rule, count(*) AS links ORDER BY links DESC, rule
        """,
    )
    return {"issues": issues, "needs_review": to_review, "links_by_rule": rules}


def describe_schema(driver: Driver) -> dict[str, Any]:
    """Node and relationship counts plus the direction convention, for an agent new to the graph."""
    nodes = _read(
        driver,
        "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS count ORDER BY label",
    )
    rels = _read(driver, "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count ORDER BY type")
    return {
        "nodes": nodes,
        "relationships": rels,
        "conventions": [
            "Dependency edges point from the thing that needs to the thing it needs: "
            "(Application)-[:DEPENDS_ON]->(Application|Database), (Application)-[:RUNS_ON]->(Server), "
            "(Database)-[:HOSTED_ON]->(Server).",
            "MAYBE_RUNS_ON marks an ambiguous placement: one edge per candidate server.",
            "RUNS_ON and HOSTED_ON carry {rule, confidence, needs_review} from reconciliation.",
            "(SourceRecord)-[:DESCRIBES {rule, confidence}]->(Entity) is the provenance of every entity.",
            "Server.id is the CMDB CI id, or UNREG-<site>/<host> for hosts only monitoring knows.",
        ],
    }
