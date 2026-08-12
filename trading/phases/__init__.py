# -*- coding: utf-8 -*-
"""trading.phases —— 交易时序触发点函数的外迁承载包（T1 模块化拆分 · wayfinder 集群 E/F/G/H）。

物理定位：
    本包按「交易日内时序阶段」收纳原 ``trading/engine.py`` 的模块级触发点函数。engine.py 作为
    APScheduler 编排容器（cron/interval wrapper + 实例状态），具体的「盘前挂单 / 盘中止损 /
    盘后对账 / 出场」逻辑逐阶段迁出为 free function，经 ``EnginePorts`` 窄接口注入 engine 实例
    依赖（gate / 白名单），实现编排与执行的物理解耦（strangler 红线①：函数逻辑零改动，只搬位置）。

子模块（按 T1 Task 渐进迁入）：
    - ``pre_open``（Task 6 · 集群 E 盘前挂单）：``pre_open`` + ``_pre_open_impl``。
    - 后续 Task 7-9 将迁入 ``stop_loss`` / ``post_close`` / ``exit``（集群 F/G/H）。

测试 patch 路径约定（W1-A/T2 收口后）：
    本包内函数对原 engine 模块级符号（``_submit`` / ``_state_store`` / ``get_gateway`` /
    ``_cancel_all_open_orders`` 等）已全量改为顶部直接 import 物理真身模块（gateway_service /
    state_store / io.breaker / critical / account）——engine 反查中间层退役。``patch(
    "trading.engine._xxx")`` / ``monkeypatch.setattr(engine, "_xxx", ...)`` 类测试因不再经
    engine 模块属性解析而失效，Task 8-19 迁 patch 至物理真身模块路径（详见各子模块 docstring）。
"""
