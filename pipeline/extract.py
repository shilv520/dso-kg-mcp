#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""extract.py — DSO Reference-Knowledge schema-guided extraction (qwen3.8-max).

对每个抽取单元（fragment）调用 LLM，输出封闭词表约束下的实体/关系/Claim JSON，
经 profile 校验（类型、谓词 domain/range、枚举、必填属性）后写入 data/extractions.jsonl。
LLM 响应按 sha256(prompt) 缓存于 data/cache.sqlite，重跑免费。

用法:
  python pipeline/extract.py --chapters 7 --limit 2      # 试跑
  python pipeline/extract.py --chapters 2,3,4,7          # 全量 MVP
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
from llm import QwenProvider  # noqa: E402

PROFILE_PATH = ROOT / "schema" / "textbook_profile.json"
FRAGMENTS_PATH = ROOT / "data" / "fragments.jsonl"
CACHE_PATH = ROOT / "data" / "cache.sqlite"
OUT_PATH = ROOT / "data" / "extractions.jsonl"
REPORT_PATH = ROOT / "data" / "extraction_report.json"
PROMPT_VERSION = "v3"
BOOK = ("Hands-On Machine Learning with Scikit-Learn and TensorFlow, "
        "Aurélien Géron, O'Reilly 1st edition 2017")

# provenance relations are minted by build_kg.py, never by the LLM
PROVENANCE_PREDS = {
    "resourceHasSource", "sourceHasFragment", "fragmentPartOfFragment",
    "resourceGroundedInFragment", "claimGroundedInFragment", "referenceClaimRevisionOf",
}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,58}$")
AUTOFILL = {
    "knowledgeOriginRole": "data_science",
    "referenceClaimStatus": "source_asserted",
}


# ---------------------------------------------------------------- profile ----

class Profile:
    def __init__(self, path: Path):
        raw = json.load(open(path, encoding="utf-8"))
        self.ns = raw["ontology"]["namespace"]
        self.obj = raw["objectTypes"]
        self.rel = raw["relationTypes"]
        self.ancestors: Dict[str, set] = {}
        for t in self.obj:
            self._anc(t)
        self.concrete = [t for t, d in self.obj.items() if not d["abstract"]]
        self.flat_props: Dict[str, Dict[str, dict]] = {}
        for t in self.concrete:
            merged: Dict[str, dict] = {}
            for a in self.ancestors[t]:
                for p in self.obj.get(a, {}).get("properties", []):
                    merged.setdefault(p["name"], p)
            self.flat_props[t] = merged

    def _anc(self, t: str) -> set:
        if t in self.ancestors:
            return self.ancestors[t]
        out = {t}
        for s in self.obj.get(t, {}).get("superTypes", []):
            out |= self._anc(s)
        self.ancestors[t] = out
        return out

    def relation_ok(self, pred: str, stype: str, ttype: str) -> bool:
        rd = self.rel.get(pred)
        if not rd:
            return False
        return (rd["sourceType"] in self.ancestors.get(stype, set())
                and rd["targetType"] in self.ancestors.get(ttype, set()))

    def required_props(self, t: str) -> List[str]:
        return [n for n, p in self.flat_props.get(t, {}).items()
                if int(p.get("minCount", 0) or 0) > 0 and n != "identifier"]


# ----------------------------------------------------------------- prompt ----

