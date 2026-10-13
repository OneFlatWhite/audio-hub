/**
 * audio-hub 授权码发放 + 吊销 Worker（模板）——star 换码 / 取消 Star 自动吊销。
 *
 * 部署（Cloudflare Workers）：
 *   1. GitHub → Developer settings → OAuth Apps → New：
 *      callback = https://<你的worker域名>/callback
 *   2. KV 命名空间绑定：LICENSE_KV（存发放台账与吊销状态）
 *   3. Secrets：GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET /
 *      LICENSE_PRIVATE_KEY（PKCS8 PEM，scripts/issue_license.py genkey 产物）/
 *      GITHUB_TOKEN（可选：提升 stargazers 列表分页上限）
 *   4. Cron Triggers：0 3 * * *（每日 3 点跑 scheduled()，比对 Star 列表自动吊销）
 *   5. 程序侧环境变量：AUDIO_HUB_LICENSE_VERIFY_URL=https://<worker域名>/api/status
 *
 * 端点：
 *   /            领取页（GitHub OAuth）
 *   /callback    OAuth 回调：校验 Star → 签发授权码（含 jti）→ 写台账
 *   /api/status?jti=xxx   程序在线复核：{"revoked": bool}
 *   scheduled()  每日拉取仓库 Stargazers 公开列表，台账里不在列表的用户标记吊销
 */

const REPO_DEFAULT = "OneFlatWhite/audio-hub";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const repo = env.REPO || REPO_DEFAULT;
    if (url.pathname === "/") return page(repo);
    if (url.pathname === "/auth") return githubAuth(env, url);
    if (url.pathname === "/callback") return githubCallback(env, url, repo);
    if (url.pathname === "/api/status") return status(env, url);
    return new Response("Not found", { status: 404 });
  },

  async scheduled(_event, env, _ctx) {
    await revokeUnstarred(env);
  },
};

function page(repo) {
  const [owner, name] = repo.split("/");
  return new Response(
    `<!doctype html><meta charset="utf-8"><title>audio-hub 授权码发放</title>
     <body style="font-family:system-ui;max-width:560px;margin:60px auto;line-height:1.8">
     <h2>领取 audio-hub 授权码</h2>
     <p>① 确认你已给
     <a href="https://github.com/${repo}" target="_blank" rel="noopener">${owner}/${name}</a> 点 Star；
     ② 点击下方按钮用 GitHub 登录，自动校验后发放授权码。</p>
     <p>提示：取消 Star 后，授权会在每日核对时自动吊销，程序端最长宽限 30 天。</p>
     <p><a href="/auth"><button style="padding:10px 22px;font-size:15px">GitHub 登录并领取</button></a></p>
     </body>`,
    { headers: { "content-type": "text/html; charset=utf-8" } },
  );
}

async function githubAuth(env, url) {
  const params = new URLSearchParams({
    client_id: env.GITHUB_CLIENT_ID,
    redirect_uri: `${url.origin}/callback`,
    scope: "read:user",
    state: crypto.randomUUID(),
  });
  return Response.redirect(`https://github.com/login/oauth/authorize?${params}`, 302);
}

