
import os
import re
import sys
import json
import glob
import time
import ctypes
import random
import socket
import struct
import shutil
import threading

try:
    import obspython as obs
except Exception:
    obs = None

import config

class OmrBridgeError(Exception):
    pass

def _default_dispatcher(fn, **kwargs):
    fn()

_run_on_main = _default_dispatcher

def set_task_dispatcher(fn):

    global _run_on_main
    _run_on_main = fn

_FE_DLL_SPECS = {
    "obs_frontend_get_current_profile_path": ([], ctypes.c_void_p, True),
    "obs_frontend_create_profile": ([ctypes.c_char_p], None, False),
    "obs_frontend_duplicate_profile": ([ctypes.c_char_p], None, False),
    "obs_frontend_delete_profile": ([ctypes.c_char_p], None, False),
    "obs_frontend_streaming_start": ([], None, False),
    "obs_frontend_streaming_stop": ([], None, False),
    "obs_frontend_streaming_active": ([], ctypes.c_bool, False),
    "obs_frontend_recording_active": ([], ctypes.c_bool, False),
}

_FE_ATOMIC_FUNCS = frozenset((
    "obs_frontend_streaming_active",
    "obs_frontend_recording_active",
))

_fe_dll = None
_fe_dll_tried = False
_fe_bfree_fn = None
_fe_ctypes_cache = {}
_fe_ini_path_warned = False
_k32 = None

def _kernel32():

    global _k32
    if _k32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetModuleHandleW.restype = ctypes.c_void_p
        k.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        k.LoadLibraryW.restype = ctypes.c_void_p
        k.LoadLibraryW.argtypes = [ctypes.c_wchar_p]
        _k32 = k
    return _k32

def _module_handle(dll_name, try_load=False):

    if sys.platform != "win32":
        return None
    try:
        h = _kernel32().GetModuleHandleW(dll_name)
        if h:
            return h
    except Exception:
        pass
    if not try_load:
        return None
    candidates = [dll_name]
    exe_dir = (os.path.dirname(sys.executable)
               if getattr(sys, "executable", None) else "")
    if exe_dir:
        full = os.path.join(exe_dir, dll_name)
        if full not in candidates:
            candidates.append(full)
    for cand in candidates:
        try:
            h = _kernel32().LoadLibraryW(cand)
            if h:
                return h
        except Exception:
            continue
    return None

def _fe_get_dll():

    global _fe_dll, _fe_dll_tried
    if _fe_dll_tried:
        return _fe_dll
    _fe_dll_tried = True
    if sys.platform != "win32":
        return None
    try:
        h = _module_handle("obs-frontend-api.dll", try_load=True)
        if not h:
            return None
        _fe_dll = ctypes.CDLL("obs-frontend-api", handle=h)
    except Exception:
        _fe_dll = None
    return _fe_dll

def _fe_bfree(ptr):

    global _fe_bfree_fn
    try:
        if _fe_bfree_fn is None:
            h = _module_handle("libobs.dll")
            if not h:
                return
            libobs = ctypes.CDLL("libobs", handle=h)
            libobs.bfree.argtypes = [ctypes.c_void_p]
            libobs.bfree.restype = None
            _fe_bfree_fn = libobs.bfree
        _fe_bfree_fn(ptr)
    except Exception:
        pass

def _fe_ctypes(func_name):

    if func_name in _fe_ctypes_cache:
        return _fe_ctypes_cache[func_name]
    fn = None
    spec = _FE_DLL_SPECS.get(func_name)
    dll = _fe_get_dll() if spec else None
    if dll is not None:
        raw = getattr(dll, func_name, None)
        if raw is not None:
            argtypes, restype, free_result = spec
            raw.argtypes = argtypes
            raw.restype = restype

            def _call(*args, _raw=raw, _free=free_result):
                cargs = [a.encode("utf-8") if isinstance(a, str) else a
                         for a in args]
                res = _raw(*cargs)
                if _free and res:
                    try:
                        return ctypes.string_at(res).decode(
                            "utf-8", "replace")
                    finally:
                        _fe_bfree(res)
                return res

            fn = _call
    _fe_ctypes_cache[func_name] = fn
    return fn

def _fe_profile_path_from_ini():

    try:
        root = config.get_obs_config_dir()
        if not root or not os.path.isdir(root):
            return None
        user_ini = os.path.join(root, "user.ini")
        global_ini = os.path.join(root, "global.ini")

        bases = []
        loc = ini_get(global_ini, "Locations", "Profiles")
        if loc:
            bases.append(os.path.join(loc, "obs-studio", "basic",
                                      "profiles"))
        bases.append(os.path.join(root, "basic", "profiles"))

        for ini, key in ((user_ini, "ProfileDir"),
                         (global_ini, "ProfileDir"),
                         (user_ini, "Profile"),
                         (global_ini, "Profile")):
            name = ini_get(ini, "Basic", key)
            if not name:
                continue
            for base in bases:
                path = os.path.join(base, name)
                if os.path.isdir(path):
                    return os.path.normpath(path)
    except Exception:
        pass
    return None

