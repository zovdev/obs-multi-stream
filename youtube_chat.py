"""Чтение чата YouTube: анонимный innertube-клиент (основной путь)
с автоматическим фолбэком на официальный Data API.

Анонимный путь (youtube_anon) читает чат так же, как браузер без
логина: без OAuth и не тратя квоту Data API, с интервалом, который
называет сервер (обычно 1-10 c). Если внутренний формат YouTube
меняется или анонимные запросы блокируются, ридер переключается на
официальный API (квота тратится только в этом режиме) и раз в 10 минут
пробует вернуться на анонимный путь.

Сообщения обоих путей попадают в одну очередь в одинаковом формате.
"""

import threading
import time

import config
import youtube_anon
import youtube_api


class YouTubeChatReader:
    """Ридер чата YouTube с выбором источника.

    config.YOUTUBE_CHAT_SOURCE:
      "auto"      — анонимный innertube, при поломке официальный Data API
                    (с периодическими пробами вернуться обратно);
      "innertube" — только анонимный, фолбэка нет;
      "official"  — только официальный API (как в версиях до 3.23).

    Отправка сообщений в чат анонимно невозможна: для записи и
    модерации по-прежнему нужен OAuth (это не затрагивается здесь —
    ридер только читает).
    """

    # Подряд schema/http-ошибок опроса до переключения на официальный API
    INNERTUBE_FAILS = 3
    # Пересозданий сессии (лечит 403/429, проверено на живом чате) до фолбэка
    INNERTUBE_REBOOTS = 4
    # Как часто из official-режима пробуем вернуться на анонимный путь
    RECOVER_PROBE_SEC = 600.0
    # Сколько ждать появления чата у анонимного клиента до фолбэка
    NOT_LIVE_GRACE_SEC = 120.0

    def __init__(self, access_token, live_chat_id, message_queue,
                 video_id=None, on_quota_warning=None,
                 on_quota_exhausted=None, on_source_change=None):
        self.access_token = access_token
        self.live_chat_id = live_chat_id
        self.video_id = video_id
        self.message_queue = message_queue
        self.on_quota_warning = on_quota_warning
        self.on_quota_exhausted = on_quota_exhausted
        self.on_source_change = on_source_change
        self._stop_event = threading.Event()
        self._thread = None

        # Состояние официального пути (liveChatMessages.list)
        self._next_page_token = None
        self._polling_interval = 5
        # Оценка квоты — общая на весь плагин (чат + опрос зрителей),
        # считается в youtube_api.quota_tracker.
        self._quota_exhausted = False
        # Первая страница официального API после фолбэка содержит уже
        # показанные анонимным путём сообщения — пропускаем её.
        self._skip_first_official_page = False

        # Состояние анонимного пути
        self._anon = None            # youtube_anon.AnonymousChatClient
        self._anon_backoff = 5.0
        self._anon_reboots = 0
        self._anon_fails = 0
        self._next_probe_mono = 0.0
        self._started_mono = time.monotonic()
        self._mode = None

    # ------------------------------------------------------ жизненный цикл

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._mode = self._select_initial_mode()
        if self._mode is None:
            config.log.error(
                "YouTube chat reader: нет ни video_id (анонимный клиент), "
                "ни live_chat_id с токеном (официальный API) — чат "
                "недоступен")
            return
        self._notify_source(self._mode, "старт")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        config.log.info(f"YouTube chat reader started (источник: "
                        f"{self._mode})")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        config.log.info("YouTube chat reader stopped")

    # ------------------------------------------------------ выбор режима

    def _source_mode(self):
        return getattr(config, "YOUTUBE_CHAT_SOURCE", "auto")

    def _can_official(self):
        return bool(self.access_token and self.live_chat_id)

    def _select_initial_mode(self):
        source = self._source_mode()
        if source == "official":
            return "official" if self._can_official() else None
        if not self.video_id:
            return "official" if self._can_official() else None
        # Сессию анонимного клиента поднимаем уже в потоке: загрузка
        # страницы чата занимает 1-2 c и не должна тормозить старт эфира.
        return "innertube"

    def _run(self):
        while not self._stop_event.is_set():
            if self._mode == "innertube":
                wait = self._innertube_cycle()
            else:
                wait = self._official_cycle()
                self._maybe_recover_innertube()
            if wait is None:
                # Чат завершён (эфир окончен) — поток корректно выходит.
                break
            self._stop_event.wait(wait)

    # ------------------------------------------------------ анонимный путь

    def _innertube_cycle(self):
        if self._anon is None:
            return self._try_anon_bootstrap()
        try:
            messages, wait, ended = self._anon.poll()
        except Exception as e:
            # InnertubeError — ожидаемые сбои; всё остальное (баг парсинга,
            # странные типы в данных) нельзя давать убить поток чата:
            # обрабатываем как сбой формата, с тем же фолбэком на API.
            if not isinstance(e, youtube_anon.InnertubeError):
                e = youtube_anon.InnertubeError(
                    f"{type(e).__name__}: {e}", "schema")
            return self._handle_anon_error(e)
        # Успешный опрос: все счётчики ошибок сбрасываются.
        self._anon_fails = 0
        self._anon_reboots = 0
        self._anon_backoff = 5.0
        for item in messages:
            self._emit(item)
        if ended:
            config.log.info(
                "YouTube anon chat: сервер закрыл чат — эфир закончен")
            return None
        return wait

    def _try_anon_bootstrap(self):
        try:
            self._anon = youtube_anon.AnonymousChatClient(self.video_id)
            self._anon.bootstrap()
            # Счётчики ошибок намеренно НЕ сбрасываем: их обнуляет только
            # успешный опрос poll() — иначе «блок на уровне опроса» (страница
            # открывается, а POST режется) маскировался бы бесконечным
            # пересозданием сессий без фолбэка на официальный API.
            return 0.0  # сразу к первому опросу
        except Exception as exc:
            e = exc if isinstance(exc, youtube_anon.InnertubeError) else (
                youtube_anon.InnertubeError(
                    f"{type(exc).__name__}: {exc}", "schema"))
            wait = min(self._anon_backoff, 30.0)
            if e.kind == "not_live":
                age = time.monotonic() - self._started_mono
                if (age > self.NOT_LIVE_GRACE_SEC
                        and self._source_mode() == "auto"
                        and self._can_official()):
                    config.log.warning(
                        "YouTube anon chat: чат не появился за "
                        f"{self.NOT_LIVE_GRACE_SEC:.0f} c — переключаюсь "
                        "на официальный API")
                    self._switch_to_official("чат недоступен анонимно")
                    return self._polling_interval
                wait = min(self._anon_backoff, 60.0)
            elif e.kind in ("blocked", "ratelimit"):
                self._anon_reboots += 1
                wait = min(20.0 * self._anon_reboots, 120.0)
                if (self._anon_reboots >= self.INNERTUBE_REBOOTS
                        and self._switch_to_official(
                            f"сессия не поднимается ({e.kind})")):
                    return self._polling_interval
            config.log.warning(
                f"YouTube anon chat: старт сессии не удался ({e.kind}): {e}")
            self._anon_backoff = min(self._anon_backoff * 2, 60.0)
            return wait

    def _handle_anon_error(self, e):
        if e.kind == "blocked":
            # Блок IP/сессии: поднимаем сессию заново со свежим
            # visitorData — на живом чате это восстанавливало поллинг.
            self._anon = None
            self._anon_reboots += 1
            config.log.warning(
                f"YouTube anon chat: блокировка (403) — пересоздаю сессию "
                f"(попытка {self._anon_reboots}/{self.INNERTUBE_REBOOTS})")
            if (self._anon_reboots >= self.INNERTUBE_REBOOTS
                    and self._switch_to_official(
                        "блок анонимных запросов (403)")):
                return self._polling_interval
            return min(20.0 * self._anon_reboots, 120.0)
        if e.kind == "ratelimit":
            self._anon = None
            self._anon_reboots += 1
            if (self._anon_reboots >= self.INNERTUBE_REBOOTS
                    and self._switch_to_official(
                        "рейт-лимит анонимных запросов (429)")):
                return self._polling_interval
            return min(60.0 * self._anon_reboots, 240.0)
        if e.kind == "network":
            # Транспортная ошибка — официальный API падал бы так же,
            # фолбэк не имеет смысла, просто ждём.
            self._anon_backoff = min(max(self._anon_backoff, 5.0) * 2, 30.0)
            config.log.debug(f"YouTube anon chat: сеть ({e})")
            return self._anon_backoff
        # schema / http: похоже на смену формата или постоянную ошибку.
        self._anon_fails += 1
        config.log.warning(
            f"YouTube anon chat: ошибка опроса ({e.kind}): {e}")
        if (self._anon_fails >= self.INNERTUBE_FAILS
                and self._switch_to_official(
                    f"неожиданный формат ответа ({e.kind})")):
            return self._polling_interval
        return 10.0

    def _maybe_recover_innertube(self):
        """Из official-режима раз в RECOVER_PROBE_SEC пробуем вернуться."""
        if self._mode != "official" or self._stop_event.is_set():
            return
        if self._source_mode() != "auto" or not self.video_id:
            return
        now = time.monotonic()
        if now < self._next_probe_mono:
            return
        self._next_probe_mono = now + self.RECOVER_PROBE_SEC
        try:
            client = youtube_anon.AnonymousChatClient(self.video_id)
            client.bootstrap()
            # Разогревочный опрос: его ответ обычно пуст («с этого
            # момента»), а история уже показана официальным путём.
            client.poll()
            self._anon = client
            self._anon_reboots = 0
            self._anon_fails = 0
            self._anon_backoff = 5.0
            self._mode = "innertube"
            self._notify_source("innertube", "восстановлен")
            config.log.info("YouTube chat: вернулся на анонимный источник "
                            "(квота больше не тратится)")
        except Exception as e:
            config.log.debug(f"YouTube chat: проба анонимного пути не "
                             f"удалась: {e}")

    # ------------------------------------------------------ официальный путь

    def _official_cycle(self):
        """Один цикл liveChatMessages.list (как в версиях до 3.23)."""
        try:
            result = youtube_api.list_live_chat_messages(
                self.access_token,
                self.live_chat_id,
                page_token=self._next_page_token,
            )
            # Успешный вызов liveChatMessages.list стоит 5 единиц квоты
            youtube_api.quota_tracker.add(
                youtube_api.quota_tracker.CHAT_POLL_UNITS)
            if self._quota_exhausted:
                self._quota_exhausted = False
                config.log.info(
                    "YouTube chat: квота снова доступна — опрос "
                    "возобновлён")

            polling_ms = result.get("polling_interval_millis", 5000)
            self._polling_interval = max(polling_ms / 1000, 5)

            quota_used = youtube_api.quota_tracker.used()
            quota_pct = quota_used / float(
                youtube_api.quota_tracker.DAILY_LIMIT)
            if quota_pct > 0.7:
                self._polling_interval = max(self._polling_interval, 15)
            if quota_pct > 0.9:
                self._polling_interval = max(self._polling_interval, 30)
                if self.on_quota_warning:
                    remaining = youtube_api.quota_tracker.remaining()
                    self.on_quota_warning(remaining)

            self._next_page_token = result.get("next_page_token")

            if self._skip_first_official_page:
                # Фолбэк с анонимного пути: первая страница официального
                # API — «история» последних сообщений, уже показанных.
                self._skip_first_official_page = False
            else:
                for item in result["items"]:
                    self._emit(item)

        except youtube_api.YouTubeAPIError as e:
            if youtube_api.is_quota_exceeded_error(e):
                # Дневная квота исчерпана (считает сервер Google):
                # продолжать долбить каждые 30 c бессмысленно — 403
                # бесплатный, но чата всё равно нет до сброса.
                self._quota_exhausted = True
                reset_in = youtube_api.quota_tracker.seconds_until_reset()
                config.log.warning(
                    f"YouTube chat: дневная квота исчерпана (HTTP 403) "
                    f"— пауза опроса до сброса "
                    f"(~{reset_in // 3600} ч {reset_in % 3600 // 60} мин)")
                if self.on_quota_exhausted:
                    try:
                        self.on_quota_exhausted(reset_in)
                    except Exception as cb_err:
                        config.log.debug(f"on_quota_exhausted: {cb_err}")
                # Проверяем раз в 15 минут: как только квота сбросится
                # (или была локальная ошибка оценки) — опрос вернётся.
                self._polling_interval = max(60.0, min(reset_in, 900.0))
            else:
                config.log.error(f"YouTube chat poll error: {e}")
                self._polling_interval = min(
                    self._polling_interval * 2, 30)

        except Exception as e:
            config.log.error(f"YouTube chat poll error: {e}")
            self._polling_interval = min(self._polling_interval * 2, 30)

        return self._polling_interval

    # ------------------------------------------------------ вспомогательное

    def _switch_to_official(self, reason):
        """Фолбэк на официальный API. False — если он невозможен."""
        if self._source_mode() != "auto" or not self._can_official():
            # Строгий innertube или нет официальных кредов: остаёмся на
            # анонимном пути, ретраи продолжатся с нарастающим бэкоффом.
            return False
        self._anon = None
        self._anon_fails = 0
        self._anon_reboots = 0
        self._anon_backoff = 5.0
        self._mode = "official"
        self._skip_first_official_page = True
        self._next_probe_mono = time.monotonic() + self.RECOVER_PROBE_SEC
        self._notify_source("official", reason)
        config.log.warning(
            f"YouTube chat: анонимный источник недоступен ({reason}) — "
            "переключаюсь на официальный API (квота тратится)")
        return True

    def _emit(self, item):
        self.message_queue.put({
            "platform": "youtube",
            "author": item["author"],
            "text": item["text"],
            "color": "#FF0000",
            "avatar": item.get("avatar"),
            "timestamp": time.time(),
            "is_owner": item.get("is_owner", False),
            "is_moderator": item.get("is_moderator", False),
        })

    def _notify_source(self, source, reason):
        if not self.on_source_change:
            return
        try:
            self.on_source_change(source, reason)
        except Exception as e:
            config.log.debug(f"on_source_change: {e}")
