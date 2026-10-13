@echo off
REM ============================================================
REM  Start CRM service  ->  http://127.0.0.1:8020
REM  Brain: AI engine + RAG knowledge base + workbench + admin + ops.
REM
REM  NOTE: uses the base Python + PYTHONPATH, NOT .venv\Scripts\python.exe.
REM        The bundled .venv launcher was created at a path containing
REM        non-ASCII characters before the repo was moved here, so it
REM        cannot launch. Base Python + .venv\Lib\site-packages works fine.
REM ============================================================
setlocal
set "ROOT=%~dp0"
set "BASEPY=C:\Users\its-admin\AppData\Local\Python\pythoncore-3.14-64\python.exe"
set "PYTHONPATH=%ROOT%crm;%ROOT%.venv\Lib\site-packages"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%ROOT%crm"

echo.
echo   CRM  -^>  http://127.0.0.1:8020  (login: /login)
echo   Press Ctrl+C to stop.
echo.
"%BASEPY%" -m uvicorn main:app --host 127.0.0.1 --port 8020
