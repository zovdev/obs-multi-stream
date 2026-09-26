
import os
import sys
import importlib

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

_MODULE_NAMES = [
    "config",
    "secret_store",
    "obs_bridge",
    "oauth_flow",
    "twitch_api",
    "youtube_api",
    "twitch_chat",
    "youtube_chat",
    "viewer_counter",
    "omr_bridge",
    "dock_server",
]

for _mod_name in _MODULE_NAMES:
    if _mod_name in sys.modules:
        try:
            importlib.reload(sys.modules[_mod_name])
        except Exception as _e:
            print(f"[obs-multi-stream] reload {_mod_name}: {_e}")

import config
import secret_store
import obs_bridge
import oauth_flow
import twitch_api
import youtube_api
import twitch_chat
import youtube_chat
import viewer_counter
import omr_bridge
import dock_server

import obspython as obs

import threading
import queue
import time
import json
import base64
import re
import uuid

_state = {
    "title": "Мой стрим",
    "description": "",
    "tags": "",
    "game_query": "",
    "yt_category": None,
    "thumbnail_path": "",
    "twitch_bitrate": config.DEFAULT_TWITCH_BITRATE,
    "youtube_bitrate": config.DEFAULT_YOUTUBE_BITRATE,
    "twitch_audio_track": config.DEFAULT_AUDIO_TRACK,
    "youtube_audio_track": config.DEFAULT_AUDIO_TRACK,

    "dock_http_port": config.DEFAULT_DOCK_HTTP_PORT,
    "dock_ws_port": config.DEFAULT_DOCK_WS_PORT,

    "twitch": {
        "token": None,
        "refresh_token": None,
        "user_id": None,
        "username": None,
        "scopes": [],
        "connected": False,
        "missing_scopes": [],
        "api_ok": None,
        "api_error": None,
        "channel_title": None,
        "in_progress": False,
    },
    "youtube": {
        "token": None,
        "refresh_token": None,
        "token_expires_at": None,
        "email": None,
        "scopes": [],
        "connected": False,
        "missing_scopes": [],
        "api_ok": None,
        "api_error": None,
        "channel_title": None,
        "channel_id": None,
        "source": None,
        "obs_client_found": False,
        "obs_connected": False,
        "client_configured": False,
        "client_id": None,
        "in_progress": False,
    },

    "streaming": False,
    "stream_started_at": None,
    "youtube_broadcast_id": None,
    "youtube_video_id": None,
    "youtube_live_chat_id": None,
    "youtube_broadcast_reused": False,
    "engine": None,
    "engine_info": None,
    "yt_stream": None,
    "last_preset_id": None,
    "last_viewers": {"twitch": None, "youtube": None},

    "dock_server": None,
    "viewer_counter": None,
    "twitch_chat_reader": None,
    "youtube_chat_reader": None,
    "chat_queue": None,
    "stream_starting": False,
    "stream_stopping": False,
    "verify_lock": threading.Lock(),
    "yt_token_refresher": None,
}

_main_task_queue = queue.Queue()
_main_thread_id = None
_dispatcher_thread_id = None

def _main_task_tick():

    global _dispatcher_thread_id
    _dispatcher_thread_id = threading.get_ident()
    while True:
        try:
            task = _main_task_queue.get_nowait()
        except queue.Empty:
            return
        try:
            task()
        except Exception as e:
            config.log.error(f"Main-thread task failed: {e}", exc_info=True)

def run_on_main(fn, timeout=30.0, description="task"):

    tid = threading.get_ident()
    if ((_main_thread_id is not None and tid == _main_thread_id)
            or (_dispatcher_thread_id is not None
                and tid == _dispatcher_thread_id)):
        fn()
        return
    done = threading.Event()

    def _task():
        try:
            fn()
        finally:
            done.set()

    _main_task_queue.put(_task)
    if not done.wait(timeout):
        raise RuntimeError(
            f"Поток-диспетчер OBS не выполнил {description} за "
            f"{timeout:.0f} с (задач в очереди: "
            f"{_main_task_queue.qsize()}; OBS занят или выключается?)")

def _measure_stream_bitrates(now):

    counters = omr_bridge.collect_stream_output_stats()
    if counters is None:
        return {}
    prev = _measure_stream_bitrates.last
    dt = now - _measure_stream_bitrates.last_ts
    result = {}
    for platform, total in counters.items():
        if prev is not None and platform in prev and dt >= 1:
            result[platform] = max(0, int((total - prev[platform]) * 8
                                          / dt / 1000))
    _measure_stream_bitrates.last = dict(counters)
    _measure_stream_bitrates.last_ts = now
    return result

_measure_stream_bitrates.last = None
_measure_stream_bitrates.last_ts = 0.0

def _reset_stream_bitrate_counters():

    _measure_stream_bitrates.last = None
    _measure_stream_bitrates.last_ts = 0.0

def _engine_tick():

    try:
        engine = _state.get("engine")
        if not engine:
            return
        try:
            status = engine.status()
        except Exception as e:
            config.log.debug(f"engine status failed: {e}")
            return

        now = time.time()
        kbps = _measure_stream_bitrates(now)

        viewers = _state.get("last_viewers") or {}
        stats = {
            "twitch": {
                "live": viewers.get("twitch") is not None,
                "viewers": viewers.get("twitch"),
                "kbps": kbps.get("twitch"),
            },
            "youtube": {
                "live": viewers.get("youtube") is not None,
                "viewers": viewers.get("youtube"),
                "kbps": kbps.get("youtube"),
            },
            "obs_streaming": status.get("streaming_active"),
        }
        if _state.get("dock_server"):
            _state["dock_server"].send_output_stats(stats)
    except Exception as e:
        config.log.warning(f"engine tick: {e}")

def _frontend_event_callback(event):

    try:
        if event == obs.OBS_FRONTEND_EVENT_STREAMING_STARTING:
            if not _state.get("stream_starting") and not _state.get("streaming"):
                config.log.info(
                    "Обнаружен запуск стрима НЕ из дока — sync-цели "
                    "obs-multi-rtmp стартуют автоматически (если настроены)")
                if _state.get("dock_server"):
                    _state["dock_server"].send_log(
                        "warning",
                        "Стрим запущен кнопкой OBS — цели obs-multi-rtmp "
                        "запущены в режиме синхронизации. Управляйте "
                        "эфиром из этой панели для полного контроля.")
        elif event == obs.OBS_FRONTEND_EVENT_STREAMING_STOPPED:
            if (_state.get("streaming") and not _state.get("stream_stopping")
                    and not _state.get("stream_starting")):
                config.log.info("Стрим остановлен снаружи — завершаю сессию")
                threading.Thread(
                    target=_stop_stream_worker, daemon=True).start()
    except Exception as e:
        config.log.debug(f"frontend event error: {e}")

def script_description():
    return (
        "<h2>OBS Multi-Stream v3.21.1 (YouTube + Twitch)</h2>"
        "<p>Одновременный стрим на Twitch и YouTube с едиными настройками. "
        "Всё управление — в док-панели: "
        "View → Docks → Custom Browser Docks → "
        "URL: <b>http://127.0.0.1:7664/</b></p>"
        "<p><b>Шаг 1:</b> авторизация Twitch/YouTube — вход в Google "
        "выполняется прямо в док-панели OBS (вебвью), без системного "
        "браузера; клиент OBS встроен в плагин (свой Google-клиент "
        "НЕ нужен).<br>"
        "<b>Шаг 2:</b> настройки стрима (автосохранение).<br>"
        "<b>Шаг 3:</b> запуск обеих трансляций одной кнопкой.</p>"
        "<p>Стриминг выполняет плагин <b>obs-multi-rtmp</b> (0.6+, "
        "https://github.com/sorayuki/obs-multi-rtmp): этот скрипт создаёт "
        "в нём две цели с ключами и битрейтами и запускает их "
        "синхронно с основным выходом OBS (в локальный sink). "
        "Пакеты: websockets, websocket-client. См. README.md.</p>"
    )

def script_defaults(settings):
    obs.obs_data_set_default_int(settings, "dock_http_port", config.DEFAULT_DOCK_HTTP_PORT)
    obs.obs_data_set_default_int(settings, "dock_ws_port", config.DEFAULT_DOCK_WS_PORT)

def script_properties():

    props = obs.obs_properties_create()

    obs.obs_properties_add_text(
        props, "dock_info",
        "👉 ВСЁ управление — в док-панели (Custom Browser Dock):\n"
        "http://127.0.0.1:7664/\n"
        "Здесь только порты док-сервера.",
        obs.OBS_TEXT_INFO
    )

    p = obs.obs_properties_create()
    obs.obs_properties_add_int(p, "dock_http_port", "HTTP port", 1, 65535, 1)
    obs.obs_properties_add_int(p, "dock_ws_port", "WebSocket port", 1, 65535, 1)
    obs.obs_properties_add_group(
        props, "dock_group", "Док-панель (порты)",
        obs.OBS_GROUP_NORMAL, p
    )

    return props

def script_update(settings):
    _state["dock_http_port"] = obs.obs_data_get_int(settings, "dock_http_port")
    _state["dock_ws_port"] = obs.obs_data_get_int(settings, "dock_ws_port")
    config.log.info("Settings updated")