SYSTEM_PROMPT = f"""You are a meticulous knowledge engineer building the Reference Knowledge layer of a Data Science Ontology (DSO) from a textbook.

You will receive one textbook fragment plus a CLOSED extraction vocabulary (entity types with required/enum properties, and allowed relation types). Follow these rules strictly:

1. Closed schema. Use ONLY the listed concrete entity types and ONLY the listed relation predicates. Never invent types, predicates, or property names.
2. No fabrication. Extract only knowledge the fragment states or directly implies. Every entity/relation/claim needs an "evidence" field: a VERBATIM quote from the fragment (max 25 words) supporting it.
3. Reusable knowledge only. Extract general, reusable data-science knowledge (concepts, methods, implementations, metrics, conditions, claims, workflow/activity patterns). Do NOT extract case-specific runtime facts about the book's demo project as Case knowledge — but DO extract its reusable domain concepts (e.g. housing market concepts) and the general procedures it demonstrates.
4. Entity identity. displayName = canonical English name ("Random Forest", "scikit-learn", "Root Mean Square Error"). Put abbreviations, library class names and surface variants in alternateName (e.g. ["RF"], ["RandomForestClassifier"]). slug = short kebab-case local id, unique within your answer.
5. Type discipline:
   - Method = conceptual algorithm/technique. MethodImplementation = concrete library API (class/function). SoftwareComponent = library/package itself.
   - Metric = what is measured (RMSE, precision). EvaluationProtocol = how estimates are produced (cross-validation, holdout, stratified sampling).
   - ReferenceClaim = a factual/applicability proposition. Guideline = advice/recommendation/warning (advisory). Constraint = hard requirement ("must", violation breaks correctness).
   - Claims: props MUST include claimStatement (one self-contained proposition), claimKind, knowledgeOriginRole, referenceClaimStatus (+ guidelineType for Guideline, constraintSeverity for Constraint). Link each claim to what it is about via claimConcerns*/claimAppliesTo*/claimAppliesUnder*/claimExcludedUnder* relations.
   - Process knowledge: SolutionTemplate (whole project skeleton) includesStage Stage; Stage containsWorkflowPattern WorkflowPattern; WorkflowPattern containsActivityPattern ActivityPattern; use activityOrder/stageOrder for sequence; workflowPatternEntryActivity for the first activity.
   - knowledgeOriginRole: data_science for DS methods/concepts; application_domain for business/domain concepts (housing, districts, etc.).
6. Relations must reference slugs defined in THIS answer (both endpoints). Do not emit provenance relations (groundedIn/hasSource/hasFragment) — they are added automatically.
7. Quality over quantity: typically 5-25 entities, 3-25 relations per fragment. Skip trivial mentions; merge duplicates; confidence in [0,1] reflects how well the fragment supports the item.
8. Output STRICT JSON only (no markdown fences, no commentary):
{{"sectionSummary": "<1-2 sentences>",
  "entities": [{{"slug": "...", "type": "...", "displayName": "...", "alternateName": ["..."], "description": "<=60 words", "props": {{}}, "evidence": "...", "confidence": 0.9}}],
  "relations": [{{"source": "<slug>", "predicate": "...", "target": "<slug>", "evidence": "...", "confidence": 0.9}}]}}"""


def build_vocab(prof: Profile) -> str:
    lines = ["# CLOSED EXTRACTION VOCABULARY (DSO textbook Reference-Knowledge profile)", "",
             "## Entity types"]
    for t in sorted(prof.concrete):
        td = prof.obj[t]
        desc = re.sub(r"\s+", " ", td["description"])[:200]
        lines.append(f"### {t}")
        lines.append(f"  {desc}")
        fp = prof.flat_props.get(t, {})
        req = [n for n in prof.required_props(t)]
        if req:
            parts = []
            for n in req:
                p = fp.get(n, {})
                parts.append(f"{n}[{'|'.join(p['enum'])}]" if p.get("enum") else n)
            lines.append("  REQUIRED props: " + ", ".join(parts))
        opts = []
        for n, p in fp.items():
            if n in req or n == "identifier":
                continue
            opts.append(f"{n}[{'|'.join(p['enum'])}]" if p.get("enum") else n)
        core = [o for o in opts if o.split("[")[0] in
                ("description", "alternateName", "assumptions", "limitations", "metricFormula",
                 "conditionStatement", "conditionOperator", "applicabilitySummary", "guidance",
                 "pitfalls", "objectiveStatement", "stageOrder", "activityOrder",
                 "implementationType", "callablePath", "programmingLanguage",
                 "characteristicKind", "statisticFormula", "evaluationProtocolType",
                 "splitStrategy", "problemCategory", "metricDirection", "methodPurpose",
                 "methodParadigm", "trainingMode", "constraintLanguage", "constraintExpression",
                 "templateVersion", "preconditionSummary", "postconditionSummary")]
        rest = [o for o in opts if o not in core]
        if core:
            lines.append("  useful optional props: " + ", ".join(core))
        if rest:
            lines.append("  other optional props: " + ", ".join(rest))
    lines += ["", "## Relation predicates (source_type -> target_type): description"]
    for r, rd in sorted(prof.rel.items()):
        if r in PROVENANCE_PREDS:
            continue
        desc = re.sub(r"\s+", " ", rd["description"])[:150]
        lines.append(f"- {r} ({rd['sourceType']} -> {rd['targetType']}): {desc}")
    lines += ["", "## Abstract types (never instantiate; use concrete subtypes)"]
    for t, td in sorted(prof.obj.items()):
        if td["abstract"]:
            subs = sorted(s for s, sd in prof.obj.items()
                          if not sd["abstract"] and t in prof.ancestors.get(s, set()))
            lines.append(f"- {t}: {', '.join(subs)}")
    return "\n".join(lines)


