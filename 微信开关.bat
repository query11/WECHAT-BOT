@echo off
REM Double-click to switch the WeChat bridge ON, double-click again to switch it OFF.
REM chcp 65001: bridge.log is UTF-8, and the failure tail is printed in Chinese.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0toggle.ps1"
echo.
pause
