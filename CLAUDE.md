# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Windows desktop app (PySide6) "Subtitle Translator" for Vietnamese creators: find/download Chinese videos (Bilibili etc.), transcribe audio to `.zh.srt` with AI, translate subtitles to `.vi.srt`, burn styled subtitles into video, and dub into Vietnamese with local TTS (VieNeu). All UI text, README, docs, test output and commit messages are in **Vietnamese** — keep new user-facing strings and commit messages in Vietnamese to match.

## Commands

- Run app: `python main.py` (or `run.bat`, which also installs `requirements.txt` and upgrades `yt-dlp`).
- Build exe: `build_exe.bat` → PyInstaller one-file `dist\SubtitleTranslator.exe` (bundles `imageio_ffmpeg` binaries).
- Optional heavy deps (torch, transformers, vieneu, qwen-asr, audio-separator, onnxruntime-gpu) for dubbing / local models: `cai_long_tieng_gpu.bat` or `cai_long_tieng_cpu.bat`. Code must keep working when these are absent (import lazily).
- No linter is configured.

### Tests

Most tests are standalone scripts (not pytest) that spin up a local mock HTTP server in place of real AI/Bilibili APIs, run Qt offscreen, and print `N/M PASS` lines plus `[FAIL]` markers. Run one test:

```
python tests/run_check.py tests/test_ai_connect.py
```

`run_check.py` grades by the last `N/M PASS` line rather than the exit code, because Qt on Windows can return odd exit codes after `os._exit`. Running the script directly (`python tests/test_xxx.py`) also works. Set `QT_QPA_PLATFORM=offscreen` and `PYTHONUTF8=1` when running headless (CI does).

- `tests/test_video_splitter.py` is a plain unittest: `python -m unittest tests.test_video_splitter`.
- `tests/qa_dubbing.py` needs `python tests/mock_ai_server.py` running on :8765 first; it writes into `tests/qa_data/` (gitignored).
- `tests/win_close_check.py` / `tests/test_close_while_busy.py` check that the app opens and exits cleanly (exit code 0) even with background threads running.
- CI: `.github/workflows/windows-test.yml` runs the above on `windows-latest`, Python 3.11, on pushes to `main`, `feat/**`, `fix/**` and on PRs. When adding a new test script, add a `run_check.py` step there.
- Tests isolate config by monkeypatching `app.gui.DATA`, `PROMPTS`, `CONFIG` to a temp dir before creating `MainWindow` — follow that pattern so tests never touch the real `data/settings.json`.

## Architecture

Entry: `main.py` → `app.gui.main()`. `app/gui.py` `MainWindow` has a left icon sidebar (`nav_btns`, `QToolButton#navItem`, icons from `theme.GLYPH` via the Segoe MDL2/Fluent icon font) and a `QStackedWidget` whose indices are fixed: 0 search, 1 translate, 2 dub, 3 render, 4 AI settings, 5 split, 6 auto (`MainWindow.PAGES` holds titles). Tab 2's AI provider editor lives on the AI settings page (table `ai_table` + editor card); tab 2 shows only `lb_ai_short`. Every page keeps its action bar (progress + primary button) outside any scroll area so it stays visible at 1366×768. Each tab is its own `*Page` module (`search_page`, `render_page`, `dub_page`, `split_page`) constructed with shared callbacks from `MainWindow`: the config dict `cfg`, `_save_cfg`, `_log`, and `_make_translate_providers` (factory that builds the list of AI providers the user enabled). Pages don't create providers themselves.

Layering: Qt-free core modules do the work; `*_page.py` / `gui.py` own widgets and run the core in `QThread` workers (`Worker`, `Task` in gui.py, `DubWorker` in dub_page.py) that report via signals. Settings are snapshotted on the UI thread before a worker starts. Keep core modules free of Qt imports.