def build_user_prompt(fr: dict, vocab: str) -> str:
    return f"""Book: {BOOK}
Fragment: {fr['fragment_id']}
Locator: {fr['locator']}
Chapter: {fr['chapter_no']} — {fr['chapter_title']}
Section: {fr['section_title']}

{vocab}

# TEXTBOOK FRAGMENT (extract DSO Reference Knowledge from this text only)
<<<FRAGMENT
{fr['text']}
FRAGMENT>>>

Now output the strict JSON extraction result for this fragment."""


# -------------------------------------------------------------- validation ---

def norm_enum(val: Any, enum: List[str]) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    for e in enum:
        if s.lower() == e.lower():
            return e
    return None


def validate_payload(payload: dict, prof: Profile, frag_id: str) -> Tuple[dict, dict]:
    dropped: List[dict] = []
    warnings: List[str] = []
    ents_out: List[dict] = []
    by_slug: Dict[str, dict] = {}
    by_name: Dict[str, str] = {}

    for e in payload.get("entities", []) or []:
        if not isinstance(e, dict):
            dropped.append({"kind": "entity", "reason": "not_object", "item": str(e)[:80]})
            continue
        slug = str(e.get("slug", "")).strip().lower()
        etype = e.get("type")
        name = e.get("displayName")
        if not SLUG_RE.match(slug or ""):
            slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:59]
        if etype not in prof.concrete:
            dropped.append({"kind": "entity", "reason": f"unknown_type:{etype}", "item": name or slug})
            continue
        if not name or not slug:
            dropped.append({"kind": "entity", "reason": "missing_name_or_slug", "item": str(e)[:80]})
            continue
        fp = prof.flat_props.get(etype, {})
        props_in = e.get("props") or {}
        props_out: Dict[str, Any] = {}
        ok = True
        for pname, pval in props_in.items():
            if pname not in fp:
                warnings.append(f"{frag_id}:{slug}: dropped unknown prop {pname}")
                continue
            pdef = fp[pname]
            if pdef.get("enum"):
                nv = norm_enum(pval, pdef["enum"])
                if nv is None:
                    dropped.append({"kind": "prop", "reason": f"bad_enum:{pname}={pval}", "item": slug})
                    continue
                props_out[pname] = nv
            elif pdef["type"].startswith("omm:") and "nteger" in pdef["type"]:
                try:
                    props_out[pname] = int(pval)
                except (TypeError, ValueError):
                    warnings.append(f"{frag_id}:{slug}: non-integer {pname}={pval!r}")
            else:
                if isinstance(pval, (dict, list)):
                    pval = json.dumps(pval, ensure_ascii=False)
                props_out[pname] = str(pval).strip()
        # required check with autofill
        for n in prof.required_props(etype):
            if n in props_out and props_out[n] not in ("", None):
                continue
            if n in AUTOFILL:
                props_out[n] = AUTOFILL[n]
                warnings.append(f"{frag_id}:{slug}: autofilled {n}={AUTOFILL[n]}")
            elif n == "displayName":
                continue
            else:
                dropped.append({"kind": "entity", "reason": f"missing_required:{n}", "item": f"{etype}:{slug}"})
                ok = False
                break
        if not ok:
            continue
        alt = e.get("alternateName") or []
        if isinstance(alt, str):
            alt = [alt]
        ent = {
            "slug": slug, "type": etype, "displayName": str(name).strip(),
            "alternateName": [str(a).strip() for a in alt if str(a).strip()][:8],
            "description": re.sub(r"\s+", " ", str(e.get("description", "")))[:1200],
            "props": props_out,
            "evidence": str(e.get("evidence", ""))[:400],
            "confidence": float(e.get("confidence", 0.8) or 0.8),
        }
        if slug in by_slug:
            warnings.append(f"{frag_id}: duplicate slug {slug} merged")
            prev = by_slug[slug]
            for a in ent["alternateName"]:
                if a not in prev["alternateName"]:
                    prev["alternateName"].append(a)
            for k, v in ent["props"].items():
                prev["props"].setdefault(k, v)
            continue
        by_slug[slug] = ent
        by_name[ent["displayName"].lower()] = slug
        for a in ent["alternateName"]:
            by_name.setdefault(a.lower(), slug)
        ents_out.append(ent)

    def resolve(ref: Any) -> Optional[str]:
        if ref is None:
            return None
        s = str(ref).strip()
        if s.lower() in by_slug:
            return s.lower()
        if s in by_slug:
            return s
        return by_name.get(s.lower())

    rels_out = []
    for r in payload.get("relations", []) or []:
        if not isinstance(r, dict):
            dropped.append({"kind": "relation", "reason": "not_object", "item": str(r)[:80]})
            continue
        pred = r.get("predicate")
        src = resolve(r.get("source"))
        tgt = resolve(r.get("target"))
        if pred in PROVENANCE_PREDS:
            dropped.append({"kind": "relation", "reason": "provenance_pred_forbidden", "item": pred})
            continue
        if src is None or tgt is None:
            dropped.append({"kind": "relation", "reason": "unresolved_endpoint",
                            "item": f"{r.get('source')} -{pred}-> {r.get('target')}"})
            continue
        st, tt = by_slug[src]["type"], by_slug[tgt]["type"]
        if not prof.relation_ok(pred, st, tt):
            rd = prof.rel.get(pred)
            expect = f"{rd['sourceType']}->{rd['targetType']}" if rd else "unknown predicate"
            dropped.append({"kind": "relation", "reason": f"domain_range_violation:{pred}({st}->{tt}) expected {expect}",
                            "item": f"{src} -{pred}-> {tgt}"})
            continue
        if src == tgt:
            dropped.append({"kind": "relation", "reason": "self_loop", "item": f"{src} -{pred}-> {tgt}"})
            continue
        rels_out.append({
            "source": src, "predicate": pred, "target": tgt,
            "evidence": str(r.get("evidence", ""))[:400],
            "confidence": float(r.get("confidence", 0.8) or 0.8),
        })

    # low-confidence filter
    ents_out = [e for e in ents_out if e["confidence"] >= 0.5]
    rels_out = [r for r in rels_out if r["confidence"] >= 0.5]
    summary = re.sub(r"\s+", " ", str(payload.get("sectionSummary", "")))[:600]
    return {"sectionSummary": summary, "entities": ents_out, "relations": rels_out}, \
           {"dropped": dropped, "warnings": warnings}


