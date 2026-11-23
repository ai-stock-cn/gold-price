/**
 * 金价采集 + 低位提醒 + GitHub 触发（Cloudflare Worker 入口）
 * 目标仓库：ai-stock-cn/gold-price
 *
 * 三条腿，互相独立：
 *   1) 价格腿（默认 5 分钟一次）
 *      抓伦敦金现（腾讯）× 美元兑人民币，折成元/克；算「当前价在近 30 日区间的分位」，
 *      命中阈值就用 PushPlus 推到微信。
 *   2) 触发腿（可选，需要 GH_TOKEN；只在整 15 分钟跑）
 *      调一次 workflow_dispatch，让报表尽快重建。
 *   3) 采集腿（旧，2026-11-16 起停用）
 *      原本把源站的零售牌价存进 KV 供 GitHub 回灌；现在价格口径换成国际金折算，
 *      这条腿留着只为兼容旧路由，不再参与计算。
 *
 * 环境变量 / 绑定：
 *   PUSHPLUS_TOKEN  PushPlus 的 token（Secret，必填才有提醒）
 *   GOLD_KV         KV 命名空间（存提醒状态 + 日 K 缓存；没绑就退化成无记忆模式）
 *   GH_TOKEN        可选。classic PAT 勾 workflow + public_repo
 *   ALLOW_ORIGIN    允许的前端来源，如 https://ai-stock-cn.github.io
 *   REPO/WORKFLOW/REF  可选
 *
 * 路由：
 *   GET  /            健康检查 + 当前分位
 *   GET  /status      当前价、分位、提醒状态（调试用）
 *   GET  /price       现抓一次价格
 *   GET  /data?date=  旧接口，返回 KV 里的历史点（兼容）
 *   POST /test        发一条测试推送
 *   POST /            触发一次 GitHub workflow
 */

const XAU_URL = 'https://qt.gtimg.cn/q=hf_XAU';
const FX_URL = 'https://hq.sinajs.cn/list=USDCNY';
const KLINE_URL =
  'https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/' +
  'GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=XAU';

const OZ_GRAMS = 31.1035;
const FEE_SPREAD = 17; // 中行积久金点差（约数，与报表保持一致）

const DEFAULTS = {
  REPO: 'ai-stock-cn/gold-price',
  WORKFLOW: 'collect.yml',
  REF: 'main',
};

// 提醒口径
const TIER_LOW = 10; // 分位 ≤10% 提示「进入低位区」
const TIER_BOTTOM = 5; // 分位 ≤5% 提示「接近 30 日最低」
const REBOUND_PCT = 0.5; // 从低点反弹 ≥0.5% 视为企稳
const MAX_ALERTS_PER_DAY = 6; // 每天主动提醒上限（防止下跌途中刷屏）
const COOLDOWN_MS = 90 * 60 * 1000; // 任意两条提醒至少间隔 90 分钟
const SUMMARY_HHMM = '22:00'; // 每日简报（北京时间）

/** 北京时间 */
function bjNow() {
  const iso = new Date(Date.now() + 8 * 3600 * 1000).toISOString();
  return {
    date: iso.slice(0, 10),
    hhmm: iso.slice(11, 16),
    ts: iso.slice(0, 19).replace('T', ' '),
  };
}

const round2 = (v) => Math.round(v * 100) / 100;

/**
 * 抓伦敦金现 + 汇率，折算成元/克。
 * 三个源都是 GBK，但这里只需要数字（ASCII），所以直接按文本解析，
 * 中文字段变成乱码也不影响——Workers 里 TextDecoder('gbk') 不一定可用。
 */
