
import os
import sys
import json
import socket
import threading
import queue
import time
import http.server
import asyncio
import urllib.parse

import config

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    config.log.warning(
        "websockets not installed — dock HTTP will work, "
        "but WebSocket (chat/controls) disabled. "
        "Install with: pip install websockets"
    )

_DOCK_ASSETS_DIR = None
_SERVER_STARTED_AT = None

def set_assets_dir(path):
    global _DOCK_ASSETS_DIR
    _DOCK_ASSETS_DIR = path
    config.log.info(f"Dock assets dir set to: {_DOCK_ASSETS_DIR}")

def _resolve_asset(filename):

    candidates = []
    if _DOCK_ASSETS_DIR:
        candidates.append(os.path.join(_DOCK_ASSETS_DIR, filename))
        candidates.append(os.path.join(_DOCK_ASSETS_DIR, "dock_assets", filename))
    try:
        module_dir = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        module_dir = None
    if module_dir:
        candidates.append(os.path.join(module_dir, filename))
        candidates.append(os.path.join(module_dir, "dock_assets", filename))

    for path in candidates:
        try:
            if os.path.isfile(path):
                return path
        except Exception:
            continue
    return None

class DockHTTPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urllib.parse.unquote(self.path.split("?", 1)[0])

        if path.startswith("/preview/"):
            self._send_preview(path[len("/preview/"):])
            return

        if path in ("/ping", "/health"):
            self._send_text("pong", "text/plain; charset=utf-8")
            return

        if path == "/debug":
            self._send_debug()
            return

        path_map = {
            "/": "dock.html",
            "/index.html": "dock.html",
            "/dock.html": "dock.html",
            "/dock.css": "dock.css",
            "/dock.js": "dock.js",
        }

        filename = path_map.get(path)
        if not filename:
            config.log.warning(f"404: path={path}")
            self.send_error(404, f"Not Found: {path}")
            return

        filepath = _resolve_asset(filename)
        if not filepath:
            config.log.error(
                f"Asset not found: {filename} (assets_dir={_DOCK_ASSETS_DIR})")
            self.send_error(404, f"File not found: {filename}")
            return

        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }
        ext = os.path.splitext(filename)[1].lower()
        ctype = content_types.get(ext, "application/octet-stream")

        try:
            with open(filepath, "rb") as f:
                content = f.read()
        except Exception as e:
            config.log.error(f"Cannot read {filepath}: {e}")
            self.send_error(500, f"Cannot read file: {e}")
            return

        if filename == "dock.js" and _DOCK_SERVER_INSTANCE is not None:
            try:
                content = content.replace(
                    b"__WS_PORT__",
                    str(_DOCK_SERVER_INSTANCE.ws_port).encode("ascii"))
            except Exception:
                pass

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(content)
        except ConnectionError:
            pass

    def _send_preview(self, name):
        name = (name or "").replace("\\", "/").split("/")[-1].strip()
        if (not name or name in (".", "..")
                or not all(ch.isalnum() or ch in "-_. " for ch in name)):
            self.send_error(404, "Bad preview name")
            return
        previews_dir = config.get_previews_dir()
        filepath = os.path.join(previews_dir, name)
        try:
            real = os.path.realpath(filepath)
            base = os.path.realpath(previews_dir)
            if real != base and not real.startswith(base + os.sep):
                self.send_error(404, "Not Found")
                return
            if not os.path.isfile(real):
                self.send_error(404, "Not Found")
                return
        except Exception:
            self.send_error(404, "Not Found")
            return
        ctypes_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
        }
        ctype = ctypes_map.get(os.path.splitext(name)[1].lower(),
                                "application/octet-stream")
        try:
            with open(filepath, "rb") as f:
                content = f.read()
        except Exception:
            self.send_error(404, "Not Found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(content)
        except ConnectionError:
            pass

    def _send_text(self, text, ctype):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except ConnectionError:
            pass

    def _send_debug(self):
        module_dir = os.path.dirname(os.path.abspath(__file__))
        info = {
            "ok": True,
            "uptime_seconds": (round(time.time() - _SERVER_STARTED_AT, 1)
                               if _SERVER_STARTED_AT else None),
            "assets_dir_configured": _DOCK_ASSETS_DIR,
            "module_dir": module_dir,
            "module_dir_has_dock_html":
                os.path.isfile(os.path.join(module_dir, "dock.html")),
            "websockets_available": WEBSOCKETS_AVAILABLE,
            "config_dir": config.get_obs_config_dir(),
            "log_file": config.get_log_file_path(),
            "resolved_dock_html": _resolve_asset("dock.html"),
        }
        body = json.dumps(info, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except ConnectionError:
            pass

class DockHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            config.log.debug(f"HTTP client {client_address} dropped: {exc!r}")
            return
        super().handle_error(request, client_address)

_DOCK_SERVER_INSTANCE = None

class DockServer:
    def __init__(self, http_port=None, ws_port=None, assets_dir=None):
        self.http_port = http_port or config.DEFAULT_DOCK_HTTP_PORT
        self.ws_port = ws_port or config.DEFAULT_DOCK_WS_PORT

        self._http_thread = None
        self._ws_thread = None
        self._dispatcher_thread = None

        self._stop_event = threading.Event()
        self._http_ready = threading.Event()
        self._loop_ready = threading.Event()

        self._clients = set()
        self._clients_lock = threading.Lock()

        self._loop = None
        self._message_queue = queue.Queue()
        self._on_command = None
        self._httpd = None

        if assets_dir:
            set_assets_dir(assets_dir)

    def set_command_handler(self, callback):
        self._on_command = callback

    def start(self):

        global _SERVER_STARTED_AT, _DOCK_SERVER_INSTANCE

        if not _DOCK_ASSETS_DIR:
            set_assets_dir(os.path.dirname(os.path.abspath(__file__)))

        if self._http_thread and self._http_thread.is_alive():
            config.log.info("Dock server already running")
            return True

        _DOCK_SERVER_INSTANCE = self
        self._stop_event.clear()
        _SERVER_STARTED_AT = time.time()

        self._http_thread = threading.Thread(
            target=self._run_http, daemon=True, name="dock-http")
        self._http_thread.start()

        if WEBSOCKETS_AVAILABLE:
            self._ws_thread = threading.Thread(
                target=self._run_ws, daemon=True, name="dock-ws")
            self._ws_thread.start()
            self._dispatcher_thread = threading.Thread(
                target=self._dispatcher, daemon=True, name="dock-dispatcher")
            self._dispatcher_thread.start()
        else:
            config.log.warning(
                f"WS server DISABLED (websockets not installed). "
                f"HTTP works on http://127.0.0.1:{self.http_port}/, "
                f"but the dock needs WS. Fix: pip install websockets")

        ready = self._wait_for_http_ready(timeout=5.0)
        if ready:
            config.log.info(
                f"✅ Dock server is UP. Open http://127.0.0.1:{self.http_port}/ "
                f"in OBS Custom Browser Dock. "
                f"WS: {'ws://127.0.0.1:' + str(self.ws_port) if WEBSOCKETS_AVAILABLE else 'DISABLED'}")
        else:
            config.log.error(
                f"❌ Dock HTTP server did NOT become ready on port "
                f"{self.http_port} within 5s. Порт занят?")
        return ready

    def stop(self):

        config.log.info("Stopping dock server...")
        self._stop_event.set()

        current = threading.current_thread()

        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception as e:
                config.log.debug(f"httpd.shutdown() error: {e}")

        if self._http_thread and self._http_thread is not current:
            self._http_thread.join(timeout=3.0)
            if self._http_thread.is_alive():
                config.log.warning("HTTP thread still alive after 3s join")
            else:
                config.log.info("HTTP thread joined — port released")

        if self._ws_thread and self._ws_thread is not current:
            self._ws_thread.join(timeout=3.0)
            if self._ws_thread.is_alive():
                config.log.warning("WS thread still alive after 3s join")

        if self._dispatcher_thread and self._dispatcher_thread is not current:
            self._dispatcher_thread.join(timeout=2.0)

        config.log.info("Dock server fully stopped")

    def _run_http(self):
        try:
            self._httpd = DockHTTPServer(
                ("127.0.0.1", self.http_port), DockHTTPHandler)
            config.log.info(
                f"Dock HTTP listening on http://127.0.0.1:{self.http_port}/")
            self._http_ready.set()
            self._httpd.serve_forever(poll_interval=0.5)
            config.log.info("Dock HTTP serve_forever() exited cleanly")
        except OSError as e:
            config.log.error(
                f"❌ Dock HTTP bind FAILED on 127.0.0.1:{self.http_port}: {e}. "
                f"Порт занят? netstat -ano | findstr :{self.http_port} (Windows)")
        except Exception as e:
            config.log.error(f"Dock HTTP server error: {e}", exc_info=True)
        finally:
            if self._httpd is not None:
                try:
                    self._httpd.server_close()
                except Exception:
                    pass
            self._http_ready.clear()
            self._httpd = None

    def _wait_for_http_ready(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._http_thread is None or not self._http_thread.is_alive():
                return False
            try:
                with socket.create_connection(
                        ("127.0.0.1", self.http_port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.1)
        return False

    def _run_ws(self):
        try:
            asyncio.run(self._ws_main())
        except Exception as e:
            config.log.error(f"Dock WS server error: {e}", exc_info=True)

    async def _ws_main(self):
        self._loop = asyncio.get_running_loop()
        self._loop_ready.set()
        try:
            async with websockets.serve(
                    self._ws_handler, "127.0.0.1", self.ws_port,
                    max_size=16 * 1024 * 1024):
                config.log.info(
                    f"Dock WS listening on ws://127.0.0.1:{self.ws_port}/")
                while not self._stop_event.is_set():
                    await asyncio.sleep(0.5)
        except OSError as e:
            config.log.error(
                f"❌ Dock WS bind FAILED on 127.0.0.1:{self.ws_port}: {e}. "
                f"Порт занят?")
        except Exception as e:
            config.log.error(f"Dock WS serve error: {e}", exc_info=True)
        finally:
            self._loop_ready.clear()
            self._loop = None

    _QUICK_COMMANDS = frozenset((
        "update_settings", "get_settings", "get_auth_status", "get_state",
        "get_oauth_session", "open_url",
        "preset_list", "preset_save", "preset_rename", "preset_delete",
        "preset_apply", "preset_select",
    ))

    async def _ws_handler(self, websocket):
        with self._clients_lock:
            self._clients.add(websocket)
        try:
            remote = getattr(websocket, "remote_address", "?")
        except Exception:
            remote = "?"
        config.log.info(f"Dock client connected: {remote}")

        try:
            await websocket.send(json.dumps({
                "type": "welcome",
                "message": "Connected to OBS Multi-Stream v3 dock",
            }))
        except Exception:
            pass

        try:
            async for raw in websocket:
                try:
                    cmd = json.loads(raw)
                except json.JSONDecodeError:
                    config.log.warning(f"Invalid JSON from dock: {raw!r}")
                    continue
                if not self._on_command:
                    continue
                if cmd.get("type") in self._QUICK_COMMANDS:
                    try:
                        self._on_command(cmd)
                    except Exception as e:
                        config.log.error(
                            f"Dock command failed: {e}", exc_info=True)
                else:
                    threading.Thread(
                        target=self._on_command,
                        args=(cmd,),
                        daemon=True,
                    ).start()
        except Exception as e:
            config.log.debug(f"Dock client disconnected: {e}")
        finally:
            with self._clients_lock:
                self._clients.discard(websocket)

    def _dispatcher(self):
        while not self._stop_event.is_set():
            try:
                msg = self._message_queue.get(timeout=1)
            except queue.Empty:
                continue
            if not self._loop or not self._loop_ready.is_set():
                continue
            try:
                asyncio.run_coroutine_threadsafe(
                    self._broadcast(msg), self._loop)
            except RuntimeError:
                break
            except Exception as e:
                config.log.debug(f"Broadcast error: {e}")
        config.log.info("Dispatcher thread exited")

    async def _broadcast(self, message):
        if not self._clients:
            return
        payload = json.dumps(message, ensure_ascii=False)
        dead = []
        with self._clients_lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send(payload)
            except Exception:
                dead.append(ws)
        if dead:
            with self._clients_lock:
                for ws in dead:
                    self._clients.discard(ws)

    def client_count(self):

        with self._clients_lock:
            return len(self._clients)

    def send_chat_message(self, platform, author, text, color=None,
                          is_owner=False, is_moderator=False,
                          timestamp=None):
        self._message_queue.put({
            "type": "chat_message",
            "platform": platform,
            "author": author,
            "text": text,
            "color": color,
            "is_owner": is_owner,
            "is_moderator": is_moderator,
            "timestamp": timestamp or time.time(),
        })

    def send_game_candidates(self, query, candidates):

        self._message_queue.put({
            "type": "game_candidates",
            "query": query,
            "candidates": candidates or [],
        })

    def send_presets(self, presets, select=None):

        self._message_queue.put({
            "type": "presets",
            "presets": presets or [],
            "select": select,
        })

    def send_preview_uploaded(self, path, url=None):

        self._message_queue.put({
            "type": "preview_uploaded",
            "path": path,
            "url": url,
        })

    def send_viewers(self, twitch_viewers, youtube_viewers):
        self._message_queue.put({
            "type": "viewers",
            "twitch": twitch_viewers,
            "youtube": youtube_viewers,
        })

    def send_stream_status(self, streaming, started_at=None, error=None):
        self._message_queue.put({
            "type": "stream_status",
            "streaming": streaming,
            "started_at": started_at,
            "error": error,
        })

    def send_output_stats(self, stats):

        self._message_queue.put({
            "type": "output_stats",
            "stats": stats or {},
        })

    def send_engine_status(self, status):

        self._message_queue.put({
            "type": "engine_status",
            "engine": status or {},
        })

    def send_quota_warning(self, remaining):
        self._message_queue.put({
            "type": "quota_warning",
            "platform": "youtube",
            "remaining": remaining,
        })

    def send_log(self, level, message):
        self._message_queue.put({
            "type": "log",
            "level": level,
            "message": message,
            "timestamp": time.time(),
        })

    def send_settings(self, settings_dict):
        self._message_queue.put({
            "type": "settings",
            "settings": settings_dict,
        })

    def send_settings_saved(self):
        self._message_queue.put({"type": "settings_saved"})

    def send_auth_status(self, twitch, youtube):

        self._message_queue.put({
            "type": "auth_status",
            "twitch": twitch,
            "youtube": youtube,
        })

    def send_oauth_progress(self, platform, stage, message, user_code=None,
                            url=None):

        self._message_queue.put({
            "type": "oauth_progress",
            "platform": platform,
            "stage": stage,
            "message": message,
            "user_code": user_code,
            "url": url,
        })

    def send_oauth_session(self, session):

        self._message_queue.put({
            "type": "oauth_session",
            "session": session or {},
        })
