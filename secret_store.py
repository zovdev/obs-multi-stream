import os
import sys
import json
import getpass
import hashlib
import base64
import socket
import threading

import config

_LOCK = threading.RLock()

def _b64e(data):
    return base64.urlsafe_b64encode(data).decode("ascii")

def _b64d(text):
    return base64.urlsafe_b64decode(text.encode("ascii"))

def _fingerprint():
    raw = "|".join([
        "obs-multi-stream-v2",
        getpass.getuser() or "user",
        socket.gethostname() or "host",
        config.get_obs_config_dir(),
    ])
    return hashlib.sha256(raw.encode("utf-8", "replace")).digest()

def _xor_crypt(data, fp):
    out = bytearray(len(data))
    block = b""
    for i, byte in enumerate(data):
        j = i % 32
        if j == 0:
            block = hashlib.sha256(fp + i.to_bytes(8, "big")).digest()
        out[i] = byte ^ block[j]
    return bytes(out)

def _is_windows():
    return sys.platform == "win32"

def _default_mode():
    return "dpapi" if _is_windows() else "fp"

def _dpapi_blob_struct():
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]
    return DATA_BLOB

def _dpapi_entropy():
    import ctypes
    DATA_BLOB = _dpapi_blob_struct()
    ent_bytes = config.STORE_SERVICE.encode("ascii")
    ent = ctypes.create_string_buffer(ent_bytes, len(ent_bytes))
    return DATA_BLOB(len(ent), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char)))

def _dpapi_protect(data):
    import ctypes
    DATA_BLOB = _dpapi_blob_struct()
    buf = ctypes.create_string_buffer(data, len(data))
    inb = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    outb = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(inb), None, ctypes.byref(_dpapi_entropy()),
        None, None, 0, ctypes.byref(outb))
    if not ok:
        raise OSError("CryptProtectData failed (WinError %d)"
                      % ctypes.get_last_error())
    try:
        return ctypes.string_at(outb.pbData, outb.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(outb.pbData)

def _dpapi_unprotect(data):
    import ctypes
    DATA_BLOB = _dpapi_blob_struct()
    buf = ctypes.create_string_buffer(data, len(data))
    inb = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    outb = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(inb), None, ctypes.byref(_dpapi_entropy()),
        None, None, 0, ctypes.byref(outb))
    if not ok:
        raise OSError("CryptUnprotectData failed (WinError %d)"
                      % ctypes.get_last_error())
    try:
        return ctypes.string_at(outb.pbData, outb.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(outb.pbData)

def _enc_value(value, mode):
    if mode == "dpapi":
        return _b64e(_dpapi_protect(value.encode("utf-8")))
    return _b64e(_xor_crypt(value.encode("utf-8"), _fingerprint()))

def _dec_value(raw, mode):
    if mode == "dpapi":
        if not _is_windows():
            raise OSError("dpapi blob on non-windows host")
        return _dpapi_unprotect(_b64d(raw)).decode("utf-8")
    if mode == "fp":
        return _xor_crypt(_b64d(raw), _fingerprint()).decode("utf-8")
    raise ValueError("unknown enc mode: %r" % (mode,))

def _path():
    return config.get_secrets_path()

def _atomic_write_json(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

def _load_raw():
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}, None
    if not isinstance(data, dict):
        return {}, None
    if data.get("_fmt") == "ms-v1" and isinstance(data.get("data"), dict):
        return data["data"], data.get("_enc")
    return {}, None

def _save_raw(store, mode):
    payload = {"_fmt": "ms-v1", "_enc": mode, "data": store}
    _atomic_write_json(_path(), payload)

def get_password(key):
    with _LOCK:
        store, mode = _load_raw()
    raw = store.get(key)
    if raw is None:
        return None
    try:
        return _dec_value(raw, mode)
    except Exception as e:
        config.log.error(
            f"secret_store: не удалось расшифровать {key} ({e})")
        return None

def set_password(key, value):
    if value is None:
        delete_password(key)
        return
    with _LOCK:
        store, mode = _load_raw()
        if mode is None:
            mode = _default_mode()
        store[key] = _enc_value(value, mode)
        _save_raw(store, mode)

def delete_password(key):
    with _LOCK:
        store, mode = _load_raw()
        if mode is None:
            return
        if key in store:
            del store[key]
            _save_raw(store, mode)

def get_store_info():
    return {
        "file": _path(),
        "enc": _default_mode(),
    }
