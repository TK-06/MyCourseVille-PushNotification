@echo off
REM Manual runner: watch.bat [run|due|check|courses|listen|chatid|test|status] [--force]
REM The scheduled task calls pythonw.exe directly so it never flashes a console.
REM Prefers this repo's own .venv; falls back to the mcv skill's venv if you have one.
if exist "%~dp0.venv\Scripts\python.exe" (
  set "MCVPY=%~dp0.venv\Scripts\python.exe"
) else (
  set "MCVPY=%USERPROFILE%\.claude\skills\mcv\.venv\Scripts\python.exe"
)
"%MCVPY%" "%~dp0watch.py" %*
