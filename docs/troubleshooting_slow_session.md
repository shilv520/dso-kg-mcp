# 故障排查记录：会话"卡死/慢"（2026-11-07）

> 现象持续一整天，先后被归因为"API 慢""端点挂"，最终查明是 **4 个互相独立的问题叠加**，
> 其中一个是本项目查询层的真 bug。此文档记录完整排查过程、修复与验证数据，方法论可复用。

## 一、现象

- Claude Code 会话提问后长时间转圈，状态栏 token 计数冻结（如 `↓197 tokens` 停 2 分钟以上）；
- 偶发 `Unable to connect to API (ConnectionRefused)`，重试数次后又能收到回复；
- 换成 DeepSeek 端点后"还是这样"；
- 工具调用后界面假死，无任何报错。

## 二、根因总览（4 个独立问题）

| # | 层 | 根因 | 修复 |
|---|---|---|---|
| ① | 会话端点 | 全局配置指向阿里云 qwen3.8-max MaaS 端点，其 SSE 流式响应频繁中途停摆（token 冻结即此症状） | 项目级 `.claude/settings.local.json` 覆盖 `model` + `env`，本目录会话改走 DeepSeek（`https://api.deepseek.com/anthropic`，deepseek-chat）；全局配置未动 |
| ② | 项目源码 | **真 bug**：`query/dso_kg.py` `sparql_raw()` 对 SELECT 结果统一取 `v.value`——该属性只有 rdflib `Literal` 有；凡返回实体 URI（`URIRef`）的查询必抛 `AttributeError` | 按类型序列化：Literal→Python 原生值，URIRef/BNode→URI 字符串；见 `query/dso_kg.py` sparql_raw |
| ③ | 权限交互 | default 权限模式下，每个 MCP 工具调用都弹确认框；不点允许会话就一直等 → **看起来像卡死**（transcript 里 tool_use 有、tool_result 无即此状态） | 经用户同意将 `mcp__dso-kg`（14 个只读查询工具）加入 `permissions.allow` 白名单 |
| ④ | 本机网络 | Clash Verge（TUN，127.0.0.1:7897）偶发抖动造成瞬时 `ConnectionRefused`/`FailedToOpenSocket`；`api.deepseek.com` 为国内直连，不需要代理 | Claude Code 自动重试可自愈（实测第 4 次成功）；频发时退出 Clash 或关 TUN |

### ②为什么一直没被测出来（测试盲区，双重）

1. 冒烟测试里唯一的 SPARQL 用例是 `SELECT (COUNT(...) AS ?n)`——COUNT 返回整数 Literal，恰好绕开 URIRef 分支；
2. `dso_mcp.py` 把工具异常吞成 `"SPARQL error: ..."` **文本**返回（协议层 `is_error=False`），而 `smoke_mcp.py` 只检查协议层标志 → 错误文本被当成 OK。

修复：`eval/smoke_mcp.py` ① 新增回归用例（`SELECT ?c ?label ... OPTIONAL ... ORDER BY`，即触发 bug 的查询形状）；② 判定加固——返回文本以 `ERROR` / `SPARQL error` 开头一律算失败。

## 三、排查方法论（可复用）

### 1. 先分清三层，别混着猜

```
你 ↔ Claude Code 会话进程 ↔ LLM 端点(SSE)     ← 聊天快慢只由这层决定（①④）
                ↕ MCP (stdio 子进程)
        dso_mcp.py + dso_kg.py（本地，不调 LLM） ← 工具快慢由这层决定（②③）
pipeline 脚本 ↔ .env 配置的 LLM                  ← 只影响建图/评测，与聊天无关
```

### 2. transcript 是取证金矿（比截图可靠）

每个会话都写 JSONL 日志：`C:\Users\<user>\.claude\projects\<项目slug>\<sessionId>.jsonl`

```bash
# 最近 30 分钟活跃的会话 + 各自实际使用的模型
find ~/.claude/projects -name '*.jsonl' -mmin -30 -printf '%T@|%p\n' | sort -rn | head -5
grep -o '"model":"[^"]*"' <file> | sort | uniq -c     # 会话真实模型（API 返回什么记什么）
grep 'api_error\|ConnectionRefused' <file>            # 网络层错误及重试记录
```

