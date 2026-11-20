
import os
import re
import sys
import json
import glob
import time
import ctypes
import random
import shutil

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

_SIMPLE_ENCODER_MAP = {
    "x264": ("obs_x264", {"preset": "veryfast", "profile": "high",
                          "rate_control": "CBR"}),
    "nvenc": ("obs_nvenc_h264", {"preset": "p5", "profile": "high",
                                 "rate_control": "CBR"}),
    "qsv": ("obs_qsv11", {"target_usage": "balanced", "profile": "high",
                          "rate_control": "CBR"}),
    "amd": ("obs_amf_h264", {"preset": "balanced", "profile": "high",
                             "rate_control": "CBR"}),
}

def read_stream_encoder_config(profile_dir):

    basic_ini = os.path.join(profile_dir, "basic.ini")
    mode = (ini_get(basic_ini, "Output", "Mode") or "Simple").lower()

    if mode == "advanced":
        enc_id = (ini_get(basic_ini, "AdvOut", "Encoder") or "").strip()
        if not enc_id:
            config.log.warning(
                "basic.ini: [AdvOut] Encoder пуст — цель YouTube получит "
                "запасной obs_x264 (veryfast). Проверьте настройки вывода "
                "OBS (Settings → Output → Streaming → Encoder)")
            return None
        param = {}
        se_path = os.path.join(profile_dir, "streamEncoder.json")
        if os.path.isfile(se_path):
            try:
                with open(se_path, "r", encoding="utf-8-sig") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    param = loaded
                else:
                    config.log.warning(
                        "streamEncoder.json не является объектом — "
                        "использую только битрейт")
            except Exception as e:
                config.log.warning(
                    f"streamEncoder.json не читается: {e} — цель YouTube "
                    "получит параметры по умолчанию кодера")
        else:
            config.log.warning(
                "streamEncoder.json не найден — цель YouTube получит "
                "параметры по умолчанию кодера " + enc_id)
        return {"encoder": enc_id, "param": dict(param),
                "mode": "advanced"}

    venc = (ini_get(basic_ini, "SimpleOut", "VEncoder")
            or "x264").strip().lower()
    if venc in _SIMPLE_ENCODER_MAP:
        enc_id, param = _SIMPLE_ENCODER_MAP[venc]
        if venc != "x264":
            config.log.info(
                f"Simple-режим: кодер '{venc}' -> {enc_id} (типовые "
                "параметры; для полного контроля параметров цели "
                "переключите вывод OBS на Advanced)")
        return {"encoder": enc_id, "param": dict(param), "mode": "simple"}

    config.log.warning(
        f"Simple-режим: неизвестный VEncoder '{venc}' — цель YouTube "
        "получит запасной obs_x264 (veryfast)")
    return None

