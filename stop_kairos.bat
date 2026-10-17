@echo off
title Stop KAIROS
echo Stopping KAIROS agents...
taskkill /f /fi "WINDOWTITLE eq KAIROS PC Agent*" >nul 2>&1
wmic process where "commandline like '%%kairos.pet%%'" call terminate >nul 2>&1
wmic process where "commandline like '%%pc_agent%%'" call terminate >nul 2>&1
echo KAIROS stopped.
ping 127.0.0.1 -n 2 >nul
