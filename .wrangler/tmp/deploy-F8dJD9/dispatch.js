var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// worker/dispatch.js
var XAU_URL = "https://qt.gtimg.cn/q=hf_XAU";
var FX_URL = "https://hq.sinajs.cn/list=USDCNY";
var KLINE_URL = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=XAU";
var OZ_GRAMS = 31.1035;
var FEE_SPREAD = 17;
var DEFAULTS = {
  REPO: "ai-stock-cn/gold-price",
  WORKFLOW: "collect.yml",
  REF: "main"
};
var REBOUND_PCT = 0.5;
var MAX_ALERTS_PER_DAY = 12;
var COOLDOWN_MS = 30 * 60 * 1e3;
var PRICE_LEVELS = (() => {
  const head = [770, 766, 760, 752, 745];
  const tail = [];
  for (let p = 740; p >= 600; p -= 5) tail.push(p);
  return head.concat(tail);
})();
var SUMMARY_SLOTS = [
  "09:00",
  "10:00",
  "11:00",
  "12:00",
  "13:00",
  "14:00",
  "15:00",
  "16:00",
  "17:00",
  "18:00",
  "19:00",
  "19:30"
];
var PUSH_WINDOW = { start: 7 * 60, end: 20 * 60, weekdays: [1, 2, 3, 4, 5] };
function bjNow() {
  const iso = new Date(Date.now() + 8 * 3600 * 1e3).toISOString();
  return {
    date: iso.slice(0, 10),
    hhmm: iso.slice(11, 16),
    ts: iso.slice(0, 19).replace("T", " ")
  };
}
__name(bjNow, "bjNow");
var round2 = /* @__PURE__ */ __name((v) => Math.round(v * 100) / 100, "round2");
function inPushWindow(now) {
  const iso = new Date(Date.now() + 8 * 3600 * 1e3);
  const dow = iso.getUTCDay();
  const minutes = iso.getUTCHours() * 60 + iso.getUTCMinutes();
  return PUSH_WINDOW.weekdays.includes(dow) && minutes >= PUSH_WINDOW.start && minutes < PUSH_WINDOW.end;
}
__name(inPushWindow, "inPushWindow");
async function fetchCnyPrice() {
  const [xauResp, fxResp] = await Promise.all([
    fetch(XAU_URL, { headers: { "User-Agent": "Mozilla/5.0" } }),
    fetch(FX_URL, {
      headers: { "User-Agent": "Mozilla/5.0", Referer: "https://finance.sina.com.cn/" }
    })
  ]);
  if (!xauResp.ok) throw new Error("\u817E\u8BAF\u884C\u60C5 HTTP " + xauResp.status);
  if (!fxResp.ok) throw new Error("\u6C47\u7387 HTTP " + fxResp.status);
  const xauTxt = await xauResp.text();
  const fxTxt = await fxResp.text();
  const quoted = xauTxt.match(/"([^"]+)"/);
  if (!quoted) throw new Error("\u884C\u60C5\u683C\u5F0F\u5F02\u5E38");
  const f = quoted[1].split(",");
  const usd = Number(f[0]);
  const chg = Number(f[1]);
  const high = Number(f[4]);
  const low = Number(f[5]);
  const qtime = f[6] || "";
  const fxQuoted = fxTxt.match(/"([^"]+)"/);
  if (!fxQuoted) throw new Error("\u6C47\u7387\u683C\u5F0F\u5F02\u5E38");
  const rate = Number(fxQuoted[1].split(",")[8]);
  if (!isFinite(usd) || !isFinite(rate)) throw new Error("\u4EF7\u683C\u6216\u6C47\u7387\u4E0D\u662F\u6570\u5B57");
  return {
    cny: usd * rate / OZ_GRAMS,
    usd,
    rate,
    chg,
    usdHigh: high,
    usdLow: low,
    qtime
  };
}
__name(fetchCnyPrice, "fetchCnyPrice");
async function fetchRange(env, days = 30) {
  const { date } = bjNow();
  const key = `k2:${days}d:${date}`;
  if (env.GOLD_KV) {
    const cached = await env.GOLD_KV.get(key);
    if (cached) {
      try {
        return JSON.parse(cached);
      } catch (_) {
      }
    }
  }
  const resp = await fetch(KLINE_URL, {
    headers: { "User-Agent": "Mozilla/5.0", Referer: "https://finance.sina.com.cn/" }
  });
  if (!resp.ok) throw new Error("\u65E5K HTTP " + resp.status);
  const txt = await resp.text();
  const m = txt.match(/var t=\((.*)\)\s*;?\s*$/s);
  if (!m) throw new Error("\u65E5K \u89E3\u6790\u5931\u8D25");
  const arr = JSON.parse(m[1]);
  const seg = arr.slice(-days);
  const out = {
    loUsd: Math.min(...seg.map((x) => Number(x.low))),
    hiUsd: Math.max(...seg.map((x) => Number(x.high))),
    // 倒数第二个交易日的收盘（昨天的收盘价），用于"较昨收"
    prevCloseUsd: seg.length >= 2 ? Number(seg[seg.length - 2].close) : null
  };
  if (env.GOLD_KV) {
    await env.GOLD_KV.put(key, JSON.stringify(out), { expirationTtl: 86400 * 2 });
  }
  return out;
}
__name(fetchRange, "fetchRange");
function rangeCny(range, rate) {
  const k = rate / OZ_GRAMS;
  return {
    lo: range.loUsd * k,
    hi: range.hiUsd * k,
    prevClose: range.prevCloseUsd ? range.prevCloseUsd * k : null
  };
}
__name(rangeCny, "rangeCny");
async function loadState(env) {
  const empty = {
    date: "",
    minToday: null,
    lowAt: 0,
    lowValue: null,
    alerts: 0,
    lastAlertAt: 0,
    flags: [],
    hitLevels: [],
    // 今天已经提醒过的价位档（防止同一档反复推）
    summaries: []
    // 今天已经发过的简报时点（'15:00' 这类）
  };
  if (!env.GOLD_KV) return empty;
  try {
    const raw = await env.GOLD_KV.get("alert:state");
    return raw ? { ...empty, ...JSON.parse(raw) } : empty;
  } catch (_) {
    return empty;
  }
}
__name(loadState, "loadState");
async function saveState(env, state) {
  if (!env.GOLD_KV) return;
  await env.GOLD_KV.put("alert:state", JSON.stringify(state));
}
__name(saveState, "saveState");
async function pushPlus(env, title, content) {
  if (!env.PUSHPLUS_TOKEN) {
    console.log("[push] PUSHPLUS_TOKEN \u672A\u914D\u7F6E\uFF0C\u8DF3\u8FC7");
    return { ok: false, error: "no token" };
  }
  const resp = await fetch("https://www.pushplus.plus/send", {
    method: "POST",
    headers: { "Content-Type": "application/json", "User-Agent": "gold-price-worker" },
    body: JSON.stringify({
      token: env.PUSHPLUS_TOKEN,
      title,
      content,
      template: "txt"
    })
  });
  const text = await resp.text();
  let ok = false;
  try {
    ok = JSON.parse(text).code === 200;
  } catch (_) {
  }
  if (!ok) console.log("[push] \u5931\u8D25 " + text.slice(0, 200));
  return { ok, detail: text.slice(0, 200) };
}
__name(pushPlus, "pushPlus");
function bodyOf(p, range, rank, extra) {
  const lines = [];
  lines.push(`\u73B0\u4EF7 ${round2(p.cny).toFixed(2)} \u5143/\u514B`);
  lines.push(`\u542B\u624B\u7EED\u8D39 ${round2(p.cny + FEE_SPREAD).toFixed(2)}`);
  lines.push(`\u5206\u4F4D ${rank.toFixed(1)}%`);
  lines.push(`\u533A\u95F4 ${range.lo.toFixed(2)}~${range.hi.toFixed(2)}`);
  lines.push(`\u56FD\u9645\u91D1 ${p.usd.toFixed(0)} \u7F8E\u5143`);
  lines.push(`\u66F4\u65B0 ${(p.qtime || "").slice(0, 5) || "\u2014"}`);
  if (extra) {
    lines.push("");
    for (const l of String(extra).split("\n")) {
      if (l.trim()) lines.push(l);
    }
  }
  lines.push("");
  lines.push(`\u8DDD30\u65E5\u6700\u4F4E ${(p.cny - range.lo).toFixed(2)} \u5143`);
  return lines.join("\n");
}
__name(bodyOf, "bodyOf");
function decide(price, range, state, at) {
  const rank = (price.cny - range.lo) / (range.hi - range.lo) * 100;
  const canPush = state.alerts < MAX_ALERTS_PER_DAY && at - (state.lastAlertAt || 0) > COOLDOWN_MS;
  const crossed = PRICE_LEVELS.filter(
    (L) => price.cny <= L && !state.hitLevels.includes(L)
  );
  if (crossed.length && canPush) {
    const lowest = Math.min(...crossed);
    const skipped = crossed.filter((L) => L > lowest);
    const next = PRICE_LEVELS.filter((L) => L < lowest).sort((a, b) => b - a)[0];
    const extra = `\u8DCC\u7834 ${lowest} \u5143/\u514B` + (skipped.length ? `\uFF08\u4E00\u6B21\u8D8A\u8FC7 ${skipped.join(" / ")}\uFF09` : "") + (next ? `
\u4E0B\u4E00\u6863 ${next}\uFF0C\u8DDD\u73B0\u4EF7 ${(price.cny - next).toFixed(2)} \u5143` : "");
    return {
      type: "level",
      title: `\u{1F4B0} \u91D1\u4EF7\u8DCC\u7834 ${lowest}`,
      content: bodyOf(price, range, rank, extra),
      rank,
      levels: crossed
    };
  }
  if (state.lowValue && at - (state.lowAt || 0) < 24 * 3600 * 1e3 && price.cny >= state.lowValue * (1 + REBOUND_PCT / 100) && !state.flags.includes("rebound" + at_date()) && canPush) {
    const up = (price.cny - state.lowValue) / state.lowValue * 100;
    return {
      type: "rebound",
      title: "\u{1F4C8} \u4F4E\u70B9\u540E\u53CD\u5F39",
      content: bodyOf(
        price,
        range,
        rank,
        `\u4ECE\u4F4E\u70B9 ${state.lowValue.toFixed(2)} \u53CD\u5F39 ${up.toFixed(2)}%\uFF0C\u53EF\u80FD\u4F01\u7A33\u3002`
      ),
      rank
    };
  }
  return null;
}
__name(decide, "decide");
function digest(price, range, state, rank) {
  const parts = [];
  if (range.prevClose) {
    const d = price.cny - range.prevClose;
    const pct = d / range.prevClose * 100;
    parts.push(`\u8F83\u6628\u6536 ${d >= 0 ? "\u25B2" : "\u25BC"}${Math.abs(d).toFixed(2)}`);
    parts.push(`\u6DA8\u8DCC ${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`);
  }
  if (state.minToday !== null) {
    parts.push(`\u4ECA\u65E5\u6700\u4F4E ${state.minToday.toFixed(2)}`);
  }
  const next = PRICE_LEVELS.filter((L) => L < price.cny).sort((a, b) => b - a)[0];
  if (next) {
    parts.push(`\u4E0B\u4E00\u6863 ${next}`);
    parts.push(`\u8FD8\u5DEE ${(price.cny - next).toFixed(2)} \u5143`);
  }
  return bodyOf(price, range, rank, parts.join("\n"));
}
__name(digest, "digest");
function at_date() {
  return bjNow().date;
}
__name(at_date, "at_date");
async function alertTick(env) {
  const at = Date.now();
  const now = bjNow();
  if (!inPushWindow(now)) {
    return;
  }
  let price;
  try {
    price = await fetchCnyPrice();
  } catch (e) {
    console.log("[alert] \u53D6\u4EF7\u5931\u8D25 " + e.message);
    return;
  }
  let range;
  try {
    range = rangeCny(await fetchRange(env), price.rate);
  } catch (e) {
    console.log("[alert] \u53D6\u533A\u95F4\u5931\u8D25 " + e.message);
    return;
  }
  const state = await loadState(env);
  if (state.date !== now.date) {
    state.date = now.date;
    state.minToday = null;
    state.alerts = 0;
    state.flags = [];
    state.hitLevels = [];
    state.summaries = [];
  }
  if (state.minToday === null || price.cny < state.minToday) {
    state.minToday = round2(price.cny);
    state.lowAt = at;
    state.lowValue = round2(price.cny);
  }
  if (SUMMARY_SLOTS.includes(now.hhmm) && !state.summaries.includes(now.hhmm)) {
    state.summaries.push(now.hhmm);
    const rank = (price.cny - range.lo) / (range.hi - range.lo) * 100;
    await pushPlus(env, `\u{1F4CA} ${now.hhmm} \u91D1\u4EF7\u7B80\u62A5`, digest(price, range, state, rank));
    console.log(`[alert] summary ${now.hhmm}`);
  }
  const hit = decide(price, range, state, at);
  if (!hit) {
    await saveState(env, state);
    return;
  }
  const r = await pushPlus(env, hit.title, hit.content);
  if (r.ok) {
    state.alerts += 1;
    state.lastAlertAt = at;
    if (hit.type === "level" && hit.levels) {
      state.hitLevels = state.hitLevels.concat(hit.levels);
    }
    if (hit.type === "rebound") state.flags.push("rebound" + now.date);
    console.log(`[alert] pushed ${hit.type} ${now.ts}`);
  }
  await saveState(env, state);
}
__name(alertTick, "alertTick");
var corsFor = /* @__PURE__ */ __name((origin) => ({
  "Access-Control-Allow-Origin": origin,
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Max-Age": "86400"
}), "corsFor");
function json(obj, status, cors) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...cors, "Content-Type": "application/json; charset=utf-8" }
  });
}
__name(json, "json");
async function triggerWorkflow(env, cors) {
  if (!env.GH_TOKEN) return json({ ok: false, error: "GH_TOKEN \u672A\u914D\u7F6E" }, 400, cors);
  const repo = env.REPO || DEFAULTS.REPO;
  const wf = env.WORKFLOW || DEFAULTS.WORKFLOW;
  const ref = env.REF || DEFAULTS.REF;
  const resp = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${wf}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "gold-price-worker",
        "X-GitHub-Api-Version": "2022-11-28"
      },
      body: JSON.stringify({ ref })
    }
  );
  if (resp.status === 204) return json({ ok: true, repo, workflow: wf, ref }, 200, cors);
  return json({ ok: false, status: resp.status, detail: (await resp.text()).slice(0, 300) }, 502, cors);
}
__name(triggerWorkflow, "triggerWorkflow");
var dispatch_default = {
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
          console.log("[cron] alert \u5F02\u5E38 " + (e && e.message));
        }
        if (env.GH_TOKEN && minute % 15 === 0) {
          try {
            const r = await triggerWorkflow(env, {});
            console.log(`[cron ${at.ts}] dispatch ${r.status}`);
          } catch (e) {
            console.log("[cron] dispatch \u5F02\u5E38 " + (e && e.message));
          }
        }
      })()
    );
  },
  async fetch(request, env) {
    const origin = env.ALLOW_ORIGIN || "*";
    const cors = corsFor(origin);
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    const reqOrigin = request.headers.get("Origin");
    if (env.ALLOW_ORIGIN && reqOrigin && reqOrigin !== env.ALLOW_ORIGIN) {
      return json({ ok: false, error: "origin not allowed" }, 403, cors);
    }
    if (request.method === "GET") {
      if (url.pathname === "/status") {
        try {
          const price = await fetchCnyPrice();
          const range = rangeCny(await fetchRange(env), price.rate);
          const state = await loadState(env);
          const rank = (price.cny - range.lo) / (range.hi - range.lo) * 100;
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
              hitLevels: state.hitLevels,
              nextLevel: PRICE_LEVELS.filter((L) => L < price.cny).sort((a, b) => b - a)[0] || null,
              summariesToday: state.summaries,
              alertsToday: state.alerts,
              minToday: state.minToday
            },
            200,
            cors
          );
        } catch (e) {
          return json({ ok: false, error: String(e && e.message || e) }, 502, cors);
        }
      }
      if (url.pathname === "/price") {
        try {
          const p = await fetchCnyPrice();
          return json(
            { ok: true, at: bjNow().ts, cur: round2(p.cny), cny: round2(p.cny), usd: p.usd, rate: p.rate },
            200,
            cors
          );
        } catch (e) {
          return json({ ok: false, error: String(e && e.message || e) }, 502, cors);
        }
      }
      if (url.pathname === "/data") {
        const date = url.searchParams.get("date") || bjNow().date;
        const raw = env.GOLD_KV ? await env.GOLD_KV.get("d:" + date) : null;
        return new Response(`{"date":"${date}","points":${raw || "[]"}}`, {
          headers: { ...cors, "Content-Type": "application/json; charset=utf-8" }
        });
      }
      return json(
        {
          ok: true,
          service: "gold-price-worker",
          routes: ["GET /status", "GET /price", "GET /data?date=", "POST /test", "POST /"]
        },
        200,
        cors
      );
    }
    if (request.method === "POST") {
      if (url.pathname === "/test") {
        const r = await pushPlus(env, "\u{1F514} \u9EC4\u91D1\u63D0\u9192\xB7\u901A\u9053\u6D4B\u8BD5", "\u8FD9\u6761\u662F\u6D4B\u8BD5\u6D88\u606F\uFF0C\u6536\u5230\u8BF4\u660E\u63A8\u9001\u5DF2\u901A\u3002");
        return json({ ok: r.ok, detail: r.detail }, r.ok ? 200 : 502, cors);
      }
      return triggerWorkflow(env, cors);
    }
    return json({ ok: false, error: "method not allowed" }, 405, cors);
  }
};
export {
  bodyOf,
  decide,
  dispatch_default as default,
  inPushWindow
};
//# sourceMappingURL=dispatch.js.map
