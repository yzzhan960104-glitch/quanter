# TradingAgents（TauricResearch）接入调研 · 2026-09-05

> 调研对象：https://github.com/vongchu/TradingAgents_TauricResearch（用户 09-05 指定）
> 方法：一手源码深读（浅克隆 50 commits + GitHub API 交叉验证；**行号引用均对应 v0.3.1**）× 本地工程缝隙比对（infra/llm、research/ 流水线、data_lake、ops cron 链）。

## TL;DR

1. **该仓库不是 fork，是上游 `TauricResearch/TradingAgents` v0.3.1 的冻结镜像**：GitHub API `fork:false / parent:null`，克隆 git log 全部为上游作者 Yijia-Xiao 提交，HEAD `01477f9` 本体即上游 commit；2026-07-06（v0.3.1 发布次日）创建后零推送。上游已到 v0.4.1（2026-09-01）——**接入应跟踪上游 pin tag，镜像无任何独有内容**。
2. 框架本体 = LangGraph 多智能体**研究器**（不下单）：4 分析师 → 多空辩论 → 交易员 → 风控三人辩论 → 基金经理五档裁决（Buy/Overweight/Hold/Underweight/Sell）；一次 run ≈ **15-20 次 LLM 调用/标的**。
3. **与本项目唯一正确的关系 = 观察层/假设源，永不进信号链**：LLM 裁决原则上不可通过全窗口组合级回测验证（六年逐日辩论成本爆炸 + 训练语料 look-ahead 污染历史回放），按幸存者偏差红线只能做「每日盘后辩论报告 → 注入 digest 的证据源之一 + 人审辅助」。
4. LLM 对接比预期顺但**本地双坑**：GLM 是框架官方一等公民 provider（默认端点就是 z.ai paas/v4）——但本账户 paas/v4 按量池为空（恒 429 code 1113）开箱即炸；且 api.z.ai AAAA 在本机 IPv6 黑洞，httpx 系 SDK 必须进程级强制 IPv4。
5. 推荐路径 **(a) 独立 venv 独立进程 PoC（3-6 人日）**：只开 market 分析师 + 本地 parquet vendor + 关闭记忆层，跑持仓/观察池 5-10 只，人工比对其裁决与颈线信号/regime 的共识分歧，验出正交增量再谈固化。

## 0. 版本事实

| 项 | 实证 |
|---|---|
| fork 关系 | GitHub API `fork:false, parent:null`；镜像创建 2026-07-06（上游 v0.3.1 发布次日），pushed_at 同日=冻结 |
| 同源性 | 克隆 git log 无一条独立 commit；HEAD `01477f9` 在上游仓库 API 返回 200（逐字节同源） |
| 上游现状 | 102,585★ / 19,769 forks / Apache-2.0；月级发版（2026-02 v0.2.0 → 2026-09-01 v0.4.1，v0.4.0 含 FRED/社媒/决策记忆 look-ahead 修复、GLM-5.3 catalog） |
| 论文 | arXiv:2412.20138 描述的是**旧 autogen 架构**；当前代码已完全重写为 LangGraph 版——读论文架构图会误导 |

## 1. 框架机制速览（源码实证）

### 1.1 Agent 图与调用档位（`tradingagents/graph/setup.py` L61-156）

| 段 | 节点 | 档位 | 机制 |
|---|---|---|---|
| 分析师 | Market / Sentiment / News / Fundamentals | 全 quick | ReAct 循环（`llm.bind_tools`，market_analyst.py L81-93）；Sentiment 特殊=节点内预取注入、单次调用无工具 |
| 多空辩论 | Bull / Bear Researcher | quick | 纯文本交替辩论；`count >= 2*max_debate_rounds`（默认 1=各 1 次）→ Research Manager（deep）出结构化 ResearchPlan |
| 交易员 | Trader | quick | ResearchPlan → TraderProposal |
| 风控+终裁 | Aggressive/Conservative/Neutral 三辩手 + Portfolio Manager | 辩手 quick、PM deep | 三人轮转（各 1 次）→ PM 出 PortfolioDecision |

### 1.2 输出物
- `TradingAgentsGraph.propagate(company, trade_date) -> (final_state, signal)`；signal=**确定性正则**从 markdown 提取五档 rating（`graph/signal_processing.py`，无二次 LLM）。
- `PortfolioDecision`（agents/schemas.py L188-229）= rating 五档 + executive_summary + investment_thesis + 可选 price_target/time_horizon。**最终决策无数值置信度**（confidence 仅 Sentiment 报告有 low/medium/high）。
- 全量落盘：`~/.tradingagents/logs/<TICKER>/full_states_log_<date>.json` + `reporting.py` 五段报告树（1_analysts…5_portfolio/complete_report.md）。
- `trade_date` 作为分析基准日严格贯穿（指标按日截断、新闻按窗口过滤）——支持回溯历史日期。

