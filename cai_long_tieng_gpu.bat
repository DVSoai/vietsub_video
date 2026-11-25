@echo off
chcp 65001 >nul
echo === Cai long tieng cho may co card NVIDIA (RTX 50/40/30...) ===
python -m pip install -r requirements.txt
REM PyTorch ban CUDA 12.8 (ho tro RTX 50-series)
python -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
python -m pip install "transformers==4.57.6" safetensors
python -m pip install vieneu
REM Model Hugging Face chay tren may: Whisper (nghe giong) + Qwen (dich), nen 4-bit cho card 8 GB
python -m pip install accelerate bitsandbytes
REM Tach giong chay tren card: thay onnxruntime CPU bang ban GPU
python -m pip uninstall -y onnxruntime
python -m pip install onnxruntime-gpu
echo.
python -c "import torch; print(\"PyTorch CUDA:\", torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"\")"
python -c "import onnxruntime as o; o.preload_dlls(); print(\"ONNX Runtime:\", o.get_available_providers())"
echo.
echo Xong. Mo app bang run.bat roi vao tab "3 - Long tieng".
pause