def script_load(settings):
    config.log.info("OBS Multi-Stream v3.21.1 loading...")

    global _main_thread_id
    _main_thread_id = threading.get_ident()

    _load_stream_settings()
    _load_cached_tokens()

    _state["chat_queue"] = queue.Queue()

    omr_bridge.set_task_dispatcher(run_on_main)
    _state["engine"] = omr_bridge.OmrEngine()
    try:
        _state["engine_info"] = omr_bridge.detect()
        info = _state["engine_info"]
        if info["state"] == "ok":
            config.log.info(
                f"obs-multi-rtmp: найден (версия "
                f"{info.get('version') or '?'})")
        elif info["state"] == "old_version":
            config.log.warning(
                f"obs-multi-rtmp {info.get('version')} слишком старый — "
                f"нужен 0.6+: {info['url']}")
        else:
            config.log.warning(
                "obs-multi-rtmp не найден в процессе OBS — стрим не "
                f"запустится. Установите: {info['url']}")
    except Exception as e:
        config.log.error(f"obs-multi-rtmp detect failed: {e}")

    obs.timer_add(_main_task_tick, 100)
    obs.timer_add(_engine_tick, 5000)

    try:
        obs.obs_frontend_add_event_callback(_frontend_event_callback)
    except Exception as e:
        config.log.warning(f"add_event_callback: {e}")

    try:
        assets_dir = _SCRIPT_DIR
        _state["dock_server"] = dock_server.DockServer(
            http_port=_state["dock_http_port"],
            ws_port=_state["dock_ws_port"],
            assets_dir=assets_dir,
        )
        _state["dock_server"].set_command_handler(_on_dock_command)
        if not _state["dock_server"].start():
            config.log.error(
                "❌ Dock HTTP server не поднялся (порт занят?). "
                "Смените HTTP port в настройках скрипта и перезагрузите скрипт."
            )
    except Exception as e:
        config.log.error(f"Failed to start dock server: {e}", exc_info=True)

    if _load_live_session():
        _send_log(
            "warning",
            "Обнаружена незавершённая трансляция YouTube (прошлый эфир "
            "прерван сбоем OBS/ПК). При запуске она будет продолжена — "
            "новая трансляция создаваться не будет")

    _state["viewer_counter"] = viewer_counter.ViewerCounter(on_update=_on_viewers_update)
    _state["viewer_counter"].start()
    threading.Thread(target=_chat_dispatcher, daemon=True).start()

    threading.Thread(target=_startup_verification_worker, daemon=True).start()

    def _recovery():
        time.sleep(15)
        try:
            engine = _state.get("engine")
            if engine and _state.get("dock_server"):
                _state["dock_server"].send_engine_status(engine.status())
            if engine and engine.recover_if_needed():
                _send_log("warning",
                          "Восстановлены настройки профиля, оставшиеся "
                          "после аварийного завершения прошлого эфира")
        except Exception as e:
            config.log.debug(f"recovery: {e}")

    threading.Thread(target=_recovery, daemon=True).start()

    _broadcast_auth_status()
    _broadcast_settings()
    _broadcast_presets()
    _broadcast_engine_status()

    config.log.info("OBS Multi-Stream v3.21.1 loaded")

def script_unload():
    config.log.info("OBS Multi-Stream v3 unloading...")

    try:
        obs.timer_remove(_main_task_tick)
    except Exception:
        pass
    try:
        obs.timer_remove(_engine_tick)
    except Exception:
        pass
    global _dispatcher_thread_id
    _dispatcher_thread_id = None
    try:
        obs.obs_frontend_remove_event_callback(_frontend_event_callback)
    except Exception:
        pass

    engine = _state.get("engine")
    if engine:
        try:
            if _state.get("streaming"):
                config.log.warning(
                    "Выгрузка скрипта во время эфира — останавливаю стрим")
                try:
                    _state["stream_stopping"] = True
                    engine.go_down()
                except Exception as e:
                    config.log.error(f"engine shutdown: {e}")
            else:
                try:
                    engine.sink.stop()
                except Exception:
                    pass
                try:
                    if engine.backup.exists():
                        engine.recover_if_needed()
                except Exception as e:
                    config.log.warning(f"backup recovery: {e}")
        except Exception as e:
            config.log.error(f"Engine cleanup error: {e}")

    if _state["twitch_chat_reader"]:
        try:
            _state["twitch_chat_reader"].stop()
        except Exception:
            pass
    if _state["youtube_chat_reader"]:
        try:
            _state["youtube_chat_reader"].stop()
        except Exception:
            pass
    if _state["viewer_counter"]:
        try:
            _state["viewer_counter"].stop()
        except Exception:
            pass
    if _state["dock_server"]:
        try:
            _state["dock_server"].stop()
        except Exception as e:
            config.log.error(f"Error stopping dock server: {e}")

    _state["streaming"] = False
    _state["stream_stopping"] = False

    for _mod_name in _MODULE_NAMES:
        sys.modules.pop(_mod_name, None)

def _load_cached_tokens():
    tw = _state["twitch"]
    cached = oauth_flow.get_cached_twitch_oauth_token()
    if cached:
        tw["token"] = cached["token"]
        tw["refresh_token"] = cached.get("refresh_token")
        tw["user_id"] = cached.get("user_id")
        tw["username"] = cached.get("username")
        tw["scopes"] = cached.get("scopes", [])
        twitch_api.set_active_client_id(cached.get("client_id"))
        config.log.info(f"Twitch: токен из кеша: {tw['username']} "
                        f"(id={tw['user_id']}, "
                        f"client={cached.get('client_id') or 'default'})")

    yt = _state["youtube"]
    cached = oauth_flow.get_cached_youtube_oauth_token()
    if cached:
        yt["token"] = cached["token"]
        yt["refresh_token"] = cached.get("refresh_token")
        yt["email"] = cached.get("email")
        yt["scopes"] = cached.get("scopes", [])
        used_cid = cached.get("client_id_used")
        if used_cid:
            yt["source"] = ("obs" if obs_bridge.is_obs_client_id(used_cid)
                            else "manual")
        config.log.info(f"YouTube: токен из кеша: {yt['email']} "
                        f"(источник: {yt['source'] or 'неизвестен'})")

    client = oauth_flow.get_google_client()
    yt["client_configured"] = bool(client)
    yt["client_id"] = client["client_id"] if client else None
    if client:
        config.log.info("YouTube: свой Google OAuth клиент настроен "
                        f"(client_id={client['client_id'][:24]}...)")

    yt["obs_client_found"] = bool(obs_bridge.get_effective_youtube_client())

def _startup_verification_worker():
    if _state["twitch"]["in_progress"] or _state["youtube"]["in_progress"]:
        return
    with _state["verify_lock"]:
        try:
            obs_client = obs_bridge.get_cached_youtube_client()
            if not (obs_client and obs_client.get("client_secret")):
                obs_client = obs_bridge.extract_youtube_client()
            _state["youtube"]["obs_client_found"] = bool(
                obs_bridge.get_effective_youtube_client())
        except Exception as e:
            config.log.warning(f"OBS bridge: извлечение клиента не удалось: {e}")
        try:
            _verify_twitch()
        except Exception as e:
            config.log.error(f"Twitch verification failed: {e}", exc_info=True)
        try:
            _verify_youtube()
        except Exception as e:
            config.log.error(f"YouTube verification failed: {e}", exc_info=True)
    _broadcast_auth_status()

def _verify_twitch():

    st = _state["twitch"]
    st["missing_scopes"] = []
    st["api_ok"] = None
    st["api_error"] = None

    if not st["token"]:
        st["connected"] = False
        return st

    def _apply_validate(v):
        st["user_id"] = v.get("user_id")
        st["username"] = v.get("login")
        st["scopes"] = v.get("scopes", [])
        if v.get("client_id"):
            twitch_api.set_active_client_id(v["client_id"])

    validated = False
    try:
        v = oauth_flow.twitch_validate_token(st["token"])
        _apply_validate(v)
        validated = True
    except oauth_flow.OAuthFlowError as e:
        config.log.warning(f"Twitch /validate failed: {e}")
        if st.get("refresh_token"):
            try:
                config.log.info("Twitch: refresh токена...")
                r = oauth_flow.twitch_refresh_token(st["refresh_token"])
                st["token"] = r["access_token"]
                if r.get("refresh_token"):
                    st["refresh_token"] = r["refresh_token"]
                v = oauth_flow.twitch_validate_token(st["token"])
                _apply_validate(v)
                oauth_flow.save_twitch_oauth_token(
                    st["token"], st.get("refresh_token"),
                    user_id=st["user_id"], username=st["username"],
                    scopes=st["scopes"],
                    client_id=twitch_api.get_active_client_id())
                validated = True
                config.log.info("Twitch: токен обновлён через refresh")
            except Exception as re_:
                config.log.warning(f"Twitch refresh failed: {re_}")

    if not validated:
        st["connected"] = False
        st["api_ok"] = False
        st["api_error"] = "Токен невалиден — авторизуйтесь заново"
        return st
    st["connected"] = True

    st["missing_scopes"] = oauth_flow.missing_twitch_scopes(st["scopes"])

    def _api_check():
        ch = twitch_api.get_channel_info(st["token"], st["user_id"])
        st["channel_title"] = ch.get("title")
        st["api_ok"] = True
        config.log.info(
            f"Twitch API OK: канал '{st['username']}', "
            f"title='{ch.get('title')}'")

    try:
        _api_check()
    except Exception as e:
        err_text = str(e)
        if "404" in err_text and st.get("refresh_token"):
            config.log.warning(
                f"Twitch API: {err_text} Пробую refresh и повторную проверку…")
            try:
                r = oauth_flow.twitch_refresh_token(st["refresh_token"])
                st["token"] = r["access_token"]
                if r.get("refresh_token"):
                    st["refresh_token"] = r["refresh_token"]
                v = oauth_flow.twitch_validate_token(st["token"])
                _apply_validate(v)
                oauth_flow.save_twitch_oauth_token(
                    st["token"], st.get("refresh_token"),
                    user_id=st["user_id"], username=st["username"],
                    scopes=st["scopes"],
                    client_id=twitch_api.get_active_client_id())
                _api_check()
            except Exception as e2:
                st["api_ok"] = False
                st["api_error"] = str(e2)
                config.log.warning(f"Twitch API check failed: {e2}")
        else:
            st["api_ok"] = False
            st["api_error"] = err_text
            config.log.warning(f"Twitch API check failed: {err_text}")

    return st

