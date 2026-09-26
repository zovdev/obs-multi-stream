
const WS_URL = "ws://127.0.0.1:__WS_PORT__";

const $ = (id) => document.getElementById(id);

const twCount = $("tw-count"), ytCount = $("yt-count");
const liveControls = $("live-controls"), timerEl = $("timer");

const panelAuth = $("panel-auth"), panelSettings = $("panel-settings"), panelLive = $("panel-live");

const twVerdict = $("tw-verdict"), twUser = $("tw-user");
const twChecks = {
  token: $("tw-check-token"),
  scopes: $("tw-check-scopes"),
  api: $("tw-check-api"),
};
const twUsercode = $("tw-usercode"), twUsercodeVal = $("tw-usercode-val");
const btnTwCopy = $("btn-tw-copy"), btnTwOpen = $("btn-tw-open");
const twError = $("tw-error"), btnTwAuth = $("btn-tw-auth");

const ytVerdict = $("yt-verdict"), ytUser = $("yt-user");
const ytChecks = {
  token: $("yt-check-token"),
  scopes: $("yt-check-scopes"),
  api: $("yt-check-api"),
};
const ytUsercode = $("yt-usercode");
const ytAuthActions = $("yt-auth-actions");
const btnYtOpenDock = $("btn-yt-open-dock"), btnYtOpenExt = $("btn-yt-open-ext");
const ytError = $("yt-error"), btnYtAuth = $("btn-yt-auth");
const ytObsNote = $("yt-obs-note"), ytHintObs = $("yt-hint-obs");

const ytClientId = $("yt-client-id"), ytClientSecret = $("yt-client-secret");
const btnYtSaveClient = $("btn-yt-save-client");

const btnRecheck = $("btn-recheck");

const fTitle = $("set-title"), fDescription = $("set-description");
const fTags = $("set-tags"), fGame = $("set-game");
const gameField = $("game-field"), gameCandidates = $("game-candidates");
const gameSelected = $("game-selected"), gameChip = $("game-chip");
const gameChipName = $("game-chip-name"), gameChipBadge = $("game-chip-badge");
const gameChipClear = $("game-chip-clear");
const fTwBitrate = $("set-twitch-bitrate"), fYtBitrate = $("set-youtube-bitrate");
const tagsChips = $("tags-chips"), twBitrateWarn = $("tw-bitrate-warn");
const thumbPreview = $("thumb-preview"), thumbImg = $("thumb-img");
const thumbEmpty = $("thumb-empty");
const thumbClear = $("thumb-clear"), thumbFile = $("thumb-file");
const saveState = $("save-state");
const thumbRow = $("thumb-row");

const presetRow = $("preset-row");
const presetSelect = $("preset-select");
const presetPopover = $("preset-popover");
const presetNameInput = $("preset-name");
const btnPresetSave = $("btn-preset-save");
const btnPresetRename = $("btn-preset-rename");
const btnPresetDelete = $("btn-preset-delete");
const btnPresetNameOk = $("btn-preset-name-ok");
const btnPresetNameCancel = $("btn-preset-name-cancel");

const btnStart = $("btn-start"), startHint = $("start-hint");
const btnStop = $("btn-stop");
const twStat = $("stat-twitch"), ytStat = $("stat-youtube"), omrStat = $("stat-omr");

const chat = $("chat"), chatPlaceholder = $("chat-placeholder"), logLine = $("log-line");

let ws = null;
let reconnectDelay = 1000;
let streaming = false;
let streamStartedAt = null;
let timerInterval = null;

let auth = {
  twitch: { connected: false, verified: false, in_progress: false },
  youtube: { connected: false, verified: false, in_progress: false },
};

let engineState = null;

let oauthSession = { twitch: null, youtube: null };

let ytCategory = null;
let gameEditMode = false;
let gameRefineMode = false;
let gameLastQuery = null;
let tags = [];
let thumbnailPath = "";
let thumbnailUrl = null;
let thumbVersion = 0;

(function handleOAuthReturn() {
  const h = location.hash;
  if (!h || !h.startsWith("#oauth")) return;
  history.replaceState(null, "", location.pathname + location.search);
      setTimeout(() => {
    if (h === "#oauth-ok") {
      log("Google: вход подтверждён — проверяем токены…", "success");
    } else if (h === "#oauth-fail") {
      log("Google: авторизация не завершена (отменена/ошибка)", "warning");
    }
  }, 300);
})();

function connect() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
            reconnectDelay = 1000;
    send({ type: "get_state" });
  };

  ws.onmessage = (event) => {
    try {
      handleMessage(JSON.parse(event.data));
    } catch (e) {
      console.error("Invalid JSON from server:", e);
    }
  };

  ws.onerror = () => {};

  ws.onclose = () => {
    log("Нет соединения с плагином, переподключение…", "warning");
    setTimeout(() => {
      reconnectDelay = Math.min(reconnectDelay * 1.5, 10000);
      connect();
    }, reconnectDelay);
  };
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

