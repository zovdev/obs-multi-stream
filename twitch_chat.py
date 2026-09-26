
import threading
import random
import queue
import time

import config

try:
    import websocket
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    config.log.error("websocket-client not installed, Twitch chat will not work")

class TwitchChatReader:
    def __init__(self, channel_login, message_queue):
        self.channel_login = channel_login.lower()
        self.message_queue = message_queue
        self.ws = None
        self._stop_event = threading.Event()
        self._thread = None
        self._reconnect_delay = 5

    def start(self):
        if not WEBSOCKET_AVAILABLE:
            raise RuntimeError("websocket-client не установлен")
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        config.log.info(f"Twitch chat reader started for #{self.channel_login}")

    def stop(self):
        self._stop_event.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)
        config.log.info("Twitch chat reader stopped")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._connect_and_listen()
            except Exception as e:
                config.log.error(f"Twitch chat error: {e}")
            if self._stop_event.is_set():
                break
            config.log.info(f"Reconnecting Twitch chat in {self._reconnect_delay} sec...")
            self._stop_event.wait(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    def _connect_and_listen(self):
        nick = f"justinfan{random.randint(10000, 99999)}"

        def on_open(ws):
            config.log.info(f"Twitch IRC connected as {nick}")
            ws.send(f"NICK {nick}\r\n")
            ws.send(
                "CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership\r\n"
            )
            ws.send(f"JOIN #{self.channel_login}\r\n")
            self._reconnect_delay = 5

        def on_message(ws, message):
            self._handle_raw_message(message)

        def on_error(ws, error):
            config.log.error(f"Twitch IRC error: {error}")

        def on_close(ws, code, msg):
            config.log.info(f"Twitch IRC closed: {code} {msg}")

        self.ws = websocket.WebSocketApp(
            config.TWITCH_IRC_WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        self.ws.run_forever(ping_interval=30)

    def _handle_raw_message(self, raw):
        for line in raw.split("\r\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("PING"):
                if self.ws:
                    self.ws.send(line.replace("PING", "PONG") + "\r\n")
                continue
            tags = {}
            if line.startswith("@"):
                tag_part, _, rest = line.partition(" ")
                for kv in tag_part[1:].split(";"):
                    if "=" in kv:
                        k, _, v = kv.partition("=")
                        tags[k] = v
                line = rest
            if "PRIVMSG" not in line:
                continue
            try:
                prefix, _, trailing = line.partition(" :")
                text = trailing
                prefix = prefix.lstrip(":")
                nick_part = prefix.split("!")[0]
                display_name = tags.get("display-name") or nick_part
                color = tags.get("color") or "#9146FF"
                msg = {
                    "platform": "twitch",
                    "author": display_name,
                    "text": text,
                    "color": color,
                    "timestamp": time.time(),
                    "is_moderator": "moderator" in tags.get("badges", ""),
                }
                self.message_queue.put(msg)
            except Exception as e:
                config.log.debug(f"Failed to parse Twitch IRC line: {line!r} ({e})")
