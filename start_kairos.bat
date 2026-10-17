@echo off
title KAIROS Desktop Starter
cd /d "D:\vibecoding\GraphHacks_Kairos\GraphHacks_Kairos"

echo ======================================================
echo    Starting KAIROS PC Agent and Desktop Pet...
echo    Cloud Hub: http://129.151.21.192
echo ======================================================
echo.

:: Launch PC Agent in minimized console window so it stays connected
start "KAIROS PC Agent" /min "D:\vibecoding\GraphHacks_Kairos\GraphHacks_Kairos\.venv\Scripts\python.exe" -u -m agents.pc_agent --hub ws://129.151.21.192 --device pc-main --token dev-token

:: Launch Desktop Pet Avatar directly with pythonw (runs as native desktop overlay)
start "KAIROS Desktop Pet" "D:\vibecoding\GraphHacks_Kairos\GraphHacks_Kairos\.venv\Scripts\pythonw.exe" -m kairos.pet --hub http://129.151.21.192 --device pc-main

echo Done! KAIROS is now active on your desktop.
ping 127.0.0.1 -n 3 >nul