async function fetchCnyPrice() {
  const [xauResp, fxResp] = await Promise.all([
    fetch(XAU_URL, { headers: { 'User-Agent': 'Mozilla/5.0' } }),
    fetch(FX_URL, {
      headers: { 'User-Agent': 'Mozilla/5.0', Referer: 'https://finance.sina.com.cn/' },
    }),
  ]);
  if (!xauResp.ok) throw new Error('腾讯行情 HTTP ' + xauResp.status);
  if (!fxResp.ok) throw new Error('汇率 HTTP ' + fxResp.status);

  const xauTxt = await xauResp.text();
  const fxTxt = await fxResp.text();

  const quoted = xauTxt.match(/"([^"]+)"/);
  if (!quoted) throw new Error('行情格式异常');
  const f = quoted[1].split(',');
  const usd = Number(f[0]);
  const chg = Number(f[1]);
  const high = Number(f[4]);
  const low = Number(f[5]);
  const qtime = f[6] || '';

  const fxQuoted = fxTxt.match(/"([^"]+)"/);
  if (!fxQuoted) throw new Error('汇率格式异常');
  const rate = Number(fxQuoted[1].split(',')[8]);
  if (!isFinite(usd) || !isFinite(rate)) throw new Error('价格或汇率不是数字');

  return {
    cny: usd * rate / OZ_GRAMS,
    usd,
    rate,
    chg,
    usdHigh: high,
    usdLow: low,
    qtime,
  };
}

/** 近 N 个交易日的高低（结果按日期缓存，一天只拉一次） */
async function fetchRange(env, days = 30) {
  const { date } = bjNow();
  const key = `k:${days}d:${date}`;
  if (env.GOLD_KV) {
    const cached = await env.GOLD_KV.get(key);
    if (cached) {
      try {
        return JSON.parse(cached);
      } catch (_) {
        /* 缓存坏了就重拉 */
      }
    }
  }
  const resp = await fetch(KLINE_URL, {
    headers: { 'User-Agent': 'Mozilla/5.0', Referer: 'https://finance.sina.com.cn/' },
  });
  if (!resp.ok) throw new Error('日K HTTP ' + resp.status);
  const txt = await resp.text();
  const m = txt.match(/var t=\((.*)\)\s*;?\s*$/s);
  if (!m) throw new Error('日K 解析失败');
  const arr = JSON.parse(m[1]);
  const seg = arr.slice(-days);
  const lo = Math.min(...seg.map((x) => Number(x.low)));
  const hi = Math.max(...seg.map((x) => Number(x.high)));
  const out = { lo, hi };
  if (env.GOLD_KV) {
    await env.GOLD_KV.put(key, JSON.stringify(out), { expirationTtl: 86400 * 2 });
  }
  return out;
}

/** 推送状态 */
async function loadState(env) {
  const empty = {
    date: '',
    tier: 0,
    minToday: null,
    lowAt: 0,
    lowValue: null,
    alerts: 0,
    lastAlertAt: 0,
    flags: [],
    lastSummaryDate: '',
  };
  if (!env.GOLD_KV) return empty;
  try {
    const raw = await env.GOLD_KV.get('alert:state');
    return raw ? { ...empty, ...JSON.parse(raw) } : empty;
  } catch (_) {
    return empty;
  }
}

async function saveState(env, state) {
  if (!env.GOLD_KV) return;
  await env.GOLD_KV.put('alert:state', JSON.stringify(state));
}

/** PushPlus 推送（markdown） */
async function pushPlus(env, title, content) {
  if (!env.PUSHPLUS_TOKEN) {
    console.log('[push] PUSHPLUS_TOKEN 未配置，跳过');
    return { ok: false, error: 'no token' };
  }
  const resp = await fetch('https://www.pushplus.plus/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'User-Agent': 'gold-price-worker' },
    body: JSON.stringify({
      token: env.PUSHPLUS_TOKEN,
      title,
      content,
      template: 'markdown',
    }),
  });
  const text = await resp.text();
  let ok = false;
  try {
    ok = JSON.parse(text).code === 200;
  } catch (_) {
    /* ignore */
  }
  if (!ok) console.log('[push] 失败 ' + text.slice(0, 200));
  return { ok, detail: text.slice(0, 200) };
}

/** 组一条提醒正文 */
function bodyOf(p, range, rank, extra) {
  const lines = [];
  lines.push(`**现价 ${round2(p.cny).toFixed(2)} 元/克**（含手续费 ${round2(p.cny + FEE_SPREAD).toFixed(2)}）`);
  lines.push(`近 30 日区间 ${range.lo.toFixed(2)} ~ ${range.hi.toFixed(2)} → 分位 **${rank.toFixed(1)}%**`);
  lines.push(`国际金 ${p.usd.toFixed(2)} 美元/盎司 · ${p.qtime || ''}`);
  if (extra) lines.push('');
  if (extra) lines.push(extra);
  lines.push('');
  lines.push(`距 30 日最低还有 ${(p.cny - range.lo).toFixed(2)} 元/克（${(((p.cny - range.lo) / p.cny) * 100).toFixed(2)}%）`);
  return lines.join('\n');
}

