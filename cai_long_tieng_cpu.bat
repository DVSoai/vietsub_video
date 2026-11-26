@echo off
chcp 65001 >nul
echo === Cai long tieng (chay bang CPU) ===
python -m pip install -r requirements.txt
python -m pip install vieneu onnxruntime
REM (tuy chon, rat cham tren CPU) Qwen3-ASR va BS-RoFormer
python -m pip install qwen-asr "audio-separator[cpu]"
echo Xong.
pause