def _yt_resolve_client():

    obs_client = obs_bridge.get_effective_youtube_client()
    manual_client = oauth_flow.get_google_client()
    used_cid = oauth_flow.get_youtube_client_id_used()

    if obs_client and obs_client.get("client_id"):
        if not used_cid or used_cid == obs_client["client_id"]:
            return obs_client, "obs"
    if manual_client and manual_client.get("client_id"):
        if not used_cid or used_cid == manual_client["client_id"]:
            return manual_client, "manual"
    if manual_client and manual_client.get("client_id"):
        return manual_client, "manual"
    if obs_client and obs_client.get("client_id"):
        return obs_client, "obs"
    return None, None

def _refresh_youtube_access_token():

    st = _state["youtube"]
    client, source = _yt_resolve_client()
    if not (st.get("refresh_token") and client):
        raise oauth_flow.OAuthFlowError(
            "Нет refresh_token или OAuth-клиента для обновления токена")

    r = oauth_flow.youtube_refresh_token(
        st["refresh_token"], client["client_id"],
        client.get("client_secret"))

    st["token"] = r["access_token"]
    if r.get("refresh_token"):
        st["refresh_token"] = r["refresh_token"]
    if r.get("scope"):
        st["scopes"] = r["scope"].split()
    st["source"] = source
    try:
        ttl = int(r.get("expires_in") or config.YOUTUBE_TOKEN_TTL_FALLBACK)
    except (TypeError, ValueError):
        ttl = config.YOUTUBE_TOKEN_TTL_FALLBACK
    st["token_expires_at"] = time.time() + ttl

    oauth_flow.save_youtube_oauth_token(
        st["token"], st.get("refresh_token"),
        email=st.get("email"), scopes=st.get("scopes"),
        client_id_used=client["client_id"])
    if _state.get("youtube_chat_reader"):
        _state["youtube_chat_reader"].access_token = st["token"]
    if _state.get("viewer_counter"):
        _state["viewer_counter"].update_credentials(
            youtube_access_token=st["token"])
    return r

def _ensure_fresh_youtube_token():

    st = _state["youtube"]
    expires_at = st.get("token_expires_at")
    fresh = (expires_at is not None
             and time.time() < expires_at - config.YOUTUBE_TOKEN_REFRESH_MARGIN)
    if fresh:
        return
    if not (st.get("refresh_token") and _yt_resolve_client()[0]):
        _send_log("info",
                  "YouTube: срок жизни токена неизвестен — при 401 обновлю "
                  "и повторю запрос по ходу запуска")
        return
    _send_log("info", "YouTube: обновляю access_token перед запуском...")
    try:
        _refresh_youtube_access_token()
        _send_log("success", "YouTube: access_token обновлён")
    except oauth_flow.OAuthFlowError as e:
        _send_log("warning",
                  f"YouTube: предварительное обновление токена не удалось "
                  f"({e}) — продолжаю с текущим; при 401 обновлю и повторю")

def _yt_call(fn, *args, **kwargs):

    try:
        return fn(_state["youtube"]["token"], *args, **kwargs)
    except youtube_api.YouTubeAPIError as e:
        if getattr(e, "http_code", None) != 401:
            raise
        _send_log("info", "YouTube: токен истёк (401) — обновляю и повторяю...")
        try:
            _refresh_youtube_access_token()
        except oauth_flow.OAuthFlowError:
            raise e
        return fn(_state["youtube"]["token"], *args, **kwargs)

def _refresh_twitch_access_token():

    st = _state["twitch"]
    if not st.get("refresh_token"):
        raise oauth_flow.OAuthFlowError(
            "Нет refresh_token — переподключите Twitch (Шаг 1)")
    r = oauth_flow.twitch_refresh_token(st["refresh_token"])
    st["token"] = r["access_token"]
    if r.get("refresh_token"):
        st["refresh_token"] = r["refresh_token"]
    oauth_flow.save_twitch_oauth_token(
        st["token"], st.get("refresh_token"),
        user_id=st["user_id"], username=st["username"],
        scopes=st["scopes"],
        client_id=twitch_api.get_active_client_id())
    if _state.get("viewer_counter"):
        _state["viewer_counter"].update_credentials(
            twitch_access_token=st["token"])
    return r

def _tw_call(fn, *args, **kwargs):

    try:
        return fn(_state["twitch"]["token"], *args, **kwargs)
    except twitch_api.TwitchAPIError as e:
        if getattr(e, "http_code", None) != 401:
            raise
        _send_log("info", "Twitch: токен истёк (401) — обновляю и повторяю...")
        try:
            _refresh_twitch_access_token()
        except Exception:
            raise e
        return fn(_state["twitch"]["token"], *args, **kwargs)

def _verify_youtube():

    st = _state["youtube"]
    st["missing_scopes"] = []
    st["api_ok"] = None
    st["api_error"] = None

    client, source = _yt_resolve_client()
    st["obs_client_found"] = bool(obs_bridge.get_effective_youtube_client())
    st["client_configured"] = bool(oauth_flow.get_google_client())

    if not st["token"] and not st.get("refresh_token"):
        obs_conn = obs_bridge.read_obs_youtube_connection()
        if obs_conn and (obs_conn.get("refresh_token")
                         or obs_conn.get("token")):
            st["obs_connected"] = True
            st["source"] = "obs"
            if obs_conn.get("refresh_token"):
                st["refresh_token"] = obs_conn["refresh_token"]
            if obs_conn.get("token"):
                st["token"] = obs_conn["token"]
            if obs_conn.get("channel_name"):
                st["email"] = st.get("email") or obs_conn["channel_name"]
            st["client_id_used"] = (client or {}).get("client_id")
            config.log.info(
                "YouTube: найден вход, выполненный в OBS "
                f"(канал: {obs_conn.get('channel_name') or '?'})")
        else:
            st["obs_connected"] = False

    if not st["token"] and not (st.get("refresh_token") and client):
        st["connected"] = False
        return st

    refreshed = False
    if st.get("refresh_token") and client:
        try:
            _refresh_youtube_access_token()
            refreshed = True
            config.log.info(f"YouTube: access_token обновлён "
                            f"(клиент: {st.get('source')})")
        except oauth_flow.OAuthFlowError as e:
            config.log.warning(f"YouTube refresh failed: {e}"
                               + (f" | ПОДСКАЗКА: {e.hint}" if e.hint else ""))
            st["api_error"] = f"Refresh не удался: {e}"

    if not refreshed and not st["token"]:
        st["connected"] = False
        st["api_ok"] = False
        if not st.get("api_error"):
            st["api_error"] = "Нет валидного токена — авторизуйтесь"
        return st

    if not refreshed:
        try:
            info = oauth_flow.google_tokeninfo(st["token"])
            if info.get("exp"):
                try:
                    st["token_expires_at"] = int(info["exp"])
                except (TypeError, ValueError):
                    pass
            if info.get("scope"):
                st["scopes"] = info["scope"].split()
            if info.get("email"):
                st["email"] = info["email"]
        except oauth_flow.OAuthFlowError as e:
            st["connected"] = False
            st["api_ok"] = False
            st["api_error"] = f"Токен истёк: {e}"
            return st

    st["connected"] = True

    st["missing_scopes"] = oauth_flow.missing_youtube_scopes(st["scopes"])

    try:
        ch = youtube_api.get_my_channel(st["token"])
        st["channel_title"] = ch.get("title")
        st["channel_id"] = ch.get("id")
        st["api_ok"] = True
        config.log.info(
            f"YouTube API OK: канал '{ch.get('title')}' ({st.get('email')})")
    except Exception as e:
        st["api_ok"] = False
        st["api_error"] = str(e)
        config.log.warning(f"YouTube API check failed: {e}")

    return st

def _twitch_verified():
    st = _state["twitch"]
    return (st["connected"] and not st["missing_scopes"]
            and st["api_ok"] is True)

def _youtube_verified():
    st = _state["youtube"]
    return (st["connected"] and not st["missing_scopes"]
            and st["api_ok"] is True)

_state["oauth_session"] = {"twitch": None, "youtube": None}

def _dock_connected():

    server = _state.get("dock_server")
    return bool(server and server.client_count() > 0)

def _dock_base_url():

    return f"http://127.0.0.1:{_state.get('dock_http_port') or 7664}"

def _oauth_progress(platform, stage, message, user_code=None, url=None):

    config.log.info(f"[{platform}] [{stage}] {message}")
    session = _state.get("oauth_session")
    if session is not None:
        entry = session.get(platform) or {}
        entry.update({
            "platform": platform,
            "stage": stage,
            "message": message,
            "user_code": user_code if user_code is not None
                        else entry.get("user_code"),
            "url": url if url is not None else entry.get("url"),
            "ts": time.time(),
        })
        session[platform] = entry
    if _state.get("dock_server"):
        _state["dock_server"].send_oauth_progress(
            platform, stage, message, user_code=user_code, url=url)

def _oauth_session_clear(platform):
    session = _state.get("oauth_session")
    if session is not None:
        session[platform] = None

def _broadcast_oauth_session():

    if _state.get("dock_server"):
        _state["dock_server"].send_oauth_session(_state.get("oauth_session"))