关键字段：`model`（实际端点返回的模型名）、`timestamp`（逐条时间线）、
`turn_duration`（整轮耗时 ms）、`usage.output_tokens`（状态栏 ↓N tokens 的来源）、
`tool_use` 后**有没有**对应 `tool_result`（没有 = 卡在权限框或工具挂起）。

本次破案的决定性证据即来自这里：
- 18:30 会话 `turn_duration: 2640` → DeepSeek 切换早已生效、端点不慢；
- 19:08 会话 3 条 `api_error`(ConnectionRefused) 后第 4 次成功，`output_tokens:197`
  与用户截图 `↓197 tokens` 完全对上 → "卡死"其实是**回复已到、在等权限框**；
- 该会话日志止于 `dso_sparql` 的 tool_use、无 tool_result → 引出对这条查询的复现 → 抓到 bug②。

### 3. 复现用户触发的那条精确输入

不要只跑自己的测试用例——把 transcript 里失败那次调用的**原始参数**抄出来直接打：

```python
kg = DsoKG()                        # 1.8s（= MCP 服务器启动成本）
kg.sparql_raw("SELECT ?c ?label WHERE { ?c a ds:DomainConcept "
              "OPTIONAL { ?c rdfs:label ?label } } ORDER BY ?label LIMIT 80")
# 修复前: AttributeError: 'URIRef' object has no attribute 'value'
# 修复后: 0.24s（热缓存 0.08s）
```

### 4. 端点健康用"同形状流量"探测

短请求 401 通 ≠ 流式推理通。用与 harness 相同形状的请求测：
system(带 cache_control) + tools + `messages.stream()`，看首 token 延迟与完整性。
实测：首 token 7.3s / 全程 7.7s / 正常返回 tool_use → 端点健康，排除①④为当前主因。

## 四、修复文件清单

| 文件 | 变更 |
|---|---|
| `.claude/settings.local.json` | `model: deepseek-chat` + `env`(DeepSeek 端点，密钥在文件内，勿外传) + `permissions.allow: ["mcp__dso-kg"]` |
| `query/dso_kg.py` | `sparql_raw` SELECT 结果按 rdflib 类型序列化（修 URIRef 崩溃） |
| `eval/smoke_mcp.py` | +1 回归用例（URI 返回型 SELECT）；失败判定加固（识别错误文本） |
| `.env` / `pipeline/llm.py` | （更早阶段）管线 LLM 端点与全局环境解耦：.env 优先、DeepSeek 8K max_tokens 截断、仅阿里云发 thinking:disabled |

## 五、验证数据（修复后实测）

- MCP 协议级冒烟：**0/17 失败，总耗时 9.0s**（含服务器启动、14 工具、2 次 graphrag_context、2 条 SPARQL、1 资源）；
- 用户触发 bug 的原查询：0.24s（热 0.08s）；
- DeepSeek 流式探针（tools+cache_control）：首 token 7.3s，正常结束；
- 用户真实会话（deepseek-chat）：单轮 2.6s / 5s（含 28K 输入 token）。

## 六、经验教训

1. **"卡死"先定位在哪一层**：token 计数冻结=会话↔端点流式层；tool_use 无 tool_result=权限框或工具挂起；两者 transcript 都能看出来，不用猜。
2. **测试要断言内容，不能只看协议**：把异常吞成正常文本返回的接口，冒烟测试必须检查文本前缀/结构，否则全盲。
3. **测试用例要覆盖"形状"而非只覆盖"通路"**：COUNT 通过不代表 SELECT 实体通过；序列化分支（Literal/URIRef/BNode）每个都要有用例。
4. **运行中的会话不会热加载配置**：改 settings 后必须关窗重开；判断有没有生效看 `/status` 或 transcript 的 `model` 字段，别看体感。
5. **本机代理（Clash TUN）是国内直连端点的无谓变量**：项目端点不需要代理时，退出代理软件排除干扰。
6. 权限弹窗不是故障，是 default 模式的正常交互；只读 MCP 服务器可整服加白名单（`mcp__<server>`）。
