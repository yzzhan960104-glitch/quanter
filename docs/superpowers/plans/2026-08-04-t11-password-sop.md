# T11: 计划任务 Password 化 — 用户手动 SOP

> 本 task 是 plan `2026-08-04-data-observation-remediation.md` 的最后一个 task,需用户在 Windows GUI 手动完成(subagent 无法代做,需账户密码)。分支 `fix/c9-data-obs-remediation` 代码部分已全部完成并过 final review。

## 为什么需要

`QuanterServer`(uvicorn 服务)与 `quanter_sync_incremental`(每日 18:00 增量同步)当前是 **Interactive only**(仅用户登录会话时运行)。改成 **Password 登录模式**后,任务在后台/session 0 运行,**开机自启 + 18:00 定时触发不依赖用户登录**——这是数据观测层长期稳定的前置(schtasks ONSTART / 后台执行)。

## 前置条件

- **yzzhan 账户密码**(PIN 不行——schtasks 后台运行需密码鉴权)
- Windows 桌面会话(本机)
- 两个任务已存在(T1 已确认 `quanter_sync_incremental` 路径修复;`QuanterServer` 已注册)

## 操作步骤

### 1. 打开任务计划程序

`Win+R` → 输入 `taskschd.msc` → 回车

### 2. 改 QuanterServer

1. 在任务列表找到 `QuanterServer`
2. 右键 → **属性**
3. **常规** 标签页:
   - 勾选 **"不管用户是否登录都要运行"**(Run whether user is logged on or not)
   - 勾选 **"不存储密码"** 不要勾(任务需要密码后台运行)
   - 点确定 → 弹窗输 **yzzhan 账户密码** → 确认

### 3. 改 quanter_sync_incremental

同步骤 2,对 `quanter_sync_incremental` 操作。

### 4. 验证

PowerShell 跑:

```powershell
schtasks /Query /TN QuanterServer /XML | Select-String "<LogonType>"
schtasks /Query /TN quanter_sync_incremental /XML | Select-String "<LogonType>"
```

**期望**:两处都输出 `<LogonType>Password</LogonType>`(不再是 `Interactive` 或 `S4U`)。

### 5. 验证后台运行(可选但推荐)

- **注销/锁屏**后,观察:
  - 重启电脑 → `QuanterServer` 应自启(端口 8000 可访问,前端能打开)
  - 到 18:00(或手动 `schtasks /Run /TN quanter_sync_incremental`)→ 同步应触发,`data_lake/.syncing/sync_incremental.stdout.log` 出新 `=== 增量同步 START` 行
- 若任务"上次运行结果"非 0,查 `历史`(任务计划程序 → 对应任务 → 历史标签)看错误。

## 回滚

若 Password 模式出问题(如密码过期/账户锁定致任务失败):

1. 任务计划程序 → 属性 → 常规 → 改回 **"只在用户登录时运行"**(Run only when user is logged on)
2. 临时方案:保持登录态运行(退化到 Interactive only,数据同步仍工作,只是不自启)。

## 与 plan 其它 task 的关系

| Task | 状态 | 备注 |
|---|---|---|
| T1 schtasks 路径 | ✅ subagent 完成 | Desktop→F:\quanter 路径 |
| T4 补跑同步 | ✅ subagent 完成 | OK 23/FAIL 0,mtime 推进 08-04 |
| T5 清哨兵 | ✅ subagent 完成 | 26 个 .failed 清除 |
| T11 Password 化 | ⏸ **本 SOP,用户手动** | 完成后数据观测层长期自启闭环 |

T11 完成后,数据观测层 A 路全部闭环:计划任务自启 → 增量同步 → 哨兵状态机 → 前端展示,不再依赖用户登录。

---

*生成于 2026-08-04,fix/c9-data-obs-remediation 分支 final review 后。*
