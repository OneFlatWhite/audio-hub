/* audio-hub 前端交互：CSRF 注入、待审队列键盘操作、表单局部提交。
   无框架依赖（原生 fetch），与 PRD §12.1 选型一致。 */

(function () {
  "use strict";

  const CSRF_META = document.querySelector('meta[name="csrf-token"]');
  const CSRF = CSRF_META ? CSRF_META.content : "";

  /* ── 所有 POST 表单自动带上 CSRF ─────────────────────────── */
  function wantsCsrf(form) {
    /* GET 表单的字段会序列化进 URL——注入 token 会把它泄进浏览器历史和服务端日志 */
    const m = (form.getAttribute("method") || "get").toLowerCase();
    return m !== "get";
  }

  function ensureCsrfField(form) {
    if (!CSRF || !wantsCsrf(form)) return;
    if (form.querySelector('input[name="csrf_token"]')) return;
    const el = document.createElement("input");
    el.type = "hidden";
    el.name = "csrf_token";
    el.value = CSRF;
    form.appendChild(el);
  }

  document.addEventListener("submit", function (e) {
    const form = e.target;
    if (form && form.tagName === "FORM") ensureCsrfField(form);
  });

  /* 按钮点击也补 CSRF（部分按钮在 form 外，用 formaction 提交） */
  document.addEventListener("click", function (e) {
    const btn = e.target.closest("button");
    if (!btn) return;
    const form = btn.form || (btn.getAttribute("form") && document.getElementById(btn.getAttribute("form")));
    if (form) ensureCsrfField(form);
  });

  /* ── 通用：提交后禁用按钮，防重复点击 ───────────────────── */
  document.addEventListener("submit", function (e) {
    const form = e.target;
    if (!form || form.tagName !== "FORM") return;
    if (form.dataset.noDisable === "1") return;
    form.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
    /* 若被服务端拒绝（如 CSRF 失效），恢复可点击 */
    setTimeout(function () {
      form.querySelectorAll("button").forEach(function (b) { b.disabled = false; });
    }, 8000);
  });

  /* ── 待审队列：键盘操作（PRD §12.2 批量审核效率要求）────── */
  const reviewList = document.getElementById("review-list");
  if (reviewList) {
    let items = Array.from(reviewList.querySelectorAll(".review-item:not(.done)"));
    let idx = items.length ? 0 : -1;

    function paint() {
      items.forEach(function (el, i) { el.classList.toggle("cursor", i === idx); });
      if (items[idx]) items[idx].scrollIntoView({ block: "nearest", behavior: "smooth" });
    }

    function move(delta) {
      if (!items.length) return;
      idx = Math.max(0, Math.min(items.length - 1, idx + delta));
      paint();
    }

    function act(kind) {
      if (idx < 0 || !items[idx]) return;
      const el = items[idx];
      const btn = el.querySelector(kind === "accept" ? "[data-act=accept]" : "[data-act=reject]");
      if (!btn) return;
      if (kind === "reject") {
        const sel = el.querySelector("select[name=reason]");
        if (sel && !sel.value) { sel.focus(); flash("请先选择拒绝原因", true); return; }
      }
      el.classList.add("done");
      btn.closest("form").submit();
    }

    document.addEventListener("keydown", function (e) {
      const tag = (e.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      if (e.key === "j" || e.key === "J" || e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "k" || e.key === "K" || e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "a" || e.key === "A") { e.preventDefault(); act("accept"); }
      else if (e.key === "r" || e.key === "R") { e.preventDefault(); act("reject"); }
    });

    paint();
  }

  /* ── 轻提示 ──────────────────────────────────────────────── */
  function flash(msg, isErr) {
    const box = document.createElement("div");
    box.className = "flash " + (isErr ? "err" : "ok");
    box.textContent = msg;
    box.style.position = "fixed";
    box.style.top = "16px";
    box.style.right = "16px";
    box.style.zIndex = "999";
    box.style.maxWidth = "340px";
    document.body.appendChild(box);
    setTimeout(function () { box.remove(); }, 2600);
  }
  window.ahFlash = flash;

  /* ── 服务端 flash 消息自动消失 ───────────────────────────── */
  document.querySelectorAll(".flash[data-autohide]").forEach(function (el) {
    setTimeout(function () { el.remove(); }, 4000);
  });

  /* ── 命中记录：下载进度条（5s 轮询 /api/download-progress）──── */
  (function () {
    const rows = document.querySelectorAll(".dlbar-wrap");
    if (!rows.length) return;

    function fmtSpeed(bps) {
      if (!bps || bps <= 0) return "";
      if (bps >= 1048576) return (bps / 1048576).toFixed(1) + " MB/s";
      return Math.max(1, Math.round(bps / 1024)) + " KB/s";
    }
    function fmtEta(s) {
      if (s === null || s === undefined || s < 0) return "";
      if (s >= 86400) return Math.round(s / 86400) + " 天";
      if (s >= 3600) return Math.round(s / 3600) + " 小时";
      if (s >= 60) return Math.round(s / 60) + " 分钟";
      return s + " 秒";
    }

    async function pull() {
      try {
        const resp = await fetch("/api/download-progress");
        if (!resp.ok) return;
        const data = await resp.json();
        document.querySelectorAll(".dlbar-wrap").forEach(function (el) {
          const d = data[el.dataset.hash];
          if (!d) return;
          const pct = Math.max(0, Math.min(100, d.pct || 0));
          el.querySelector(".seedbar > span").style.width = pct + "%";
          el.querySelector(".seedbar").classList.toggle("done", pct >= 100);
          const bits = [pct + "%"];
          const sp = fmtSpeed(d.dlspeed);
          if (sp && pct < 100) bits.push(sp);
          const eta = fmtEta(d.eta);
          if (eta && pct < 100) bits.push(eta + (sp ? "" : "后完成"));
          el.querySelector(".dlmeta").textContent = bits.join(" · ");
        });
      } catch (_) { /* 下轮再取 */ }
    }
    pull();
    setInterval(function () {
      if (document.visibilityState === "visible") pull();
    }, 5000);
  })();

  /* ── 页面自动刷新：页面可见时周期整页重载 ──────────────────
     服务端渲染没有常驻连接，下载器状态的时效性靠它兜底。
     正在输入（表单聚焦）/ 菜单展开 / 订阅表单展开时绝不打扰；
     从后台切回时若页面已停留超过 15s 立即刷新一次。 */
  (function () {
    const secs = parseInt(document.body.dataset.autorefresh || "0", 10);
    if (!secs) return;
    const loadedAt = Date.now();
    const busy = function () {
      const ae = document.activeElement;
      if (ae && ["INPUT", "SELECT", "TEXTAREA"].indexOf(ae.tagName) >= 0) return true;
      return !!document.querySelector("details.menu[open], details.newsub[open]");
    };
    setInterval(function () {
      if (document.visibilityState === "visible" && !busy()) location.reload();
    }, secs * 1000);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible" && Date.now() - loadedAt > 15000 && !busy()) {
        location.reload();
      }
    });
  })();

  /* ── 日志页实时跟随：每 3s 拉 /api/logs/tail 重绘 ─────────── */
  (function () {
    const box = document.getElementById("logbox");
    if (!box) return;
    const followEl = document.getElementById("follow");
    const statusEl = document.getElementById("log-status");

    async function pull() {
      try {
        const params = new URLSearchParams({ n: "400" });
        const lvl = new URLSearchParams(location.search).get("level");
        if (lvl) params.set("level", lvl);
        const resp = await fetch("/api/logs/tail?" + params);
        if (!resp.ok) return;
        const data = await resp.json();
        if (!data.lines || !data.lines.length) return;
        const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
        box.textContent = data.lines.join("\n") + "\n";
        if (statusEl) statusEl.textContent = data.count + " 行 · 更新于 " + new Date().toLocaleTimeString();
        if (atBottom) box.scrollTop = box.scrollHeight;
      } catch (_) { /* 网络抖动下轮再取 */ }
    }

    setInterval(function () {
      if (document.visibilityState === "visible" && followEl && followEl.checked) pull();
    }, 3000);
  })();

  /* ── 明暗主题切换：icon 随当前主题（亮色显示月亮=切到暗）──── */
  const themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) {
    const setIcon = function () {
      themeBtn.textContent = document.documentElement.getAttribute("data-theme") === "light" ? "🌙" : "☀️";
    };
    setIcon();
    themeBtn.addEventListener("click", function () {
      const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("ah-theme", next); } catch (_) { /* 隐私模式忽略 */ }
      setIcon();
    });
  }

  /* ── 行内溢出菜单（details.menu）：点外部关闭、选中动作后收起 ── */
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.menu[open]").forEach(function (d) {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
  });
  document.querySelectorAll("details.menu").forEach(function (d) {
    d.addEventListener("click", function (e) {
      if (e.target.closest("button")) d.removeAttribute("open");
    });
  });

  /* ── /run-once 二次确认（会真实触发一轮站点请求）──────────── */
  document.querySelectorAll("[data-confirm]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      if (!window.confirm(el.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  /* ── 订阅表单：按类型显示 / 隐藏字段（无 JS 时全部可见，不影响提交）── */
  document.querySelectorAll("form[data-sub-form]").forEach(function (form) {
    const typeSel = form.querySelector("select[name=sub_type]");
    if (!typeSel) return;
    let labels = {};
    try { labels = JSON.parse(form.dataset.queryLabels || "{}"); } catch (_) { /* 用默认文案 */ }
    const queryLabel = form.querySelector("[data-query-label]");

    function apply() {
      const t = typeSel.value;
      form.querySelectorAll("[data-for]").forEach(function (el) {
        const show = el.dataset.for.split(/\s+/).indexOf(t) >= 0;
        el.hidden = !show;
      });
      if (queryLabel && labels[t]) queryLabel.textContent = labels[t];
    }
    typeSel.addEventListener("change", apply);
    apply();
  });
})();
