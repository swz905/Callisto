@echo off
title KAIROS PC Companion Agent
cd /d "%~dp0"
echo ============================================================
echo   KAIROS PC Companion Agent (Connecting to Cloud Hub)
echo   Hub: ws://129.151.21.192
echo ============================================================
echo.
python -m agents.pc_agent --hub ws://129.151.21.192 --device pc-main --token dev-token
pause
