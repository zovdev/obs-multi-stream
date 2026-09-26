
import os
import re
import sys
import struct
import configparser

import config
import secret_store

def _nibbles_from_hash(val):

    return [(val >> (4 * i)) & 0xF for i in range(16)]

def _hash_from_nibbles(nibbles):

    val = 0
    for i in range(16):
        val |= (nibbles[i] & 0xF) << (4 * i)
    return val

def _obs_deobfuscate(data, val):

    nib = _nibbles_from_hash(val)
    return bytes(b ^ nib[i % 16] for i, b in enumerate(data))

def _obs_obfuscate(data, val):

    nib = _nibbles_from_hash(val)
    return bytes(b ^ nib[i % 16] for i, b in enumerate(data))

_GOOGLE_SUFFIX = b".apps.googleusercontent.com"
_GOOGLE_PREFIX_RE = re.compile(
    rb"^[0-9]{6,16}-[A-Za-z0-9_-]{10,64}\.apps\.googleusercontent\.com$")
_SECRET_PREFIX = b"GOCSPX-"
_SECRET_RE = re.compile(rb"^GOCSPX-[A-Za-z0-9_-]{20,45}$")
_OLD_SECRET_RE = re.compile(rb"^[A-Za-z0-9_-]{24}$")
_OLD_SECRET_NEAR_RE = re.compile(rb"^[A-Za-z0-9_-]{20,45}$")

_RUN_RE = re.compile(rb"[\x20-\x7f]{16,96}")
_MOVABS_RE = re.compile(rb"[\x48-\x4f][\xb8-\xbf]")

_OBS_BINARY_NAMES = ("obs64.exe", "obs.exe", "obs32.exe", "obs")

def _looks_like_obs_binary(path):
    if not path or not os.path.isfile(path):
        return False
    return os.path.basename(path).lower() in _OBS_BINARY_NAMES

def find_obs_binary():

    try:
        exe = sys.executable
        if _looks_like_obs_binary(exe):
            return exe
    except Exception:
        pass

    candidates = []

    try:
        d = os.path.dirname(os.path.abspath(__file__))
        for _ in range(5):
            parent = os.path.dirname(d)
            if parent == d:
                break
            candidates += [
                os.path.join(parent, "bin", "64bit", "obs64.exe"),
                os.path.join(parent, "obs64.exe"),
                os.path.join(parent, "obs"),
            ]
            d = parent
    except Exception:
        pass

    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates += [
            os.path.join(pf, "obs-studio", "bin", "64bit", "obs64.exe"),
            os.path.join(pfx86, "obs-studio", "bin", "64bit", "obs64.exe"),
            os.path.join(local, "Programs", "obs-studio", "bin", "64bit",
                         "obs64.exe"),
            os.path.join(local, "obs-studio", "bin", "64bit", "obs64.exe"),
        ]
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/OBS.app/Contents/MacOS/OBS",
        ]
    else:
        candidates += [
            "/usr/bin/obs", "/usr/local/bin/obs", "/opt/OBS/obs",
            "/snap/bin/obs", "/app/bin/obs", "/usr/lib/obs/obs",
        ]

    for path in candidates:
        if _looks_like_obs_binary(path):
            return path
    return None

def _find_client_id(data):

    suffix = _GOOGLE_SUFFIX
    for m in _RUN_RE.finditer(data):
        run = m.group()
        n = len(run)
        if not (55 <= n <= 96):
            continue
        base = n - len(suffix)

        nibbles = [None] * 16
        ok = True
        for k, plain_ch in enumerate(suffix):
            x = run[base + k] ^ plain_ch
            if x > 0xF:
                ok = False
                break
            j = (base + k) % 16
            if nibbles[j] is not None and nibbles[j] != x:
                ok = False
                break
            nibbles[j] = x
        if not ok or None in nibbles:
            continue

        val = _hash_from_nibbles(nibbles)
        plain = _obs_deobfuscate(run, val)
        if _GOOGLE_PREFIX_RE.match(plain):
            return plain, val
    return None, None

def _candidate_secret_hashes(data, clientid_hash):

    target = struct.pack("<Q", clientid_hash)
    positions = [m.start() for m in re.finditer(re.escape(target), data)]
    if not positions:
        return set()

    found = set()
    for pos in positions:
        lo = max(0, pos - 1024)
        hi = min(len(data), pos + 1024)
        window = data[lo:hi]
        for m in _MOVABS_RE.finditer(window):
            start = m.end()
            imm = window[start:start + 8]
            if len(imm) != 8:
                continue
            val = struct.unpack("<Q", imm)[0]
            if val == 0 or val == clientid_hash:
                continue
            found.add(val)
    return found

