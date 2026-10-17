@echo off
title KAIROS Desktop Starter
cd /d "%~dp0"

echo ======================================================
echo    Starting KAIROS PC Agent and Desktop Pet...
echo    Cloud Hub: http://129.151.21.192
echo ======================================================
echo.

:: Clean up any existing instances first so latest code is always loaded
taskkill /f /fi "WINDOWTITLE eq KAIROS PC Agent*" >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'kairos\.pet' -or $_.CommandLine -match 'agents\.pc_agent' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

:: Launch PC Agent in minimized console window so it stays connected
start "KAIROS PC Agent" /min "%~dp0.venv\Scripts\python.exe" -u -m agents.pc_agent --hub ws://129.151.21.192 --device pc-main --token dev-token

:: Launch Desktop Pet Avatar directly with pythonw (runs as native desktop overlay)
start "KAIROS Desktop Pet" "%~dp0.venv\Scripts\pythonw.exe" -m kairos.pet --hub http://129.151.21.192 --device pc-main

echo Done! KAIROS is now active on your desktop.
ping 127.0.0.1 -n 3 >nul