function handleMessage(msg) {
  switch (msg.type) {
    case "welcome":
      send({ type: "get_state" });
      break;

    case "auth_status":
      auth.twitch = msg.twitch || {};
      auth.youtube = msg.youtube || {};
      renderAuth();
      break;

    case "oauth_progress":
      handleOauthProgress(msg);
      break;

    case "oauth_session":
      if (msg.session) {
        oauthSession.twitch = msg.session.twitch || null;
        oauthSession.youtube = msg.session.youtube || null;
        renderOauthActions();
      }
      break;

    case "settings":
      applySettings(msg.settings);
      break;

    case "settings_saved":
      setSaveState("saved");
      break;

    case "stream_status":
      setStreaming(msg.streaming, msg.started_at, msg.error);
      break;

    case "output_stats":
      renderOutputStats(msg.stats || {});
      break;

    case "engine_status":
      engineState = msg.engine || null;
      renderOmrLine();
      renderStages();
      break;

    case "viewers":
      twCount.textContent = (msg.twitch !== null && msg.twitch !== undefined) ? msg.twitch : "—";
      ytCount.textContent = (msg.youtube !== null && msg.youtube !== undefined) ? msg.youtube : "—";
      break;

    case "chat_message":
      appendChatMessage(msg);
      break;

    case "game_candidates":
      renderGameCandidates(msg);
      break;

    case "presets":
      renderPresets(msg);
      break;

    case "preview_uploaded":
      thumbnailPath = msg.path || "";
      thumbnailUrl = msg.url || null;
      thumbVersion++;
      renderThumbnail();
      send({ type: "update_settings", settings: collectSettings() });
      log("Превью обновлено", "success");
      break;

    case "quota_warning":
      log(`YouTube API quota: осталось ~${msg.remaining} единиц`, "warning");
      break;

    case "log":
      log(msg.message, msg.level);
      break;

    default:
      console.log("Unknown message type:", msg.type, msg);
  }
}

function setCheck(el, status, text) {
  el.className = "check-" + status;
  el.textContent = text;
}

function setVerdict(el, cls, text) {
  el.className = "verdict " + cls;
  el.textContent = text;
}

function renderAuth() {
  const tw = auth.twitch || {};
  const yt = auth.youtube || {};

    if (tw.in_progress) {
    setVerdict(twVerdict, "busy", "авторизация…");
    btnTwAuth.disabled = true;
    btnTwAuth.textContent = "Ожидание подтверждения…";
  } else {
    btnTwAuth.disabled = false;
    btnTwAuth.textContent = tw.connected ? "Переподключить Twitch" : "Подключить Twitch";
  }

  if (tw.connected) {
    setCheck(twChecks.token, "ok",
      `Токен: ${tw.user || "ok"}${tw.user_id ? " (id " + tw.user_id + ")" : ""}`);
  } else {
    setCheck(twChecks.token, tw.in_progress ? "busy" : "fail",
             tw.in_progress ? "Токен: ожидание…" : "Токен: нет");
  }

  if (tw.connected) {
    if (tw.missing_scopes && tw.missing_scopes.length) {
      setCheck(twChecks.scopes, "fail", "Права: не хватает " + tw.missing_scopes.join(", "));
    } else {
      setCheck(twChecks.scopes, "ok", "Права: управление трансляцией ✓");
    }
  } else {
    setCheck(twChecks.scopes, tw.in_progress ? "busy" : "unknown",
             "Права: управление трансляцией");
  }

  if (tw.api_ok === true) {
    setCheck(twChecks.api, "ok",
      "API канала: " + (tw.channel_title ? "«" + tw.channel_title + "»" : "доступен"));
  } else if (tw.api_ok === false) {
    setCheck(twChecks.api, "fail", "API: " + (tw.api_error || "недоступен"));
  } else {
    setCheck(twChecks.api, tw.in_progress ? "busy" : "unknown", "Проверка API канала");
  }

  if (!tw.in_progress) {
    if (tw.verified) setVerdict(twVerdict, "ok", "готово");
    else if (tw.connected) setVerdict(twVerdict, "warn", "нужна проверка");
    else setVerdict(twVerdict, "fail", "не подключён");
  }

  showErr(twError, tw.api_error);

    if (yt.in_progress) {
    setVerdict(ytVerdict, "busy", "авторизация…");
    btnYtAuth.disabled = true;
    btnYtAuth.textContent = "Вход выполняется… (кнопки ниже)";
            ytUsercode.hidden = true;
  } else {
    btnYtAuth.disabled = false;
    btnYtAuth.textContent = yt.connected ? "Переподключить YouTube" : "Подключить YouTube";
    if (ytAuthActions) ytAuthActions.hidden = true;
  }

    renderYtNote(yt);

  if (yt.connected) {
    setCheck(ytChecks.token, "ok", "Токен: " + (yt.email || "получен"));
  } else {
    setCheck(ytChecks.token, yt.in_progress ? "busy" : "fail",
             yt.in_progress ? "Токен: ожидание…" : "Токен: нет");
  }

  if (yt.connected) {
    if (yt.missing_scopes && yt.missing_scopes.length) {
      setCheck(ytChecks.scopes, "fail", "Права: не хватает " + yt.missing_scopes.join(", "));
    } else {
      const thumb = yt.has_thumbnail_scope ? " + превью ✓" : "";
      setCheck(ytChecks.scopes, "ok", "Права: управление трансляцией ✓" + thumb);
    }
  } else {
    setCheck(ytChecks.scopes, yt.in_progress ? "busy" : "unknown",
             "Права: управление трансляцией");
  }

  if (yt.api_ok === true) {
    setCheck(ytChecks.api, "ok",
      "API канала: " + (yt.channel_title ? "«" + yt.channel_title + "»" : "доступен"));
  } else if (yt.api_ok === false) {
    setCheck(ytChecks.api, "fail", "API: " + (yt.api_error || "недоступен"));
  } else {
    setCheck(ytChecks.api, yt.in_progress ? "busy" : "unknown", "Проверка API канала");
  }

  if (!yt.in_progress) {
    if (yt.verified) setVerdict(ytVerdict, "ok", "готово");
    else if (yt.connected) setVerdict(ytVerdict, "warn", "нужна проверка");
    else setVerdict(ytVerdict, "fail", "не подключён");
  }

  showErr(ytError, yt.api_error);

    twUser.hidden = !tw.user;
  twUser.textContent = tw.user ? "Аккаунт: " + tw.user : "";
  ytUser.hidden = !yt.email;
  ytUser.textContent = yt.email ? "Аккаунт: " + yt.email : "";

  renderOauthActions();
  renderStages();
}

