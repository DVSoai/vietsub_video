# AI connection — Windows acceptance checks

Branch: feat/provider-auto-connect. No Windows runtime verification yet.

1. Open tab 5, connect Groq, verify embedded console loads. If login is blocked, use external browser.
2. Create a key on the official console and copy it. Use explicit import; verify invalid clipboard is rejected.
3. Verify valid key and selected model activate provider. Invalid key must not be saved.
4. Close during validation: dialog must remain safe and responsive.
5. Restart app; verify provider remains active. Inspect settings.json: on Windows per_provider keys should be stored as keys_dpapi, not plaintext keys.
6. Test translation and dubbing with the same provider configuration.
7. Verify clipboard watching is off by default and other providers still work.
8. Verify legacy settings migrate on save. Keep a secure backup before testing.

Limitations: no verified official automated Groq key provisioning; user creates and copies key. Embedded login may be blocked by identity providers. DPAPI is Windows-user scoped; non-Windows retains legacy storage. No runtime tests have been performed by the implementer.

## Kết quả kiểm thử (26/11/2026)

Windows thật qua GitHub Actions (`.github/workflows/windows-test.yml`, windows-latest, Python 3.11) và Linux.
Mỗi lần đẩy code lên `main` hoặc `feat/**`, GitHub tự chạy lại. Bài kiểm thử được chấm theo dòng `N/M PASS`
(`tests/run_check.py`); mã thoát của tiến trình kiểm thử được ghi riêng, vì trên Windows Qt có thể báo lỗi lúc
thoát đột ngột sau khi đã mở nhiều cửa sổ, chuyện này không xảy ra khi app đóng bình thường.

- `tests/test_ai_connect.py`: Groq /models, đóng hộp thoại khi đang kiểm tra (đóng ngay), hết giờ 20 s,
  thông báo lỗi theo mã HTTP không chứa key, copy key lúc đang bận không bị mất, DPAPI (Windows),
  lưu/mở lại, tab 5 (lưu model/key, xoá key), key không giải mã được (hỏi; No = không ghi đè, Yes = bỏ key hỏng,
  giữ cài đặt khác, sao lưu settings.json.bak).
- `tests/test_free_keys_dialog.py`: clipboard mặc định tắt, tích ô thì tự nhận; key sai/đúng; tab Dịch dùng ngay.
