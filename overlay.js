/* «Чат для показа» — клиент страницы /overlay.
 *
 * Подключается к тому же WS-серверу, что и док-панель:
 * получает chat_message (рассылка всем клиентам) и overlay_config
 * (формат/TTL/лимит/включённость). Сообщения живут не дольше TTL
 * секунд, на экране — не больше max_messages; и то и другое
 * ограничивает память (таймеры TTL самоочищаются, лишние узлы
 * удаляются сразу).
 */

"use strict";

const WS_URL = "ws://127.0.0.1:__WS_PORT__";
const CHAT = document.getElementById("overlay-chat");

const cfg = {
  enabled: false,                 // «чат для показа» выключен в настройках
  format: (window.ChatFormat && ChatFormat.DEFAULT_FORMAT) ||
          "<time> <source> <avatar> <username>: <message>",
  ttl_sec: 30,
  max_messages: 15,
};

let ws = null;
let reconnectDelay = 1000;

function connect() {
  try {
    ws = new WebSocket(WS_URL);
  } catch (e) {
    scheduleReconnect();
    return;
  }
  ws.onopen = () => {
    reconnectDelay = 1000;
    ws.send(JSON.stringify({ type: "get_overlay_config" }));
  };
  ws.onclose = scheduleReconnect;
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch (e) {
      return;
    }
    handleMessage(msg);
  };
}

function scheduleReconnect() {
  setTimeout(connect, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 8000);
}

function handleMessage(msg) {
  switch (msg.type) {
    case "overlay_config":
      applyConfig(msg.overlay || {});
      break;
    case "chat_message":
      if (cfg.enabled) {
        appendMessage(msg);
      }
      break;
    // остальные типы (settings, viewers, …) странице не нужны
  }
}

function applyConfig(c) {
  if (typeof c.format === "string" && c.format.trim()) {
    cfg.format = c.format;
  }
  const num = (v, fallback) => {
    const n = parseInt(v, 10);
    return isNaN(n) ? fallback : n;
  };
  cfg.ttl_sec = Math.max(1, num(c.ttl_sec, cfg.ttl_sec));
  cfg.max_messages = Math.max(1, num(c.max_messages, cfg.max_messages));
  cfg.enabled = !!c.enabled;
  trim();
}

function appendMessage(msg) {
  const row = (window.ChatFormat)
    ? ChatFormat.renderRow(msg, cfg.format)
    : fallbackRow(msg);

  CHAT.appendChild(row);
  trim();
  CHAT.scrollTop = CHAT.scrollHeight;

  // TTL: сообщение исчезает само, чтобы не висеть поверх стрима,
  // если все замолчали. Таймер держит ссылку только до срабатывания —
  // копилки ссылок нет. Если узел уже удалён лимитом, remove() — no-op.
  setTimeout(() => removeRow(row), cfg.ttl_sec * 1000);
}

function removeRow(row) {
  if (!row.parentNode) return;           // уже удалён лимитом/TTL
  row.classList.add("fade-out");
  setTimeout(() => { row.remove(); }, 460);
}

function trim() {
  while (CHAT.children.length > cfg.max_messages) {
    CHAT.removeChild(CHAT.firstElementChild);
  }
}

/** Резервный вид, если chat_format.js не загрузился. */
function fallbackRow(msg) {
  const div = document.createElement("div");
  div.className = "chat-message " + (msg.platform === "twitch" ? "twitch" : "youtube");

  const time = document.createElement("span");
  time.className = "time";
  const d = new Date((msg.timestamp ? msg.timestamp * 1000 : Date.now()));
  time.textContent = String(d.getHours()).padStart(2, "0") + ":" +
    String(d.getMinutes()).padStart(2, "0");

  const prefix = document.createElement("span");
  prefix.className = "prefix";
  prefix.textContent = msg.platform === "twitch" ? "TW" : "YT";

  const author = document.createElement("span");
  author.className = "author";
  author.style.color = msg.color || "#fff";
  author.textContent = msg.author;

  const text = document.createElement("span");
  text.className = "text";
  text.textContent = ": " + msg.text;

  div.appendChild(time);
  div.appendChild(prefix);
  div.appendChild(author);
  div.appendChild(text);
  return div;
}

connect();
