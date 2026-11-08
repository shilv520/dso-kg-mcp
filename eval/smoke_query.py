#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""smoke_query.py — 直接调用 MCP tool 函数冒烟测试（不经 stdio 协议层）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from mcp_server import dso_mcp as m  # noqa: E402

CASES = [
    ("dso_stats", lambda: m.dso_stats()),
    ("dso_search(bagging)", lambda: m.dso_search("bagging")),
    ("dso_search(precision, Metric)", lambda: m.dso_search("precision", type="Metric")),
    ("dso_get_concept(Random Forests)", lambda: m.dso_get_concept("Random Forests")),
    ("dso_taxonomy(Ensemble Learning)", lambda: m.dso_taxonomy("Ensemble Learning")),
    ("dso_related(AdaBoost)", lambda: m.dso_related("AdaBoost")),
    ("dso_path(AdaBoost -> classification)", lambda: m.dso_path("AdaBoost", "classification")),
    ("dso_find_methods(classification)", lambda: m.dso_find_methods(problem_type="classification")),
    ("dso_get_claims(guideline)", lambda: m.dso_get_claims(kind="guideline", limit=5)),
    ("dso_get_claims(about=bagging)", lambda: m.dso_get_claims(about="bagging", limit=5)),
    ("dso_get_workflow()", lambda: m.dso_get_workflow()),
    ("dso_get_metrics(classification)", lambda: m.dso_get_metrics(problem_type="classification")),
    ("dso_provenance(Bagging)", lambda: m.dso_provenance("Bagging")),
    ("dso_graphrag_context(imbalanced accuracy)", lambda: m.dso_graphrag_context(
        "Why is accuracy a bad measure for imbalanced classification?")),
    ("dso_sparql(Method count)", lambda: m.dso_sparql(
        "SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE { ?s rdf:type ds:Method }")),
    ("dso_similar(AdaBoost)", lambda: m.dso_similar("AdaBoost", limit=5)),
    ("dso_search(paraphrase: fight overfitting)", lambda: m.dso_search(
        "ways to fight overfitting in deep neural networks")),
    ("dso_get_concept(Support Vector Machine)", lambda: m.dso_get_concept("Support Vector Machine")),
]


def main():
    fails = 0
    for name, fn in CASES:
        try:
            out = fn()
            ok = out and not out.startswith("ERROR") and "No " not in out.split("\n")[0][:40]
            head = out.split("\n")
            print(f"\n{'='*70}\n## {name}  [{'OK' if ok else 'CHECK'}] ({len(out)} chars)")
            print("\n".join(head[:14]))
            if len(head) > 14:
                print(f"... (+{len(head)-14} lines)")
            if not ok:
                fails += 1
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"\n{'='*70}\n## {name}  [EXC] {type(e).__name__}: {e}")
            traceback.print_exc(limit=3)
            fails += 1
    print(f"\n{'='*70}\nsmoke done, suspicious/failed: {fails}/{len(CASES)}")


if __name__ == "__main__":
    main()
