
import io
import json
import time
import os
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

import config

class YouTubeAPIError(Exception):

    def __init__(self, message, http_code=None):
        super().__init__(message)
        self.http_code = http_code

def _make_request(method, url, access_token, params=None, body=None,
                  is_multipart=False, multipart_data=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {access_token}")

    if is_multipart and multipart_data:
        boundary = "obs-multi-stream-boundary-" + str(int(time.time()))
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.data = _build_multipart_body(boundary, multipart_data)
    elif body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode("utf-8")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        config.log.error(f"YouTube API {method} {url} -> {e.code}: {err_body}")
        if e.code == 401:
            raise YouTubeAPIError(
                f"HTTP 401 (токен истёк/невалиден): {err_body[:200]}",
                http_code=401)
        if e.code == 403:
            raise YouTubeAPIError(
                f"HTTP 403 (нет прав/квота): {err_body[:400]}",
                http_code=403)
        raise YouTubeAPIError(f"HTTP {e.code}: {err_body[:400]}",
                              http_code=e.code)
    except urllib.error.URLError as e:
        raise YouTubeAPIError(f"Сетевая ошибка: {e}")

def _build_multipart_body(boundary, fields):
    lines = []
    for name, (filename, content, content_type) in fields.items():
        lines.append(f"--{boundary}".encode("utf-8"))
        lines.append(
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"'.encode("utf-8")
        )
        lines.append(f"Content-Type: {content_type}".encode("utf-8"))
        lines.append(b"")
        lines.append(content)
    lines.append(f"--{boundary}--".encode("utf-8"))
    lines.append(b"")
    return b"\r\n".join(lines)

def get_my_channel(access_token):

    result = _make_request(
        "GET", config.YOUTUBE_CHANNELS_URL, access_token,
        params={"part": "snippet", "mine": "true"},
    )
    items = result.get("items", [])
    if not items:
        raise YouTubeAPIError(
            "Канал YouTube не найден для этого аккаунта. "
            "Убедитесь, что в Google Console включён YouTube Data API v3 "
            "и аккаунт добавлен в Test users.")
    return {
        "id": items[0].get("id"),
        "title": items[0].get("snippet", {}).get("title"),
    }

def create_broadcast(access_token, title, description, tags=None,
                     scheduled_start_time=None):

    if scheduled_start_time is None:
        scheduled_start_time = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    body = {
        "snippet": {
            "title": title,
            "description": description or "",
            "scheduledStartTime": scheduled_start_time,
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": config.YOUTUBE_MADE_FOR_KIDS,
        },
        "contentDetails": {
            "enableAutoStart": False,
            "enableAutoStop": False,
            "monitorStream": {"enableMonitorStream": False},
        },
    }

    if tags:
        clean_tags = []
        total_len = 0
        for tag in tags:
            tag = tag.strip()
            if not tag:
                continue
            if total_len + len(tag) > 480:
                break
            clean_tags.append(tag)
            total_len += len(tag)
        if clean_tags:
            body["snippet"]["tags"] = clean_tags

    result = _make_request(
        "POST", config.YOUTUBE_BROADCASTS_URL, access_token,
        params={"part": "snippet,status,contentDetails"}, body=body,
    )
    broadcast_id = result.get("id")
    if not broadcast_id:
        raise YouTubeAPIError(f"Не получен broadcast_id: {result}")
    config.log.info(f"YouTube broadcast создан: {broadcast_id} "
                    "(доступ: открытый)")
    return broadcast_id

def get_broadcast(access_token, broadcast_id):

    result = _make_request(
        "GET", config.YOUTUBE_BROADCASTS_URL, access_token,
        params={"part": "snippet,status,contentDetails", "id": broadcast_id},
    )
    items = result.get("items", [])
    if not items:
        return None
    item = items[0]
    return {
        "id": item.get("id"),
        "title": item.get("snippet", {}).get("title"),
        "life_cycle_status": item.get("status", {}).get("lifeCycleStatus"),
        "bound_stream_id": item.get("contentDetails", {}).get("boundStreamId"),
    }

def get_stream(access_token, stream_id):

    result = _make_request(
        "GET", config.YOUTUBE_STREAMS_URL, access_token,
        params={"part": "snippet,cdn,contentDetails", "id": stream_id},
    )
    items = result.get("items", [])
    if not items:
        return None
    info = items[0].get("cdn", {}).get("ingestionInfo", {})
    if not (info.get("ingestionAddress") and info.get("streamName")):
        return None
    return {
        "id": items[0].get("id"),
        "rtmp_url": info.get("ingestionAddress"),
        "stream_key": info.get("streamName"),
    }

def create_stream(access_token, title, reusable=True):

    body = {
        "snippet": {"title": title},
        "cdn": {
            "frameRate": "variable",
            "ingestionType": "rtmp",
            "resolution": "variable",
        },
        "contentDetails": {"isReusable": bool(reusable)},
    }
    result = _make_request(
        "POST", config.YOUTUBE_STREAMS_URL, access_token,
        params={"part": "snippet,cdn,contentDetails"}, body=body,
    )
    stream_id = result.get("id")
    ingestion_info = result.get("cdn", {}).get("ingestionInfo", {})
    ingestion = ingestion_info.get("ingestionAddress")
    stream_name = ingestion_info.get("streamName")
    if not (stream_id and ingestion and stream_name):
        raise YouTubeAPIError(f"Не получен stream_id/ingestion: {result}")
    config.log.info(f"YouTube stream создан: {stream_id} "
                    f"(reusable={reusable})")
    return {
        "id": stream_id,
        "rtmp_url": ingestion,
        "stream_key": stream_name,
    }

def bind_broadcast_to_stream(access_token, broadcast_id, stream_id):

    _make_request(
        "POST", config.YOUTUBE_BROADCASTS_URL + "/bind", access_token,
        params={
            "id": broadcast_id,
            "streamId": stream_id,
            "part": "id,snippet,contentDetails,status",
        },
    )
    config.log.info(f"YouTube broadcast {broadcast_id} ↔ stream {stream_id}")

def _shrink_thumbnail(content, fname):

    max_size = 2 * 1024 * 1024
    try:
        from PIL import Image
    except ImportError:
        raise YouTubeAPIError(
            "Размер превью превышает 2 MB. Сожмите изображение (JPEG до "
            "1920px, до 2 MB) или установите Pillow: pip install pillow")

    try:
        img = Image.open(io.BytesIO(content)).convert("RGB")
        for max_side, quality in ((1920, 85), (1920, 75), (1600, 70),
                                  (1280, 60), (1280, 50)):
            if max(img.size) > max_side:
                img.thumbnail((max_side, max_side), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= max_size:
                config.log.info(
                    f"Превью >2 MB — сжато Pillow до "
                    f"{len(data) // 1024} KB (JPEG q{quality})")
                stem = os.path.splitext(fname)[0]
                return data, stem + ".jpg", "image/jpeg"
        raise YouTubeAPIError(
            "Превью не удалось ужать до 2 MB даже через Pillow — "
            "используйте другое изображение")
    except YouTubeAPIError:
        raise
    except Exception as e:
        raise YouTubeAPIError(f"Превью >2 MB, сжатие не удалось: {e}")

def upload_thumbnail(access_token, video_id, image_path):

    if not image_path or not os.path.isfile(image_path):
        raise YouTubeAPIError(f"Файл превью не найден: {image_path}")

    ext = os.path.splitext(image_path)[1].lower()
    content_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".png": "image/png"}
    if ext not in content_types:
        raise YouTubeAPIError(
            f"Неподдерживаемый формат превью: {ext}. Допускаются JPG, PNG.")

    with open(image_path, "rb") as f:
        content = f.read()
    fname = os.path.basename(image_path)
    ctype = content_types[ext]
    if len(content) > 2 * 1024 * 1024:
        content, fname, ctype = _shrink_thumbnail(content, fname)

    _make_request(
        "POST", config.YOUTUBE_THUMBNAIL_URL, access_token,
        params={"videoId": video_id},
        is_multipart=True,
        multipart_data={"image": (fname, content, ctype)},
    )
    config.log.info(f"YouTube превью загружено для {video_id}")

def get_video_categories(access_token):

    result = _make_request(
        "GET", config.YOUTUBE_VIDEO_CATEGORIES_URL, access_token,
        params={"part": "snippet"},
    )
    cats = []
    for item in result.get("items", []):
        snippet = item.get("snippet", {})
        if snippet.get("assignable"):
            cats.append({"id": item.get("id"), "title": snippet.get("title")})
    return cats

def set_video_category(access_token, video_id, title, description=None,
                       tags=None, category_id="20"):

    body = {
        "id": video_id,
        "snippet": {
            "title": title,
            "description": description or "",
            "categoryId": str(category_id),
        },
    }
    if tags:
        clean_tags = []
        total_len = 0
        for tag in tags:
            tag = (tag or "").strip()
            if not tag:
                continue
            if total_len + len(tag) > 480:
                break
            clean_tags.append(tag)
            total_len += len(tag)
        if clean_tags:
            body["snippet"]["tags"] = clean_tags
    _make_request(
        "PUT", config.YOUTUBE_VIDEOS_URL, access_token,
        params={"part": "snippet"}, body=body,
    )
    config.log.info(
        f"YouTube: категория видео {video_id} -> {category_id}")

def transition_broadcast(access_token, broadcast_id, status):

    if status not in ("testing", "live", "complete"):
        raise YouTubeAPIError(f"Неверный статус transition: {status}")
    _make_request(
        "POST", config.YOUTUBE_BROADCASTS_URL + "/transition", access_token,
        params={
            "id": broadcast_id,
            "broadcastStatus": status,
            "part": "id,snippet,contentDetails,status",
        },
    )
    config.log.info(f"YouTube broadcast {broadcast_id} -> {status}")

def delete_broadcast(access_token, broadcast_id):

    _make_request(
        "DELETE", config.YOUTUBE_BROADCASTS_URL, access_token,
        params={"id": broadcast_id},
    )
    config.log.info(f"YouTube broadcast {broadcast_id} удалён")

def get_broadcast_live_chat_id(access_token, broadcast_id):

    result = _make_request(
        "GET", config.YOUTUBE_VIDEOS_URL, access_token,
        params={"id": broadcast_id, "part": "liveStreamingDetails"},
    )
    items = result.get("items", [])
    if not items:
        raise YouTubeAPIError(f"Видео {broadcast_id} не найдено")
    chat_id = items[0].get("liveStreamingDetails", {}).get("activeLiveChatId")
    if not chat_id:
        raise YouTubeAPIError("activeLiveChatId не найден (broadcast ещё не live)")
    return chat_id

def get_video_viewers(access_token, video_id):

    result = _make_request(
        "GET", config.YOUTUBE_VIDEOS_URL, access_token,
        params={"id": video_id, "part": "liveStreamingDetails"},
    )
    items = result.get("items", [])
    if not items:
        return None
    return items[0].get("liveStreamingDetails", {}).get("concurrentViewers")

def list_live_chat_messages(access_token, live_chat_id, page_token=None):

    params = {
        "liveChatId": live_chat_id,
        "part": "authorDetails,snippet",
        "maxResults": 200,
    }
    if page_token:
        params["pageToken"] = page_token

    result = _make_request(
        "GET", config.YOUTUBE_LIVECHAT_URL, access_token, params=params,
    )
    items = []
    for item in result.get("items", []):
        author_details = item.get("authorDetails", {})
        snippet = item.get("snippet", {})
        items.append({
            "author": author_details.get("displayName", "Аноним"),
            "text": snippet.get("displayMessage", ""),
            "is_owner": author_details.get("isChatOwner", False),
            "is_moderator": author_details.get("isChatModerator", False),
        })
    return {
        "items": items,
        "next_page_token": result.get("nextPageToken"),
        "polling_interval_millis": result.get("pollingIntervalMillis", 5000),
    }