### 1.3 记忆/反思
每次 run 决策 append `~/.tradingagents/memory/trading_memory.md`；下次同 ticker run 用 **yfinance** 拉 realized return 调 1 次反思注入 PM prompt（trading_graph.py L296-334）。`config["memory_log_path"]=None` 时整层 no-op（memory.py L39-43）——**A 股替换场景推荐先关**（收益源必须换本地湖，否则=第二个外网依赖+幸存者偏差新来源）。

### 1.4 成本结构
一次 run ≈ **15-20 次 LLM 调用/标的**（分析师 7-11 + 辩论 3 + trader 1 + 风控 4）；debate/risk 轮次翻倍各 +2/+3。quick 档 12-16 次可配 glm flash/air 级压成本；deep 仅 RM+PM 共 2 次。无 embedding/RAG 成本。

## 2. 数据层：A 股适配缝隙

### 2.1 现状
- 所有工具经 `dataflows/interface.py route_to_vendor` → `VENDOR_METHODS` 注册表（L95-144）；vendor 链由 `data_vendors`/`tool_vendors` 配置决定，**无静默回退**，错误统一 `NO_DATA_AVAILABLE` 哨兵串。
- 默认源全 keyless（除可选 FRED/AlphaVantage）：yfinance（行情/指标/财报/新闻——A 股 `.SS/.SZ` 原生透传，README 明示支持）+ StockTwits/Reddit（keyless，国内需代理；**全链优雅降级**，失败返回 `<unavailable>` 占位不炸）。
- **无 SimFin、无 finnhub**（老版本残留已移除；pyproject 里的 backtrader/redis 是零引用历史遗留）。

### 2.2 换本地湖的五个落点（侵入性升序，全部源码定位）
1. **`VENDOR_METHODS` 加 `"local"` vendor**——返回契约=给 LLM 读的 CSV/markdown 字符串；最干净的路由级改法。
2. **`stockstats_utils.load_ohlcv`（L125-192）= 指标+verified snapshot 的共同咽喉**——改成读 `data_lake/a_shares_daily.parquet`，一举三得（行情/指标/快照）；**必须保留 curr_date 截断防未来函数 + stale 检查**；列契约 Date/Open/High/Low/Close/Volume。
3. `y_finance.get_YFin_data_online`（单独供 get_stock_data）。
4. `resolve_instrument_identity`（agent_utils.py L78，fail-open 返回 {} 不炸）→ 换读 `stock_basic`（名称/行业）。
5. 记忆反思收益源 → 先整层关闭（见 1.3）。
- `selected_analysts=("market",)` = 只开技术面分析师、图照走全链辩论的**正式运行形态**（其余三份报告为空串注入不报错）——国内新闻/社媒全缺场景的标准答案。
- **增强位**：market 分析师的工具可查任意 symbol——local vendor 同时喂 `index_daily`（创业板指 399006.SZ）即可让辩论自带大盘 regime 语境（呼应 neckline-regime-dependence：六年期望全部来自指数>MA60 日）。

### 2.3 工作量与剩余价值判断
- 最小集成 **3-6 人日**（vendor 1-2 + 旁路/config 0.5-1 + venv/联调 0.5-1 + 跑批解析 1-2）；prompt 深度中文化另 +1-2 人日（`output_language=Chinese` 只翻报告正文，系统提示/辩论语境仍是美股叙事——alpha 对标、Fed 词表、辩论角色语言）。
- 诚实判断：news/sentiment/fundamentals 缺位后，实际剩下「market 报告 + 形式化辩论 + 结构化裁决」——**增值点在辩论/裁决结构，不在分析师深度**。

## 3. LLM 对接 × 本地三坑（本地实证部分，外部报告不可能覆盖）

框架侧（子代理已验）：
- 纯 LangChain 栈（langchain-openai/anthropic/google-genai + langgraph），无 litellm/autogen。
- OpenAI 兼容注册表**官方含 glm**（默认端点 `https://api.z.ai/api/paas/v4/`，key=`ZHIPU_API_KEY`，openai_client.py L212-233）；base_url 优先级 config `backend_url` > provider env > 注册表默认（L286-299）；自定义端点一律走标准 Chat Completions（无 Responses API 坑）；结构化输出按能力表选 function_calling 兜底、失败自动降级自由文本（GLM 生态友好）。

本地坑（本项目 09-04 实证地图）：
1. **paas/v4 按量空池（恒 429 code 1113）**：`llm_provider="glm"` 默认路径开箱即炸。出路二选一：
   - a. **充值按量池**（最省事；function calling 在 GLM OpenAI 协议上成熟）；
   - b. **anthropic 兼容路径吃 Coding Plan 周配额**：provider=anthropic + `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic` + `ANTHROPIC_API_KEY=<GLM key>`（anthropic SDK 读 env base_url）——但 **bind_tools/with_structured_output 在 z.ai anthropic 端点的工具调用兼容性未验**（本项目 GlmClient 从不用 tools），列 PoC 第一测试项；不通则回落 a。
