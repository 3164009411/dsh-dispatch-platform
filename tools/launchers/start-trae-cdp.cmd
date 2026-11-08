@echo off
rem Launch App with CDP debug port 9444.
rem Set TRAE_EXE to the installed executable path.
setlocal
if not defined TRAE_EXE (
  echo [ERROR] TRAE_EXE is not set.
  pause
  exit /b 1
)
start "" "%TRAE_EXE%" --remote-debugging-port=9444
endlocal
