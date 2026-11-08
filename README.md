# dso-kg-mcp — 数据科学教材 → DSO 知识图谱 → MCP 服务

把《Hands-On Machine Learning with Scikit-Learn and TensorFlow》（Géron，第 1 版，
**全书 16 章**）按 **DSO v0.2 本体**（老师提供的 `DSO_v0.2_Ontology_Model.yaml`，
OMM v0.1 清单格式）抽取为 RDF 知识图谱，并封装成 **MCP 服务**，供 Codex / Claude Code
等 harness 做 GraphRAG 式教材问答。历史抽取与修复由 **qwen3.8-max**（阿里云 MaaS 的
Anthropic 兼容端点）生成；LLM 端点现由项目根 `.env` 配置（当前 **DeepSeek**
deepseek-chat，详见「实现要点」），图存储与工程脚手架复用 **semantica**
（semantica-agi/semantica 0.7.0）生态。
近重复实体用**本地嵌入（fastembed bge-small-en-v1.5）生成候选 + LLM 裁决**合并；
查询层为 **BM25 全文 + 嵌入向量余弦的 RRF 混合检索**。

## 与课程规范的对齐

| 规范要求（DSO 两份规范文档） | 本项目实现 |
|---|---|
| YAML 是唯一机器可读事实源 | `adapter/omm2schema.py` 直接把老师的 YAML 编译为 `schema/textbook_profile.json` + SHACL shapes + 提示词词表；不手抄、不另造持久化中间语言 |
| 教材抽取只建 **Reference Knowledge** 层 | profile 仅取 21 个具体参考知识类型 + 62 个关系；实体一律 `knowledgeOriginRole=data_science`、`referenceClaimStatus=source_asserted` |
| 出处（provenance）一等公民 | KnowledgeSource（书）→ SourceFragment（章/节/子片段，含 locator+页码+contentHash）→ 实体经 `resourceGroundedInFragment` / `claimGroundedInFragment` 落地；原文引文（evidence quote）保存在 sidecar 并随每条 MCP 回答返回 |
| MCP Server 属 Runtime Binding，不进本体核心 | 图谱构建完全独立于服务层；`mcp_server/` 只是只读查询封装 |
| 规范第 14 章的 agent 检索路径 | 预定义 13 个查询工具，`dso_graphrag_context` 即"一步编译上下文"，其余工具对应逐层下钻 |
| 类型/属性/关系/词表封闭 | 抽取提示词内嵌全部合法词表；payload 校验（域/值域含抽象祖先闭包、枚举归一、必填属性）；建图后 SHACL 校验 **0 违规** |

## 架构与数据流

```
教材 PDF ──slice_book.py──▶ data/fragments.jsonl (143 片段/129 抽取单元, 全书 ch1-16)
DSO YAML ──omm2schema.py──▶ schema/{textbook_profile.json, dso_reference_shapes.ttl, vocab_for_prompt.md}
                                   │
fragments + 词表 ──extract.py──▶ qwen3.8-max（闭卷 schema-guided 抽取, 逐字 evidence,
                                   │        sqlite 缓存 = 断点续跑）
                                   │  data/extractions.jsonl (129/129 单元)
                                   ▼
                            build_kg.py
              ① 全局实体归一（type+规范名+别名精确合并）
              ② 嵌入判重（bge-small-en-v1.5 余弦候选 → qwen 裁决 → union-find 合并）
              ③ 基数修复三段式（见下）
              ④ RDF 生成 + SHACL 校验 + 实体向量入库（entity_embeddings）
                                   │
        data/dso_kg.ttl + data/kb.sqlite (全文/evidence/FTS5/向量)
                                   ▼
   query/dso_kg.py（DsoKG：内存边索引 + rdflib SPARQL + FTS5 bm25 ⊕ 向量余弦 RRF 混合）
                                   ▼
              mcp_server/dso_mcp.py（14 tools + 1 resource, stdio）
                                   ▼
                Codex / Claude Code ── GraphRAG 教材问答
```

### 基数修复三段式（build_kg.py `repair`）

DSO 对若干关系有 `minCount=1 / maxCount=1` 约束，而抽取提示词只允许**片段内**关系，
跨片段链接会系统性缺失。修复原则：**确定性规则 > 闭集 LLM 链接 > 保守降级 > 丢弃**。

1. **确定性**：`maxCount=1` 谓词按置信度去重；合成语料级 `IndustryDomain: Machine Learning`
   （全书领域断言，evidence 取自正文），所有缺链 DomainConcept 确定性地挂到该领域。
