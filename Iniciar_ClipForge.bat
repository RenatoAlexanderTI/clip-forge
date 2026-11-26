@echo off
cd /d "%~dp0"
python -c "import webview" 2>nul || (
  echo Instalando dependencias por primera vez...
  python -m pip install -r requirements.txt
)
start "" pythonw clipforge.py
exit