function renderYtNote(yt) {
  let note = "";

  if (yt.verified && yt.source === "obs") {
    note = "✓ Вход через OBS — токен обновляется клиентом OBS, ничего создавать не нужно.";
  } else if (yt.verified && yt.source === "manual") {
    note = "✓ Вход со своим Google-клиентом.";
  } else if (yt.obs_client_found) {
    note = "Клиент OBS найден ✓ — вход выполняется тем же способом, что и в самом OBS (без создания своего клиента).";
  } else if (yt.obs_connected) {
    note = "YouTube подключён в OBS, но клиент OBS ещё не извлечён — нажмите «Перепроверить».";
  }

  if (note) {
    ytObsNote.hidden = false;
    ytObsNote.textContent = note;
  } else {
    ytObsNote.hidden = true;
  }

    ytHintObs.hidden = !!yt.connected;
}

function showErr(el, text) {
  if (text) {
    el.hidden = false;
    el.textContent = text;
  } else {
    el.hidden = true;
  }
}

function renderStages() {
  const twOk = !!auth.twitch.verified;
  const ytOk = !!auth.youtube.verified;

      panelAuth.hidden = streaming || (twOk && ytOk);

    panelSettings.hidden = !(twOk && ytOk && !streaming);

    panelLive.hidden = !streaming;

      liveControls.hidden = !streaming;

      chat.hidden = !streaming;
  if (!streaming) clearChat();

      const wsOpen = ws && ws.readyState === WebSocket.OPEN;
  const omr = (engineState && engineState.omr) || {};
  const omrUnknown = !engineState || !engineState.omr;
  const omrOk = omr.state === "ok";
  const canStart = twOk && ytOk && !streaming && wsOpen && omrOk;
  btnStart.disabled = !canStart;
  startHint.textContent = streaming ? "Стрим идёт"
    : (!twOk || !ytOk) ? "Запуск доступен после подключения Twitch и YouTube"
    : omrUnknown ? "Проверяю плагин obs-multi-rtmp…"
    : !omrOk ? "Нужен плагин obs-multi-rtmp (0.6+) — он гонит оба потока"
    : !wsOpen ? "Нет соединения с плагином"
    : "";
  startHint.hidden = !startHint.textContent;
}

function handleOauthProgress(msg) {
  const platform = msg.platform;
  const isTwitch = platform === "twitch";
  const card = isTwitch ? twVerdict : ytVerdict;

    if (!oauthSession[platform]) oauthSession[platform] = {};
  const sess = oauthSession[platform];
  if (msg.user_code) sess.user_code = msg.user_code;
  if (msg.url) sess.url = msg.url;
  sess.stage = msg.stage;

  if (msg.user_code && isTwitch) {
    twUsercode.hidden = false;
    twUsercodeVal.textContent = msg.user_code;
  }

  if (msg.stage === "success" || msg.stage === "verifying") {
    if (isTwitch) twUsercode.hidden = true;
    else ytUsercode.hidden = true;
  }

  setVerdict(card, "busy",
             msg.stage === "user_code" ? "введите код" :
             msg.stage === "auth_url" ? "ожидание входа" :
             msg.stage === "waiting" ? "ожидание…" :
             msg.stage === "verifying" ? "проверка прав…" :
             msg.stage === "opening_browser" || msg.stage === "server_started" ? "открыт браузер" :
             "авторизация…");

  renderOauthActions();
  log("[" + platform + "] " + msg.message, "info");
}

function renderOauthActions() {
    const ytSess = oauthSession.youtube;
  const ytActive = !!(auth.youtube && auth.youtube.in_progress && ytSess && ytSess.url);
  if (ytAuthActions) {
    ytAuthActions.hidden = !ytActive;
    if (ytActive) {
      btnYtOpenDock.disabled = false;
      btnYtOpenExt.disabled = false;
                }
  }
    if (btnTwOpen) {
    const twSess = oauthSession.twitch;
    btnTwOpen.disabled = !(auth.twitch && auth.twitch.in_progress && twSess && twSess.url);
  }
}

function openUrlExternal(url) {
  if (!url) return;
  send({ type: "open_url", url: String(url) });
  log("Открываю в системном браузере…", "info");
}

if (btnYtOpenDock) btnYtOpenDock.addEventListener("click", () => {
  const sess = oauthSession.youtube;
  if (!sess || !sess.url) return;
      location.href = sess.url;
});

if (btnYtOpenExt) btnYtOpenExt.addEventListener("click", () => {
  const sess = oauthSession.youtube;
  if (!sess || !sess.url) return;
  openUrlExternal(sess.url);
});

if (btnTwOpen) btnTwOpen.addEventListener("click", () => {
  const sess = oauthSession.twitch;
  if (!sess || !sess.url) return;
  openUrlExternal(sess.url);
});

