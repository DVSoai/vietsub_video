# Báo cáo kiểm thử chức năng Lồng tiếng

Ngày: 2026-11-25 · Commit gốc trước khi sửa: `65e0f62` · Kịch bản kiểm thử: `tests/qa_dubbing.py`

## 1. Môi trường và cách kiểm thử

- Linux, Python 3, PySide6 chạy offscreen, CPU 2 nhân, không có GPU.
- **Chạy thật**: toàn bộ giao diện và luồng `DubWorker`, ffmpeg (imageio-ffmpeg), tách giọng MDX-Net (onnxruntime CPU), `Exporter`, `preview_frame`.
- **Giả lập**:
  - VieNeu-TTS: mỗi giọng là một âm sine tần số riêng, độ dài tỉ lệ số chữ, có đếm số lần đọc. Lý do: môi trường không tải được model Hugging Face.
  - Gemini/Vertex: `tests/mock_ai_server.py`, có đếm số lần gọi.
- Dữ liệu tự tạo:
  - `hard.mp4`: 1280×720, 40 s, có "chữ cứng" là hộp trắng chữ đen ở 80–90 % chiều cao.
  - `hard.zh.srt`: 8 câu.
  - `long.mp4`: 240 s, dùng để kiểm thử nút Dừng.
- Kiểm thử đồng bộ tắt nhạc nền (`bg_gain = 0`), giải mã tiếng trong video xuất rồi dò điểm bắt đầu từng câu và so với thời gian câu đã chia cho tốc độ.

Chạy lại:

```
python tests/mock_ai_server.py &
python tests/qa_dubbing.py <nhãn> [T2,T5,...]
```

## 2. Kết quả (trước sửa → sau sửa)

"Trước" là commit `65e0f62` (chưa sửa). "Sau" là commit bàn giao. Cột chi tiết là số đo của lần chạy "sau".

