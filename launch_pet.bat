@echo off
title KAIROS Desktop Avatar
cd /d "%~dp0"
echo Starting KAIROS Desktop Avatar on your desktop...
"%~dp0.venv\Scripts\pythonw.exe" -m kairos.pet --hub http://129.151.21.192 --device pc-main
