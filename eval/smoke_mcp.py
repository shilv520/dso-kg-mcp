#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""smoke_mcp.py — 以真实 MCP stdio 子进程方式冒烟测试 dso_mcp 服务器。

模拟 Codex/Claude Code 的连接方式：启动服务器进程 → initialize → list_tools
→ 逐个调用代表性 tool → 读 resource。
"""
from __future__ import annotations

import anyio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
SERVER = str(ROOT / "mcp_server" / "dso_mcp.py")

CALLS = [
    ("dso_stats", {}),
    ("dso_search", {"query": "bagging", "limit": 5}),
    ("dso_get_concept", {"name": "Random Forests"}),
    ("dso_find_methods", {"problem_type": "classification"}),
    ("dso_get_claims", {"kind": "guideline", "limit": 3}),
    ("dso_get_workflow", {}),
    ("dso_get_metrics", {"problem_type": "classification"}),
    ("dso_provenance", {"name": "AdaBoost"}),
    ("dso_graphrag_context", {"question": "Why is accuracy bad for imbalanced classification?"}),
    ("dso_sparql", {"query": "SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE { ?s rdf:type ds:Method }"}),
    ("dso_sparql", {"query": "SELECT ?c ?label WHERE { ?c a ds:DomainConcept "
                             "OPTIONAL { ?c rdfs:label ?label } } ORDER BY ?label LIMIT 10"}),
    ("dso_taxonomy", {"name": "Ensemble Learning"}),
    ("dso_related", {"name": "AdaBoost", "depth": 1, "limit": 10}),
    ("dso_path", {"source": "AdaBoost", "target": "Classification"}),
    ("dso_similar", {"name": "AdaBoost", "limit": 5}),
    ("dso_graphrag_context", {"question": "How does dropout regularize a deep neural network?"}),
]


def text_of(result) -> str:
    parts = []
    for c in getattr(result, "content", []) or []:
        t = getattr(c, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts)


async def main():
    params = StdioServerParameters(command=PY, args=[SERVER])
    fails = 0
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"server: {init.server_info.name} v{init.server_info.version}")
            tools = await session.list_tools()
            print(f"tools ({len(tools.tools)}): " + ", ".join(t.name for t in tools.tools))
            for name, args in CALLS:
                try:
                    res = await session.call_tool(name, arguments=args)
                    txt = text_of(res)
                    is_err = getattr(res, "is_error", None)
                    if is_err is None:
                        is_err = getattr(res, "isError", False)
                    # dso_mcp 把工具异常吞成 "ERROR…"/"SPARQL error…" 文本（is_error=False），
                    # 必须按前缀判失败，否则冒烟测试对这类 bug 全盲
                    bad_text = txt.startswith(("ERROR", "SPARQL error"))
                    status = "ERR" if is_err or not txt or bad_text else "OK"
                    if status != "OK":
                        fails += 1
                    first = txt.split("\n")[0][:100]
                    print(f"[{status}] {name}({args}) -> {len(txt)} chars | {first}")
                except Exception as e:  # noqa: BLE001
                    fails += 1
                    print(f"[EXC] {name}: {type(e).__name__}: {e}")
            try:
                r = await session.read_resource("dso://ontology/summary")
                n = len(list(r.contents))
                print(f"[OK] resource dso://ontology/summary ({n} part(s))")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"[EXC] resource read: {type(e).__name__}: {e}")
    print(f"\nsmoke done, failures: {fails}/{len(CALLS) + 1}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    anyio.run(main)
