import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const sourcePath = new URL('../../src/audio_hub/web/static/re0_refill.user.js', import.meta.url);
const TODAY = new Date().toISOString().slice(0, 10);

function loadScript(options = {}) {
  const {
    html = '', text = '', values = {}, responses = [], href = 'https://re0.me/',
    buttons = [], immediateTimers = false, onTimer = null,
  } = options;
  const source = readFileSync(sourcePath, 'utf8');
  const instrumented = source.replace(
    /\}\)\(\);\s*$/,
    'globalThis.__review = { buildPanel, main, submitLinks, onPageLoad, failTask, pause, sessionPageCount, reservePage };})();',
  );
  assert.notEqual(instrumented, source);
  const calls = [];
  const storage = new Map(Object.entries(values));
  function makeNode(tagName) {
    return {
      tagName,
      children: [],
      id: '',
      className: '',
      textContent: '',
      value: '',
      clicked: 0,
      listeners: {},
      classList: { toggle() {}, add() {} },
      appendChild(child) {
        if (typeof child === 'string') throw new TypeError('appendChild 需要节点');
        this.children.push(child);
        return child;
      },
      replaceChildren() { this.children = []; },
      addEventListener(type, fn) { this.listeners[type] = fn; },
      click() { this.clicked += 1; return this.listeners.click?.({ target: this }); },
    };
  }
  const body = makeNode('body');
  body.textContent = text;
  function findById(node, id) {
    if (node.id === id) return node;
    for (const child of node.children) {
      const found = findById(child, id);
      if (found) return found;
    }
    return null;
  }
  const document = {
    readyState: 'loading',
    documentElement: { innerHTML: html },
    body,
    createElement: makeNode,
    addEventListener() {},
    getElementById(id) { return findById(body, id); },
    querySelector() { return null; },
    querySelectorAll(selector) { return selector === 'button' ? buttons : []; },
  };
  const context = {
    console,
    document,
    location: new URL(href),
    history: { pushState() {} },
    GM_addStyle() {},
    GM_getValue(key, fallback) { return storage.has(key) ? storage.get(key) : fallback; },
    GM_setValue(key, value) { storage.set(key, value); },
    GM_xmlhttpRequest(settings) {
      calls.push(settings);
      const response = responses.shift() || { status: 200, body: { success: true, data: {} } };
      queueMicrotask(() => settings.onload({
        status: response.status,
        responseText: JSON.stringify(response.body),
      }));
    },
    MutationObserver: class { observe() {} disconnect() {} },
    setInterval() { return 1; },
    setTimeout(fn) {
      if (immediateTimers) {
        onTimer?.();
        queueMicrotask(fn);
      }
      return 1;
    },
    clearTimeout() {},
    URL,
    URLSearchParams,
    encodeURIComponent,
    decodeURIComponent,
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(instrumented, context, { filename: 're0_refill.user.js' });
  return { api: context.__review, calls, storage, document };
}

function configured(extra = {}) {
  return {
    audioHubUrl: 'http://localhost:8000',
    refillToken: 'secret-token',
    autoRunning: true,
    ...extra,
  };
}

function resourceTask(extra = {}) {
  return { tmdb_id: 100, title: '测试剧', stage: 'resource', steps: 1, ...extra };
}

function findButton(node, text) {
  if (node.tagName === 'button' && node.textContent === text) return node;
  for (const child of node.children) {
    const found = findButton(child, text);
    if (found) return found;
  }
  return null;
}

test('首次配置无需旧地址即可保存并清空令牌输入框', async () => {
  const { api, document, storage } = loadScript({
    values: {},
    responses: [{ status: 200, body: { success: true, data: { items: [] } } }],
  });
  api.buildPanel();
  document.getElementById('ah-base').value = 'http://localhost:8000';
  const token = document.getElementById('ah-token');
  token.value = 'new-secret-token';
  await findButton(document.body, '保存连接配置').listeners.click();
  assert.equal(storage.get('audioHubUrl'), 'http://localhost:8000');
  assert.equal(storage.get('refillToken'), 'new-secret-token');
  assert.equal(token.value, '');
});

test('更换地址未重新输入令牌时不得发送或覆盖配置', async () => {
  const { api, document, storage, calls } = loadScript({ values: configured() });
  api.buildPanel();
  document.getElementById('ah-base').value = 'http://other-host:9000';
  await findButton(document.body, '保存连接配置').listeners.click();
  assert.equal(storage.get('audioHubUrl'), 'http://localhost:8000');
  assert.equal(storage.get('refillToken'), 'secret-token');
  assert.equal(calls.length, 0);
});

