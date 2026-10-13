/* 遊戲化獎勵區塊（國考結果頁／口說結算頁／寫作批改頁共用）。
   用法：GmRewards.render(containerEl, gm, { kind: "exam" | "speaking" | "writing" })
   gm 是後端 /quiz/submit、/speaking/summary、/writing/review 回傳的 gm 欄位。
   __LIFE_LIFF_ID__ 由後端 /assets/gm/ 路由在送出時替換成生活線 LIFF ID。 */
(function () {
  var LIFE_LIFF_URL = "https://liff.line.me/__LIFE_LIFF_ID__";
  var CSS = ".gm-wrap{background:linear-gradient(135deg,#FFF6D8,#F5E4AE);border:1px solid #E2D6B8;border-radius:16px;padding:16px;display:flex;flex-direction:column;gap:12px;font-family:'Noto Sans TC',sans-serif;color:#2A2118}" +
    ".gm-row{display:flex;align-items:center;gap:10px}.gm-row img{width:54px;filter:drop-shadow(0 4px 6px rgba(120,80,0,.25))}" +
    ".gm-cards{display:flex;flex-wrap:wrap;gap:10px;justify-content:center}" +
    ".gm-card{position:relative;width:92px;background:#FFFDF7;border:2px solid #D9A92C;border-radius:12px;padding:6px;text-align:center;box-shadow:0 4px 10px rgba(120,80,0,.2);opacity:0;animation:gmpop .5s cubic-bezier(.2,1.4,.4,1) forwards}" +
    ".gm-card b{display:block;font-size:13px;margin-top:4px}" +
    ".gm-new{position:absolute;top:4px;right:4px;background:#B23A2E;color:#fff;font-size:10px;font-weight:700;padding:1px 6px;border-radius:99px;z-index:1}" +
    ".gm-qty{position:absolute;top:4px;right:4px;background:#2A2118;color:#F1DFA6;font-size:10px;font-weight:700;padding:1px 6px;border-radius:99px;z-index:1}" +
    ".gm-synth{background:#2A2118;color:#F1DFA6;border-radius:12px;padding:10px 12px;display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:14px}" +
    ".gm-synth button,.gm-open{background:#1F6F54;color:#fff;border:0;border-radius:10px;padding:8px 14px;font-weight:700;font-size:13.5px;cursor:pointer}" +
    ".gm-note{font-size:13px;color:#6B5E4B;line-height:1.7}" +
    "@keyframes gmpop{from{opacity:0;transform:translateY(10px) scale(.9)}to{opacity:1;transform:none}}";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (m) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m];
    });
  }
  function debug() { return new URLSearchParams(location.search).get("debug") === "1"; }

  function openCodex(tab) {
    var q = "page=codex&track=student" + (tab ? "&tab=" + tab : "");
    if (debug()) { location.href = "/liff/life?debug=1&" + q; return; }
    try { liff.openWindow({ url: LIFE_LIFF_URL + "?" + q, external: false }); } catch (e) { location.href = LIFE_LIFF_URL + "?" + q; }
  }

  function noteFor(gm, kind) {
    var th = gm.threshold || {};
    if (kind === "exam") {
      if (gm.qualified) return "";
      if (gm.reason === "too_few_questions") return "這份考卷題數不足 " + th.min_questions + " 題。湊滿 " + th.min_questions + " 題、且得分達 " + th.min_score + " 分，就能抽 " + th.draws + " 張藥材卡。";
      if (gm.reason === "score_below_threshold") return "得分達 " + th.min_score + " 分才能抽藥材卡，再加油！";
      if (gm.reason === "daily_limit") return "今天的抽卡次數已經用完囉，明天再來。";
      return "";
    }
    if (gm.reason === "too_short") {
      return kind === "speaking"
        ? "對話至少 " + th.speaking_min_turns + " 句才算一次有效練習，多聊幾句就能拿到經驗值和藥材卡。"
        : "至少寫 " + th.writing_min_words + " 個英文單字才算一次有效練習，寫長一點就能拿到經驗值和藥材卡。";
    }
    if (!(gm.cards || []).length) return "今天的免費掉落已經領過囉，明天再練習還會有。";
    return "";
  }

  function cardHtml(c, k) {
    var em = esc(c.emoji || "🌿");
    var fallback = "<div style=&quot;font-size:40px;height:78px;display:grid;place-items:center&quot;>" + em + "</div>";
    var img = c.image
      ? "<img src=\"/assets/" + esc(c.image) + "\" alt=\"\" style=\"width:100%;height:78px;object-fit:cover;border-radius:8px\" onerror=\"this.outerHTML='" + fallback + "'\">"
      : "<div style=\"font-size:40px;height:78px;display:grid;place-items:center\">" + em + "</div>";
    return "<div class=\"gm-card\" style=\"animation-delay:" + (0.15 + k * 0.25) + "s\">" +
      (c.is_new ? "<span class=\"gm-new\">NEW</span>" : "<span class=\"gm-qty\">×" + c.quantity + "</span>") +
      img + "<b>" + esc((c.name_zh || "").replace(/（.*?）/, "")) + "</b></div>";
  }

  function render(box, gm, opts) {
    if (!box) return;
    var kind = (opts && opts.kind) || "exam";
    if (!gm || (!gm.xp && gm.reason !== "too_short")) { box.innerHTML = ""; return; }
    var cards = (gm.cards || []).map(cardHtml).join("");
    var synth = (gm.new_formulas || []).map(function (f) {
      return "<div class=\"gm-synth\"><span>✨ 合成成功：<b>" + esc(f.name_zh) + "</b></span><button type=\"button\" data-open=\"formula\">看方劑</button></div>";
    }).join("") + (gm.new_recipes || []).map(function (r) {
      return "<div class=\"gm-synth\"><span>✨ 解鎖茶飲：<b>" + esc(r.name_zh) + "</b></span><button type=\"button\" data-open=\"recipe\">看圖鑑</button></div>";
    }).join("");
    var note = noteFor(gm, kind);
    var head = gm.xp
      ? "<div style=\"font-weight:700;font-size:15px\">遊戲獎勵　+" + gm.xp.gained + " XP</div><div style=\"font-size:12.5px;color:#6B5E4B\">Lv" + gm.xp.level + " " + esc(gm.xp.level_name) + " ・ 連續 " + (gm.streak_days || 0) + " 天" + (gm.xp.leveled_up ? " ・ ⬆ 升級！" : "") + "</div>"
      : "<div style=\"font-weight:700;font-size:15px\">遊戲獎勵</div>";
    box.innerHTML = "<style>" + CSS + "</style><div class=\"gm-wrap\"><div class=\"gm-row\"><img src=\"/assets/mascot/" + ((gm.cards || []).length ? "celebrate" : "happy") + ".webp\" alt=\"\"><div>" + head + "</div></div>" +
      (cards ? "<div class=\"gm-cards\">" + cards + "</div>" : "") + synth + (note ? "<div class=\"gm-note\">" + esc(note) + "</div>" : "") +
      "<button type=\"button\" class=\"gm-open\" data-open=\"\">看我的藥材圖鑑</button></div>";
    box.querySelectorAll("[data-open]").forEach(function (b) { b.onclick = function () { openCodex(b.getAttribute("data-open")); }; });
  }
  window.GmRewards = { render: render, openCodex: openCodex };
})();