if (btnTwCopy) btnTwCopy.addEventListener("click", async () => {
  const code = twUsercodeVal.textContent || (oauthSession.twitch && oauthSession.twitch.user_code) || "";
  if (!code) return;
  try {
    await navigator.clipboard.writeText(code);
    log("Код " + code + " скопирован", "success");
  } catch (e) {
    const range = document.createRange();
    range.selectNodeContents(twUsercodeVal);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    log("Код выделен — скопируйте вручную (Ctrl+C)", "info");
  }
});

let saveTimeout = null;
let settingsBusy = false;

function buildTrackSeg(seg, onChange) {
  if (!seg) return { set: () => {}, get: () => 1 };
  let value = 1;
  const buttons = [];
  for (let i = 1; i <= 6; i++) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "seg-btn";
    b.textContent = String(i);
    b.title = "Трек " + i + " микшера OBS";
    b.addEventListener("click", () => {
      set(i);
      onChange();
    });
    seg.appendChild(b);
    buttons.push(b);
  }
  function set(v) {
    value = Math.min(6, Math.max(1, parseInt(v) || 1));
    buttons.forEach((b, idx) => b.classList.toggle("active", idx + 1 === value));
    seg.dataset.value = String(value);
  }
  set(1);
  return { set, get: () => value };
}

const twTrackSeg = buildTrackSeg($("seg-twitch-track"), scheduleSave);
const ytTrackSeg = buildTrackSeg($("seg-youtube-track"), scheduleSave);

function applySettings(s) {
  if (!s) return;
  settingsBusy = true;
  const set = (el, v) => { if (v !== undefined && document.activeElement !== el) el.value = v; };
  set(fTitle, s.title);
  set(fDescription, s.description);
  tags = parseTagList(s.tags || "");
  fTags.value = "";
  renderTags();
  set(fGame, s.game);
  set(fTwBitrate, s.twitch_bitrate);
  set(fYtBitrate, s.youtube_bitrate);
  ytCategory = (s.yt_category && s.yt_category.id)
    ? { id: String(s.yt_category.id), title: s.yt_category.title || "" }
    : null;
  thumbnailPath = s.thumbnail || "";
  thumbnailUrl = s.thumbnail_url || null;
  twTrackSeg.set(s.twitch_audio_track);
  ytTrackSeg.set(s.youtube_audio_track);
  gameEditMode = false;
  gameRefineMode = false;
  hideGameCandidates();
  renderGameMode();
  renderThumbnail();
  updateTwitchBitrateWarn();
  settingsBusy = false;
}

function collectSettings() {
  return {
    title: fTitle.value,
    description: fDescription.value,
    tags: tags.join(", "),
    game: fGame.value,
    yt_category: ytCategory,
    thumbnail: thumbnailPath,
    twitch_bitrate: parseInt(fTwBitrate.value) || 6000,
    youtube_bitrate: parseInt(fYtBitrate.value) || 9000,
    twitch_audio_track: twTrackSeg.get(),
    youtube_audio_track: ytTrackSeg.get(),
  };
}

function updateTwitchBitrateWarn() {
  if (!twBitrateWarn) return;
  const v = parseInt(fTwBitrate.value, 10);
  twBitrateWarn.hidden = !(v > 6000);
}

function scheduleSave() {
  if (!settingsBusy) updateTwitchBitrateWarn();
  setSaveState("saving");
  if (saveTimeout) clearTimeout(saveTimeout);
  saveTimeout = setTimeout(() => {
    send({ type: "update_settings", settings: collectSettings() });
  }, 800);
}

function setSaveState(state) {
  if (state === "saving") {
    saveState.textContent = "сохранение…";
    saveState.className = "save-state saving";
  } else if (state === "saved") {
    const t = new Date().toLocaleTimeString();
    saveState.textContent = "✓ сохранено " + t;
    saveState.className = "save-state saved";
  }
}

[fTitle, fDescription, fGame, fTwBitrate, fYtBitrate].forEach((el) => {
  el.addEventListener("input", scheduleSave);
  el.addEventListener("change", scheduleSave);
});

document.querySelectorAll(".stepper").forEach((st) => {
  const input = st.querySelector("input");
  if (!input) return;
  const min = parseInt(st.dataset.min) || 0;
  const max = parseInt(st.dataset.max) || 0;
  const step = parseInt(st.dataset.step) || 500;
  st.querySelectorAll(".st-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const dir = parseInt(btn.dataset.dir) || 1;
      let v = (parseInt(input.value) || min) + dir * step;
      v = Math.min(max, Math.max(min, v));
      input.value = v;
      scheduleSave();
    });
  });
});

let gameSearchTimer = null;

function hideGameCandidates() {
  if (!gameCandidates) return;
  gameCandidates.hidden = true;
  gameCandidates.innerHTML = "";
  gameRefineMode = false;
}

function renderGameMode() {
  const name = (fGame.value || "").trim();
  const chipMode = !gameEditMode && !!name;
  fGame.hidden = chipMode;
  if (gameSelected) gameSelected.hidden = !chipMode;
  if (chipMode) {
    gameChipName.textContent = name;
    gameChipName.parentElement.title = name + " — нажмите, чтобы изменить";
    renderGameBadge();
  }
}

function renderGameBadge() {
  if (!gameChipBadge) return;
  if (ytCategory && ytCategory.id) {
    gameChipBadge.textContent = "YT: " + (ytCategory.title || "категория");
    gameChipBadge.classList.add("cat");
  } else {
    gameChipBadge.textContent = "YT: авто";
    gameChipBadge.classList.remove("cat");
  }
}