test('已保存令牌不进入 Re0 页面 DOM', () => {
  const { api, document } = loadScript({ values: configured() });
  api.buildPanel();
  const input = document.getElementById('ah-token');
  assert.ok(input);
  assert.equal(input.value, '');
});

test('115 宿主页不挂载配置面板或令牌控件', async () => {
  const { api, document } = loadScript({
    href: 'https://115cdn.com/s/abc123',
    values: configured({ cur: null }),
    responses: [{ status: 200, body: { success: true, data: { items: [] } } }],
  });
  await api.main();
  assert.equal(document.getElementById('ah-panel'), null);
  assert.equal(document.getElementById('ah-token'), null);
});

test('付费页即使含 115 链接也先暂停且不点击解锁', async () => {
  const button = { textContent: '确认解锁', clicked: 0, click() { this.clicked += 1; } };
  const { api, calls, storage } = loadScript({
    href: 'https://re0.me/resource/115/abc',
    html: '<a href="https://115.com/s/abc123">115</a>',
    text: '需要使用 5 积分解锁',
    buttons: [button],
    values: configured({ cur: resourceTask() }),
    responses: [{ status: 200, body: { success: true } }],
  });
  await api.onPageLoad();
  assert.equal(button.clicked, 0);
  assert.equal(storage.get('autoRunning'), false);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'http://localhost:8000/api/lackfill/refill_status');
});

test('同页混合 0 与非 0 积分时不自动提交链接', async () => {
  const { api, calls, storage } = loadScript({
    href: 'https://re0.me/resource/115/abc',
    html: '<a href="https://115.com/s/abc123">115</a>',
    text: '当前需要使用 0 积分；另一资源需要使用 5 积分',
    values: configured({ cur: resourceTask() }),
    responses: [{ status: 200, body: { success: true } }],
  });
  await api.onPageLoad();
  assert.equal(storage.get('autoRunning'), false);
  assert.equal(calls[0].url, 'http://localhost:8000/api/lackfill/refill_status');
});

test('唯一 0 积分资源按钮才可自动解锁并回填', async () => {
  const parent = { textContent: '需要使用 0 积分' };
  const button = {
    textContent: '确认解锁', clicked: 0, parentElement: parent,
    closest() { return parent; },
    click() {
      this.clicked += 1;
      document.documentElement.innerHTML = '<a href="https://115.com/s/abc123">115</a>';
    },
  };
  const { api, calls, document } = loadScript({
    href: 'https://re0.me/resource/115/abc',
    text: parent.textContent,
    buttons: [button],
    immediateTimers: true,
    values: configured({ cur: resourceTask() }),
    responses: [{ status: 200, body: { success: true, data: { accepted: 1 } } }],
  });
  await api.onPageLoad();
  assert.equal(button.clicked, 1);
  assert.equal(calls[0].url, 'http://localhost:8000/api/lackfill/refill_submit');
});

test('部分链接已成功时油猴不整体重试', async () => {
  const { api, storage } = loadScript({
    values: configured({ autoRunning: false }),
    responses: [{ status: 200, body: { success: false, data: { accepted: 1 } } }],
  });
  await api.submitLinks(resourceTask(), ['https://115.com/s/abc123'], 'review');
  assert.equal(storage.get('ah_done_100'), TODAY);
});

test('不同剧连续三次失败也会全局熔断', async () => {
  const { api, storage, calls } = loadScript({ values: configured() });
  await api.failTask(resourceTask({ tmdb_id: 101, title: 'A' }), '失败');
  await api.failTask(resourceTask({ tmdb_id: 102, title: 'B' }), '失败');
  await api.failTask(resourceTask({ tmdb_id: 103, title: 'C' }), '失败');
  assert.equal(storage.get('autoRunning'), false);
  assert.equal(calls.at(-1).url, 'http://localhost:8000/api/lackfill/refill_status');
});

test('会话页数跨天自动重置', () => {
  const { api } = loadScript({ values: { sessionDay: '2000-01-01', sessionPages: 30 } });
  assert.equal(api.sessionPageCount(), 0);
});

test('达到页数预算后当前任务也不能继续导航', () => {
  const { api, storage } = loadScript({ values: configured({ sessionDay: TODAY, sessionPages: 30 }) });
  assert.equal(api.reservePage(), false);
  assert.equal(storage.get('autoRunning'), false);
});

test('Cloudflare 自动消失后无需新导航即可继续当前资源任务', async () => {
  const { api, calls, document } = loadScript({
    href: 'https://re0.me/resource/115/abc',
    text: '正在进行安全验证',
    immediateTimers: true,
    onTimer() {
      document.body.textContent = '';
      document.documentElement.innerHTML = '<a href="https://115.com/s/abc123">115</a>';
    },
    values: configured({ cur: resourceTask() }),
    responses: [{ status: 200, body: { success: true, data: { accepted: 1 } } }],
  });
  await api.onPageLoad();
  assert.equal(calls[0].url, 'http://localhost:8000/api/lackfill/refill_submit');
});

