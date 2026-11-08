#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""dso_mcp.py — DSO 教材知识图谱 MCP 服务（stdio transport）。

把 query/dso_kg.py 的预定义查询操作暴露为 MCP tools，供 Codex / Claude Code 等
harness 调用，支撑 GraphRAG 式教材问答。所有回答均携带出处（章节/页码/引文）。

运行:  <venv>/Scripts/python.exe -m mcp_server.dso_mcp   （或 python mcp_server/dso_mcp.py）
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from query.dso_kg import DsoKG  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402

_kg: Optional[DsoKG] = None


def kg() -> DsoKG:
    global _kg
    if _kg is None:
        _kg = DsoKG(ROOT / "data")
    return _kg


mcp = MCPServer(
    name="dso-kg",
    title="DSO Textbook Knowledge Graph (Hands-On ML, all 16 chapters)",
    version="0.3.0",
    description=(
        "Reference knowledge graph of the data-science textbook 'Hands-On Machine "
        "Learning with Scikit-Learn and TensorFlow' (Géron, 1st ed., all 16 chapters), "
        "modeled with the DSO v0.2 ontology (Data Science Ontology). Every answer "
        "carries provenance: chapter/section locator, PDF pages, and verbatim evidence "
        "quotes. Retrieval is hybrid (BM25 full-text + embedding vectors, RRF-fused). "
        "Use it to answer data-science questions with textbook grounding (GraphRAG style)."
    ),
    instructions=(
        "Recommended workflow for textbook QA:\n"
        "1. dso_graphrag_context(question) — one-shot compiled context with citations "
        "for a natural-language question; usually enough to answer.\n"
        "2. Drill down when needed: dso_search → dso_get_concept → dso_get_claims → "
        "dso_provenance (mirrors the DSO ch.14 agent retrieval path).\n"
        "3. Method selection questions: dso_find_methods(problem_type, data_condition) "
        "+ dso_get_metrics(problem_type) + dso_get_workflow(problem_type).\n"
        "4. dso_similar(name) to find semantically near entities (embedding cosine) "
        "when keyword search misses paraphrases.\n"
        "5. dso_sparql for arbitrary read-only SPARQL 1.1 over the graph "
        "(prefixes ds:/inst:/rdfs:/rdf:/xsd: are pre-declared).\n"
        "Answers should cite locators like \"Ch.7 'Boosting', PDF pp.215-222\"."
    ),
)


def _md_entity_hit(h: dict) -> str:
    return f"- **{h['name']}** (`{h['type']}`) — {h.get('snippet', '')}  \n  uri: {h['uri']}"


@mcp.tool(
    description="Hybrid search over DSO entities (methods, concepts, claims, metrics, "
                "workflows, …) extracted from the textbook: BM25 full-text fused with "
                "embedding-similarity ranking (RRF), so paraphrased queries still hit. "
                "Returns name, type, snippet, URI. "
                "Optional type filter: Method, DomainConcept, ReferenceClaim, Guideline, "
                "Constraint, Metric, EvaluationProtocol, ProblemType, DataCondition, "
                "DataCharacteristic, SolutionTemplate, Stage, WorkflowPattern, "
                "ActivityPattern, MethodImplementation, SoftwareComponent, ModelSpecification, "
                "IndustryDomain, DataStatistic.",
)
def dso_search(query: str, type: str = "", limit: int = 10) -> str:
    try:
        hits = kg().search(query, type_=type or None, limit=limit)
        if not hits:
            return f"No entities match '{query}'."
        return f"**{len(hits)} match(es) for '{query}'**\n" + "\n".join(_md_entity_hit(h) for h in hits)
    except Exception:
        return "ERROR:\n" + traceback.format_exc(limit=3)


@mcp.tool(
    description="Get the full DSO concept card for one entity (by display name, alias, or URI): "
                "type, description, alternate names, ontology properties, outgoing/incoming "
                "relations with typed neighbors, and provenance (locator + evidence quote).",
)
def dso_get_concept(name: str) -> str:
    try:
        rec = kg().entity(name)
        if not rec:
            return f"No entity found for '{name}'. Try dso_search first."
        lines = [f"# {rec['displayName']} (`{rec['type']}`)",
                 f"uri: {rec['uri']}"]
        if rec.get("alternateName"):
            lines.append("aka: " + ", ".join(rec["alternateName"]))
        if rec.get("description"):
            lines.append("\n" + rec["description"])
        props = {k: v for k, v in (rec.get("props") or {}).items()
                 if k not in ("displayName", "description", "alternateName", "identifier")}
        if props:
            lines.append("\n**Ontology properties**")
            lines += [f"- {k}: {v}" for k, v in props.items()]
        if rec.get("relationsOut"):
            lines.append("\n**Relations (outgoing)**")
            lines += [f"- `{r['predicate']}` → {r['target']} ({r['targetType']})"
                      for r in rec["relationsOut"]]
        if rec.get("relationsIn"):
            lines.append("\n**Relations (incoming)**")
            lines += [f"- {r['source']} ({r['sourceType']}) —`{r['predicate']}`→ this"
                      for r in rec["relationsIn"]]
        if rec.get("provenance"):
            lines.append("\n**Provenance**")
            for p in rec["provenance"][:6]:
                q = f' — "{p["evidence"]}"' if p.get("evidence") else ""
                lines.append(f"- {p['locator']}{q}")
        return "\n".join(lines)
    except Exception:
        return "ERROR:\n" + traceback.format_exc(limit=3)


