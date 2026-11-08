@echo off
rem Launch App with CDP debug port 9222.
rem Set WORKBUDDY_EXE to the installed executable path.
setlocal
if not defined WORKBUDDY_EXE (
  echo [ERROR] WORKBUDDY_EXE is not set.
  pause
  exit /b 1
)
start "" "%WORKBUDDY_EXE%" --remote-debugging-port=9222
endlocal
