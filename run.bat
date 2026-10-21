@echo off
rem Запуск SexyClean из исходников (ставит зависимости и открывает приложение)
cd /d "%~dp0"
python -m pip install -r requirements.txt
python sexy_cleaner.py
