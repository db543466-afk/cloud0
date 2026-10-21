@echo off
rem Сборка автономного EXE через PyInstaller
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean --windowed --name "SexyClean" --add-data "assets;assets" sexy_cleaner.py
echo.
echo Готово. EXE: dist\SexyClean\SexyClean.exe
pause
