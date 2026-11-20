"""Анонимное чтение чата YouTube через внутренний API (innertube).

Это тот же путь, которым пользуется браузер без логина: отдельная
страница live_chat (её же открывает iframe на странице эфира) отдаёт
публичный ключ API, версию WEB-клиента, visitorData и continuation-токен
опроса. Дальше ходят POST-запросы get_live_chat: сервер возвращает
сообщения, следующий токен и интервал опроса (timeoutMs). OAuth и квота
Data API для этого пути не нужны вообще.

Важно понимать ограничения:
- это НЕ публичный API: YouTube может изменить формат без предупреждения
  (InnertubeError.kind позволяет отличить «сломался формат» от «сеть»);
- анонимно можно только ЧИТАТЬ: отправка сообщений и модерация требуют
  OAuth (официальный Data API), поэтому youtube_chat.py держит фолбэк;
- слишком частые запросы сервер режет (HTTP 429/403) — интервал опроса
  берётся из ответа сервера, а при блокировке помогает пересоздание
  сессии (свежий visitorData), что проверено на живом чате.

Все сетевые ошибки типизируются kind'ом:
- "network"  — транспорт (нет сети, таймаут);
- "ratelimit" — HTTP 429;
- "blocked"  — HTTP 403 / страница-блок Google (обычно по IP) — лечится
               пересозданием сессии;
- "http"     — прочие HTTP-коды;
- "schema"   — ответ/страница не похожи на ожидаемый формат;
- "not_live" — чата нет (эфир не live или чат отключён).
"""

import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import config

_LIVE_CHAT_PAGE_URL = "https://www.youtube.com/live_chat"
_GET_LIVE_CHAT_URL = ("https://www.youtube.com/youtubei/v1/live_chat/"
                      "get_live_chat")

# Публичные константы WEB-клиента: ключ встроен в JS каждой страницы
# YouTube и годами не меняется; версия нужна лишь правдоподобная — точную
# берём со страницы чата, это лишь резерв на случай, если регэксп не сработал.
_FALLBACK_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
_FALLBACK_CLIENT_VERSION = "2.20240101.00.00"

_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/131.0.0.0 Safari/537.36")

_HEADERS_BASE = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    # Обход EU-стены согласия (для российских IP не нужен, но не мешает)
    "Cookie": "CONSENT=YES+cb.20210328-17-p0.en+FX+678; SOCS=CAI",
}


class InnertubeError(Exception):

    def __init__(self, message, kind="network"):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------- HTTP

def _maybe_gunzip(raw):
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except Exception:
            return raw
    return raw


def _raise_http_error(e):
    """HTTPError -> типизированный InnertubeError."""
    body = ""
    try:
        body = e.read(300).decode("utf-8", errors="replace")
    except Exception:
        pass
    if e.code == 429:
        raise InnertubeError("HTTP 429 (слишком часто)", "ratelimit")
    if e.code == 403 or "<title>Sorry" in body:
        raise InnertubeError(
            f"HTTP 403/блок ({body[:80]})", "blocked")
    raise InnertubeError(f"HTTP {e.code}: {body[:120]}", "http")


def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=_HEADERS_BASE)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        _raise_http_error(e)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise InnertubeError(f"Сеть: {e}", "network")
    return _maybe_gunzip(raw).decode("utf-8", errors="replace")


def _http_post_json(url, body, timeout=15):
    req = urllib.request.Request(url, method="POST")
    req.add_header("User-Agent", _USER_AGENT)
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", "https://www.youtube.com")
    req.data = json.dumps(body).encode("utf-8")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        _raise_http_error(e)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise InnertubeError(f"Сеть: {e}", "network")
    text = _maybe_gunzip(raw).decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except Exception:
        raise InnertubeError("Ответ не является JSON", "schema")


# ---------------------------------------------------------------- разбор

def _extract_json_after(html, marker):
    """JSON-объект, следующий за маркером в HTML.

    Скобочный счёт с учётом строк: содержимое ytInitialData может
    включать вложенные «};» внутри строк, поэтому наивные регэкспы
    вроде (.+?);} иногда обрезают объект.
    """
    i = html.find(marker)
    if i < 0:
        return None
    start = html.find('{', i)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for pos in range(start, len(html)):
        ch = html[pos]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start:pos + 1])
                    except Exception:
                        return None
    return None