def _find_secret(data, candidate_hashes, clientid_offset=None):

    if not candidate_hashes:
        return None

    hashes = list(candidate_hashes)

    prefix_keys = {}
    for h in hashes:
        key = tuple(_nibbles_from_hash(h)[i] for i in range(7))
        prefix_keys.setdefault(key, []).append(h)

    best_rank = None
    best_plain = None

    def offer(plain, offset):
        nonlocal best_rank, best_plain
        dist = (abs(offset - clientid_offset)
                if clientid_offset is not None else (1 << 30))
        rank = (0 if plain.startswith(_SECRET_PREFIX) else 1, dist)
        if best_rank is None or rank < best_rank:
            best_rank, best_plain = rank, plain

    def scan(lo, hi, near):
        for m in _RUN_RE.finditer(data, lo, hi):
            run = m.group()
            n = len(run)
            if n > 64:
                continue
            if 27 <= n <= 52:
                key = tuple(run[i] ^ _SECRET_PREFIX[i] for i in range(7))
                for h in prefix_keys.get(key, ()):
                    cand = _obs_deobfuscate(run, h)
                    if _SECRET_RE.match(cand):
                        offer(cand, m.start())
            if near and 20 <= n <= 45:
                for h in hashes:
                    cand = _obs_deobfuscate(run, h)
                    if _OLD_SECRET_NEAR_RE.match(cand):
                        offer(cand, m.start())
            elif not near and n == 24:
                for h in hashes:
                    cand = _obs_deobfuscate(run, h)
                    if _OLD_SECRET_RE.match(cand):
                        offer(cand, m.start())

    if clientid_offset is not None:
        scan(max(0, clientid_offset - 4096),
             min(len(data), clientid_offset + 4096), near=True)
        if best_plain is not None and best_rank[0] == 0:
            return best_plain

    scan(0, len(data), near=False)

    return best_plain

_B64URL_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")

def _nibble_masks_for_run(run, prefix=None):

    masks = [0xFFFF] * 16
    for i, ch in enumerate(run):
        j = i & 15
        want = prefix[i] if (prefix is not None and i < len(prefix)) else None
        allowed = 0
        for n in range(16):
            p = ch ^ n
            if (p == want) if want is not None else (p in _B64URL_BYTES):
                allowed |= 1 << n
        masks[j] &= allowed
        if not masks[j]:
            return None
    return masks

def _hash_matches_masks(val, masks):

    for j in range(16):
        nib = (val >> (4 * j)) & 0xF
        if not (masks[j] >> nib) & 1:
            return False
    return True

def _qword_candidates(data, center, radius=4096, exclude=(), align=4):

    lo = max(0, center - radius)
    hi = min(len(data), center + radius)
    window = data[lo:hi]
    found = {}
    n = len(window)
    for i in range(-lo % align, n - 7, align):
        pos = lo + i
        if pos < center + 8 and pos + 8 > center:
            continue
        chunk = window[i:i + 8]
        if all(0x20 <= b <= 0x7F for b in chunk):
            continue
        val = struct.unpack("<Q", chunk)[0]
        if val == 0 or val in exclude:
            continue
        dist = abs(pos - center)
        if val not in found or dist < found[val]:
            found[val] = dist
    return found

