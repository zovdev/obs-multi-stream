
import threading
import time
import concurrent.futures

import config
import twitch_api
import youtube_api

class ViewerCounter:
    # Базовый каденс цикла опроса. Реальные интервалы платформ берутся
    # из настроек расширения: YouTube — каждый цикл по умолчанию
    # (config.YOUTUBE_VIEWERS_INTERVAL_SEC = 15 c, 1 вызов videos.list
    # = 1 юнит), Twitch — config.TWITCH_VIEWERS_INTERVAL_SEC (30 c).
    # Оба значения читаются каждый цикл — смена в настройках применяется
    # на лету, без перезапуска эфира. При давлении квоты YouTube интервал
    # автоматически растягивается (см. _youtube_every_cycles):
    # >70% — 30 c, >90% — 60 c.
    POLL_INTERVAL_SEC = 15

    # Границы интервалов (защита от кривых значений из настроек)
    INTERVAL_MIN_SEC = 5
    INTERVAL_MAX_SEC = 120

    # Сколько держать последнее значение при «падении» запроса (сеть/ошибка
    # API), чтобы разовый сбой не выглядел как «стрим умер»:
    # 75 c — 2.5 цикла Twitch / 5 циклов YouTube.
    GRACE_SEC = 75

    # Служебный маркер: запрос упал (исключение), а не «нет данных».
    _ERROR = object()

    def __init__(self, on_update):
        self.on_update = on_update
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread = None

        self.twitch_access_token = None
        self.twitch_user_login = None
        self.youtube_access_token = None
        self.youtube_video_id = None

        # platform -> (значение, monotonic-метка), для grace при ошибках
        self._last_good = {}
        # Последние показанные значения (платформы теперь опрашиваются
        # в разном каденсе — в on_update всегда уходят оба числа)
        self._last_values = {"twitch": None, "youtube": None}
        # Расписание следующего опроса (в номерах циклов)
        self._twitch_due = 0
        self._yt_due = 0
        # poll_now() форсирует опрос обеих платформ в ближайшем цикле
        self._force_poll = False

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

    def reset_cache(self):
        """Сброс последнего известного значения (вызывается при старте/остановке эфира)."""
        self._last_good.clear()
        self._last_values = {"twitch": None, "youtube": None}

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
        # Немедленный опрос обеих платформ в ближайшем цикле.
        self._force_poll = True
        self._wake_event.set()

    def _interval_sec(self, name, default):
        """Интервал из настроек расширения (по имени ключа конфига)."""
        try:
            value = float(getattr(config, name, default))
        except (TypeError, ValueError):
            value = float(default)
        return max(self.INTERVAL_MIN_SEC, min(value, self.INTERVAL_MAX_SEC))

    def _twitch_every_cycles(self):
        """Интервал Twitch в циклах (квоты нет — без растяжения)."""
        sec = self._interval_sec("TWITCH_VIEWERS_INTERVAL_SEC", 30)
        return max(1, int(round(sec / self.POLL_INTERVAL_SEC)))

    def _youtube_every_cycles(self):
        """Интервал YouTube в циклах (адаптив по квоте)."""
        base = self._interval_sec("YOUTUBE_VIEWERS_INTERVAL_SEC",
                                  self.POLL_INTERVAL_SEC)
        every = max(1, int(round(base / self.POLL_INTERVAL_SEC)))
        try:
            used = youtube_api.quota_tracker.used()
            pct = used / float(youtube_api.quota_tracker.DAILY_LIMIT)
        except Exception:
            return every
        if pct >= 0.9:
            return max(every, 4)   # ~60 c: квота на исходе
        if pct >= 0.7:
            return max(every, 2)   # ~30 c
        return every               # ~15 c

    def _run(self):
        cycle_idx = 0
        while not self._stop_event.is_set():
            # Фиксированный каденс: следующий опрос привязан к моменту старта
            # цикла, а не к «после завершения работы + интервал». Даже если
            # один из запросов зависнет (таймаут urllib до 30 c), цикл не
            # растянется до «работа + интервал» — следующий опрос начнётся
            # по расписанию.
            cycle_started = time.monotonic()
            if self._force_poll:
                self._force_poll = False
                self._twitch_due = cycle_idx
                self._yt_due = cycle_idx
            self._wake_event.clear()

            twitch_due = bool(self.twitch_access_token
                              and self.twitch_user_login
                              and cycle_idx >= self._twitch_due)
            youtube_due = bool(self.youtube_access_token
                               and self.youtube_video_id
                               and cycle_idx >= self._yt_due)

            raw = {}
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=2)
            try:
                futures = {}
                if twitch_due:
                    futures["twitch"] = ex.submit(
                        self._safe_twitch_viewers,
                        self.twitch_access_token,
                        self.twitch_user_login,
                    )
                if youtube_due:
                    futures["youtube"] = ex.submit(
                        self._safe_youtube_viewers,
                        self.youtube_access_token,
                        self.youtube_video_id,
                    )
                for key, fut in futures.items():
                    try:
                        raw[key] = fut.result(timeout=10)
                    except Exception as e:
                        config.log.error(f"Viewer poll {key} failed: {e}")
                        raw[key] = self._ERROR
            finally:
                # Не ждём зависшие потоки (в отличие от `with`-блока):
                # shutdown(wait=False) не блокирует цикл; поток завершится
                # сам по своему таймауту urllib и будет собран.
                ex.shutdown(wait=False)

            if "twitch" in raw:
                self._last_values["twitch"] = self._apply_grace(
                    "twitch", raw.get("twitch"))
                self._twitch_due = cycle_idx + self._twitch_every_cycles()
            if "youtube" in raw:
                self._last_values["youtube"] = self._apply_grace(
                    "youtube", raw.get("youtube"))
                self._yt_due = cycle_idx + self._youtube_every_cycles()

            try:
                self.on_update(self._last_values["twitch"],
                               self._last_values["youtube"])
            except Exception as e:
                config.log.error(f"on_update callback failed: {e}")

            next_deadline = cycle_started + self.POLL_INTERVAL_SEC
            now_mono = time.monotonic()
            if next_deadline <= now_mono:
                # Просроченный дедлайн (запросы висели дольше интервала):
                # догоняем сразу, но не чаще раза в 5 c.
                next_deadline = now_mono + 5.0
            self._wake_event.wait(max(0.0, next_deadline - time.monotonic()))
            cycle_idx += 1

    def _apply_grace(self, platform, raw):
        """None = платформа честно не в эфире; _ERROR = запрос упал.

        При ошибке короткое время (GRACE_SEC) держим последнее валидное
        значение — разовый сбой сети/API не должен сбрасывать счётчик и
        флаг «в эфире» в панели.
        """
        if raw is self._ERROR:
            entry = self._last_good.get(platform)
            if entry and (time.monotonic() - entry[1]) <= self.GRACE_SEC:
                age = int(time.monotonic() - entry[1])
                config.log.debug(
                    f"Viewer poll {platform} упал — держу последнее "
                    f"значение {entry[0]} ({age} c назад)")
                return entry[0]
            return None
        if raw is not None:
            self._last_good[platform] = (raw, time.monotonic())
        else:
            # Платформа ответила «нет данных» (офлайн) — кэш протух,
            # чтобы позже ошибка не воскресила старое число.
            self._last_good.pop(platform, None)
        return raw

    def _safe_twitch_viewers(self, token, login):
        try:
            # None = стрим офлайн; исключения (сеть/401/403) летят выше
            return twitch_api.get_stream_viewers(token, login)
        except Exception as e:
            config.log.debug(f"Twitch viewers error: {e}")
            return self._ERROR

    def _safe_youtube_viewers(self, token, video_id):
        try:
            return youtube_api.get_video_viewers(token, video_id)
        except Exception as e:
            config.log.debug(f"YouTube viewers error: {e}")
            return self._ERROR