2. **IPv6 黑洞**：api.z.ai AAAA 在本机 v6 出口黑洞，openai/httpx SDK 挂 TLS 握手。项目内修法是作用域化 `_IPv4HTTPSConnection`；独立进程修法=**runner 入口 monkeypatch `socket.getaddrinfo` 强制 AF_INET**（~10 行，仅作用本进程）。
3. **配额竞争**：loser_review 已用 glm-5.3 thinking=max 吃 Coding Plan 配额；TA 每标的 15-20 次 × 5-10 只=每日 100-200 次新增调用。quick 档配 flash/air、只跑持仓+观察池小样本控消耗。
- （第三条路=7002 网关自建 OpenAI 兼容 /v1/chat/completions 代理包 GlmClient——要透传工具调用，工作量大于 a/b，仅当两者都死才值得。）

## 4. 与研究流水线的接线（本地缝隙）

现有链：18:05 loser_review（逐持仓归因）→ 18:30 digest（摘要+提案）→ 18:45 explore_loop（归因意见→网格→evaluate_replay→提案流）→ proposals A 档闸（APPROVED→publish DRAFT→人审）。

- explore_loop 已有「探索摘要注入次日 digest md」学习回路——**TA 报告走同款注入位**，只是证据源之一。
- 建议档位：独立进程 ~19:00（避开 18:00-18:02 湖写窗口），产物 `logs/ta_review_{day}.md/json`；不进 explore_loop 网格（其输出无参数语义，进不了 NecklineConfig 探索面）。
- 对照位：TA=**事前视角**五档评级 vs loser_review=**事后视角**亏损解剖，互补；先并行人工对照，评估 TA 增量再谈任何自动化。

## 5. 红线对照：为什么只能是观察层

- 幸存者偏差红线（09-04 三连实锤）：LLM 结论=假设，必须过全窗口组合级回测才能部署。
- TA 五档评级**原则上不可回测**：① 成本爆炸（六年×逐标的×15-20 调用）；② 更根本的 **look-ahead 污染**——GLM 训练语料包含分析日之后的行情，历史回放的评级混入未来知识，通过/不通过都不可信。
- 因此：评级**永不进信号/过滤/sizing 链**；合法用途=观察层报告（人读）+ 归因假设源（走既有提案验证闸）+ 人审辅助。
- 与 09-05 七波过滤器否决潮元结论一致：能进部署链的只有可全窗口重放的确定性规则。

## 6. 路径裁决与 PoC 清单

| 路径 | 改动面 | 判断 |
|---|---|---|
| (a) 独立 venv 独立进程 PoC | §2.2 五落点，3-6 人日 | **推荐先做**：可逆、不动主工程（主工程刻意零 langchain 依赖） |
| (b) 借图骨架自研复刻 | ~300 行 + 全 prompt 自研 | (a) 验出正交增量后再议 |
| (c) 拆 memory/反思组件 | 收益源必须换本地湖 | 与 loser_review 重叠，暂缓 |

### PoC 验收（go/no-go）
1. **LLM 通路**：anthropic 路径工具调用是否可用（不通→充值 paas/v4 走 a）；同时验 IPv4 monkeypatch 生效。
2. **数据通路**：local vendor 喂 3 只持仓，market 报告数字与湖对账（框架自带 `get_verified_market_snapshot` 防编造机制可复用）。
3. **增量评估**：5-10 只 × 3-5 个历史日（**仅人工观察对照，不做统计推断**——look-ahead 污染）+ 前瞻 paper run 2 周，人工比对与颈线信号/regime 的共识分歧率。
4. **go 条件**：辩论裁决层提供颈线信号之外的正交视角（大盘 regime 转折预警、持仓风险第二意见）——否则停在做报告层。

## 7. 工程注意
- 跟踪上游 `TauricResearch/TradingAgents` 并 pin tag；本文行号=v0.3.1；v0.4.x 的 look-ahead 修复对方法论是加分项，升级时重验 §2.2 五落点。
- `requires-python >=3.10`（本机 3.10.11 满足，无版本障碍）；**独立 venv**（langchain 全家桶不进主工程 requirements）；Windows 无官方 CI 背书但无已知硬伤（v0.2.4 已全量修 UTF-8），跑批用 Python API 非 CLI（CLI 是 questionary 交互式，无定时形态；**框架无 HTTP 服务形态**，backend 需自建）。
- Apache-2.0，商用无碍。

## 附：源码克隆存档
`C:\Users\yzzhan\AppData\Local\Temp\tmp.xsHfC5gfPK\ta`（v0.3.1 @ `01477f9`，depth 50；PoC 可直接复用，废弃可整目录删除）
