/* audio-hub 前端交互：CSRF 注入、待审队列键盘操作、表单局部提交、局部无感刷新。
   无框架依赖（原生 fetch），与 PRD §12.1 选型一致。

   结构约定：随 DOM 存亡的初始化收进 initPage(root)，首屏与每次局部刷新后各调一次；
   document 级事件委托（CSRF / 防重复提交 / 菜单外点关闭 / 键盘）只绑一次；
   周期任务（进度条 / 日志 tail / 无感刷新）各起一份，tick 时重查当前 DOM，
   局部替换内容后自动续上，不重复起定时器。 */

(function () {
  "use strict";

  const CSRF_META = document.querySelector('meta[name="csrf-token"]');
  const CSRF = CSRF_META ? CSRF_META.content : "";

  /* ── 所有 POST 表单自动带上 CSRF（document 委托，换内容后依然生效）── */
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

  /* ── 待审队列：键盘操作（PRD §12.2 批量审核效率要求）──────
     键盘监听在 document 上只绑一份；游标与条目集合随局部刷新重建。 */
  const review = { items: [], idx: -1 };

  function paintReview() {
    review.items.forEach(function (el, i) { el.classList.toggle("cursor", i === review.idx); });
    if (review.items[review.idx]) review.items[review.idx].scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function moveReview(delta) {
    if (!review.items.length) return;
    review.idx = Math.max(0, Math.min(review.items.length - 1, review.idx + delta));
    paintReview();
  }

  function actReview(kind) {
    if (review.idx < 0 || !review.items[review.idx]) return;
    const el = review.items[review.idx];
    const btn = el.querySelector(kind === "accept" ? "[data-act=accept]" : "[data-act=reject]");
    if (!btn) return;
    if (kind === "reject") {
      const sel = el.querySelector("select[name=reason]");
      if (sel && !sel.value) { flash("请先选择拒绝原因", true); return; }
    }
    el.classList.add("done");
    btn.closest("form").submit();
  }

  document.addEventListener("keydown", function (e) {
    /* 只在待审页（有可审条目）生效，其余页面不拦截 j/k/a/r 与方向键 */
    if (!review.items.length) return;
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (e.key === "j" || e.key === "J" || e.key === "ArrowDown") { e.preventDefault(); moveReview(1); }
    else if (e.key === "k" || e.key === "K" || e.key === "ArrowUp") { e.preventDefault(); moveReview(-1); }
    else if (e.key === "a" || e.key === "A") { e.preventDefault(); actReview("accept"); }
    else if (e.key === "r" || e.key === "R") { e.preventDefault(); actReview("reject"); }
  });

  /* ── 日志行级别判定：与 templates.py 的 loglevel 过滤器同一套规则 ──
     loguru 行格式 `ts | LEVEL | name:func:line | msg`，取第二段；
     解析不出管道结构时按关键词兜底。 */
  function logLevelClass(line) {
    const s = String(line || "");
    const parts = s.split("|");
    if (parts.length >= 3) {
      const lvl = parts[1].trim().toUpperCase();
      if (lvl === "ERROR" || lvl === "CRITICAL") return "log-err";
      if (lvl === "WARNING") return "log-warn";
      if (lvl === "DEBUG" || lvl === "TRACE") return "log-debug";
      if (lvl === "INFO") return "log-info";
    }
    const low = s.toLowerCase();
    if (low.indexOf("error") >= 0) return "log-err";
    if (low.indexOf("warn") >= 0) return "log-warn";
    if (low.indexOf("debug") >= 0 || low.indexOf("trace") >= 0) return "log-debug";
    return "log-info";
  }

  /* ── initPage(root)：随 DOM 存亡的初始化 ─────────────────────
     首屏传 document，局部刷新后传新的主内容区；document 级委托不在其列。 */
  function initPage(root) {
    /* 服务端 flash 消息自动消失 */
    root.querySelectorAll(".flash[data-autohide]").forEach(function (el) {
      setTimeout(function () { el.remove(); }, 4000);
    });

    /* /run-once 等二次确认（会真实触发一轮站点请求） */
    root.querySelectorAll("[data-confirm]").forEach(function (el) {
      el.addEventListener("click", function (e) {
        if (!window.confirm(el.getAttribute("data-confirm"))) e.preventDefault();
      });
    });

    /* 订阅表单：按类型显示 / 隐藏字段（无 JS 时全部可见，不影响提交） */
    root.querySelectorAll("form[data-sub-form]").forEach(function (form) {
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

    /* 待审队列键盘游标：条目集合随 DOM 重建 */
    const list = root.querySelector("#review-list");
    review.items = list ? Array.from(list.querySelectorAll(".review-item:not(.done)")) : [];
    review.idx = review.items.length ? 0 : -1;
    paintReview();

    /* 新内容带下载进度条时立即拉一次，不等下一个 5s 周期 */
    if (root.querySelector(".dlbar-wrap")) pullProgress();
  }

  /* ── 主题切换：icon 随当前主题（亮色显示月亮=切到暗）────
     按钮在 sidebar，局部刷新只换 <main>，绑一次即可。 */
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

  /* ── 行内溢出菜单（details.menu）：document 委托，点外部或点菜单项后收起 ── */
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.menu[open]").forEach(function (d) {
      if (!d.contains(e.target) || e.target.closest("button")) d.removeAttribute("open");
    });
  });

  /* ── 下载进度条（5s 轮询 /api/download-progress）──────────────
     定时器只起一份，tick 时重查当前 DOM —— 局部刷新换上的新进度条自动续上。 */
  setInterval(function () {
    if (document.visibilityState !== "visible") return;
    if (!document.querySelector(".dlbar-wrap")) return;
    pullProgress();
  }, 5000);

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

  async function pullProgress() {
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

  /* ── 局部无感刷新：fetch 当前 URL → 用新文档替换主内容区 ────
     服务端渲染没有常驻连接，下载器状态的时效性靠它兜底。
     整页 location.reload() 会打断点击/阅读并闪白屏，这里只替换
     <main class="main"> 的子节点（h1 / flash / 内容一起换，语义完整），
     滚动位置与输入焦点天然保留。正在输入、有文本选区、展开了
     details / dialog 时绝不打扰；后台标签页跳过本轮顺延，
     从后台切回时若内容已停留超过 15s 立即补刷一次。 */
  const loadedAt = Date.now();
  let lastSwapAt = loadedAt;

  function refreshBusy() {
    const ae = document.activeElement;
    if (ae && ["INPUT", "SELECT", "TEXTAREA"].indexOf(ae.tagName) >= 0) return true;
    if (document.querySelector("details[open], dialog[open]")) return true;
    if (String(window.getSelection())) return true;
    return false;
  }

  async function softRefresh() {
    if (document.hidden || refreshBusy()) return false;
    let doc;
    try {
      const resp = await fetch(location.href, { credentials: "same-origin" });
      if (!resp.ok) return false;
      doc = new DOMParser().parseFromString(await resp.text(), "text/html");
    } catch (_) { return false; }
    const cur = document.querySelector("main.main");
    const next = doc.querySelector("main.main");
    if (!cur || !next) {
      /* 拿回来的不是应用布局（多半是会话过期落到登录页），退回整页跳转 */
      location.reload();
      return true;
    }
    if (refreshBusy()) return false;  /* 拉取期间用户开始了操作，放弃本轮 */
    document.title = doc.title;
    cur.innerHTML = next.innerHTML;
    initPage(cur);
    lastSwapAt = Date.now();
    return true;
  }

  const refreshSecs = parseInt(document.body.dataset.autorefresh || "0", 10);
  if (refreshSecs) setInterval(softRefresh, refreshSecs * 1000);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible"
        && Date.now() - Math.max(loadedAt, lastSwapAt) > 15000) {
      softRefresh();
    }
  });

  /* ── 日志页实时跟随：每 3s 拉 /api/logs/tail，按级别着色重绘 ──
     同样只起一份定时器；logbox 随局部刷新换新，tick 时重新查找。
     行内容用 textContent 写入，日志里若有 HTML 字样也不被执行。 */
  setInterval(function () {
    if (document.visibilityState !== "visible") return;
    const box = document.getElementById("logbox");
    const followEl = document.getElementById("follow");
    if (!box || !followEl || !followEl.checked) return;
    pullLogs(box);
  }, 3000);

  async function pullLogs(box) {
    try {
      const params = new URLSearchParams({ n: "400" });
      const lvl = new URLSearchParams(location.search).get("level");
      if (lvl) params.set("level", lvl);
      const resp = await fetch("/api/logs/tail?" + params);
      if (!resp.ok) return;
      const data = await resp.json();
      if (!data.lines || !data.lines.length) return;
      const statusEl = document.getElementById("log-status");
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
      box.textContent = "";
      data.lines.forEach(function (ln) {
        const span = document.createElement("span");
        span.className = logLevelClass(ln);
        span.textContent = ln;
        box.appendChild(span);
        box.appendChild(document.createTextNode("\n"));
      });
      if (statusEl) statusEl.textContent = data.count + " 行 · 更新于 " + new Date().toLocaleTimeString();
      if (atBottom) box.scrollTop = box.scrollHeight;
    } catch (_) { /* 网络抖动下轮再取 */ }
  }

  /* ── 首屏初始化 ─────────────────────────────────────────── */
  initPage(document);
})();