@mcp.tool(
    description="Walk the DSO taxonomy around an entity: broader (parents) and/or narrower "
                "(children) concepts along *Broader* / refines relations. direction: "
                "'broader', 'narrower', or 'both'.",
)
def dso_taxonomy(name: str, direction: str = "both", depth: int = 3) -> str:
    try:
        tax = kg().taxonomy(name, direction=direction, max_depth=depth)
        if not tax:
            return f"No entity found for '{name}'."

        def render(nodes: list, indent: int = 0) -> list:
            out = []
            for n in nodes:
                out.append("  " * indent + f"- {n['name']} (`{n['type']}`)")
                kids = n.get("parents", []) + n.get("children", [])
                out += render(kids, indent + 1)
            return out

        lines = [f"# Taxonomy: {tax['name']} (`{tax['type']}`)"]
        if tax.get("broader"):
            lines.append("\n**Broader (more general)**")
            lines += render(tax["broader"])
        if tax.get("narrower"):
            lines.append("\n**Narrower (more specific)**")
            lines += render(tax["narrower"])
        return "\n".join(lines)
    except Exception:
        return "ERROR:\n" + traceback.format_exc(limit=3)


@mcp.tool(
    description="List entities related to a given entity within N graph hops, with the "
                "connecting DSO predicate and direction. Good for exploration around a concept.",
)
def dso_related(name: str, depth: int = 1, limit: int = 40) -> str:
    try:
        rows = kg().neighbors(name, depth=depth, limit=limit)
        if not rows:
            return f"No neighbors found for '{name}'."
        lines = [f"# Neighborhood of '{name}' (depth≤{depth})"]
        for r in rows:
            arrow = f"—`{r['predicate']}`→" if r["direction"] == "out" else f"←`{r['predicate']}`—"
            lines.append(f"- [hop {r['hops']}] {arrow} **{r['entity']}** (`{r['type']}`)")
        return "\n".join(lines)
    except Exception:
        return "ERROR:\n" + traceback.format_exc(limit=3)


@mcp.tool(
    description="Find a connecting path between two entities in the knowledge graph "
                "(shortest, ≤4 hops), listing each predicate traversed. Useful to explain "
                "how a method relates to a problem type, metric, or claim.",
)
def dso_path(source: str, target: str) -> str:
    try:
        path = kg().path(source, target)
        if path is None:
            return (f"No path (≤4 hops) found between '{source}' and '{target}' "
                    "(or an endpoint could not be resolved).")
        k = kg()
        su = source if source.startswith("http") else k.resolve(source)
        steps = [f"**{k.name_of(su)}**"]
        for hop in path:
            arrow = f"—`{hop['predicate']}`→" if hop["direction"] == "out" else f"←`{hop['predicate']}`—"
            steps.append(f"{arrow} **{hop['to']}**")
        return "# Path: " + " ".join(steps)
    except Exception:
        return "ERROR:\n" + traceback.format_exc(limit=3)


@mcp.tool(
    description="DSO method selection: find textbook Methods applicable to a problem type "
                "(e.g. 'classification', 'regression') and/or under a data condition "
                "(e.g. 'large dataset', 'class imbalance'), including exclusion conditions. "
                "Leave filters empty to list all methods.",
)
def dso_find_methods(problem_type: str = "", data_condition: str = "") -> str:
    try:
        rows = kg().find_methods(problem=problem_type or None, condition=data_condition or None)
        if not rows:
            return "No methods match the given filters."
        lines = [f"# Methods ({len(rows)})" +
                 (f" for problem '{problem_type}'" if problem_type else "") +
                 (f" under condition '{data_condition}'" if data_condition else "")]
        for m in rows[:40]:
            head = f"## {m['method']}"
            if m.get("paradigm"):
                head += f" [{m['paradigm']}]"
            lines.append(head)
            if m.get("applicableToProblemTypes"):
                lines.append(f"- applicable to: {', '.join(m['applicableToProblemTypes'])}")
            if m.get("applicableUnder"):
                lines.append(f"- applicable under: {', '.join(m['applicableUnder'])}")
            if m.get("excludedUnder"):
                lines.append(f"- excluded under: {', '.join(m['excludedUnder'])}")
            if m.get("purpose"):
                lines.append(f"- purpose: {m['purpose']}")
            if m.get("description"):
                lines.append(f"- {m['description']}")
        return "\n".join(lines)
    except Exception:
        return "ERROR:\n" + traceback.format_exc(limit=3)