def _fe(func_name, *args, timeout=30.0):

    global _fe_ini_path_warned
    if obs is None:
        raise OmrBridgeError(
            "obspython недоступен — скрипт должен работать внутри OBS")
    fn = getattr(obs, func_name, None)
    if fn is None:
        fn = _fe_ctypes(func_name)
    if (fn is None
            and func_name == "obs_frontend_get_current_profile_path"):
        path = _fe_profile_path_from_ini()
        if path:
            if not _fe_ini_path_warned:
                _fe_ini_path_warned = True
                config.log.warning(
                    "obs-frontend-api.dll недоступна через ctypes — "
                    "каталог профиля взят из user.ini/global.ini: "
                    + path)
            return path
        raise OmrBridgeError(
            "Не удалось определить каталог профиля OBS: функции нет в "
            "obspython, obs-frontend-api.dll недоступна, а в "
            "user.ini/global.ini нет [Basic] ProfileDir/Profile")
    if fn is None:
        raise OmrBridgeError(
            f"Функция {func_name} недоступна: нет в obspython и не "
            "найдена в obs-frontend-api.dll (ctypes). Перезапустите "
            "OBS и пришлите лог, если ошибка повторится")
    if func_name in _FE_ATOMIC_FUNCS:
        return fn(*args)
    if _run_on_main is _default_dispatcher:
        return fn(*args)
    result = {}

    def _task():
        try:
            result["value"] = fn(*args)
        except Exception as e:
            result["error"] = e

    _run_on_main(_task, timeout=timeout, description=func_name)
    if "error" in result:
        raise result["error"]
    return result.get("value")

OMR_MIN_VERSION = (0, 6, 0)
OMR_URL = "https://github.com/sorayuki/obs-multi-rtmp"

def is_module_loaded(dll_name="obs-multi-rtmp.dll"):

    return _module_handle(dll_name) is not None

def get_omr_version_from_obs_log():

    try:
        log_dir = os.path.join(config.get_obs_config_dir(), "logs")
        if not os.path.isdir(log_dir):
            return None
        files = sorted(
            glob.glob(os.path.join(log_dir, "*.txt"))
            + glob.glob(os.path.join(log_dir, "*.log")),
            key=lambda p: os.path.getmtime(p), reverse=True)
        for path in files[:3]:
            try:
                with open(path, "r", encoding="utf-8",
                          errors="replace") as f:
                    for i, line in enumerate(f):
                        m = re.search(
                            r"obs-multi-rtmp\]?\s*version:\s*([0-9.]+)",
                            line)
                        if m:
                            return m.group(1)
                        if i > 2000:
                            break
            except Exception:
                continue
    except Exception:
        pass
    return None

def _version_tuple(v):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])
    except Exception:
        return None

_DETECT_CACHE_TTL = 60.0
_detect_cache = {"ts": 0.0, "info": None}

def detect():

    now = time.time()
    cached = _detect_cache["info"]
    if cached is not None and now - _detect_cache["ts"] < _DETECT_CACHE_TTL:
        return dict(cached)
    loaded = is_module_loaded()
    version = get_omr_version_from_obs_log()
    ver_tuple = _version_tuple(version) if version else None

    if loaded and (not version or (ver_tuple and ver_tuple >= OMR_MIN_VERSION)):
        state = "ok"
    elif loaded and ver_tuple and ver_tuple < OMR_MIN_VERSION:
        state = "old_version"
    else:
        state = "not_found"

    info = {
        "loaded": loaded,
        "version": version,
        "state": state,
        "url": OMR_URL,
    }
    _detect_cache["info"] = info
    _detect_cache["ts"] = now
    return dict(info)

def _amf_string(s):
    b = s.encode("utf-8")
    return b"\x02" + struct.pack(">H", len(b)) + b

def _amf_number(n):
    return b"\x00" + struct.pack(">d", float(n))

def _amf_bool(v):
    return b"\x01" + (b"\x01" if v else b"\x00")

def _amf_null():
    return b"\x05"

def _amf_object(d):
    out = b"\x03"
    for key, val in d.items():
        kb = key.encode("utf-8")
        out += struct.pack(">H", len(kb)) + kb + _amf_value(val)
    return out + b"\x00\x00\x09"

def _amf_value(v):
    if v is None:
        return _amf_null()
    if isinstance(v, bool):
        return _amf_bool(v)
    if isinstance(v, (int, float)):
        return _amf_number(v)
    if isinstance(v, str):
        return _amf_string(v)
    if isinstance(v, dict):
        return _amf_object(v)
    raise TypeError(f"AMF: неподдерживаемый тип {type(v)}")

def _amf_decode(buf, pos):

    if pos >= len(buf):
        raise ValueError("AMF: нет данных")
    t = buf[pos]
    if t == 0x00:
        return struct.unpack(">d", buf[pos + 1:pos + 9])[0], pos + 9
    if t == 0x01:
        return buf[pos + 1] == 1, pos + 2
    if t == 0x02:
        n = struct.unpack(">H", buf[pos + 1:pos + 3])[0]
        return (buf[pos + 3:pos + 3 + n].decode("utf-8", "replace"),
                pos + 3 + n)
    if t in (0x03, 0x08):
        pos += 1 if t == 0x03 else 5
        obj = {}
        while True:
            if buf[pos:pos + 3] == b"\x00\x00\x09":
                return obj, pos + 3
            n = struct.unpack(">H", buf[pos:pos + 2])[0]
            key = buf[pos + 2:pos + 2 + n].decode("utf-8", "replace")
            val, pos = _amf_decode(buf, pos + 2 + n)
            obj[key] = val
    if t in (0x05, 0x06):
        return None, pos + 1
    if t == 0x0B:
        return struct.unpack(">d", buf[pos + 1:pos + 9])[0], pos + 10
    raise ValueError(f"AMF: тип {t:#x} не поддерживается")

