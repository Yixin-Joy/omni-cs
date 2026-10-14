@echo off
REM ============================================================
REM  Start CRM service  ->  http://127.0.0.1:8020
REM  Brain: AI engine + RAG knowledge base + workbench + admin + ops.
REM
REM  NOTE: uses the base Python + PYTHONPATH (NOT .venv\Scripts\python.exe,
REM        whose launcher shim cannot read pyvenv.cfg in this environment).
REM        Dependencies live in .venv\Lib\site-packages.
REM ============================================================
setlocal
set "ROOT=%~dp0"
set "BASEPY=C://Users//its-admin//AppData//Local//Python//pythoncore-3.14-64//python.exe"
set "PYTHONPATH=%ROOT%crm;%ROOT%.venv\Lib\site-packages"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%ROOT%crm"

echo.
echo   CRM  -^>  http://127.0.0.1:8020
echo.
echo   If you see "no module named ...", dependencies are missing:
echo     "%BASEPY%" -m pip install --target="%ROOT%.venv\Lib\site-packages" --upgrade -r "%ROOT%requirements.txt"
echo.
echo   Press Ctrl+C to stop.
echo.
"%BASEPY%" -m uvicorn main:app --host 127.0.0.1 --port 8020