def _find_secret_crossarch(data, clientid_offset, clientid_hash):

    if clientid_offset is None:
        return None

    target = struct.pack("<Q", clientid_hash)
    hash_positions = [m.start() for m in re.finditer(re.escape(target), data)]

    tiers = []
    seen = set()
    for radius in (64, 1024):
        tier = {}
        for pos in hash_positions:
            for val, dist in _qword_candidates(
                    data, pos, radius=radius,
                    exclude=(clientid_hash,)).items():
                if val in seen:
                    continue
                if val not in tier or dist < tier[val]:
                    tier[val] = dist
        seen |= tier.keys()
        tiers.append(tier)
    tier_c = _qword_candidates(data, clientid_offset, radius=4096,
                               exclude=(clientid_hash,))
    tiers.append({val: dist for val, dist in tier_c.items()
                  if val not in seen})

    lo = max(0, clientid_offset - 4096)
    hi = min(len(data), clientid_offset + 4096)

    best_rank = None
    best_plain = None

    def offer(plain, hash_dist, run_dist, tier):
        nonlocal best_rank, best_plain
        rank = (0 if plain.startswith(_SECRET_PREFIX) else 1,
                tier, hash_dist, run_dist)
        if best_rank is None or rank < best_rank:
            best_rank, best_plain = rank, plain

    for m in _RUN_RE.finditer(data, lo, hi):
        run = m.group()
        n = len(run)
        run_dist = abs(m.start() - clientid_offset)
        if 27 <= n <= 52:
            masks = _nibble_masks_for_run(run, prefix=_SECRET_PREFIX)
            if masks:
                for tier, cands in enumerate(tiers):
                    for h, hash_dist in cands.items():
                        if _hash_matches_masks(h, masks):
                            cand = _obs_deobfuscate(run, h)
                            if _SECRET_RE.match(cand):
                                offer(cand, hash_dist, run_dist, tier)
        elif n == 24:
            masks = _nibble_masks_for_run(run)
            if masks:
                for tier, cands in enumerate(tiers):
                    for h, hash_dist in cands.items():
                        if _hash_matches_masks(h, masks):
                            cand = _obs_deobfuscate(run, h)
                            if _OLD_SECRET_RE.match(cand):
                                offer(cand, hash_dist, run_dist, tier)

    return best_plain

KNOWN_OBS_YT_CLIENTS = [
    {
        "client_id": ("111793709140-1l5ms0nh204r8rd5lvb3pp4sl1eq6206"
                      ".apps.googleusercontent.com"),
        "client_secret": "4mQ8FST_7u0Aa5dkCIY2oS4Z",
        "since": "OBS 30.2+ (проверено вплоть до 32.2.2, x64; клиент"
                 " один и тот же во всех официальных сборках)",
    },
]

def _builtin_client():
    first = KNOWN_OBS_YT_CLIENTS[0]
    return {"client_id": first["client_id"],
            "client_secret": first["client_secret"]}

FALLBACK_OBS_YT_CLIENT_ID = KNOWN_OBS_YT_CLIENTS[0]["client_id"]
FALLBACK_OBS_YT_CLIENT_SECRET = KNOWN_OBS_YT_CLIENTS[0]["client_secret"]

def is_obs_client_id(client_id):

    if not client_id:
        return False
    for known in KNOWN_OBS_YT_CLIENTS:
        if client_id == known["client_id"]:
            return True
    cached = get_cached_youtube_client()
    return bool(cached and cached.get("client_id") == client_id)

def get_effective_youtube_client():

    client = get_cached_youtube_client()
    if client and client.get("client_id") and client.get("client_secret"):
        return client
    if client and client.get("client_id"):
        for known in KNOWN_OBS_YT_CLIENTS:
            if client["client_id"] == known["client_id"]:
                return {"client_id": known["client_id"],
                        "client_secret": known["client_secret"]}
    return _builtin_client()

def _binary_stamp(path):
    try:
        st = os.stat(path)
        return f"{path}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return None

