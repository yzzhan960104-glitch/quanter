# Task 6 报告：CR-6 补采回路复活（部分落盘 + 限频降速）+ 实弹验证

**分支** `debt/full-wave-0815` · **Commit** `4b636756` · **日期** 2026-08-15 · **状态** DONE（实弹结论与简报预期分叉，见「关键发现」）

---

## 1. 交付内容

### 1.1 实现（`data/tools/repair_gaps.py`）

| 改动 | 位置 | 物理意图 |
|---|---|---|
| 单日拉取异常 → 部分落盘 | `repair_gaps` 拉取循环：`_fetch_paged` 包 try/except，异常 `logger.warning` + `break`，已拉日继续走既有 merge 落盘路径 | 与既有超时分支（:134-144 原行号）同语义：**部分补采 > 完全不补**。旧实现单日「频率超限」直接 raise，把已拉 230/350 日全部丢弃白拉（repair_auto.log 实锤 25 连败熔断循环根因） |
| `REPAIR_DAY_SLEEP` env（默认 1.5s） | 模块常量 + 循环内日间隔 `time.sleep`（首日不睡；sleep 计入总超时预算） | Tushare 服务端 500/min 计数窗口与客户端令牌桶错位：令牌桶允许「合规」瞬时突发，服务端按自己的滚动窗口掐表。日间隔把按日分组的分页请求摊开，降速换通过率 |
| `partial` 标记 | `_tag_partial(df)`：返回 df 的 `attrs["partial"]=True`（为何 attrs 不改签名：返回值已被 main/既有单测/外部 mock 按「单 df」消费，改 tuple 破坏兼容；main 紧随读取无 attrs 丢失面）；日志/stdout 带 ASCII `partial`（多道转码后仍可 grep） | 透传 main 决定熔断计数方向：`record_repair_result(success=not partial)` —— 数据已落盘不丢，但「本轮被限频打断」是失败信号（连续 3 次中断才熔断退避 6h，取代旧「单次中断即整轮 raise 雪崩」） |

三个 return 路径（`raw_frames` 空 / `new.empty` 筛后空 / 正常 `combined`）均带 partial 透传，含「第 1 日即被打断」边界。

### 1.2 测试（TDD 红 → 绿）

- **红**：`tests/test_repair_gaps.py::test_partial_persist_on_fetch_error` —— gap 缺 09-03/04/05 三日，第 3 日 `_fetch_paged` 抛 `Exception("抱歉，您访问接口(daily)频率超限(500次/分钟)")`。旧实现 Exception 一路 raise 出 main（白拉复现）→ 红。
- **绿**：断言 ①`rc==0` 且 stdout 含 `partial` 与 `+2`；②已拉 09-03/09-04 落湖、被打断日 09-05 不在、原有日不丢（4 行）；③sidecar `fail_count==1`、`open_until==0`（单次中断只计数不熔断）。
- mock 手法：`data.tools.scan_integrity.scan`（main 局部 import，patch 源模块属性）、`data._tushare_compat.get_pro`、`rg._fetch_paged`、`rg.REPAIR_DAY_SLEEP=0`。

**测试面**：`tests/test_repair_gaps.py` **7/7 绿**（既有 6 + 新 1）；相邻面 `tests/test_integrity.py` 32/32、`tests/test_sync_daily_incremental.py` 2/2 无误伤。

---

## 2. 实弹验证（2026-08-15 13:45–13:57，周六，无 pipeline 写窗口冲突）

**命令**（同 pipeline 触发式，进程内 env 不落盘）：
```
REPAIR_RECOVERY_HOURS=0 PYTHONUTF8=1 E:/quanter/.venv310/Scripts/python.exe \
  -m data.tools.repair_gaps --auto --lake-dir data_lake >> logs/repair_auto.log 2>&1
```

