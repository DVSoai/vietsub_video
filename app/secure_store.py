"""Protect provider keys at rest with Windows user-scoped DPAPI."""
import base64
import copy
import ctypes
import sys
from ctypes import wintypes

class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

def _crypt(data: bytes, decrypt: bool) -> bytes:
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    src = Blob(len(data), buf)
    dst = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel.LocalFree(dst.pbData)

def protect_config(cfg: dict) -> dict:
    result = copy.deepcopy(cfg)
    if sys.platform != "win32":
        return result
    for pc in result.get("per_provider", {}).values():
        keys = pc.pop("keys", "")
        if keys:
            pc["keys_dpapi"] = base64.b64encode(_crypt(keys.encode("utf-8"), False)).decode("ascii")
    return result

def restore_config(cfg: dict) -> dict:
    result = copy.deepcopy(cfg)
    for pc in result.get("per_provider", {}).values():
        if "keys_dpapi" in pc:
            if sys.platform != "win32":
                raise RuntimeError("API keys are protected by Windows DPAPI")
            pc["keys"] = _crypt(base64.b64decode(pc.pop("keys_dpapi"), validate=True), True).decode("utf-8")
    return result


def restore_config_partial(cfg: dict) -> tuple[dict, list[str]]:
    """Giải mã từng nhà; nhà nào không giải mã được (đổi máy / đổi tài khoản Windows) thì bỏ key,
    giữ nguyên mọi cài đặt khác. Trả về (cấu hình, danh sách nhà bị bỏ key)."""
    result = copy.deepcopy(cfg)
    failed = []
    for name, pc in result.get("per_provider", {}).items():
        if "keys_dpapi" not in pc:
            continue
        blob = pc.pop("keys_dpapi")
        try:
            if sys.platform != "win32":
                raise RuntimeError("API keys are protected by Windows DPAPI")
            pc["keys"] = _crypt(base64.b64decode(blob, validate=True), True).decode("utf-8")
        except Exception:  # noqa: BLE001
            pc["keys"] = ""
            pc["enabled"] = False
            failed.append(name)
    return result, failed