/**
 * 核心：判断要不要提醒。
 * 返回 null 表示不推；否则返回 { title, content, type }
 */
function decide(price, range, state, at) {
  const rank = ((price.cny - range.lo) / (range.hi - range.lo)) * 100;
  const tier = rank <= TIER_BOTTOM ? 2 : rank <= TIER_LOW ? 1 : 0;

  const canPush =
    state.alerts < MAX_ALERTS_PER_DAY &&
    at - (state.lastAlertAt || 0) > COOLDOWN_MS;

  // ① 分位破档（往下走才推，回升不推）
  if (tier > state.tier && tier > 0 && canPush) {
    const label = tier === 2 ? '接近 30 日最低' : '进入 30 日低位区';
    const mark = tier === 2 ? '⚠️' : '🟡';
    return {
      type: 'tier' + tier,
      title: `${mark} 黄金${label}`,
      content: bodyOf(price, range, rank, `**${label}**——分位跌破 ${tier === 2 ? TIER_BOTTOM : TIER_LOW}%。`),
      rank,
      tier,
    };
  }

  // ② 创当日新低（当天第一次跌破前低时提示）
  if (state.minToday !== null && price.cny < state.minToday - 0.01 && canPush &&
      !state.flags.includes('newlow' + at_date())) {
    return {
      type: 'newlow',
      title: '📉 黄金创当日新低',
      content: bodyOf(price, range, rank, `跌破此前当日最低 ${state.minToday.toFixed(2)}，现 ${round2(price.cny).toFixed(2)}。`),
      rank,
      tier,
    };
  }

  // ③ 反弹确认：从最近低点反弹 ≥0.5%，且低点是 24 小时内的
  if (
    state.lowValue &&
    at - (state.lowAt || 0) < 24 * 3600 * 1000 &&
    price.cny >= state.lowValue * (1 + REBOUND_PCT / 100) &&
    !state.flags.includes('rebound') &&
    canPush
  ) {
    const up = ((price.cny - state.lowValue) / state.lowValue) * 100;
    return {
      type: 'rebound',
      title: '📈 黄金低点后反弹',
      content: bodyOf(
        price, range, rank,
        `从低点 ${state.lowValue.toFixed(2)} 反弹 ${up.toFixed(2)}%，可能企稳——低点只有反弹后才算确认。`
      ),
      rank,
      tier,
    };
  }

  return null;
}

function at_date() {
  return bjNow().date;
}