def _twitch_oauth_flow_worker():
    st = _state["twitch"]
    if st["in_progress"]:
        _send_log("warning", "Twitch: авторизация уже идёт")
        return
    st["in_progress"] = True
    _oauth_session_clear("twitch")
    _broadcast_auth_status()
    try:
        _oauth_progress("twitch", "start", "Запуск Twitch Device Code Flow...")

        result = oauth_flow.twitch_device_code_start()
        device_code = result["device_code"]
        user_code = result["user_code"]
        verification_uri = result.get("verification_uri",
                                      "https://www.twitch.tv/activate")
        verification_uri_complete = result.get("verification_uri_complete") \
            or verification_uri

        _oauth_progress(
            "twitch", "user_code",
            "Введите код на twitch.tv/activate (кнопка в панели)",
            user_code=user_code, url=verification_uri_complete)

        if not _dock_connected():
            try:
                import webbrowser
                webbrowser.open(verification_uri_complete)
            except Exception as e:
                config.log.warning(f"Не удалось открыть браузер: {e}")

        def on_status(status_type, message):
            if status_type != "waiting":
                _oauth_progress("twitch", status_type, message)
            else:
                _oauth_progress("twitch", "waiting", message,
                                user_code=user_code,
                                url=verification_uri_complete)

        token_result = oauth_flow.twitch_device_code_wait(
            device_code, on_status=on_status,
            timeout=result.get("expires_in", 1800),
            poll_interval=result.get("interval", 5),
        )

        st["token"] = token_result["access_token"]
        st["refresh_token"] = token_result.get("refresh_token")
        if token_result.get("scope"):
            st["scopes"] = (
                token_result["scope"]
                if isinstance(token_result["scope"], list)
                else token_result["scope"].split()
            )
        twitch_api.set_active_client_id(config.TWITCH_CLIENT_ID)

        _oauth_progress("twitch", "verifying", "Проверка токена и прав...")
        oauth_flow.save_twitch_oauth_token(
            st["token"], st.get("refresh_token"), scopes=st["scopes"],
            client_id=config.TWITCH_CLIENT_ID)
        _verify_twitch()

        if st["connected"] and not st["missing_scopes"] and st["api_ok"]:
            _send_log("success",
                      f"Twitch: авторизован как {st['username']} — "
                      f"управление трансляцией подтверждено")
        elif st["connected"] and st["missing_scopes"]:
            _send_log("warning",
                      f"Twitch: авторизован как {st['username']}, но не хватает "
                      f"скопов: {', '.join(st['missing_scopes'])}. "
                      f"API: {st['api_error'] or 'ok'}")
        elif st["connected"]:
            _send_log("warning",
                      f"Twitch: авторизован как {st['username']}, но проверка "
                      f"API не прошла: {st['api_error']}")
        else:
            _send_log("error", "Twitch: токен получен, но проверка не прошла")
    except Exception as e:
        st["connected"] = False
        st["api_ok"] = False
        hint = getattr(e, "hint", None)
        msg = f"Twitch OAuth: {e}" + (f"\nПОДСКАЗКА: {hint}" if hint else "")
        config.log.error(msg, exc_info=True)
        _send_log("error", msg)
    finally:
        st["in_progress"] = False
        _oauth_session_clear("twitch")
        _broadcast_auth_status()

def _youtube_run_flow(client, on_status):

    open_mode = "dock" if _dock_connected() else "browser"
    if client.get("is_own"):
        return oauth_flow.youtube_pkce_flow(
            client["client_id"], client["client_secret"],
            on_status=on_status,
            open_mode=open_mode, dock_base_url=_dock_base_url())
    return oauth_flow.youtube_obs_flow(
        client["client_id"], client["client_secret"],
        on_status=on_status,
        open_mode=open_mode, dock_base_url=_dock_base_url())

def _youtube_oauth_flow_worker():
    st = _state["youtube"]
    if st["in_progress"]:
        _send_log("warning", "YouTube: авторизация уже идёт")
        return

    st["in_progress"] = True
    _oauth_session_clear("youtube")
    _broadcast_auth_status()
    try:
        own_client = oauth_flow.get_google_client()
        obs_client = (obs_bridge.get_cached_youtube_client()
                      or obs_bridge.extract_youtube_client()
                      or obs_bridge.get_effective_youtube_client())

        def on_status(status_type, message):
            _oauth_progress(
                "youtube", status_type, message,
                url=message if status_type == "auth_url" else None)

        client = None
        if own_client and own_client.get("client_id") \
                and own_client.get("client_secret"):
            client = {"client_id": own_client["client_id"],
                      "client_secret": own_client["client_secret"],
                      "is_own": True}
            _oauth_progress("youtube", "start",
                            "Вход через Google (свой клиент, PKCE)…")
        elif obs_client and obs_client.get("client_id") \
                and obs_client.get("client_secret"):
            client = dict(obs_client)
            client["is_own"] = False
            _oauth_progress("youtube", "start",
                            "Вход через Google (клиент OBS)…")

        if client is None:
            _send_log("error",
                      "YouTube: клиент OAuth не определён (не должно "
                      "случаться — встроенный клиент всегда есть)")
            return

        try:
            token_result = _youtube_run_flow(client, on_status)
        except oauth_flow.OAuthFlowError as e:
            msg = str(e)
            if ("invalid_client" in msg and not client.get("is_own")):
                _send_log("warning",
                          "YouTube: извлечённый клиент отклонён Google — "
                          "повторяю встроенным проверенным клиентом OBS")
                for key in (config.STORE_OBS_YT_CLIENT_ID,
                            config.STORE_OBS_YT_CLIENT_SECRET,
                            config.STORE_OBS_YT_STAMP):
                    secret_store.delete_password(key)
                builtin = obs_bridge.get_effective_youtube_client()
                client = dict(builtin)
                client["is_own"] = False
                _oauth_session_clear("youtube")
                token_result = _youtube_run_flow(client, on_status)
            else:
                raise

        st["source"] = "manual" if client.get("is_own") else "obs"
        st["client_id_used"] = client["client_id"]
        used_client_id = client["client_id"]

        st["token"] = token_result["access_token"]
        st["refresh_token"] = token_result.get("refresh_token")
        if token_result.get("scope"):
            st["scopes"] = token_result["scope"].split()
        try:
            st["token_expires_at"] = time.time() + int(
                token_result.get("expires_in") or config.YOUTUBE_TOKEN_TTL_FALLBACK)
        except (TypeError, ValueError):
            st["token_expires_at"] = (
                time.time() + config.YOUTUBE_TOKEN_TTL_FALLBACK)
        if not st.get("email"):
            st["email"] = oauth_flow.google_get_userinfo(st["token"]) or None

        _oauth_progress("youtube", "verifying",
                        "Проверка скопов и доступа к YouTube API...")
        oauth_flow.save_youtube_oauth_token(
            st["token"], st.get("refresh_token"),
            email=st.get("email"), scopes=st["scopes"],
            client_id_used=used_client_id)
        _verify_youtube()

        if st["connected"] and not st["missing_scopes"] and st["api_ok"]:
            via = "клиент OBS" if st["source"] == "obs" else "свой клиент"
            _send_log("success",
                      f"YouTube: авторизован как {st['email']} "
                      f"(канал «{st['channel_title']}», {via}) — "
                      f"управление трансляцией подтверждено")
        else:
            missing = ", ".join(st["missing_scopes"]) or "—"
            _send_log(
                "warning" if st["connected"] else "error",
                f"YouTube: токен получен, но проверка не пройдена. "
                f"Скопы: ok/нет ({missing}). API: {st['api_error'] or 'ok'}")
    except Exception as e:
        st["connected"] = False
        st["api_ok"] = False
        hint = getattr(e, "hint", None)
        msg = f"YouTube OAuth: {e}" + (f"\nПОДСКАЗКА: {hint}" if hint else "")
        config.log.error(msg, exc_info=True)
        _send_log("error", msg)
    finally:
        st["in_progress"] = False
        _oauth_session_clear("youtube")
        _broadcast_auth_status()

def _on_dock_command(cmd):
    cmd_type = cmd.get("type")

    if cmd_type == "start":
        if _state["streaming"] or _state.get("stream_starting"):
            _send_log("warning", "Запуск уже выполняется или стрим уже идёт")
            return
        _state["stream_starting"] = True
        threading.Thread(target=_start_stream_worker, daemon=True).start()

    elif cmd_type == "stop":
        if _state["streaming"] and not _state.get("stream_stopping"):
            threading.Thread(target=_stop_stream_worker, daemon=True).start()

    elif cmd_type == "twitch_oauth":
        threading.Thread(target=_twitch_oauth_flow_worker, daemon=True).start()

    elif cmd_type == "youtube_oauth":
        threading.Thread(target=_youtube_oauth_flow_worker, daemon=True).start()

    elif cmd_type == "set_google_client":
        threading.Thread(
            target=_set_google_client_worker,
            args=(cmd.get("client_id", "").strip(),
                  cmd.get("client_secret", "").strip()),
            daemon=True).start()

    elif cmd_type == "recheck_auth":
        threading.Thread(target=_startup_verification_worker, daemon=True).start()

    elif cmd_type == "open_url":
        _open_url_in_system_browser(cmd.get("url", ""))

    elif cmd_type == "search_game":
        threading.Thread(
            target=_search_game_worker,
            args=(cmd.get("query", ""),),
            daemon=True).start()

    elif cmd_type == "update_settings":
        _apply_settings_from_dock(cmd.get("settings", {}))

    elif cmd_type == "preset_list":
        _broadcast_presets()

    elif cmd_type == "preset_select":
        _state["last_preset_id"] = cmd.get("id") or None
        _save_stream_settings()

    elif cmd_type == "preset_save":
        _preset_save_worker(cmd.get("name", ""), cmd.get("id"))

    elif cmd_type == "preset_rename":
        _preset_rename_worker(cmd.get("id"), cmd.get("name", ""))

    elif cmd_type == "preset_delete":
        _preset_delete_worker(cmd.get("id"))

    elif cmd_type == "preset_apply":
        _preset_apply_worker(cmd.get("id"))

    elif cmd_type == "upload_preview":
        threading.Thread(
            target=_upload_preview_worker,
            args=(cmd.get("name", ""), cmd.get("data_base64", "")),
            daemon=True).start()

    elif cmd_type == "get_settings":
        _broadcast_settings()

    elif cmd_type == "get_auth_status":
        _broadcast_auth_status()

    elif cmd_type == "get_oauth_session":
        _broadcast_oauth_session()

    elif cmd_type == "get_state":
        _broadcast_auth_status()
        _broadcast_oauth_session()
        _broadcast_settings()
        _broadcast_presets()
        _broadcast_engine_status()
        if _state.get("dock_server"):
            _state["dock_server"].send_stream_status(
                _state["streaming"], _state["stream_started_at"])