async function githubCallback(env, url, repo) {
  const code = url.searchParams.get("code");
  if (!code) return text("缺少 code", 400);

  const tokenRes = await fetch("https://github.com/login/oauth/access_token", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({
      client_id: env.GITHUB_CLIENT_ID, client_secret: env.GITHUB_CLIENT_SECRET, code,
    }),
  });
  const { access_token } = await tokenRes.json();
  if (!access_token) return text("GitHub 登录失败", 401);

  const user = (await (await fetch("https://api.github.com/user", {
    headers: { authorization: `Bearer ${access_token}`, "user-agent": "audio-hub-issuer" },
  })).json()).login;

  const star = await fetch(`https://api.github.com/user/starred/${repo}`, {
    headers: { authorization: `Bearer ${access_token}`, "user-agent": "audio-hub-issuer" },
  });
  if (star.status !== 204) {
    return text(`尚未 Star ${repo}。点 Star 后 <a href="/">重新领取</a>。`, 403);
  }

  // 签发（jti 进台账，供吊销核对）
  const jti = crypto.randomUUID();
  const key = await crypto.subtle.importKey(
    "pkcs8", pemToBytes(env.LICENSE_PRIVATE_KEY),
    { name: "Ed25519" }, false, ["sign"],
  );
  const payload = JSON.stringify({
    v: 1, user, repo, jti, issued_at: Math.floor(Date.now() / 1000), exp: null,
  });
  const sig = await crypto.subtle.sign("Ed25519", key, new TextEncoder().encode(payload));
  const b64u = (b) => btoa(String.fromCharCode(...new Uint8Array(b)))
    .replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
  const license = `AH1.${b64u(new TextEncoder().encode(payload))}.${b64u(sig)}`;

  if (env.LICENSE_KV) {
    await env.LICENSE_KV.put(`issued:${jti}`, JSON.stringify({ user, ts: Date.now() }));
    await env.LICENSE_KV.put(`revoked:${jti}`, "0");
  }
  return text(`你的授权码（粘贴到 audio-hub 的 /activate 页）：\n\n${license}`);
}

async function status(env, url) {
  const jti = url.searchParams.get("jti") || "";
  if (!jti) return jsonResp({ revoked: true, error: "missing jti" });
  let revoked = true;                       // 未知的 jti 一律视为吊销
  if (env.LICENSE_KV) {
    const issued = await env.LICENSE_KV.get(`issued:${jti}`);
    if (issued) revoked = (await env.LICENSE_KV.get(`revoked:${jti}`)) === "1";
  }
  return jsonResp({ revoked });
}

// 每日 cron：拉取仓库 Stargazers 公开列表；台账里不在列表的用户标记吊销
async function revokeUnstarred(env) {
  const repo = env.REPO || REPO_DEFAULT;
  const headers = { "user-agent": "audio-hub-issuer" };
  if (env.GITHUB_TOKEN) headers.authorization = `Bearer ${env.GITHUB_TOKEN}`;
  const starers = new Set();
  for (let pageNum = 1; pageNum <= 20; pageNum++) {          // 每页 100，最多 2000 人
    const res = await fetch(
      `https://api.github.com/repos/${repo}/stargazers?per_page=100&page=${pageNum}`,
      { headers },
    );
    if (!res.ok) return;                                     // 拉取失败：本轮不动状态
    const users = await res.json();
    if (!users.length) break;
    users.forEach((u) => starers.add(u.login));
  }
  if (!env.LICENSE_KV) return;
  let cursor = undefined;
  do {
    const list = await env.LICENSE_KV.list({ prefix: "issued:", cursor });
    for (const item of list.keys) {
      const jti = item.name.slice("issued:".length);
      const rec = JSON.parse(await env.LICENSE_KV.get(item.name) || "{}");
      const shouldRevoke = !starers.has(rec.user);
      const cur = await env.LICENSE_KV.get(`revoked:${jti}`);
      if (shouldRevoke && cur !== "1") await env.LICENSE_KV.put(`revoked:${jti}`, "1");
      if (!shouldRevoke && cur !== "0") await env.LICENSE_KV.put(`revoked:${jti}`, "0");
    }
    cursor = list.list_complete ? undefined : list.cursor;
  } while (cursor);
}

function pemToBytes(pem) {
  const b64 = pem.replace(/-----(BEGIN|END) PRIVATE KEY-----/g, "").replace(/\s+/g, "");
  const raw = atob(b64);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function jsonResp(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status, headers: { "content-type": "application/json" },
  });
}

function text(body, status = 200) {
  return new Response(body, {
    status, headers: { "content-type": "text/html; charset=utf-8" },
  });
}