function enterGameEditMode(focus) {
  gameEditMode = true;
  renderGameMode();
  if (focus) fGame.focus();
}

fGame.addEventListener("input", () => {
  if (settingsBusy) return;
  gameEditMode = true;
  ytCategory = null;
  renderGameBadge();
  if (gameSearchTimer) clearTimeout(gameSearchTimer);
  const q = fGame.value.trim();
  if (q.length < 2) {
    gameLastQuery = null;
    hideGameCandidates();
    return;
  }
  showGameLoading();
  gameSearchTimer = setTimeout(() => {
    gameLastQuery = q;
    send({ type: "search_game", query: q });
  }, 350);
});

function showGameLoading() {
  if (gameRefineMode) return;
  gameCandidates.innerHTML = "";
  const loading = document.createElement("div");
  loading.className = "game-group-title";
  loading.textContent = "Ищу…";
  gameCandidates.appendChild(loading);
  gameCandidates.hidden = false;
}

fGame.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    const first = (gameCandidates && !gameCandidates.hidden)
      ? gameCandidates.querySelector("button.game-cand") : null;
    if (first) { first.click(); return; }
    if ((fGame.value || "").trim()) {
      gameEditMode = false;
      hideGameCandidates();
      renderGameMode();
      scheduleSave();
    }
    return;
  }
  if (e.key === "Escape" && (fGame.value || "").trim()) {
    gameEditMode = false;
    hideGameCandidates();
    renderGameMode();
  }
});

if (gameChip) gameChip.addEventListener("click", () => {
  enterGameEditMode(true);
});

if (gameChipClear) gameChipClear.addEventListener("click", () => {
  fGame.value = "";
  ytCategory = null;
  gameEditMode = false;
  renderGameMode();
  hideGameCandidates();
  scheduleSave();
});

if (gameChipBadge) gameChipBadge.addEventListener("click", () => {
  const name = (fGame.value || "").trim();
  if (!name) {
    log("Сначала укажите игру — потом уточните категорию", "warning");
    return;
  }
  gameRefineMode = true;
  gameLastQuery = name;
  gameCandidates.innerHTML = "";
  const loading = document.createElement("div");
  loading.className = "game-group-title";
  loading.textContent = "Ищу категории YouTube…";
  gameCandidates.appendChild(loading);
  gameCandidates.hidden = false;
  send({ type: "search_game", query: name });
});

function renderGameCandidates(msg) {
  if (!gameCandidates) return;
    const q = (msg.query || "").trim().toLowerCase();

  gameCandidates.innerHTML = "";
  const list = msg.candidates || [];
  const cats = list.filter((c) => c.group !== "game");

  if (gameRefineMode) {
    if (gameLastQuery !== null && (msg.query || "") !== gameLastQuery) return;
    gameLastQuery = null;
    const auto = document.createElement("button");
    auto.type = "button";
    auto.className = "game-cand tw";
    const autoMark = document.createElement("span");
    autoMark.className = "cand-mark";
    autoMark.textContent = "АВТО";
    const autoName = document.createElement("span");
    autoName.textContent = "Категория по игре (как в самом OBS)";
    auto.appendChild(autoMark);
    auto.appendChild(autoName);
    auto.addEventListener("click", () => {
      ytCategory = null;
      hideGameCandidates();
      renderGameBadge();
      scheduleSave();
    });
    gameCandidates.appendChild(auto);
    for (const c of cats) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "game-cand yt" + (c.is_default ? " default" : "");
      const mark = document.createElement("span");
      mark.className = "cand-mark";
      mark.textContent = "YT";
      const name = document.createElement("span");
      name.textContent = c.name + (c.is_default ? " · по умолчанию" : "");
      btn.appendChild(mark);
      btn.appendChild(name);
      btn.addEventListener("click", () => {
        ytCategory = { id: String(c.id), title: c.name };
        hideGameCandidates();
        renderGameBadge();
        scheduleSave();
      });
      gameCandidates.appendChild(btn);
    }
    gameCandidates.hidden = false;
    return;
  }

  if (gameLastQuery !== null && (msg.query || "") !== gameLastQuery) return;
  gameLastQuery = null;
  if (!list.length) {
    gameCandidates.hidden = true;
    return;
  }
  const games = list.filter((c) => c.group === "game");

  const addSection = (title) => {
    const h = document.createElement("div");
    h.className = "game-group-title";
    h.textContent = title;
    gameCandidates.appendChild(h);
  };

  const addCandidate = (c) => {
    const isYt = c.platform === "youtube";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "game-cand " + (isYt ? "yt" : "tw");

    const mark = document.createElement("span");
    mark.className = "cand-mark";
    mark.textContent = isYt ? "YT" : "TW+YT";

    const name = document.createElement("span");
    name.textContent = c.name + (c.is_default ? " · по умолчанию" : "");

    btn.appendChild(mark);
    btn.appendChild(name);
    btn.addEventListener("click", () => {
      if (isYt) {
                                if (!c.is_default) fGame.value = c.name;
        ytCategory = { id: String(c.id), title: c.name };
      } else {
                        fGame.value = c.name;
        ytCategory = null;
      }
      gameEditMode = false;
      hideGameCandidates();
      renderGameMode();
      scheduleSave();
    });
    gameCandidates.appendChild(btn);
  };

  if (games.length) {
    addSection("Игры — Twitch и YouTube");
    games.forEach(addCandidate);
  }
  if (cats.length) {
    addSection("Категория видео YouTube");
    cats.forEach(addCandidate);
  }
  gameCandidates.hidden = false;
}

