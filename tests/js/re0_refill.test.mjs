import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const sourcePath = new URL('../../src/audio_hub/web/static/re0_refill.user.js', import.meta.url);

function loadScript({ html = '', text = '', values = {}, responses = [], href = 'https://re0.me/' } = {}) {
  const source = readFileSync(sourcePath, 'utf8');
  const instrumented = source.replace(
    /\}\)\(\);\s*$/,
    'globalThis.__re0Test = { harvest115, isChallenge, request, submitLinks, pause, requiredPoints, searchUrl, normalizeBaseUrl, onPageLoad, buildPanel, readPointsPage, pointsDue, maybeStartPointsCheck, maybeReportPoints };})();',
  );
  assert.notEqual(instrumented, source, '测试应能注入到油猴 IIFE 末尾');
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
      classList: { toggle() {}, add() {} },
      appendChild(child) {
        if (typeof child === 'string') throw new TypeError('appendChild 需要 DOM 节点');
        this.children.push(child);
        return child;
      },
      replaceChildren() { this.children = []; },
      addEventListener() {},
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
    querySelectorAll() { return []; },
  };
  const context = {
    console,
    document,
    location: new URL(href),
    history: { pushState() {} },
    GM_addStyle() {},
    GM_getValue(key, fallback) { return storage.has(key) ? storage.get(key) : fallback; },
    GM_setValue(key, value) { storage.set(key, value); },
    GM_xmlhttpRequest(options) {
      calls.push(options);
      const respond = responses.shift() || { status: 200, body: { success: true, data: {} } };
      queueMicrotask(() => options.onload({
        status: respond.status,
        responseText: JSON.stringify(respond.body),
      }));
    },
    MutationObserver: class { observe() {} disconnect() {} },
    setInterval() { return 1; },
    setTimeout() { return 1; },
    clearTimeout() {},
    URL,
    URLSearchParams,
    encodeURIComponent,
    decodeURIComponent,
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(instrumented, context, { filename: 're0_refill.user.js' });
  return { api: context.__re0Test, calls, storage, document };
}

test('油猴面板用 DOM 节点挂载任务列表', () => {
  const { api, document } = loadScript();
  assert.doesNotThrow(() => api.buildPanel());
  assert.ok(document.getElementById('ah-tasks'));
});

test('从资源页解码并去重 115 链接，最多三条', () => {
  const html = '{"url":"https:\\/\\/115cdn.com\\/s\\/abc123?password=wd40"}'
    + '<a href="https://115.com/s/abc123?password=wd40">重复</a>'
    + '<a href="https://anxia.com/s/def456#code9">第二条</a>'
    + '<a href="https://115.com/s/xyz789">第三条</a>'
    + '<a href="https://115.com/s/extra4">第四条</a>';
  const { api } = loadScript({ html });
  assert.deepEqual([...api.harvest115()], [
    'https://115cdn.com/s/abc123?password=wd40',
    'https://anxia.com/s/def456#code9',
    'https://115.com/s/xyz789',
  ]);
});

test('识别 Cloudflare 质询而不是把它当搜索结果', () => {
  const { api } = loadScript({ text: 're0.me 正在进行安全验证 Ray ID: abc' });
  assert.equal(api.isChallenge(), true);
});

test('搜索使用 TMDB ID，不拼接 RSS 或 MoviePilot 地址', () => {
  const { api } = loadScript();
  assert.equal(
    api.searchUrl({ tmdb_id: 123 }),
    'https://re0.me/search?tab=media&query=123&type=tv_tmdb_id',
  );
});

test('回填通过 POST JSON 提交，令牌只在请求头', async () => {
  const { api, calls, storage } = loadScript({
    values: {
      audioHubUrl: 'http://192.168.1.20:8090/',
      refillToken: 'secret-token',
      autoRunning: false,
    },
    responses: [{ status: 200, body: { success: true, data: { accepted: 1 } } }],
  });
  await api.submitLinks(
    { tmdb_id: 100, title: '测试剧' },
    ['https://115cdn.com/s/abc123?password=wd40'],
    'node-test',
  );
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'POST');
  assert.equal(calls[0].url, 'http://192.168.1.20:8090/api/lackfill/refill_submit');
  assert.equal(calls[0].headers['X-AudioHub-Refill-Key'], 'secret-token');
  assert.equal(calls[0].url.includes('secret-token'), false);
  const body = JSON.parse(calls[0].data);
  assert.equal(body.tmdb_id, 100);
  assert.deepEqual(body.links, ['https://115cdn.com/s/abc123?password=wd40']);
  assert.equal(body.source, 'node-test');
  assert.equal(storage.get('ah_done_100'), new Date().toISOString().slice(0, 10));
});

test('Cloudflare 质询会停机、保留当前任务并上报 audio-hub', async () => {
  const current = { tmdb_id: 100, title: '测试剧', stage: 'search', steps: 1 };
  const { api, calls, storage } = loadScript({
    values: {
      audioHubUrl: 'http://localhost:8000',
      refillToken: 'secret-token',
      autoRunning: true,
      cur: current,
    },
    responses: [{ status: 200, body: { success: true } }],
  });
  await api.pause('需要人工完成 Cloudflare 验证', 'challenge');
  assert.equal(storage.get('autoRunning'), false);
  assert.deepEqual(storage.get('cur'), current);
  assert.equal(calls[0].method, 'POST');
  assert.equal(calls[0].url, 'http://localhost:8000/api/lackfill/refill_status');
  assert.equal(JSON.parse(calls[0].data).status, 'challenge');
});

test('只把 0 积分识别为可自动解锁', () => {
  assert.equal(loadScript({ text: '需要使用 0 积分解锁' }).api.requiredPoints(), 0);
  assert.equal(loadScript({ text: '需要使用 5 积分解锁' }).api.requiredPoints(), 5);
  assert.equal(loadScript({ text: '普通资源说明' }).api.requiredPoints(), null);
});

