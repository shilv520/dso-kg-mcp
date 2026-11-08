#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""embeddings.py — 本地文本嵌入（fastembed / ONNX，无 torch、离线推理）。

build_kg.py 用它在建图时：
  ① 同类型实体做近重复候选对（余弦阈值）→ 交 LLM 裁决合并；
  ② 把最终实体向量写入 kb.sqlite（entity_embeddings 表）。
query/dso_kg.py 用它在查询时把问题嵌入，与 FTS5 bm25 做 RRF 混合排序。

模型：BAAI/bge-small-en-v1.5（384 维，~130MB，首次运行自动下载并缓存到
~/.cache/fastembed）。国内网络经 hf-mirror 下载（HF_ENDPOINT 默认镜像，
可显式覆盖）。
"""
from __future__ import annotations

import os

# 必须在 import fastembed / huggingface_hub 之前设置镜像
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBED_VERSION = "bge-small-en-v1.5"   # 写入 sqlite 的模型标识（换模型需重建）

# 项目内固定缓存目录（默认的 Temp 会被系统清理；随项目走、离线可复现）
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "fastembed_cache")

_model = None


def get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(MODEL_NAME, cache_dir=_CACHE_DIR)
    return _model


def embed(texts, batch_size: int = 64):
    """texts: list[str] -> numpy float32 数组 (n, dim)，已 L2 归一化（点积=余弦）。"""
    import numpy as np
    texts = [t if t and t.strip() else " " for t in texts]
    vecs = list(get_model().embed(texts, batch_size=batch_size))
    m = np.asarray(vecs, dtype="float32")
    m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)
    return m


def entity_embed_text(rec: dict) -> str:
    """实体记录（build_kg entities dict 或 sqlite 行）→ 嵌入文本。"""
    parts = [rec.get("displayName") or rec.get("display_name") or ""]
    alts = rec.get("alternateName") or []
    if isinstance(alts, str):
        import json as _json
        try:
            alts = _json.loads(alts)
        except Exception:
            alts = [alts]
    parts.extend(a for a in alts if a)
    desc = (rec.get("description") or "")[:400]
    if desc:
        parts.append(desc)
    return " | ".join(parts)
