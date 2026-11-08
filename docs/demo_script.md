# 演示脚本（约 8–10 分钟）

## 0. 开场：一张图（1 min）

浏览器打开 `viz/kg_explorer.html`（离线可用）：
- **全书 16 章**：1527 个 DSO 实体、1578 条关系、23398 三元组，按类型着色；
- 现场操作：图例点掉 ReferenceClaim/Guideline → 只剩方法/概念骨架；搜索 "AdaBoost" 回车定位，
  点击节点 → 右侧显示描述、本体属性、**教材出处 + 原文引文**。

## 1. 本体是事实源（1 min）

- 展示 `DSO_extracted/DSO_v0.2_Ontology_Model.yaml`（老师发的，只读）；
- `adapter/omm2schema.py` 一键编译出三样东西：抽取词表（`schema/vocab_for_prompt.md`）、
  校验 shapes（`schema/dso_reference_shapes.ttl`）、运行时 profile——**没有任何手抄**；
- `data/shacl_report.txt`：`Conforms: True`，0 违规。

## 2. Harness 问答（核心，3 min）

在 Codex（或 Claude Code，`.mcp.json` 已配好）里依次问：

1. **"教材里处理类别不平衡时推荐怎么做？"**
   → agent 调 `dso_graphrag_context` → 回答带 Guideline/claimStatement + 出处
   （Ch.3 'Performance Measures'）。追问"出处原文给我看看" → `dso_provenance`。
2. **"数据量大、特征多的分类任务该选什么方法？"**
   → `dso_find_methods(problem_type=classification, data_condition=…)`
   → 方法列表含适用/排除条件（如 SGD 适合超大训练集）。
3. **"第 2 章的端到端项目流程是什么？"**
   → `dso_get_workflow` → SolutionTemplate → Stage → WorkflowPattern 链。
4. **混合检索彩蛋**：`dso_search("ways to fight overfitting in deep networks")` ——
   纯关键词搜不到的改写提问照样命中（BM25+嵌入向量 RRF 融合）；
   再 `dso_similar("AdaBoost")` → Gradient Boosting / Shrinkage / SAMME 语义近邻。
5. 兜底逃生舱：`dso_sparql("SELECT … WHERE { ?m rdf:type ds:Method }")` 现场数一下 283 个 Method。

强调：每条回答都能落到 **章/节 + PDF 页码 + ≤25 词原文引文**，这是与"直接问大模型"的本质区别。

## 3. 评测结果（1–2 min）

- 50 道题（覆盖全书 16 章），同一模型 qwen3.8-max 双臂对比：裸答 vs KG 增强；
- LLM 盲评（随机交换 A/B 顺序）按参考要点打分 0–5，结果：
  - 均分 **4.50 (KG) vs 4.18 (裸)**；胜/负/平 **19/5/26**；
  - **杀手锏：教材出处引用率 50/50 vs 0/50**——KG 臂每条回答可溯源到章/节/页码/原文；
  - 现场讲一个好例子：**q29**（SGD 为何能逃逸局部极小）裸答 3 分、KG 臂 5 分；
    或 **q03**（加州房价项目问题类型）2→4；
  - 诚实讲失分例子：**q43**（决策树超参数名）/ **q50**（MDP/Bellman 细节）未入图 →
    抽取覆盖是已知局限，说明评测不是自吹；
  - 可加讲评测驱动迭代的故事：首轮全书图谱 KG 臂反而落后（3.94 vs 4.20），
    诊断出链接噪声 + 模型锚定上下文两个病因，修复后反超——评测集是调试工具，
    不只是成绩单。

## 4. 工程细节彩蛋（1 min，可选）

- 抽取用 **schema-guided 闭卷提示词**：类型/属性/关系/枚举全部来自 YAML 编译的词表，
  校验层丢弃了 239 条域值域/必填违规——宁缺毋滥；
- 跨片段缺失的 `minCount=1` 关系用 **闭集 LLM 实体链接** 修复（只能从既有实体里选，
  必须给 evidence），修不了的**保守降级类型**而不是删除（Stage→ActivityPattern 等）；
- **嵌入判重**：本地 bge-small-en-v1.5 余弦生成候选对 → LLM 裁决"是否同一指称"
  （防 Classifier/Regressor 这种余弦 0.9 的兄弟概念误并）→ union-find 合并，
  日志逐条可审计；
- **混合检索**：BM25 + 向量余弦 RRF 融合 + 精确名称加分；向量索引缺失自动退化纯 FTS；
- 全部 LLM 调用带 sqlite 缓存，整个管线可复现、可断点续跑（全书 129 个抽取单元
  中断后重跑只补新单元）；
- pyoxigraph 文件库在 Windows 上 segfault → 运行时切 rdflib；onnxruntime 1.31 也
  segfault → 锁 1.20.1，说明了对存储/推理后端的取舍（README 有记录）。

## 5. 收尾

架构图（README）+ 目录结构一页过。全书 16 章、嵌入判重、混合检索均已落地
（含片段级向量：GraphRAG 的原文摘录按"实体提及×2 + 问题词密度 + 片段向量余弦"
挑选）；未来工作：多教材 KnowledgeSource、附录/习题入图、Linux 下切回 oxigraph 后端。

---

### 演示前检查清单

```bash
.venv/Scripts/python.exe eval/smoke_query.py    # 0/18 suspicious
.venv/Scripts/python.exe eval/smoke_mcp.py      # 0/16 failures
start viz\kg_explorer.html                       # 浏览器（2.5MB，离线可开）
# Codex: ~/.codex/config.toml 已含 [mcp_servers.dso_kg]
# Claude Code: 项目根启动即可（.mcp.json）
```
