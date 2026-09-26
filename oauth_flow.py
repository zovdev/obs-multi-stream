
import os
import sys
import json
import time
import threading
import secrets
import hashlib
import base64
import urllib.request
import urllib.parse
import urllib.error
import http.server
import socketserver
import webbrowser

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import config
import secret_store

class OAuthFlowError(Exception):

    def __init__(self, message, hint=None, transient=False):
        super().__init__(message)
        self.hint = hint
        self.transient = bool(transient)

def _post_form(url, data, timeout=15):

    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except ValueError:
                raise OAuthFlowError(
                    f"Не-JSON ответ от {url}: {raw[:150]!r}", transient=True)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return _http_error_to_exception(url, e.code, raw)
    except urllib.error.URLError as e:
        raise OAuthFlowError(f"Сетевая ошибка ({url}): {e.reason}",
                             transient=True)

def _http_error_to_exception(url, code, raw):

    try:
        err = json.loads(raw)
        error = str(err.get("error") or err.get("message") or "")
        desc = str(err.get("error_description") or "")
    except Exception:
        error, desc = raw[:200], ""
    if not error:
        error = raw[:200]

    hint = None
    if "invalid_client" in error or "client_secret is missing" in desc:
        hint = ("Проверьте Client ID / Client Secret в Шаге 1 док-панели. "
                "Клиент должен быть типа 'Desktop application'.")
    if "redirect_uri_mismatch" in error:
        hint = ("Тип OAuth-клиента должен быть 'Desktop application' "
                "(для него loopback-redirect разрешён автоматически).")
    if "invalid_grant" in error:
        hint = ("Refresh-токен истёк или отозван. Выйдите и авторизуйтесь "
                "заново. Если приложение в Google Console в режиме Testing — "
                "refresh-токены живут 7 дней, опубликуйте приложение.")
    if "unauthorized_client" in error:
        hint = ("Этот client_id нельзя использовать в данном flow. "
                "Создайте клиент типа 'Desktop application'.")
    raise OAuthFlowError(f"HTTP {code} от {url}: {error}: {desc}", hint=hint,
                         transient=(code >= 500))

_REFRESH_RETRY_DELAYS = (2, 5)

def _call_with_retries(fn, what):

    last = None
    for attempt in range(1 + len(_REFRESH_RETRY_DELAYS)):
        try:
            return fn()
        except OAuthFlowError as e:
            if not getattr(e, "transient", False):
                raise
            last = e
            if attempt >= len(_REFRESH_RETRY_DELAYS):
                break
            delay = _REFRESH_RETRY_DELAYS[attempt]
            config.log.warning(
                f"{what}: временная ошибка ({e}); повтор через {delay}с "
                f"(попытка {attempt + 2}/3)")
            time.sleep(delay)
    raise last

def twitch_device_code_start(client_id=None):

    if not client_id:
        client_id = config.TWITCH_CLIENT_ID
    result = _post_form(config.TWITCH_DEVICE_URL, {
        "client_id": client_id,
        "scopes": " ".join(config.TWITCH_OAUTH_SCOPES),
    })
    config.log.info(
        f"Twitch DCF started: user_code={result.get('user_code')}, "
        f"verification_uri={result.get('verification_uri')}"
    )
    return result

def twitch_device_code_poll(device_code, client_id=None):

    if not client_id:
        client_id = config.TWITCH_CLIENT_ID
    scope_str = " ".join(config.TWITCH_OAUTH_SCOPES)
    data = {
        "client_id": client_id,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "scope": scope_str,
        "scopes": scope_str,
    }
    try:
        return _post_form(config.TWITCH_TOKEN_URL, data)
    except OAuthFlowError as e:
        msg = str(e)
        if "authorization_pending" in msg:
            raise OAuthFlowError("authorization_pending")
        if "slow_down" in msg:
            raise OAuthFlowError("slow_down")
        if "expired" in msg.lower():
            raise OAuthFlowError("device_code_expired")
        raise

def twitch_device_code_wait(device_code, on_status=None, timeout=1800,
                            poll_interval=5, client_id=None):

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = twitch_device_code_poll(device_code, client_id=client_id)
            if on_status:
                on_status("success", "Twitch: авторизация подтверждена")
            return result
        except OAuthFlowError as e:
            msg = str(e)
            if msg == "authorization_pending":
                if on_status:
                    on_status("waiting", "Ожидание подтверждения на twitch.tv/activate...")
                time.sleep(poll_interval)
                continue
            elif msg == "slow_down":
                poll_interval = min(poll_interval * 2, 30)
                if on_status:
                    on_status("slow_down",
                              f"Twitch: увеличиваем интервал до {poll_interval}с")
                time.sleep(poll_interval)
                continue
            elif msg == "device_code_expired":
                raise OAuthFlowError("Код Twitch истёк. Нажмите кнопку заново.")
            else:
                raise
    raise OAuthFlowError(f"Twitch: время ожидания истекло ({timeout}с).")

