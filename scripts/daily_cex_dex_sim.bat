@echo off
REM Daily CEX-DEX sim batch. Schedule via Task Scheduler pointing here.
cd /d "%~dp0\.."
if exist venv\Scripts\python.exe (
  set PY=venv\Scripts\python.exe
) else (
  set PY=python
)
set TEST_MODE=true
%PY% -m src.scripts.cex_dex_sim_batch --count 50 --mode kamino_collateral --signer primary
exit /b %ERRORLEVEL%
