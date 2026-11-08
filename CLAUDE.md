# CLAUDE.md

本项目：教材 → DSO 本体知识图谱 → MCP 查询服务。

- Harness 使用指南（MCP 工具目录、推荐问答流程、边界）见 **AGENTS.md**。
- 项目架构、复现命令、建模决策记录见 **README.md**。
- MCP 服务器已由 `.mcp.json` 注册（server 名 `dso-kg`，需重启会话加载）。
- `DSO_extracted/`（上级目录）中老师的 YAML 是只读事实源，不得修改；
  schema 一律用 `adapter/omm2schema.py` 重新编译生成。
- 数据管线可安全重跑：`slice_book.py → extract.py → build_kg.py`（LLM 调用有 sqlite 缓存，
  断点续跑；extract.py 默认全书 16 章，`--chapters` 可限定）。
- LLM 端点由项目根 `.env` 配置（pipeline/llm.py 优先读它，其次系统环境变量；当前
  DeepSeek deepseek-chat，.env 内附回退 qwen 注释）。历史抽取/评测结果由 qwen3.8-max
  生成；sqlite 缓存键不含模型名，换模型后同 prompt 重跑会命中旧缓存，真重跑需先清
  `data/cache.sqlite` / `eval/cache.sqlite`。MCP 查询服务不调 LLM，不受端点切换影响。
- build_kg.py 含嵌入判重（fastembed bge-small-en-v1.5 候选 + LLM 裁决合并，`--no-dedup` 可关）
  与实体向量入库（kb.sqlite `entity_embeddings` 表）；查询层 search 为 BM25+向量 RRF 混合，
  向量索引缺失时自动退化纯 FTS。嵌入模型缓存在 `data/fastembed_cache/`（勿删，离线依赖）。
- onnxruntime 必须 1.20.1（1.31.1 在本机 segfault，1.17.x 与 numpy2 ABI 冲突）。
