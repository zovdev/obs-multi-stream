
import threading
import time
import concurrent.futures

import config
import twitch_api
import youtube_api

class ViewerCounter:
    POLL_INTERVAL_SEC = 30

    def __init__(self, on_update):
        self.on_update = on_update
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread = None

        self.twitch_access_token = None
        self.twitch_user_login = None
        self.youtube_access_token = None
        self.youtube_video_id = None

    def update_credentials(self, twitch_access_token=None, twitch_user_login=None,
                           youtube_access_token=None, youtube_video_id=None):
        if twitch_access_token is not None:
            self.twitch_access_token = twitch_access_token
        if twitch_user_login is not None:
            self.twitch_user_login = twitch_user_login
        if youtube_access_token is not None:
            self.youtube_access_token = youtube_access_token
        if youtube_video_id is not None:
            self.youtube_video_id = youtube_video_id

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        config.log.info("Viewer counter started")

    def stop(self):
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        config.log.info("Viewer counter stopped")

    def poll_now(self):

        self._wake_event.set()

    def _run(self):
        while not self._stop_event.is_set():
            self._wake_event.clear()
            twitch_viewers = None
            youtube_viewers = None

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                futures = {}
                if self.twitch_access_token and self.twitch_user_login:
                    futures["twitch"] = ex.submit(
                        self._safe_twitch_viewers,
                        self.twitch_access_token,
                        self.twitch_user_login,
                    )
                if self.youtube_access_token and self.youtube_video_id:
                    futures["youtube"] = ex.submit(
                        self._safe_youtube_viewers,
                        self.youtube_access_token,
                        self.youtube_video_id,
                    )
                for key, fut in futures.items():
                    try:
                        result = fut.result(timeout=15)
                        if key == "twitch":
                            twitch_viewers = result
                        else:
                            youtube_viewers = result
                    except Exception as e:
                        config.log.error(f"Viewer poll {key} failed: {e}")

            try:
                self.on_update(twitch_viewers, youtube_viewers)
            except Exception as e:
                config.log.error(f"on_update callback failed: {e}")

            self._wake_event.wait(self.POLL_INTERVAL_SEC)

    def _safe_twitch_viewers(self, token, login):
        try:
            return twitch_api.get_stream_viewers(token, login)
        except Exception as e:
            config.log.debug(f"Twitch viewers error: {e}")
            return None

    def _safe_youtube_viewers(self, token, video_id):
        try:
            return youtube_api.get_video_viewers(token, video_id)
        except Exception as e:
            config.log.debug(f"YouTube viewers error: {e}")
            return None
