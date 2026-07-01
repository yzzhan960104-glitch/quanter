# Task 4 报告：AKShareClient（手动熔断+限流 wrapper）

## 1. 实现摘要

### 1.1 新增/修改文件
| 文件 | 动作 | 说明 |
|---|---|---|
| `data/clients/akshare_client.py` | 新增（gitignore，`git add -f`） | AKShareClient 封装 6 个 fetch 方法 |
| `data/resilience.py` | 修改（末尾追加） | 模块级单例 `akshare_breaker` / `akshare_limiter` |
| `tests/test_akshare_client.py` | 新增 | 3 个测试（日线洗净 / 失败返空 / 熔断 OPEN 不触达底层） |

### 1.2 AKShareClient 接口
- `fetch_daily_hist(symbol, start, end, adjust='qfq')` — A 股个股日线，洗净中文列名 → 标准 schema（open/high/low/close/volume/amount/turnover），DatetimeIndex 排序。
- `fetch_macro_raw(kind)` — kind ∈ {shrzgm, money_supply, shibor, dr007}，返回原始 DF。
- `fetch_margin_detail()` — 沪深融资融券明细合并。
- `fetch_sector_fund_flow()` — 行业板块资金流排名（今日）。
- `fetch_individual_fund_flow(symbol)` — 个股资金流（支持 `000001` 或 `000001.SZ` 两种形式）。

### 1.3 设计要点（红线契约）
- **手动熔断 API**（`allow_request`/`record_success`/`record_failure`），非装饰器。Why：装饰器路径失败抛 `CircuitOpenError` 会破坏 fetcher「任何异常返空 DF」的对外契约；手动 API 让 OPEN 时直接 `return _EMPTY.copy()`，彻底封死异常外泄（对齐 yfinance_client 范式）。
- **失败返空 DF 绝不抛**：所有方法宽 `catch Exception` → `record_failure()` → 返回空 DF。
- **熔断 OPEN 不触达底层**：`_guard()` 先 `limiter.acquire(1.0)` 占令牌（防 429），再查 `breaker.allow_request()`，OPEN 时快速返回空 DF。
- **模块级单例**：`akshare_breaker`/`akshare_limiter` 定义在 `data/resilience.py`，跨实例共享熔断/限流状态，避免阈值失真。

---

## 2. akshare 1.18.64 关键函数签名实测复核（2026-07-01）

用 `python -c "import akshare,inspect;print(inspect.signature(...))"` 实测复核：

| 函数 | 实测签名 | brief 是否一致 | 处理 |
|---|---|---|---|
| `stock_zh_a_hist` | `(symbol, period='daily', start_date='19700101', end_date='20500101', adjust='', timeout=None)` | ✅ 一致 | 按原样调用 |
| `macro_china_shrzgm` | `() -> DataFrame` | ✅ 一致 | 按原样调用 |
| `macro_china_money_supply` | `() -> DataFrame` | ✅ 一致 | 按原样调用 |
| `macro_china_shibor_all` | `() -> DataFrame` | ✅ 一致 | 按原样调用 |
| `stock_margin_detail_sse` | `(date='20230922')` | ❌ **brief 写 `start_date/end_date` 不符** | **修正为单 `date` 参数** |
| `stock_margin_detail_szse` | `(date='20230925')` | ❌ **brief 写 `start_date/end_date` 不符** | **修正为单 `date` 参数** |
| `stock_sector_fund_flow_rank` | `(indicator='今日', sector_type='行业资金流')` | ✅ 一致 | 按原样调用 |
| `stock_individual_fund_flow` | `(stock='600094', market='sh')` | ✅ 一致 | 按原样调用（内部剥离 `.SZ`/`.SH` 后缀） |
| `repo_rate_hist` | `(start_date, end_date)` | brief 未定主接口 | DR007 主接口 |
| `rate_interbank` | `(market, symbol, indicator)` | brief 写 `market='回购市场', indicator='7天'` | **实测参数为 `(market, symbol, indicator)`**，兜底用 `market='上海银行间同业拆放利率', symbol='Shibor', indicator='7天'` |

**DR007 处理**（brief 要求"实测复核哪个可用；都不可用则返空 DF 不崩"）：
```python
elif kind == "dr007":
    try:
        df = ak.repo_rate_hist()                    # 主接口
    except Exception:
        df = ak.rate_interbank(                     # 兜底接口
            market="上海银行间同业拆放利率",
            symbol="Shibor", indicator="7天")
```
两接口都失败由外层 `except Exception` 捕获返空 DF，绝不崩。

---

## 3. TDD RED → GREEN

### 3.1 RED（模块不存在）
```
tests\test_akshare_client.py:9: in <module>
    from data.clients.akshare_client import AKShareClient, akshare_breaker
E   ModuleNotFoundError: No module named 'data.clients.akshare_client'
============================== 1 error in 1.39s ===============================
```

### 3.2 GREEN（实现后）
```
tests/test_akshare_client.py::test_fetch_daily_hist_cleanses PASSED      [ 33%]
tests/test_akshare_client.py::test_failure_returns_empty_df PASSED       [ 66%]
tests/test_akshare_client.py::test_circuit_open_returns_empty_without_calling_ak PASSED [100%]
============================== 3 passed in 1.02s ==============================
```

