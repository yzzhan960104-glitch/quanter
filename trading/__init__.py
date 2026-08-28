"""trading 包——QMT 退役后的保留面（W6-A，2026-08-28 全库评审清偿·完成退役）。

历史：本包曾是 QMT 本地交易引擎（engine/phases/io/gateway live 面）。掘金升格
唯一实盘平台（2026-08-27 用户裁决）后，live 面于 2026-08-28 W6-A 删除——
P1-13（撤当日单无日期过滤）/P1-14（chase 死链）/P1-15（止损限价挂死）三个
dormant bug 随删除自然消账；复活路径=git 历史（archive/qmt-stack-final 锚点
分支保有删除前完整状态）。

保留面（仍有活消费者，掘金/研究/运维共用）：
    ├─ calendar/clock      交易日历与单一时间源（pipeline/export_snapshot/pilot 对拍）
    ├─ job_ledger          作业台账（ops_sched 四 cron 幂等）
    ├─ state_store         sqlite 状态库（data_ready 台账；risk 双值；trading_plan 摘要）
    ├─ data_ctx            主湖加载 helper（export_snapshot 的 universe 单源）
    ├─ trading_plan        当日计划读写（broadcast 播报消费）
    ├─ compute/            纯决策函数（止损/熔断判定——pilot 的本地对拍参照真身）
    ├─ types/              纯数据契约（OrderState 枚举——state_store 依赖）
    ├─ orchestrate/pipeline 数据管道编排（ops_sched 18:00 消费）
    ├─ critical            L1 致命异常与告警通道（pipeline/compute 共用）
    ├─ single_instance     pid 文件（process_topology 引擎探测锚）
    └─ risk_ctrl           人工风控双值 CLI（ADR-16，state_store 单源）

已删除（git 历史可考）：engine/__main__/gateway_service/catchup/eod_plan/
reconcile_job/phases/io/live 工具三件/dynamic_whitelist/stop_loss_context/
plan_report/review_report/position_book/order_state(根)/account/alerting/
ports/protocols/broker_ports/qmt_market_data。
"""

from .types import OrderState

__all__ = ["OrderState"]
