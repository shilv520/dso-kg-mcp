#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_kg.py — 全局实体归一 + DSO 知识图谱构建。

输入:  data/fragments.jsonl + data/extractions.jsonl + schema/textbook_profile.json
输出:
  - data/dso_kg.ttl           RDF 图（Turtle，持久化交付物 / SHACL / 查询层加载）
  - data/kb.sqlite            sidecar：fragment 全文、evidence 引文、实体 FTS 索引
  - data/entities.json        归一后实体表（调试/查询层用）
  - data/shacl_report.txt     pyshacl 对 schema/dso_reference_shapes.ttl 的校验报告
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PROFILE_PATH = ROOT / "schema" / "textbook_profile.json"
FRAGMENTS_PATH = DATA / "fragments.jsonl"
EXTRACTIONS_PATH = DATA / "extractions.jsonl"
TOC_PATH = DATA / "toc.json"
TTL_PATH = DATA / "dso_kg.ttl"
SQLITE_PATH = DATA / "kb.sqlite"
ENTITIES_PATH = DATA / "entities.json"
SHAPES_PATH = ROOT / "schema" / "dso_reference_shapes.ttl"
OXI_PATH = DATA / "oxigraph"

DS = "https://example.org/dso/"
INST = DS + "inst/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
XSD_INT = "http://www.w3.org/2001/XMLSchema#integer"
XSD_DATE = "http://www.w3.org/2001/XMLSchema#date"
XSD_DECIMAL = "http://www.w3.org/2001/XMLSchema#decimal"

BOOK_URI = INST + "knowledge-source/holml-1e"
BOOK_CHAPTERS = list(range(1, 17))   # full book (1st edition)


def norm_name(name: str) -> str:
    s = re.sub(r"\s+", " ", str(name).strip().lower())
    s = re.sub(r"[^a-z0-9 \-]", "", s)
    s = re.sub(r"\s+'s$|\s+s$", "", s)  # possessive / simple plural
    return s.strip(" -")


def slug(name: str, maxlen: int = 58) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", norm_name(name)).strip("-")
    return (s[:maxlen] or "x")


