# LLM 委员会深度嵌入设计 · 2026-09-06

> **实施状态（同日完成）**：三决策点经用户「按此实施」按推荐默认值落地——
> kb.py 三源加载 + Tier 0 谱面闸（create_proposal 内嵌）；review.py 统一评审
> 服务（B 质证官/A scoped 四席，verdict schema，超时护栏，transcript 落
> logs/committee_reviews/）；publish_proposal 闸（唯一 fail-closed：ESCALATE→
> NEEDS_HUMAN + CommitteeEscalated，UNAVAILABLE 照常放行带附签）；loser_review
> /explore_loop 钩子 + digest 评审台段；tests/research/test_committee_review.py
> 14 用例（tier0/KB 三源/fail-open/解析降级/publish 拦截与放行/闸开关）全绿，
> 全量 tests/research+ops 155 通过；tests/conftest.py 默认 COMMITTEE_GATE=0
> 防测试烧真实配额（曾实锤一次）。首次运行会自动 ALTER 提案表加 committee_json
> 列（已对生产库执行）。

> 需求（用户 09-06）：把 LLM 委员会深度嵌入项目，**所有结论都过一遍委员会评审**。
> 前置实证：09-06 会战证明委员会当「反方质证」有效——40+ 抽审零编造、拦下 rr 事后
> 分层提案、知识库注入防重蹈；但其 floor 几何失误被回测实证逮住=评审本身仍是假设。
> 本文=架构设计与接线方案，实施待人审拍板三个决策点（§6）。

## 0. 先校验需求：三个必须说破的边界

1. **「所有结论」必须分级**。确定性结论（回测数字、autopromote 七门、口径校验）
   过 LLM 是纯浪费+引入噪声——它们的问题域是代码审计，不是语义评审。真正需要
   质证的是**语义性结论**：归因观点、参数假设、提案机制叙述、研究文档论断。
2. **LLM 审 LLM = 同源偏差**。委员会提供的是「视角多样性 + 知识库强制 + 工具
   核数」，**不是独立性**。独立裁判仍然是回测闸与人审。因此评审采用**附签制**
   （annotation），唯一 fail-closed 点在 publish（ESCALATE→NEEDS_HUMAN）。
3. **fail-open 是生存底线**。额度耗尽/端点挂了不能让 18:05/18:30/18:45 停摆
   （server lifespan 收编教训：单点死=一切死）。评审不可用→结论照常流转，
   携带 `UNAVAILABLE` 标记。

防锚定元规则：评审输出模板强制附「本评审仍为假设，部署裁决权在回测闸与人审」；
**委员会自身的结论不过委员会**（防无限递归），走既有闸。

## 1. 三档评审制

| 档 | 成本 | 构成 | 适用 |
|---|---|---|---|
| **Tier 0 谱面闸** | 0 LLM，纯代码 | 参数形状/关键词 vs 否决知识库（REJECTED 提案+七波否决）冲突扫描；口径校验 | 每条提案创建时免费内嵌（create_proposal 内） |
| **Tier B 轻量质证** | 5-8 calls / 3-5 min | 单「质证官」带工具：①抽核结论引用数字（工具直查）②知识库冲突 ③证据等级标注（实证/推断/猜想）④verdict | 日度 loser_review 产物、explore_loop 最优格、digest 生成的提案 |
| **Tier A scoped 委员会** | 8-12 calls / ~10 min | 紧凑辩论：空头质证人（攻击结论）→多头辩护人（最强辩护）→风控官（过拟合/口径）→PM 裁决，专审单一结论 | 提案 APPROVED→publish DRAFT 之间、换代就绪包、重要研究文档定稿 |
| 完整版 | 33 calls / 29 min | 09-06 会战原版（分析师×3→四轮辩论→三辩→PM） | 季度级/会战级深审（手动触发） |

统一输出 schema（reviews 落 `logs/committee_reviews/{kind}_{id}_{ts}.json`，
含完整 transcript）：
```json
{"verdict": "PASS|NOTES|ESCALATE|UNAVAILABLE",
 "fact_checks": [{"claim": "…", "tool": "…", "status": "命中|漂移|不可验"}],
 "kb_conflicts": ["…"], "evidence_grade": "实证|推断|猜想",
 "notes": "…", "llm_calls": 6,
 "disclaimer": "本评审仍为假设，部署裁决权在回测闸与人审"}
```

## 2. 接线点（每处都是小钩子，fail-open + 超时护栏）