2. **闭集 LLM 实体链接**：对缺失 `minCount=1` 关系的实体，把**全图同值域类型的既有实体**
   作为封闭候选集交给 qwen3.8-max 单选（要求给出 evidence，置信度 <0.6 拒绝）——
   只能选已有实体，不能发明；共补 80 条。
3. **保守类型降级**（内容保留、承诺减弱）：无 WorkflowPattern 内容的 Stage →
   ActivityPattern（5 个）；找不到所属 Method 的函数级 MethodImplementation →
   SoftwareComponent `componentType=function`（13 个，如 `train_test_split`、`pd.read_csv`）；
   找不到刻画维度的 DataCondition → DomainConcept（5 个，如 class-imbalance、overfitting）。
4. **丢弃**（最后手段，4 个）：3 个近重复的端到端项目模板变体（其中 1 个被确定性救回：
   ch2 模板 → `Housing Price Prediction`，教材实例即加州房价回归，属语料级建模断言）、
   1 个无法给出出口活动的 WorkflowPattern。

> 所有确定性断言（ML 领域、模板→房价回归）都在实体描述/本 README 中显式声明为
> 建模决策；LLM 修复边保留 evidence 引文与置信度，`relation_evidence` 表可审计。

### 嵌入判重（近重复实体合并）

确定性名称归一只能合并字面变体，抓不到措辞不同的同义实体（如 Stage
"Data Exploration" vs "Explore and Visualize Data"）。判重管线两段式：

1. **嵌入候选**：每个实体以 `displayName | 别名 | 描述前 400 字` 用本地
   **bge-small-en-v1.5**（fastembed/ONNX，384 维，CPU 推理，无外部 API）嵌入；
   同类型实体两两余弦 ≥ 0.80 进入候选（无关对实测 ~0.67，真重复 ~0.83–0.96）。
2. **LLM 裁决**：候选对分批（12 对/次）交 qwen3.8-max 判断"是否同一现实指称"——
   必须区分**命名变体**（合并：单复数/缩写/同义措辞）与**同族兄弟**（不合并：
   Random Forest Classifier vs Regressor、Batch GD vs Stochastic GD——它们的嵌入
   余弦高达 ~0.9，纯阈值必然误并，这是引入裁决的原因）。
3. **union-find 合并**：确认对做传递闭包；幸存者取出现次数最多者，吸收别名/
   描述（取长）/属性/出现记录，关系端点经 `frag_map` 重写。全部裁决带 sqlite
   缓存（`llm_cache_repair` 表，prompt 哈希键），合并明细打印在建图日志中可审计。

效果（MVP 数据实测）：4 个端到端项目模板变体合一，修复阶段的实体丢弃从 4 → 0；
合并后实体既减少冗余，也让后续 LLM 补链的候选集更干净。

### 混合向量检索

- 建图最后一步把**最终实体集**（判重+修复后）的向量写入 `kb.sqlite`
  `entity_embeddings` 表（float32 BLOB，模型名随行存储，换模型需重建）。
- 查询层 `search()` = FTS5 bm25 排名 ⊕ 向量余弦排名 的 **RRF 融合**
  （`score = Σ 1/(60+rank)`），另加"精确名称命中加分"防长描述 bm25 噪声压过
  本尊实体；`resolve()` 第 4 步与 `graphrag_context` 的实体链接自动受益。
- 新工具 `dso_similar(name)`：纯向量语义近邻（发现近义概念/相关方法）。
- **优雅降级**：向量表缺失、numpy/模型加载失败、模型版本不匹配时自动退回纯
  FTS——MCP 服务不会因嵌入依赖挂掉。

## 图谱规模（全书 16 章）

- **1527 个实体**（抽取 2174 原始实体 → 名称归一合并 603 → 嵌入判重合并 54 → 修复降级/丢弃）：
  Method 283 / ReferenceClaim 277 / Guideline 163 / DomainConcept 150 / ActivityPattern 149 /
  MethodImplementation 108 / SoftwareComponent 85 / Metric 51 / Constraint 51 /
  DataCharacteristic 46 / ProblemType 36 / WorkflowPattern 32 / ModelSpecification 28 /
  DataCondition 26 / Stage 14 / EvaluationProtocol 11 / KnowledgeSource 7 / IndustryDomain 5 /
  SolutionTemplate 3 / DataStatistic 2
- **1578 条去重关系**（1427 抽取 + 247 修复补链 − 去重/降级），RDF 共 **23398 三元组**
  （143 SourceFragment 覆盖 129 个抽取单元 + 书/章节点）
- **实体向量索引**：1527 × 384（bge-small-en-v1.5，`kb.sqlite entity_embeddings`）
- **SHACL 校验：conforms=True，0 违规**（shapes 由 YAML 机器编译，`data/shacl_report.txt`）

