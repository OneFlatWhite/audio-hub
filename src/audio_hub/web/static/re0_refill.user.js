// ==UserScript==
// @name         audio-hub Re0 缺集回填
// @namespace    https://github.com/OneFlatWhite/audio-hub
// @version      1.2.0
// @description  从 audio-hub 缺集队列获取任务，在 Re0 搜索并回填 115 分享链接或 ed2k 离线指令；每周自动巡检账户页积分并回传周报。无需 MoviePilot。
// @author       OneFlatWhite
// @match        *://re0.me/*
// @match        *://www.re0.me/*
// @match        *://115.com/*
// @match        *://www.115.com/*
// @match        *://115cdn.com/*
// @match        *://www.115cdn.com/*
// @match        *://115cdn.net/*
// @match        *://www.115cdn.net/*
// @match        *://anxia.com/*
// @match        *://www.anxia.com/*
// @connect      *
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_addStyle
// @run-at       document-start
// @noframes
// ==/UserScript==

(function () {
  'use strict';

  const POLL_MS = 60000;
  const SEARCH_WAIT_MS = 3000;
  const MAX_LINKS = 3;
  const MAX_DAILY_TASKS = 20;
  const MAX_SESSION_PAGES = 30;
  const MAX_TASK_STEPS = 8;
  const MAX_TASK_FAILURES = 3;
  const POINTS_WEEK_MS = 7 * 24 * 3600 * 1000;
  const POINTS_BACKOFF_MS = 24 * 3600 * 1000;
  const ACCOUNT_URL = 'https://re0.me/manager/account';
  const API_QUEUE = '/api/lackfill/refill_queue';
  const API_SUBMIT = '/api/lackfill/refill_submit';
  const API_STATUS = '/api/lackfill/refill_status';
  const API_POINTS = '/api/lackfill/re0_points';

  let queue = [];
  let autoRunning = GM_getValue('autoRunning', false);
  let busy = false;

  function todayStr() { return new Date().toISOString().slice(0, 10); }
  function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }
  function saveCur(cur) { GM_setValue('cur', cur); }
  function getCur() { return GM_getValue('cur', null); }
  function clearCur() { GM_setValue('cur', null); }
  function doneKey(task) { return 'ah_done_' + task.tmdb_id; }
  function isDone(task) { return GM_getValue(doneKey(task), '') === todayStr(); }

  function normalizeBaseUrl(value) {
    const url = new URL(String(value || '').trim());
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
      throw new Error('audio-hub 地址必须是完整的 HTTP(S) 地址');
    }
    return (url.origin + url.pathname).replace(/\/+$/, '');
  }

  function getConfig() {
    const base = normalizeBaseUrl(GM_getValue('audioHubUrl', ''));
    const token = String(GM_getValue('refillToken', '') || '').trim();
    if (!base || !token) throw new Error('请先在面板填写 audio-hub 地址和回填令牌');
    return { base, token };
  }

  function request(path, options = {}) {
    const { base, token } = getConfig();
    const method = options.method || 'GET';
    const headers = { 'X-AudioHub-Refill-Key': token };
    const settings = {
      method,
      url: base + path,
      headers,
      timeout: method === 'POST' ? 60000 : 20000,
      onload(response) {
        let payload;
        try { payload = JSON.parse(response.responseText); } catch (e) {
          settings.reject?.(new Error('audio-hub 返回的不是 JSON'));
          return;
        }
        if (response.status < 200 || response.status >= 300) {
          settings.reject?.(new Error(payload.detail || `audio-hub HTTP ${response.status}`));
          return;
        }
        settings.resolve?.(payload);
      },
      onerror() { settings.reject?.(new Error('无法连接 audio-hub')); },
      ontimeout() { settings.reject?.(new Error('audio-hub 请求超时')); },
    };
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      settings.data = JSON.stringify(options.body);
    }
    return new Promise((resolve, reject) => {
      settings.resolve = resolve;
      settings.reject = reject;
      GM_xmlhttpRequest(settings);
    });
  }

  function log(message) {
    const el = document.getElementById('ah-log');
    if (!el) return;
    const line = `[${new Date().toTimeString().slice(0, 8)}] ${message}`;
    el.textContent = (line + '\n' + el.textContent).split('\n').slice(0, 80).join('\n');
  }

  function dailyCount() {
    if (GM_getValue('ah_daily_day', '') !== todayStr()) return 0;
    return GM_getValue('ah_daily_count', 0);
  }

  function sessionPageCount() {
    if (GM_getValue('sessionDay', '') !== todayStr()) {
      GM_setValue('sessionDay', todayStr());
      GM_setValue('sessionPages', 0);
    }
    return GM_getValue('sessionPages', 0);
  }

  function reservePage() {
    const pages = sessionPageCount();
    if (pages >= MAX_SESSION_PAGES) {
      void pause('已达到本次会话页数上限，请人工重置后再继续', 'quota');
      return false;
    }
    GM_setValue('sessionPages', pages + 1);
    return true;
  }

  function markDone(task) {
    if (isDone(task)) return;
    GM_setValue(doneKey(task), todayStr());
    GM_setValue('ah_daily_day', todayStr());
    GM_setValue('ah_daily_count', dailyCount() + 1);
  }

  function failCount(task) {
    if (GM_getValue('ah_failday_' + task.tmdb_id, '') !== todayStr()) return 0;
    return GM_getValue('ah_fail_' + task.tmdb_id, 0);
  }

  function addFail(task) {
    if (GM_getValue('ah_failday_' + task.tmdb_id, '') !== todayStr()) {
      GM_setValue('ah_failday_' + task.tmdb_id, todayStr());
      GM_setValue('ah_fail_' + task.tmdb_id, 0);
    }
    GM_setValue('ah_fail_' + task.tmdb_id, failCount(task) + 1);
  }

  function randomDelay() {
    let u = 0;
    let v = 0;
    while (u === 0) u = Math.random();
    while (v === 0) v = Math.random();
    const normal = Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
    return Math.round(Math.min(18000, Math.max(6000, 11000 + normal * 3000)));
  }

  function isChallenge() {
    const text = document.body?.textContent || '';
    return text.includes('正在进行安全验证')
      || text.includes('正在验证您不是自动程序')
      || /Cloudflare[\s\S]*Ray ID/i.test(text);
  }

  // ── 每周积分巡检：/manager/account 快照解析与上报 ──────────────

  function readPointsPage() {
    const clean = String(document.body?.textContent || '').replace(/\u00a0/g, ' ');
    const num = (value) => (value == null ? null : Number(String(value).replace(/,/g, '')));
    let match = clean.match(/([0-9][0-9,]*)\s*可用积分/);
    const balance = match ? num(match[1]) : null;
    match = clean.match(/未使用完累积额度[：:]\s*([0-9][0-9,]*)/);
    const rollover = match ? num(match[1]) : null;
    match = clean.match(/([0-9]{1,3})\s*%\s*每周免费额度/);
    const weekly_used_percent = match ? Number(match[1]) : null;
    let weekly_quota_raw = '';
    const anchor = clean.indexOf('每周免费额度');
    if (anchor >= 0) {
      const seg = clean.slice(anchor, anchor + 120).match(/([0-9][0-9,]*)\s*\/\s*([0-9][0-9,]*)/);
      if (seg) weekly_quota_raw = `${seg[1]} / ${seg[2]}`;
    }
    const vip = /长期\s*VIP/.test(clean) ? '长期VIP' : '';
    return { balance, rollover, weekly_used_percent, weekly_quota_raw, vip };
  }

  function pointsDue() {
    return Date.now() - Number(GM_getValue('ah_points_last_at', 0)) >= POINTS_WEEK_MS;
  }

  function maybeStartPointsCheck() {
    if (busy || getCur()) return; // 绝不打断在途任务
    if (!isRe0Host(location.hostname) || location.pathname === '/manager/account') return;
    const pending = Boolean(GM_getValue('ah_points_pending', false));
    const due = pointsDue();
    if (!pending && !due) return;
    if (pending && !due && Number(GM_getValue('ah_points_attempts', 0)) >= 3) return; // 退避中
    GM_setValue('ah_points_pending', true);
    log('开始每周积分巡检（/manager/account）');
    location.href = ACCOUNT_URL;
  }

  async function maybeReportPoints() {
    if (!GM_getValue('ah_points_pending', false)) return; // 用户自己浏览账户页：不动作
    if (isChallenge() && !(await waitForChallenge())) return; // 等人工过盾，pending 保留
    try {
      const points = readPointsPage();
      if (points.balance === null) throw new Error('积分页解析失败，页面结构可能变化');
      await request(API_POINTS, { method: 'POST', body: points });
      GM_setValue('ah_points_pending', false);
      GM_setValue('ah_points_attempts', 0);
      GM_setValue('ah_points_last_at', Date.now());
      GM_setValue('ah_points_last_text',
        `可用 ${points.balance}`
        + (points.weekly_quota_raw ? ` · 每周 ${points.weekly_quota_raw}` : '')
        + (points.weekly_used_percent != null ? ` · 已用 ${points.weekly_used_percent}%` : '')
        + (points.rollover != null ? ` · 结转 ${points.rollover}` : ''));
      log(`积分上报成功：可用 ${points.balance}`);
    } catch (e) {
      const attempts = Number(GM_getValue('ah_points_attempts', 0)) + 1;
      GM_setValue('ah_points_attempts', attempts);
      log(`积分上报失败（第 ${attempts} 次）：${e.message}`);
      if (attempts >= 3) {
        // 连败退避：清 pending，24h 后重新到期再试
        GM_setValue('ah_points_pending', false);
        GM_setValue('ah_points_attempts', 0);
        GM_setValue('ah_points_last_at', Date.now() - POINTS_WEEK_MS + POINTS_BACKOFF_MS);
      }
    }
    location.href = 'https://re0.me/';
  }

  function requiredPointsList(root = document.body) {
    const text = root?.textContent || '';
    return [...text.matchAll(/需要使用\s*(\d+)\s*积分/g)]
      .map(match => Number.parseInt(match[1], 10));
  }

  function requiredPoints(root = document.body) {
    return requiredPointsList(root)[0] ?? null;
  }

  function hasPaidPoints() {
    return requiredPointsList().some(points => points > 0);
  }

  function zeroCostUnlockButton() {
    if (hasPaidPoints() || requiredPointsList().length !== 1) return null;
    const buttons = [...document.querySelectorAll('button')]
      .filter(el => (el.textContent || '').includes('解锁'));
    if (buttons.length !== 1) return null;
    const button = buttons[0];
    let scope = button.closest?.('div[role="dialog"],.modal,.modal-content,[data-resource],article,section')
      || button.parentElement;
    for (let depth = 0; scope && depth < 6; depth += 1) {
      if (requiredPoints(scope) === 0) return button;
      scope = scope.parentElement;
    }
    return null;
  }

  function cleanResourceUrl(value) {
    return String(value || '').replace(/[\u00ad\u180e\u200b-\u200d\u2060\ufeff]/g, '')
      .replace(/\s+/g, ' ').trim();
  }

  function harvest115() {
    let html = document.documentElement?.innerHTML || '';
    try { html = decodeURIComponent(html); } catch (e) { /* 保留原始 HTML */ }
    html = html.replace(/&amp;/g, '&').replace(/\\u002f/gi, '/').replace(/\\\//g, '/');
    const found = new Set();
    const links = [];
    let match;
    const rx = /https?:\/\/(?:www\.)?(?:115\.com|115cdn\.com|115cdn\.net|anxia\.com)\/s\/[A-Za-z0-9]+(?:\?password=[A-Za-z0-9]{1,32}|#[A-Za-z0-9]{1,32})?/g;
    while ((match = rx.exec(html)) !== null && links.length < MAX_LINKS) {
      const url = cleanResourceUrl(match[0]);
      const parts = url.match(/\/s\/([A-Za-z0-9]+)(?:\?password=([A-Za-z0-9]+)|#([A-Za-z0-9]+))?/);
      const identity = `share:${parts ? `${parts[1]}|${parts[2] || parts[3] || ''}` : url}`;
      if (!found.has(identity)) {
        found.add(identity);
        links.push(url);
      }
    }
    const ed2kRx = /ed2k:\/\/\|file\|[^<>]*?\|\d+\|[0-9a-f]{32}/gi;
    while ((match = ed2kRx.exec(html)) !== null && links.length < MAX_LINKS) {
      const url = cleanResourceUrl(match[0]) + '|/';
      const hash = (url.match(/\|([0-9a-f]{32})\|\/$/i) || [])[1] || url;
      const identity = `ed2k:${hash.toUpperCase()}`;
      if (!found.has(identity)) {
        found.add(identity);
        links.push(url);
      }
    }
    return links;
  }

  async function harvestWithRetry() {
    for (let i = 0; i < 6; i += 1) {
      const links = harvest115();
      if (links.length) return links;
      await sleep(2500);
    }
    return [];
  }

  function searchUrl(task) {
    return 'https://re0.me/search?tab=media&query='
      + encodeURIComponent(task.tmdb_id) + '&type=tv_tmdb_id';
  }

  async function reportStatus(status, reason, task = null) {
    return request(API_STATUS, {
      method: 'POST',
      body: {
        status,
        tmdb_id: task?.tmdb_id || null,
        title: task?.title || '',
        reason: String(reason || '').slice(0, 500),
        stage: task?.stage || '',
        attempts: task ? failCount(task) : 0,
        auto_running: autoRunning,
        script_version: '1.2.0',
        queue_count: queue.length,
      },
    });
  }

  async function pause(reason, status) {
    GM_setValue('ah_paused_status', status);
    GM_setValue('ah_paused_reason', String(reason || '').slice(0, 500));
    autoRunning = false;
    GM_setValue('autoRunning', false);
    log(`已暂停：${reason}`);
    updateBadge();
    try { await reportStatus(status, reason, getCur()); } catch (e) {
      log(`状态上报失败：${e.message}`);
    }
  }

  async function submitLinks(task, links, source) {
    const response = await request(API_SUBMIT, {
      method: 'POST',
      body: {
        tmdb_id: task.tmdb_id,
        title: task.title,
        links,
        source: source || 'audio_hub_re0_userscript',
      },
    });
    const completed = response.success
      || (response.data?.accepted || 0) > 0
      || (response.data?.duplicates || 0) > 0;
    if (!completed) throw new Error(response.message || 'audio-hub 未接受链接');
    GM_setValue('ah_consecutive_failures', 0);
    markDone(task);
    queue = queue.filter(item => item.tmdb_id !== task.tmdb_id);
    updateBadge();
    log(`已回填「${task.title}」：${links.length} 条链接`);
    return response.data;
  }

  function waitForEl(getEl, timeoutMs) {
    return new Promise(resolve => {
      const existing = getEl();
      if (existing) { resolve(existing); return; }
      const observer = new MutationObserver(() => {
        const el = getEl();
        if (el) { observer.disconnect(); resolve(el); }
      });
      observer.observe(document.documentElement, { childList: true, subtree: true });
      setTimeout(() => { observer.disconnect(); resolve(null); }, timeoutMs);
    });
  }

  function absoluteUrl(href) {
    return new URL(href, location.origin).href;
  }

  function startTask(task, manual = false) {
    if (!reservePage()) return false;
    saveCur({
      tmdb_id: task.tmdb_id,
      title: task.title,
      year: task.year || '',
      missing: task.missing,
      stage: 'search',
      steps: 0,
      manual,
    });
    log(`开始寻找「${task.title}」（缺 ${task.missing} 集）`);
    location.href = searchUrl(task);
    return true;
  }

  function nextTask() {
    return queue.find(task => !isDone(task) && failCount(task) < MAX_TASK_FAILURES) || null;
  }

  function pump() {
    if (!autoRunning || busy || getCur()) return;
    if (dailyCount() >= MAX_DAILY_TASKS || sessionPageCount() >= MAX_SESSION_PAGES) {
      void pause('已达到每日任务或本次会话页数上限', 'quota');
      return;
    }
    const task = nextTask();
    if (!task) { updateBadge(); return; }
    busy = true;
    if (!startTask(task)) busy = false;
  }

  async function failTask(cur, reason, status = 'failed') {
    addFail(cur);
    const consecutive = GM_getValue('ah_consecutive_failures', 0) + 1;
    GM_setValue('ah_consecutive_failures', consecutive);
    log(`「${cur.title}」失败：${reason}`);
    try { await reportStatus(status, reason, cur); } catch (e) {
      log(`任务状态上报失败：${e.message}`);
    }
    clearCur();
    busy = false;
    if (consecutive >= MAX_TASK_FAILURES || failCount(cur) >= MAX_TASK_FAILURES) {
      await pause(`连续 ${consecutive} 次任务失败，已全局熔断`, 'failed');
      return;
    }
    if (autoRunning) setTimeout(pump, randomDelay());
  }

  async function finishTask(cur, links, source) {
    await submitLinks(cur, links, source);
    try { await reportStatus('success', '链接已提交并等待入库核销', cur); } catch (e) {
      log(`完成状态上报失败：${e.message}`);
    }
    clearCur();
    busy = false;
    if (autoRunning) setTimeout(pump, randomDelay());
  }

  async function waitForChallenge() {
    for (let i = 0; i < 12 && isChallenge(); i += 1) await sleep(5000);
    if (!isChallenge()) return true;
    await pause('Cloudflare 需要人工验证；完成后重新打开自动找', 'challenge');
    return false;
  }

  function isShareHost(hostname) {
    return /^(?:www\.)?(?:115\.com|115cdn\.com|115cdn\.net|anxia\.com)$/.test(hostname);
  }

  function isRe0Host(hostname) {
    return /^(?:www\.)?re0\.me$/.test(hostname);
  }

  async function onPageLoad() {
    const cur = getCur();
    if (isRe0Host(location.hostname) && location.pathname === '/manager/account') {
      await maybeReportPoints();
      return;
    }
    if (!cur || (!autoRunning && !cur.manual)) return;
    if (isChallenge() && !(await waitForChallenge())) return;

    if (isShareHost(location.hostname)) {
      busy = true;
      try {
        let link = null;
        const gotoUrl = new URLSearchParams(location.search).get('goto');
        if (gotoUrl) {
          try {
            const match = decodeURIComponent(gotoUrl).match(/https?:\/\/[^&]+/);
            if (match) link = match[0];
          } catch (e) {
            throw new Error('115 goto 编码无法解析');
          }
        } else if (location.pathname.startsWith('/s/')) {
          link = location.href;
        }
        if (!link) throw new Error('115 页面没有可回填的分享链接');
        await finishTask(cur, [link], 'audio_hub_re0_goto');
      } catch (e) {
        await failTask(cur, e.message);
      } finally {
        busy = false;
      }
      return;
    }
    if (!isRe0Host(location.hostname)) return;

    busy = true;
    try {
      if (cur.stage === 'search' && location.pathname === '/search') {
        const findCard = () => document.querySelector('a[href*="/tv/"]')
          || [...document.querySelectorAll('a')].find(a =>
            (a.textContent || '').includes(cur.title) && a.getAttribute('href'));
        const card = await waitForEl(findCard, 40000);
        if (!card) {
          const text = document.body?.textContent || '';
          if (/没有找到|暂无搜索结果|无搜索结果|没有相关.*(?:结果|资源)|未找到.*(?:结果|资源)|No results found/i.test(text)) {
            await failTask(cur, 'Re0 搜索明确无结果', 'not_found');
            return;
          }
          throw new Error('搜索页面结构变化或等待结果超时');
        }
        if (!reservePage()) return;
        saveCur({ ...cur, stage: 'detail', steps: cur.steps + 1 });
        location.href = absoluteUrl(card.getAttribute('href'));
        return;
      }

      if (cur.stage === 'detail' && location.pathname.startsWith('/tv/')) {
        const findResource = () => document.querySelector('a[href*="/resource/115/"]')
          || document.querySelector('a[href*="/resource/"]');
        let resource = await waitForEl(findResource, 30000);
        if (!resource && window.scrollBy) {
          window.scrollBy({ top: 1000, behavior: 'smooth' });
          resource = await waitForEl(findResource, 15000);
        }
        if (!resource) throw new Error('详情页没有 115 资源入口');
        if (!reservePage()) return;
        saveCur({ ...cur, stage: 'resource', steps: cur.steps + 1 });
        location.href = absoluteUrl(resource.getAttribute('href'));
        return;
      }

      if (cur.stage === 'resource' && location.pathname.startsWith('/resource/')) {
        if (hasPaidPoints()) {
          const points = requiredPointsList().find(value => value > 0);
          await pause(`「${cur.title}」需要 ${points} 积分，请手动处理`, 'manual');
          return;
        }
        let links = await harvestWithRetry();
        if (links.length) { await finishTask(cur, links, 'audio_hub_re0_auto'); return; }
        if (requiredPoints() === 0) {
          const button = zeroCostUnlockButton();
          if (!button) {
            await pause('无法确认唯一的 0 积分资源与按钮，请手动处理', 'manual');
            return;
          }
          if (!reservePage()) return;
          button.click();
          await sleep(4000);
          links = await harvestWithRetry();
          if (links.length) { await finishTask(cur, links, 'audio_hub_re0_free_unlock'); return; }
          if (location.pathname.includes('/resource/115/')) {
            throw new Error('解锁后仍未获得 115 链接');
          }
          return;
        }
        throw new Error('资源页没有 115 链接');
      }

      if (cur.steps < MAX_TASK_STEPS) {
        if (!reservePage()) return;
        saveCur({ ...cur, stage: 'search', steps: cur.steps + 1 });
        location.href = searchUrl(cur);
      } else {
        await failTask(cur, '页面导航步骤超限');
      }
    } catch (e) {
      await failTask(cur, e.message);
    } finally {
      busy = false;
    }
  }

  const CSS = `
    #ah-panel{position:fixed;right:16px;bottom:16px;z-index:999999;width:360px;
      background:#11151c;color:#dfe6ee;border:1px solid #2b3442;border-radius:12px;
      font:12px/1.5 -apple-system,"PingFang SC","Microsoft Yahei",sans-serif;
      box-shadow:0 8px 30px rgba(0,0,0,.45);overflow:hidden}
    #ah-panel button{background:#2f6fed;color:#fff;border:0;border-radius:6px;padding:6px 10px;cursor:pointer}
    #ah-panel button.secondary{background:#3a4657}
    #ah-panel button:disabled{opacity:.5;cursor:not-allowed}
    #ah-panel input{box-sizing:border-box;width:100%;padding:7px;border:1px solid #3a4657;
      border-radius:6px;background:#0c1016;color:#dfe6ee}
    #ah-panel .hd{display:flex;align-items:center;gap:8px;padding:10px;background:#1a2230;cursor:pointer}
    #ah-panel .bd{display:none;padding:10px;max-height:70vh;overflow:auto}
    #ah-panel.open .bd{display:block}
    #ah-panel .row{display:flex;gap:8px;margin:8px 0;flex-wrap:wrap}
    #ah-panel .task{padding:6px 0;border-bottom:1px dashed #2b3442}
    #ah-panel .log{height:140px;overflow:auto;background:#0c1016;border-radius:6px;
      padding:7px;white-space:pre-wrap;word-break:break-all;color:#9fb3c8}
    #ah-panel .muted{color:#9fb3c8}
    #ah-panel .cnt{background:#e5484d;color:#fff;border-radius:9px;padding:0 7px}
  `;

  function element(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function button(text, onClick, secondary = false) {
    const el = element('button', secondary ? 'secondary' : '', text);
    el.type = 'button';
    el.addEventListener('click', onClick);
    return el;
  }

  function updateBadge() {
    const count = document.getElementById('ah-count');
    const state = document.getElementById('ah-state');
    if (count) count.textContent = String(queue.length);
    if (state) state.textContent = autoRunning ? '自动中' : '待机';
    const tasks = document.getElementById('ah-tasks');
    if (!tasks) return;
    tasks.replaceChildren();
    queue.slice(0, 8).forEach(task => {
      const row = element('div', 'task');
      const seasons = Object.entries(task.seasons || {})
        .map(([season, eps]) => `S${season}×${eps.length}`).join(' ');
      row.appendChild(element('span', '', `${task.title}（缺 ${task.missing} 集 ${seasons}） `));
      row.appendChild(button('单找', () => { if (!busy) startTask(task, true); }, true));
      tasks.appendChild(row);
    });
  }

  async function refreshQueue() {
    const response = await request(API_QUEUE);
    if (!response.success) throw new Error(response.message || '队列读取失败');
    queue = response.data.items || [];
    updateBadge();
    log(`audio-hub 队列：${queue.length} 部待找`);
    return queue;
  }

  function buildPanel() {
    if (!isRe0Host(location.hostname) || document.getElementById('ah-panel')) return;
    GM_addStyle(CSS);
    const panel = element('div');
    panel.id = 'ah-panel';
    const header = element('div', 'hd');
    header.appendChild(element('b', '', 'audio-hub · Re0 缺集回填'));
    header.appendChild(element('span', 'cnt', '0')).id = 'ah-count';
    header.appendChild(element('span', '', '待机')).id = 'ah-state';
    header.addEventListener('click', () => panel.classList.toggle('open'));
    const body = element('div', 'bd');
    const actions = element('div', 'row');
    const autoButton = button(autoRunning ? '停止自动找' : '开始自动找', async () => {
      autoRunning = !autoRunning;
      GM_setValue('autoRunning', autoRunning);
      autoButton.textContent = autoRunning ? '停止自动找' : '开始自动找';
      updateBadge();
      if (autoRunning) {
        GM_setValue('ah_paused_status', null);
        GM_setValue('ah_paused_reason', null);
        GM_setValue('ah_consecutive_failures', 0);
        try {
          await refreshQueue();
          await onPageLoad();
          await sendHeartbeat();
          if (autoRunning) pump();
        } catch (e) {
          await pause(e.message, 'failed');
        }
      } else {
        try { await sendHeartbeat(); } catch (e) { log(`状态上报失败：${e.message}`); }
      }
    });
    autoButton.id = 'ah-auto';
    actions.appendChild(autoButton);
    actions.appendChild(button('刷新队列', async () => {
      try { await refreshQueue(); } catch (e) { log(e.message); }
    }, true));
    actions.appendChild(button('重置会话', () => {
      GM_setValue('sessionDay', todayStr());
      GM_setValue('sessionPages', 0);
      GM_setValue('ah_consecutive_failures', 0);
      log('会话页数与连续失败计数已重置；每日任务配额不变');
    }, true));
    actions.appendChild(button('清除当前任务', () => {
      clearCur();
      busy = false;
      log('当前任务已清除，可重新选择');
    }, true));
    actions.appendChild(button('立即查积分', () => {
      if (busy) { log('当前有任务在跑，稍后再查'); return; }
      GM_setValue('ah_points_pending', true);
      location.href = ACCOUNT_URL;
    }, true));
    body.appendChild(actions);

    const setup = element('div');
    const baseInput = element('input');
    baseInput.id = 'ah-base';
    baseInput.placeholder = 'audio-hub 地址，如 http://192.168.1.20:8090';
    baseInput.value = GM_getValue('audioHubUrl', '');
    const tokenInput = element('input');
    tokenInput.id = 'ah-token';
    tokenInput.type = 'password';
    tokenInput.placeholder = GM_getValue('refillToken', '')
      ? '已保存令牌；输入新令牌可替换' : '回填令牌（在 audio-hub 缺集补齐页生成）';
    tokenInput.autocomplete = 'off';
    setup.appendChild(baseInput);
    setup.appendChild(tokenInput);
    setup.appendChild(button('保存连接配置', async () => {
      try {
        const base = normalizeBaseUrl(baseInput.value);
        const previousRaw = GM_getValue('audioHubUrl', '');
        const previousBase = previousRaw ? normalizeBaseUrl(previousRaw) : '';
        const enteredToken = tokenInput.value.trim();
        if (base !== previousBase && !enteredToken) {
          throw new Error('更换 audio-hub 地址时必须重新输入回填令牌');
        }
        const token = enteredToken || String(GM_getValue('refillToken', '') || '').trim();
        if (!token) throw new Error('回填令牌不能为空');
        GM_setValue('audioHubUrl', base);
        GM_setValue('refillToken', token);
        await refreshQueue();
        tokenInput.value = '';
      } catch (e) { log(`连接配置或请求失败：${e.message}`); }
    }));
    body.appendChild(setup);
    body.appendChild(element('div', 'muted', '浏览器需保持 Re0 页面打开；Cloudflare 与付费积分只转人工，不自动绕过。'));
    const pointsInfo = element('div', 'muted');
    pointsInfo.id = 'ah-points';
    const lastAt = Number(GM_getValue('ah_points_last_at', 0));
    const lastText = String(GM_getValue('ah_points_last_text', '') || '');
    pointsInfo.textContent = lastAt && lastText
      ? `积分快照（${new Date(lastAt).toLocaleDateString()}）：${lastText}`
      : '积分快照：尚无上报（每周自动巡检一次）';
    body.appendChild(pointsInfo);
    const taskList = element('div');
    taskList.id = 'ah-tasks';
    body.appendChild(taskList);
    const logBox = element('div', 'log');
    logBox.id = 'ah-log';
    body.appendChild(logBox);
    panel.appendChild(header);
    panel.appendChild(body);
    document.body.appendChild(panel);
    if (!GM_getValue('audioHubUrl', '') || !GM_getValue('refillToken', '')) {
      panel.classList.add('open');
      autoRunning = false;
      GM_setValue('autoRunning', false);
    }
    updateBadge();
  }

  async function sendHeartbeat() {
    const status = autoRunning ? 'running' : (GM_getValue('ah_paused_status', '') || 'waiting');
    const reason = autoRunning
      ? '自动查找运行中'
      : GM_getValue('ah_paused_reason', '等待任务');
    await reportStatus(status, reason, getCur());
  }

  async function main() {
    if (isRe0Host(location.hostname)) buildPanel();
    if (!GM_getValue('audioHubUrl', '') || !GM_getValue('refillToken', '')) return;
    try {
      await refreshQueue();
      await onPageLoad();
      await sendHeartbeat();
      if (autoRunning) pump();
    } catch (e) {
      await pause(e.message, 'failed');
    }
    setInterval(async () => {
      try {
        await refreshQueue();
        await sendHeartbeat();
        if (autoRunning && !getCur()) pump();
      } catch (e) {
        if (autoRunning) await pause(e.message, 'failed');
        else log(`队列或心跳更新失败：${e.message}`);
      }
      maybeStartPointsCheck();
    }, POLL_MS);
  }

  if (document.readyState === 'complete' || document.readyState === 'interactive') main();
  else document.addEventListener('DOMContentLoaded', main);
})();
