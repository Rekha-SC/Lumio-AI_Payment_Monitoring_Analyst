@echo off
cd /d "%~dp0"
if not exist .venv (
  echo Creating a Python environment - first run only, takes a minute...
  py -3 -m venv .venv || python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)
python run_local.py %*
pause
