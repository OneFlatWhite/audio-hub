/**
 * audio-hub 授权码发放 Worker（模板）——star 换码。
 *
 * 部署（Cloudflare Workers → Wrangler 或面板粘贴）：
 *   1. GitHub → Settings → Developer settings → OAuth Apps → New：
 *      callback = https://<你的worker域名>/callback
 *   2. Worker Secrets：
 *        GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET
 *        LICENSE_PRIVATE_KEY   （PKCS8 PEM，scripts/issue_license.py genkey 产物）
 *      Variables：
 *        REPO                  （默认 OneFlatWhite/audio-hub）
 *   3. 用户流程：/ → 点「GitHub 登录并领取」→ /auth → /callback → 校验 Star → 显示授权码。
 *
 * Ed25519 签名用 WebCrypto（与 scripts/issue_license.py 的 PKCS8 私钥同源）。
 */

const REPO_DEFAULT = "OneFlatWhite/audio-hub";

export default {
  fetch(request, env) {
    const url = new URL(request.url);
    const repo = env.REPO || REPO_DEFAULT;
    if (url.pathname === "/") return page(repo);
    if (url.pathname === "/auth") return githubAuth(env, url, repo);
    if (url.pathname === "/callback") return githubCallback(env, url, repo);
    return new Response("Not found", { status: 404 });
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
     <p><a href="/auth"><button style="padding:10px 22px;font-size:15px">GitHub 登录并领取</button></a></p>
     </body>`,
    { headers: { "content-type": "text/html; charset=utf-8" } },
  );
}

async function githubAuth(env, url, repo) {
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

  // 1. code 换 user token
  const tokenRes = await fetch("https://github.com/login/oauth/access_token", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({
      client_id: env.GITHUB_CLIENT_ID, client_secret: env.GITHUB_CLIENT_SECRET, code,
    }),
  });
  const { access_token } = await tokenRes.json();
  if (!access_token) return text("GitHub 登录失败", 401);

  // 2. 用户名
  const user = (await (await fetch("https://api.github.com/user", {
    headers: { authorization: `Bearer ${access_token}`, "user-agent": "audio-hub-issuer" },
  })).json()).login;

  // 3. 是否 Star 过
  const star = await fetch(`https://api.github.com/user/starred/${repo}`, {
    headers: { authorization: `Bearer ${access_token}`, "user-agent": "audio-hub-issuer" },
  });
  if (star.status !== 204) {
    return text(`尚未 Star ${repo}。点 Star 后 <a href="/">重新领取</a>。`, 403);
  }

  // 4. 签发授权码（WebCrypto Ed25519）
  const key = await crypto.subtle.importKey(
    "pkcs8", pemToBytes(env.LICENSE_PRIVATE_KEY),
    { name: "Ed25519" }, false, ["sign"],
  );
  const payload = JSON.stringify({
    v: 1, user, repo, issued_at: Math.floor(Date.now() / 1000), exp: null,
  });
  const sig = await crypto.subtle.sign("Ed25519", key, new TextEncoder().encode(payload));
  const b64u = (b) => btoa(String.fromCharCode(...new Uint8Array(b)))
    .replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
  const license = `AH1.${b64u(new TextEncoder().encode(payload))}.${b64u(sig)}`;

  return text(`你的授权码（粘贴到 audio-hub 的 /activate 页）：\n\n${license}`);
}

function pemToBytes(pem) {
  const b64 = pem.replace(/-----(BEGIN|END) PRIVATE KEY-----/g, "").replace(/\s+/g, "");
  const raw = atob(b64);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function text(body, status = 200) {
  return new Response(body, {
    status, headers: { "content-type": "text/html; charset=utf-8" },
  });
}
