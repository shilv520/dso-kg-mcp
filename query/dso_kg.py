#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""dso_kg.py — DSO 教材知识图谱查询层。

存储:
  - data/dso_kg.ttl RDF 图（Turtle；rdflib 内存加载，SPARQL 1.1 via rdflib）
  - data/kb.sqlite  sidecar（fragment 全文、evidence 引文、实体属性/FTS5 索引）

注: semantica 的 TripletStore oxigraph 后端需要文件持久化 pyoxigraph Store，
在本机 Windows 上 segfault（pyoxigraph 0.5.13/RocksDB），故运行时引擎为 rdflib。

设计: 图规模小（万级三元组），启动时把 IRI 边、label、type 一次性载入内存做遍历，
rdflib SPARQL 保留给 sparql_raw 逃生舱。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DS = "https://example.org/dso/"
INST = DS + "inst/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

PREFIXES = (
    "PREFIX ds: <https://example.org/dso/> "
    "PREFIX inst: <https://example.org/dso/inst/> "
    "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
    "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> "
    "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> "
)

BROADER_PREDS = {  # source is MORE SPECIFIC than target
    "methodBroaderMethod", "problemTypeBroaderProblemType", "domainConceptBroaderConcept",
    "dataCharacteristicBroaderCharacteristic", "industryDomainBroaderDomain",
    "dataConditionRefinesDataCondition",
}
CLAIM_PRED_PREFIX = "claim"
# question words / generic verbs that must not drive entity or fragment probes
STOP_TOKENS = {
    "what", "which", "how", "why", "when", "where", "who", "does", "did", "the",
    "and", "for", "with", "from", "that", "this", "these", "those", "can", "could",
    "should", "would", "will", "about", "into", "over", "under", "main", "mainly",
    "key", "work", "works", "working", "use", "used", "uses", "using", "help",
    "helps", "technique", "techniques", "way", "ways", "good", "bad", "best",
    "better", "than", "then", "there", "their", "they", "you", "your", "not",
    "but", "all", "any", "some", "more", "most", "other", "between", "versus",
    "give", "gives", "name", "named", "called", "many", "much", "several",
    "properly", "successfully", "typically", "usually", "often", "also",
}
READONLY_OK = re.compile(r"^\s*(SELECT|ASK|CONSTRUCT|DESCRIBE)\b", re.I)
READONLY_BAD = re.compile(r"\b(INSERT|DELETE|LOAD|CLEAR|DROP|CREATE|COPY|MOVE|ADD)\b", re.I)


def pred_name(p: str) -> str:
    return p.rsplit("/", 1)[-1] if "/" in p else p