## 运行

> **仓库内容说明**：本仓库附带图谱本体 `data/dso_kg.ttl`、SHACL/抽取报告与可视化
> `viz/kg_explorer.html`（浏览器离线可开）；教材 PDF、含原文的产物（`kb.sqlite`、
> `fragments.jsonl` 等）、嵌入模型权重与 LLM 缓存因版权与体积**不随仓库分发**——
> 完整复现请自备教材，跑下方管线重建，并复制 `.env.example` 为 `.env` 配置自己的
> LLM 端点密钥。
>
> **第三方组件**：vis-network（Apache-2.0/MIT 双许可，版权头完整保留）、
> semantica 0.7.0（MIT，`pipeline/llm.py` 的 Provider 基类派生自其 AnthropicProvider）、
> PyMuPDF（AGPL-3.0，仅运行时 import，不随仓库分发）。

```bash
# 环境（Windows）：venv 内已装 semantica[documents]==0.7.0, anthropic, pymupdf, rdflib,
#   pyshacl, mcp, fastembed, onnxruntime==1.20.1（版本敏感，见实现要点）
# LLM 端点/密钥/模型：项目根 .env（优先于系统环境变量；当前 DeepSeek，内附回退 qwen 注释）

.venv/Scripts/python.exe pipeline/slice_book.py     # PDF → fragments.jsonl（TOC 切分, 全书 16 章）
.venv/Scripts/python.exe pipeline/extract.py --workers 4   # LLM 抽取，默认全书（sqlite 缓存，可断点续跑；--chapters 2,3 可限定）
.venv/Scripts/python.exe pipeline/build_kg.py       # 归一 + 嵌入判重 + 修复 + RDF + SHACL + 向量入库
                                                    #   （--no-dedup / --no-repair / --no-shacl 可关对应阶段）
.venv/Scripts/python.exe viz/build_explorer.py      # 重新生成交互式图谱浏览器
.venv/Scripts/python.exe eval/smoke_query.py        # 查询层冒烟（18 用例）
.venv/Scripts/python.exe eval/smoke_mcp.py          # MCP 协议级冒烟（stdio 子进程, 16 用例）
.venv/Scripts/python.exe eval/run_eval.py           # 50 题评测：裸 LLM vs KG 增强（LLM 盲评）
```

## MCP 接入

服务器：`mcp_server/dso_mcp.py`（stdio，13 tools + `dso://ontology/summary` 资源，
工具目录见 [AGENTS.md](AGENTS.md)）。

**Codex**（`~/.codex/config.toml`，路径换成你的克隆位置）：

```toml
[mcp_servers.dso_kg]
command = "D:/path/to/dso-kg-mcp/.venv/Scripts/python.exe"
args = ["D:/path/to/dso-kg-mcp/mcp_server/dso_mcp.py"]
```

**Claude Code**：项目级 `.mcp.json` 已就位（server 名 `dso-kg`，路径用
`${CLAUDE_PROJECT_DIR}` 自动跟随项目目录），在项目目录启动即可。

## 评测（50 题，双臂对比）

同一底座模型 qwen3.8-max：裸答（仅参数化知识）vs KG 增强（`graphrag_context` 编译上下文
作引证来源；上下文是证据而非覆盖上限——缺失细节允许用自身知识补齐，但不得冒充教材
引用）。LLM 评委盲评（随机交换 A/B 顺序），按 `eval/questions.jsonl` 的参考要点打 0–5 分：

| 指标 | 裸 LLM | KG 增强 |
|---|---|---|
| 平均得分 | 4.18 | **4.50** |
| 胜/负/平 | — | **19 / 5 / 26** |
| MVP 四章（ch02/03/04/07，n=40） | 4.22 | **4.60** |
| 新增章节（ch01/05/06/08–11/13/15/16，n=10） | 4.00 | **4.10** |
| **答案含教材出处引用** | **0/50** | **50/50** |

- KG 臂的显著优势在教材特有事实（q03 房价项目问题类型 2→4、q29 SGD 逃逸局部极小
  3→5、q37 stacking blender、q48 CNN 组成 4→5）与**可验证性**（引用率 50/50 vs 0/50）；
- 50 题全部成功链接实体（上限 10 个/题），检索链路无空转；
- 仅剩 5 处失分（如 q43 决策树超参数名、q50 MDP/Bellman 方程细节未入图）——
  抽取覆盖缺口如实记录为已知局限；