def _amf_decode_all(buf):
    vals, pos = [], 0
    while pos < len(buf):
        try:
            v, pos = _amf_decode(buf, pos)
        except Exception:
            break
        vals.append(v)
    return vals

def _be24(buf, pos):
    return (buf[pos] << 16) | (buf[pos + 1] << 8) | buf[pos + 2]

class _ChunkReader:

    def __init__(self, sock, should_stop=None):
        self._sock = sock
        self._buf = b""
        self._streams = {}
        self.in_chunk_size = 128
        self._should_stop = should_stop or (lambda: False)

    def _fill(self):

        if self._should_stop():
            return False
        try:
            chunk = self._sock.recv(65536)
        except socket.timeout:
            return True
        except OSError:
            return False
        if not chunk:
            return False
        self._buf += chunk
        return True

    def _need(self, n):
        while len(self._buf) < n:
            if not self._fill():
                return False
        return True

    def _take(self, n):
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def next_message(self):
        while not self._should_stop():
            if not self._need(1):
                return None
            b0 = self._buf[0]
            fmt = (b0 >> 6) & 0x03
            csid = b0 & 0x3F
            if csid == 0:
                if not self._need(2):
                    return None
                csid = self._buf[1] + 64
                self._take(2)
            elif csid == 1:
                if not self._need(3):
                    return None
                csid = struct.unpack("<H", self._buf[1:3])[0] + 64
                self._take(3)
            else:
                self._take(1)

            st = self._streams.get(csid)
            if fmt == 0:
                if not self._need(11):
                    return None
                hdr = self._take(11)
                ts = _be24(hdr, 0)
                length = _be24(hdr, 3)
                mtype = hdr[6]
                msid = struct.unpack("<I", hdr[7:11])[0]
                if ts == 0xFFFFFF:
                    if not self._need(4):
                        return None
                    self._take(4)
                st = {"len": length, "mtype": mtype, "msid": msid,
                      "buf": b""}
                self._streams[csid] = st
            elif fmt == 1:
                if not self._need(7):
                    return None
                hdr = self._take(7)
                delta = _be24(hdr, 0)
                length = _be24(hdr, 3)
                mtype = hdr[6]
                if delta == 0xFFFFFF:
                    if not self._need(4):
                        return None
                    self._take(4)
                if st is None:
                    st = {"len": length, "mtype": mtype, "msid": 0,
                          "buf": b""}
                    self._streams[csid] = st
                else:
                    st["len"] = length
                    st["mtype"] = mtype
                    st["buf"] = b""
            elif fmt == 2:
                if not self._need(3):
                    return None
                hdr = self._take(3)
                delta = _be24(hdr, 0)
                if delta == 0xFFFFFF:
                    if not self._need(4):
                        return None
                    self._take(4)
                if st is None:
                    st = {"len": 0, "mtype": 0, "msid": 0, "buf": b""}
                    self._streams[csid] = st
                else:
                    st["buf"] = b""
            else:
                if st is None:
                    st = {"len": 0, "mtype": 0, "msid": 0, "buf": b""}
                    self._streams[csid] = st

            if st["len"] == 0:
                continue

            take = min(self.in_chunk_size, st["len"] - len(st["buf"]))
            if take > 0:
                if not self._need(take):
                    return None
                st["buf"] += self._take(take)

            if len(st["buf"]) >= st["len"]:
                payload = st["buf"]
                st["buf"] = b""
                return st["mtype"], st["msid"], payload
        return None

