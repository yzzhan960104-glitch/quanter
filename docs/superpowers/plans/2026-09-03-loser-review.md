# 每日亏损持仓 LLM 深度归因（loser review · 2026-09-03）

> 用户需求：对持仓为负的部分每日做大模型的深度分析，为什么亏。
> 决策：产物本地+上站（腿详情页新卡+持仓抽屉摘要）；模型 glm-5.3（z.ai
> Anthropic 兼容端点，用户指定 5.3 max 档；z.ai 无独立 max id，旗舰=glm-5.3）。

## 一、架构（全走既有不变量）

```
16:15 cron（OPS_TASK_CRONS，mon-fri；server 重启后生效）
  └─ python -m ops.loser_review          # DETACHED 子进程，logs/ops_loser_review.log
       ├─ 组装：7002 positions(fpnl<0) ∩ state.pkl 活仓 → 每只上下文六块
       │   身份(stock_basic 名/行业) / 进场(entry+audit SIGNAL 回扫：颈线/RR/ATR/理论价)
       │   现状(浮亏%/持有交易日 vs max_holding/距超期) / 定身位(stop 距%/TP/trailing)
       │   K线(进场前20根+进场后全部，湖前复权；高点回撤统计) / 市场(上证+沪深300 同期%)
       ├─ LLM：infra/llm 现成端口（get_llm_client），每只一次串行
       │   输出=JSON(主因/次因/证据/置信度/风险状态三档)+markdown 深度正文
       └─ 产物 logs/loser_review_{day}.json + job_run 台账(ops_loser_review)
快照：public_snapshot._loser_review 透传当日产物 → loser_review.json（16:15 前
  发布自然降级 legs=null）
前端：api/review.ts facade + LoserReviewCard（腿详情页）+ PositionsPanel 抽屉摘要
```

## 二、LLM 通道改动（infra/llm，最小侵入）

- `glm.py`：**流式（SSE）**——z.ai glm-5.3 思考期完全静默（thinking delta 都
  不发，实测 2k 字 prompt 静默 150s+），非流式读超时必炸；流式下 timeout=包
  间隔（300s 容忍思考段）。**thinking_budget 参数**（Anthropic 协议
  thinking.budget_tokens，计入 max_tokens）——不控制思考时长但约束思考产出。
- `base.py` Protocol 同步可选参数（老调用方 proposals/training_analyzer 零影响）。
- 归因调用参数：max_tokens=4096 + thinking_budget=1024；单只实测 ~2-3 分钟，
  10 只串行 ~30 分钟（16:15→16:45，18:00 管道前收口）。

## 三、红线

- 只读分析：输出归因观点与风险状态三档（持有观察/收紧关注/临近风控线）——
  是状态描述不是交易指令；prompt 明令"绝不给出买卖指令或参数修改建议"。
- 输入全为持仓/行情事实，无未来计划数据——零前跑面。
- 输出经 sanitize 上站（无账户/凭证面）；risk_state 词表外置 null（前端中性灰）。

## 四、踩坑实录（09-03 实测时间线）

1. glm-5.3 content 首块=thinking：`content[0]["text"]` KeyError → 改拼接全部
   text 块（后随流式重构覆盖）。
2. max_tokens 4096 被 thinking 耗尽（响应只剩 thinking 无 text）→ 上调。
3. 16k 无预算：thinking 无节制膨胀，300s 读超时 → 加 budget。
4. budget 1024/3072 均超时：**z.ai 思考期不发任何 SSE 包**，budget 不控时长 →
   流式 + 包间隔 300s 才扛住。

## 五、验证与产物形态

- pytest 10 件（解析围栏/词表守卫/降级/K线窗口终点/市场背景/信号回扫——
  CSV 夹具必须 csv.writer 写，手拼裸行 JSON 含逗号会被 reader 拆碎）。
- vitest 3 件（按腿过滤/三档 tag/展开交互/空态）。
- 手动首跑当日全量 → 快照 → 发布 → 线上 JSON+浏览器验收。

## 六、明确不做

- 不产生交易信号/参数修改执行链（参数演进只属研究线提案流）；
- 不动策略与 server 只读架构；不为分析新建 DB（logs JSON 即产物）；
- 不做盘中实时归因（快照时点语义，16:15 一次/日）。
