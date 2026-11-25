@echo off
python -m pip install -r requirements.txt
REM yt-dlp hay cap nhat theo Bilibili -> luon lay ban moi nhat
python -m pip install -U yt-dlp
python main.py
