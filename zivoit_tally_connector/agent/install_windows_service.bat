@echo off
REM ZivoIT Tally Bridge Agent - install as a Windows scheduled task that starts at boot
REM and restarts automatically. Run this file "as Administrator".
setlocal
set AGENT_DIR=%~dp0
where python >nul 2>nul || (echo Python not found in PATH. Install Python 3.10+ first. & pause & exit /b 1)
python "%AGENT_DIR%zivoit_tally_agent.py" --check
if errorlevel 1 (
  echo.
  echo Fix the problems above, then run this installer again.
  pause
  exit /b 1
)
schtasks /Create /F /TN "ZivoIT Tally Agent" /SC ONSTART /RU SYSTEM /RL HIGHEST ^
  /TR "\"%~dp0run_agent.cmd\""
> "%AGENT_DIR%run_agent.cmd" (
  echo @echo off
  echo :loop
  echo python "%AGENT_DIR%zivoit_tally_agent.py"
  echo timeout /t 10 /nobreak ^>nul
  echo goto loop
)
schtasks /Run /TN "ZivoIT Tally Agent"
echo.
echo ZivoIT Tally Agent installed and started. Log: %AGENT_DIR%zivoit_tally_agent.log
pause