**前置处理**：熔断 sidecar 当时 `fail_count=25, open_until` 尚余 ~4.07h。`REPAIR_RECOVERY_HOURS=0` 解不开**存量** open_until（env 只影响新写入值，`is_repair_breaker_open` 只比较已存值）——把 sidecar 挪至 `logs/repair_breaker.bak-20260815.json` 留证（logs/ 与 data_lake/* 均 git-ignore，不入库；25 连败系旧 bug 产物，修复后清零重启语义合理）。`REPAIR_RECOVERY_HOURS=0` 保留：本轮若 partial 记 fail_count=1 但不把下一调度轮锁 6h。

### 2.1 实弹 log 尾部摘录（logs/repair_auto.log :724-761）

```
2026-08-15 13:45:27,922 WARNING repair_gaps 配额截断：16371 → 50 段（剩余下次补采）
2026-08-15 13:45:46,944 INFO repair_gaps 进度：10/350 日已拉（raw 10 adj 10 帧）
2026-08-15 13:46:07,685 INFO repair_gaps 进度：20/350 日已拉（raw 20 adj 20 帧）
...（每 10 日稳定 ~20.7s，含 1.5s/日降速 sleep；全程零 WARNING 零限频）...
2026-08-15 13:53:23,556 INFO repair_gaps 进度：230/350 日已拉（raw 230 adj 230 帧）  ← 旧实现 25 连轮的死亡点，本轮无中断越过
2026-08-15 13:57:15,338 INFO repair_gaps 进度：340/350 日已拉（raw 340 adj 340 帧）
2026-08-15 13:57:38,305 WARNING 补采段筛 symbol/date 后为空（gap 标的该日 Tushare 无数据？）
待补采：16371 段漏采（418 标的）
补采完成：a_shares_daily 10320400 → 10320400 行（+0）
EXIT_CODE=0
```

### 2.2 实弹结论

- **回路复活实证**：350/350 漏采日全部拉通，**零限频中断**（此前 25 连轮全部中途死于 `Exception: 频率超限(500次/分钟)`，最多拉到 ~230/350）。降速 `REPAIR_DAY_SLEEP=1.5s` 生效（~2.07s/日稳定节拍，请求速率远低于 500/min）。
- **健康面**：`EXIT_CODE=0`；湖 10,320,400 行守卫+原子重写，前后 index 本地 diff **0 增 0 删**、git 无 M（byte-identical，零数据漂移）；熔断 sidecar 正确复位 `{"fail_count": 0, "open_until": 0}`。
- **补成段数：0 段（+0 行）**——与简报预期（部分落盘净增长）分叉，原因见下。

---

## 3. 关键发现：首批 50 段配额被「停牌真缺口」占据（净收敛的新阻塞点）

`+0` 不是本修复的缺陷（过滤/merge 逻辑未动，单测证明有数据时正常落湖）：350 日 raw/adj 全部拉到**全市场**数据，但筛 `gap_symbols × missing_dates` 后为空——首批 50 段配额的标的在这些日期 **Tushare 本身无 daily 行**。

本地取证（纯湖内分析，工作日历近似复现 scan 段序）：

- 首批 50 段涉及 36 标的，洞高度集中于 **2016-2018 多月长洞**（A股停牌潮年代）：如 `000006.SZ` 2017-09-11~2018-03-07（177 天）、`000007.SZ` 两段 ~180 天。
- **铁证**：`000029.SZ`（深深房A）湖内洞 `2016-09-14 ~ 2020-11-06`（1514 天），与其因恒大重组停牌四年、2020-11 复牌的公开事实完全吻合——这是真停牌，非漏采。
- 根因链：本地 `suspend_d.parquet` ground-truth 未覆盖这些停牌区间 → `scan_integrity` 误判 `suspend_justified=False` → 50 段配额被**永不可补**的段占用 → 每轮 `--auto` 拉完 +0、记 success、下次重试同样 50 段 → **16371 段永不净收敛**。

**跟进项建议**（超出 CR-6 范围，建议入波次债务清单）：
1. scan 侧：`suspend_justified` 判定引入第二证据源（如 `pro.suspend_d` 拉全 / namechange ST 标记 / 「该 (symbol,date) 全市场有数据而此标的无」启发式）；
2. repair 侧：对「拉到该日数据但段内标的零行」的段打 unfillable 标记（sidecar），配额自动跳过，避免死占。

---

## 4. 与任务简报的偏差（及理由）

| 简报 | 实际 | 理由 |
|---|---|---|
| Commit 标题括号「16371 段开始净收敛」 | 改为「实弹 350/350 日零中断」 | 本轮 +0 行，净收敛说法与实弹证据不符（诚实优先；+0 根因与修复无关，已在正文/报告说明） |
| 「用 REPAIR_RECOVERY_HOURS=0 跑一轮」即可绕过熔断 | 需另挪 sidecar | env=0 只影响**新写入**的 open_until，解不开存量 open_until（还剩 ~4h）；挪 sidecar 是该指令意图的机械等价实现，证据保留 logs/ |
| 预期「部分落盘/净增长」或「限频打断即成功」 | 实际第三种：全拉通 + 筛后空 +0 | 部分落盘路径由单测覆盖验证；实弹被停牌真缺截胡（见 §3） |

## 5. Self-Review

- 实现-测试-实弹三面互证：单测锁定异常语义（落盘/计数/exit 0），实弹锁定节拍与零中断；湖零漂移由 index diff + git 双证。
- 遗留：`REPAIR_DAY_SLEEP` 对全市场大页日期（近代日期 ~26 请求/日）的降速余量未实弹验证（本轮 350 日均为早年小页日期）；周一 18:00 pipeline 首轮自动 --auto 将是下一个真实观测点（若部分落盘，log 会出现 `partial` 标记）。
- Wave A 门（run_checks + 全量 pytest）属波次级验收，未在本任务内执行；本任务测试面（repair_gaps/integrity/sync_daily_incremental 共 41 测）全绿。

---

## 6. 评审修复（Task 6 Review，2026-08-15）

评审 1 Important + 3 Minor（#4 不动作），全部处置：

### 6.1 改动

| # | 级别 | 改动 | 位置 |
|---|---|---|---|
| 1 | Important | **daily/adj 原子入列**：`d`、`a` 两个 `_fetch_paged` 都成功后才一起 append（原实现 daily 成功即入列、adj 随后抛限频 → 该日 adj_factor=NaN → 前复权价格全 NaN 落湖，P1-A 红线同案；且 scan 按 index 在场判「已补」永不复查）。被打断日零贡献 | `data/tools/repair_gaps.py` 拉取循环 try 块 |
| 2 | Minor | 拉取异常 `logger.warning` 加 `exc_info=True`——非限频类 bug（网络栈/解析 KeyError）只落 `str(exc)` 会丢栈 | 同上 except 块 |
| 3 | Minor | 超时分支注释写明观测盲点：超时不置 partial → main 记 success=True，超时打断不进熔断失败计数（**语义不改**，T13-B 既有行为，brief 锚「同语义」） | 超时 break 分支上方 |
| 4 | Minor | `_tag_partial` attrs 副作用——docstring 已自认，不动作 | — |

### 6.2 新测试：`test_partial_persist_on_adj_error_atomic_day`

与既有 `test_partial_persist_on_fetch_error`（daily 抛异常形态）镜像的 **adj 抛异常形态**：gap 缺 09-03/04/05 三日，第 3 日 daily 成功、adj_factor 抛 `Exception("抱歉，您访问接口(adj_factor)频率超限(500次/分钟)")`。断言：①exit 0 + stdout 带 `partial` 与 `+2`（非 +3）；②**09-05 不在湖中**（旧实现该日以 adj_factor=NaN → 前复权价格全 NaN 落湖）、已拉 09-03/04 落湖、原 4 行；③熔断 `fail_count==1`。

**红验证**（证明用例真锁 bug）：临时回退为旧「daily 成功即 append」形态跑该用例 → **FAILED**（+3 行、09-05 NaN 落湖被断言抓住）；恢复修复后绿。

### 6.3 命令与输出

```
$ PYTHONUTF8=1 ./.venv310/Scripts/python.exe -m pytest tests/test_repair_gaps.py -v
tests/test_repair_gaps.py ........                                    [100%]
============================== 8 passed in 2.17s ==============================

# 相邻面无误伤：
$ PYTHONUTF8=1 ./.venv310/Scripts/python.exe -m pytest tests/test_repair_gaps.py tests/test_integrity.py tests/test_sync_daily_incremental.py -q
============================= 42 passed in 2.34s =============================

# 红验证（旧形态下新用例）：
tests/test_repair_gaps.py::test_partial_persist_on_adj_error_atomic_day FAILED [100%]
=========================== 1 failed in 0.82s ===========================

# 回退-恢复自证：git diff 落 /tmp/t6_fix.patch → 回退跑红 → git checkout + git apply 恢复 → 全套重跑绿
```