@mcp.tool(
    description="Get DSO Reference Knowledge claims (ReferenceClaim / Guideline / Constraint) — "
                "optionally about a specific entity — each with claim statement, kind, "
                "severity/status, what it concerns, and source locator + evidence quote. "
                "kind filter examples: 'guideline', 'constraint', 'heuristic', 'best_practice'.",
)
def dso_get_claims(about: str = "", kind: str = "", limit: int = 20) -> str:
    try:
        rows = kg().claims(about=about or None, kind=kind or None, limit=limit)
        if not rows:
            return "No claims match." + (f" (about '{about}')" if about else "")
        lines = [f"# Claims ({len(rows)})" + (f" about '{about}'" if about else "")]
        for c in rows:
            tag = c["type"]
            if c.get("guidelineType"):
                tag += f"/{c['guidelineType']}"
            if c.get("constraintSeverity"):
                tag += f"/{c['constraintSeverity']}"
            lines.append(f"## {c['name']} [{tag}]")
            if c.get("claimStatement"):
                lines.append(f"- statement: {c['claimStatement']}")
            if c.get("concerns"):
                lines.append("- concerns: " + ", ".join(
                    f"{x['entity']} ({x['type']}, {x['predicate']})" for x in c["concerns"][:6]))
            for s in (c.get("source") or [])[:2]:
                q = f' — "{s["quote"]}"' if s.get("quote") else ""
                lines.append(f"- source: {s['locator']}{q}")
        return "\n".join(lines)
    except Exception:
        return "ERROR:\n" + traceback.format_exc(limit=3)


@mcp.tool(
    description="Get the textbook's process knowledge: SolutionTemplates, their Stages, "
                "WorkflowPatterns, entry/exit ActivityPatterns — optionally filtered by "
                "problem type. Answers 'how do you do X' questions with the DSO workflow model.",
)
def dso_get_workflow(problem_type: str = "") -> str:
    try:
        rows = kg().workflows(problem=problem_type or None)
        if not rows:
            return "No workflow/solution-template knowledge found" + \
                   (f" for '{problem_type}'." if problem_type else ".")
        lines = [f"# Process knowledge ({len(rows)} templates/patterns)"]
        for w in rows:
            lines.append(f"## [{w['kind']}] {w['name']}")
            if w.get("applicableTo"):
                lines.append(f"- applicable to: {', '.join(w['applicableTo'])}")
            if w.get("description"):
                lines.append(f"- {w['description']}")
            for p in w.get("parts", []):
                lines.append(f"- `{p['predicate']}` → {p['entity']} (`{p['type']}`)")
                for sub in p.get("contains", []):
                    lines.append(f"    - `{sub['predicate']}` → {sub['entity']} (`{sub['type']}`)")
        return "\n".join(lines)
    except Exception:
        return "ERROR:\n" + traceback.format_exc(limit=3)


@mcp.tool(
    description="List Metrics and EvaluationProtocols from the textbook, optionally those used "
                "to evaluate a given problem type (e.g. 'classification' → precision/recall/F1/"
                "ROC-AUC, cross-validation). Includes metric properties (direction, formula).",
)
def dso_get_metrics(problem_type: str = "") -> str:
    try:
        rows = kg().metrics(problem=problem_type or None)
        if not rows:
            return "No metrics found."
        lines = [f"# Metrics & evaluation ({len(rows)})" +
                 (f" for '{problem_type}'" if problem_type else "")]
        for m in rows[:30]:
            lines.append(f"## {m['name']} [{m['kind']}]")
            if m.get("usedByProblemTypes"):
                lines.append(f"- evaluates: {', '.join(m['usedByProblemTypes'])}")
            props = m.get("props") or {}
            for k in ("metricFormula", "metricDirection", "optimalValue", "sensitivityType",
                      "evaluationFolds", "evaluationRepetitions"):
                if props.get(k):
                    lines.append(f"- {k}: {props[k]}")
            if m.get("description"):
                lines.append(f"- {m['description']}")
        return "\n".join(lines)
    except Exception:
        return "ERROR:\n" + traceback.format_exc(limit=3)