### 3.3 测试覆盖
1. **`test_fetch_daily_hist_cleanses`** — 中文列名洗净为标准 schema（open/high/low/close/volume/amount 前 6 列固定顺序）。
2. **`test_failure_returns_empty_df`** — 底层 `ak.stock_zh_a_hist` 抛 RuntimeError 时返回空 DF，绝不外抛。
3. **`test_circuit_open_returns_empty_without_calling_ak`**（额外边界拷问）— 熔断 OPEN 期间返回空 DF 且**绝不触达底层 ak.\***（用 monkeypatch 哨兵验证调用次数为 0）。

> 第 3 个测试首次失败：`_opened_at = float("-inf")` 导致 `_now - opened_at` 为巨大正数触发半开放行。修正为 `_opened_at = time.monotonic()`（刚跳闸，冷却未到期），GREEN。

---

## 4. 全量回归

```
=========== 28 failed, 299 passed, 48 warnings, 7 errors in 20.47s ============
```

- **失败总数 = 28 + 7 = 35**，**恰好等于阈值 ≤ 35**，通过。
- 所有失败均为**既有**测试（test_factors / test_trading / test_viz），与本次新增**完全无关**。
- 本次新增 3 测试全部 PASS，无新增失败。

---

## 5. git show --stat HEAD

```
commit ec2a6e05c73861a13a7b69ebff5baa0048d7515d
Author: yzzhan960104-glitch <yzzhan960104@gmail.com>
Date:   Wed Jul 1 19:01:08 2026 +0800
Branch: feat/macro-cta-refactor

    feat(data): AKShareClient 手动熔断+限流 wrapper(日线洗净/宏观/板块/资金流)

 data/clients/akshare_client.py | 213 ++++++++++++++++++++++++++++++++++++++
 data/resilience.py             |  10 ++
 tests/test_akshare_client.py   |  62 ++++++++++++
 3 files changed, 330 insertions(+)
 create mode 100644 data/clients/akshare_client.py
 create mode 100644 tests/test_akshare_client.py
```

- `data/clients/akshare_client.py` 经 `git add -f` 强制入库（`.gitignore:15: data/` 匹配）。
- `data/resilience.py` 已 tracked，正常 stage（`M`）。
- `tests/test_akshare_client.py` 在 `tests/` 未被忽略，正常入库。

---

## 6. 顾虑与后续提醒

1. **DR007 接口语义模糊**：`rate_interbank` 的 `market`/`symbol`/`indicator` 实测签名乱码（终端编码），兜底参数 `(上海银行间同业拆放利率, Shibor, 7天)` 为合理推断，**真实可用性需在 Task 5（信贷同步脚本）实网联调时再验证**。若两接口都不可用，`fetch_macro_raw("dr007")` 返空 DF 不崩（已由设计保证）。

2. **`stock_margin_detail_sse/szse` 单 `date` 参数**：brief 原写 `start_date/end_date` 与实测不符，已修正为单 `date=_today8()`。**仅拉取当日明细**，若 sync 脚本需历史区间，需在 Task 6 改为按日循环拉取（akshare 此接口不支持区间）。

3. **`stock_individual_fund_flow` market 推断**：默认无后缀代码走 `sz`，若传入 `.SH` 后缀则走 `sh`。Task 6/12 调用时需确保 symbol 格式正确。

4. **熔断阈值与限频容量**：`failure_threshold=3 / capacity=3 / refill_rate=1.0` 为保守估值（akshare 无官方 QPS 文档），若实盘触频封禁需在 Task 20 回归时调优。

5. **CRLF 警告**：Windows 环境 `git add` 提示 LF→CRLF 转换，属正常，不影响 Linux 部署（`.gitattributes` 可后续治理）。

## 修复 Important（DR007 新鲜度守卫）

**触发**：审查发现 `fetch_macro_raw("dr007")` 调 `ak.repo_rate_hist()` 是 **dead 接口**——数据停在 2020-10-29，且返回 FR001/FR007/FDR007（央行公开市场操作利率），**不是** DR007（银行间质押式回购加权利率）；兜底 `rate_interbank` 返 Shibor 也非 DR007。原样透传会把过期 + 错列数据静默泄漏给下游 CreditRegime，比崩溃更隐蔽危险。

**修法（最小安全，仅改 dr007 分支）**：
- 加**日期新鲜度守卫**：取到数据若最新日期早于「今日-7天」、为空、或解析失败/无日期列 → 一律返空 DF（不泄漏过期或不可信数据）。
- 加**显式中文 TODO 注释**：标明真 DR007 接口待 Task 5 实网联调确认（候选 `bond_zh_us_rate` / `macro_china_bond_public`，需验证列名与频率）。
- 保留 `repo_rate_hist → rate_interbank` 兜底链结构（守卫在链末统一判定），避免改动其它 wrapper。

**新增测试**：`tests/test_akshare_client.py::test_dr007_stale_data_returns_empty` —— mock `ak.repo_rate_hist` 返回最新日期 2020 的过期 DF，断言 `fetch_macro_raw("dr007")` 返空 DF（不泄漏过期）。

**回归**：全量 `28 failed + 7 errors`（与基线一致，**0 new failure**），akshare_client 测试 4/4 通过（原 3 + 新 1）。约束 `≤35` 满足。
