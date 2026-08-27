@echo off
rem QMT 退役 P1（2026-08-27）：研究面-only 服务器启动（QUANTER_TRADING_FACE=off 在 .env，
rem trading/__main__ load_dotenv(override=True) 读入；引擎装配已随 P3 删除，此变量仅作留痕）。
cd /d E:\quanter
E:\quanter\.venv310\Scripts\python.exe -m trading >> E:\quanter\logs\server_research_only.log 2>&1
