@echo off
rem Launch App with CDP debug port 9666.
rem Set DUMATE_EXE to the installed executable path.
setlocal
if not defined DUMATE_EXE (
  echo [ERROR] DUMATE_EXE is not set.
  pause
  exit /b 1
)
start "" "%DUMATE_EXE%" --remote-debugging-port=9666
endlocal
