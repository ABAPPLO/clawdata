@echo off
chcp 65001 >nul
cd /d D:\project\clawdata
REM MCP 常驻服务（与面板 start_dashboard.bat 配套）：127.0.0.1:8000 面板 -> 0.0.0.0:8100 MCP
python -m clawdata.mcp --transport http --host 0.0.0.0 --port 8100 --api http://127.0.0.1:8000
pause