| Mã | Trường hợp | Trước | Sau | Chi tiết (sau) |
|---|---|---|---|---|
| T1 | Làm tự động tới bước kiểm tra giọng | PASS | PASS | kết quả=review, 8 câu, nhân vật=[('Lâm Triệt', 'hai_dang'), ('Sư tôn', 'ngoc_huyen')], 51s, gọi AI=4 |
| T2 | Giữ giọng/tốc độ/cao độ/âm lượng nhân vật khi chạy tự động lại | FAIL | PASS | trước=('thao_vy',1.2,2.0,1.3) sau=('thao_vy', 1.2, 2.0, 1.3) |
| T2b | Giữ câu đã sửa tay khi chạy tự động lại (không bật dịch lại) | FAIL | PASS | câu 1 sau khi chạy lại='Sư tôn, đệ tử đến muộn.' |
| T3 | Cache TTS: chạy lại / sửa câu / đổi giọng / đổi tốc độ-cao độ / tạo lại 1 câu | PASS | PASS | đọc lần đầu=8, chạy lại=0, sửa 1 câu=1, đổi giọng Lâm Triệt=4 (Lâm Triệt có 4 câu), đổi tốc độ/cao độ=0, tạo lại 1 câu=1 |
| T4@0.5 | Đồng bộ hình/tiếng/câu thoại ở tốc độ 0.5x | PASS | PASS | video=79.96s audio=79.96s mong đợi=80.00s, câu lệch tối đa=0ms, câu không thấy=0/8, lỗi= |
| T4s@0.5 | Phụ đề Việt khớp tốc độ 0.5x | PASS | PASS | lệch tối đa=0.0ms |
| T4@1.0 | Đồng bộ hình/tiếng/câu thoại ở tốc độ 1.0x | PASS | PASS | video=40.00s audio=40.00s mong đợi=40.00s, câu lệch tối đa=0ms, câu không thấy=0/8, lỗi= |
| T4@1.25 | Đồng bộ hình/tiếng/câu thoại ở tốc độ 1.25x | PASS | PASS | video=32.00s audio=32.02s mong đợi=32.00s, câu lệch tối đa=0ms, câu không thấy=0/8, lỗi= |
| T4s@1.25 | Phụ đề Việt khớp tốc độ 1.25x | PASS | PASS | lệch tối đa=0.0ms |
| T4@2.0 | Đồng bộ hình/tiếng/câu thoại ở tốc độ 2.0x | PASS | PASS | video=20.00s audio=20.01s mong đợi=20.00s, câu lệch tối đa=60ms, câu không thấy=0/8, lỗi= |
| T4s@2.0 | Phụ đề Việt khớp tốc độ 2.0x | PASS | PASS | lệch tối đa=0.0ms |
| T12 | Câu quá dài không chồng lên câu sau | FAIL | PASS | số khung 50ms có 2 giọng cùng lúc=0 (0 ms) |
| T13 | Chạy tự động khi đã có phụ đề, không có nhà cung cấp AI | FAIL | PASS | có provider=False, kết quả=review, nhân vật=['Người dẫn chuyện'] |
| T5 | Xuất lại ở 1.25x sau khi đã trộn 1.5x dùng đúng tiếng 1.25x | FAIL | PASS | file tiếng 1.25x=hard.vi.dub.1.25x.m4a, 1.5x=hard.vi.dub.1.5x.m4a (trùng tên=False); video=32.00s audio=32.02s mong đợi 32.00s |
| T6 | Chế độ xuất không gọi lại AI/TTS | PASS | PASS | TTS=0 lần, AI=0 lần, 5.1s |
| T7a | Dừng khi đang ghép video (đổi tốc độ, không hiệu ứng) | FAIL | PASS | kết quả=Đã dừng, dừng sau 0.2s kể từ lúc bấm, tổng 3.3s, ffmpeg còn chạy=[] |
| T7b | Dừng khi đang xuất có xoá chữ (Exporter) | PASS | PASS | kết quả=Đã dừng, dừng sau 0.2s kể từ lúc bấm, ffmpeg còn chạy=[] |
| T7c | Dừng khi đang tách giọng | FAIL | PASS | kết quả=Đã dừng, dừng sau 1.5s kể từ lúc bấm |
| T8 thường | Xem trước khớp video xuất (thường) | PASS | PASS | PSNR=48.2 dB (≥30 là giống) |
| T8 phóng to+màu+lật | Xem trước khớp video xuất (phóng to+màu+lật) | PASS | PASS | PSNR=45.9 dB (≥30 là giống) |
| T8 9:16+logo | Xem trước khớp video xuất (9:16+logo) | PASS | PASS | PSNR=49.2 dB (≥30 là giống) |
| T9a | Thứ tự filter | PASS | PASS | drawbox → hflip → crop=trunc → eq= → ass= → force_original_aspect_ratio |
| T9b | Đổi phóng to sau khi căn vùng che: chữ cứng không lọt ra | FAIL | PASS | tỉ lệ điểm trắng (chữ cứng) ngay dưới vùng che=0.5% |
| T10a | Lồng tiếng lấy màu/phóng to từ tab Xuất video, tốc độ & logo từ tab Lồng tiếng | PASS | PASS | sat=140 zoom=110 speed=1.25 audio_sped=True logo='' audio_file=hard.vi.dub.1.25x.m4a |
| T10b | Căn vùng ở tab Xuất video rồi quay lại: vị trí về tab Lồng tiếng | PASS | PASS | sau khi quay lại: y=70.0 cao=9.0 chế độ='Làm mờ chữ Trung' |
| T11 | DubWorker không gọi hàm đọc widget từ luồng nền | FAIL | PASS | gọi từ luồng nền: không có |

**Sau sửa: 26/26 PASS.** Trước sửa: 9 trường hợp FAIL.

## 3. Lỗi đã xác minh và cách sửa