def twitch_refresh_token(refresh_token, client_id=None):

    return _call_with_retries(
        lambda: _twitch_refresh_token_once(refresh_token, client_id),
        "Twitch refresh")

def _twitch_refresh_token_once(refresh_token, client_id=None):
    if not client_id:
        client_id = config.TWITCH_CLIENT_ID

    proxy_error = None
    try:
        result = _post_form(config.TWITCH_REFRESH_PROXY_URL, {
            "action": "redirect",
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
        if isinstance(result, dict):
            if result.get("error"):
                proxy_error = OAuthFlowError(
                    f"{result['error']}: "
                    f"{result.get('error_description', '')}")
            elif result.get("access_token"):
                return result
            else:
                proxy_error = OAuthFlowError(
                    f"неожиданный ответ прокси: {str(result)[:120]}")
        else:
            proxy_error = OAuthFlowError(
                f"неожиданный ответ прокси: {str(result)[:120]}")
    except OAuthFlowError as e:
        proxy_error = e
    except Exception as e:
        proxy_error = OAuthFlowError(str(e))

    msg = str(proxy_error).lower()
    if "invalid_grant" in msg or "invalid refresh token" in msg:
        raise OAuthFlowError(
            "Twitch: refresh-токен отозван или истёк — "
            "нужна повторная авторизация (кнопка в Шаге 1).",
            hint="Refresh-токены Twitch одноразовые и живут 30 дней "
                 "без активности. Просто переподключите Twitch.")
    config.log.warning(
        f"Twitch refresh через прокси OBS не удался: {proxy_error}. "
        "Пробую напрямую (без секрета)…")

    return _post_form(config.TWITCH_TOKEN_URL, {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })

def twitch_validate_token(access_token):

    req = urllib.request.Request(config.TWITCH_VALIDATE_URL)
    req.add_header("Authorization", f"OAuth {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        if e.code == 401:
            raise OAuthFlowError("Twitch: токен истёк или невалиден (401)")
        raise OAuthFlowError(f"Twitch /validate: HTTP {e.code}: {raw}")
    except urllib.error.URLError as e:
        raise OAuthFlowError(f"Twitch /validate: сетевая ошибка: {e.reason}")

def _generate_pkce():

    code_verifier = base64.urlsafe_b64encode(
        secrets.token_bytes(48)).decode("ascii").rstrip("=")
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    return code_verifier, code_challenge

def _find_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        config.log.info(f"OAuth callback request: path={self.path}, "
                        f"params={ {k: v for k, v in params.items() if k != 'code'} }")

        if "code" in params:
            self.server.auth_code = params["code"][0]
            self.server.auth_state = params.get("state", [None])[0]
            self.server.auth_error = None
            config.log.info(f"  OAuth code received: {params['code'][0][:20]}...")
            self._send_html(self._page(
                "✅ Авторизация успешна!", "#2e7d32", "oauth-ok"), 200)
        elif "error" in params:
            self.server.auth_code = None
            self.server.auth_error = params.get("error", ["unknown"])[0]
            config.log.info(f"  OAuth error received: {self.server.auth_error}")
            self._send_html(self._page(
                f"Ошибка авторизации: {self.server.auth_error}",
                "#c62828", "oauth-fail"), 400)
        elif parsed.path in ("/favicon.ico", "/robots.txt"):
            self.send_response(404)
            self.end_headers()
        else:
            self._send_html("<p>Ожидание авторизации…</p>", 200)

    def _page(self, title, color, hash_tag):

        dock = getattr(self.server, "dock_return", None)
        redirect = ""
        if dock:
            safe = dock.rstrip("/") + "#" + hash_tag
            redirect = (f"<script>setTimeout(function(){{"
                        f"try{{location.replace('{safe}');}}catch(e){{}}}}, 1000);"
                        f"</script>")
        return (f"<h1 style='color:{color}'>{title}</h1>"
                f"<p>Вернитесь в OBS — док-панель обновится автоматически.</p>"
                f"{redirect}")

    def _send_html(self, html, code):
        body = (f"<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
                f"<body style='font-family:sans-serif;text-align:center;"
                f"padding:50px;'>{html}</body></html>").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

def youtube_loopback_flow(client_id, client_secret, scopes, on_status=None,
                           timeout=600, use_pkce=True,
                           open_mode="dock", dock_base_url=None):

    if not client_id:
        raise OAuthFlowError("Не задан Google OAuth client_id.")
    if not client_secret and use_pkce is False:
        raise OAuthFlowError(
            "Не извлечён client_secret OBS — YouTube-вход через OBS сейчас "
            "невозможен. Попробуйте «Расширенные → свой Google-клиент».")

    state = secrets.token_urlsafe(24)
    port = _find_free_port()
    redirect_uri = f"http://127.0.0.1:{port}"

    if use_pkce:
        code_verifier, code_challenge = _generate_pkce()
    else:
        code_verifier, code_challenge = None, None

    if on_status:
        on_status("starting", f"Локальный сервер: {redirect_uri}")

    httpd = None
    try:
        socketserver.TCPServer.allow_reuse_address = True
        httpd = socketserver.TCPServer(("127.0.0.1", port), _OAuthCallbackHandler)
        httpd.auth_code = None
        httpd.auth_error = None
        httpd.auth_state = None
        httpd.dock_return = dock_base_url
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        if on_status:
            on_status("server_started", f"Loopback-сервер запущен: {redirect_uri}")

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        if use_pkce:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        auth_url = config.GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)

        if on_status:
            on_status("auth_url", auth_url)

        if open_mode == "browser":
            if on_status:
                on_status("opening_browser",
                          "Открываем внешний браузер для входа в Google…")
            try:
                webbrowser.open(auth_url)
            except Exception as e:
                config.log.warning(
                    f"Не удалось открыть браузер: {e}. URL: {auth_url}")
                if on_status:
                    on_status("browser_failed",
                              "Браузер не открылся — используйте кнопку в панели.")

        deadline = time.time() + timeout
        while time.time() < deadline:
            if httpd.auth_code is not None:
                if httpd.auth_state != state:
                    raise OAuthFlowError("State mismatch — возможная CSRF.")
                break
            if httpd.auth_error is not None:
                err = httpd.auth_error
                if err == "access_denied":
                    raise OAuthFlowError("Вы отменили авторизацию в Google.")
                raise OAuthFlowError(
                    f"Google вернул ошибку: {err}",
                    hint="redirect_uri_mismatch → нужен клиент типа "
                         "'Desktop application'.")
            time.sleep(0.3)

        if httpd.auth_code is None:
            raise OAuthFlowError(
                f"Таймаут: авторизация не завершена за {timeout}с.",
                hint="Нажмите кнопку подключения заново и подтвердите вход.")

        if on_status:
            on_status("exchanging", "Обмениваем code на токены…")

        return _youtube_exchange_code(
            httpd.auth_code, code_verifier, redirect_uri,
            client_id, client_secret,
        )
    finally:
        if httpd:
            try:
                httpd.shutdown()
            except Exception:
                pass

def youtube_obs_flow(client_id, client_secret, on_status=None, timeout=600,
                     open_mode="dock", dock_base_url=None):

    return youtube_loopback_flow(
        client_id, client_secret, [config.OBS_YOUTUBE_SCOPE],
        on_status=on_status, timeout=timeout, use_pkce=False,
        open_mode=open_mode, dock_base_url=dock_base_url)

def youtube_pkce_flow(client_id, client_secret, on_status=None, timeout=600,
                      open_mode="dock", dock_base_url=None):

    if not client_id or not client_secret:
        raise OAuthFlowError(
            "Не задан Google OAuth клиент (Client ID / Client Secret).",
            hint="Расширенные настройки → «Свой Google-клиент». Тип клиента — "
                 "'Desktop application'.",
        )
    return youtube_loopback_flow(
        client_id, client_secret, config.YOUTUBE_OAUTH_SCOPES,
        on_status=on_status, timeout=timeout, use_pkce=True,
        open_mode=open_mode, dock_base_url=dock_base_url)

def _youtube_exchange_code(code, code_verifier, redirect_uri,
                           client_id, client_secret):

    data = {
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if client_secret:
        data["client_secret"] = client_secret
    if code_verifier:
        data["code_verifier"] = code_verifier
    return _post_form(config.GOOGLE_TOKEN_URL, data)

def youtube_refresh_token(refresh_token, client_id, client_secret):

    if not client_id or not client_secret:
        raise OAuthFlowError(
            "Не задан Google OAuth клиент — refresh невозможен.",
            hint="Заполните Client ID / Secret в Шаге 1 док-панели.")
    return _call_with_retries(
        lambda: _post_form(config.GOOGLE_TOKEN_URL, {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }), "YouTube refresh")

def google_tokeninfo(access_token):

    url = config.GOOGLE_TOKENINFO_URL + "?" + urllib.parse.urlencode({
        "access_token": access_token})
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        if e.code == 400:
            raise OAuthFlowError("Google: токен невалиден или истёк (tokeninfo 400)")
        raise OAuthFlowError(f"Google tokeninfo: HTTP {e.code}: {raw}")
    except urllib.error.URLError as e:
        raise OAuthFlowError(f"Google tokeninfo: сетевая ошибка: {e.reason}")

def google_get_userinfo(access_token):

    req = urllib.request.Request(config.GOOGLE_USERINFO_URL)
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")).get("email", "")
    except Exception as e:
        config.log.warning(f"Google userinfo failed: {e}")
        return ""

def get_google_client():

    client_id = secret_store.get_password(config.STORE_GOOGLE_CLIENT_ID)
    client_secret = secret_store.get_password(config.STORE_GOOGLE_CLIENT_SECRET)
    if client_id and client_secret:
        return {"client_id": client_id, "client_secret": client_secret}
    return None

def set_google_client(client_id, client_secret):

    secret_store.set_password(config.STORE_GOOGLE_CLIENT_ID, client_id)
    secret_store.set_password(config.STORE_GOOGLE_CLIENT_SECRET, client_secret)
    config.log.info("Google OAuth client сохранён в хранилище")

def _blob_save(blob_key, data):
    secret_store.set_password(blob_key, json.dumps(
        data, ensure_ascii=False, separators=(",", ":")))

def _blob_load(blob_key):

    raw = secret_store.get_password(blob_key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        config.log.error(f"Блоб {blob_key}: повреждён — игнорирую")
        return None

def save_twitch_oauth_token(access_token, refresh_token=None, user_id=None,
                             username=None, scopes=None, client_id=None):
    data = {"token": access_token}
    if refresh_token:
        data["refresh_token"] = refresh_token
    if user_id:
        data["user_id"] = str(user_id)
    if username:
        data["username"] = username
    if scopes:
        data["scopes"] = " ".join(scopes)
    if client_id:
        data["client_id"] = client_id
    _blob_save(config.STORE_TWITCH_BLOB, data)

def get_cached_twitch_oauth_token():
    data = _blob_load(config.STORE_TWITCH_BLOB)
    if not data or not data.get("token"):
        return None
    return {
        "token": data["token"],
        "refresh_token": data.get("refresh_token"),
        "user_id": data.get("user_id"),
        "username": data.get("username"),
        "scopes": (data.get("scopes") or "").split(),
        "client_id": data.get("client_id"),
    }

def clear_twitch_oauth_token():
    secret_store.delete_password(config.STORE_TWITCH_BLOB)

def save_youtube_oauth_token(access_token, refresh_token=None, email=None,
                             scopes=None, client_id_used=None):
    data = {"token": access_token}
    if refresh_token:
        data["refresh_token"] = refresh_token
    if email:
        data["email"] = email
    if scopes:
        data["scopes"] = " ".join(scopes)
    if client_id_used:
        data["client_id_used"] = client_id_used
    _blob_save(config.STORE_YOUTUBE_BLOB, data)

def get_cached_youtube_oauth_token():
    data = _blob_load(config.STORE_YOUTUBE_BLOB)
    if not data or not data.get("token"):
        return None
    return {
        "token": data["token"],
        "refresh_token": data.get("refresh_token"),
        "email": data.get("email"),
        "scopes": (data.get("scopes") or "").split(),
        "client_id_used": data.get("client_id_used"),
    }

def get_youtube_client_id_used():

    data = _blob_load(config.STORE_YOUTUBE_BLOB)
    if data:
        return data.get("client_id_used")
    return None

def clear_youtube_oauth_token():
    secret_store.delete_password(config.STORE_YOUTUBE_BLOB)

def missing_twitch_scopes(scopes):

    have = set(scopes or [])
    missing = []
    for req in config.TWITCH_REQUIRED_SCOPES:
        if req not in have:
            missing.append(req)
    for req, alternatives in config.TWITCH_ALT_SCOPES.items():
        if req not in have and not (set(alternatives) & have):
            missing.append(req)
    return missing

def missing_youtube_scopes(scopes):

    have = set(scopes or [])
    return [s for s in config.YOUTUBE_REQUIRED_SCOPES if s not in have]