class RtmpSinkServer:

    def __init__(self, host="127.0.0.1"):
        self._host = host
        self._server_sock = None
        self._port = None
        self._stop = threading.Event()
        self._accept_thread = None
        self._client_threads = []
        self._lock = threading.Lock()
        self._publishing = 0

    def start(self):

        if self._server_sock is not None:
            return self.url
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self._host, 0))
            sock.listen(4)
        except OSError as e:
            sock.close()
            raise OmrBridgeError(f"RTMP sink: не удалось занять порт: {e}")
        self._server_sock = sock
        self._port = sock.getsockname()[1]
        self._stop.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="rtmp-sink-accept")
        self._accept_thread.start()
        config.log.info(f"RTMP sink слушает {self.url}")
        return self.url

    def stop(self):
        self._stop.set()
        sock = self._server_sock
        self._server_sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        for t in list(self._client_threads):
            if t.is_alive():
                t.join(timeout=2.0)
        self._client_threads.clear()
        self._port = None
        with self._lock:
            self._publishing = 0

    @property
    def url(self):
        return f"rtmp://{self._host}:{self._port}/sink" if self._port else None

    @property
    def port(self):
        return self._port

    def is_publishing(self):
        with self._lock:
            return self._publishing > 0

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client, _addr = self._server_sock.accept()
            except OSError:
                break
            if self._stop.is_set():
                try:
                    client.close()
                except Exception:
                    pass
                break
            t = threading.Thread(
                target=self._handle_client, args=(client,),
                daemon=True, name="rtmp-sink-client")
            self._client_threads.append(t)
            t.start()
            self._client_threads = [
                x for x in self._client_threads if x.is_alive()]

    def _handle_client(self, sock):
        published = False
        try:
            sock.settimeout(1.0)
            published = self._rtmp_session(sock)
        except Exception as e:
            config.log.debug(f"RTMP sink: сессия завершена: {e}")
        finally:
            if published:
                with self._lock:
                    self._publishing = max(0, self._publishing - 1)
            try:
                sock.close()
            except Exception:
                pass

    def _rtmp_session(self, sock):

        stop = self._stop.is_set

        def _read_exact(n):
            data = b""
            while len(data) < n:
                if stop():
                    return None
                try:
                    chunk = sock.recv(n - len(data))
                except socket.timeout:
                    continue
                except OSError:
                    return None
                if not chunk:
                    return None
                data += chunk
            return data

        c0c1 = _read_exact(1537)
        if c0c1 is None or c0c1[0] not in (0x03, 0x06):
            raise OmrBridgeError("RTMP sink: неожиданное рукопожатие")
        s1 = (struct.pack(">I", int(time.time()) & 0xFFFFFFFF)
              + b"\x00" * 4
              + bytes(random.getrandbits(8) for _ in range(1528)))
        sock.sendall(b"\x03" + s1 + c0c1[1:])
        if _read_exact(1536) is None:
            raise OmrBridgeError("RTMP sink: клиент не прислал C2")

        out_chunk_size = 4096
        self._send_message(sock, 2, 0x01, 0,
                           struct.pack(">I", out_chunk_size), out_chunk_size)

        reader = _ChunkReader(sock, should_stop=stop)
        published = False

        while not stop():
            msg = reader.next_message()
            if msg is None:
                break
            mtype, msid, payload = msg

            if mtype == 0x01 and len(payload) >= 4:
                new_size = struct.unpack(">I", payload[:4])[0] & 0x7FFFFFFF
                if new_size >= 1:
                    reader.in_chunk_size = new_size
                continue

            if mtype == 0x04 and len(payload) >= 6:
                event = struct.unpack(">H", payload[:2])[0]
                if event == 0x0006:
                    self._send_message(
                        sock, 2, 0x04, 0,
                        struct.pack(">H", 0x0007) + payload[2:6],
                        out_chunk_size)
                continue

            if mtype == 0x14:
                vals = _amf_decode_all(payload)
                if not vals or not isinstance(vals[0], str):
                    continue
                cmd = vals[0]
                txn = vals[1] if len(vals) > 1 and isinstance(
                    vals[1], (int, float)) else 0.0

                if cmd == "connect":
                    body = (_amf_string("_result") + _amf_number(txn)
                            + _amf_null()
                            + _amf_object({"fmsVer": "FMS/3,5,7,7009",
                                           "capabilities": 31.0}))
                    self._send_message(sock, 3, 0x14, 0, body,
                                       out_chunk_size)

                elif cmd == "createStream":
                    body = (_amf_string("_result") + _amf_number(txn)
                            + _amf_null() + _amf_number(1.0))
                    self._send_message(sock, 3, 0x14, 0, body,
                                       out_chunk_size)

                elif cmd == "publish":
                    info = {"level": "status",
                            "code": "NetStream.Publish.Start",
                            "description": "Connection succeeded."}
                    body = (_amf_string("onStatus") + _amf_number(0.0)
                            + _amf_null() + _amf_object(info))
                    self._send_message(sock, 3, 0x14, 1, body,
                                       out_chunk_size)
                    if not published:
                        published = True
                        with self._lock:
                            self._publishing += 1
                        config.log.info(
                            "RTMP sink: основной выход OBS подключился "
                            "(publish принят)")

                elif cmd in ("releaseStream", "FCPublish", "FCUnpublish",
                             "deleteStream", "call", "checkBandwidth"):
                    body = (_amf_string("_result") + _amf_number(txn)
                            + _amf_null() + _amf_null())
                    self._send_message(sock, 3, 0x14, 0, body,
                                       out_chunk_size)

        return published

    def _send_message(self, sock, csid, mtype, msid, payload,
                      chunk_size=128):

        if csid >= 64:
            csid = 3
        hdr = bytes([csid & 0x3F])
        hdr += struct.pack(">I", 0)[1:]
        hdr += struct.pack(">I", len(payload))[1:]
        hdr += bytes([mtype])
        hdr += struct.pack("<I", msid)
        out = [hdr]
        for i in range(0, len(payload), chunk_size):
            if i:
                out.append(bytes([0xC0 | (csid & 0x3F)]))
            out.append(payload[i:i + chunk_size])
        try:
            sock.sendall(b"".join(out))
        except OSError as e:
            raise OmrBridgeError(f"RTMP sink: отправка не удалась: {e}")