| Mã | Lỗi quan sát được (trước) | Nguyên nhân | Sửa |
|---|---|---|---|
| T2 | Chạy tự động lại làm mất giọng/tốc độ/cao độ/âm lượng: `('thao_vy',1.2,2.0,1.3)` → `('ngoc_huyen',1.0,0.0,1.0)` | `_chars()` tạo lại danh sách nhân vật mới | Giữ thông số của nhân vật cùng tên. Nếu mọi câu đã có nhân vật và không bật dịch lại thì giữ nguyên và không gọi AI. |
| T2b | Câu sửa tay bị thay bằng bản dịch cũ | `lines_from_srt()` đọc lại `.vi.srt` | Khi không bật "Dịch lại", giữ chữ/nhân vật/cảm xúc của câu khớp `index` + lời gốc |
| T5 | Xuất ở 1.25x sau khi trộn 1.5x cho video 26,68 s thay vì 32 s | Mọi tốc độ khác 1x dùng chung `video.vi.dub.m4a` | Tên file theo tốc độ: `video.vi.dub.1.25x.m4a` |
| T7a | Bấm Dừng khi đang ghép video (đổi tốc độ) phải chờ 26,5 s, rồi vẫn báo xong | `mux_video` dùng `subprocess.run` | `_run_stoppable`: Popen, gửi `q` rồi kill, ném `Stopped` |
| T7c | Bấm Dừng khi tách giọng: chờ 23,1 s, báo lỗi `Stopped:` thay vì "Đã dừng" | Trên CPU gộp 4 đoạn mỗi lượt; `vocal.Stopped` khác `dub.Stopped` | CPU 1 đoạn/lượt; `dub.Stopped` dùng chung lớp của `vocal` |
| T9b | Căn vùng che rồi đổi phóng to 120 %: 27,9 % điểm chữ cứng lọt ra | Che chữ chạy **sau** phóng to | Che chữ chạy trên khung gốc, trước tốc độ/lật/phóng to/màu. Khung vùng che trên ảnh xem trước bị ẩn khi phóng to (toạ độ không còn trùng). |
| T11 | Luồng nền gọi `find_srt`/`out_dir_getter`/`providers`/`dub_audio_path`/`dub_video_path`. `providers()` đọc combobox và ô key của tab Dịch, còn ghi cấu hình. | Worker gọi thẳng hàm của trang | Chụp toàn bộ vào `DubWorker.o` trên luồng UI |
| T12 | Câu quá dài chồng lên câu sau 900 ms | `fit_lines` chỉ tăng tốc tới `max_speed` | Lùi câu sau tối đa 0,6 s, còn thừa thì cắt đuôi + mờ dần 120 ms, ghi log số câu |
| T13 | Đã có `.zh.srt` + `.vi.srt` nhưng không cấu hình AI thì lỗi `ProviderError` | `_chars()` luôn gọi `providers()[0]` | Lỗi AI chỉ báo khi bước nào thật sự cần. Nhận diện nhân vật không có AI thì chia theo giới tính / "Người dẫn chuyện". |

Các điểm kiểm tra **không** có lỗi: cache TTS (T3), đồng bộ tốc độ 0,5×/1×/1,25×/2× (T4), xuất không gọi lại AI/TTS (T6), dừng Exporter (T7b), xem trước = video xuất (T8), cấu hình giữa 2 tab (T10).

## 4. Lỗi / giới hạn còn tồn tại (chưa sửa trong đợt này)

1. Không kiểm thử được VieNeu-TTS, Whisper, Qwen thật: Hugging Face không truy cập được từ môi trường kiểm thử. Cần chạy trên máy có RTX 5060 để kiểm tra chất lượng giọng, tốc độ đọc thực và VRAM.
2. Không kiểm thử NVENC (máy kiểm thử không có GPU). Khi NVENC lỗi, `mux_video` tự chuyển sang x264 (đường x264 đã chạy thật).
3. Chưa kiểm thử video dọc 9:16 làm đầu vào và video 1080p thật. Đã kiểm thử 1280×720 và khung ra 9:16.
4. Cảm xúc ngoài `[cười]`/`[thở dài]` chưa ảnh hưởng giọng đọc (giới hạn của VieNeu). Đề xuất xử lý ở đợt tính năng sau.
5. Bảng câu/nhân vật vẫn sửa được trong lúc worker chạy. Chưa thấy lỗi nhưng chưa khoá giao diện.
6. Tab Xuất video chỉ tự nhận tiếng lồng 1x.
7. Ở 2x, câu bị lùi để không chồng câu trước lệch tối đa 60 ms (T4@2.0). Đây là chủ ý, không phải lệch đồng bộ.

## Nhánh nang-cap-ai-mien-phi (26/11/2026)

Kết quả chạy thật trong máy Linux 2 nhân CPU, không có GPU (offscreen Qt, máy chủ AI giả lập thay cho nhà cung cấp thật):

| Bộ kiểm thử | Kết quả |
|---|---|
| tests/qa_dubbing.py (hồi quy Lồng tiếng) | 26/26 PASS |
| tests/test_free_providers.py (5 nhà miễn phí, giới hạn request, bỏ `<think>`) | 6/6 PASS |
| tests/test_local_models.py (Qwen3-ASR giả lập + BS-RoFormer thật) | 7/7 PASS — R1: CPU, 61 s cho 4 s âm thanh (gồm nạp model); R2: dừng ngay, không còn tiến trình con |
| tests/test_translation_quality.py (thuật ngữ + dịch kỹ) | 7/7 PASS |
| tests/test_free_keys_dialog.py (Kết nối AI miễn phí) | 6/6 PASS |

Ghi chú: BS-RoFormer trên CPU khoảng 10–15 lần thời lượng âm thanh; với card 2 GB VRAM nên dùng MDX-Net.
Qwen3-ASR mới kiểm thử phần ghép câu/cửa sổ bằng model giả lập; chạy model thật cần máy người dùng.
