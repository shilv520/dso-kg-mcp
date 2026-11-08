#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""slice_book.py — 按目录把教材切成 SourceFragment 候选片段（章 → 节 → 过长节再分段）。

输出 data/fragments.jsonl，每行一个 fragment：
  {fragment_id, level, parent_id, chapter_no, chapter_title, section_title,
   locator, page_start, page_end, n_words, text}
页码为 PDF 物理页（1-based，与 pymupdf TOC 一致）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent           # 仓库根目录
# 教材 PDF 约定放在仓库同级目录（版权原因不随仓库分发）；可用环境变量 DSO_BOOK_PDF 覆盖
PDF_PATH = Path(os.environ.get(
    "DSO_BOOK_PDF",
    ROOT.parent / "Hands On Machine Learning with Scikit Learn and TensorFlow.pdf"))
TOC_PATH = ROOT / "data" / "toc.json"
OUT_PATH = ROOT / "data" / "fragments.jsonl"

# Full book (1st edition, 2017): chapters 1-16 (appendices excluded)
CHAPTERS = list(range(1, 17))
MAX_SECTION_WORDS = 2600      # sections longer than this get split
SUBFRAGMENT_WORDS = 1800      # target size for sub-fragments


def clean_page_text(text: str, chapter_title: str) -> str:
    """Drop running headers/footers and normalize whitespace."""
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            lines.append("")
            continue
        if re.fullmatch(r"\d{1,3}", s):          # page numbers
            continue
        if s == chapter_title:                     # running header
            continue
        if re.fullmatch(r"Chapter \d+: .{0,80}", s) and len(s) < 90:
            continue                               # running header variant
        if re.fullmatch(r"(CHAPTER|Chapter) \d+", s):
            continue                               # big typographic chapter opener
        lines.append(ln.rstrip())
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "sec"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    toc = json.load(open(TOC_PATH, encoding="utf-8"))
    doc = pymupdf.open(str(PDF_PATH))

    # locate chapter entries (level 2, "Chapter N. Title") inside Part I/II
    chap_entries = []  # (no, title, start_page)
    for lvl, title, page in toc:
        m = re.match(r"^Chapter (\d+)\.\s+(.*)$", title.strip())
        if lvl == 2 and m and "Exercise Solutions" not in title:
            chap_entries.append((int(m.group(1)), m.group(2).strip(), page))
    chap_entries.sort(key=lambda x: x[2])
    # book end = first Appendix (level-1) page, so the last chapter does not
    # swallow exercise solutions / appendices
    book_end = doc.page_count + 1
    for lvl, title, page in toc:
        if lvl == 1 and str(title).strip().startswith("Appendix"):
            book_end = page
            break
    # chapter end = next chapter entry start (physical order)
    ranges = {}
    for i, (no, title, start) in enumerate(chap_entries):
        end = chap_entries[i + 1][2] if i + 1 < len(chap_entries) else book_end
        ranges[no] = (title, start, end)

    fragments = []
    for no in CHAPTERS:
        if no not in ranges:
            print(f"!! chapter {no} not found in TOC")
            continue
        title, c_start, c_end = ranges[no]
        # sections = level-3 entries inside chapter range
        secs = [(t.strip(), p) for lvl, t, p in toc if lvl == 3 and c_start <= p < c_end]
        if not secs:
            secs = [(title, c_start)]
        sec_bounds = []
        for i, (st, sp) in enumerate(secs):
            ep = secs[i + 1][1] if i + 1 < len(secs) else c_end
            sec_bounds.append((st, sp, ep))

        for si, (st, sp, ep) in enumerate(sec_bounds):
            pages_text = []
            for p in range(sp - 1, min(ep - 1, doc.page_count)):
                pages_text.append(clean_page_text(doc[p].get_text("text"), title))
            text = "\n\n".join(t for t in pages_text if t)
            n_words = len(text.split())
            if n_words < 25:
                continue
            base_id = f"holml1-ch{no:02d}-s{si:02d}-{slugify(st)}"
            locator = f"Ch.{no} '{st}', PDF pp.{sp}-{ep - 1}"
            if n_words <= MAX_SECTION_WORDS:
                fragments.append(dict(
                    fragment_id=base_id, level="section", parent_id=None, unit=True,
                    chapter_no=no, chapter_title=title, section_title=st,
                    locator=locator, page_start=sp, page_end=ep - 1,
                    n_words=n_words, text=text))
            else:
                # section-level parent fragment: provenance holder only, NOT an
                # extraction unit (its sub-fragments carry the text for extraction)
                fragments.append(dict(
                    fragment_id=base_id, level="section", parent_id=None, unit=False,
                    chapter_no=no, chapter_title=title, section_title=st,
                    locator=locator, page_start=sp, page_end=ep - 1,
                    n_words=n_words, text=text))
                # split into sub-fragments at paragraph boundaries
                paras = text.split("\n\n")
                buf, k = [], 0
                for para in paras:
                    buf.append(para)
                    if len(" ".join(buf).split()) >= SUBFRAGMENT_WORDS:
                        k += 1
                        sub = "\n\n".join(buf)
                        fragments.append(dict(
                            fragment_id=f"{base_id}-p{k:02d}", level="sub", unit=True,
                            parent_id=base_id, chapter_no=no, chapter_title=title,
                            section_title=f"{st} (part {k})", locator=f"{locator} [part {k}]",
                            page_start=sp, page_end=ep - 1,
                            n_words=len(sub.split()), text=sub))
                        buf = []
                if buf:
                    sub = "\n\n".join(buf)
                    if len(sub.split()) > 60:
                        k += 1
                        fragments.append(dict(
                            fragment_id=f"{base_id}-p{k:02d}", level="sub", unit=True,
                            parent_id=base_id, chapter_no=no, chapter_title=title,
                            section_title=f"{st} (part {k})", locator=f"{locator} [part {k}]",
                            page_start=sp, page_end=ep - 1,
                            n_words=len(sub.split()), text=sub))
                    elif k == 0:
                        # single-paragraph giant section: extract at section level
                        fragments[-1]["unit"] = True
    # extraction units = sub-fragments where present, else sections
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for fr in fragments:
            f.write(json.dumps(fr, ensure_ascii=False) + "\n")

    by_ch = {}
    for fr in fragments:
        by_ch.setdefault(fr["chapter_no"], []).append(fr)
    print(f"fragments written: {len(fragments)} -> {OUT_PATH}")
    for ch, frs in sorted(by_ch.items()):
        units = [x for x in frs if x.get("unit")]
        words = sum(x["n_words"] for x in units)
        print(f"  ch{ch:02d}: sections={sum(1 for x in frs if x['level']=='section')}, "
              f"subs={sum(1 for x in frs if x['level']=='sub')}, "
              f"extraction_units={len(units)}, words={words}")


if __name__ == "__main__":
    main()