document.addEventListener("mousedown", (e) => {
  if (!gameCandidates || gameCandidates.hidden) return;
  if (gameField && gameField.contains(e.target)) return;
  hideGameCandidates();
});

function normalizeTag(raw) {
  return String(raw || "").replace(/#/g, "").trim()
    .replace(/\s+/g, "_").replace(/^_+|_+$/g, "");
}

function parseTagList(str) {
  const out = [];
  const seen = new Set();
  for (const part of String(str || "").split(",")) {
    const tag = normalizeTag(part);
    if (!tag) continue;
    const key = tag.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(tag);
    if (out.length >= 30) break;
  }
  return out;
}

function renderTags() {
  if (!tagsChips) return;
  tagsChips.innerHTML = "";
  tags.forEach((tag, idx) => {
    const chip = document.createElement("span");
    chip.className = "tag-chip";
    const label = document.createElement("span");
    label.textContent = tag;
    const x = document.createElement("span");
    x.className = "tag-x";
    x.title = "Убрать тег";
    x.textContent = "✕";
    x.addEventListener("click", () => {
      tags.splice(idx, 1);
      renderTags();
      scheduleSave();
    });
    chip.appendChild(label);
    chip.appendChild(x);
    tagsChips.appendChild(chip);
  });
}

function addTag(tag) {
  if (!tag) return false;
  const key = tag.toLowerCase();
  if (tags.some((t) => t.toLowerCase() === key)) return false;
  if (tags.length >= 30) return false;
  tags.push(tag);
  return true;
}

function commitTagsInput() {
  const raw = fTags.value;
  if (!raw.includes(",")) return;
  const parts = raw.split(",");
  const rest = parts.pop();
  let added = false;
  for (const part of parts) {
    if (addTag(normalizeTag(part))) added = true;
  }
  fTags.value = rest || "";
  if (added) { renderTags(); scheduleSave(); }
}

function commitTagsFinal() {
  const tag = normalizeTag(fTags.value);
  fTags.value = "";
  if (!tag) return;
  if (!addTag(tag)) return;
  renderTags();
  scheduleSave();
}

fTags.addEventListener("input", commitTagsInput);
fTags.addEventListener("blur", commitTagsFinal);
fTags.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    commitTagsFinal();
  } else if (e.key === "Backspace" && !fTags.value && tags.length) {
    tags.pop();
    renderTags();
    scheduleSave();
  } else if (e.key === "Escape") {
    fTags.value = "";
  }
});

let presets = [];
let pendingPresetAction = null;
let presetConfirmTimer = null;

function selectedPresetId() {
  return presetSelect.value || null;
}

function selectedPreset() {
  const id = selectedPresetId();
  return presets.find((p) => p.id === id) || null;
}

function updatePresetButtons() {
  const has = !!selectedPresetId();
  btnPresetRename.hidden = !has;
  btnPresetDelete.hidden = !has;
}

function renderPresets(msg) {
  presets = msg.presets || [];
  const prev = presetSelect.value;
  presetSelect.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = presets.length
    ? "— выберите пресет —" : "— пресетов нет —";
  presetSelect.appendChild(placeholder);
  for (const p of presets) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name;
    presetSelect.appendChild(opt);
  }
  if (msg.select && String(msg.select)) {
    presetSelect.value = String(msg.select);
  } else if (presets.some((p) => p.id === prev)) {
    presetSelect.value = prev;
  } else {
    presetSelect.value = "";
  }
  updatePresetButtons();
}

function twoClickConfirm(btn, label, action) {
  if (btn.dataset.confirm === "1") {
    clearTimeout(presetConfirmTimer);
    btn.dataset.confirm = "";
    if (btn.dataset.orig) btn.textContent = btn.dataset.orig;
    delete btn.dataset.orig;
    action();
    return;
  }
  btn.dataset.confirm = "1";
  btn.dataset.orig = btn.textContent;
  btn.textContent = label;
  presetConfirmTimer = setTimeout(() => {
    btn.dataset.confirm = "";
    if (btn.dataset.orig) btn.textContent = btn.dataset.orig;
    delete btn.dataset.orig;
  }, 2500);
}

function requestPresetName(action, initial) {
  pendingPresetAction = action;
  presetNameInput.value = initial || "";
  presetPopover.hidden = false;
  presetNameInput.focus();
  presetNameInput.select();
}

function finishPresetName(ok) {
  const action = pendingPresetAction;
  const name = presetNameInput.value.trim();
  pendingPresetAction = null;
  presetPopover.hidden = true;
  presetNameInput.value = "";
  if (!ok || !action) return;
  if (!name) {
    log("Пресет: название не может быть пустым", "error");
    return;
  }
  if (action.mode === "save") {
    send({ type: "preset_save", name });
  } else if (action.mode === "rename") {
    send({ type: "preset_rename", id: action.id, name });
  }
}

document.addEventListener("mousedown", (e) => {
  if (presetPopover.hidden) return;
  if (presetRow.contains(e.target)) return;
  finishPresetName(false);
});

presetSelect.addEventListener("change", () => {
  updatePresetButtons();
  const id = selectedPresetId();
  send({ type: "preset_select", id });
  if (!id) return;
  send({ type: "preset_apply", id });
});