- **AI providers** (`providers.py`): `PROVIDERS` dict (display name → defaults) and `make_provider()`. Classes: `OpenAICompatible` (most providers, incl. Groq/Cerebras/custom), `AnthropicProvider`, `VertexProvider` (API key), `VertexSAProvider` (service-account JSON → cached auto-refreshing tokens). Errors split into `ProviderError` (fatal: bad key/model → drop that key) vs `RetryableError` (429/5xx/network → retry). A retired-model map + `current_model()`/`model_gone()` auto-migrate models that providers shut down; `gui.migrate_models()` applies this to saved config. `strip_thinking()` removes `<think>` blocks.
- **Translation** (`translator.py`): `Translator` splits cues into JSON batches, runs them in parallel threads under a shared `RateLimiter` (RPM), rotates across all keys of all enabled providers, retries, then re-translates missing/untranslated lines (`needs_retranslation`). Supports glossary injection and a "dub mode" that tags each cue with its duration (`⟪2.4s⟫`) so translations fit speaking time.
- **Transcription** (`transcribe.py`): ffmpeg extracts audio, splits ~2‑min chunks at silences, Gemini/Vertex (or local Whisper/Qwen3-ASR via `hf_local.py`) transcribes in parallel; optionally returns speaker/gender/emotion. Existing `.zh.srt` next to the video is reused instead of re-transcribing.
- **Rendering** (`render.py`): SRT → ASS styling (`SubStyle`), hardsub covering (`Cover`: blur/box/inpaint), look/frame effects (`Look`), `ExportOpts`, builds the ffmpeg filter graph; `Exporter` encodes with x264 or NVENC; `preview_frame` renders one frame for live preview. `region_dialog.py` is the region editor. The dub tab reuses `RenderPage._style()/_look()/_opts()`.
- **Dubbing**: `dub.py` (Qt-free: `Project/Character/Line`, character analysis via AI, VieNeu `TTSEngine`, per-line TTS cache keyed by sha1, tempo/pitch, `fit_lines`, `mix`, `mux_video`), `vocal.py` (UVR MDX-Net via onnxruntime) / `roformer.py` (BS-RoFormer) for vocal separation, `dub_page.py` UI. Pipeline pauses at the "characters & voices" review step for the user before synthesis. Full sequence diagram and module responsibilities: `docs/DUBBING_ARCHITECTURE.md`.
- **Download queue** (`search_page.DownloadThread`): a list guarded by a `Condition` (not a `queue.Queue`) so queued jobs can be paused/removed. Per-job control goes through `DownloadJob.cancel` (`"pause"`/`"remove"`), checked by `Downloader._halt()`; the global `stop_event` is only for "stop all"/shutdown. Pause keeps `.part` files so resume continues via HTTP Range; remove deletes `job.temps`.
- **Bilibili login & subtitles** (`bilibili.py`, `bili_dialogs.py`): Bilibili only returns subtitle URLs (`/x/player/wbi/v2`) to logged-in sessions, so the app has QR login (passport `qrcode/generate` + `poll`) or pasted `SESSDATA`. Login cookies live in `cfg["bili_login"]["cookies"]` (JSON string, DPAPI-encrypted by `secure_store`). Subtitles saved next to a download as `<video stem>.<lang>.srt` so the translate tab reuses them.
- **Auto mode** (`auto_page.py`): `AutoWorker` chains `Downloader` → `gui.Worker.run()` (called synchronously in its own thread) → `render.Exporter`, emitting per-step states to `StepRow`s. Re-running reuses the video, `.zh.srt` and a complete newer `.vi.srt`.
- **Subtitle editor** (`sub_editor.py`): `SubtitleEditor` dialog; `problems()` flags empty/untranslated/CJK-left/too-fast/overlapping cues.
- **Downloading** (`bilibili.py`, `sources.py`, `search_page.py`): Bilibili search/browse via guest cookie (buvid3) with WBI signing fallback; downloads DASH H.264 video + audio directly from the playurl API (`try_look=1` allows 1080p without login) and muxes with ffmpeg, falling back to `yt-dlp`. Other sites (Douyin, Weibo, XHS, AcFun…) only open the browser search page; pasted share text is parsed for URLs and short links resolved in `sources.py`.
- **ffmpeg** always comes from `imageio-ffmpeg` (no system install assumed).

## Config & data

- `data/` lives next to the script or next to the frozen exe (`gui.app_dir()`); holds `settings.json`, `prompts/` (per-film `.txt` prompts; lines starting with `#` are comments not sent to the AI), and `data/models/` (downloaded models). `settings.json` and `models/` are gitignored.
- On Windows, API keys in `settings.json` are encrypted with DPAPI (`secure_store.py`, stored as `keys_dpapi`); `restore_config_partial` handles undecryptable keys without wiping other settings.
- Output naming convention: `name.<lang>.srt` (e.g. `.zh.srt`, `.vi.srt`), `name.vietsub.mp4`, `name.long_tieng.mp4`; dub intermediates in `name_long_tieng/` beside the video. Other code locates files by these names, so keep them stable.