| 结论 | 位置 | 档位 | 时机 | 语义 |
|---|---|---|---|---|
| 亏损归因 | `ops/loser_review.py` 尾 | B 批量（全日一份） | 18:05 写盘后 | JSON 加 `committee_review` 字段；explore_loop 自动消费（归因意见先质证再进网格） |
| 探索最优格 | `research/explore_loop.py` | B | create_proposal 前 | 提案 note 附 review id；ESCALATE→跳过该格（fail-closed 于「进提案库」这步可接受：网格还有别的格） |
| 提案发布 | `research/proposals.py publish_proposal` | A scoped | APPROVED→DRAFT 之间 | **唯一全局 fail-closed 点**：ESCALATE→NEEDS_HUMAN 人审；UNAVAILABLE→照常 publish 带标记 |
| 换代就绪包 | `promote_watch` 出包 | A scoped | 包内附委员会意见书 | 只附签（换代本就人审一键） |
| digest | `research/digest.py` | 展示层 | 次日摘要加「评审台」段（昨日各评审 verdict 一览） | 人读 |

import 方向防环：`committee.review` 不 import `proposals`（KB 直接 sqlite 读
research_proposals.db），`proposals.publish_proposal` 反向调用 committee.review。

## 3. 知识库自动保鲜（委员会越用越准的关键）

`research/committee/kb.py` = 三源合并加载：
1. **策展层** `research/committee/kb/*.md`：七波否决潮、regime 实证、R10 终审、
   幸存者偏差红线、口径地图、time_stop 史（从 strat_tools._KB 迁出）；
   **09-06 会战新增**：above=牛市代理（2022-23 归零+98pp）、rr 列=事后 R 倍数
   （strategy.py:188）、trailing 几何（收紧全程=30 天临界/理论节省≤25%）、
   floor day20 前不绑定、跳空恶化趋势（2026 stop 均 -11.95% 六年最差）。
2. **自动层**：research_proposals.db REJECTED 条目（verification reason+note）
   ——复用 T3.3 学习回路的渲染逻辑，让每条被否提案自动成为委员会的先验。
3. **监控层**（后续）：above×年滚动期望等前向指标读数。

## 4. 成本与配额

- 日常：Tier B ×2/日 ≈ 10-16 calls；Tier A ≈ 1-2 次/周 ≈ 20 calls/周
  → **周增 ≤100 calls**（Coding Plan 配额；本周会战全程才 38）。
- 模型档位：B/A 默认 glm-5.3 reasoning=low（工具回路已验）；glm-4.7-air 级
  降成本需先过一次 tools probe（同 diag/glm_tools_probe.py 方法，1 次 probe 钱）。
- 台账：沿用 GlmToolsSession.call_count，评审 json 记 llm_calls，digest 评审台
  汇总周消耗。

## 5. 诚实的预期管理：委员会能抓什么、抓不了什么

- **能抓**（有战例）：事后变量混入特征（rr 分层）、与已否决方向冲突的提案、
  无工具支撑的数字断言、证据等级混乱（猜想当实证呈报）、机制叙述的几何错误
  （部分——floor 方向错是回测抓的，rr 是委员会抓的）。
- **抓不了**：代码 bug（verify 口径 13 连拒冤案是修代码救的）、数据 bug（湖
  amount、nav_history 双缺口）、过拟合（B3 outer 翻转靠 holdout 设计拦）、
  端到端延迟类运维问题。
- 结论：委员会嵌入的期望收益=**结论质量的下限抬升**（错误早暴露、否决先验
  强制在场、证据等级纪律化），不是上限突破。

## 6. 三个决策点（待人审）

1. **fail-closed 范围**：推荐「仅 publish 一点 ESCALATE→NEEDS_HUMAN，其余全
   附签 fail-open」；激进版=explore 最优格也 ESCALATE 停（推荐），保守版=全附签。
2. **接入顺序**：推荐先挂三处（loser_review B / explore B / publish A）跑两周
   观察误报率与配额实耗，再扩 promote_watch 与文档定稿审；一次性全量接入的
   风险=评审噪声淹没信号+配额失控。
3. **提案库表结构**：research_proposal 加 `committee_json` 列（ALTER，向后兼容，
   与 verification_json 对称）vs 只存文件系统由 note 引用——推荐加列（正式资产）。

## 7. 实施清单（拍板后 1 个会话内）

1. `research/committee/kb.py` + `kb/*.md` 迁移与三源加载（含 09-06 新知识入库）
2. `research/committee/review.py`：`review_conclusion(kind, doc, tier, subject)`
   统一服务（Tier 0 代码闸 + B 质证官 + A scoped 委员会，verdict schema，超时
   护栏，transcript 落盘）
3. 三处接线钩子 + proposals 表 ALTER
4. digest「评审台」段
5. 测试：mock GlmToolsSession 的 B/A 流程测试 + Tier 0 冲突扫描纯代码测试
   + fail-open（端点不可用→UNAVAILABLE）测试
6. 观察窗两周：误报率/ESCALATE 命中率/配额实耗 → 复盘决定全量推广