def _open_url_in_system_browser(url):
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return
    try:
        import webbrowser
        webbrowser.open(url)
        config.log.info(f"Открыто в системном браузере: {url[:120]}")
    except Exception as e:
        config.log.error(f"Не удалось открыть системный браузер: {e}")
        _send_log("error", f"Не удалось открыть ссылку в браузере: {e}")

_YT_CATEGORIES_CACHE = {"ts": 0.0, "items": []}

def _search_game_worker(query):

    query = (query or "").strip()
    if len(query) < 2:
        return
    candidates = []

    tw = _state["twitch"]
    if tw.get("token") and tw.get("connected"):
        try:
            for c in twitch_api.search_categories(tw["token"], query)[:10]:
                if c.get("name"):
                    candidates.append({
                        "group": "game",
                        "platform": "twitch",
                        "name": c["name"],
                        "id": c.get("id"),
                    })
        except Exception as e:
            config.log.debug(f"search_game twitch failed: {e}")

    yt_cats = []
    yt = _state["youtube"]
    if yt.get("token") and yt.get("connected"):
        try:
            now = time.time()
            if now - _YT_CATEGORIES_CACHE["ts"] > 600:
                _YT_CATEGORIES_CACHE["items"] = \
                    youtube_api.get_video_categories(yt["token"])
                _YT_CATEGORIES_CACHE["ts"] = now
            q = query.lower()
            for c in _YT_CATEGORIES_CACHE["items"]:
                title = (c.get("title") or "")
                if title and q in title.lower():
                    yt_cats.append({
                        "group": "category",
                        "platform": "youtube",
                        "name": title,
                        "id": c.get("id"),
                    })
        except Exception as e:
            config.log.debug(f"search_game youtube failed: {e}")

    if not any(c.get("id") == "20" for c in yt_cats):
        yt_cats.append({
            "group": "category",
            "platform": "youtube",
            "name": "Gaming",
            "id": "20",
            "is_default": True,
        })
    candidates.extend(yt_cats)

    if _state.get("dock_server"):
        _state["dock_server"].send_game_candidates(query, candidates)

def _set_google_client_worker(client_id, client_secret):
    if not client_id or not client_secret:
        _send_log("error",
                  "Google OAuth: нужны ОБА поля — Client ID и Client Secret "
                  "(тип клиента: Desktop application)")
        return
    oauth_flow.set_google_client(client_id, client_secret)
    st = _state["youtube"]
    st["client_configured"] = True
    st["client_id"] = client_id
    _send_log("success",
              "Свой Google-клиент сохранён. Теперь нажмите «Подключить "
              "YouTube» (или используйте «Перепроверить», если уже есть токен).")
    _broadcast_auth_status()

def _get_presets_path():
    return config.get_presets_path()

def _load_presets():

    path = _get_presets_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        presets = data.get("presets", []) if isinstance(data, dict) else []
        return [p for p in presets
                if isinstance(p, dict) and p.get("id") and p.get("name")]
    except Exception as e:
        config.log.error(f"Failed to load presets: {e}")
        return []

def _save_presets_file(presets):
    path = _get_presets_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"presets": presets}, f, indent=2, ensure_ascii=False)

def _preset_from_state(name, preset_id):

    return {
        "id": preset_id or f"p_{uuid.uuid4().hex[:10]}",
        "name": name,
        "title": _state["title"],
        "description": _state["description"],
        "tags": _state["tags"],
        "game_query": _state["game_query"],
        "yt_category": _state["yt_category"],
        "thumbnail_path": _state["thumbnail_path"],
        "twitch_bitrate": _state["twitch_bitrate"],
        "youtube_bitrate": _state["youtube_bitrate"],
        "twitch_audio_track": _state["twitch_audio_track"],
        "youtube_audio_track": _state["youtube_audio_track"],
        "updated_at": int(time.time()),
    }

def _broadcast_presets(select_id=None):
    if not _state.get("dock_server"):
        return
    if select_id is None:
        select_id = _state.get("last_preset_id")
    _state["dock_server"].send_presets(
        [{"id": p["id"], "name": p["name"]} for p in _load_presets()],
        select=select_id)

def _preset_save_worker(name, preset_id=None):

    name = (name or "").strip()
    if not name:
        _send_log("error", "Пресет: укажите название (пустым быть не может)")
        return
    presets = _load_presets()
    exists = bool(preset_id) and any(
        p.get("id") == preset_id for p in presets)
    if any((p.get("name") or "").strip().lower() == name.lower()
           and p.get("id") != preset_id for p in presets):
        _send_log("error",
                  f"Пресет «{name}» уже существует — выберите другое название")
        return
    data = _preset_from_state(name, preset_id if exists else None)
    if exists:
        presets = [data if p.get("id") == preset_id else p
                   for p in presets]
        _send_log("success", f"Пресет «{name}» обновлён текущими настройками")
    else:
        presets.append(data)
        _send_log("success", f"Пресет «{name}» создан")
    try:
        _save_presets_file(presets)
    except Exception as e:
        _send_log("error", f"Пресет: не удалось сохранить файл ({e})")
        return
    _state["last_preset_id"] = data["id"]
    _save_stream_settings()
    _broadcast_presets(select_id=data["id"])

def _preset_rename_worker(preset_id, name):
    name = (name or "").strip()
    if not preset_id or not name:
        _send_log("error", "Пресет: не выбран или имя пустое")
        return
    presets = _load_presets()
    target = next((p for p in presets if p.get("id") == preset_id), None)
    if not target:
        _send_log("error", "Пресет не найден (список мог измениться)")
        return
    if any((p.get("name") or "").strip().lower() == name.lower()
           and p.get("id") != preset_id for p in presets):
        _send_log("error",
                  f"Пресет «{name}» уже существует — выберите другое название")
        return
    old_name = target.get("name")
    target["name"] = name
    target["updated_at"] = int(time.time())
    try:
        _save_presets_file(presets)
    except Exception as e:
        _send_log("error", f"Пресет: не удалось сохранить файл ({e})")
        return
    _send_log("success", f"Пресет «{old_name}» переименован в «{name}»")
    _broadcast_presets(select_id=preset_id)

def _preset_delete_worker(preset_id):
    if not preset_id:
        _send_log("error", "Пресет: не выбран")
        return
    presets = _load_presets()
    target = next((p for p in presets if p.get("id") == preset_id), None)
    if not target:
        _send_log("error", "Пресет не найден (список мог измениться)")
        return
    presets = [p for p in presets if p.get("id") != preset_id]
    try:
        _save_presets_file(presets)
    except Exception as e:
        _send_log("error", f"Пресет: не удалось сохранить файл ({e})")
        return
    if _state.get("last_preset_id") == preset_id:
        _state["last_preset_id"] = None
        _save_stream_settings()
    _send_log("success", f"Пресет «{target['name']}» удалён")
    _broadcast_presets()

def _preset_apply_worker(preset_id):

    if not preset_id:
        return
    preset = next((p for p in _load_presets()
                   if p.get("id") == preset_id), None)
    if not preset:
        _send_log("error", "Пресет не найден (список мог измениться)")
        return
    settings = {
        "title": preset.get("title", ""),
        "description": preset.get("description", ""),
        "tags": preset.get("tags", ""),
        "game": preset.get("game_query", ""),
        "yt_category": preset.get("yt_category"),
        "twitch_bitrate": preset.get("twitch_bitrate",
                                     config.DEFAULT_TWITCH_BITRATE),
        "youtube_bitrate": preset.get("youtube_bitrate",
                                      config.DEFAULT_YOUTUBE_BITRATE),
    }
    for key in ("twitch_audio_track", "youtube_audio_track"):
        try:
            settings[key] = max(1, min(
                config.AUDIO_TRACKS_MAX,
                int(preset.get(key, config.DEFAULT_AUDIO_TRACK))))
        except (TypeError, ValueError):
            settings[key] = config.DEFAULT_AUDIO_TRACK
    if (preset.get("thumbnail_path") or "").strip():
        settings["thumbnail"] = str(preset["thumbnail_path"])
    _state["last_preset_id"] = preset_id
    _apply_settings_from_dock(settings)
    _save_stream_settings()
    _broadcast_settings()
    _send_log("success",
              f"Пресет «{preset.get('name')}» применён — поля заполнены")

_PREVIEW_MAX_BYTES = 8 * 1024 * 1024