btnPresetSave.addEventListener("click", () => {
  requestPresetName({ mode: "save" }, "");
});

btnPresetRename.addEventListener("click", () => {
  const p = selectedPreset();
  if (!p) {
    log("Выберите пресет в списке", "warning");
    return;
  }
  requestPresetName({ mode: "rename", id: p.id }, p.name);
});

btnPresetDelete.addEventListener("click", () => {
  const p = selectedPreset();
  if (!p) {
    log("Выберите пресет для удаления", "warning");
    return;
  }
  twoClickConfirm(btnPresetDelete, "Точно?", () => {
    send({ type: "preset_delete", id: p.id });
  });
});

btnPresetNameOk.addEventListener("click", () => finishPresetName(true));
btnPresetNameCancel.addEventListener("click", () => finishPresetName(false));
presetNameInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") finishPresetName(true);
  if (e.key === "Escape") finishPresetName(false);
});

function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

async function preparePreviewPayload(file) {
  try {
    if (file.size <= 1.5 * 1024 * 1024 &&
        (file.type === "image/jpeg" || file.type === "image/png")) {
      const url = await readAsDataURL(file);
      return { name: file.name || "preview", data: url.slice(url.indexOf(",") + 1) };
    }
    const url = await readAsDataURL(file);
    const img = await new Promise((resolve, reject) => {
      const i = new Image();
      i.onload = () => resolve(i);
      i.onerror = () => reject(new Error("не удалось открыть изображение"));
      i.src = url;
    });
    const scale = Math.min(1, 1920 / Math.max(img.width || 1, img.height || 1));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round((img.width || 1) * scale));
    canvas.height = Math.max(1, Math.round((img.height || 1) * scale));
    canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
    const out = canvas.toDataURL("image/jpeg", 0.85);
    const stem = (file.name || "preview").replace(/\.[^.]*$/, "") || "preview";
    return { name: stem + ".jpg", data: out.slice(out.indexOf(",") + 1) };
  } catch (e) {
    log("Превью: не удалось прочитать файл (" + e + ")", "error");
    return null;
  }
}

["dragenter", "dragover"].forEach((ev) =>
  thumbRow.addEventListener(ev, (e) => {
    e.preventDefault();
    e.stopPropagation();
    thumbRow.classList.add("dragover");
  }));
["dragleave", "drop"].forEach((ev) =>
  thumbRow.addEventListener(ev, (e) => {
    e.preventDefault();
    e.stopPropagation();
    thumbRow.classList.remove("dragover");
  }));
thumbRow.addEventListener("drop", async (e) => {
  const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (!file) return;
  await uploadPreviewFile(file);
});

function renderThumbnail() {
  const has = !!thumbnailPath;
  if (has) {
    const name = thumbnailPath.replace(/\\/g, "/").split("/").pop() || "превью";
    if (thumbnailUrl) {
      thumbEmpty.hidden = true;
      thumbImg.hidden = false;
      const key = thumbnailUrl + "@" + thumbVersion;
      if (thumbImg.dataset.key !== key) {
        thumbImg.dataset.key = key;
        thumbImg.src = thumbnailUrl + "?v=" + thumbVersion;
      }
    } else {
      delete thumbImg.dataset.key;
      thumbImg.hidden = true;
      thumbImg.removeAttribute("src");
      thumbEmpty.hidden = false;
      const ext = (name.split(".").pop() || "").toUpperCase();
      thumbEmpty.textContent = ext === "JPG" || ext === "JPEG" || ext === "PNG" ? ext : "ФАЙЛ";
    }
    thumbClear.hidden = false;
  } else {
    delete thumbImg.dataset.key;
    thumbImg.hidden = true;
    thumbImg.removeAttribute("src");
    thumbEmpty.hidden = false;
    thumbEmpty.textContent = "＋";
    thumbClear.hidden = true;
  }
}

async function uploadPreviewFile(file) {
  if (!file) return;
  if (!/^image\//.test(file.type)) {
    log("Превью: перетащите файл изображения (JPG/PNG)", "error");
    return;
  }
  const payload = await preparePreviewPayload(file);
  if (!payload) return;
  log("Превью: загрузка файла в плагин…", "info");
  send({ type: "upload_preview", name: payload.name, data_base64: payload.data });
}

if (thumbPreview) thumbPreview.addEventListener("click", () => {
  thumbFile.click();
});

if (thumbFile) thumbFile.addEventListener("change", async () => {
  const file = thumbFile.files && thumbFile.files[0];
  thumbFile.value = "";
  await uploadPreviewFile(file);
});

if (thumbClear) thumbClear.addEventListener("click", (e) => {
  e.stopPropagation();
  e.preventDefault();
  thumbnailPath = "";
  thumbnailUrl = null;
  thumbVersion++;
  renderThumbnail();
  scheduleSave();
  log("Превью убрано", "info");
});

renderThumbnail();

btnTwAuth.addEventListener("click", () => {
  send({ type: "twitch_oauth" });
  log("Twitch: запускаем Device Code Flow…", "info");
});

btnYtAuth.addEventListener("click", () => {
  send({ type: "youtube_oauth" });
  log("YouTube: запускаем вход через Google…", "info");
});

btnYtSaveClient.addEventListener("click", () => {
  const id = ytClientId.value.trim();
  const secret = ytClientSecret.value.trim();
  if (!id || !secret) {
    log("Нужны ОБА поля: Client ID и Client Secret", "error");
    return;
  }
  send({ type: "set_google_client", client_id: id, client_secret: secret });
  log("Google OAuth клиент: сохранение…", "info");
});

