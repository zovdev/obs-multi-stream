/* ChatFormat — общий рендер сообщений чата по пользовательскому шаблону.
 *
 * Используется и док-панелью (dock.js), и страницей «чата для показа»
 * (overlay.js). Шаблон — строка с тегами:
 *   <time>            — время сообщения, по умолчанию часы:минуты (HH:MM)
 *   <time[%H:%M:%S]>  — время в своём формате (strftime-подмножество:
 *                       %H %M %S %d %m %Y %y, %% — литеральный %)
 *   <source>          — YT/TW — откуда сообщение (цвета платформы)
 *   <avatar>          — аватарка автора; если нет — имитация: цветной
 *                       круг с первой буквой ника (цвет детерминирован
 *                       из ника; у Twitch может прийти цвет пользователя)
 *   <username>        — ник автора
 *   <message>         — текст сообщения
 * Пример: <time><avatar>[<username>] - <message>
 *
 * Весь пользовательский контент вставляется через textContent —
 * XSS-невозможен по построению.
 */
(function (global) {
  "use strict";

  var TAG_RE = /<(time(?:\[[^\]]*\])?|source|avatar|username|message)>/g;
  var DEFAULT_FORMAT = "<time> <source> <avatar> <username>: <message>";

  var TAG_DOCS = [
    { tag: "<time>", desc: "время сообщения, ЧЧ:ММ (например 02:05)" },
    { tag: "<time[%H:%M:%S]>", desc: "время в своём формате: %H часы, %M минуты, %S секунды, %d день, %m месяц, %Y год" },
    { tag: "<source>", desc: "YT или TW — платформа сообщения (цвета как в чате)" },
    { tag: "<avatar>", desc: "аватарка автора; если её нет — цветной круг с первой буквой ника" },
    { tag: "<username>", desc: "ник автора" },
    { tag: "<message>", desc: "текст сообщения" },
  ];

  function pad2(n) { return String(n).padStart(2, "0"); }

  /** strftime-подмножество для времени сообщения. */
  function formatTime(date, fmt) {
    fmt = fmt || "%H:%M";
    return fmt.replace(/%[%HMSdmyY]/g, function (m) {
      switch (m) {
        case "%H": return pad2(date.getHours());
        case "%M": return pad2(date.getMinutes());
        case "%S": return pad2(date.getSeconds());
        case "%d": return pad2(date.getDate());
        case "%m": return pad2(date.getMonth() + 1);
        case "%Y": return String(date.getFullYear());
        case "%y": return pad2(date.getFullYear() % 100);
        case "%%": return "%";
        default: return m;
      }
    });
  }

  /**
   * Шаблон -> массив сегментов:
   *   { kind: "literal", text }
   *   { kind: "time", fmt }     — fmt по умолчанию "%H:%M"
   *   { kind: "source" | "avatar" | "username" | "message" }
   */
  function parse(template) {
    var segments = [];
    var src = String(template || "").trim() || DEFAULT_FORMAT;
    var last = 0;
    TAG_RE.lastIndex = 0;
    var m;
    while ((m = TAG_RE.exec(src)) !== null) {
      if (m.index > last) segments.push({ kind: "literal", text: src.slice(last, m.index) });
      // m[1] — либо "time", либо "time[<fmt>]"
      if (m[1] === "time" || m[1].slice(0, 5) === "time[") {
        var fmt = "%H:%M";
        var fm = /^\[([^\]]*)\]$/.exec(m[1].slice(4));
        if (fm && fm[1].trim()) fmt = fm[1];
        segments.push({ kind: "time", fmt: fmt });
      } else {
        segments.push({ kind: m[1] });
      }
      last = m.index + m[0].length;
    }
    if (last < src.length) segments.push({ kind: "literal", text: src.slice(last) });
    return segments;
  }

  /** Неизвестные «тегоподобные» куски — для предупреждения в UI. */
  function unknownTags(template) {
    var known = {};
    ["time", "source", "avatar", "username", "message"].forEach(function (k) { known[k] = true; });
    var found = [];
    var re = /<([^<>\n]{1,60})>/g;
    var m;
    var src = String(template || "");
    while ((m = re.exec(src)) !== null) {
      var body = m[1];
      var name = body.split("[", 1)[0];
      if (!known[name] && found.indexOf(m[0]) < 0) found.push(m[0]);
    }
    return found;
  }

  /** Детерминированный «цвет ника» для имитации аватара. */
  function nickColor(nick) {
    var s = String(nick || "?");
    var h = 0;
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
    return "hsl(" + h + ", 65%, 42%)";
  }

  function firstLetter(nick) {
    var chars = Array.from(String(nick || "?").trim());
    var ch = chars.length ? chars[0] : "?";
    // Букву — в верхний регистр; эмодзи и прочее оставляем как есть
    var upper = ch.toLocaleUpperCase ? ch.toLocaleUpperCase("ru") : ch;
    return upper || "?";
  }

  function el(tag, cls, doc) {
    var node = (doc || document).createElement(tag);
    if (cls) node.className = cls;
    return node;
  }

  /** Аватар: <img> с фолбэком на букву-круг при ошибке загрузки. */
  function buildAvatar(msg, doc) {
    var url = msg.avatar;
    if (url && /^https?:\/\//i.test(String(url))) {
      var img = el("img", "chat-avatar", doc);
      img.src = url;
      img.alt = "";
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      var nick = msg.author;
      img.addEventListener("error", function () {
        var letter = letterAvatar(nick, msg, doc);
        if (img.parentNode) img.parentNode.replaceChild(letter, img);
      });
      return img;
    }
    return letterAvatar(msg.author, msg, doc);
  }

  function letterAvatar(nick, msg, doc) {
    var span = el("span", "chat-avatar letter", doc);
    span.textContent = firstLetter(nick);
    var color = msg.color || nickColor(nick);
    span.style.background = color;
    span.title = nick;
    return span;
  }

  /**
   * Сообщение -> DOM-строка (.chat-message.<platform>) по шаблону.
   * msg: {platform, author, text, color?, avatar?, timestamp, is_owner?, is_moderator?}
   */
  function renderRow(msg, template, doc) {
    doc = doc || document;
    var row = el("div", "chat-message " + (msg.platform === "twitch" ? "twitch" : "youtube"), doc);
    if (msg.is_owner) row.classList.add("owner");
    if (msg.is_moderator) row.classList.add("moderator");

    var segments = parse(template);
    var ts = (msg.timestamp ? msg.timestamp * 1000 : Date.now());
    var date = new Date(ts);

    segments.forEach(function (seg) {
      switch (seg.kind) {
        case "literal": {
          if (!seg.text) return;
          var lit = el("span", "chat-lit", doc);
          lit.textContent = seg.text;
          row.appendChild(lit);
          return;
        }
        case "time": {
          var t = el("span", "time", doc);
          t.textContent = formatTime(date, seg.fmt);
          t.title = date.toLocaleString();
          row.appendChild(t);
          return;
        }
        case "source": {
          var p = el("span", "prefix", doc);
          p.textContent = msg.platform === "twitch" ? "TW" : "YT";
          p.title = msg.platform === "twitch" ? "Twitch" : "YouTube";
          row.appendChild(p);
          return;
        }
        case "avatar": {
          row.appendChild(buildAvatar(msg, doc));
          return;
        }
        case "username": {
          var a = el("span", "author", doc);
          a.textContent = msg.author;
          a.style.color = msg.color || (msg.platform === "twitch" ? "#9146FF" : "#FF4444");
          if (msg.is_owner) a.title = "Владелец канала";
          else if (msg.is_moderator) a.title = "Модератор";
          row.appendChild(a);
          return;
        }
        case "message": {
          var text = el("span", "text", doc);
          text.textContent = msg.text;
          row.appendChild(text);
          return;
        }
      }
    });

    return row;
  }

  var ChatFormat = {
    DEFAULT_FORMAT: DEFAULT_FORMAT,
    TAGS: TAG_DOCS,
    parse: parse,
    formatTime: formatTime,
    nickColor: nickColor,
    firstLetter: firstLetter,
    unknownTags: unknownTags,
    renderRow: renderRow,
  };

  global.ChatFormat = ChatFormat;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = ChatFormat;
  }
})(typeof window !== "undefined" ? window : globalThis);
