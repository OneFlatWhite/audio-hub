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