test('audio-hub 地址只允许 HTTP(S) 并去掉尾斜杠', () => {
  const { api } = loadScript();
  assert.equal(api.normalizeBaseUrl('http://localhost:8000/'), 'http://localhost:8000');
  assert.throws(() => api.normalizeBaseUrl('javascript:alert(1)'), /地址/);
});

test('手动单找在自动开关关闭时仍会完成资源页回填', async () => {
  const cur = { tmdb_id: 100, title: '测试剧', stage: 'resource', steps: 1, manual: true };
  const { api, calls, storage } = loadScript({
    href: 'https://re0.me/resource/115/abc',
    html: '<a href="https://115cdn.com/s/abc123?password=wd40">115</a>',
    values: {
      audioHubUrl: 'http://localhost:8000',
      refillToken: 'secret-token',
      autoRunning: false,
      cur,
    },
    responses: [{ status: 200, body: { success: true, data: { accepted: 1 } } }],
  });
  await api.onPageLoad();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, 'http://localhost:8000/api/lackfill/refill_submit');
  assert.equal(calls[1].url, 'http://localhost:8000/api/lackfill/refill_status');
  assert.equal(JSON.parse(calls[1].data).status, 'success');
  assert.equal(storage.get('cur'), null);
});

const ACCOUNT_TEXT = '个人中心coldbrewliu积分2944可用积分分享0分享次数注册315天'
  + '2025.12.31会员状态∞长期 VIP0%每周免费额度长期 VIP1400/ 1400'
  + '未使用完累积额度： 1000详细信息用户名yushixin1993@163.com邮箱';

test('积分页解析：余额、结转、每周额度与已用百分比', () => {
  const { api } = loadScript({ href: 'https://re0.me/manager/account', text: ACCOUNT_TEXT });
  const points = JSON.parse(JSON.stringify(api.readPointsPage()));
  assert.deepEqual(points, {
    balance: 2944,
    rollover: 1000,
    weekly_used_percent: 0,
    weekly_quota_raw: '1400 / 1400',
    vip: '长期VIP',
  });
});

test('积分页结构变化时余额解析为 null 而不是猜数', () => {
  const { api } = loadScript({ href: 'https://re0.me/manager/account', text: '页面改版了' });
  const points = api.readPointsPage();
  assert.equal(points.balance, null);
  assert.equal(points.weekly_quota_raw, '');
});

test('每周积分巡检到期且空闲时置 pending 并跳账户页，在途任务绝不打断', () => {
  const weekAgo = Date.now() - 8 * 24 * 3600 * 1000;
  const idle = loadScript({
    href: 'https://re0.me/',
    values: { ah_points_last_at: weekAgo },
  });
  idle.api.maybeStartPointsCheck();
  assert.equal(idle.storage.get('ah_points_pending'), true);

  const busyWithTask = loadScript({
    href: 'https://re0.me/',
    values: { ah_points_last_at: weekAgo, cur: { tmdb_id: 1, stage: 'search', steps: 0 } },
  });
  busyWithTask.api.maybeStartPointsCheck();
  assert.notEqual(busyWithTask.storage.get('ah_points_pending'), true);

  const backedOff = loadScript({
    href: 'https://re0.me/',
    values: { ah_points_last_at: Date.now() - 23 * 3600 * 1000, ah_points_pending: true, ah_points_attempts: 3 },
  });
  backedOff.api.maybeStartPointsCheck();
  assert.equal(backedOff.storage.get('ah_points_pending'), true); // 退避期内不再重试导航
});

test('账户页待上报时回传积分快照、清 pending 并记录快照文案', async () => {
  const { api, calls, storage } = loadScript({
    href: 'https://re0.me/manager/account',
    text: ACCOUNT_TEXT,
    values: {
      audioHubUrl: 'http://localhost:8000',
      refillToken: 'secret-token',
      ah_points_pending: true,
    },
    responses: [{ status: 200, body: { success: true, data: { delta: null, notified: true } } }],
  });
  await api.maybeReportPoints();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'http://localhost:8000/api/lackfill/re0_points');
  assert.deepEqual(JSON.parse(calls[0].data), {
    balance: 2944,
    rollover: 1000,
    weekly_used_percent: 0,
    weekly_quota_raw: '1400 / 1400',
    vip: '长期VIP',
  });
  assert.equal(storage.get('ah_points_pending'), false);
  assert.equal(storage.get('ah_points_attempts'), 0);
  assert.ok(String(storage.get('ah_points_last_text')).includes('可用 2944'));
});

test('积分上报失败累计尝试，3 次后退避 24 小时', async () => {
  const { api, storage } = loadScript({
    href: 'https://re0.me/manager/account',
    text: ACCOUNT_TEXT,
    values: {
      audioHubUrl: 'http://localhost:8000',
      refillToken: 'secret-token',
      ah_points_pending: true,
    },
    responses: [
      { status: 500, body: { detail: 'boom' } },
      { status: 500, body: { detail: 'boom' } },
      { status: 500, body: { detail: 'boom' } },
    ],
  });
  await api.maybeReportPoints();
  await api.maybeReportPoints();
  await api.maybeReportPoints();
  assert.equal(storage.get('ah_points_pending'), false); // 3 次后退避，不再自动重试
  assert.equal(storage.get('ah_points_attempts'), 0);
  const lastAt = Number(storage.get('ah_points_last_at'));
  const remaining = lastAt - (Date.now() - 7 * 24 * 3600 * 1000);
  assert.ok(Math.abs(remaining - 24 * 3600 * 1000) < 5 * 60 * 1000); // 约 24h 后重新到期
});
