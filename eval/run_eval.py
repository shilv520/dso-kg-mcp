#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""run_eval.py — 教材问答评测：裸 LLM vs DSO-KG 增强（GraphRAG），LLM 盲评打分。

用法:
  .venv/Scripts/python.exe eval/run_eval.py [--limit 40] [--workers 3]

流程:
  1) 对每个问题生成两臂答案：
     raw — qwen3.8-max 直接回答（无外部知识源）
     kg  — DsoKG.graphrag_context 编译上下文后回答（要求引用 locator）
  2) 盲评：随机交换两臂顺序，评委按 expected_points 给 0-5 分
     （覆盖度+正确性），输出缺失要点。
  3) 结果写 eval/results.jsonl + 汇总打印；LLM 调用有 sqlite 缓存可重跑。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

from llm import QwenProvider, parse_json  # noqa: E402

QUESTIONS = ROOT / "eval" / "questions.jsonl"
RESULTS = ROOT / "eval" / "results.jsonl"
CACHE = ROOT / "eval" / "cache.sqlite"

RAW_SYSTEM = ("You are a knowledgeable data-science tutor. Answer the question "
              "concisely and accurately in English, from your own knowledge.")
KG_SYSTEM = ("You are a data-science tutor with access to a curated knowledge graph of the "
             "textbook 'Hands-On Machine Learning' (Géron, 1st ed.). Use the KG context to "
             "ground your answer and cite source locators like \"Ch.7 'Boosting', "
             "PDF pp.215-222\" for what you take from it. The context is evidence, NOT a "
             "coverage limit: answer the question completely and concisely, including "
             "standard details you know (algorithms, hyperparameters, equations) even when "
             "the context omits them — just never attach a textbook citation to unsourced "
             "supplements. Stay focused on what was asked; do not dump unrelated context "
             "material.")
JUDGE_SYSTEM = ("You are a strict grader. Given a question, a list of expected points, and two "
                "candidate answers, score each answer 0-5: 5 = covers essentially all expected "
                "points correctly; 3 = covers the main points, minor gaps; 1 = mostly wrong or "
                "misses the point; 0 = no useful content. Penalize factual errors. "
                "Output STRICT JSON: {\"a_score\": int, \"b_score\": int, "
                "\"a_missing\": [str], \"b_missing\": [str], \"notes\": str}")

_cache_lock = threading.Lock()
_con = sqlite3.connect(str(CACHE), check_same_thread=False)
_con.execute("CREATE TABLE IF NOT EXISTS eval_cache(key TEXT PRIMARY KEY, response TEXT)")


def cached_call(llm: QwenProvider, tag: str, system: str, prompt: str, **kw) -> str:
    key = hashlib.sha256(f"v1|{tag}|{system}|{prompt}".encode("utf-8")).hexdigest()
    with _cache_lock:
        row = _con.execute("SELECT response FROM eval_cache WHERE key=?", (key,)).fetchone()
    if row:
        return row[0]
    text = llm.generate_structured_raw(prompt, system=system, temperature=0.0,
                                       max_tokens=kw.get("max_tokens", 4000))
    with _cache_lock:
        _con.execute("INSERT OR REPLACE INTO eval_cache VALUES(?,?)", (key, text))
        _con.commit()
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    qs = [json.loads(l) for l in open(QUESTIONS, encoding="utf-8")]
    if args.limit:
        qs = qs[:args.limit]
    print(f"questions: {len(qs)}")

    from query.dso_kg import DsoKG
    kg_obj = DsoKG(ROOT / "data")
    llm = QwenProvider()

    def run_one(q: dict) -> dict:
        question = q["question"]
        # --- arms ---
        raw_ans = cached_call(llm, "raw", RAW_SYSTEM, question, max_tokens=2500)
        ctx = kg_obj.graphrag_context(question, max_chars=14000)
        kg_prompt = (f"Question: {question}\n\n"
                     f"=== KG CONTEXT ===\n{ctx['context']}\n=== END CONTEXT ===\n\n"
                     f"Linked entities: "
                     f"{', '.join(e['name'] for e in ctx['linkedEntities']) or '(none)'}\n"
                     f"Answer the question using the context above.")
        kg_ans = cached_call(llm, "kg3", KG_SYSTEM, kg_prompt, max_tokens=2500)
        # --- blind judge ---
        rng = random.Random(hashlib.md5(question.encode()).digest()[0])
        swap = rng.random() < 0.5
        ans_a, ans_b = (kg_ans, raw_ans) if swap else (raw_ans, kg_ans)
        judge_prompt = (
            f"Question: {question}\n\n"
            f"Expected points:\n" + "\n".join(f"- {p}" for p in q["expected_points"]) +
            f"\n\nAnswer A:\n{ans_a}\n\nAnswer B:\n{ans_b}\n\nScore both answers.")
        jtxt = cached_call(llm, "judge2", JUDGE_SYSTEM, judge_prompt, max_tokens=1500)
        try:
            j = parse_json(jtxt)
            a_s, b_s = int(j["a_score"]), int(j["b_score"])
        except Exception as e:  # noqa: BLE001
            a_s, b_s = -1, -1
            j = {"error": f"{type(e).__name__}: {e}", "raw": jtxt[:300]}
        kg_score, raw_score = (a_s, b_s) if swap else (b_s, a_s)
        kg_missing, raw_missing = (j.get("a_missing", []), j.get("b_missing", [])) if swap \
            else (j.get("b_missing", []), j.get("a_missing", []))
        return {"id": q["id"], "chapter": q["chapter"], "question": question,
                "linked": [e["name"] for e in ctx["linkedEntities"]],
                "n_linked": len(ctx["linkedEntities"]),
                "raw_score": raw_score, "kg_score": kg_score,
                "raw_missing": raw_missing, "kg_missing": kg_missing,
                "judge_notes": j.get("notes", ""),
                "raw_answer": raw_ans, "kg_answer": kg_ans}

    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, q): q for q in qs}
        for fut in as_completed(futs):
            q = futs[fut]
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001
                print(f"!! {q['id']} failed: {type(e).__name__}: {e}")
                results.append({"id": q["id"], "error": str(e)})
            done += 1
            print(f"[{done}/{len(qs)}] {q['id']}")

    results.sort(key=lambda r: r["id"])
    with open(RESULTS, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    ok = [r for r in results if "kg_score" in r and r["kg_score"] >= 0]
    if ok:
        raw_avg = sum(r["raw_score"] for r in ok) / len(ok)
        kg_avg = sum(r["kg_score"] for r in ok) / len(ok)
        win = sum(1 for r in ok if r["kg_score"] > r["raw_score"])
        lose = sum(1 for r in ok if r["kg_score"] < r["raw_score"])
        tie = len(ok) - win - lose
        print("\n===== SUMMARY =====")
        print(f"graded: {len(ok)}/{len(results)}")
        print(f"raw LLM avg score : {raw_avg:.2f} / 5")
        print(f"KG-augmented avg  : {kg_avg:.2f} / 5")
        print(f"KG wins/loses/ties: {win}/{lose}/{tie}")
        by_ch = {}
        for r in ok:
            by_ch.setdefault(r["chapter"], []).append(r)
        for ch, rs in sorted(by_ch.items()):
            print(f"  ch{ch:02d}: raw={sum(x['raw_score'] for x in rs)/len(rs):.2f} "
                  f"kg={sum(x['kg_score'] for x in rs)/len(rs):.2f} (n={len(rs)})")
        nolink = [r["id"] for r in ok if r["n_linked"] == 0]
        if nolink:
            print("questions with 0 linked entities:", nolink)


if __name__ == "__main__":
    main()