# ------------------------------------------------------------------- cache ---

_CACHE_DDL = ("CREATE TABLE IF NOT EXISTS llm_cache"
              "(key TEXT PRIMARY KEY, fragment_id TEXT, response TEXT, ts REAL)")


def cache_get(fid: str, key: str) -> Optional[str]:
    con = sqlite3.connect(str(CACHE_PATH))
    try:
        con.execute(_CACHE_DDL)
        row = con.execute("SELECT response FROM llm_cache WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def cache_put(key: str, response: str, fid: str):
    con = sqlite3.connect(str(CACHE_PATH))
    try:
        con.execute(_CACHE_DDL)
        con.execute("INSERT OR REPLACE INTO llm_cache VALUES(?,?,?,?)", (key, fid, response, time.time()))
        con.commit()
    finally:
        con.close()


# -------------------------------------------------------------------- run ----

_tls = threading.local()


def get_llm() -> QwenProvider:
    if not hasattr(_tls, "llm"):
        _tls.llm = QwenProvider()
    return _tls.llm


def extract_one(fr: dict, vocab: str, prof: Profile, force: bool) -> dict:
    fid = fr["fragment_id"]
    user = build_user_prompt(fr, vocab)
    key = hashlib.sha256((PROMPT_VERSION + SYSTEM_PROMPT + user).encode("utf-8")).hexdigest()
    t0 = time.time()
    cached = False
    if not force:
        resp_text = cache_get(fid, key)
        cached = resp_text is not None
    else:
        resp_text = None
    if resp_text is None:
        resp_text = get_llm().generate_structured_raw(user, system=SYSTEM_PROMPT,
                                                       temperature=0.2, max_tokens=16000)
        cache_put(key, resp_text, fid)
    from llm import parse_json
    payload = parse_json(resp_text) if isinstance(resp_text, str) else resp_text
    clean, report = validate_payload(payload, prof, fid)
    return {
        "fragment_id": fid, "locator": fr["locator"], "chapter_no": fr["chapter_no"],
        "section_title": fr["section_title"], "cached": cached,
        "n_entities": len(clean["entities"]), "n_relations": len(clean["relations"]),
        "result": clean, "report": report, "seconds": round(time.time() - t0, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chapters", default="all",
                    help="'all' or comma list, e.g. '2,3,4,7'")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    all_frags = [json.loads(l) for l in open(FRAGMENTS_PATH, encoding="utf-8")]
    if args.chapters.strip().lower() == "all":
        units = [f for f in all_frags if f.get("unit")]
    else:
        chaps = {int(c) for c in args.chapters.split(",") if c.strip()}
        units = [f for f in all_frags if f.get("unit") and f["chapter_no"] in chaps]
    units.sort(key=lambda f: (f["chapter_no"], f["fragment_id"]))
    if args.limit:
        units = units[:args.limit]

    prof = Profile(PROFILE_PATH)
    vocab = build_vocab(prof)
    (ROOT / "data" / "vocab_runtime.md").write_text(vocab, encoding="utf-8")
    print(f"extraction units: {len(units)} | vocab chars: {len(vocab)} | prompt {PROMPT_VERSION}")

    results: List[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(extract_one, fr, vocab, prof, args.force): fr for fr in units}
        for fut in as_completed(futs):
            fr = futs[fut]
            done += 1
            try:
                r = fut.result()
                results.append(r)
                print(f"[{done}/{len(units)}] {r['fragment_id']}: "
                      f"entities={r['n_entities']} relations={r['n_relations']} "
                      f"dropped={len(r['report']['dropped'])} {r['seconds']}s"
                      f"{' (cached)' if r['cached'] else ''}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[{done}/{len(units)}] {fr['fragment_id']}: FAILED {type(e).__name__}: {e}", flush=True)
                results.append({"fragment_id": fr["fragment_id"], "error": f"{type(e).__name__}: {e}"})

    # merge into extractions.jsonl by fragment_id
    merged: Dict[str, dict] = {}
    if OUT_PATH.exists():
        for l in open(OUT_PATH, encoding="utf-8"):
            rec = json.loads(l)
            merged[rec["fragment_id"]] = rec
    for r in results:
        merged[r["fragment_id"]] = r
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for fid in sorted(merged):
            f.write(json.dumps(merged[fid], ensure_ascii=False) + "\n")

    ok = [r for r in results if "result" in r]
    rep = {
        "prompt_version": PROMPT_VERSION, "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "units": len(units), "succeeded": len(ok), "failed": len(results) - len(ok),
        "entities": sum(r["n_entities"] for r in ok),
        "relations": sum(r["n_relations"] for r in ok),
        "dropped_total": sum(len(r["report"]["dropped"]) for r in ok),
        "drop_reasons": {},
        "per_fragment": {r["fragment_id"]: {"e": r.get("n_entities"), "r": r.get("n_relations"),
                                            "dropped": len(r.get("report", {}).get("dropped", [])),
                                            "error": r.get("error")} for r in results},
    }
    for r in ok:
        for d in r["report"]["dropped"]:
            key = d["reason"].split(":")[0]
            rep["drop_reasons"][key] = rep["drop_reasons"].get(key, 0) + 1
    REPORT_PATH.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: rep[k] for k in ("units", "succeeded", "failed", "entities",
                                          "relations", "dropped_total", "drop_reasons")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