def _atomic_write(path, data_bytes):
    tmp = path + ".msv3tmp"
    with open(tmp, "wb") as f:
        f.write(data_bytes)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def _write_json(path, obj):
    _atomic_write(path, json.dumps(
        obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

def ini_patch(path, section, key, value):

    if not os.path.isfile(path):
        return False
    with open(path, "r", encoding="utf-8", errors="surrogateescape",
              newline="") as f:
        lines = f.readlines()

    eol = "\r\n" if any(l.endswith("\r\n") for l in lines) else "\n"

    def _key_line(newval):
        return f"{key}={newval}{eol}"

    in_section = False
    found = False
    out = []
    pat = re.compile(r"^\s*" + re.escape(key) + r"\s*=", re.IGNORECASE)

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section and not found and value is not None:
                out.append(_key_line(value))
                found = True
            in_section = stripped[1:-1].strip().lower() == section.lower()
            out.append(line)
            continue
        if in_section and pat.match(line):
            found = True
            if value is None:
                continue
            out.append(_key_line(value))
            continue
        out.append(line)

    if in_section and not found and value is not None:
        out.append(_key_line(value))

    new_text = "".join(out)
    with open(path, "r", encoding="utf-8", errors="surrogateescape",
              newline="") as f:
        old_text = f.read()
    if new_text == old_text:
        return False
    _atomic_write(path, new_text.encode("utf-8", errors="surrogateescape"))
    return True

def ini_get(path, section, key):

    if not os.path.isfile(path):
        return None
    try:
        in_section = False
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    in_section = (stripped[1:-1].strip().lower()
                                  == section.lower())
                    continue
                if in_section:
                    m = re.match(r"^\s*([^=]+?)\s*=\s*(.*)$", line)
                    if m and m.group(1).strip().lower() == key.lower():
                        return m.group(2).strip()
    except Exception:
        pass
    return None

class SessionBackup:

    FILES = ("service.json", "streamEncoder.json", "basic.ini",
             "obs-multi-rtmp.json")
    MARKER = "session.json"

    def __init__(self, backup_dir):
        self.dir = backup_dir

    def exists(self):
        if os.path.isfile(os.path.join(self.dir, self.MARKER)):
            return True
        return os.path.isdir(self.dir) and any(
            os.path.isfile(os.path.join(self.dir, n)) for n in self.FILES)

    def save(self, profile_dir):

        os.makedirs(self.dir, exist_ok=True)
        for name in self.FILES:
            src = os.path.join(profile_dir, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(self.dir, name))
        _write_json(os.path.join(self.dir, self.MARKER), {
            "profile_dir": profile_dir,
            "saved_at": time.time(),
        })
        config.log.info(f"Бэкап файлов профиля: {self.dir}")

    def restore(self, profile_dir):

        restored = []
        for name in self.FILES:
            src = os.path.join(self.dir, name)
            dst = os.path.join(profile_dir, name)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
                restored.append(name)
            elif os.path.isfile(dst) and name in ("service.json",
                                                  "streamEncoder.json"):
                try:
                    os.remove(dst)
                    restored.append(name + " (удалён)")
                except Exception:
                    pass
        config.log.info(f"Файлы профиля восстановлены: {restored}")
        return restored

    def clear(self):
        if os.path.isdir(self.dir):
            shutil.rmtree(self.dir, ignore_errors=True)

TARGET_ID = {"twitch": "msv3-twitch", "youtube": "msv3-youtube"}
def audio_config_id(platform):
    return f"msv3-ac-{platform}"

LEGACY_AUDIO_CONFIG_ID = "msv3-ac"

def omr_config_path(profile_dir):
    return os.path.join(profile_dir, "obs-multi-rtmp.json")

def read_omr_config(profile_dir):
    path = omr_config_path(profile_dir)
    if not os.path.isfile(path):
        return {"targets": [], "video_configs": [], "audio_configs": []}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        config.log.warning(f"obs-multi-rtmp.json не читается: {e}")
        return {"targets": [], "video_configs": [], "audio_configs": []}
    for key in ("targets", "video_configs", "audio_configs"):
        if not isinstance(data.get(key), list):
            data[key] = []
    return data

def _upsert_by_id(items, item):
    for i, existing in enumerate(items):
        if isinstance(existing, dict) and existing.get("id") == item["id"]:
            items[i] = item
            return
    items.append(item)

def write_omr_targets(profile_dir, platforms):

    cfg = read_omr_config(profile_dir)
    targets = [t for t in cfg["targets"] if isinstance(t, dict)]
    video_configs = [v for v in cfg["video_configs"]
                     if isinstance(v, dict) and v.get("id")]
    audio_configs = [a for a in cfg["audio_configs"]
                     if isinstance(a, dict) and a.get("id")]

    our_ids = set(TARGET_ID.values())

    for t in targets:
        if t.get("id") not in our_ids:
            if t.get("sync-start") or t.get("sync-stop"):
                config.log.info(
                    "obs-multi-rtmp: цель '%s' не наша — выключаю "
                    "sync-start/sync-stop (запуск вручную из его дока)"
                    % t.get("name", t.get("id")))
            t["sync-start"] = False
            t["sync-stop"] = False

    for platform, target_id in TARGET_ID.items():
        info = platforms.get(platform)
        if not info:
            continue
        bitrate = int(info.get("bitrate", 6000))
        audio_bitrate = int(info.get("audio_bitrate",
                                     config.DEFAULT_AUDIO_BITRATE))
        preset = info.get("preset") or config.DEFAULT_X264_PRESET
        audio_track = int(info.get("audio_track", 1) or 1)
        audio_track = max(1, min(config.AUDIO_TRACKS_MAX, audio_track))

        target = {
            "id": target_id,
            "name": info.get("name") or platform.capitalize(),
            "protocol": "RTMP",
            "sync-start": True,
            "sync-stop": True,
            "service-param": {
                "server": info["server"],
                "key": info["key"],
            },
            "output-param": {
                "bind_ip": "default",
                "drop_threshold_ms": 700,
                "pframe_drop_threshold_ms": 900,
                "max_shutdown_time_sec": 30,
            },
            "video-config": f"msv3-vc-{platform}",
            "audio-config": audio_config_id(platform),
        }
        _upsert_by_id(targets, target)

        video = {
            "id": f"msv3-vc-{platform}",
            "encoder": "obs_x264",
            "param": {
                "bitrate": bitrate,
                "rate_control": "CBR",
                "keyint_sec": config.RTMP_KEYINT_SEC,
                "preset": preset,
                "profile": "high",
            },
            "fps-denumerator": 1,
        }
        _upsert_by_id(video_configs, video)

        audio = {
            "id": audio_config_id(platform),
            "encoder": "ffmpeg_aac",
            "param": {"bitrate": audio_bitrate},
            "mixerId": audio_track - 1,
        }
        _upsert_by_id(audio_configs, audio)

    audio_configs = [a for a in audio_configs
                     if a.get("id") != LEGACY_AUDIO_CONFIG_ID]

    cfg["targets"] = targets
    cfg["video_configs"] = video_configs
    cfg["audio_configs"] = audio_configs
    _write_json(omr_config_path(profile_dir), cfg)
    tracks = ", ".join(
        f"{p}: дорожка {platforms[p].get('audio_track', 1)}"
        for p in sorted(TARGET_ID) if platforms.get(p))
    config.log.info(
        "obs-multi-rtmp.json: цели %s готовы (sync-start/sync-stop; "
        "звук — %s)"
        % (", ".join(sorted(TARGET_ID.values())), tracks))

def strip_profile_auth_gate(profile_dir):

    basic_ini = os.path.join(profile_dir, "basic.ini")
    auth_type = ini_get(basic_ini, "Auth", "Type")
    if not auth_type or "youtube" not in auth_type.lower():
        return False
    if ini_patch(basic_ini, "Auth", "Type", None):
        config.log.info(
            f"basic.ini: [Auth] Type='{auth_type}' временно убран — "
            "обход гейта OBS «Трансляция не настроена» (YouTube-вход). "
            "Вернётся из бэкапа после эфира")
        return True
    return False

def patch_profile_for_sink(profile_dir, sink_url):

    _write_json(os.path.join(profile_dir, "service.json"), {
        "type": "rtmp_custom",
        "settings": {"server": sink_url, "key": "sink"},
    })

    basic_ini = os.path.join(profile_dir, "basic.ini")
    mode = (ini_get(basic_ini, "Output", "Mode") or "Simple").lower()
    encoder = ini_get(basic_ini, "AdvOut", "Encoder")

    if mode != "advanced":
        ini_patch(basic_ini, "SimpleOut", "VBitrate",
                  str(config.SINK_MAIN_BITRATE))
        config.log.info(
            f"Основной выход (Simple): VBitrate → {config.SINK_MAIN_BITRATE} "
            "kbps на время эфира")
    elif encoder and encoder != "obs_x264":
        config.log.info(
            f"Основной выход (Advanced, {encoder}): кодер не трогаю — "
            "аппаратный, почти не грузит CPU")
    else:
        _write_json(os.path.join(profile_dir, "streamEncoder.json"), {
            "bitrate": config.SINK_MAIN_BITRATE,
            "rate_control": "CBR",
            "keyint_sec": 2,
            "preset": "ultrafast",
            "profile": "high",
        })
        config.log.info(
            f"Основной выход (Advanced, x264): ultrafast / "
            f"{config.SINK_MAIN_BITRATE} kbps на время эфира")

def _wait_profile(name, timeout=15.0):

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _fe("obs_frontend_get_current_profile") == name:
                return True
        except Exception:
            return False
        time.sleep(0.2)
    return False

def profile_switch_patch(patch_fn, log_prefix=""):

    if _fe("obs_frontend_streaming_active"):
        raise OmrBridgeError(
            "Нельзя перезагружать цели obs-multi-rtmp во время стрима")
    if _fe("obs_frontend_recording_active"):
        raise OmrBridgeError(
            "Смена профиля при активной записи небезопасна — остановите "
            "запись")

    original = _fe("obs_frontend_get_current_profile")
    original_dir = _fe("obs_frontend_get_current_profile_path")
    if not original or not original_dir:
        raise OmrBridgeError("Не удалось получить текущий профиль OBS")
    original_dir = str(original_dir).replace("\\", "/").rstrip("/")
    config.log.info(f"{log_prefix}Профиль: '{original}' → {original_dir}")

    profiles = _fe("obs_frontend_get_profiles") or []
    base = f"msv3 tmp {random.randint(1000, 9999)}"
    temp_name = base
    attempt = 0
    while temp_name in profiles and attempt < 20:
        attempt += 1
        temp_name = f"{base} {attempt}"

    _fe("obs_frontend_duplicate_profile", temp_name, timeout=60)
    if not _wait_profile(temp_name, timeout=20):
        try:
            _fe("obs_frontend_set_current_profile", temp_name, timeout=30)
        except Exception as e:
            config.log.warning(f"set_current_profile({temp_name}): {e}")
        if not _wait_profile(temp_name, timeout=15):
            raise OmrBridgeError(
                "OBS не переключился на временный профиль — не удалось "
                "перечитать конфиг obs-multi-rtmp")
    time.sleep(0.5)

    try:
        patch_fn(original_dir)
    finally:
        _fe("obs_frontend_set_current_profile", original, timeout=30)
        if not _wait_profile(original, timeout=20):
            raise OmrBridgeError(
                f"OBS не вернулся на профиль '{original}'! Верните его "
                "вручную в меню Profile и сообщите об ошибке.")

    time.sleep(0.5)

    try:
        _fe("obs_frontend_delete_profile", temp_name, timeout=30)
    except Exception as e:
        config.log.warning(
            f"Временный профиль '{temp_name}' не удалён: {e} "
            "(удалите вручную в меню Profile)")
    return True

class OmrEngine:

    def __init__(self, backup_dir=None):
        self.sink = RtmpSinkServer()
        self.backup = SessionBackup(
            backup_dir or config.get_backup_dir())
        self.session_profile_dir = None
        self._start_pending = False

    @staticmethod
    def check_ready():

        info = detect()
        if info["state"] == "not_found":
            raise OmrBridgeError(
                "obs-multi-rtmp не загружен в OBS. Установите плагин "
                f"({OMR_URL}, версия 0.6+) и перезапустите OBS. Он должен "
                "появиться в меню Docks («Multiple RTMP outputs»).")
        if info["state"] == "old_version":
            raise OmrBridgeError(
                f"obs-multi-rtmp {info['version']} слишком старый: нужен "
                f"0.6+ (новый формат конфига). Обновите: {OMR_URL}")
        return info

    def prepare(self, platforms):

        info = self.check_ready()
        config.log.info(
            f"obs-multi-rtmp найден (версия {info.get('version') or '?'})")

        sink_url = self.sink.start()

        profile_dir = str(
            _fe("obs_frontend_get_current_profile_path")) \
            .replace("\\", "/").rstrip("/")
        self.session_profile_dir = profile_dir
        self.backup.save(profile_dir)

        def _patch(pdir):
            write_omr_targets(pdir, platforms)
            patch_profile_for_sink(pdir, sink_url)
            strip_profile_auth_gate(pdir)

        profile_switch_patch(_patch, log_prefix="Подготовка эфира: ")
        return {"sink_url": sink_url, "omr": info}

    def go_live(self, wait_publish_timeout=30.0, wait_start_timeout=15.0):

        if _fe("obs_frontend_streaming_active"):
            raise OmrBridgeError(
                "Основной стрим OBS уже активен — сначала остановите его")
        self._start_pending = True
        _fe("obs_frontend_streaming_start", timeout=30)

        became_active = False
        deadline = time.time() + wait_start_timeout
        while time.time() < deadline:
            if self.sink.is_publishing():
                self._start_pending = False
                return True
            try:
                if _fe("obs_frontend_streaming_active"):
                    became_active = True
                    break
            except Exception:
                break
            time.sleep(0.1)

        if not became_active:
            raise OmrBridgeError(
                "Основной выход OBS не стал активным за "
                f"{wait_start_timeout:.0f} с после команды старта — "
                "смотрите лог OBS (диалог «Трансляция не настроена», "
                "ошибка кодера или недоступный сервер)")

        deadline = time.time() + wait_publish_timeout
        while time.time() < deadline:
            if self.sink.is_publishing():
                self._start_pending = False
                return True
            try:
                active = _fe("obs_frontend_streaming_active")
            except Exception:
                break
            if not active:
                self._start_pending = False
                raise OmrBridgeError(
                    "Основной выход OBS отключился, не подключившись к "
                    "локальному RTMP sink — смотрите лог OBS (обычно: "
                    "соединение сброшено или исчерпаны повторные попытки)")
            time.sleep(0.25)

        self._start_pending = False
        config.log.warning(
            f"RTMP sink: основной выход не прислал publish за "
            f"{wait_publish_timeout:.0f} с (продолжаю — цели obs-multi-rtmp "
            "уже стартовали по sync-start)")
        return True

    def go_down(self, wait_inactive_timeout=30.0, wait_start_settle=None):

        errors = []

        if self._start_pending:
            settle_timeout = (config.OMR_START_TIMEOUT
                              if wait_start_settle is None
                              else wait_start_settle)
            deadline = time.time() + settle_timeout
            settled = False
            while time.time() < deadline:
                try:
                    if (_fe("obs_frontend_streaming_active")
                            or self.sink.is_publishing()):
                        settled = True
                        break
                except Exception:
                    settled = True
                    break
                time.sleep(0.1)
            if not settled:
                config.log.warning(
                    "go_down: старт из неудавшегося go_live так и не "
                    "долетел до OBS — останавливать нечего (гейт "
                    "«Трансляция не настроена» или ошибка старта)")
        self._start_pending = False

        try:
            if (_fe("obs_frontend_streaming_active")
                    or self.sink.is_publishing()):
                _fe("obs_frontend_streaming_stop", timeout=30)
        except Exception as e:
            errors.append(f"streaming_stop: {e}")

        deadline = time.time() + wait_inactive_timeout
        stopped = False
        while time.time() < deadline:
            try:
                if not _fe("obs_frontend_streaming_active"):
                    stopped = True
                    break
            except Exception:
                stopped = True
                break
            time.sleep(0.5)
        if not stopped:
            errors.append("основной выход OBS не остановился вовремя")

        time.sleep(1.0)

        if self.session_profile_dir:
            try:
                current_dir = str(
                    _fe("obs_frontend_get_current_profile_path")) \
                    .replace("\\", "/").rstrip("/")
                profile_dir = (current_dir
                               if current_dir == self.session_profile_dir
                               else self.session_profile_dir)
                if profile_dir != current_dir:
                    config.log.warning(
                        "Профиль сменился во время эфира — восстанавливаю "
                        "файлы профиля, в котором была сессия")

                def _restore(pdir):
                    self.backup.restore(pdir)

                try:
                    profile_switch_patch(_restore,
                                         log_prefix="Завершение эфира: ")
                    self.backup.clear()
                except OmrBridgeError as e:
                    errors.append(str(e))
                    config.log.warning(
                        "Восстановление настроек отложено: " + str(e))
            except Exception as e:
                errors.append(f"restore: {e}")

        try:
            self.sink.stop()
        except Exception as e:
            errors.append(f"sink: {e}")

        self.session_profile_dir = None
        return errors

    def recover_if_needed(self):

        if not self.backup.exists():
            return False
        try:
            if _fe("obs_frontend_streaming_active"):
                config.log.warning(
                    "Найден бэкап прошлой сессии, но стрим активен — "
                    "восстановление отложено до остановки")
                return False
            config.log.warning(
                "Найден бэкап прошлой сессии (OBS упал во время эфира?) — "
                "восстанавливаю настройки профиля")

            def _restore(pdir):
                self.backup.restore(pdir)

            profile_switch_patch(_restore, log_prefix="Восстановление: ")
            self.backup.clear()
            return True
        except Exception as e:
            config.log.error(f"Восстановление не удалось: {e}")
            return False

    def status(self):

        try:
            streaming_active = bool(_fe("obs_frontend_streaming_active"))
        except Exception:
            streaming_active = False
        return {
            "omr": detect(),
            "streaming_active": streaming_active,
            "backup_pending": self.backup.exists(),
            "session_active": self.session_profile_dir is not None,
        }

_OMR_OUTPUT_NAMES = ("multi-output", "multi-output 2",
                     "multi-output 3", "multi-output 4")
_STATS_UNAVAILABLE_WARNED = False
_UNMATCHED_WARNED = False

def _output_platform(out):

    try:
        service = obs.obs_output_get_service(out)
        if not service:
            return None
        settings = obs.obs_service_get_settings(service)
        try:
            server = (obs.obs_data_get_string(settings, "server")
                      or "").lower()
            key = (obs.obs_data_get_string(settings, "key") or "").lower()
        finally:
            obs.obs_data_release(settings)
        url = f"{server} {key}"
        if "twitch" in url:
            return "twitch"
        if "youtube" in url:
            return "youtube"
        return None
    except Exception as e:
        config.log.debug(f"определение платформы выхода: {e}")
        return None

def collect_stream_output_stats():

    global _STATS_UNAVAILABLE_WARNED, _UNMATCHED_WARNED
    if obs is None or not hasattr(obs, "obs_get_output_by_name"):
        if not _STATS_UNAVAILABLE_WARNED:
            _STATS_UNAVAILABLE_WARNED = True
            config.log.warning(
                "obspython не экспортирует obs_get_output_by_name (нужен "
                "OBS 30.2+) — битрейт потоков в док-панели будет недоступен")
        return None
    per_platform = {}
    found_active = 0
    unmatched = []
    for name in _OMR_OUTPUT_NAMES:
        out = obs.obs_get_output_by_name(name)
        if out is None:
            continue
        try:
            if not obs.obs_output_active(out):
                continue
            found_active += 1
            platform = _output_platform(out)
            if platform and platform not in per_platform:
                per_platform[platform] = obs.obs_output_get_total_bytes(out)
            elif not platform:
                unmatched.append(name)
        except Exception as e:
            config.log.debug(f"статистика выхода {name}: {e}")
        finally:
            obs.obs_output_release(out)
    if found_active and not per_platform and not _UNMATCHED_WARNED:
        _UNMATCHED_WARNED = True
        config.log.warning(
            f"Активные выходы найдены (имена: {unmatched or _OMR_OUTPUT_NAMES}), "
            "но платформы по URL сервиса не распознаны — битрейт в доке не "
            "покажется. Сообщите эту строку разработчику.")
    return per_platform
