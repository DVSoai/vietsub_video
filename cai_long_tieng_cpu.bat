@echo off
chcp 65001 >nul
echo === Cai long tieng (chay bang CPU) ===
python -m pip install -r requirements.txt
python -m pip install vieneu onnxruntime
echo Xong.
pause
