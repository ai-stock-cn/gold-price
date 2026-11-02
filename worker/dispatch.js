/**
 * GitHub workflow 触发代理（Cloudflare Worker 入口）
 * 目标仓库：ai-stock-cn/gold-price（数据与站点都在这个仓库；bang325 那个仅作历史备份）
 *
 * 作用：报表是纯静态页面，不能安全持有 GitHub token（公开页面源码谁都能看，
 * GitHub 还会自动吊销扫描到的 token）。所以 token 放在这里，浏览器只能调用
 * 「触发一次采集」这一个动作，拿不到 token 本身。
 *
 * 环境变量（在 Cloudflare → Worker → Settings → Variables and Secrets 配置）：
 *   GH_TOKEN      fine-grained PAT，仅需该仓库 Actions: Read and write   （类型：Secret）
 *   ALLOW_ORIGIN  允许的页面来源，如 https://bang325.github.io            （类型：Text）
 *   REPO          可选，默认 bang325/gold-price
 *   WORKFLOW      可选，默认 collect.yml
 *   REF           可选，默认 main
 *
 * 部署方式：本仓库已接入 Workers Builds，push 到 main 即自动构建部署
 * （构建命令 npx wrangler deploy，配置见根目录 wrangler.toml）。
 */

const DEFAULTS = {
  REPO: 'ai-stock-cn/gold-price',
  WORKFLOW: 'collect.yml',
  REF: 'main',
};

/**
 * 核心动作：调 GitHub 的 workflow_dispatch。HTTP 请求和定时器都走这里。
 */
async function triggerWorkflow(env, cors) {
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
}

export default {
  /**
   * 定时触发：wrangler.toml 里配了 crons 就会按点调用这里。
   * GitHub 自己的 schedule 被节流到 3~7 小时一次，这个才是真正的 15 分钟源。
   */
  async scheduled(event, env, ctx) {
    const res = await triggerWorkflow(env, {});
    const body = await res.text();
    console.log(`[cron ${new Date(event.scheduledTime).toISOString()}] ${res.status} ${body}`);
  },

  async fetch(request, env) {
    const origin = env.ALLOW_ORIGIN || '*';
    const cors = {
      'Access-Control-Allow-Origin': origin,
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Access-Control-Max-Age': '86400',
    };

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: cors });
    }
    if (request.method !== 'POST') {
      return json({ ok: false, error: 'method not allowed' }, 405, cors);
    }

    // 只接受来自自己站点的浏览器调用。
    // ⚠️ 这挡不住 curl 伪造 Origin，只能拦普通跨站页面。需要更严请加 Rate Limiting。
    const reqOrigin = request.headers.get('Origin');
    if (env.ALLOW_ORIGIN && reqOrigin && reqOrigin !== env.ALLOW_ORIGIN) {
      return json({ ok: false, error: 'origin not allowed' }, 403, cors);
    }
    return triggerWorkflow(env, cors);
  },
};

function json(obj, status, cors) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...cors, 'Content-Type': 'application/json; charset=utf-8' },
  });
}