def _upload_preview_worker(filename, data_base64):

    try:
        raw = base64.b64decode(data_base64 or "", validate=True)
    except Exception:
        _send_log("error", "Превью: не удалось декодировать данные файла")
        return
    if not raw:
        _send_log("error", "Превью: пустой файл")
        return
    if len(raw) > _PREVIEW_MAX_BYTES:
        _send_log("error", "Превью: файл больше 8 МБ — используйте меньший")
        return
    if raw[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    else:
        _send_log("error", "Превью: допускаются только изображения JPG/PNG")
        return
    last_seg = (filename or "").replace("\\", "/").split("/")[-1]
    stem = re.sub(r"[^\w\-. ]+", "_", last_seg).strip()
    stem = os.path.splitext(stem)[0][:60].strip() or "preview"
    try:
        dir_path = config.get_previews_dir()
        os.makedirs(dir_path, exist_ok=True)
        path = os.path.join(dir_path, f"{int(time.time())}_{stem}{ext}")
        with open(path, "wb") as f:
            f.write(raw)
    except Exception as e:
        _send_log("error", f"Превью: не удалось сохранить файл ({e})")
        return
    _send_log("success",
              f"Превью сохранено: {os.path.basename(path)} "
              f"({len(raw) // 1024} КБ)")
    if _state.get("dock_server"):
        _state["dock_server"].send_preview_uploaded(
            path, _thumbnail_preview_url(path))

def _cleanup_youtube_after_failed_start():

    broadcast_id = _state.get("youtube_broadcast_id")
    reused = _state.pop("youtube_broadcast_reused", False)
    token = (_state.get("youtube") or {}).get("token")
    _state["youtube_broadcast_id"] = None
    _state["youtube_video_id"] = None
    if not broadcast_id or not token:
        return
    if reused:
        _send_log(
            "warning",
            f"YouTube: запуск не удался, но прежняя трансляция "
            f"{broadcast_id} сохранена — следующий запуск продолжит её")
        return
    _clear_live_session()
    _send_log("info", "YouTube: удаление broadcast неудачного запуска...")
    try:
        _yt_call(youtube_api.delete_broadcast, broadcast_id)
        _send_log("info", f"YouTube: broadcast {broadcast_id} удалён")
        return
    except Exception as e:
        try:
            _yt_call(youtube_api.transition_broadcast,
                     broadcast_id, "complete")
        except Exception as e2:
            _send_log(
                "warning",
                f"YouTube: broadcast {broadcast_id} не прибран ({e}; {e2}) "
                "— удалите вручную в YouTube Studio")
        else:
            _send_log(
                "info",
                f"YouTube: broadcast {broadcast_id} завершён (complete) "
                "вместо удаления")

def _build_youtube_description(description, tags):

    description = (description or "").rstrip()
    hashtags = []
    for tag in (tags or []):
        tag = (tag or "").strip().lstrip("#").strip()
        if not tag:
            continue
        tag = "".join(tag.split())
        hashtags.append("#" + tag)
    tags_line = " ".join(hashtags)
    if description and tags_line:
        return f"{description}\n\n{tags_line}"
    return description or tags_line

def _resolve_twitch_game(game_query):

    q = (game_query or "").strip()
    if not q:
        return None
    try:
        cats = _tw_call(twitch_api.search_categories, q)
    except Exception as e:
        config.log.warning(
            f"Twitch: поиск категорий не удался ({e}) — пробую точное "
            "совпадение через /games")
        cats = []
    for c in cats:
        if (c.get("name") or "").strip().lower() == q.lower():
            return c
    games = _tw_call(twitch_api.search_games, q)
    if games:
        return games[0]
    return None

def _verify_twitch_channel_settings(title, game_name, tags):

    tw = _state["twitch"]
    ch = _tw_call(twitch_api.get_channel_info, tw["user_id"])
    expected_title = (title or "").strip()[:140]
    actual_title = (ch.get("title") or "").strip()
    sent_tags = {t.lower() for t in twitch_api.clean_tags(tags)}
    got_tags = {t.strip().lower() for t in (ch.get("tags") or [])
                if isinstance(t, str) and t.strip()}
    problems = []
    if actual_title != expected_title:
        problems.append(f"название: отправлено «{expected_title}», "
                        f"на канале «{actual_title}»")
    if game_name is not None:
        actual_game = (ch.get("game_name") or "").strip().lower()
        if actual_game != game_name.strip().lower():
            problems.append(f"игра: отправлена «{game_name}», на канале "
                            f"«{ch.get('game_name') or '—'}»")
    if sent_tags != got_tags:
        problems.append(f"теги: отправлены {sorted(sent_tags) or '—'}, "
                        f"на канале {sorted(got_tags) or '—'}")
    if problems:
        raise RuntimeError(
            "Twitch: настройки не применены (" + "; ".join(problems) +
            "). Стрим НЕ запущен — поправьте значения и запустите снова.")

def _resolve_youtube_category(game_query, explicit):

    if explicit and explicit.get("id"):
        return {"id": str(explicit["id"]),
                "title": explicit.get("title") or "Gaming"}
    cats = []
    try:
        cats = _yt_call(youtube_api.get_video_categories)
    except Exception as e:
        _send_log("warning",
                  f"YouTube: список категорий не получен ({e}) — "
                  "использую «Gaming»")
    q = (game_query or "").strip().lower()
    if q and cats:
        for c in cats:
            if (c.get("title") or "").strip().lower() == q:
                return c
        for c in cats:
            if q in (c.get("title") or "").lower():
                return c
    return {"id": config.YOUTUBE_CATEGORY_ID, "title": "Gaming"}

_REUSABLE_BROADCAST_STATUSES = frozenset((
    "created", "ready", "testing", "testStarting", "live", "liveStarting"))

def _load_live_session():
    try:
        with open(config.get_live_session_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("broadcast_id"):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        config.log.debug(f"live_session load: {e}")
    return None

def _save_live_session(data):
    try:
        path = config.get_live_session_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        config.log.error(f"live_session save: {e}")

def _clear_live_session():
    try:
        os.remove(config.get_live_session_path())
    except FileNotFoundError:
        pass
    except Exception as e:
        config.log.debug(f"live_session remove: {e}")

def _acquire_youtube_broadcast(title, description, tags):
    session = _load_live_session()
    if session and session.get("broadcast_id"):
        bc = None
        try:
            bc = _yt_call(youtube_api.get_broadcast, session["broadcast_id"])
        except Exception as e:
            config.log.warning(f"recovery: get_broadcast: {e}")
        if bc and bc.get("life_cycle_status") in _REUSABLE_BROADCAST_STATUSES:
            stream_info = None
            if session.get("stream_id"):
                try:
                    stream_info = _yt_call(
                        youtube_api.get_stream, session["stream_id"])
                except Exception as e:
                    config.log.warning(f"recovery: get_stream: {e}")
            if not stream_info:
                stream_info = _yt_call(youtube_api.create_stream, title=title)
                try:
                    _yt_call(youtube_api.bind_broadcast_to_stream,
                             bc["id"], stream_info["id"])
                except Exception as e:
                    config.log.warning(f"recovery: rebind: {e}")
            _send_log(
                "warning",
                f"YouTube: продолжаю ПРЕЖНЮЮ трансляцию {bc['id']} "
                f"(статус {bc.get('life_cycle_status')}) — та же ссылка и "
                "чат, новая трансляция не создаётся")
            _save_live_session({
                "broadcast_id": bc["id"],
                "stream_id": stream_info["id"],
                "title": title,
                "started_at": session.get("started_at") or int(time.time()),
            })
            return {"broadcast_id": bc["id"], "stream": stream_info,
                    "reused": True,
                    "was_live": bc.get("life_cycle_status") == "live"}
        if bc:
            config.log.info(
                f"recovery: прежняя трансляция {bc['id']} в статусе "
                f"{bc.get('life_cycle_status')} — не переиспользуется, "
                "создаю новую")
    _clear_live_session()
    _send_log("info", "YouTube: создание broadcast через API "
                      "(доступ: открытый)...")
    broadcast_id = _yt_call(
        youtube_api.create_broadcast, title=title,
        description=description, tags=tags)
    stream_info = _yt_call(youtube_api.create_stream, title=title)
    _yt_call(youtube_api.bind_broadcast_to_stream,
             broadcast_id, stream_info["id"])
    _save_live_session({
        "broadcast_id": broadcast_id,
        "stream_id": stream_info["id"],
        "title": title,
        "started_at": int(time.time()),
    })
    return {"broadcast_id": broadcast_id, "stream": stream_info,
            "reused": False, "was_live": False}

def _start_stream_worker():
    engine_went_live = False
    try:
        _send_log("info", "Запуск стрима (Twitch + YouTube)...")

        tw = _state["twitch"]
        yt = _state["youtube"]

        if not _twitch_verified():
            _send_log("error",
                      "Twitch не авторизован или не проверен (Шаг 1). "
                      "Подключите Twitch и дождитесь проверки прав.")
            return
        if not _youtube_verified():
            _send_log("error",
                      "YouTube не авторизован или не проверен (Шаг 1). "
                      "Подключите YouTube и дождитесь проверки прав.")
            return

        engine = _state.get("engine")
        if not engine:
            raise RuntimeError("Движок obs-multi-rtmp не инициализирован")
        omr_bridge.OmrEngine.check_ready()

        _ensure_fresh_youtube_token()

        tags = []
        for part in (_state["tags"] or "").split(","):
            part = re.sub(r"\s+", "_", part.strip())
            if part:
                tags.append(part)
        title = (_state["title"] or "").strip() or "Стрим"
        game_query = (_state["game_query"] or "").strip()

        _reset_stream_bitrate_counters()
        _state["last_viewers"] = {"twitch": None, "youtube": None}

        _send_log("info", "Twitch: получаем stream key через API...")
        twitch_stream_key = _tw_call(twitch_api.get_stream_key, tw["user_id"])

        game_id = None
        game_name = None
        if game_query:
            game = _resolve_twitch_game(game_query)
            if game:
                game_id = game["id"]
                game_name = game["name"]
                _send_log("info",
                          f"Twitch: игра — «{game_name}» (id={game_id})")
            else:
                explicit_yt = _state.get("yt_category") or {}
                if explicit_yt.get("title", "").strip().lower() == \
                        game_query.lower():
                    _send_log("warning",
                              f"Twitch: категории «{game_query}» нет — это "
                              "категория YouTube, игра на Twitch не меняется")
                else:
                    raise RuntimeError(
                        f"Twitch: игра «{game_query}» не найдена — настройка "
                        "не применена, стрим НЕ запущен. Проверьте название "
                        "или выберите игру из списка (начните вводить "
                        "название в поле «Игра» в настройках).")

        _send_log("info", "Twitch: применяю название, игру и теги...")
        _tw_call(twitch_api.update_channel_info, tw["user_id"],
                 title=title, game_id=game_id, tags=tags)
        _verify_twitch_channel_settings(title, game_name, tags)
        _send_log("success",
                  "Twitch: настройки применены (название/игра/теги "
                  "подтверждены каналом)")

        yt_description = _build_youtube_description(
            _state["description"], tags)
        if tags:
            _send_log("info",
                      f"YouTube: теги ({', '.join(tags)}) — в теги "
                      "трансляции и в описание хэштегами")
        if _state["twitch_bitrate"] > config.TWITCH_BITRATE_MAX_KBPS:
            _send_log(
                "warning",
                f"Битрейт Twitch {_state['twitch_bitrate']} Kbps выше "
                f"рекомендуемых {config.TWITCH_BITRATE_MAX_KBPS} Kbps — "
                "Twitch может отключить транскоды, эфир может быть "
                "нестабильным")

        acquired = _acquire_youtube_broadcast(title, yt_description, tags)
        broadcast_id = acquired["broadcast_id"]
        stream_info = acquired["stream"]
        _state["youtube_broadcast_id"] = broadcast_id
        _state["youtube_video_id"] = broadcast_id
        _state["youtube_broadcast_reused"] = acquired["reused"]
        youtube_rtmp_url = stream_info["rtmp_url"]
        youtube_stream_key = stream_info["stream_key"]

        yt_category = _state.get("yt_category")
        if game_query or yt_category:
            category = _resolve_youtube_category(game_query, yt_category)
            _send_log("info",
                      f"YouTube: применяю категорию «{category['title']}» "
                      f"(id={category['id']})...")
            _yt_call(youtube_api.set_video_category, broadcast_id,
                     title=title, description=yt_description,
                     tags=tags, category_id=category["id"])
            _send_log("success",
                      f"YouTube: категория — «{category['title']}»")

        if _state["thumbnail_path"]:
            scopes_have = set(yt.get("scopes", []))
            if (config.YOUTUBE_THUMBNAIL_SCOPE in scopes_have
                    or config.OBS_YOUTUBE_SCOPE in scopes_have):
                try:
                    _yt_call(youtube_api.upload_thumbnail,
                             broadcast_id, _state["thumbnail_path"])
                    _send_log("info", "YouTube: превью загружено")
                except Exception as e:
                    _send_log("warning", f"Превью не загружено: {e}")
            else:
                _send_log("warning",
                          "Превью пропущено: у токена нет прав на загрузку")

        platforms = {
            "twitch": {
                "server": config.DEFAULT_TWITCH_RTMP_URL,
                "key": twitch_stream_key,
                "bitrate": _state["twitch_bitrate"],
                "audio_track": _state["twitch_audio_track"],
                "name": "Twitch",
            },
            "youtube": {
                "server": youtube_rtmp_url,
                "key": youtube_stream_key,
                "bitrate": _state["youtube_bitrate"],
                "audio_track": _state["youtube_audio_track"],
                "name": "YouTube",
            },
        }

        _send_log("info", "obs-multi-rtmp: запись целей Twitch + YouTube...")
        try:
            engine.prepare(platforms)
            _send_log("info", "obs-multi-rtmp: запуск обоих потоков...")
            engine.go_live(
                wait_publish_timeout=config.OMR_PUBLISH_TIMEOUT,
                wait_start_timeout=config.OMR_START_TIMEOUT)
        except Exception as e:
            config.log.error(f"omr engine failed: {e}", exc_info=True)
            try:
                for err in engine.go_down():
                    config.log.warning(f"Откат эфира: {err}")
            except Exception as e2:
                config.log.error(f"Откат эфира не удался: {e2}")
            raise
        engine_went_live = True

        _send_log("success",
                  "obs-multi-rtmp: цели Twitch и YouTube запущены "
                  "(кодеры — его собственные)")

        transitioned = acquired["was_live"]
        if transitioned:
            _send_log("success",
                      "YouTube: трансляция уже в эфире — продолжаю её")
        else:
            _send_log("info", "YouTube: перевод broadcast в live...")
            for attempt in range(5):
                time.sleep(10 if attempt else 5)
                try:
                    _yt_call(youtube_api.transition_broadcast,
                             broadcast_id, "live")
                    transitioned = True
                    _send_log("success", "YouTube: broadcast в эфире")
                    break
                except Exception as e:
                    _send_log(
                        "info",
                        f"Transition ещё не готов ({e}), попытка "
                        f"{attempt + 1}/5...")
        if not transitioned:
            _send_log("warning",
                      "YouTube не перешёл в live автоматически. Проверьте "
                      "статус в YouTube Studio (возможно, нужен ингест).")

        if transitioned:
            for attempt in range(3):
                try:
                    _state["youtube_live_chat_id"] = _yt_call(
                        youtube_api.get_broadcast_live_chat_id, broadcast_id)
                    break
                except Exception:
                    if attempt < 2:
                        time.sleep(10)

        if _state["twitch"].get("username"):
            _state["twitch_chat_reader"] = twitch_chat.TwitchChatReader(
                channel_login=_state["twitch"]["username"],
                message_queue=_state["chat_queue"],
            )
            _state["twitch_chat_reader"].start()

        if _state["youtube_live_chat_id"]:
            _state["youtube_chat_reader"] = youtube_chat.YouTubeChatReader(
                access_token=yt["token"],
                live_chat_id=_state["youtube_live_chat_id"],
                message_queue=_state["chat_queue"],
                on_quota_warning=lambda r: (
                    _state["dock_server"].send_quota_warning(r)
                    if _state.get("dock_server") else None),
            )
            _state["youtube_chat_reader"].start()

        _state["viewer_counter"].update_credentials(
            twitch_access_token=tw["token"],
            twitch_user_login=tw["username"],
            youtube_access_token=yt["token"],
            youtube_video_id=_state["youtube_video_id"],
        )
        _state["viewer_counter"].poll_now()

        _state["streaming"] = True
        _state["stream_started_at"] = time.time()
        _send_log("success", "🚀 Стрим запущен на Twitch и YouTube")
        if _state["dock_server"]:
            _state["dock_server"].send_stream_status(
                True, _state["stream_started_at"])

        _start_yt_token_refresher()

    except Exception as e:
        config.log.error(f"Start stream failed: {e}", exc_info=True)
        _send_log("error", f"Ошибка запуска: {e}")
        if not engine_went_live:
            try:
                _cleanup_youtube_after_failed_start()
            except Exception as ec:
                config.log.warning(f"YouTube cleanup: {ec}")
        if _state.get("dock_server"):
            _state["dock_server"].send_stream_status(
                False, error=str(e))
    finally:
        _state["stream_starting"] = False

def _start_yt_token_refresher():

    stop_event = threading.Event()

    def worker():
        while not stop_event.wait(2700):
            try:
                _refresh_youtube_access_token()
                config.log.info("YouTube: access_token обновлён во время стрима")
            except Exception as e:
                config.log.warning(f"YouTube token refresh failed: {e}")

    thread = threading.Thread(target=worker, daemon=True,
                              name="yt-token-refresher")
    thread.start()
    _state["yt_token_refresher"] = (thread, stop_event)

def _stop_yt_token_refresher():
    refresher = _state.get("yt_token_refresher")
    if refresher:
        thread, stop_event = refresher
        stop_event.set()
        _state["yt_token_refresher"] = None

def _stop_stream_worker():
    if _state.get("stream_stopping"):
        return
    _state["stream_stopping"] = True
    try:
        _stop_yt_token_refresher()

        if _state["youtube_broadcast_id"] and _state["youtube"]["token"]:
            _send_log("info", "YouTube: завершение broadcast...")
            try:
                _yt_call(youtube_api.transition_broadcast,
                         _state["youtube_broadcast_id"], "complete")
                _clear_live_session()
            except Exception as e:
                _send_log(
                    "warning",
                    f"Transition to complete не удался: {e} — сессия "
                    "сохранена, следующий запуск продолжит эту трансляцию")

        engine = _state.get("engine")
        if engine:
            _send_log("info", "Остановка эфира (obs-multi-rtmp)...")
            try:
                for err in engine.go_down():
                    _send_log("warning", f"Остановка: {err}")
            except Exception as e:
                _send_log("warning", f"Остановка движка: {e}")

        if _state["twitch_chat_reader"]:
            _state["twitch_chat_reader"].stop()
            _state["twitch_chat_reader"] = None
        if _state["youtube_chat_reader"]:
            _state["youtube_chat_reader"].stop()
            _state["youtube_chat_reader"] = None

        _state["streaming"] = False
        _state["stream_started_at"] = None
        _state["youtube_broadcast_id"] = None
        _state["youtube_video_id"] = None
        _state["youtube_live_chat_id"] = None
        _state["youtube_broadcast_reused"] = False
        _state["last_viewers"] = {"twitch": None, "youtube": None}
        _reset_stream_bitrate_counters()

        _send_log("success", "Стрим остановлен")
        if _state["dock_server"]:
            _state["dock_server"].send_stream_status(False)

    except Exception as e:
        config.log.error(f"Stop stream failed: {e}", exc_info=True)
        _send_log("error", f"Ошибка остановки: {e}")
    finally:
        _state["stream_stopping"] = False

def _chat_dispatcher():
    while True:
        try:
            msg = _state["chat_queue"].get(timeout=1)
        except queue.Empty:
            continue
        if _state.get("dock_server"):
            _state["dock_server"].send_chat_message(
                platform=msg["platform"],
                author=msg["author"],
                text=msg["text"],
                color=msg.get("color"),
                is_owner=msg.get("is_owner", False),
                is_moderator=msg.get("is_moderator", False),
                timestamp=msg.get("timestamp"),
            )

def _on_viewers_update(twitch_viewers, youtube_viewers):
    _state["last_viewers"] = {
        "twitch": twitch_viewers,
        "youtube": youtube_viewers,
    }
    if _state.get("dock_server"):
        _state["dock_server"].send_viewers(twitch_viewers, youtube_viewers)

def _apply_settings_from_dock(settings):

    changed = False
    field_map = {
        "title": "title",
        "description": "description",
        "game": "game_query",
        "thumbnail": "thumbnail_path",
    }
    for key, state_key in field_map.items():
        if key in settings:
            _state[state_key] = str(settings[key])
            changed = True
    if "tags" in settings:
        normalized = _normalize_tags(str(settings["tags"]))
        if normalized != _state["tags"]:
            changed = True
        _state["tags"] = normalized

    if "yt_category" in settings:
        cat = settings["yt_category"]
        if isinstance(cat, dict) and cat.get("id"):
            _state["yt_category"] = {
                "id": str(cat["id"]),
                "title": str(cat.get("title") or ""),
            }
        else:
            _state["yt_category"] = None
        changed = True

    for key, state_key, default in (
            ("twitch_bitrate", "twitch_bitrate", config.DEFAULT_TWITCH_BITRATE),
            ("youtube_bitrate", "youtube_bitrate", config.DEFAULT_YOUTUBE_BITRATE)):
        if key in settings:
            try:
                value = int(settings[key])
            except (TypeError, ValueError):
                value = default
            _state[state_key] = max(500, min(50000, value))
            changed = True

    for key, state_key, default in (
            ("twitch_audio_track", "twitch_audio_track",
             config.DEFAULT_AUDIO_TRACK),
            ("youtube_audio_track", "youtube_audio_track",
             config.DEFAULT_AUDIO_TRACK)):
        if key in settings:
            try:
                value = int(settings[key])
            except (TypeError, ValueError):
                value = default
            _state[state_key] = max(1, min(config.AUDIO_TRACKS_MAX, value))
            changed = True

    if ("twitch_bitrate" in settings
            and _state["twitch_bitrate"] > config.TWITCH_BITRATE_MAX_KBPS):
        _send_log(
            "warning",
            f"Битрейт Twitch {_state['twitch_bitrate']} Kbps: Twitch "
            f"рекомендует не более {config.TWITCH_BITRATE_MAX_KBPS} Kbps "
            "— возможно отключение транскодов и нестабильность эфира")
    if changed:
        _save_stream_settings()
        _sync_selected_preset()
        if _state.get("dock_server"):
            _state["dock_server"].send_settings_saved()

def _normalize_tags(text):

    tags = []
    seen = set()
    for part in str(text or "").replace(";", ",").split(","):
        tag = re.sub(r"\s+", "_", part.strip().lstrip("#").strip())
        tag = tag.strip("_")
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    return ", ".join(tags[:30])

def _sync_selected_preset():

    preset_id = _state.get("last_preset_id")
    if not preset_id:
        return
    presets = _load_presets()
    target = next((p for p in presets if p.get("id") == preset_id), None)
    if not target:
        return
    data = _preset_from_state(target["name"], preset_id)
    presets = [data if p.get("id") == preset_id else p for p in presets]
    try:
        _save_presets_file(presets)
    except Exception as e:
        config.log.debug(f"preset autosync: {e}")

def _get_stream_settings_path():
    return config.get_settings_path()

def _save_stream_settings():
    data = {
        "title": _state["title"],
        "description": _state["description"],
        "tags": _state["tags"],
        "game_query": _state["game_query"],
        "yt_category": _state["yt_category"],
        "thumbnail_path": _state["thumbnail_path"],
        "twitch_bitrate": _state["twitch_bitrate"],
        "youtube_bitrate": _state["youtube_bitrate"],
        "twitch_audio_track": _state["twitch_audio_track"],
        "youtube_audio_track": _state["youtube_audio_track"],
        "last_preset_id": _state.get("last_preset_id"),
    }
    try:
        path = _get_stream_settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        config.log.info(f"Stream settings saved to {path}")
    except Exception as e:
        config.log.error(f"Failed to save stream settings: {e}")

def _load_stream_settings():
    path = _get_stream_settings_path()
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _state["title"] = data.get("title", _state["title"])
        _state["description"] = data.get("description", _state["description"])
        _state["tags"] = _normalize_tags(
            data.get("tags", _state["tags"]))
        _state["game_query"] = data.get("game_query", _state["game_query"])
        _state["last_preset_id"] = data.get("last_preset_id") or None
        cat = data.get("yt_category")
        if isinstance(cat, dict) and cat.get("id"):
            _state["yt_category"] = {
                "id": str(cat["id"]),
                "title": str(cat.get("title") or ""),
            }
        _state["thumbnail_path"] = data.get("thumbnail_path",
                                            _state["thumbnail_path"])
        _state["twitch_bitrate"] = data.get("twitch_bitrate",
                                            _state["twitch_bitrate"])
        _state["youtube_bitrate"] = data.get("youtube_bitrate",
                                             _state["youtube_bitrate"])
        for key in ("twitch_audio_track", "youtube_audio_track"):
            try:
                _state[key] = max(1, min(
                    config.AUDIO_TRACKS_MAX,
                    int(data.get(key, config.DEFAULT_AUDIO_TRACK))))
            except (TypeError, ValueError):
                _state[key] = config.DEFAULT_AUDIO_TRACK
        config.log.info(f"Stream settings loaded from {path}")
    except Exception as e:
        config.log.error(f"Failed to load stream settings: {e}")

def _thumbnail_preview_url(path):
    if not path:
        return None
    try:
        previews = os.path.realpath(config.get_previews_dir())
        real = os.path.realpath(path)
        if real != previews and real.startswith(previews + os.sep):
            return "/preview/" + os.path.basename(real)
    except Exception:
        pass
    return None

def _broadcast_settings():
    if not _state.get("dock_server"):
        return
    _state["dock_server"].send_settings({
        "title": _state["title"],
        "description": _state["description"],
        "tags": _state["tags"],
        "game": _state["game_query"],
        "yt_category": _state["yt_category"],
        "thumbnail": _state["thumbnail_path"],
        "thumbnail_url": _thumbnail_preview_url(_state["thumbnail_path"]),
        "twitch_bitrate": _state["twitch_bitrate"],
        "youtube_bitrate": _state["youtube_bitrate"],
        "twitch_audio_track": _state["twitch_audio_track"],
        "youtube_audio_track": _state["youtube_audio_track"],
    })

def _broadcast_auth_status():
    if not _state.get("dock_server"):
        return

    tw = _state["twitch"]
    yt = _state["youtube"]

    _state["dock_server"].send_auth_status(
        twitch={
            "connected": tw["connected"],
            "verified": _twitch_verified(),
            "user": tw["username"],
            "user_id": tw["user_id"],
            "scopes": tw["scopes"],
            "missing_scopes": tw["missing_scopes"],
            "api_ok": tw["api_ok"],
            "api_error": tw["api_error"],
            "channel_title": tw["channel_title"],
            "in_progress": tw["in_progress"],
        },
        youtube={
            "connected": yt["connected"],
            "verified": _youtube_verified(),
            "email": yt["email"],
            "channel_title": yt["channel_title"],
            "channel_id": yt["channel_id"],
            "scopes": yt["scopes"],
            "missing_scopes": yt["missing_scopes"],
            "api_ok": yt["api_ok"],
            "api_error": yt["api_error"],
            "source": yt["source"],
            "obs_client_found": yt["obs_client_found"],
            "obs_connected": yt["obs_connected"],
            "client_configured": yt["client_configured"],
            "client_id": yt["client_id"],
            "has_thumbnail_scope": (
                config.YOUTUBE_THUMBNAIL_SCOPE in set(yt.get("scopes", []))
                or config.OBS_YOUTUBE_SCOPE in set(yt.get("scopes", []))),
            "in_progress": yt["in_progress"],
        },
    )

def _broadcast_engine_status():

    if not _state.get("dock_server"):
        return
    engine = _state.get("engine")
    if not engine:
        _state["dock_server"].send_engine_status(
            {"omr": {"state": "not_found", "loaded": False,
                     "version": None,
                     "url": omr_bridge.OMR_URL},
             "streaming_active": False,
             "backup_pending": False, "session_active": False})
        return
    try:
        _state["dock_server"].send_engine_status(engine.status())
    except Exception as e:
        config.log.debug(f"engine status broadcast failed: {e}")

def _send_log(level, message):
    config.log.info(f"[{level.upper()}] {message}")
    if _state.get("dock_server"):
        _state["dock_server"].send_log(level, message)