def write_omr_targets(profile_dir, platforms):

    warnings = []
    cfg = read_omr_config(profile_dir)
    targets = [t for t in cfg["targets"] if isinstance(t, dict)]
    video_configs = [v for v in cfg["video_configs"]
                     if isinstance(v, dict) and v.get("id")]
    audio_configs = [a for a in cfg["audio_configs"]
                     if isinstance(a, dict) and a.get("id")]

    legacy_ids = {TARGET_ID["twitch"], f"msv3-vc-twitch",
                  audio_config_id("twitch")}
    if any(t.get("id") in legacy_ids for t in targets):
        config.log.info(
            "obs-multi-rtmp.json: удаляю устаревшую цель msv3-twitch "
            "(Twitch теперь основной выход OBS)")
    targets = [t for t in targets if t.get("id") not in legacy_ids]
    video_configs = [v for v in video_configs
                     if v.get("id") not in legacy_ids]
    audio_configs = [a for a in audio_configs
                     if a.get("id") not in legacy_ids]

    our_ids = {TARGET_ID["youtube"]}

    for t in targets:
        if t.get("id") not in our_ids:
            if t.get("sync-start") or t.get("sync-stop"):
                config.log.info(
                    "obs-multi-rtmp: цель '%s' не наша — выключаю "
                    "sync-start/sync-stop (запуск вручную из его дока)"
                    % t.get("name", t.get("id")))
            t["sync-start"] = False
            t["sync-stop"] = False

    user_enc = read_stream_encoder_config(profile_dir)

    info = platforms.get("youtube")
    if info:
        bitrate = int(info.get("bitrate", 6000))
        audio_bitrate = int(info.get("audio_bitrate",
                                     config.DEFAULT_AUDIO_BITRATE))
        audio_track = int(info.get("audio_track", 1) or 1)
        audio_track = max(1, min(config.AUDIO_TRACKS_MAX, audio_track))

        target = {
            "id": TARGET_ID["youtube"],
            "name": info.get("name") or "YouTube",
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
            "video-config": f"msv3-vc-youtube",
            "audio-config": audio_config_id("youtube"),
        }
        _upsert_by_id(targets, target)

        if user_enc:
            param = dict(user_enc["param"])
            encoder_id = user_enc["encoder"]
            param["bitrate"] = bitrate
            param.setdefault("keyint_sec", config.RTMP_KEYINT_SEC)
            if not param.get("rate_control"):
                param["rate_control"] = "CBR"
            config.log.info(
                f"Цель YouTube: кодер {encoder_id}, параметры — из "
                f"настроек вывода OBS ({user_enc['mode']}), битрейт "
                f"перезаписан на {bitrate} kbps")
        else:
            encoder_id = "obs_x264"
            param = {
                "bitrate": bitrate,
                "rate_control": "CBR",
                "keyint_sec": config.RTMP_KEYINT_SEC,
                "preset": config.DEFAULT_X264_PRESET,
                "profile": "high",
            }
            warnings.append(
                "Кодер стрима OBS не определён в профиле — цель YouTube "
                "получила запасной obs_x264, он грузит CPU: выберите кодер "
                "в Settings → Output → Streaming (Encoder)")
            config.log.warning(warnings[-1])
            config.log.info(
                f"Цель YouTube: запасной obs_x264 "
                f"({config.DEFAULT_X264_PRESET}) @ {bitrate} kbps")

        video = {
            "id": "msv3-vc-youtube",
            "encoder": encoder_id,
            "param": param,
            "fps-denumerator": 1,
        }
        _upsert_by_id(video_configs, video)

        audio = {
            "id": audio_config_id("youtube"),
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
    track = (info or {}).get("audio_track", 1)
    config.log.info(
        "obs-multi-rtmp.json: цель YouTube готова (sync-start/sync-stop; "
        f"звук — дорожка {track})")
    return warnings

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

def patch_profile_for_twitch(profile_dir, server, key, bitrate,
                             audio_track=1):

    warnings = []
    _write_json(os.path.join(profile_dir, "service.json"), {
        "type": "rtmp_custom",
        "settings": {"server": server, "key": key},
    })

    basic_ini = os.path.join(profile_dir, "basic.ini")
    mode = (ini_get(basic_ini, "Output", "Mode") or "Simple").lower()

    if mode != "advanced":
        ini_patch(basic_ini, "SimpleOut", "VBitrate", str(bitrate))
        if audio_track != 1:
            warnings.append(
                f"Simple-режим OBS стримит один общий микс звука: дорожка "
                f"{audio_track} для Twitch не применится — переключите "
                "вывод OBS в Advanced для раздельных дорожек")
            config.log.warning(warnings[-1])
        config.log.info(
            f"Основной выход (Simple) → Twitch: VBitrate {bitrate} kbps "
            "на время эфира")
    else:
        se_path = os.path.join(profile_dir, "streamEncoder.json")
        param = {}
        if os.path.isfile(se_path):
            try:
                with open(se_path, "r", encoding="utf-8-sig") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    param = loaded
            except Exception as e:
                config.log.warning(
                    f"streamEncoder.json не читается: {e} — битрейт "
                    "основного выхода не перезаписан")
        if param:
            param["bitrate"] = bitrate
            _write_json(se_path, param)
            encoder = ini_get(basic_ini, "AdvOut", "Encoder") or "?"
            config.log.info(
                f"Основной выход (Advanced, {encoder}) → Twitch: параметры "
                f"кодера пользователя, битрейт перезаписан на {bitrate} kbps")
        if ini_patch(basic_ini, "AdvOut", "TrackIndex", str(audio_track)):
            config.log.info(
                f"Основной выход (Advanced): дорожка звука стрима → "
                f"{audio_track}")
    return warnings

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

        profile_dir = str(
            _fe("obs_frontend_get_current_profile_path")) \
            .replace("\\", "/").rstrip("/")
        self.session_profile_dir = profile_dir
        self.backup.save(profile_dir)

        tw = platforms.get("twitch") or {}
        collected = {}

        def _patch(pdir):
            warnings = write_omr_targets(pdir, platforms)
            warnings += patch_profile_for_twitch(
                pdir,
                server=tw.get("server") or config.DEFAULT_TWITCH_RTMP_URL,
                key=tw.get("key") or "",
                bitrate=int(tw.get("bitrate", config.DEFAULT_TWITCH_BITRATE)),
                audio_track=int(tw.get("audio_track", 1) or 1),
            )
            strip_profile_auth_gate(pdir)
            collected["warnings"] = warnings

        profile_switch_patch(_patch, log_prefix="Подготовка эфира: ")
        return {"omr": info, "warnings": collected.get("warnings", [])}

    def go_live(self, wait_bytes_timeout=30.0, wait_start_timeout=15.0):

        if _fe("obs_frontend_streaming_active"):
            raise OmrBridgeError(
                "Основной стрим OBS уже активен — сначала остановите его")
        self._start_pending = True
        _fe("obs_frontend_streaming_start", timeout=30)

        became_active = False
        deadline = time.time() + wait_start_timeout
        while time.time() < deadline:
            try:
                if _fe("obs_frontend_streaming_active"):
                    became_active = True
                    break
            except Exception:
                break
            time.sleep(0.1)

        if not became_active:
            self._start_pending = False
            raise OmrBridgeError(
                "Основной выход OBS не стал активным за "
                f"{wait_start_timeout:.0f} с после команды старта — "
                "смотрите лог OBS (диалог «Трансляция не настроена», "
                "ошибка кодера или недоступный сервер Twitch)")

        deadline = time.time() + wait_bytes_timeout
        flowing = False
        while time.time() < deadline:
            try:
                if not _fe("obs_frontend_streaming_active"):
                    self._start_pending = False
                    raise OmrBridgeError(
                        "Основной выход OBS отключился сразу после старта — "
                        "смотрите лог OBS (соединение с Twitch сброшено "
                        "или исчерпаны повторные попытки)")
                sent = main_stream_bytes()
                if sent is None:
                    flowing = True
                    break
                if sent > 0:
                    flowing = True
                    break
            except OmrBridgeError:
                raise
            except Exception:
                flowing = True
                break
            time.sleep(0.5)

        self._start_pending = False
        if not flowing:
            config.log.warning(
                f"Twitch-ингест не подтвердил передачу данных за "
                f"{wait_bytes_timeout:.0f} с — проверьте лог OBS (цель "
                "YouTube уже стартовала по sync-start)")
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
                    if _fe("obs_frontend_streaming_active"):
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
            if _fe("obs_frontend_streaming_active"):
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
_MAIN_OUTPUT_NAMES = ("adv_stream", "simple_stream")
_STATS_UNAVAILABLE_WARNED = False
_UNMATCHED_WARNED = False

def _run_on_main_task(fn, timeout=30.0):

    if _run_on_main is _default_dispatcher:
        return fn()
    result = {}

    def _task():
        try:
            result["value"] = fn()
        except Exception as e:
            result["error"] = e

    _run_on_main(_task, timeout=timeout, description=fn.__name__)
    if "error" in result:
        raise result["error"]
    return result.get("value")

def _main_stream_bytes():

    if obs is None or not hasattr(obs, "obs_get_output_by_name"):
        return None
    for name in _MAIN_OUTPUT_NAMES:
        out = obs.obs_get_output_by_name(name)
        if out is None:
            continue
        try:
            if obs.obs_output_active(out):
                return obs.obs_output_get_total_bytes(out)
        except Exception as e:
            config.log.debug(f"байты выхода {name}: {e}")
        finally:
            obs.obs_output_release(out)
    return 0

def main_stream_bytes():

    return _run_on_main_task(_main_stream_bytes)

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

    def _collect():
        per_platform = {}
        found_active = 0
        unmatched = []
        for name in _OMR_OUTPUT_NAMES + _MAIN_OUTPUT_NAMES:
            out = obs.obs_get_output_by_name(name)
            if out is None:
                continue
            try:
                if not obs.obs_output_active(out):
                    continue
                found_active += 1
                platform = _output_platform(out)
                if platform and platform not in per_platform:
                    per_platform[platform] = \
                        obs.obs_output_get_total_bytes(out)
                elif not platform:
                    unmatched.append(name)
            except Exception as e:
                config.log.debug(f"статистика выхода {name}: {e}")
            finally:
                obs.obs_output_release(out)
        return per_platform, found_active, unmatched

    try:
        per_platform, found_active, unmatched = \
            _run_on_main_task(_collect)
    except Exception as e:
        config.log.debug(f"статистика выходов: {e}")
        return None

    if found_active and not per_platform and not _UNMATCHED_WARNED:
        _UNMATCHED_WARNED = True
        config.log.warning(
            f"Активные выходы найдены (имена: {unmatched or _OMR_OUTPUT_NAMES}), "
            "но платформы по URL сервиса не распознаны — битрейт в доке не "
            "покажется. Сообщите эту строку разработчику.")
    return per_platform
