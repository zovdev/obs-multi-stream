
import json
import urllib.request
import urllib.parse
import urllib.error

import config
import secret_store

class TwitchAPIError(Exception):

    def __init__(self, message, http_code=None):
        super().__init__(message)
        self.http_code = http_code

_active_client_id = None

def set_active_client_id(client_id):

    global _active_client_id
    _active_client_id = client_id or None

def get_active_client_id():
    if _active_client_id:
        return _active_client_id
    try:
        import oauth_flow
        cached = oauth_flow.get_cached_twitch_oauth_token()
        if cached and cached.get("client_id"):
            return cached["client_id"]
    except Exception:
        pass
    try:
        cached = secret_store.get_password(config.STORE_TWITCH_API_CLIENT_ID)
        if cached:
            return cached
    except Exception:
        pass
    return config.TWITCH_CLIENT_ID

def _make_request(method, url, access_token, params=None, body=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Client-Id", get_active_client_id())
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode("utf-8")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            if not raw.strip():
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raise TwitchAPIError(
                    f"Twitch API вернул не-JSON ответ: {raw[:200]!r}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        config.log.error(f"Twitch API {method} {url} -> {e.code}: {err_body}")
        if e.code == 401:
            raise TwitchAPIError(
                f"HTTP 401 (токен истёк): {err_body[:200]}", http_code=401)
        if e.code == 403:
            raise TwitchAPIError(
                f"HTTP 403 (нет прав/scope): {err_body[:200]}", http_code=403)
        if e.code == 404:
            short = err_body.strip()[:120]
            if not short or '"message":""' in err_body.replace(" ", ""):
                raise TwitchAPIError(
                    "HTTP 404 (пустой): токен выдан заблокированному для "
                    "сторонних вызовов клиенту (first-party) либо Client-Id "
                    "не совпадает с токеном. Переподключите Twitch кнопкой "
                    "в Шаге 1.", http_code=404)
            raise TwitchAPIError(f"HTTP 404: {err_body[:200]}", http_code=404)
        raise TwitchAPIError(f"HTTP {e.code}: {err_body[:300]}",
                              http_code=e.code)
    except urllib.error.URLError as e:
        raise TwitchAPIError(f"Сетевая ошибка: {e}")

def get_channel_info(access_token, broadcaster_id):

    result = _make_request(
        "GET", config.TWITCH_CHANNELS_URL, access_token,
        params={"broadcaster_id": broadcaster_id},
    )
    data = result.get("data", [])
    if not data:
        raise TwitchAPIError(f"Канал {broadcaster_id} не найден")
    return data[0]

def clean_tags(tags):

    return [t.strip()[:25] for t in (tags or []) if t and t.strip()][:10]

def update_channel_info(access_token, broadcaster_id,
                        title=None, game_id=None, tags=None):

    if not broadcaster_id:
        raise TwitchAPIError("broadcaster_id неизвестен")

    body = {}
    if title is not None:
        body["title"] = title[:140]
    if game_id is not None:
        body["game_id"] = str(game_id)
    if tags is not None:
        body["tags"] = clean_tags(tags)

    if not body:
        return

    config.log.info(
        f"Twitch: обновление канала: title={title!r}, game_id={game_id}, tags={tags}")
    _make_request(
        "PATCH", config.TWITCH_CHANNELS_URL, access_token,
        params={"broadcaster_id": broadcaster_id}, body=body,
    )

def search_categories(access_token, query):

    if not query or not query.strip():
        return []
    result = _make_request(
        "GET", config.TWITCH_SEARCH_CATEGORIES_URL, access_token,
        params={"query": query.strip(), "first": 20},
    )
    return result.get("data", [])

def search_games(access_token, game_name):

    if not game_name or not game_name.strip():
        return []
    result = _make_request(
        "GET", config.TWITCH_GAMES_URL, access_token,
        params={"name": game_name.strip()},
    )
    return result.get("data", [])

def get_stream_key(access_token, broadcaster_id):

    result = _make_request(
        "GET", config.TWITCH_STREAM_KEY_URL, access_token,
        params={"broadcaster_id": broadcaster_id},
    )
    data = result.get("data", [])
    if not data or not data[0].get("stream_key"):
        raise TwitchAPIError("Stream key не получен от Twitch API")
    return data[0]["stream_key"]

def get_stream_viewers(access_token, user_login):

    try:
        result = _make_request(
            "GET", config.TWITCH_STREAMS_URL, access_token,
            params={"user_login": user_login},
        )
        streams = result.get("data", [])
        if not streams:
            return None
        return streams[0].get("viewer_count")
    except TwitchAPIError as e:
        config.log.warning(f"get_stream_viewers failed: {e}")
        return None
