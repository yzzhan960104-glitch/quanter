---
title: T15 代码层防复发——tushare 调用屏蔽代理 env
type: design/spec
status: draft
date: 2026-08-11
related: [plans/wayfinder/T15.md, docs/architecture/06-tech-debt.md]
---

# T15 代码层防复发：tushare 调用屏蔽代理 env

## 背景

T15（tushare 代理 env 根治）已于 2026-08-08 **closed**（决策 B：删除 Windows User 环境变量 `ALL_PROXY`）。2026-08-11 核实：

| 核实项 | 结果 |
|---|---|
| 当前进程 `ALL_PROXY`/`HTTPS_PROXY`/`HTTP_PROXY` | 全 `[]`（干净） |
| 注册表 `HKCU\Environment` | 「系统找不到指定的注册表项」（已删） |
| `scripts/*.bat` | grep 无 `ALL_PROXY` |

**环境层已根治**。T13-blueprint（08-10）写「T15 未做」是信息滞后。

### 残留风险（T15.md Resolution :50）

代理软件（v2ray/clash）重启且开「系统代理」会**重写 `ALL_PROXY`** → quanter 进程继承 → tushare（底层 requests，`trust_env=True` 默认读代理 env）走失效代理 → `trade_cal`/`daily`/`adj_factor` 全失败（T14 事故重演）。

T15.md 原决策 C（代码层绕过）当时最不偏好（「演进优先不改代码」），但作为**防复发兜底**有独立价值——代理软件复发是真实风险（用户重装/重启代理软件即可触发）。本次补上决策 C，与环境层根治（决策 B）形成双层防线。

## 目标

tushare 调用**不继承系统代理 env**：即使代理软件复发写 `ALL_PROXY`/`HTTP_PROXY`/`HTTPS_PROXY`，tushare 仍直连 `api.waditu.com`，不受失效代理影响。

## 方案

`data/_tushare_compat.py` 模块加载时，把 tushare 域名加入 `NO_PROXY` env：

- **域名**：`api.waditu.com`（tushare pro 主域）、`api.tushare.pro`（备用）
- **机制**：requests 默认 `trust_env=True`，读 `NO_PROXY`，命中域名则**跳过代理直连**。tushare 的 `pro_api`（DataApi）底层用 requests，故 `NO_PROXY` 对其生效
- **幂等**：仅在域名未存在于 `NO_PROXY` 时追加（不覆盖用户已配的 `NO_PROXY`）

```python
import os
# T15 代码层防复发：tushare 域名加入 NO_PROXY，防代理软件（v2ray/clash）复发
# 重写 ALL_PROXY 致 tushare 走失效代理。requests 默认 trust_env=True 读 NO_PROXY，
# 命中则跳过代理直连。T15.md 决策 B 已删注册表 ALL_PROXY（环境层），本层为代码兜底。
_TUSHARE_HOSTS = ("api.waditu.com", "api.tushare.pro")
_no_proxy = os.environ.get("NO_PROXY", "")
_missing = [h for h in _TUSHARE_HOSTS if h not in _no_proxy.split(",")]
if _missing:
    os.environ["NO_PROXY"] = ",".join(filter(None, [_no_proxy] + _missing))
```

## 接入点

`data/_tushare_compat.py` 模块顶部（`import tushare` 之前）。本模块被 `calendar` / `tushare_sync` / `TushareDataFetcher` / 各 sync 脚本多处 import，**加载即生效**（首次 import 时设置，进程内持久）。

`get_pro` / `source_name` / `ts_module` / `ensure_token` 四函数签名**保持不变**（_tushare_compat 铁律）。

## 降级 / 风险

- **不覆盖用户 NO_PROXY**：仅追加缺失的 tushare 域名，保留用户既有配置
- **不影响其他服务**：仅 tushare 域名走 NO_PROXY；其他服务（discovery/backtest 访问境外数据）仍可用代理
- **env 变更副作用**：模块加载改 `os.environ`，进程内生效，不污染系统（进程隔离）
- **trust_env 仍 True**：不动 requests session 的 trust_env，仅靠 NO_PROXY 白名单——最小侵入

## 验收

1. **单测**：测试内设 `ALL_PROXY=socks5://127.0.0.1:5001`（失效）→ `import data._tushare_compat` → 断言 `os.environ["NO_PROXY"]` 含 `api.waditu.com`（tushare 域名被白名单，不走代理）
2. **幂等**：重复 import / 已配 NO_PROXY 时不重复追加、不覆盖
3. **回归**：现有 tushare 单测（test_tushare_sync / test_resilience_quota 等）全绿
