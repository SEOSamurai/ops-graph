"""Entity reconciliation: decide which known server a source record describes.

The rules are ordered from strongest to weakest evidence. The first rule
that yields exactly one candidate wins; a rule that yields several
candidates stops the search and reports an ambiguity instead of guessing.

    rule            evidence                                    confidence  review
    ip_exact        same IP address                             1.0         no
    name_site       same stem, number and site                  0.9         no
    name_unique     same stem and number, site unknown,         0.8         no
                    exactly one candidate
    fuzzy_stem      same site, same number, stem similar        0.6         yes
                    (rapidfuzz ratio >= 80): catches typos
                    like "bacth01" without confusing web01
                    with web11
    ambiguous       several candidates at the same level        -           yes
    unresolved      nothing matched                             -           yes

Stage 2 (the graph stage, bottom of this file) reads the raw
(:SourceRecord) nodes back from Neo4j, builds a plan of canonical entities
and relationships in plain Python (`build_plan`, testable without a
database), then replaces the derived graph in one write transaction.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from neo4j import Driver, ManagedTransaction
from rapidfuzz import fuzz

from ops_graph.normalize import HostKey, parse_host
from ops_graph.rag import relink_mentions_tx

FUZZY_THRESHOLD = 80.0


@dataclass(frozen=True)
class ServerCandidate:
    """A server the record could resolve to."""

    id: str
    key: HostKey
    ip: str | None = None


@dataclass(frozen=True)
class Match:
    """Outcome of reconciling one host spelling.

    `server_id` is None for "ambiguous" and "unresolved"; in the ambiguous
    case `candidates` lists every server that could be the one.
    """

    server_id: str | None
    rule: str
    confidence: float
    needs_review: bool
    candidates: tuple[str, ...] = field(default=())

    @property
    def resolved(self) -> bool:
        return self.server_id is not None


def _one_or_ambiguous(hits: list[ServerCandidate], rule: str, confidence: float, review: bool) -> Match | None:
    if not hits:
        return None
    if len(hits) == 1:
        return Match(hits[0].id, rule, confidence, review)
    return Match(None, "ambiguous", 0.0, True, tuple(sorted(c.id for c in hits)))


def match_host(
    raw_name: str,
    candidates: list[ServerCandidate],
    *,
    ip: str | None = None,
    site_hint: str | None = None,
) -> Match:
    """Resolve a host spelling (plus optional IP and site context) to one candidate."""
    if ip:
        found = _one_or_ambiguous([c for c in candidates if c.ip == ip], "ip_exact", 1.0, False)
        if found:
            return found

    key = parse_host(raw_name).with_site(site_hint)

    if key.site is not None:
        found = _one_or_ambiguous(
            [c for c in candidates if (c.key.stem, c.key.number, c.key.site) == (key.stem, key.number, key.site)],
            "name_site",
            0.9,
            False,
        )
        if found:
            return found
    else:
        found = _one_or_ambiguous(
            [c for c in candidates if (c.key.stem, c.key.number) == (key.stem, key.number)],
            "name_unique",
            0.8,
            False,
        )
        if found:
            return found

    # Fuzzy only where the context pins the site and the number: a typo in
    # the letters is plausible, a different number is a different machine.
    if key.site is not None and key.number is not None:
        scored = [
            (fuzz.ratio(key.stem, c.key.stem), c)
            for c in candidates
            if c.key.site == key.site and c.key.number == key.number
        ]
        scored = [(score, c) for score, c in scored if score >= FUZZY_THRESHOLD]
        if scored:
            best = max(score for score, _ in scored)
            found = _one_or_ambiguous([c for score, c in scored if score == best], "fuzzy_stem", 0.6, True)
            if found:
                return found

    return Match(None, "unresolved", 0.0, True)


# --- stage 2: from raw records to the canonical graph --------------------------------


@dataclass(frozen=True)
class StoredRecord:
    """A (:SourceRecord) as read back from Neo4j."""

    source: str
    key: str
    kind: str
    payload: dict[str, Any]


@dataclass
class GraphPlan:
    """Everything reconciliation wants the graph to contain, before writing it."""

    nodes: dict[str, dict[str, dict[str, Any]]] = field(default_factory=lambda: defaultdict(dict))
    rels: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    describes: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)

    def node(self, label: str, props: dict[str, Any]) -> str:
        self.nodes[label].setdefault(props["id"], {}).update(props)
        return props["id"]

    def rel(self, rel_type: str, start: str, end: str, **props: Any) -> None:
        self.rels[rel_type].append({"start": start, "end": end, "props": props})

    def describe(self, rec: StoredRecord, entity_id: str, rule: str, confidence: float, needs_review: bool) -> None:
        self.describes.append(
            {"source": rec.source, "key": rec.key, "entity": entity_id, "rule": rule,
             "confidence": confidence, "needs_review": needs_review}
        )

    def issue(self, rec: StoredRecord, kind: str, value: str, candidates: Iterable[str] = ()) -> None:
        self.issues.append({"source": rec.source, "key": rec.key, "kind": kind, "value": value, "candidates": list(candidates)})

    def ids(self) -> set[str]:
        return {i for by_id in self.nodes.values() for i in by_id}


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _slug(text: str) -> str:
    return "-".join(text.lower().replace("&", " ").split())


def build_plan(records: Iterable[StoredRecord]) -> GraphPlan:
    """Turn raw records into canonical entities, links and review items.

    Order matters and is deliberate:
      1. teams, then CMDB servers (the master list of machines);
      2. monitoring hosts, matched against CMDB; a host nobody registered
         becomes an UNREG server (shadow IT) and joins the candidates;
      3. catalog services and databases, matched against CMDB + UNREG
         servers, so a service on a shadow host (mq01) is still placed;
      4. dependencies, ownership and alerts, once every name exists.
    """
    plan = GraphPlan()
    by_kind: dict[tuple[str, str], list[StoredRecord]] = defaultdict(list)
    for rec in sorted(records, key=lambda r: (r.source, r.key)):
        by_kind[(rec.source, rec.kind)].append(rec)

    # 1a. teams from the catalog
    team_by_name: dict[str, str] = {}
    team_ids: set[str] = set()
    for rec in by_kind[("catalog", "team")]:
        tid = plan.node("Team", {"id": f"team:{rec.payload['id']}", "name": rec.payload["name"]})
        plan.describe(rec, tid, "catalog", 1.0, False)
        team_by_name[rec.payload["name"].lower()] = tid
        team_ids.add(tid)

    # 1b. CMDB servers: the CI id is the master identity
    candidates: list[ServerCandidate] = []
    for rec in by_kind[("cmdb", "server")]:
        p = rec.payload
        key = parse_host(p["hostname"])
        sid = plan.node(
            "Server",
            {
                "id": p["ci_id"],
                "name": p["hostname"],
                "host_key": str(key),
                "ip": p["ip_address"] or None,
                "site": (p["site"] or "").lower() or key.site,
                "environment": p["environment"].lower(),
                "os": p["os"],
                "os_end_of_support": date.fromisoformat(p["os_end_of_support"]) if p["os_end_of_support"] else None,
                "status": p["status"].strip().lower().replace(" ", "_"),
                "in_cmdb": True,
                "monitored": False,
            },
        )
        plan.describe(rec, sid, "cmdb_master", 1.0, False)
        candidates.append(ServerCandidate(sid, key, p["ip_address"] or None))
        if p.get("support_group"):
            name = p["support_group"]
            tid = team_by_name.get(name.lower()) or plan.node("Team", {"id": f"team:{_slug(name)}", "name": name})
            team_by_name.setdefault(name.lower(), tid)
            plan.describe(rec, tid, "cmdb_support_group", 1.0, False)
            plan.rel("SUPPORTS", tid, sid)

    # 2. monitoring hosts
    host_to_server: dict[str, str] = {}
    for rec in by_kind[("monitoring", "host")]:
        p = rec.payload
        seen = {"last_seen": _dt(p.get("last_seen")), "monitored": True}
        m = match_host(p["host"], candidates, ip=p.get("address"))
        if m.resolved:
            plan.node("Server", {"id": m.server_id, **seen})
            plan.describe(rec, m.server_id, m.rule, m.confidence, m.needs_review)
            host_to_server[p["host"]] = m.server_id
        elif m.rule == "ambiguous":
            plan.issue(rec, "ambiguous_host", p["host"], m.candidates)
        else:
            key = parse_host(p["host"])
            sid = plan.node(
                "Server",
                {"id": f"UNREG-{key}", "name": p["host"], "host_key": str(key), "ip": p.get("address"),
                 "site": key.site, "status": "unregistered", "in_cmdb": False, **seen},
            )
            plan.describe(rec, sid, "monitoring_only", 1.0, True)
            plan.issue(rec, "not_in_cmdb", p["host"])
            candidates.append(ServerCandidate(sid, key, p.get("address")))
            host_to_server[p["host"]] = sid

    # 3. catalog services and databases, placed on servers
    def place(rec: StoredRecord, entity_id: str, host: str, site: str | None, rel_type: str) -> None:
        m = match_host(host, candidates, site_hint=site)
        if m.resolved:
            plan.rel(rel_type, entity_id, m.server_id, rule=m.rule, confidence=m.confidence, needs_review=m.needs_review)
        elif m.rule == "ambiguous":
            for sid in m.candidates:
                plan.rel("MAYBE_RUNS_ON", entity_id, sid, rule="ambiguous", confidence=0.0, needs_review=True)
            plan.issue(rec, "ambiguous_host", host, m.candidates)
        else:
            plan.issue(rec, "unresolved_host", host)

    for rec in by_kind[("catalog", "service")]:
        p = rec.payload
        aid = plan.node(
            "Application",
            {"id": f"app:{p['name']}", "name": p["name"], "criticality": p.get("criticality"),
             "personal_data": bool(p.get("personal_data")), "status": p.get("status", "active"), "site": p.get("site")},
        )
        plan.describe(rec, aid, "catalog", 1.0, False)
        for host in p.get("hosts", []):
            place(rec, aid, host, p.get("site"), "RUNS_ON")

    for rec in by_kind[("catalog", "database")]:
        p = rec.payload
        did = plan.node(
            "Database",
            {"id": f"db:{p['name']}", "name": p["name"], "engine": p.get("engine"),
             "personal_data": bool(p.get("personal_data"))},
        )
        plan.describe(rec, did, "catalog", 1.0, False)
        if p.get("host"):
            place(rec, did, p["host"], p.get("site"), "HOSTED_ON")

    # 4. dependencies, ownership, alerts
    names = {props["name"]: eid for label in ("Application", "Database") for eid, props in plan.nodes[label].items()}
    for kind in ("service", "database"):
        for rec in by_kind[("catalog", kind)]:
            p = rec.payload
            eid = names[p["name"]]
            owner = f"team:{p['owner']}" if p.get("owner") else None
            if owner in team_ids:
                plan.rel("OWNS", owner, eid)
            elif owner:
                plan.issue(rec, "unknown_team", p["owner"])
            for dep in p.get("depends_on") or []:
                if dep in names:
                    plan.rel("DEPENDS_ON", eid, names[dep])
                else:
                    plan.issue(rec, "unknown_dependency", dep)

    for rec in by_kind[("monitoring", "alert")]:
        p = rec.payload
        target = host_to_server.get(p["target"]) if p["target_type"] == "host" else names.get(p["target"])
        if target is None:
            plan.issue(rec, "unknown_alert_target", p["target"])
            continue
        alert_id = plan.node(
            "Alert", {"id": p["id"], "severity": p["severity"], "message": p["message"], "raised_at": _dt(p["raised_at"])}
        )
        plan.describe(rec, alert_id, "monitoring", 1.0, False)
        plan.rel("RAISED_ON", alert_id, target)

    return plan


# --- graph IO ------------------------------------------------------------------------

LABELS = {
    "Server": "Server:Asset",
    "Application": "Application:Asset",
    "Database": "Database:Asset",
    "Team": "Team",
    "Alert": "Alert",
}
REL_TYPES = ("SUPPORTS", "RUNS_ON", "MAYBE_RUNS_ON", "HOSTED_ON", "DEPENDS_ON", "OWNS", "RAISED_ON")


def load_records(driver: Driver) -> list[StoredRecord]:
    """Live records only: a stale record is kept for history, not reconciled."""
    rows, _, _ = driver.execute_query(
        "MATCH (r:SourceRecord) WHERE NOT r.stale "
        "RETURN r.source AS source, r.source_key AS key, r.kind AS kind, r.payload AS payload"
    )
    return [StoredRecord(r["source"], r["key"], r["kind"], json.loads(r["payload"])) for r in rows]


def _write_plan_tx(tx: ManagedTransaction, plan: GraphPlan) -> dict[str, int]:
    # 1. drop everything derived: it is rebuilt from the records below
    tx.run("MATCH (:Entity)-[r]->(:Entity) DELETE r")
    tx.run("MATCH (:SourceRecord)-[r:DESCRIBES]->() DELETE r")
    tx.run("MATCH (i:Issue) DETACH DELETE i")
    # 2. entities no longer described by any live record disappear
    removed = tx.run(
        "MATCH (e:Entity) WHERE NOT e.id IN $ids DETACH DELETE e RETURN count(*) AS n", ids=sorted(plan.ids())
    ).single()["n"]
    # 3. upsert entities (labels come from the LABELS table, never from input)
    for label, by_id in plan.nodes.items():
        tx.run(
            f"UNWIND $rows AS row MERGE (n:Entity {{id: row.id}}) SET n = row, n:{LABELS[label]}",
            rows=list(by_id.values()),
        )
    # 4. provenance, then derived relationships, then review items
    tx.run(
        """
        UNWIND $rows AS row
        MATCH (s:SourceRecord {source: row.source, source_key: row.key})
        MATCH (e:Entity {id: row.entity})
        CREATE (s)-[:DESCRIBES {rule: row.rule, confidence: row.confidence, needs_review: row.needs_review}]->(e)
        """,
        rows=plan.describes,
    )
    for rel_type in REL_TYPES:
        tx.run(
            f"""
            UNWIND $rows AS row
            MATCH (a:Entity {{id: row.start}})
            MATCH (b:Entity {{id: row.end}})
            CREATE (a)-[r:{rel_type}]->(b)
            SET r = row.props
            """,
            rows=plan.rels.get(rel_type, []),
        )
    tx.run(
        """
        UNWIND $rows AS row
        MATCH (s:SourceRecord {source: row.source, source_key: row.key})
        CREATE (s)-[:HAS_ISSUE]->(:Issue {kind: row.kind, value: row.value, candidates: row.candidates})
        """,
        rows=plan.issues,
    )
    # 5. runbook mentions follow the entities they name (no-op before any embedding)
    mentions = relink_mentions_tx(tx)
    return {"entities": len(plan.ids()), "removed": removed, "issues": len(plan.issues), "mentions": mentions}


def reconcile(driver: Driver) -> dict[str, int]:
    """Rebuild the canonical graph from live records, all or nothing."""
    plan = build_plan(load_records(driver))
    with driver.session() as session:
        return session.execute_write(_write_plan_tx, plan)