btnRecheck.addEventListener("click", () => {
  send({ type: "recheck_auth" });
  log("Повторная проверка токенов…", "info");
});

btnStart.addEventListener("click", () => {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    log("Нет соединения с плагином", "error");
    return;
  }
        send({ type: "update_settings", settings: collectSettings() });
  send({ type: "start" });
  log("Запуск стрима (Twitch + YouTube)…", "info");
  btnStart.disabled = true;
  setTimeout(() => renderStages(), 3000);
});

btnStop.addEventListener("click", () => {
  send({ type: "stop" });
  log("Остановка стрима…", "info");
});

twUsercodeVal.addEventListener("click", async () => {
  const code = twUsercodeVal.textContent;
  try {
    await navigator.clipboard.writeText(code);
    log("Код " + code + " скопирован в буфер обмена", "success");
  } catch (e) {
        const range = document.createRange();
    range.selectNodeContents(twUsercodeVal);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
});

function setStreaming(isStreaming, startedAt, error) {
  streaming = !!isStreaming;
  streamStartedAt = startedAt ? startedAt * 1000 : null;

    liveControls.hidden = !streaming;

  if (streaming) {
    startTimer();
    if (error) log(error, "warning");
    if (twStat) twStat.textContent = "подключение…";
    if (ytStat) ytStat.textContent = "подключение…";
  } else {
    stopTimer();
    if (twStat) twStat.textContent = "—";
    if (ytStat) ytStat.textContent = "—";
    if (error) log("Запуск не удался: " + error, "error");
  }
  renderStages();
}

function renderOutputStats(stats) {
  const fmt = (s) => {
    if (!s) return "—";
    const kbps = s.kbps ? " · " + s.kbps + " kbps" : "";
    if (s.live) return "в эфире" + kbps;
    if (stats.obs_streaming) return "подключение…" + kbps;
    return "остановлен";
  };
  if (twStat) twStat.textContent = fmt(stats.twitch);
  if (ytStat) ytStat.textContent = fmt(stats.youtube);
}

function renderOmrLine() {
  if (!omrStat) return;
  if (!engineState || !engineState.omr) {
    omrStat.textContent = "проверка…";
    omrStat.className = "stat-text";
    return;
  }
  const omr = engineState.omr;
  if (omr.state === "ok") {
    omrStat.textContent = "v" + (omr.version || "?");
    omrStat.className = "stat-text ok";
  } else if (omr.state === "old_version") {
    omrStat.textContent = "версия " + (omr.version || "?") +
      " устарела — нужна 0.6+";
    omrStat.className = "stat-text warn";
  } else {
    omrStat.textContent = "не найден — установите плагин (0.6+)";
    omrStat.className = "stat-text fail";
  }
}

function startTimer() {
  stopTimer();
  const update = () => {
    if (!streamStartedAt) return;
    const elapsed = Math.floor((Date.now() - streamStartedAt) / 1000);
    const h = Math.floor(elapsed / 3600);
    const m = Math.floor((elapsed % 3600) / 60);
    const s = elapsed % 60;
    const text = h > 0
      ? h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0")
      : m + ":" + String(s).padStart(2, "0");
        timerEl.textContent = text;
  };
  update();
  timerInterval = setInterval(update, 1000);
}

function stopTimer() {
  if (timerInterval) {
    clearInterval(timerInterval);
    timerInterval = null;
  }
  timerEl.textContent = "";
}

function appendChatMessage(msg) {
  if (chat.hidden) return;

  if (chatPlaceholder) chatPlaceholder.hidden = true;

  const div = document.createElement("div");
  div.className = "chat-message " + msg.platform;

      const timeEl = document.createElement("span");
  timeEl.className = "time";
  const ts = (msg.timestamp ? msg.timestamp * 1000 : Date.now());
  const d = new Date(ts);
  timeEl.textContent = String(d.getHours()).padStart(2, "0") + ":" +
    String(d.getMinutes()).padStart(2, "0");
  timeEl.title = d.toLocaleString();

  const prefix = document.createElement("span");
  prefix.className = "prefix";
  prefix.textContent = msg.platform === "twitch" ? "TW" : "YT";

  const author = document.createElement("span");
  author.className = "author";
  author.style.color = msg.color || "#ccc";
  author.textContent = msg.author;

  const text = document.createElement("span");
  text.className = "text";
  text.textContent = ": " + msg.text;

  div.appendChild(timeEl);
  div.appendChild(prefix);
  div.appendChild(author);
  div.appendChild(text);
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;

  while (chat.children.length > 200) {
    const first = chat.firstElementChild;
    if (!first || first === chatPlaceholder) break;
    chat.removeChild(first);
  }
}

function clearChat() {
  if (!chatPlaceholder) return;
  while (chat.lastElementChild && chat.lastElementChild !== chatPlaceholder) {
    chat.removeChild(chat.lastElementChild);
  }
    if (chatPlaceholder.parentNode !== chat) chat.appendChild(chatPlaceholder);
  chatPlaceholder.hidden = false;
}

function log(message, level) {
  logLine.textContent = message;
  logLine.className = "log-line";
  if (level === "warning") logLine.classList.add("warning");
  else if (level === "error") logLine.classList.add("error");
  else if (level === "success") logLine.classList.add("success");
}

renderStages();
connect();
