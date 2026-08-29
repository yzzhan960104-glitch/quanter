@echo off
rem Server startup (W6+ V2, 2026-08-30): python -m trading was removed with
rem the engine retirement. Correct entry = uvicorn. Server lifespan hosts
rem ops_sched: pipeline_then_eod 18:00 mon-fri + discovery + digest + W9 six
rem ops crons. Server dead = pipeline dead (root cause of 08-28/29 lake gap).
cd /d E:\quanter
E:\quanter\.venv310\Scripts\python.exe -m uvicorn presentation.server.main:app --host 127.0.0.1 --port 8000 >> E:\quanter\logs\server_research_only.log 2>&1
