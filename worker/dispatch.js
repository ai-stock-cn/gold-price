/**
 * 金价采集 + GitHub 触发（Cloudflare Worker 入口）
 * 目标仓库：ai-stock-cn/gold-price
 *
 * 两条腿，互相独立：
 *   1) 采集腿（不需要任何 GitHub token）
 *      Cron 每 15 分钟抓一次源站 JSON，把点存进 Workers KV（绑定名 GOLD_KV）。
 *      GitHub Actions 每次运行时来 GET /data，把 KV 里的点全量补进 CSV。
 *      —— 这样即使 GitHub 的 schedule 被节流到几小时一次，数据也不会断点。
 *   2) 触发腿（可选，需要 GH_TOKEN）
 *      Cron 顺带调一次 workflow_dispatch，让报表尽快重建。
 *      没有 token 就跳过，完全不影响采集。
 *
 * 环境变量 / 绑定：
 *   GOLD_KV        KV 命名空间绑定（必需，否则采集的点无处存放）
 *   GH_TOKEN       可选。classic PAT 勾 workflow + public_repo；或 fine-grained 给 Actions: RW
 *   ALLOW_ORIGIN   允许的前端来源，如 https://ai-stock-cn.github.io
 *   REPO           可选，默认 ai-stock-cn/gold-price
 *   WORKFLOW       可选，默认 collect.yml
 *   REF            可选，默认 main
 *
 * 路由：
 *   GET  /            健康检查 + 路由说明
 *   GET  /price       现抓一次源站，返回当前价（不落库）
 *   GET  /data?date=  返回某天已采集的点（默认今天，北京时间）
 *   POST /            触发一次 GitHub workflow（需要 GH_TOKEN）
 *
 * 部署：面板 Edit code 粘贴本文件 → Deploy；
 *       Settings → Trigger events → Cron triggers 填 0,15,30,45 * * * *
 *       Settings → Bindings 绑一个 KV 命名空间到 GOLD_KV
 */

const SOURCE_URL = 'https://www.chnau99999.com/page/board';
const DEFAULTS = {
  REPO: 'ai-stock-cn/gold-price',
  WORKFLOW: 'collect.yml',
  REF: 'main',
};

/** 北京时间的日期 / 时分 / 时间戳 */
function bjNow() {
  const iso = new Date(Date.now() + 8 * 3600 * 1000).toISOString();
  return { date: iso.slice(0, 10), hhmm: iso.slice(11, 16), ts: iso.slice(0, 19).replace('T', ' ') };
}

const num = (v) => (v === null || v === undefined || v === '' || v === '-' ? null : Number(v));

/** 抓源站。注意是 POST 到 /page/board 拿 JSON，不是抓网页。 */
async function fetchPrice() {
  const resp = await fetch(SOURCE_URL, {
    method: 'POST',
    headers: {
      'User-Agent': 'Mozilla/5.0',
      'X-Requested-With': 'XMLHttpRequest',
      Referer: 'https://www.chnau99999.com/page/goldPrice',
      'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8',
    },
    body: '',
  });
  if (!resp.ok) throw new Error('源站 HTTP ' + resp.status);
  const obj = await resp.json();
  if (obj.code !== 200) throw new Error('源站 code=' + obj.code);
  const d = obj.data || {};
  const out = { cur: num(d.cur), sel: num(d.sel), buy: num(d.buy), acc: num(d.accessories) };
  if (out.cur === null) throw new Error('缺少基础金价字段');
  return out;
}

/** 追加一个点到 KV。KV 没有原子追加，读-改-写即可（并发极低）。 */
async function savePoint(env, price) {
  if (!env.GOLD_KV) return false;
  const { date, hhmm, ts } = bjNow();
  const key = 'd:' + date;
  const arr = JSON.parse((await env.GOLD_KV.get(key)) || '[]');
  const idx = arr.findIndex((p) => p.slot === hhmm);
  const point = { slot: hhmm, cur: price.cur, sel: price.sel, buy: price.buy, acc: price.acc, ts };
  if (idx >= 0) arr[idx] = point;
  else arr.push(point);
  arr.sort((a, b) => (a.slot < b.slot ? -1 : 1));
  await env.GOLD_KV.put(key, JSON.stringify(arr.slice(-400)));
  return true;
}

/** 调 GitHub workflow_dispatch。没配 token 就跳过，不算失败。 */
async function triggerWorkflow(env, cors) {
  if (!env.GH_TOKEN) {
    return json({ ok: false, error: 'GH_TOKEN 未配置（采集不受影响）' }, 400, cors);
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
        'User-Agent': 'gold-price-worker',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      body: JSON.stringify({ ref }),
    }
  );
  if (resp.status === 204) return json({ ok: true, repo, workflow: wf, ref }, 200, cors);
  return json({ ok: false, status: resp.status, detail: (await resp.text()).slice(0, 300) }, 502, cors);
}

const corsFor = (origin) => ({
  'Access-Control-Allow-Origin': origin,
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
  'Access-Control-Max-Age': '86400',
});

function json(obj, status, cors) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...cors, 'Content-Type': 'application/json; charset=utf-8' },
  });
}

export default {
  /** Cron 入口：每 15 分钟采集一次，顺带（可选）触发 GitHub 重建报表 */
  async scheduled(event, env, ctx) {
    const at = bjNow();
    let price = null;
    let err = null;
    try {
      price = await fetchPrice();
      if (!(await savePoint(env, price))) err = 'GOLD_KV 未绑定，点未落库';
    } catch (e) {
      err = String((e && e.message) || e);
    }
    console.log(`[cron ${at.ts}] price=${price ? price.cur : '-'} err=${err || '-'}`);

    if (env.GH_TOKEN) {
      const r = await triggerWorkflow(env, {});
      console.log(`[cron ${at.ts}] dispatch ${r.status}`);
    }
  },

  async fetch(request, env) {
    const origin = env.ALLOW_ORIGIN || '*';
    const cors = corsFor(origin);
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: cors });
    }

    // 只允许自己的站点跨域调用；curl 可伪造 Origin，仅作基础拦截
    const reqOrigin = request.headers.get('Origin');
    if (env.ALLOW_ORIGIN && reqOrigin && reqOrigin !== env.ALLOW_ORIGIN) {
      return json({ ok: false, error: 'origin not allowed' }, 403, cors);
    }

    if (request.method === 'GET') {
      if (url.pathname === '/price') {
        try {
          return json({ ok: true, at: bjNow().ts, price: await fetchPrice() }, 200, cors);
        } catch (e) {
          return json({ ok: false, error: String((e && e.message) || e) }, 502, cors);
        }
      }
      if (url.pathname === '/data') {
        const date = url.searchParams.get('date') || bjNow().date;
        const raw = env.GOLD_KV ? await env.GOLD_KV.get('d:' + date) : null;
        return new Response(`{"date":"${date}","points":${raw || '[]'}}`, {
          headers: { ...cors, 'Content-Type': 'application/json; charset=utf-8' },
        });
      }
      return json(
        { ok: true, service: 'gold-price-worker', routes: ['GET /price', 'GET /data?date=YYYY-MM-DD', 'POST /'] },
        200,
        cors
      );
    }

    if (request.method === 'POST') return triggerWorkflow(env, cors);
    return json({ ok: false, error: 'method not allowed' }, 405, cors);
  },
};