test('直接 115 分享页没有 goto 也能回填', async () => {
  const { api, calls, document } = loadScript({
    href: 'https://115cdn.net/s/abc123?password=wd40',
    values: configured({ cur: resourceTask() }),
    responses: [{ status: 200, body: { success: true, data: { accepted: 1 } } }],
  });
  await api.onPageLoad();
  assert.equal(calls[0].url, 'http://localhost:8000/api/lackfill/refill_submit');
  assert.equal(JSON.parse(calls[0].data).links[0], 'https://115cdn.net/s/abc123?password=wd40');
});

test('直接 ed2k 资源页清理零宽字符并回填到 audio-hub', async () => {
  const raw = 'ed2k://|file|善良\u200b妈妈的朋友 (2017) - 720p.HDRip|4021117121|6A8A258330DB0DA10B020623AE256150|/';
  const { api, calls } = loadScript({
    href: 'https://re0.me/resource/7702b35d-d5be-45d2-b9f1-58e4e2d77dbe',
    html: `<input value="${raw}">`,
    values: configured({ cur: resourceTask() }),
    responses: [{ status: 200, body: { success: true, data: { accepted: 1 } } }],
  });
  await api.onPageLoad();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, 'http://localhost:8000/api/lackfill/refill_submit');
  assert.equal(calls[1].url, 'http://localhost:8000/api/lackfill/refill_status');
  assert.equal(JSON.parse(calls[1].data).status, 'success');
  assert.equal(JSON.parse(calls[0].data).links[0],
    'ed2k://|file|善良妈妈的朋友 (2017) - 720p.HDRip|4021117121|6A8A258330DB0DA10B020623AE256150|/');
});

test('畸形 goto 有界失败并清除当前任务', async () => {
  const { api, storage } = loadScript({
    href: 'https://115.com/?goto=%E0%A4%A',
    values: configured({ cur: resourceTask() }),
  });
  await api.onPageLoad();
  assert.equal(storage.get('cur'), null);
  assert.equal(storage.get('ah_fail_100'), 1);
});

test('待机也上报心跳、脚本版本和当前阶段', async () => {
  const { api, calls } = loadScript({
    values: configured({ autoRunning: false, cur: resourceTask() }),
    responses: [
      { status: 200, body: { success: true, data: { items: [] } } },
      { status: 200, body: { success: true } },
    ],
  });
  await api.main();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].url, 'http://localhost:8000/api/lackfill/refill_status');
  const body = JSON.parse(calls[1].data);
  assert.equal(body.status, 'waiting');
  assert.equal(body.stage, 'resource');
  assert.equal(body.auto_running, false);
  assert.equal(body.script_version, '1.1.0');
});

test('暂停原因在后续待机心跳中保留', async () => {
  const { api, calls } = loadScript({
    values: configured({ autoRunning: false }),
    responses: [
      { status: 200, body: { success: true } },
      { status: 200, body: { success: true, data: { items: [] } } },
      { status: 200, body: { success: true } },
    ],
  });
  await api.pause('需要人工完成 Cloudflare 验证', 'challenge');
  await api.main();
  const body = JSON.parse(calls.at(-1).data);
  assert.equal(body.status, 'challenge');
  assert.match(body.reason, /Cloudflare/);
  assert.equal(body.auto_running, false);
});

test('显式搜索无结果与页面结构失败分开上报', async () => {
  const { api, calls } = loadScript({
    href: 'https://re0.me/search?tab=media&query=100&type=tv_tmdb_id',
    text: '没有找到相关内容',
    immediateTimers: true,
    values: configured({ cur: { tmdb_id: 100, title: '测试剧', stage: 'search', steps: 0 } }),
    responses: [{ status: 200, body: { success: true } }],
  });
  await api.onPageLoad();
  const body = JSON.parse(calls.at(-1).data);
  assert.equal(body.status, 'not_found');
  assert.equal(body.attempts, 1);
});

test('启动自动找会清除旧的暂停状态', async () => {
  const { api, document, storage } = loadScript({
    values: configured({
      autoRunning: false,
      ah_paused_status: 'challenge',
      ah_paused_reason: '需要人工验证',
    }),
    responses: [{ status: 200, body: { success: true, data: { items: [] } } }],
  });
  api.buildPanel();
  await findButton(document.body, '开始自动找').listeners.click();
  assert.equal(storage.get('ah_paused_status'), null);
  assert.equal(storage.get('ah_paused_reason'), null);
  assert.equal(storage.get('autoRunning'), true);
});