def _find_first(node, key):
    """Первое значение ключа в JSON-дереве (в глубину)."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            r = _find_first(v, key)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_first(v, key)
            if r is not None:
                return r
    return None


def _page_config(html):
    """(api_key, client_version, visitor_data) из ytcfg страницы."""
    api_key = version = visitor = None
    m = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    if m:
        api_key = m.group(1)
    m = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    if not m:
        m = re.search(r'"clientVersion":"([\d.]+)"', html)
    if m:
        version = m.group(1)
    m = re.search(r'"visitorData":"([^"]+)"', html)
    if m:
        visitor = m.group(1)
    return api_key, version, visitor


def _continuations_of(ytdata):
    """Список continuation-обёрток для чата из ytInitialData.

    Страница live_chat?v=... держит токен в liveChatRenderer.continuations;
    страница, открытая по continuation (переключение на «Live chat»), — в
    continuationContents.liveChatContinuation.continuations (та же форма,
    что и ответ get_live_chat).
    """
    if not isinstance(ytdata, dict):
        return []
    lcr = ytdata.get("liveChatRenderer")
    if isinstance(lcr, dict) and lcr.get("continuations"):
        return lcr["continuations"]
    lcc = _find_first(ytdata, "liveChatContinuation")
    if isinstance(lcc, dict) and lcc.get("continuations"):
        return lcc["continuations"]
    return []


def _polling_token(continuations):
    """(токен, timeout_ms) из continuation-обёрток."""
    for c in continuations or []:
        if not isinstance(c, dict):
            continue
        for key in ("timedContinuationData", "invalidationContinuationData",
                    "restartContinuationData"):
            d = c.get(key)
            if isinstance(d, dict) and d.get("continuation"):
                token = urllib.parse.unquote(d["continuation"])
                return token, d.get("timeoutMs")
    return None, None


def _live_chat_reload_token(ytdata):
    """Reload-токен пункта «Live chat» (хронологический чат).

    Токен из subMenuItems открывает страницу чата в выбранном режиме
    (Top chat / Live chat). Использовать его напрямую в get_live_chat
    нельзя — сервер отвечает 400 (проверено): это токен загрузки
    страницы, а не опроса.
    """
    sub = _find_first(ytdata, "sortFilterSubMenuRenderer")
    items = (sub or {}).get("subMenuItems") if isinstance(sub, dict) else None
    for it in items or []:
        if it.get("title") == "Live chat":
            d = (it.get("continuation") or {}).get(
                "reloadContinuationData") or {}
            if d.get("continuation"):
                return d["continuation"]  # в исходном (url-кодированном) виде
    return None


def _runs_to_text(node):
    runs = (node or {}).get("runs") or []
    return "".join(r.get("text", "") for r in runs if isinstance(r, dict))


def _author_flags(renderer):
    is_owner = False
    is_moderator = False
    for b in renderer.get("authorBadges") or []:
        badge = b.get("liveChatAuthorBadgeRenderer") if isinstance(b, dict) else None
        if not isinstance(badge, dict):
            continue
        icon = (badge.get("icon") or {}).get("iconType") or ""
        if icon == "OWNER":
            is_owner = True
        elif icon == "MODERATOR":
            is_moderator = True
    return is_owner, is_moderator


def _author_avatar(renderer):
    """URL аватарки автора из authorPhoto (миниатюра подходящего размера).

    innertube отдаёт несколько миниатюр; для строки чата достаточно
    маленькой (обычно 32x32). Внутренний формат меняется без
    предупреждения — любая странность просто даёт None (сработает
    имитация аватара на фронте).
    """
    thumbs = ((renderer.get("authorPhoto") or {}).get("thumbnails") or [])
    best = None
    for t in thumbs:
        if not isinstance(t, dict) or not t.get("url"):
            continue
        w = t.get("width") or 0
        if 24 <= w <= 64:      # оптимальный размер для чата
            return t["url"]
        if best is None:
            best = t["url"]
    return best


def _message_item(renderer, text):
    is_owner, is_moderator = _author_flags(renderer)
    author_node = renderer.get("authorName") or {}
    return {
        "author": author_node.get("simpleText") or "Аноним",
        "text": text,
        "avatar": _author_avatar(renderer),
        "is_owner": is_owner,
        "is_moderator": is_moderator,
    }


def _parse_action(action, seen_types):
    """Одно действие get_live_chat -> dict сообщения или None.

    Неизвестные типы элементов пропускаются (ticker-дубликаты Super Chat,
    системные сообщения, анонсы подарков и пр.) — падать на них нельзя:
    формат внутреннего API меняется без предупреждения.
    """
    if not isinstance(action, dict):
        return None
    item = (action.get("addChatItemAction") or {}).get("item")
    if not isinstance(item, dict):
        return None

    r = item.get("liveChatTextMessageRenderer")
    if isinstance(r, dict):
        return _message_item(r, _runs_to_text(r.get("message")))

    # Super Chat: платное сообщение с текстом
    r = item.get("liveChatPaidMessageRenderer")
    if isinstance(r, dict):
        amount_node = r.get("purchaseAmountText") or {}
        amount = amount_node.get("simpleText") or ""
        text = _runs_to_text(r.get("message"))
        label = f"[Super Chat {amount}] ".rstrip() if amount else "[Super Chat] "
        return _message_item(r, label + text)

    # Super Sticker: платный стикер без текста
    r = item.get("liveChatPaidStickerRenderer")
    if isinstance(r, dict):
        amount_node = r.get("purchaseAmountText") or {}
        amount = amount_node.get("simpleText") or ""
        label = f"[Стикер {amount}]".rstrip()
        return _message_item(r, label)

    # Прочее (milestone/подарки/системные): пропускаем, но запоминаем тип
    for key in item:
        if key not in seen_types:
            seen_types.add(key)
            config.log.debug(f"YouTube anon chat: пропущен неизвестный "
                             f"тип элемента: {key}")
    return None


# ---------------------------------------------------------------- клиент

class AnonymousChatClient:
    """Чтение чата YouTube как анонимный браузерный клиент.

    Проверенный на живом эфире сценарий:
    1) GET live_chat?v=<ID> — настройки + токен опроса (Top chat);
    2) GET live_chat?continuation=<«Live chat» reload-токен> — та же
       страница, но в хронологическом режиме (как «Live chat» в меню
       чата и как официальный Data API, который возвращает всё подряд);
    3) POST get_live_chat в цикле: каждый ответ приносит сообщения,
       следующий токен и timeoutMs — интервал задаёт сервер.
    """

    POLL_MIN_SEC = 1.0
    POLL_MAX_SEC = 20.0

    def __init__(self, video_id):
        self.video_id = video_id
        self._api_key = _FALLBACK_API_KEY
        self._client_version = _FALLBACK_CLIENT_VERSION
        self._visitor_data = None
        self._continuation = None
        self._seen_types = set()

    def bootstrap(self):
        """Поднятие сессии. Бросает InnertubeError (not_live — чата нет)."""
        # Шаг 1: страница чата по умолчанию.
        html = _http_get(
            f"{_LIVE_CHAT_PAGE_URL}?v={self.video_id}&hl=en")
        self._apply_page(html)
        ytdata = _extract_json_after(html, "ytInitialData")
        if not isinstance(ytdata, dict):
            raise InnertubeError("ytInitialData не найден на live_chat",
                                 "schema")
        token, _ = _polling_token(_continuations_of(ytdata))

        # Шаг 2: переключение в хронологический режим («Live chat»).
        # Ошибка шага не фатальна: останется Top chat (сообщения,
        # скрытые антиспамом, будут пропущены, но чат продолжит работать).
        reload_token = _live_chat_reload_token(ytdata)
        if reload_token:
            try:
                url = _LIVE_CHAT_PAGE_URL + "?" + urllib.parse.urlencode(
                    {"continuation": reload_token, "hl": "en"})
                html2 = _http_get(url)
                self._apply_page(html2)
                ytdata2 = _extract_json_after(html2, "ytInitialData")
                if isinstance(ytdata2, dict):
                    token2, _ = _polling_token(_continuations_of(ytdata2))
                    if token2:
                        token = token2
            except InnertubeError as e:
                config.log.debug(f"YouTube anon chat: переключение на "
                                 f"'Live chat' не удалось ({e.kind}): {e}")

        if not token:
            raise InnertubeError(
                "continuation не найден (эфир не live или чат отключён)",
                "not_live")
        self._continuation = token
        config.log.info(
            f"YouTube anon chat: сессия поднята "
            f"(client {self._client_version})")
        return self

    def _apply_page(self, html):
        api_key, version, visitor = _page_config(html)
        if api_key:
            self._api_key = api_key
        if version:
            self._client_version = version
        if visitor:
            self._visitor_data = visitor

    def poll(self):
        """Один запрос get_live_chat.

        Возвращает (messages, wait_sec, ended):
        - messages: список сообщений в формате официального ридера
          (author/text/is_owner/is_moderator);
        - wait_sec: интервал до следующего опроса (timeoutMs сервера);
        - ended: True, если сервер перестал выдавать continuation — чат
          закрыт (эфир закончен), ридеру следует остановиться.
        """
        client = {"clientName": "WEB", "clientVersion": self._client_version}
        if self._visitor_data:
            client["visitorData"] = self._visitor_data
        body = {"context": {"client": client},
                "continuation": self._continuation}
        url = (f"{_GET_LIVE_CHAT_URL}"
               f"?key={self._api_key}&prettyPrint=false")
        resp = _http_post_json(url, body)

        lcc = (resp.get("continuationContents") or {}).get(
            "liveChatContinuation")
        if not isinstance(lcc, dict):
            raise InnertubeError("в ответе нет liveChatContinuation",
                                 "schema")

        messages = []
        for action in lcc.get("actions") or []:
            msg = _parse_action(action, self._seen_types)
            if msg:
                messages.append(msg)

        next_token, wait_ms = _polling_token(lcc.get("continuations"))
        if not next_token:
            # Сервер перестал выдавать следующий токен: чат завершён.
            return messages, self.POLL_MAX_SEC, True
        self._continuation = next_token
        wait = max(self.POLL_MIN_SEC,
                   min((wait_ms or 5000) / 1000.0, self.POLL_MAX_SEC))
        return messages, wait, False