/** 每 5 分钟跑一次：算分位、决定要不要推 */
async function alertTick(env) {
  const at = Date.now();
  const now = bjNow();

  let price;
  try {
    price = await fetchCnyPrice();
  } catch (e) {
    console.log('[alert] 取价失败 ' + e.message);
    return;
  }

  let range;
  try {
    range = await fetchRange(env);
  } catch (e) {
    console.log('[alert] 取区间失败 ' + e.message);
    return;
  }

  const state = await loadState(env);

  // 跨日重置（每天 0 点北京时间）
  if (state.date !== now.date) {
    state.date = now.date;
    state.minToday = null;
    state.alerts = 0;
    state.flags = [];
    // tier 不重置：仍在低位区就不该重复播报
  }

  // 记录当日最低 & 最近低点
  if (state.minToday === null || price.cny < state.minToday) {
    state.minToday = round2(price.cny);
    state.lowAt = at;
    state.lowValue = round2(price.cny);
  }

  // 每日简报（22:00 之后第一次 tick）
  if (now.hhmm >= SUMMARY_HHMM && state.lastSummaryDate !== now.date) {
    state.lastSummaryDate = now.date;
    const rank = ((price.cny - range.lo) / (range.hi - range.lo)) * 100;
    await pushPlus(
      env,
      '📊 黄金今日简报',
      bodyOf(
        price, range, rank,
        `今日最低 ${state.minToday.toFixed(2)}，当前较最低 ${(((price.cny - state.minToday) / state.minToday) * 100).toFixed(2)}%。`
      )
    );
    console.log(`[alert] summary pushed ${now.ts}`);
  }

  const hit = decide(price, range, state, at);
  if (!hit) {
    state.tier = ((price.cny - range.lo) / (range.hi - range.lo)) * 100 <= TIER_BOTTOM ? 2 :
                 ((price.cny - range.lo) / (range.hi - range.lo)) * 100 <= TIER_LOW ? 1 : 0;
    await saveState(env, state);
    return;
  }

  const r = await pushPlus(env, hit.title, hit.content);
  if (r.ok) {
    state.alerts += 1;
    state.lastAlertAt = at;
    state.tier = hit.tier !== undefined ? hit.tier : state.tier;
    if (hit.type === 'newlow') state.flags.push('newlow' + now.date);
    if (hit.type === 'rebound') state.flags.push('rebound');
    console.log(`[alert] pushed ${hit.type} ${now.ts}`);
  }
  await saveState(env, state);
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

/** 调 GitHub workflow_dispatch。没配 token 就跳过，不算失败。 */
async function triggerWorkflow(env, cors) {
  if (!env.GH_TOKEN) return json({ ok: false, error: 'GH_TOKEN 未配置' }, 400, cors);
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

export default {
  /**
   * Cron 入口（wrangler.toml 配的是每 5 分钟一次）：
   *   每次都跑提醒判断；只有整 15 分钟才触发 GitHub 重建（避免 Actions 被刷 3 倍）。
   */
  async scheduled(event, env, ctx) {
    const at = bjNow();
    const minute = Number(at.hhmm.slice(3, 5));

    ctx.waitUntil(
      (async () => {
        try {
          await alertTick(env);
        } catch (e) {
          console.log('[cron] alert 异常 ' + (e && e.message));
        }
        if (env.GH_TOKEN && minute % 15 === 0) {
          try {
            const r = await triggerWorkflow(env, {});
            console.log(`[cron ${at.ts}] dispatch ${r.status}`);
          } catch (e) {
            console.log('[cron] dispatch 异常 ' + (e && e.message));
          }
        }
      })()
    );
  },

  async fetch(request, env) {
    const origin = env.ALLOW_ORIGIN || '*';
    const cors = corsFor(origin);
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });

    const reqOrigin = request.headers.get('Origin');
    if (env.ALLOW_ORIGIN && reqOrigin && reqOrigin !== env.ALLOW_ORIGIN) {
      return json({ ok: false, error: 'origin not allowed' }, 403, cors);
    }

    if (request.method === 'GET') {
      if (url.pathname === '/status') {
        try {
          const price = await fetchCnyPrice();
          const range = await fetchRange(env);
          const state = await loadState(env);
          const rank = ((price.cny - range.lo) / (range.hi - range.lo)) * 100;
          return json(
            {
              ok: true,
              at: bjNow().ts,
              cny: round2(price.cny),
              cnyWithFee: round2(price.cny + FEE_SPREAD),
              usd: price.usd,
              rate: price.rate,
              range: { lo: round2(range.lo), hi: round2(range.hi) },
              rank: Math.round(rank * 10) / 10,
              tier: state.tier,
              alertsToday: state.alerts,
              minToday: state.minToday,
            },
            200,
            cors
          );
        } catch (e) {
          return json({ ok: false, error: String((e && e.message) || e) }, 502, cors);
        }
      }
      if (url.pathname === '/price') {
        try {
          const p = await fetchCnyPrice();
          return json({ ok: true, at: bjNow().ts, cny: round2(p.cny), usd: p.usd, rate: p.rate }, 200, cors);
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
        {
          ok: true,
          service: 'gold-price-worker',
          routes: ['GET /status', 'GET /price', 'GET /data?date=', 'POST /test', 'POST /'],
        },
        200,
        cors
      );
    }

    if (request.method === 'POST') {
      if (url.pathname === '/test') {
        const r = await pushPlus(env, '🔔 黄金提醒·通道测试', '这条是测试消息，收到说明推送已通。');
        return json({ ok: r.ok, detail: r.detail }, r.ok ? 200 : 502, cors);
      }
      return triggerWorkflow(env, cors);
    }
    return json({ ok: false, error: 'method not allowed' }, 405, cors);
  },
};

// 供离线单测使用（Workers 运行时忽略额外的具名导出）
export { decide, bodyOf };
