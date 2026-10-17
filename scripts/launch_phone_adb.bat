@echo off
setlocal enabledelayedexpansion

echo ========================================================
echo         KAIROS Phone Agent - ADB Setup & Launcher
echo ========================================================
echo.

:: 1. Check for Python
where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python is not found in PATH!
    echo Please install Python 3.11+ and try again.
    pause
    exit /b 1
)

:: 2. Check for ADB
where adb >nul 2>nul
if %ERRORLEVEL% neq 0 (
    if exist "%LOCALAPPDATA%\Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe" (
        set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools;%PATH%"
    ) else if exist "%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe" (
        set "PATH=%LOCALAPPDATA%\Android\Sdk\platform-tools;%PATH%"
    ) else if exist "C:\platform-tools\adb.exe" (
        set "PATH=C:\platform-tools;%PATH%"
    ) else (
        echo [!] ADB not found in PATH.
        echo Attempting to install Google.PlatformTools via winget...
        winget install --id Google.PlatformTools -e --source winget
        set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools;%PATH%"
    )
)

where adb >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] ADB is still not in PATH.
    echo Please download platform-tools from:
    echo https://developer.android.com/tools/releases/platform-tools
    echo Extract to C:\platform-tools and add to PATH.
    pause
    exit /b 1
)

echo [OK] ADB found:
adb version
echo.

:: 3. Check connected devices
echo Checking connected devices...
adb devices
echo.
echo If your phone is not listed above, choose:
echo   [1] Connect via USB (Ensure USB Debugging is ON in Developer Options)
echo   [2] Connect via Wireless ADB (Developer Options -^> Wireless Debugging)
echo.
set /p CHOICE="Enter choice (1 for USB / already connected, 2 for Wireless IP): "

if "%CHOICE%"=="2" (
    set /p PHONE_IP="Enter Phone IP:Port (e.g. 192.168.1.50:5555): "
    echo Connecting to !PHONE_IP!...
    adb connect !PHONE_IP!
    echo.
    adb devices
)

:: 4. Verify websockets package
python -c "import websockets" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo Installing required 'websockets' package...
    pip install websockets
)

echo.
echo ========================================================
echo Launching KAIROS Phone Agent in ADB Mode...
echo Hub: ws://localhost:8000
echo Controls: Notifications, Files (/sdcard), Chat (WhatsApp/SMS), UI Automation
echo Type chat lines directly in this window to talk to KAIROS.
echo ========================================================
echo.

python -m agents.phone_agent --hub ws://localhost:8000 --device phone-arjun --adb --chat

pause
