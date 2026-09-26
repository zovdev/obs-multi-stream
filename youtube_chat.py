
import threading
import time
import queue
import calendar

import config
import youtube_api

class YouTubeChatReader:
    def __init__(self, access_token, live_chat_id, message_queue,
                 on_quota_warning=None):
        self.access_token = access_token
        self.live_chat_id = live_chat_id
        self.message_queue = message_queue
        self.on_quota_warning = on_quota_warning
        self._stop_event = threading.Event()
        self._thread = None
        self._next_page_token = None
        self._polling_interval = 5
        self._quota_used_today = 0
        self._quota_reset_at = self._next_day_utc()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        config.log.info("YouTube chat reader started")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        config.log.info("YouTube chat reader stopped")

    def _next_day_utc(self):
        now = time.time()
        tomorrow = time.gmtime(now + 86400)
        return calendar.timegm(
            (tomorrow.tm_year, tomorrow.tm_mon, tomorrow.tm_mday, 0, 0, 0, 0, 0, 0)
        )

    def _run(self):
        while not self._stop_event.is_set():
            try:
                result = youtube_api.list_live_chat_messages(
                    self.access_token,
                    self.live_chat_id,
                    page_token=self._next_page_token,
                )
                if time.time() >= self._quota_reset_at:
                    self._quota_used_today = 0
                    self._quota_reset_at = self._next_day_utc()

                self._quota_used_today += 5

                polling_ms = result.get("polling_interval_millis", 5000)
                self._polling_interval = max(polling_ms / 1000, 5)

                quota_pct = self._quota_used_today / 10000.0
                if quota_pct > 0.7:
                    self._polling_interval = max(self._polling_interval, 15)
                if quota_pct > 0.9:
                    self._polling_interval = max(self._polling_interval, 30)
                    if self.on_quota_warning:
                        remaining = 10000 - self._quota_used_today
                        self.on_quota_warning(remaining)

                self._next_page_token = result.get("next_page_token")

                for item in result["items"]:
                    self.message_queue.put({
                        "platform": "youtube",
                        "author": item["author"],
                        "text": item["text"],
                        "color": "#FF0000",
                        "timestamp": time.time(),
                        "is_owner": item.get("is_owner", False),
                        "is_moderator": item.get("is_moderator", False),
                    })

            except Exception as e:
                config.log.error(f"YouTube chat poll error: {e}")
                self._polling_interval = min(self._polling_interval * 2, 30)

            self._stop_event.wait(self._polling_interval)
