# AGENTS.md — 使用 DSO 教材知识图谱 MCP 服务

你（Codex / Claude Code 等 harness 中的 agent）可以通过 MCP 服务器 **`dso_kg`** 查询一个
按 DSO v0.2 本体（Data Science Ontology）建模的教材知识图谱，语料为
《Hands-On Machine Learning with Scikit-Learn and TensorFlow》（Géron，第 1 版）
**全书 16 章**（机器学习基础 → 端到端项目 → 经典算法 → TensorFlow/神经网络 →
CNN/RNN/自编码器/强化学习；附录与习题解答除外）。检索为**混合模式**：
BM25 全文 + 嵌入向量余弦，RRF 融合排序，改写/近义提问也能命中。

## 何时使用

数据科学/机器学习问答中，凡涉及教材覆盖的概念、方法选型、指标、经验法则（guideline）、
约束（constraint）、流程（workflow），优先查询知识图谱而不是仅凭参数化知识回答。
KG 的回答带有教材出处（章节 + PDF 页码 + 原文引文），应保留在最终答案里。

## 工具目录（14 个）

| 工具 | 用途 |
|---|---|
| `dso_graphrag_context(question)` | **问答首选入口**：一次调用返回编译好的 KG 上下文（关联实体+属性+分类+适用性+claims+带引文的原文摘录），可直接据此作答 |
| `dso_search(query, type?, limit?)` | **混合检索**（BM25+向量 RRF 融合）实体；type 可用 Method / DomainConcept / Guideline / Constraint / ReferenceClaim / Metric / EvaluationProtocol / ProblemType / DataCondition / DataCharacteristic / SolutionTemplate / Stage / WorkflowPattern / ActivityPattern / MethodImplementation / SoftwareComponent 等 |
| `dso_similar(name, limit?, same_type?)` | 语义近邻：与某实体嵌入余弦最近的实体（发现近义概念/相关方法，关键词搜不到时用） |
| `dso_get_concept(name)` | 单个实体的完整概念卡：类型、描述、别名、本体属性、出/入关系、出处 |
| `dso_taxonomy(name, direction?)` | 沿 *Broader*/refines 关系走上下位分类树 |
| `dso_related(name, depth?)` | N 跳邻域浏览 |
| `dso_path(source, target)` | 两个实体间的图谱连接路径（≤4 跳） |
| `dso_find_methods(problem_type?, data_condition?)` | **方法选型**：按问题类型/数据条件过滤 Method，含适用条件与排除条件 |
| `dso_get_claims(about?, kind?, limit?)` | 教材经验知识：ReferenceClaim/Guideline/Constraint，含 claimStatement 与出处 |
| `dso_get_workflow(problem_type?)` | 过程知识：SolutionTemplate → Stage → WorkflowPattern → ActivityPattern |
| `dso_get_metrics(problem_type?)` | 指标与评估协议（公式、优化方向、被哪些问题类型使用） |
| `dso_provenance(name)` | 实体的全部出处：章节/页码/引文/上下文摘录 |
| `dso_sparql(query)` | 只读 SPARQL 1.1 逃生舱（前缀 ds:/inst:/rdfs:/rdf:/xsd: 已声明） |
| `dso_stats()` | 图谱规模统计 |

另有 MCP 资源 `dso://ontology/summary`：本体 profile（类型/属性/关系/受控词表）速查。

## 推荐问答流程（对应 DSO 规范第 14 章的检索路径）

1. `dso_graphrag_context(问题)` → 通常已足够作答；
2. 需要细节时下钻：`dso_search` → `dso_get_concept` → `dso_get_claims(about=…)` → `dso_provenance`；
   关键词搜不到时换 `dso_similar` 找近义实体再下钻；
3. 方法选型问题：`dso_find_methods` + `dso_get_metrics` + `dso_get_workflow`；
4. 引用答案时附出处，如：*（Ch.7 'Boosting', PDF pp.215-222）*。

## 边界与注意

- 覆盖全书 16 章正文，但**附录（A–F）与习题解答未入图**；教材之外的内容
  （如第 2 版新增的 Keras/TF2 内容）KG 会查不到，此时应明说"知识图谱未覆盖"，
  再谨慎用自身知识补充。
- KG 内容是**教材的参考知识（Reference Knowledge）**，非实证验证结论；
  claims 带 `referenceClaimStatus`（source_asserted）标记。
- 所有实体经 LLM 抽取 + 模式校验（域/值域、基数、受控词表）+ SHACL 校验（0 违规），
  但抽取仍可能有遗漏；查不到 ≠ 教材里没有，可换关键词重试或直接用 `dso_sparql`。
