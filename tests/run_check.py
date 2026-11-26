"""Chạy 1 bài kiểm thử, chấm theo dòng 'N/M PASS' (Windows có thể báo mã thoát lạ khi os._exit sau khi mở nhiều cửa sổ Qt)."""
import re, subprocess, sys
p = subprocess.run([sys.executable, sys.argv[1]], capture_output=True, text=True, encoding="utf-8", errors="replace")
print(p.stdout)
m = re.findall(r"^(\d+)/(\d+) PASS", p.stdout, re.M)
ok = bool(m) and m[-1][0] == m[-1][1] and "[FAIL]" not in p.stdout
print(f"==> {sys.argv[1]}: {'XANH' if ok else 'ĐỎ'} | mã thoát tiến trình = {p.returncode}")
if not ok:
    print(p.stderr[-3000:])
sys.exit(0 if ok else 1)
