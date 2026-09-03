# 提案同意后的换代自动化（promote pipeline · 2026-09-03）

> 用户需求：融入研究提案，提案同意后的下一步自动化。
> 探索实证：最后三公里是断头路——ACTIVE 变更后 export/build/部署全手动
> 零钩子（params_snapshot sources.champion 停 r68 而 ACTIVE 已 r610_b3=
> 三层分叉）；promote 播报「即刻生效」对掘金腿是假话（腿不读 DB）。

## 闭环全景（提案同意后）

```
提案 APPROVED →（自动）publish_proposal → experiment DRAFT
DRAFT → promote ACTIVE：人审 CLI（08-25 全停裁决封死自动写库）
ACTIVE → 18:50 watch【新】：
   三方分叉检测（resolve_champion vs params_snapshot.sources.experiment
   .champion_experiment_id vs 双腿部署 §0 PILOT_BUILD_STAMP importlib 运行真相）
   分叉 → 换代就绪包：export_snapshot（config 工作区变更，未提交）+ 键级
   diff 报告 + logs/pending_deployment.json + 钉钉播报部署指引
   一致 → no-op（台账 done）
就绪包 → deploy（人审一键，永不进 cron）：
   默认 dry-run（打印计划）；--execute 才真做：
   时窗闸（09:35-15:35 拒）→ git 第一段（commit config）→ build_pilot
   --leg（stamp 锚新提交）→ git 第二段（commit 产物）→ 备份 bak_<stamp> 链 →
   复制掘金目录 → relaunch → 钉钉回报（验证与回滚指引）
   main 腿默认拒绝（--allow-main 例外通道）
部署后：既有自动面收口（晨检 INIT 核 9:40 / ab_compare 双锚 15:50 /
EOD 指纹 / leg_detail §0 运行真相）；次日 watch 三方一致=自愈确认
```

## 安全边界
- **deploy 永人审触发**（资金动作永不自动）；watch 全自动但零资金风险
  （不 build 不 commit 不部署——build 留 deploy 内是两段 stamp 语义要求）
- exp 腿换代=标准组装线产物**整体替换** r10leg 手改变体（新代际切换，
  bak 链+git revert 可回退；非 r10leg 补丁同步语境）
- 不自动 promote（08-25 裁决）；promote 播报文案已修正消解「即刻生效」误导

## 09-03 首跑实证
- watch 检出真实分叉：ACTIVE r610_b3 vs 快照 r68（历史遗留——08-26 promote
  后从未部署），diff 3 键（decay_tau 60→90/max_h_atr 4.5→5.5/
  min_suppression 0.3→0.15=B3 参数），就绪包+播报 4s 产出
- deploy dry-run 计划输出与 main 拒绝闸实证
- **就绪包留用户裁决**：exp 腿当前跑 r10leg 变体（R10 研究线），部署 r610_b3
  =切回 R6-10 提案流代际——研究线悬决，不代用户做

## 挂载
OPS_TASK_CRONS 第十一任务 ops_promote_watch 18:50 mon-fri（台账
job_run=ops_promote_watch，运维面板可见）；pytest 9 件（键路径/三方比对
语义/时窗闸/main 拒/pending 拒/dry-run 零变更）。
