@echo off
REM Đóng gói thành SubtitleTranslator.exe (chạy trên Windows, cần Python 3.10+)
chcp 65001 >nul
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name SubtitleTranslator main.py
echo.
echo Xong! File exe nam o thu muc dist\SubtitleTranslator.exe
pause