@mcp.tool(
    description="Provenance for any KG entity: which textbook fragments (chapter/section, PDF "
                "pages, locator) mention it, the verbatim evidence quote, and a surrounding "
                "text snippet. Use to verify/ground any answer.",
)
def dso_provenance(name: str) -> str:
    try:
        rows = kg().provenance(name)
        if not rows:
            return f"No provenance found for '{name}'."
        lines = [f"# Provenance: {name} ({len(rows)} fragment(s))"]
        for r in rows:
            lines.append(f"## {r['section']} — {r['locator']}")
            if r.get("evidenceQuote"):
                lines.append(f'evidence: "{r["evidenceQuote"]}"')
            lines.append(f"snippet: …{r['snippet']}…")
        return "\n".join(lines)
    except Exception:
        return "ERROR:\n" + traceback.format_exc(limit=3)


@mcp.tool(
    description="GraphRAG one-shot: give a natural-language data-science question, get compiled "
                "KG context — linked entities with their properties/taxonomy/applicability, "
                "relevant claims, and cited source excerpts — ready to answer from. This is the "
                "recommended entry point for QA.",
)
def dso_graphrag_context(question: str) -> str:
    try:
        ctx = kg().graphrag_context(question)
        if not ctx["linkedEntities"]:
            return ("No KG entities linked to this question. The graph covers all 16 chapters "
                    "of Hands-On ML (1st ed.). Try dso_search with different keywords.")
        head = ("# GraphRAG context\nquestion: " + ctx["question"] +
                "\nlinked entities: " + ", ".join(
                    f"{e['name']} ({e['type']})" for e in ctx["linkedEntities"]) +
                ("\nsources: " + ", ".join(ctx["sources"]) if ctx.get("sources") else ""))
        return head + "\n\n" + ctx["context"]
    except Exception:
        return "ERROR:\n" + traceback.format_exc(limit=3)


@mcp.tool(
    description="Run a read-only SPARQL 1.1 query against the DSO knowledge graph. Prefixes "
                "ds:, inst:, rdfs:, rdf:, xsd: are pre-declared. Returns JSON rows. "
                "Write operations are rejected.",
)
def dso_sparql(query: str, limit: int = 100) -> str:
    try:
        res = kg().sparql_raw(query, limit=limit)
        return json.dumps(res, ensure_ascii=False, indent=1)[:20000]
    except Exception as e:
        return f"SPARQL error: {type(e).__name__}: {e}"


@mcp.tool(
    description="Find entities semantically similar to a given entity (embedding cosine over "
                "name+description). Use to discover near-synonyms, related methods/claims the "
                "keyword search misses, or to check what a concept is close to. "
                "same_type=true restricts to the same DSO type.",
)
def dso_similar(name: str, limit: int = 10, same_type: bool = False) -> str:
    try:
        rows = kg().similar(name, limit=limit, same_type=same_type)
        if not rows:
            return (f"No similar entities for '{name}' "
                    "(entity not found, or vector index unavailable).")
        lines = [f"# Entities similar to '{name}' (embedding cosine)"]
        for r in rows:
            lines.append(f"- **{r['name']}** (`{r['type']}`) cos={r['cosine']} — {r['description']}")
        return "\n".join(lines)
    except Exception:
        return "ERROR:\n" + traceback.format_exc(limit=3)


@mcp.tool(description="Knowledge graph statistics: entity counts by DSO type, relation counts, "
                      "fragment counts, vector index status. Quick health/orientation check.")
def dso_stats() -> str:
    try:
        s = kg().stats()
        lines = [f"# DSO KG stats", f"- RDF statements (IRI→IRI edges incl. types): {s['edges']}",
                 f"- entities: {s['entities_total']} | distinct relations: {s['distinct_relations']}",
                 f"- source fragments: {s['fragments']}", "- by type:"]
        lines += [f"  - {t}: {n}" for t, n in sorted(s["entities"].items(),
                                                      key=lambda x: -x[1])]
        v = s.get("vector_index")
        lines.append(f"- vector index: {v['model']} ({v['entities']} entities, dim {v['dim']})"
                     if v else "- vector index: none (FTS-only mode)")
        return "\n".join(lines)
    except Exception:
        return "ERROR:\n" + traceback.format_exc(limit=3)


@mcp.resource(
    "dso://ontology/summary",
    name="DSO textbook profile (ontology summary)",
    description="The DSO v0.2 Reference-Knowledge profile used for extraction: object types, "
                "properties, relation types with cardinalities, and controlled vocabularies.",
    mime_type="text/markdown",
)
def ontology_summary() -> str:
    p = ROOT / "schema" / "vocab_for_prompt.md"
    return p.read_text(encoding="utf-8") if p.exists() else "(vocab file missing)"


if __name__ == "__main__":
    mcp.run("stdio")
