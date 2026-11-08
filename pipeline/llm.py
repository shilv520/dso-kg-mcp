#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""llm.py — 项目 LLM 调用封装（端点/模型由项目根 .env 配置，与全局环境变量解耦）。

配置优先级：项目根 .env > 系统环境变量 ANTHROPIC_* > 内置默认值。
当前默认端点：DeepSeek Anthropic 兼容端点（https://api.deepseek.com/anthropic，
deepseek-chat）。本项目历史全书抽取/修复/评测结果由 qwen3.8-max（阿里云 MaaS
端点）生成；因该端点流式请求频繁超时，现切换 DeepSeek。回退 qwen 只需改 .env
（见 .env 内注释），阿里云端点会自动附带 thinking:disabled 提速，DeepSeek 不发。

基于 semantica 的 AnthropicProvider 子类化：
semantica 0.7.0 原实现用 response.content[0].text 解析，遇到 qwen3.8-max
返回的 thinking 块会崩；这里覆写 generate / generate_structured，做健壮的
文本块拼接 + JSON 解析修复 + 退避重试。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from semantica.semantic_extract.providers import AnthropicProvider

_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path = _ROOT / ".env") -> Dict[str, str]:
    """Minimal KEY=VALUE parser (no third-party dep); ignores comments/blank lines."""
    vals: Dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return vals


_DOTENV = _load_dotenv()


def _cfg(name: str, default: Optional[str] = None) -> Optional[str]:
    return _DOTENV.get(name) or os.environ.get(name) or default


BASE_URL = _cfg("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
API_KEY = _cfg("ANTHROPIC_API_KEY")
MODEL = _cfg("ANTHROPIC_MODEL", "deepseek-chat")
_IS_ALIYUN = "aliyuncs" in (BASE_URL or "")
# 阿里云 qwen 端点需显式 thinking:disabled（响应快 10-30x）；DeepSeek 无此参数不发
DISABLE_THINKING = _cfg("DSO_LLM_DISABLE_THINKING", "1" if _IS_ALIYUN else "0") == "1"
# DeepSeek 输出上限 8K：非阿里云端点默认把 max_tokens 截到 8000（0 = 不截）
MAX_TOKENS_CAP = int(_cfg("DSO_LLM_MAX_TOKENS_CAP", "0" if _IS_ALIYUN else "8000"))


class LLMProvider(AnthropicProvider):
    """AnthropicProvider patched for reasoning models + project-scoped endpoint."""

    def __init__(self, model: str = MODEL, **kwargs):
        super().__init__(api_key=kwargs.pop("api_key", None) or API_KEY,
                         model=model, **kwargs)

    def _init_client(self) -> None:
        """Build the client against the project endpoint (explicit base_url,
        so a global ANTHROPIC_BASE_URL env can never hijack project runs)."""
        try:
            from anthropic import Anthropic

            if self.api_key:
                self.client = Anthropic(api_key=self.api_key, base_url=BASE_URL)
        except (ImportError, OSError):
            self.client = None

    def _create(self, system: Optional[str], user: str, **kwargs) -> str:
        last_err = None
        for attempt in range(5):
            try:
                max_tokens = kwargs.get("max_tokens", 16000)
                if MAX_TOKENS_CAP:
                    max_tokens = min(max_tokens, MAX_TOKENS_CAP)
                create_kwargs: Dict[str, Any] = {
                    "model": kwargs.get("model", self.model),
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": user}],
                }
                if system:
                    create_kwargs["system"] = system
                # only send the thinking switch on endpoints that honor it (aliyun qwen)
                if DISABLE_THINKING:
                    create_kwargs["thinking"] = kwargs.get("thinking", {"type": "disabled"})
                elif "thinking" in kwargs:
                    create_kwargs["thinking"] = kwargs["thinking"]
                # anthropic SDK 2.x dropped temperature/top_p/top_k as named params;
                # pass them through extra_body (proxy merges them into the request).
                extra_body = {p: kwargs[p] for p in ("temperature", "top_p", "top_k") if p in kwargs}
                if extra_body:
                    create_kwargs["extra_body"] = extra_body
                for p in ("stop_sequences", "metadata"):
                    if p in kwargs:
                        create_kwargs[p] = kwargs[p]
                resp = self.client.messages.create(**create_kwargs)
                # join only text blocks; skip thinking blocks (reasoning models)
                parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
                text = "".join(parts).strip()
                if not text:
                    raise RuntimeError(f"empty text response, stop_reason={resp.stop_reason}")
                if resp.stop_reason == "max_tokens":
                    raise RuntimeError("hit max_tokens; response truncated")
                return text
            except Exception as e:  # noqa: BLE001
                # programming/parse errors will never succeed on retry
                if isinstance(e, (TypeError, ValueError, KeyError)):
                    raise
                last_err = e
                wait = min(2 ** attempt * 3, 60)
                time.sleep(wait)
        raise RuntimeError(f"LLM call failed after retries: {last_err}")

    def generate(self, prompt: str, **kwargs) -> str:
        return self._create(kwargs.pop("system", None), prompt, **kwargs)

    def generate_structured(self, prompt: str, **kwargs) -> Any:
        text = self._create(kwargs.pop("system", None), prompt, **kwargs)
        return parse_json(text)

    def generate_structured_raw(self, prompt: str, **kwargs) -> str:
        """Return the raw response text (caller parses; used for response caching)."""
        return self._create(kwargs.pop("system", None), prompt, **kwargs)


# 兼容旧导入名（extract.py / build_kg.py / eval/run_eval.py 均 import QwenProvider）
QwenProvider = LLMProvider


def parse_json(text: str) -> Any:
    """Robust JSON extraction: strip fences, then outermost braces."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end > start:
        return json.loads(t[start:end + 1])
    start = t.find("[")
    end = t.rfind("]")
    if start != -1 and end > start:
        return json.loads(t[start:end + 1])
    raise ValueError(f"no JSON found in response: {text[:200]}...")


if __name__ == "__main__":
    key = API_KEY or ""
    masked = key[:3] + "***" + key[-4:] if len(key) > 8 else "(none)"
    print(f"endpoint : {BASE_URL}")
    print(f"model    : {MODEL}")
    print(f"api key  : {masked}")
    print(f"thinking : {'disabled (sent)' if DISABLE_THINKING else 'not sent'}")
    print(f"max_tokens cap: {MAX_TOKENS_CAP or '(none)'}")
    llm = LLMProvider()
    print("available:", llm.is_available())
    t0 = time.time()
    out = llm.generate_structured(
        "Return a JSON object with keys: ok (bool), model (string, the model you are).",
        temperature=0.0, max_tokens=4000)
    print(f"smoke test ({time.time() - t0:.1f}s):", out)