def kebab(t: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", t).lower()


def load_profile():
    raw = json.load(open(PROFILE_PATH, encoding="utf-8"))
    obj = raw["objectTypes"]
    ancestors: Dict[str, set] = {}

    def anc(t: str) -> set:
        if t not in ancestors:
            ancestors[t] = {t}  # placeholder also guards against cycles
            for s in obj.get(t, {}).get("superTypes", []):
                ancestors[t] |= anc(s)
        return ancestors[t]

    flat: Dict[str, Dict[str, dict]] = {}
    for t in obj:
        merged: Dict[str, dict] = {}
        for a in anc(t):
            for p in obj.get(a, {}).get("properties", []):
                merged.setdefault(p["name"], p)
        flat[t] = merged
    return raw, obj, anc, flat


def is_int_prop(pdef: dict) -> bool:
    return "nteger" in str(pdef.get("type", ""))


def is_float_prop(pdef: dict) -> bool:
    t = str(pdef.get("type", "")).lower()
    return "float" in t or "decimal" in t


def is_date_prop(pdef: dict) -> bool:
    return str(pdef.get("type", "")).lower().endswith("date")


def date_value(v) -> Optional[str]:
    """Normalize to xsd:date lexical form; None if unparseable."""
    s = str(v).strip()
    if re.fullmatch(r"\d{4}", s):          # year-only -> Jan 1 (documented)
        return s + "-01-01"
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return s + "-01"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    return None


SKIP_PROPS = {"description", "displayName", "alternateName", "identifier"}


def bad_prop_value(pv) -> bool:
    return pv is None or str(pv).strip() in ("", "None", "null", "N/A")


# ---------------------------------------------------------------- repair ----
REPAIR_PROMPT_VERSION = "repair-v1"


def _repair_cache_get(con, key):
    con.execute("CREATE TABLE IF NOT EXISTS llm_cache_repair(key TEXT PRIMARY KEY, response TEXT)")
    row = con.execute("SELECT response FROM llm_cache_repair WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _repair_cache_put(con, key, response):
    con.execute("CREATE TABLE IF NOT EXISTS llm_cache_repair(key TEXT PRIMARY KEY, response TEXT)")
    con.execute("INSERT OR REPLACE INTO llm_cache_repair VALUES(?,?)", (key, response))
    con.commit()


def llm_link_group(pred: str, pred_desc: str, tgt_type: str,
                   sources: List[dict], candidates: List[dict], llm, cache_con) -> List[dict]:
    """Closed-set entity linking: choose, per source entity, the single candidate
    target that the textual evidence supports for `pred` — or null."""
    import hashlib as _hl
    src_lines = "\n".join(
        f"[{i+1}] {s['name']} — {(s['desc'] or '')[:200]} | evidence: \"{(s['evidence'] or '')[:200]}\""
        for i, s in enumerate(sources))
    cand_lines = "\n".join(
        f"[{i+1}] {c['name']} — {(c['desc'] or '')[:120]}" for i, c in enumerate(candidates))
    prompt = f"""You are performing global entity linking for a DSO knowledge graph extracted from "Hands-On Machine Learning" (Géron, 1st ed.).

Relation: ds:{pred} — {pred_desc}
Source type: {sources[0]['type'] if sources else '?'}; target type: {tgt_type}.

For EACH numbered source entity, choose the single best candidate target (by its number) such that the relation is clearly supported by the source's description/evidence. If no candidate is clearly supported, use null. Do not force a link.

Output STRICT JSON only:
{{"links": [{{"source": <int>, "target": <int|null>, "evidence": "<= 25 words quoted or closely paraphrased from the source's description/evidence", "confidence": <0.0-1.0>}}]}}

SOURCE ENTITIES MISSING THIS RELATION:
{src_lines}

CANDIDATE TARGETS ({tgt_type}):
{cand_lines}
"""
    key = _hl.sha256((REPAIR_PROMPT_VERSION + prompt).encode("utf-8")).hexdigest()
    cached = _repair_cache_get(cache_con, key)
    if cached:
        text = cached
    else:
        text = llm.generate_structured_raw(prompt, system=None, temperature=0.0, max_tokens=8000)
        _repair_cache_put(cache_con, key, text)
    from llm import parse_json  # pipeline/ is sys.path[0] when run as a script
    data = parse_json(text)
    links = []
    for lk in data.get("links", []):
        si, ti = lk.get("source"), lk.get("target")
        if not isinstance(si, int) or not (1 <= si <= len(sources)):
            continue
        if ti is None or not isinstance(ti, int) or not (1 <= ti <= len(candidates)):
            continue
        if float(lk.get("confidence", 0)) < 0.6:
            continue
        links.append({"source": sources[si - 1], "target": candidates[ti - 1],
                      "evidence": str(lk.get("evidence", ""))[:200],
                      "confidence": float(lk.get("confidence", 0.8))})
    return links


# ------------------------------------------------- embedding near-dup merge ----
DEDUP_PROMPT_VERSION = "dedup-v1"
DEDUP_COS_MIN = 0.80        # candidate threshold (unrelated pairs measured ~0.67)
DEDUP_MAX_PER_ENTITY = 4    # candidate pairs kept per entity
DEDUP_MAX_PAIRS = 400       # global adjudication cap


def llm_adjudicate_pairs(etype: str, pairs: List[Tuple[dict, dict, float]],
                         llm, cache_con) -> List[Tuple[dict, dict, float, str]]:
    """LLM decides, per embedding-generated candidate pair, whether the two
    entities denote the same real-world thing. Returns confirmed merges."""
    import hashlib as _hl
    lines = []
    for i, (a, b, c) in enumerate(pairs):
        lines.append(f"[{i+1}] A: {a['displayName']} — {(a['description'] or '')[:250]}\n"
                     f"    B: {b['displayName']} — {(b['description'] or '')[:250]}")
    prompt = f"""You are adjudicating entity merges for a DSO knowledge graph extracted from "Hands-On Machine Learning" (Géron, 1st ed.). All pairs below have DSO type {etype} and high embedding similarity.

For each numbered pair decide whether A and B denote the SAME real-world thing and should be merged into one entity.
MERGE when: naming variants, singular/plural, abbreviation vs full name, or trivially different phrasing of one concept/activity/claim (e.g. stage "Data Exploration" vs "Explore and Visualize Data").
Do NOT merge when: distinct siblings/variants that coexist in the textbook (e.g. "Random Forest Classifier" vs "Random Forest Regressor", "Batch Gradient Descent" vs "Stochastic Gradient Descent"), a general concept vs a specific method/instance of it, two different claims/guidelines, or merely topically similar items.

Output STRICT JSON only:
{{"verdicts": [{{"pair": <int>, "same": <true|false>, "reason": "<= 20 words"}}]}}

PAIRS:
{chr(10).join(lines)}
"""
    key = _hl.sha256((DEDUP_PROMPT_VERSION + prompt).encode("utf-8")).hexdigest()
    cached = _repair_cache_get(cache_con, key)
    if cached:
        text = cached
    else:
        text = llm.generate_structured_raw(prompt, system=None, temperature=0.0, max_tokens=8000)
        _repair_cache_put(cache_con, key, text)
    from llm import parse_json
    data = parse_json(text)
    out = []
    for v in data.get("verdicts", []):
        pi = v.get("pair")
        if isinstance(pi, int) and 1 <= pi <= len(pairs) and v.get("same") is True:
            a, b, c = pairs[pi - 1]
            out.append((a, b, c, str(v.get("reason", ""))[:120]))
    return out


def embedding_dedup(entities: Dict[str, dict], cache_path: Path):
    """Same-type entities -> embedding candidate pairs -> LLM adjudication ->
    union-find merge. Mutates `entities` (survivors absorb merged records) and
    returns (dedup_map {absorbed_uri: survivor_uri}, log entries)."""
    import numpy as np
    if str(ROOT / "pipeline") not in sys.path:
        sys.path.insert(0, str(ROOT / "pipeline"))
    from embeddings import embed, entity_embed_text

    by_type: Dict[str, List[str]] = defaultdict(list)
    for u, r in entities.items():
        by_type[r["type"]].append(u)

    cand: List[Tuple[str, str, float]] = []
    for t, uris in sorted(by_type.items()):
        if len(uris) < 2:
            continue
        V = embed([entity_embed_text(entities[u]) for u in uris])
        S = V @ V.T
        iu = np.triu_indices(len(uris), k=1)
        scored = sorted(((float(S[i, j]), uris[i], uris[j])
                         for i, j in zip(*iu) if float(S[i, j]) >= DEDUP_COS_MIN),
                        reverse=True)
        per_ent: Counter = Counter()
        for c, a, b in scored:
            if per_ent[a] >= DEDUP_MAX_PER_ENTITY or per_ent[b] >= DEDUP_MAX_PER_ENTITY:
                continue
            per_ent[a] += 1
            per_ent[b] += 1
            cand.append((a, b, c))
        if len(cand) > DEDUP_MAX_PAIRS:
            break
    cand = cand[:DEDUP_MAX_PAIRS]
    if not cand:
        return {}, []

    cache_con = sqlite3.connect(str(cache_path))
    llm = None
    confirmed: List[Tuple[str, str, float, str]] = []
    by_type_pairs: Dict[str, List[Tuple[str, str, float]]] = defaultdict(list)
    for a, b, c in cand:
        by_type_pairs[entities[a]["type"]].append((a, b, c))
    for t, plist in by_type_pairs.items():
        for i in range(0, len(plist), 12):           # batch 12 pairs per LLM call
            chunk = plist[i:i + 12]
            recs = [(entities[a], entities[b], c) for a, b, c in chunk]
            if llm is None:
                from llm import QwenProvider
                llm = QwenProvider()
            for a_rec, b_rec, c, why in llm_adjudicate_pairs(t, recs, llm, cache_con):
                confirmed.append((a_rec["uri"], b_rec["uri"], c, why))
    cache_con.close()
    if not confirmed:
        return {}, []

    # union-find; survivor = most appearances, then longest description
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        ka = (len(entities[ra]["appearances"]), len(entities[ra]["description"] or ""))
        kb = (len(entities[rb]["appearances"]), len(entities[rb]["description"] or ""))
        keep, absorb = (ra, rb) if ka >= kb else (rb, ra)
        parent[absorb] = keep

    for a, b, _c, _why in confirmed:
        union(a, b)

    groups: Dict[str, List[str]] = defaultdict(list)
    for u in list(parent):
        groups[find(u)].append(u)
    dedup_map: Dict[str, str] = {}
    log = []
    cos_by_pair = {(min(a, b), max(a, b)): (c, w) for a, b, c, w in confirmed}
    for root, members in groups.items():
        for u in members:
            if u == root:
                continue
            dedup_map[u] = root
            surv, absd = entities[root], entities[u]
            surv["alternateName"] = sorted(
                (set(surv["alternateName"]) | {absd["displayName"]} | set(absd["alternateName"]))
                - {surv["displayName"]})
            if len(absd["description"] or "") > len(surv["description"] or ""):
                surv["description"] = absd["description"]
            for pn, pv in absd["props"].items():
                cur = surv["props"].get(pn)
                if cur is None or (isinstance(cur, str) and isinstance(pv, str) and len(pv) > len(cur)):
                    surv["props"][pn] = pv
            surv["appearances"].extend(absd["appearances"])
            key = (min(root, u), max(root, u))
            c, why = cos_by_pair.get(key, (0.0, ""))
            log.append((surv["displayName"], absd["displayName"], surv["type"], c, why))
    return dedup_map, log


def fragment_role(title: str, summary: str) -> str:
    text = (title + " " + summary).lower()
    if re.search(r"project|workflow|step|checklist|end-to-end|pipeline|process|procedure", text):
        return "procedure_description"
    if re.search(r"introduc|what is|overview|landscape|concept|definition", text):
        return "definition"
    if re.search(r"assumption|limitation|bias|variance|theory|why|understand", text):
        return "background_theory"
    return "method_description"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-shacl", action="store_true")
    ap.add_argument("--no-repair", action="store_true")
    ap.add_argument("--no-dedup", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    raw, obj, anc, flat = load_profile()
    frags = {f["fragment_id"]: f for f in (json.loads(l) for l in open(FRAGMENTS_PATH, encoding="utf-8"))}
    extractions = {}
    for l in open(EXTRACTIONS_PATH, encoding="utf-8"):
        rec = json.loads(l)
        if "result" in rec:
            extractions[rec["fragment_id"]] = rec
    print(f"fragments: {len(frags)} | extracted: {len(extractions)}")

    # ---------------------------------------------------------- resolution --
    canon: Dict[Tuple[str, str], dict] = {}     # (type, normkey) -> record
    alias_index: Dict[Tuple[str, str], Tuple[str, str]] = {}  # (type, aliaskey) -> canon key
    used_uris = set()
    frag_map: Dict[str, Dict[str, str]] = defaultdict(dict)   # fragment_id -> slug -> uri
    merges = 0

    def register(frag_id: str, e: dict) -> str:
        nonlocal merges
        etype = e["type"]
        keys = [norm_name(e["displayName"])] + [norm_name(a) for a in e.get("alternateName", [])]
        hit = None
        for k in keys:
            if (etype, k) in canon:
                hit = (etype, k)
                break
            if (etype, k) in alias_index:
                hit = alias_index[(etype, k)]
                break
        if hit is None:
            uri = f"{INST}{kebab(etype)}/{slug(e['displayName'])}"
            n = 2
            while uri in used_uris:
                uri = f"{INST}{kebab(etype)}/{slug(e['displayName'])}-{n}"
                n += 1
            used_uris.add(uri)
            rec = {
                "uri": uri, "type": etype, "displayName": e["displayName"],
                "nameVotes": Counter({e["displayName"]: 1}),
                "alternateName": set(e.get("alternateName", [])),
                "description": e.get("description", ""), "props": {},
                "appearances": [],
            }
            key = (etype, keys[0])
            canon[key] = rec
            for k in keys:
                alias_index[(etype, k)] = key
        else:
            rec = canon[hit]
            merges += 1
            rec["nameVotes"][e["displayName"]] += 1
            rec["alternateName"].update(e.get("alternateName", []))
            if len(e.get("description", "")) > len(rec["description"]):
                rec["description"] = e["description"]
            for k in keys:
                alias_index.setdefault((etype, k), hit)
        # merge props (first non-empty wins; keep longest strings)
        for pn, pv in (e.get("props") or {}).items():
            cur = rec["props"].get(pn)
            if cur is None or (isinstance(cur, str) and isinstance(pv, str) and len(pv) > len(cur)):
                rec["props"][pn] = pv
        rec["appearances"].append({
            "fragment_id": frag_id, "evidence": e.get("evidence", ""),
            "confidence": e.get("confidence", 0.8)})
        frag_map[frag_id][e["slug"]] = rec["uri"]
        return rec["uri"]

    for fid in sorted(extractions):
        for e in extractions[fid]["result"]["entities"]:
            register(fid, e)

    # finalize display names
    entities: Dict[str, dict] = {}
    for rec in canon.values():
        rec["displayName"] = rec["nameVotes"].most_common(1)[0][0]
        rec["alternateName"] = sorted(a for a in rec["alternateName"] if a != rec["displayName"])
        rec.pop("nameVotes")
        entities[rec["uri"]] = rec
    print(f"canonical entities: {len(entities)} (merged {merges} duplicates)")
    print("by type:", dict(Counter(r["type"] for r in entities.values()).most_common()))

    # corpus-level modeling decision (documented in README): the whole textbook
    # belongs to the machine-learning domain, so provide that IndustryDomain
    # instance explicitly — DSO requires every DomainConcept to have one
    # (domainConceptInIndustryDomain outMin=1) and the extracted domains
    # ("Real Estate") only cover the ch2 running example.
    ML_DOMAIN_URI = INST + "industry-domain/machine-learning"
    ml_ev_frag, ml_ev_quote = None, ""
    if not any(r["type"] == "IndustryDomain" and "machine learning" in norm_name(r["displayName"])
               for r in entities.values()):
        for fid, fr in sorted(frags.items()):
            i = fr["text"].lower().find("machine learning")
            if i >= 0:
                ml_ev_frag = fid
                ml_ev_quote = re.sub(r"\s+", " ",
                                     fr["text"][max(0, i - 60):i + 120]).strip()[:200]
                break
        entities[ML_DOMAIN_URI] = {
            "uri": ML_DOMAIN_URI, "type": "IndustryDomain",
            "displayName": "Machine Learning", "alternateName": ["ML"],
            "description": ("Corpus-level domain assertion: the whole textbook covers the "
                            "machine-learning / data-science domain. Added at build time "
                            "as an explicit modeling decision (see README)."),
            "props": {"knowledgeOriginRole": "data_science"},
            "appearances": ([{"fragment_id": ml_ev_frag, "evidence": ml_ev_quote,
                              "confidence": 1.0}] if ml_ev_frag else []),
        }
        used_uris.add(ML_DOMAIN_URI)
        print(f"synthesized corpus-level IndustryDomain: {ML_DOMAIN_URI}")

    # ------------------------- embedding near-duplicate merge (before rels) --
    if not args.no_dedup:
        try:
            dedup_map, dedup_log = embedding_dedup(entities, DATA / "cache.sqlite")
        except Exception as e:  # noqa: BLE001 — build must not fail on dedup
            print(f"!! embedding dedup skipped ({type(e).__name__}: {e})")
            dedup_map, dedup_log = {}, []
        if dedup_map:
            for u in dedup_map:
                del entities[u]
            for _fid, m in frag_map.items():
                for s_, u in list(m.items()):
                    m[s_] = dedup_map.get(u, u)
            print(f"embedding dedup: {len(dedup_map)} entities merged into survivors")
            for name_s, name_a, t, c, why in sorted(dedup_log):
                print(f"  merged '{name_a}' -> '{name_s}' ({t}, cos={c:.3f}) {why}")

    # relations (dedup + evidence)
    rels: Dict[Tuple[str, str, str], dict] = {}
    unresolved = 0
    for fid in sorted(extractions):
        for r in extractions[fid]["result"]["relations"]:
            s = frag_map[fid].get(r["source"])
            t = frag_map[fid].get(r["target"])
            if not s or not t:
                unresolved += 1
                continue
            key = (s, r["predicate"], t)
            rec = rels.setdefault(key, {"evidence": [], "confidence": 0.0})
            rec["evidence"].append({"fragment_id": fid, "quote": r.get("evidence", "")})
            rec["confidence"] = max(rec["confidence"], r.get("confidence", 0.8))
    print(f"canonical relations: {len(rels)} (unresolved endpoint refs: {unresolved})")

    # ------------------------------------------------- cardinality repair ----
    # The extraction prompt only allowed within-fragment relations, so edges
    # whose endpoints live in different fragments (e.g. DomainConcept ->
    # IndustryDomain) are systematically missing. This stage restores DSO
    # cardinality conformance deterministically where possible and via
    # closed-set LLM entity linking otherwise (candidates = existing entities
    # only; a link must cite supporting evidence).
    if not args.no_repair:
        raw_rels = raw["relationTypes"]
        max1_out = {n for n, r in raw_rels.items() if r.get("outgoingMax") == 1}
        req_out = defaultdict(list)
        for n, r in raw_rels.items():
            if r.get("outgoingMin", 0) >= 1:
                req_out[r["sourceType"]].append((n, r["targetType"], r.get("description", "")))

        # 1) dedup maxCount=1 outgoing predicates: keep highest confidence
        groups: Dict[Tuple[str, str], List[Tuple[str, str, str]]] = defaultdict(list)
        for k in rels:
            groups[(k[0], k[1])].append(k)
        removed_max = 0
        for (_s, p), keys in groups.items():
            if p in max1_out and len(keys) > 1:
                keep = max(keys, key=lambda k: rels[k]["confidence"])
                for k in keys:
                    if k != keep:
                        del rels[k]
                        removed_max += 1

        cache_con = sqlite3.connect(str(DATA / "cache.sqlite"))
        llm = None

        def required_missing() -> List[Tuple[str, str, str, str]]:
            have: Dict[str, set] = defaultdict(set)
            for (s_u, p, _t) in rels:
                have[s_u].add(p)
            miss = []
            for u, rec in entities.items():
                need: Dict[str, Tuple[str, str]] = {}
                for a in anc(rec["type"]):
                    for (pn, tt, pd) in req_out.get(a, []):
                        need.setdefault(pn, (tt, pd))
                for pn, (tt, pd) in need.items():
                    if pn not in have.get(u, ()):
                        miss.append((u, pn, tt, pd))
            return miss

        def first_evidence(rec) -> Tuple[str, str]:
            for a in rec["appearances"]:
                if a.get("evidence"):
                    return a["evidence"], a["fragment_id"]
            return "", rec["appearances"][0]["fragment_id"]

        def edge_ok(s_u: str, p: str, t_u: str) -> bool:
            r = raw_rels.get(p)
            if not r or s_u not in entities or t_u not in entities:
                return False
            return (r["sourceType"] in anc(entities[s_u]["type"])
                    and r["targetType"] in anc(entities[t_u]["type"]))

        repaired = 0
        retyped: List[Tuple[str, str, str, str]] = []   # (uri, pred, old_type, new_type)
        dropped_entities: List[Tuple[str, str]] = []

        def fallback_unlinkable(u: str, pn: str) -> bool:
            """Last resort for an entity that cannot satisfy a required relation.
            Prefer a conservative type downgrade (content preserved, weaker DSO
            commitments) over dropping:
              - Stage w/o WorkflowPattern content  -> ActivityPattern (plain step)
              - MethodImplementation w/o a Method  -> SoftwareComponent (function)
              - DataCondition w/o DataCharacteristic -> DomainConcept
            Drop only when no downgrade applies."""
            t = entities[u]["type"]
            if t == "Stage" and pn == "stageContainsWorkflowPattern":
                entities[u]["type"] = "ActivityPattern"
                so = entities[u]["props"].pop("stageOrder", None)
                if so is not None:
                    entities[u]["props"].setdefault("activityOrder", so)
                retyped.append((u, pn, t, "ActivityPattern"))
                return True
            if t == "MethodImplementation" and pn == "implementationImplementsMethod":
                entities[u]["type"] = "SoftwareComponent"
                entities[u]["props"].setdefault("componentType", "function")
                retyped.append((u, pn, t, "SoftwareComponent"))
                return True
            if t == "DataCondition" and pn == "dataConditionCharacterizes":
                entities[u]["type"] = "DomainConcept"
                retyped.append((u, pn, t, "DomainConcept"))
                return True
            dropped_entities.append((u, pn))
            del entities[u]
            return False

        for _round in range(4):
            miss = required_missing()
            if not miss:
                break
            by_pred: Dict[Tuple[str, str, str], List[str]] = defaultdict(list)
            for u, pn, tt, pd in miss:
                by_pred[(pn, tt, pd)].append(u)
            changed = False
            for (pn, tt, pd), uris in by_pred.items():
                uris = [u for u in uris if u in entities]
                if not uris:
                    continue
                # deterministic: textbook-wide ML domain for every DomainConcept
                if pn == "domainConceptInIndustryDomain":
                    for u in uris:
                        key = (u, pn, ML_DOMAIN_URI)
                        if key not in rels:
                            rels[key] = {"evidence": [{"fragment_id": ml_ev_frag or "",
                                                        "quote": ml_ev_quote}],
                                         "confidence": 1.0}
                            repaired += 1
                            changed = True
                    continue
                # deterministic: the ch2 end-to-end project template is worked on
                # the California-housing REGRESSION task (corpus-level fact). If
                # the LLM found no justified problem-type link, rescue the single
                # best template variant (most surviving stage edges) this way.
                if pn == "solutionTemplateApplicableToProblemType":
                    tgt = None
                    for want in ("housing price prediction", "regression"):
                        for u2, r2 in entities.items():
                            if r2["type"] == "ProblemType" and norm_name(r2["displayName"]) == want:
                                tgt = u2
                                break
                        if tgt:
                            break
                    def n_stages(u):
                        return sum(1 for (s_, p_, t_) in rels
                                   if s_ == u and p_ == "solutionTemplateIncludesStage"
                                   and t_ in entities and entities[t_]["type"] == "Stage")
                    best = max(uris, key=n_stages)
                    if tgt and n_stages(best) > 0:
                        ev, fid = first_evidence(entities[best])
                        rels[(best, pn, tgt)] = {
                            "evidence": [{"fragment_id": fid, "quote": ev}],
                            "confidence": 0.9}
                        repaired += 1
                        changed = True
                        uris = [u for u in uris if u != best]
                        if not uris:
                            continue
                cands = [{"uri": u, "name": r["displayName"], "desc": r["description"],
                          "type": r["type"]}
                         for u, r in entities.items() if tt in anc(r["type"])]
                if not cands:
                    for u in uris:
                        fallback_unlinkable(u, pn)
                    changed = True
                    continue
                srcs = []
                for u in uris:
                    r = entities[u]
                    ev, fid = first_evidence(r)
                    srcs.append({"uri": u, "name": r["displayName"], "desc": r["description"],
                                 "evidence": ev, "fragment_id": fid, "type": r["type"]})
                if llm is None:
                    from llm import QwenProvider
                    llm = QwenProvider()
                links = llm_link_group(pn, pd, tt, srcs, cands, llm, cache_con)
                linked = {lk["source"]["uri"] for lk in links}
                for lk in links:
                    key = (lk["source"]["uri"], pn, lk["target"]["uri"])
                    if key in rels:
                        linked.add(key[0])
                        continue
                    rels[key] = {"evidence": [{"fragment_id": lk["source"]["fragment_id"],
                                                "quote": lk["evidence"]}],
                                 "confidence": lk["confidence"]}
                    repaired += 1
                    changed = True
                for u in uris:
                    if u not in linked and u in entities:
                        fallback_unlinkable(u, pn)
                        changed = True
            # prune edges whose endpoints vanished or whose domain/range broke
            # (e.g. after Stage -> ActivityPattern retyping)
            before = len(rels)
            rels = {k: v for k, v in rels.items() if edge_ok(*k)}
            if len(rels) != before:
                changed = True
            if not changed:
                break
        print(f"repair: dedup_max1={removed_max} llm_links={repaired} "
              f"retyped={len(retyped)} dropped={len(dropped_entities)}")
        for u, pn, ot, nt in retyped:
            print(f"  retyped {ot}->{nt}: {u.rsplit('/', 1)[-1]} ({pn} unlinkable)")
        for u, pn in dropped_entities:
            print(f"  dropped {u.rsplit('/', 1)[-1]} (no justified {pn})")
        cache_con.close()

    # ------------------------------------------------------------- triplets --
    T = []  # (s, p, o, datatype|None)  — o is URI if startswith http

    def add(s, p, o, dt=None):
        T.append((s, p, o, dt))

    def add_typed(uri: str, etype: str):
        add(uri, RDF_TYPE, DS + etype)
        for a in anc(etype):
            if a != etype:
                add(uri, RDF_TYPE, DS + a)

    # book
    add_typed(BOOK_URI, "KnowledgeSource")
    add(BOOK_URI, DS + "identifier", "holml-1e")
    add(BOOK_URI, DS + "displayName", "Hands-On Machine Learning with Scikit-Learn and TensorFlow")
    add(BOOK_URI, RDFS_LABEL, "Hands-On Machine Learning with Scikit-Learn and TensorFlow")
    add(BOOK_URI, DS + "sourceType", "textbook")
    add(BOOK_URI, DS + "sourceTitle", "Hands-On Machine Learning with Scikit-Learn and TensorFlow")
    add(BOOK_URI, DS + "authors", "Aurélien Géron")
    add(BOOK_URI, DS + "publisherOrVenue", "O'Reilly Media")
    add(BOOK_URI, DS + "publicationDate", "2017-03-10", XSD_DATE)
    add(BOOK_URI, DS + "resourceVersion", "1st edition")
    add(BOOK_URI, DS + "description", "Data science / machine learning textbook used as corpus for the DSO Reference Knowledge layer.")

    # chapter fragments from TOC
    toc = json.load(open(TOC_PATH, encoding="utf-8"))
    chap_entries = []
    for lvl, title, page in toc:
        m = re.match(r"^Chapter (\d+)\.\s+(.*)$", str(title).strip())
        if lvl == 2 and m and "Exercise Solutions" not in title:
            chap_entries.append((int(m.group(1)), m.group(2).strip(), page))
    chap_entries.sort(key=lambda x: x[2])
    book_end = 565
    for lvl, title, page in toc:
        if lvl == 1 and str(title).strip().startswith("Appendix"):
            book_end = page
            break
    chapter_uri = {}
    for i, (no, title, start) in enumerate(chap_entries):
        if no not in BOOK_CHAPTERS:
            continue
        end = chap_entries[i + 1][2] if i + 1 < len(chap_entries) else book_end
        uri = f"{INST}source-fragment/ch{no:02d}"
        chapter_uri[no] = uri
        add_typed(uri, "SourceFragment")
        add(uri, DS + "identifier", f"ch{no:02d}")
        add(uri, DS + "displayName", f"Chapter {no}. {title}")
        add(uri, RDFS_LABEL, f"Chapter {no}. {title}")
        add(uri, DS + "fragmentType", "chapter")
        add(uri, DS + "fragmentRole", "other")
        add(uri, DS + "fragmentLocator", f"PDF pp.{start}-{end - 1}")
        add(BOOK_URI, DS + "sourceHasFragment", uri)
        add(uri, DS + "resourceHasSource", BOOK_URI)

    # section/sub fragments
    summaries = {fid: extractions[fid]["result"].get("sectionSummary", "") for fid in extractions}
    for fid, fr in sorted(frags.items()):
        uri = f"{INST}source-fragment/{fid}"
        add_typed(uri, "SourceFragment")
        add(uri, DS + "identifier", fid)
        add(uri, DS + "displayName", fr["section_title"])
        add(uri, RDFS_LABEL, fr["section_title"])
        add(uri, DS + "fragmentType", {"section": "section", "sub": "subsection"}[fr["level"]])
        add(uri, DS + "fragmentRole", fragment_role(fr["section_title"], summaries.get(fid, "")))
        add(uri, DS + "fragmentLocator", fr["locator"])
        add(uri, DS + "contentHash", hashlib.sha256(fr["text"].encode()).hexdigest()[:16])
        if summaries.get(fid):
            add(uri, DS + "fragmentSummary", summaries[fid])
        add(BOOK_URI, DS + "sourceHasFragment", uri)
        add(uri, DS + "resourceHasSource", BOOK_URI)
        parent = fr.get("parent_id")
        if parent:
            add(uri, DS + "fragmentPartOfFragment", f"{INST}source-fragment/{parent}")
        else:
            add(uri, DS + "fragmentPartOfFragment", chapter_uri.get(fr["chapter_no"], BOOK_URI))

    # entities
    claim_types = {"ReferenceClaim", "Guideline", "Constraint"}
    for uri, rec in sorted(entities.items()):
        etype = rec["type"]
        add_typed(uri, etype)
        add(uri, DS + "identifier", uri.rsplit("/", 1)[-1])
        add(uri, DS + "displayName", rec["displayName"])
        add(uri, RDFS_LABEL, rec["displayName"])
        for a in rec["alternateName"]:
            add(uri, DS + "alternateName", a)
        if rec["description"]:
            add(uri, DS + "description", rec["description"])
        for pn, pv in rec["props"].items():
            if pn in SKIP_PROPS or bad_prop_value(pv):
                continue
            pdef = flat.get(etype, {}).get(pn, {})
            sv = str(pv).strip()
            if is_int_prop(pdef) and sv.lstrip("-").isdigit():
                add(uri, DS + pn, sv, XSD_INT)
            elif is_float_prop(pdef):        # SHACL shapes require xsd:decimal
                try:
                    float(sv)
                    add(uri, DS + pn, sv, XSD_DECIMAL)
                except ValueError:
                    pass                     # drop non-numeric for float-typed prop
            elif is_date_prop(pdef):
                dv = date_value(sv)
                if dv:
                    add(uri, DS + pn, dv, XSD_DATE)
            else:
                add(uri, DS + pn, sv)
        add(uri, DS + "resourceHasSource", BOOK_URI)
        frag_uris = {a["fragment_id"] for a in rec["appearances"]}
        # KnowledgeSource is NOT a ReferenceResource in DSO v0.2, so
        # resourceGroundedInFragment (domain ReferenceResource) does not apply;
        # fragment provenance of extracted citations lives in the sidecar only.
        grounded = None if etype == "KnowledgeSource" else \
            DS + ("claimGroundedInFragment" if etype in claim_types else "resourceGroundedInFragment")
        for fid in sorted(frag_uris):
            if grounded:
                add(uri, grounded, f"{INST}source-fragment/{fid}")

    # relations
    for (s, p, t) in sorted(rels):
        add(s, DS + p, t)

    print(f"total triplets: {len(T)}")

    # --------------------------------------------------------- store (ttl) --
    # NOTE: semantica's TripletStore oxigraph backend needs a file-backed
    # pyoxigraph Store, which segfaults on this Windows machine (pyoxigraph
    # 0.5.13 / RocksDB). Runtime store is therefore rdflib + Turtle file;
    # the query layer loads data/dso_kg.ttl in memory (<1s at this scale).
    if OXI_PATH.exists():
        shutil.rmtree(OXI_PATH)  # remove stale dir from earlier attempts
    import rdflib
    from rdflib import Literal, URIRef
    g = rdflib.Graph()
    for s, p, o, dt in T:
        obj = URIRef(o) if str(o).startswith("http") else Literal(str(o), datatype=URIRef(dt) if dt else None)
        g.add((URIRef(s), URIRef(p), obj))
    g.serialize(destination=str(TTL_PATH), format="turtle")
    print(f"TTL written: {TTL_PATH} ({len(g)} triples)")

    # ------------------------------------------------------------ sidecar ----
    if SQLITE_PATH.exists():
        SQLITE_PATH.unlink()
    con = sqlite3.connect(str(SQLITE_PATH))
    con.executescript("""
    CREATE TABLE fragments(fragment_id TEXT PRIMARY KEY, chapter_no INT, chapter_title TEXT,
      section_title TEXT, locator TEXT, page_start INT, page_end INT, level TEXT,
      parent_id TEXT, unit INT, summary TEXT, text TEXT);
    CREATE TABLE entities(uri TEXT PRIMARY KEY, type TEXT, display_name TEXT,
      description TEXT, aliases TEXT, props_json TEXT);
    CREATE TABLE entity_fragments(uri TEXT, fragment_id TEXT, evidence TEXT, confidence REAL);
    CREATE TABLE relation_evidence(s TEXT, p TEXT, o TEXT, fragment_id TEXT, quote TEXT, confidence REAL);
    CREATE INDEX idx_ef_uri ON entity_fragments(uri);
    CREATE INDEX idx_re_s ON relation_evidence(s);
    CREATE INDEX idx_re_o ON relation_evidence(o);
    """)
    fts = True
    try:
        con.execute("CREATE VIRTUAL TABLE entities_fts USING fts5(uri UNINDEXED, type UNINDEXED, name, aliases, description)")
    except sqlite3.OperationalError:
        fts = False
    for fid, fr in frags.items():
        con.execute("INSERT INTO fragments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fid, fr["chapter_no"], fr["chapter_title"], fr["section_title"],
                     fr["locator"], fr["page_start"], fr["page_end"], fr["level"],
                     fr.get("parent_id"), int(bool(fr.get("unit"))), summaries.get(fid, ""), fr["text"]))
    for uri, rec in entities.items():
        con.execute("INSERT INTO entities VALUES(?,?,?,?,?,?)",
                    (uri, rec["type"], rec["displayName"], rec["description"],
                     json.dumps(rec["alternateName"], ensure_ascii=False),
                     json.dumps(rec["props"], ensure_ascii=False)))
        for a in rec["appearances"]:
            con.execute("INSERT INTO entity_fragments VALUES(?,?,?,?)",
                        (uri, a["fragment_id"], a["evidence"], a["confidence"]))
        if fts:
            con.execute("INSERT INTO entities_fts VALUES(?,?,?,?,?)",
                        (uri, rec["type"], rec["displayName"],
                         " ".join(rec["alternateName"]), rec["description"]))
    for (s, p, t), rec in rels.items():
        for ev in rec["evidence"]:
            con.execute("INSERT INTO relation_evidence VALUES(?,?,?,?,?,?)",
                        (s, p, t, ev["fragment_id"], ev["quote"], rec["confidence"]))
    # vector index for hybrid retrieval: FTS5 bm25 + cosine, fused via RRF in
    # query/dso_kg.py. Written last so it covers the FINAL entity set (after
    # dedup + repair retypes). Failure degrades queries to FTS-only.
    try:
        con.execute("CREATE TABLE entity_embeddings(uri TEXT PRIMARY KEY, model TEXT, dim INT, vec BLOB)")
        if str(ROOT / "pipeline") not in sys.path:
            sys.path.insert(0, str(ROOT / "pipeline"))
        from embeddings import embed, entity_embed_text, EMBED_VERSION
        emb_uris = sorted(entities)
        V = embed([entity_embed_text(entities[u]) for u in emb_uris])
        con.executemany("INSERT INTO entity_embeddings VALUES(?,?,?,?)",
                        [(u, EMBED_VERSION, int(V.shape[1]), V[i].tobytes())
                         for i, u in enumerate(emb_uris)])
        print(f"entity embeddings: {len(emb_uris)} x {int(V.shape[1])} ({EMBED_VERSION})")
        # fragment-level vectors (title + summary + head): powers fragment
        # retrieval in graphrag_context for enumeration-style questions
        con.execute("CREATE TABLE fragment_embeddings(fragment_id TEXT PRIMARY KEY, "
                    "model TEXT, dim INT, vec BLOB)")
        fids = sorted(frags)
        ftexts = []
        for fid in fids:
            fr = frags[fid]
            ftexts.append(" | ".join(x for x in [
                fr.get("section_title", ""), summaries.get(fid, ""),
                re.sub(r"\s+", " ", fr["text"])[:400]] if x))
        FV = embed(ftexts)
        con.executemany("INSERT INTO fragment_embeddings VALUES(?,?,?,?)",
                        [(fid, EMBED_VERSION, int(FV.shape[1]), FV[i].tobytes())
                         for i, fid in enumerate(fids)])
        print(f"fragment embeddings: {len(fids)} x {int(FV.shape[1])} ({EMBED_VERSION})")
    except Exception as e:  # noqa: BLE001
        print(f"!! entity embeddings skipped ({type(e).__name__}: {e}); queries fall back to FTS-only")
    con.commit()
    con.close()
    print(f"sidecar written: {SQLITE_PATH} (fts5={'yes' if fts else 'no'})")
    ENTITIES_PATH.write_text(json.dumps(
        {u: {k: v for k, v in r.items()} for u, r in sorted(entities.items())},
        ensure_ascii=False, indent=1), encoding="utf-8")

    # --------------------------------------------------------------- SHACL --
    if not args.no_shacl:
        try:
            from pyshacl import validate
            conforms, _, results_text = validate(
                str(TTL_PATH), shacl_graph=str(SHAPES_PATH),
                data_graph_format="turtle", shacl_graph_format="turtle",
                inference="none", abort_on_first=False)
            (DATA / "shacl_report.txt").write_text(results_text, encoding="utf-8")
            n_viol = results_text.count("Severity: sh:Violation")
            print(f"SHACL conforms={conforms} violations={n_viol} -> data/shacl_report.txt")
        except Exception as e:  # noqa: BLE001
            print("SHACL validation skipped due to error:", type(e).__name__, e)

    # predicate stats
    pc = Counter(p.rsplit("/", 1)[-1] if "/" in p else p for _, p, _, _ in T)
    print("top predicates:", dict(pc.most_common(12)))


if __name__ == "__main__":
    main()
