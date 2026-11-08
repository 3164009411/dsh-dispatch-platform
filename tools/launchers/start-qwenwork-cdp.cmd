@echo off
rem Launch QwenWork with CDP debug port 9555.
rem Set QWENWORK_EXE to the installed executable path.
setlocal
if not defined QWENWORK_EXE (
  echo [ERROR] QWENWORK_EXE is not set.
  pause
  exit /b 1
)
start "" "%QWENWORK_EXE%" --remote-debugging-port=9555
endlocal