def extract_youtube_client(obs_binary=None, force=False):

    builtin = _builtin_client()
    path = obs_binary or find_obs_binary()
    if not path:
        config.log.info("OBS bridge: бинарник OBS не найден — "
                        "используется встроенный проверенный клиент OBS")
        return builtin

    stamp = _binary_stamp(path)
    if not force and stamp:
        cached_id = secret_store.get_password(config.STORE_OBS_YT_CLIENT_ID)
        cached_secret = secret_store.get_password(
            config.STORE_OBS_YT_CLIENT_SECRET)
        cached_stamp = secret_store.get_password(
            config.STORE_OBS_YT_STAMP)
        if (cached_id and cached_secret and cached_stamp == stamp):
            return {"client_id": cached_id, "client_secret": cached_secret}

    config.log.info(f"OBS bridge: сканирую {path} "
                    f"({os.path.getsize(path) / 1024 / 1024:.0f} МБ)…")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        config.log.warning(f"OBS bridge: не удалось прочитать {path}: {e}")
        return builtin

    def _cache(client):
        secret_store.set_password(config.STORE_OBS_YT_CLIENT_ID,
                                   client["client_id"])
        secret_store.set_password(config.STORE_OBS_YT_CLIENT_SECRET,
                                   client["client_secret"])
        if stamp:
            secret_store.set_password(config.STORE_OBS_YT_STAMP, stamp)

    client_id, clientid_hash = _find_client_id(data)
    if not client_id:
        config.log.warning(
            "OBS bridge: YouTube client_id в бинарнике OBS не найден "
            "(возможно, это сборка без YouTube-интеграции) — "
            "используется встроенный проверенный клиент OBS")
        _cache(builtin)
        return builtin

    offset = data.find(_obs_obfuscate(client_id, clientid_hash))

    secret = None
    movabs_hashes = _candidate_secret_hashes(data, clientid_hash)
    if movabs_hashes:
        secret = _find_secret(data, movabs_hashes, clientid_offset=offset)

    if not secret:
        secret = _find_secret_crossarch(data, offset, clientid_hash)
        if secret:
            config.log.info("OBS bridge: секрет найден кросс-"
                            "платформенным сканом (без movabs)")

    client_id_str = client_id.decode("ascii")

    if secret:
        result = {"client_id": client_id_str,
                  "client_secret": secret.decode("ascii")}
        _cache(result)
        config.log.info(
            f"OBS bridge: YouTube-клиент OBS извлечён "
            f"(id={client_id_str[:20]}..., секрет=…"
            f"{result['client_secret'][-4:]})")
        return result

    for known in KNOWN_OBS_YT_CLIENTS:
        if client_id_str == known["client_id"]:
            config.log.info("OBS bridge: секрет взят из таблицы "
                            "известных клиентов OBS (client_id совпадает)")
            result = {"client_id": known["client_id"],
                      "client_secret": known["client_secret"]}
            _cache(result)
            return result

    config.log.warning(
        "OBS bridge: client_secret для извлечённого client_id не найден "
        "— используется встроенный проверенный клиент OBS (вход "
        "продолжает работать)")
    _cache(builtin)
    return builtin

def get_cached_youtube_client():

    cid = secret_store.get_password(config.STORE_OBS_YT_CLIENT_ID)
    if not cid:
        return None
    return {
        "client_id": cid,
        "client_secret": secret_store.get_password(
            config.STORE_OBS_YT_CLIENT_SECRET) or None,
    }

def _basic_ini_path():
    return os.path.join(config.get_obs_config_dir(), "basic.ini")

def _basic_ini_candidates():

    root = config.get_obs_config_dir()
    candidates = [os.path.join(root, "basic.ini")]
    try:
        profiles_base = os.path.join(root, "basic", "profiles")
        names = []
        for ini_name in ("user.ini", "global.ini"):
            ini_path = os.path.join(root, ini_name)
            if not os.path.isfile(ini_path):
                continue
            try:
                cp = configparser.ConfigParser(
                    interpolation=None, strict=False)
                cp.optionxform = str
                with open(ini_path, "r", encoding="utf-8-sig",
                          errors="replace") as f:
                    cp.read_file(f)
                for key in ("ProfileDir", "Profile"):
                    val = (cp.get("Basic", key, fallback="") or "").strip()
                    if val:
                        names.append(val)
            except Exception:
                pass
        for name in names:
            p = os.path.join(profiles_base, name, "basic.ini")
            if os.path.isfile(p) and p not in candidates:
                candidates.append(p)
    except Exception:
        pass
    return candidates

def read_obs_youtube_connection():

    for path in _basic_ini_candidates():
        conn = _read_youtube_section(path)
        if conn:
            return conn
    return None

def _read_youtube_section(path):

    if not os.path.isfile(path):
        return None

    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            cp.read_file(f)
    except Exception as e:
        config.log.warning(f"OBS bridge: не удалось прочитать {path}: {e}")
        return None

    if not cp.has_section("YouTube"):
        return None

    section = cp["YouTube"]
    refresh = (section.get("RefreshToken") or "").strip()
    token = (section.get("Token") or "").strip()
    if not refresh and not token:
        return None

    try:
        expire = int(float(section.get("ExpireTime") or 0))
    except ValueError:
        expire = 0

    config.log.debug(f"OBS bridge: [YouTube] найден в {path}")
    return {
        "refresh_token": refresh or None,
        "token": token or None,
        "expire_time": expire,
        "channel_name": (section.get("ChannelName") or "").strip() or None,
    }
