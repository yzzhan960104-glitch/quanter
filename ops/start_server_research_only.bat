@echo off
rem QMT 退役 P1（2026-08-27）：研究面-only 服务器启动。QUANTER_TRADING_FACE=off 经
rem .env 生效，此处显式再设防 .env 丢失时静默回退。输出重定向日志（脱离终端可诊断）。
cd /d E:\quanter
set QUANTER_TRADING_FACE=off
E:\quanter\.venv310\Scripts\python.exe -m trading >> E:\quanter\logs\server_research_only.log 2>&1
