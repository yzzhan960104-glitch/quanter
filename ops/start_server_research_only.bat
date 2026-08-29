@echo off
rem 研究面-only 服务器启动（W6 后 V2，2026-08-29 修正：python -m trading 已随引擎退役删除，
rem 正确入口=uvicorn 直启。server 的 lifespan 托管 ops_sched：pipeline_then_eod 18:00
rem 周一至周五（采集→校验→brief）+ discovery daemon + digest——server 死=管道死（08-28/29
rem 湖断供两天的根因）。
cd /d E:\quanter
E:\quanter\.venv310\Scripts\python.exe -m uvicorn presentation.server.main:app --host 127.0.0.1 --port 8000 >> E:\quanter\logs\server_research_only.log 2>&1