class DsoKG:
    def __init__(self, data_dir: Path = DATA):
        from rdflib import Graph
        self.graph = Graph()
        self.graph.parse(str(data_dir / "dso_kg.ttl"), format="turtle")
        self.con = sqlite3.connect(str(data_dir / "kb.sqlite"), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self._load()

    # ------------------------------------------------------------ loading --
    def _load(self):
        from rdflib import URIRef
        self.label: Dict[str, str] = {}
        self.types: Dict[str, set] = defaultdict(set)
        self.out_edges: Dict[str, List[Tuple[str, str]]] = defaultdict(list)   # s -> (p, o)
        self.in_edges: Dict[str, List[Tuple[str, str]]] = defaultdict(list)    # o -> (p, s)
        n = 0
        for ts, tp, to in self.graph:
            if not (isinstance(ts, URIRef) and isinstance(to, URIRef)):
                continue
            n += 1
            s, p, o = str(ts), str(tp), str(to)
            if p == RDFS_LABEL:
                continue
            if p == RDF_TYPE:
                self.types[s].add(o)
                continue
            self.out_edges[s].append((p, o))
            self.in_edges[o].append((p, s))
        # labels come from the sidecar (entities + fragments)
        for row in self.con.execute("SELECT uri, display_name FROM entities"):
            self.label[row["uri"]] = row["display_name"]
        for row in self.con.execute("SELECT fragment_id, section_title FROM fragments"):
            self.label[f"{INST}source-fragment/{row['fragment_id']}"] = row["section_title"]
        self.n_edges = n
        # most-specific type per entity
        prof = json.load(open(ROOT / "schema" / "textbook_profile.json", encoding="utf-8"))
        self._obj = prof["objectTypes"]
        self.entity_type: Dict[str, str] = {}
        for uri, ts in self.types.items():
            locals_ = [t for t in ts if t.startswith(DS)]
            if not locals_:
                continue
            # most specific = deepest in the superTypes chain (leaf type)
            def depth(t):
                d, cur = 0, t.rsplit("/", 1)[-1]
                while self._obj.get(cur, {}).get("superTypes"):
                    cur = self._obj[cur]["superTypes"][0].split(":")[-1]
                    d += 1
                return d
            best = max(locals_, key=depth)
            self.entity_type[uri] = best.rsplit("/", 1)[-1]
        # vector index for hybrid retrieval (entity_embeddings written by
        # build_kg.py). Absent table / numpy / model all degrade to FTS-only.
        self._emb_uris: List[str] = []
        self._emb_idx: Dict[str, int] = {}
        self._emb_mat = None
        self._emb_model: Optional[str] = None
        self._fvec_ids: List[str] = []
        self._fvec_mat = None
        try:
            rows = self.con.execute(
                "SELECT uri, model, dim, vec FROM entity_embeddings").fetchall()
            if rows:
                import numpy as np
                M = np.zeros((len(rows), rows[0]["dim"]), dtype="float32")
                for i, r in enumerate(rows):
                    M[i] = np.frombuffer(r["vec"], dtype="float32")
                self._emb_uris = [r["uri"] for r in rows]
                self._emb_idx = {u: i for i, u in enumerate(self._emb_uris)}
                self._emb_mat = M
                self._emb_model = rows[0]["model"]
            frows = self.con.execute(
                "SELECT fragment_id, dim, vec FROM fragment_embeddings").fetchall()
            if frows:
                import numpy as np
                FM = np.zeros((len(frows), frows[0]["dim"]), dtype="float32")
                for i, r in enumerate(frows):
                    FM[i] = np.frombuffer(r["vec"], dtype="float32")
                self._fvec_ids = [r["fragment_id"] for r in frows]
                self._fvec_mat = FM
        except Exception:  # noqa: BLE001
            self._emb_mat = None
        # fragment rows in memory (143-ish rows; used for token-density scoring
        # and excerpt windowing in graphrag_context)
        self._frag_rows = [dict(r) for r in self.con.execute(
            "SELECT fragment_id, locator, section_title, summary, text FROM fragments")]

    # ------------------------------------------------------------- helpers --
    def name_of(self, uri: str) -> str:
        return self.label.get(uri, uri.rsplit("/", 1)[-1])

    def type_of(self, uri: str) -> str:
        return self.entity_type.get(uri, "?")

    def _sqlite_entity(self, uri: str) -> Optional[dict]:
        row = self.con.execute("SELECT * FROM entities WHERE uri=?", (uri,)).fetchone()
        if not row:
            return None
        return {"uri": row["uri"], "type": row["type"], "displayName": row["display_name"],
                "description": row["description"],
                "alternateName": json.loads(row["aliases"] or "[]"),
                "props": json.loads(row["props_json"] or "{}")}

    @staticmethod
    def _name_variants(n: str) -> List[str]:
        """singular/plural variants for exact matching"""
        low = n.lower()
        vs = [n]
        if low.endswith("ies"):
            vs.append(n[:-3] + "y")
        if low.endswith("es"):
            vs.append(n[:-2])
        if low.endswith("s"):
            vs.append(n[:-1])
        vs.append(n + "s")
        return list(dict.fromkeys(vs))

    def resolve(self, name: str, type_: Optional[str] = None) -> Optional[str]:
        n = re.sub(r"\s+", " ", name.strip())
        # 1) exact display_name match (with plural/singular variants)
        for v in self._name_variants(n):
            q = "SELECT uri FROM entities WHERE display_name = ? COLLATE NOCASE"
            args: List[Any] = [v]
            if type_:
                q += " AND type = ?"
                args.append(type_)
            row = self.con.execute(q + " LIMIT 1", args).fetchone()
            if row:
                return row["uri"]
        # 2) alias match
        rows = self.con.execute("SELECT uri, aliases FROM entities" +
                                (" WHERE type = ?" if type_ else ""),
                                [type_] if type_ else []).fetchall()
        vars_low = {v.lower() for v in self._name_variants(n)}
        for r in rows:
            al = {a.lower() for a in json.loads(r["aliases"] or "[]")}
            if vars_low & al:
                return r["uri"]
        # 3) containment either way; prefer the shortest matching name
        key = n.lower().rstrip("s")
        if len(key) >= 4:
            rows = self.con.execute("SELECT uri, display_name FROM entities" +
                                    (" WHERE type = ?" if type_ else ""),
                                    [type_] if type_ else []).fetchall()
            cands = [r for r in rows
                     if key in r["display_name"].lower().rstrip("s")
                     or r["display_name"].lower().rstrip("s") in key]
            if cands:
                return min(cands, key=lambda r: len(r["display_name"]))["uri"]
        # 4) FTS fallback
        hits = self.search(n, type_=type_, limit=1)
        return hits[0]["uri"] if hits else None

    def _fts_search(self, text: str, type_: Optional[str] = None, limit: int = 10,
                    name_only: bool = False) -> List[dict]:
        """name_only=True restricts matching to the name/aliases columns —
        used for single-token probes so description-only mentions (bm25 noise)
        cannot hijack graphrag context slots."""
        out = []
        toks = [t for t in re.split(r"[^A-Za-z0-9_\-]+", text) if len(t) >= 2]
        try:
            if name_only:
                match = " OR ".join(f'(name : "{t}"* OR aliases : "{t}"*)' for t in toks[:4])
            else:
                match = " OR ".join(f'"{t}"*' for t in toks[:8])
                if len(toks) > 1:  # phrase matches should outrank token matches
                    phr = " ".join(toks[:8])
                    phr_s = " ".join(t[:-1] if t.endswith("s") and len(t) > 3 else t
                                     for t in toks[:8])
                    phrases = list(dict.fromkeys([phr, phr_s]))
                    match = " OR ".join(f'"{p}"' for p in phrases) + " OR " + match
            q = ("SELECT uri, type, name, snippet(entities_fts, 4, '[', ']', '…', 12) AS snip "
                 "FROM entities_fts WHERE entities_fts MATCH ?" +
                 (" AND type = ?" if type_ else "") +
                 " ORDER BY bm25(entities_fts, 0.0, 0.0, 10.0, 4.0, 1.0) LIMIT ?")
            args = [match] + ([type_] if type_ else []) + [limit]
            for r in self.con.execute(q, args):
                out.append({"uri": r["uri"], "type": r["type"], "name": r["name"],
                            "snippet": (r["snip"] or "").replace("\n", " "), "via": "fts"})
        except sqlite3.OperationalError:
            pass
        return out

    def _embed_query(self, text: str):
        """Query vector, or None when the vector index/model is unavailable."""
        if self._emb_mat is None:
            return None
        try:
            if str(ROOT / "pipeline") not in sys.path:
                sys.path.insert(0, str(ROOT / "pipeline"))
            from embeddings import embed, EMBED_VERSION
            if self._emb_model and self._emb_model != EMBED_VERSION:
                return None          # index built with a different model
            return embed([text])[0]  # model load is lazy+cached in embeddings.py
        except Exception:  # noqa: BLE001
            return None

    def _vector_search(self, text: str, type_: Optional[str] = None, k: int = 30) -> List[Tuple[str, float]]:
        qv = self._embed_query(text)
        if qv is None:
            return []
        sims = self._emb_mat @ qv
        out = []
        for j in sims.argsort()[::-1]:
            uri = self._emb_uris[int(j)]
            if type_ and self.type_of(uri) != type_:
                continue
            out.append((uri, float(sims[int(j)])))
            if len(out) >= k:
                break
        return out

    def search(self, text: str, type_: Optional[str] = None, limit: int = 10) -> List[dict]:
        """Hybrid retrieval: FTS5 bm25 + embedding cosine, fused with RRF.
        Falls back to pure FTS (then LIKE) when the vector index is absent."""
        fts = self._fts_search(text, type_, limit * 3)
        vec = self._vector_search(text, type_, k=limit * 3)
        if not vec:
            out = fts[:limit]
        else:
            rrf: Dict[str, float] = defaultdict(float)
            for rank, h in enumerate(fts):
                rrf[h["uri"]] += 1.0 / (60 + rank)
            vec_set = set()
            for rank, (uri, _s) in enumerate(vec):
                rrf[uri] += 1.0 / (60 + rank)
                vec_set.add(uri)
            # exact-name bonus: an entity whose name/alias equals the query
            # (modulo plural) should outrank bm25 noise from longer descriptions
            vars_low = {v.lower() for v in self._name_variants(text.strip())}
            for uri in list(rrf):
                row = self.con.execute(
                    "SELECT display_name, aliases FROM entities WHERE uri=?", (uri,)).fetchone()
                if row and (row["display_name"].lower() in vars_low
                            or vars_low & {a.lower() for a in json.loads(row["aliases"] or "[]")}):
                    rrf[uri] += 0.05
            by_uri = {h["uri"]: h for h in fts}
            out = []
            for uri in sorted(rrf, key=rrf.get, reverse=True)[:limit]:
                if uri in by_uri:
                    h = dict(by_uri[uri])
                    h["via"] = "fts+vec" if uri in vec_set else "fts"
                else:
                    rec = self._sqlite_entity(uri) or {}
                    h = {"uri": uri, "type": rec.get("type", self.type_of(uri)),
                         "name": rec.get("displayName", self.name_of(uri)),
                         "snippet": (rec.get("description") or "")[:160], "via": "vec"}
                out.append(h)
        if not out:  # LIKE fallback (no FTS hit, no vector index)
            like = f"%{text.strip()}%"
            q = "SELECT uri, type, display_name, description FROM entities WHERE display_name LIKE ?"
            args: List[Any] = [like]
            if type_:
                q += " AND type = ?"
                args.append(type_)
            for r in self.con.execute(q + " LIMIT ?", args + [limit]):
                out.append({"uri": r["uri"], "type": r["type"], "name": r["display_name"],
                            "snippet": (r["description"] or "")[:160], "via": "like"})
        return out

    def similar(self, name_or_uri: str, limit: int = 10,
                same_type: bool = False) -> List[dict]:
        """Entities closest in embedding space to the given entity."""
        uri = name_or_uri if name_or_uri.startswith("http") else self.resolve(name_or_uri)
        if not uri or self._emb_mat is None or uri not in self._emb_idx:
            return []
        sims = self._emb_mat @ self._emb_mat[self._emb_idx[uri]]
        t0 = self.type_of(uri)
        out = []
        for j in sims.argsort()[::-1]:
            u2 = self._emb_uris[int(j)]
            if u2 == uri or (same_type and self.type_of(u2) != t0):
                continue
            rec = self._sqlite_entity(u2) or {}
            out.append({"uri": u2, "name": rec.get("displayName", self.name_of(u2)),
                        "type": rec.get("type", self.type_of(u2)),
                        "cosine": round(float(sims[int(j)]), 3),
                        "description": (rec.get("description") or "")[:160]})
            if len(out) >= limit:
                break
        return out

    # --------------------------------------------------------------- cards --
    def entity(self, name_or_uri: str, max_rel: int = 40) -> Optional[dict]:
        uri = name_or_uri if name_or_uri.startswith("http") else self.resolve(name_or_uri)
        if not uri:
            return None
        rec = self._sqlite_entity(uri) or {"uri": uri, "type": self.type_of(uri),
                                           "displayName": self.name_of(uri),
                                           "description": "", "alternateName": [], "props": {}}
        rel_out, rel_in = [], []
        for p, o in self.out_edges.get(uri, [])[:max_rel]:
            rel_out.append({"predicate": pred_name(p), "target": self.name_of(o),
                            "targetUri": o, "targetType": self.type_of(o)})
        for p, s in self.in_edges.get(uri, [])[:max_rel]:
            rel_in.append({"predicate": pred_name(p), "source": self.name_of(s),
                           "sourceUri": s, "sourceType": self.type_of(s)})
        prov = [dict(r) for r in self.con.execute(
            "SELECT f.fragment_id, f.locator, ef.evidence, ef.confidence "
            "FROM entity_fragments ef JOIN fragments f ON f.fragment_id = ef.fragment_id "
            "WHERE ef.uri = ? ORDER BY ef.confidence DESC LIMIT 8", (uri,))]
        rec["relationsOut"] = rel_out
        rec["relationsIn"] = rel_in
        rec["provenance"] = prov
        return rec

    def taxonomy(self, name_or_uri: str, direction: str = "both", max_depth: int = 3) -> dict:
        uri = name_or_uri if name_or_uri.startswith("http") else self.resolve(name_or_uri)
        if not uri:
            return {}

        def walk(up: bool) -> List[dict]:
            """up=True follows *broader* preds via out_edges (toward parents);
            up=False follows them via in_edges (toward narrower children)."""
            key = "parents" if up else "children"
            root: Dict[str, Any] = {}
            frontier = [(root, uri, 0)]
            seen = {uri}
            while frontier:
                node, cur, d = frontier.pop(0)
                if d >= max_depth:
                    continue
                pairs = self.out_edges.get(cur, []) if up else self.in_edges.get(cur, [])
                for p, other in pairs:
                    if pred_name(p) not in BROADER_PREDS or other in seen:
                        continue
                    seen.add(other)
                    child = {"name": self.name_of(other), "type": self.type_of(other), "uri": other}
                    node.setdefault(key, []).append(child)
                    frontier.append((child, other, d + 1))
            return root.get(key, [])

        res: Dict[str, Any] = {"name": self.name_of(uri), "type": self.type_of(uri), "uri": uri}
        if direction in ("both", "broader"):
            res["broader"] = walk(up=True)
        if direction in ("both", "narrower"):
            res["narrower"] = walk(up=False)
        return res

    def neighbors(self, name_or_uri: str, depth: int = 1, limit: int = 60) -> List[dict]:
        uri = name_or_uri if name_or_uri.startswith("http") else self.resolve(name_or_uri)
        if not uri:
            return []
        out, seen, frontier = [], {uri}, deque([(uri, 0)])
        while frontier and len(out) < limit:
            cur, d = frontier.popleft()
            if d >= depth:
                continue
            for p, o in self.out_edges.get(cur, []):
                if o not in seen:
                    seen.add(o)
                    out.append({"hops": d + 1, "predicate": pred_name(p), "direction": "out",
                                "entity": self.name_of(o), "type": self.type_of(o), "uri": o})
                    frontier.append((o, d + 1))
            for p, s in self.in_edges.get(cur, []):
                if s not in seen:
                    seen.add(s)
                    out.append({"hops": d + 1, "predicate": pred_name(p), "direction": "in",
                                "entity": self.name_of(s), "type": self.type_of(s), "uri": s})
                    frontier.append((s, d + 1))
        return out[:limit]

    def path(self, a: str, b: str, max_hops: int = 4) -> Optional[List[dict]]:
        ua = a if a.startswith("http") else self.resolve(a)
        ub = b if b.startswith("http") else self.resolve(b)
        if not ua or not ub:
            return None
        prev = {ua: None}
        frontier = deque([(ua, [])])
        while frontier:
            cur, trail = frontier.popleft()
            if cur == ub:
                return trail
            if len(trail) >= max_hops:
                continue
            for p, o in self.out_edges.get(cur, []):
                if o not in prev:
                    prev[o] = cur
                    frontier.append((o, trail + [{"predicate": pred_name(p), "direction": "out",
                                                  "to": self.name_of(o)}]))
            for p, s in self.in_edges.get(cur, []):
                if s not in prev:
                    prev[s] = cur
                    frontier.append((s, trail + [{"predicate": pred_name(p), "direction": "in",
                                                  "to": self.name_of(s)}]))
        return None

    # ------------------------------------------------------ DSO query ops --
    def find_methods(self, problem: Optional[str] = None, condition: Optional[str] = None) -> List[dict]:
        rows = self.con.execute("SELECT uri FROM entities WHERE type='Method'").fetchall()
        out = []
        for r in rows:
            uri = r["uri"]
            probs = [self.name_of(o) for p, o in self.out_edges.get(uri, [])
                     if pred_name(p) == "methodApplicableToProblemType"]
            conds = [self.name_of(o) for p, o in self.out_edges.get(uri, [])
                     if pred_name(p) == "methodApplicableUnderDataCondition"]
            excl = [self.name_of(o) for p, o in self.out_edges.get(uri, [])
                     if pred_name(p) == "methodExcludedUnderDataCondition"]
            if problem and not any(problem.lower() in x.lower() for x in probs):
                continue
            if condition and not any(condition.lower() in x.lower() for x in conds):
                continue
            rec = self._sqlite_entity(uri) or {}
            out.append({"method": rec.get("displayName", self.name_of(uri)), "uri": uri,
                        "applicableToProblemTypes": probs, "applicableUnder": conds,
                        "excludedUnder": excl,
                        "purpose": rec.get("props", {}).get("methodPurpose"),
                        "paradigm": rec.get("props", {}).get("methodParadigm"),
                        "description": (rec.get("description") or "")[:200]})
        return out

    def claims(self, about: Optional[str] = None, kind: Optional[str] = None,
               limit: int = 30) -> List[dict]:
        claim_uris = []
        if about:
            uri = about if about.startswith("http") else self.resolve(about)
            if not uri:
                return []
            claim_uris = [s for p, s in self.in_edges.get(uri, [])
                          if pred_name(p).startswith(CLAIM_PRED_PREFIX)]
        else:
            claim_uris = [r["uri"] for r in self.con.execute(
                "SELECT uri FROM entities WHERE type IN ('ReferenceClaim','Guideline','Constraint')")]
        out = []
        seen = set()
        for cu in dict.fromkeys(claim_uris):
            if cu in seen:
                continue
            seen.add(cu)
            rec = self._sqlite_entity(cu)
            if not rec:
                continue
            props = rec["props"]
            if kind and kind.lower() not in (props.get("claimKind", "").lower(),
                                             props.get("guidelineType", "").lower(),
                                             rec["type"].lower()):
                continue
            concerns = [{"predicate": pred_name(p), "entity": self.name_of(o),
                         "type": self.type_of(o)}
                        for p, o in self.out_edges.get(cu, [])
                        if pred_name(p).startswith(CLAIM_PRED_PREFIX)]
            prov = self.con.execute(
                "SELECT f.locator, ef.evidence FROM entity_fragments ef "
                "JOIN fragments f ON f.fragment_id=ef.fragment_id WHERE ef.uri=? LIMIT 2",
                (cu,)).fetchall()
            out.append({"type": rec["type"], "name": rec["displayName"], "uri": cu,
                        "claimStatement": props.get("claimStatement"),
                        "claimKind": props.get("claimKind"),
                        "guidelineType": props.get("guidelineType"),
                        "constraintSeverity": props.get("constraintSeverity"),
                        "status": props.get("referenceClaimStatus"),
                        "concerns": concerns,
                        "source": [{"locator": p["locator"], "quote": p["evidence"]} for p in prov]})
            if len(out) >= limit:
                break
        return out

    def workflows(self, problem: Optional[str] = None) -> List[dict]:
        out = []
        for t_type in ("SolutionTemplate", "WorkflowPattern"):
            for r in self.con.execute("SELECT uri FROM entities WHERE type=?", (t_type,)):
                uri = r["uri"]
                if problem:
                    probs = [self.name_of(o) for p, o in self.out_edges.get(uri, [])
                             if pred_name(p).endswith("ApplicableToProblemType")]
                    if not any(problem.lower() in x.lower() for x in probs):
                        continue
                rec = self._sqlite_entity(uri) or {}
                kids = []
                for p, o in self.out_edges.get(uri, []):
                    pn = pred_name(p)
                    if pn in ("solutionTemplateIncludesStage", "stageContainsWorkflowPattern",
                              "workflowPatternContainsActivityPattern", "workflowPatternEntryActivity",
                              "workflowPatternExitActivity", "workflowPatternContainsTransitionPattern"):
                        kids.append({"predicate": pn, "entity": self.name_of(o),
                                     "type": self.type_of(o), "uri": o})
                out.append({"kind": t_type, "name": rec.get("displayName", self.name_of(uri)),
                            "uri": uri, "description": (rec.get("description") or "")[:250],
                            "parts": kids,
                            "applicableTo": [self.name_of(o) for p, o in self.out_edges.get(uri, [])
                                             if pred_name(p).endswith("ApplicableToProblemType")]})
        # expand stages of templates one level
        for item in out:
            if item["kind"] == "SolutionTemplate":
                for part in item["parts"]:
                    if part["predicate"] == "solutionTemplateIncludesStage":
                        part["contains"] = [
                            {"predicate": pred_name(p), "entity": self.name_of(o), "type": self.type_of(o)}
                            for p, o in self.out_edges.get(part["uri"], [])
                            if pred_name(p) in ("stageContainsWorkflowPattern",)]
        return out

    def metrics(self, problem: Optional[str] = None) -> List[dict]:
        out = []
        for r in self.con.execute("SELECT uri FROM entities WHERE type IN ('Metric','EvaluationProtocol')"):
            uri = r["uri"]
            linked = [self.name_of(s) for p, s in self.in_edges.get(uri, [])
                      if pred_name(p) in ("problemTypeEvaluatedByMetric",
                                          "problemTypeUsesEvaluationProtocol")]
            if problem and not any(problem.lower() in x.lower() for x in linked):
                continue
            rec = self._sqlite_entity(uri) or {}
            out.append({"kind": rec.get("type"), "name": rec.get("displayName", self.name_of(uri)),
                        "uri": uri, "usedByProblemTypes": linked,
                        "props": rec.get("props", {}),
                        "description": (rec.get("description") or "")[:200]})
        return out

    def provenance(self, name_or_uri: str) -> List[dict]:
        uri = name_or_uri if name_or_uri.startswith("http") else self.resolve(name_or_uri)
        if not uri:
            return []
        out = []
        for r in self.con.execute(
                "SELECT f.fragment_id, f.chapter_no, f.section_title, f.locator, f.page_start, "
                "f.page_end, f.text, ef.evidence FROM entity_fragments ef "
                "JOIN fragments f ON f.fragment_id = ef.fragment_id WHERE ef.uri=?", (uri,)):
            text = r["text"]
            quote = r["evidence"] or ""
            i = text.lower().find(quote.lower()[:40]) if quote else -1
            if i >= 0:
                snippet = text[max(0, i - 250): i + len(quote) + 350]
            else:
                snippet = text[:500]
            out.append({"fragmentId": r["fragment_id"], "chapter": r["chapter_no"],
                        "section": r["section_title"], "locator": r["locator"],
                        "pages": [r["page_start"], r["page_end"]],
                        "evidenceQuote": quote, "snippet": re.sub(r"\s+", " ", snippet).strip()})
        return out

    # ------------------------------------------------------------ graphrag --
    def graphrag_context(self, question: str, max_chars: int = 16000) -> dict:
        linked: List[dict] = []
        seen, seen_names = set(), set()

        def add_hits(hits: List[dict]) -> None:
            for h in hits:
                if h["uri"] in seen:
                    continue
                nk = re.sub(r"[^a-z0-9]+", "", h["name"].lower())
                if nk in seen_names:          # same name, different type: one slot only
                    continue
                seen.add(h["uri"])
                seen_names.add(nk)
                linked.append(h)

        # 1) whole question -> HYBRID search (vectors shine on full natural questions)
        add_hits(self.search(question, limit=8))
        # 2) meaningful tokens -> name/alias-only FTS (vector on single tokens
        #    pulls generic hubs; description-only bm25 hits are noise as well)
        for t in re.split(r"[^A-Za-z0-9]+", question):
            if len(t) > 3 and t.lower() not in STOP_TOKENS and len(linked) < 10:
                add_hits(self._fts_search(t, limit=4, name_only=True))
        linked = linked[:10]
        blocks = []
        frag_scores: Counter = Counter()
        for hit in linked:
            uri = hit["uri"]
            rec = self._sqlite_entity(uri) or {}
            lines = [f"### {rec.get('displayName', hit['name'])} ({rec.get('type', hit['type'])})"]
            if rec.get("alternateName"):
                lines.append(f"aka: {', '.join(rec['alternateName'][:5])}")
            if rec.get("description"):
                lines.append(rec["description"][:400])
            props = rec.get("props") or {}
            if props:
                lines.append("props: " + "; ".join(f"{k}={v}" for k, v in list(props.items())[:8]))
            tax = self.taxonomy(uri, "both", 2)
            if tax.get("broader"):
                lines.append("broader: " + ", ".join(x["name"] for x in tax["broader"][:5]))
            if tax.get("narrower"):
                lines.append("narrower: " + ", ".join(x["name"] for x in tax["narrower"][:8]))
            applic = [f"{pred_name(p)} → {self.name_of(o)}"
                      for p, o in self.out_edges.get(uri, [])
                      if pred_name(p) in ("methodApplicableToProblemType",
                                          "methodApplicableUnderDataCondition",
                                          "methodExcludedUnderDataCondition",
                                          "implementationImplementsMethod",
                                          "problemTypeEvaluatedByMetric")][:8]
            if applic:
                lines.append("applicability: " + "; ".join(applic))
            cls = self.claims(about=uri, limit=4)
            for c in cls:
                st = c.get("claimStatement") or c["name"]
                lines.append(f"- [{c['type']}] {st}")
                for s in c.get("source", []):
                    if s.get("locator"):
                        frag_scores[s["locator"]] += 1
            for r in self.con.execute(
                    "SELECT f.locator, ef.evidence FROM entity_fragments ef JOIN fragments f "
                    "ON f.fragment_id=ef.fragment_id WHERE ef.uri=? LIMIT 2", (uri,)):
                frag_scores[r["locator"]] += 1
            blocks.append("\n".join(lines))
        # source excerpts: combined fragment scoring (entity mentions + question
        # token density + fragment vector cosine), excerpt = densest token window
        toks = [t.lower() for t in re.split(r"[^A-Za-z0-9]+", question)
                if len(t) > 3 and t.lower() not in STOP_TOKENS]
        rows_by_loc = {}
        fid_to_loc = {}
        for r in self._frag_rows:
            rows_by_loc[r["locator"]] = r
            fid_to_loc[r["fragment_id"]] = r["locator"]
        fscore: Counter = Counter()
        for loc, c in frag_scores.items():
            if loc in rows_by_loc:
                fscore[loc] += 2.0 * c
        for r in self._frag_rows:
            if not toks:
                break
            head = (r["section_title"] + " " + (r["summary"] or "")).lower()
            body = r["text"].lower()
            hits = sum(3 * head.count(t) + body.count(t) for t in toks)
            if hits:
                fscore[r["locator"]] += min(hits, 30)
        qv = self._embed_query(question)
        if self._fvec_mat is not None and qv is not None:
            sims = self._fvec_mat @ qv
            for i, fid in enumerate(self._fvec_ids):
                loc = fid_to_loc.get(fid)
                if loc:
                    fscore[loc] += max(0.0, float(sims[i]) - 0.35) * 25.0

        def excerpt(text: str, width: int = 700, step: int = 200) -> str:
            # section HEAD (definitions usually live in the opening paragraphs)
            # + the densest question-token window deeper in the text
            head = re.sub(r"\s+", " ", text[:400]).strip()
            low = text.lower()
            best_s, best_i = -1, 400
            for i in range(400, max(401, len(low) - width // 2), step):
                w = low[i:i + width]
                s = sum(w.count(t) for t in toks) if toks else -1
                if s > best_s:
                    best_s, best_i = s, i
            body = re.sub(r"\s+", " ", text[best_i:best_i + width]).strip()
            return head + (" […] " + body if best_s > 0 else "")

        snippets = []
        for loc, _s in fscore.most_common(3):
            row = rows_by_loc.get(loc)
            if row and row["text"]:
                snippets.append({"locator": loc, "text": excerpt(row["text"])})
        ctx = "\n\n".join(blocks)
        if snippets:
            ctx += "\n\n## Source excerpts\n" + "\n\n".join(
                f"[{s['locator']}] …{s['text']}…" for s in snippets)
        return {"question": question,
                "linkedEntities": [{"name": h["name"], "type": h["type"], "uri": h["uri"]} for h in linked],
                "context": ctx[:max_chars],
                "sources": [s["locator"] for s in snippets]}

    # -------------------------------------------------------------- misc ----
    def sparql_raw(self, query: str, limit: int = 200) -> Any:
        if READONLY_BAD.search(query) or not READONLY_OK.match(query):
            raise ValueError("only read-only SELECT/ASK/CONSTRUCT/DESCRIBE queries are allowed")
        import itertools
        res = self.graph.query(PREFIXES + query)
        rtype = str(getattr(res, "type", "")).upper()
        if rtype == "SELECT":
            from rdflib import Literal
            vars_ = [str(v) for v in (res.vars or [])]
            nm = self.graph.namespace_manager

            def _ser(v):
                # Literal → Python 原生值（JSON 可序列化）；URIRef/BNode → 前缀短名。
                # 旧实现对所有绑定调 v.value，遇 URIRef 抛 AttributeError。
                if v is None:
                    return None
                if isinstance(v, Literal):
                    val = v.value
                    if val is None:
                        return str(v)
                    return val if isinstance(val, (str, int, float, bool)) else str(val)
                return v.n3(nm)

            rows = [{str(k): _ser(v) for k, v in b.items()}
                    for b in itertools.islice(res.bindings, limit)]
            return {"variables": vars_, "rows": rows}
        if rtype == "ASK":
            return bool(res.askAnswer)
        if rtype in ("CONSTRUCT", "DESCRIBE"):
            return {"type": rtype, "triples": len(res.graph)}
        return {"type": rtype or str(type(res))}

    def stats(self) -> dict:
        by_type = dict(self.con.execute("SELECT type, COUNT(*) FROM entities GROUP BY type"))
        rel_counts = dict(self.con.execute(
            "SELECT p, COUNT(DISTINCT s || o) FROM relation_evidence GROUP BY p"))
        return {"edges": self.n_edges, "entities": by_type,
                "entities_total": sum(by_type.values()),
                "distinct_relations": sum(rel_counts.values()),
                "fragments": self.con.execute("SELECT COUNT(*) FROM fragments").fetchone()[0],
                "vector_index": ({"model": self._emb_model, "entities": len(self._emb_uris),
                                  "dim": int(self._emb_mat.shape[1])}
                                 if self._emb_mat is not None else None)}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    kg = DsoKG()
    print(json.dumps(kg.stats(), ensure_ascii=False, indent=1))
