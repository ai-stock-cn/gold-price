/**
 * GitHub workflow 触发代理 —— 部署在 Cloudflare Workers（免费额度足够）
 *
 * 为什么需要它
 * ------------
 * 报表是纯静态页面，没法安全地持有 GitHub token：
 *   1. 公开仓库的页面源码谁都能看，token 写进去等于公开泄露；
 *   2. GitHub 会主动扫描公开代码里的 token 并自动吊销；
 *   3. 拿到 token 的人可以往你的仓库里触发任意 workflow。
 * 所以 token 必须放在服务端。这个 Worker 就是那个服务端：它持有 token，
 * 对浏览器只暴露一个「触发」动作，不暴露 token 本身。
 *
 * 部署步骤（全程网页操作，约 5 分钟）
 * ----------------------------------
 * 1. 建 token：GitHub → Settings → Developer settings → Personal access tokens
 *    → Fine-grained tokens → Generate new token
 *      · Repository access: Only select repositories → 勾 bang325/gold-price
 *      · Permissions → Repository permissions → **Actions: Read and write**
 *      · 其他一律不给（Contents 也不需要）
 *    生成后复制 `github_pat_...`，只显示一次。
 *
 * 2. 建 Worker：https://dash.cloudflare.com → Workers & Pages → Create → Worker
 *    把本文件内容整体粘贴进去 → Deploy。
 *
 * 3. 配环境变量：该 Worker → Settings → Variables and Secrets
 *      · GH_TOKEN    = 上一步的 token        （类型选 **Secret**）
 *      · ALLOW_ORIGIN = https://bang325.github.io   （不加结尾斜杠）
 *    REPO / WORKFLOW 已有默认值，仓库换了再改。
 *
 * 4. 把 Worker 的地址（形如 https://gold-dispatch.xxx.workers.dev）
 *    填进项目 config.json 的 repo.dispatch_url，重新生成并推送报表即可。
 *
 * 说明：ALLOW_ORIGIN 只能挡住浏览器跨站调用，挡不住 curl 伪造 Origin。
 * 想更严可以加 Cloudflare 的 Rate Limiting 规则（免费版有），或改成带路径密钥的形式。
 */

const DEFAULTS = {
  REPO: 'bang325/gold-price',
  WORKFLOW: 'collect.yml',
  REF: 'main',
};

export default {
  async fetch(request, env) {
    const origin = env.ALLOW_ORIGIN || '*';
    const cors = {
      'Access-Control-Allow-Origin': origin,
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Access-Control-Max-Age': '86400',
    };

    // 浏览器预检
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: cors });
    }
    if (request.method !== 'POST') {
      return json({ ok: false, error: 'method not allowed' }, 405, cors);
    }
    // 只接受来自自己站点的调用（可被 curl 伪造，作为最基础的一道门）
    const reqOrigin = request.headers.get('Origin');
    if (env.ALLOW_ORIGIN && reqOrigin && reqOrigin !== env.ALLOW_ORIGIN) {
      return json({ ok: false, error: 'origin not allowed' }, 403, cors);
    }
    if (!env.GH_TOKEN) {
      return json({ ok: false, error: 'GH_TOKEN 未配置' }, 500, cors);
    }

    const repo = env.REPO || DEFAULTS.REPO;
    const wf = env.WORKFLOW || DEFAULTS.WORKFLOW;
    const ref = env.REF || DEFAULTS.REF;

    const resp = await fetch(
      `https://api.github.com/repos/${repo}/actions/workflows/${wf}/dispatches`,
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${env.GH_TOKEN}`,
          Accept: 'application/vnd.github+json',
          'Content-Type': 'application/json',
          'User-Agent': 'gold-price-dispatch-worker',
          'X-GitHub-Api-Version': '2022-11-28',
        },
        body: JSON.stringify({ ref }),
      }
    );

    // 触发成功时 GitHub 返回 204 No Content
    if (resp.status === 204) {
      return json({ ok: true, repo, workflow: wf, ref }, 200, cors);
    }
    const text = (await resp.text()).slice(0, 300);
    return json({ ok: false, status: resp.status, detail: text }, 502, cors);
  },
};

function json(obj, status, cors) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...cors, 'Content-Type': 'application/json; charset=utf-8' },
  });
}
