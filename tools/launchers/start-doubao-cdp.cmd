@echo off
rem Launch App with CDP debug port 9333.
rem Set DOUBAO_EXE to the installed executable path.
setlocal
if not defined DOUBAO_EXE (
  echo [ERROR] DOUBAO_EXE is not set.
  pause
  exit /b 1
)
start "" "%DOUBAO_EXE%" --remote-debugging-port=9333
endlocal
