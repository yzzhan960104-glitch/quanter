"""Windows 子进程静默执行公共件（2026-09-07/08 · 用户实测弹窗根治两轮）。

物理根因（弹窗定律）：**无控制台的进程再 spawn 控制台程序（powershell/git/
netstat/dws/npm…）时，Windows 会为每个孙进程新建一个可见控制台窗口。** 第一轮
现场：server cron 子进程全走 DETACHED_PROCESS（=无控制台）——gm_guard 每 5 分钟
弹 powershell、publish_public 每小时弹 npm、18 点段 promote/git/sync 连环弹、
每次钉钉推送弹 dws、connect bot 链的 node 各开一窗。

两层修法：
  ① 本模块 ``SILENT``（Windows=CREATE_NO_WINDOW 0x08000000，其余平台空 dict）：
     无人值守链上的零散 subprocess 调用统一展开 ``**SILENT``——子进程零窗口，
     管道/capture 语义不变（句柄继承自父进程，日志重定向照旧）。
  ② 范式级（更根本，09-08 第二轮）：**后台子树拉起一律 CREATE_NO_WINDOW 而非
     DETACHED_PROCESS**（presentation/server/main.py、broadcast/connect_manager.py、
     trading/orchestrate/pipeline.py 三源已换）。区别：DETACHED=子进程无控制台，
     孙控制台程序各自开窗；CREATE_NO_WINDOW=子进程拿到一个**隐藏但可继承**的控制台，
     整个子树（node/dws/逐数据集 python）继承同一个隐藏控制台，永不开窗。
     stdout 仍重定向日志文件，不受影响；CREATE_NEW_PROCESS_GROUP 照常可组合。

边界（何时不加）：ops/dev.py 这类需要肉眼盯输出的手动启动器不要加——会把本该
看见的窗口藏掉；从「父进程自带控制台」的场景（终端里手动跑脚本）也不必加（子
进程继承父控制台，本就不弹窗），加了无害但属噪音。

关联坑（同日实弹）：子进程 stdout 用管道（capture_output=True）接 powershell 而
孙进程长命时，timeout kill 后 communicate 读线程永远等不到 EOF=父进程僵尸
（gm_guard auto_heal 09-07 13:06 实弹卡 11h）——长命孙进程链一律改文件重定向。
"""
from __future__ import annotations

import os
import subprocess

# CREATE_NO_WINDOW 常量仅 Windows 平台的 subprocess 模块存在；非 Windows 展开
# 空 dict，调用点写法统一为 subprocess.run([...], **SILENT) 无需平台分支。
SILENT: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
)