- 评测驱动了两轮迭代：首轮全书图谱上 KG 臂反而 3.94 < 4.20，诊断为链接质量
  （单 token 向量探针拉入泛化枢纽实体）与**锚定效应**（模型只答上下文内的内容），
  分别以"整句混合检索 + name-only FTS 单 token 探针 + 片段级向量摘录"和去锚定
  系统提示词修复。

## 可视化

`viz/build_explorer.py` 生成自包含的 `viz/kg_explorer.html`（vis-network 内联，离线可开）：
按 DSO 类型着色/过滤、搜索定位、点击节点显示属性与教材出处引文。演示流程见
`docs/demo_script.md`。

## 实现要点 / 已知问题

- **图引擎**：semantica 的 `TripletStore(backend="oxigraph")` 需要文件持久化 pyoxigraph
  Store，本机 Windows 上 pyoxigraph 0.5.13（RocksDB）**segfault**，故运行时用 rdflib
  加载 `dso_kg.ttl`（万级三元组 <1s），SPARQL 1.1 由 rdflib 提供；semantica 仍用于
  抽取 Provider 基类与工程骨架。换 Linux/macOS 可无痛切回 oxigraph 后端。
- **LLM 端点（pipeline/llm.py）**：项目根 `.env` 优先于系统环境变量
  （`ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY / ANTHROPIC_MODEL`），当前为 DeepSeek
  Anthropic 兼容端点 + deepseek-chat（非阿里云端点自动把 max_tokens 截到 8000，
  对应其输出上限）；回退阿里云 qwen3.8-max 时自动附带 `thinking: {"type":"disabled"}`
  （单片段抽取从 >10min 降到 ~1min）。anthropic SDK 2.6.0 不再接受 `temperature`
  具名参数（走 `extra_body`）。**缓存不感知模型**：抽取/修复缓存 `data/cache.sqlite`
  （`llm_cache`、`llm_cache_repair` 表）与评测缓存 `eval/cache.sqlite` 均按 prompt
  哈希键——换模型后同 prompt 重跑会命中旧 qwen 结果；要用新模型真重跑需先清对应缓存。
  现有图谱与评测数字（4.50 vs 4.18）均由 qwen3.8-max 生成，不受端点切换影响。
- **evidence 引文不入 RDF**：DSO YAML 未定义"引文"属性，遵循"不发明本体外持久化"
  规则，引文/置信度/片段全文放 sidecar `kb.sqlite`，随查询返回。
- **近重复实体**：已由嵌入判重管线处理（见上节）；传递合并的日志行 cos 显示 0.000
  表示该对经中间实体传递确认而非直接候选对，属展示口径问题。个别裁决偏激进
  （如 Random Forest Classifier 被并入 Random Forest，尽管提示词明令禁止合并共存
  兄弟概念）——54 条合并日志逐条可审计，误并可人工回滚；同名不同类型的实体
  （如 Vanishing Gradients Problem 有 3 种类型）因本体承诺不同而**不**合并。
- **onnxruntime 版本敏感**（本机 Windows）：1.31.1 `import` 即 segfault，1.17.x 与
  numpy 2.x ABI 冲突，**必须 1.20.1**；fastembed 模型缓存在 `data/fastembed_cache/`
  （项目内固定路径，离线可复现；HF 下载默认走 hf-mirror）。
- 教材为 2017 年第 1 版；`activityOrder` 等个别属性值有 "None" 字符串已在建图时清洗；
  附录 A–F 与习题解答未入图（正文 16 章全覆盖）。

## 目录

```
adapter/omm2schema.py      DSO YAML → profile/SHACL/词表（唯一事实源编译器）
pipeline/slice_book.py     PDF → SourceFragment 候选（TOC 章→节→子片段, 全书 16 章）
pipeline/llm.py            LLMProvider（.env 端点配置/thinking 块处理/重试；QwenProvider=兼容别名）
pipeline/extract.py        schema-guided 闭卷抽取 + payload 校验 + 缓存（断点续跑）
pipeline/embeddings.py     本地嵌入封装（fastembed bge-small-en-v1.5, 判重/检索共用）
pipeline/build_kg.py       全局归一 + 嵌入判重 + 基数修复 + RDF/SHACL/sidecar/向量入库
query/dso_kg.py            DsoKG 查询层（14 种预定义操作 + RRF 混合检索 + GraphRAG 编译）
mcp_server/dso_mcp.py      MCP stdio 服务器
schema/                    由 YAML 编译的 profile、SHACL shapes、提示词词表
data/                      fragments/extractions/dso_kg.ttl/kb.sqlite/fastembed_cache/报告
eval/                      50 题评测集 + 双臂评测运行器 + 冒烟测试
DSO_extracted/../          老师原始材料（YAML 为只读事实源）
```
