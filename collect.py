#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国银行积存金 · 价格采集 + HTML 报表生成

数据来源：chnau99999.com（中国黄金销售终端报价，公开 JSON 接口）
用途：每日固定 3 个时点采样，追加写入 CSV，并生成一份可离线打开的单文件报表。

设计原则
  1. 只读公开报价，不登录、不碰账户、不做任何交易。
  2. 源站给的是"基准金价"，不是中行积存金官方报价；
     报表里中行价格一律标注"估算"，由标定偏移换算而来。
  3. 漏采、未更新都要留痕，不允许悄悄少一行当成"价格没动"。

用法
  python3 collect.py              # 采一个点 + 重生成报表（周末自动跳过）
  python3 collect.py --force      # 忽略周末/交易日判断，强制采
  python3 collect.py --report     # 不采集，只重生成报表
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")

with open(CONFIG_PATH, encoding="utf-8") as f:
    CFG = json.load(f)

# ---- 时点栅格：按 slot_step_minutes 生成，避免在配置里硬写 288 行。
#      5 分钟 → 00:00, 00:05, …, 23:55 共 288 个；改成 15 → 96 个，全部代码自适应。
if CFG.get("slot_step_minutes"):
    _step = int(CFG["slot_step_minutes"])
    if 1440 % _step:
        raise SystemExit(f"slot_step_minutes={_step} 不能整除 1440，一天切不匀")
    CFG["slots"] = [f"{m // 60:02d}:{m % 60:02d}" for m in range(0, 1440, _step)]

CSV_PATH = os.path.join(BASE, CFG["files"]["csv"])
REPORT_PATH = os.path.join(BASE, CFG["files"]["report"])
EXTREMES_PATH = os.path.join(BASE, CFG["files"]["extremes"])

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

HEADER = ["ts", "d", "slot", "cur", "sel", "buy", "acc",
          "est_buy", "est_sell", "status", "note", "src"]

# 数据来源分三类，报表里必须能区分，不能混着算：
#   live       = 真实请求拿到的报价（唯一可作为「分时实采」的证据）
#   carry      = 某时点没有采到，沿用最近一次真实报价补齐（估算，非实采）
#   sge_daily  = 用上金所日线回溯出的历史价
SRC_LIVE = "live"
SRC_CARRY = "carry"
SRC_DAILY = "sge_daily"

REAL_SRCS = (SRC_LIVE, SRC_DAILY)

SGE_URL = "https://www.sge.com.cn/graph/Dailyhq"

# 与报表 --bg 同色，用来挖空补齐点的圆心，做出「空心点」效果
CFG_HOLLOW_FILL = "#0f1115"


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- 采集

def fetch_quote(timeout=20):
    req = urllib.request.Request(
        CFG["source"]["url"],
        data=b"",
        method="POST",
        headers={
            "User-Agent": UA,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.chnau99999.com/page/goldPrice",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    obj = json.loads(raw)
    if obj.get("code") != 200:
        raise RuntimeError(f"接口返回异常: {raw[:200]}")
    data = obj["data"]
    out = {}
    for k in ("cur", "sel", "buy", "accessories"):
        v = data.get(k)
        out[k] = float(v) if v not in (None, "", "-") else None
    if out["cur"] is None:
        raise RuntimeError(f"缺少基础金价字段: {raw[:200]}")
    return out


def fetch_with_retry(attempts=3):
    delays = [3, 6, 10]
    last = None
    for i in range(attempts):
        try:
            return fetch_quote(), None
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"第 {i + 1}/{attempts} 次采集失败: {type(e).__name__}: {e}")
            if i < attempts - 1:
                time.sleep(delays[min(i, len(delays) - 1)])
    return None, last


def read_rows():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r.pop(None, None)                  # 旧文件多出的无名列，丢弃
        if not r.get("src"):
            r["src"] = SRC_LIVE            # 加 src 列之前的历史行，一律视为真采
    return rows


def migrate_csv():
    """表头缺 src 列时重写文件补齐，避免 DictWriter 写出错位。"""
    if not os.path.exists(CSV_PATH):
        return
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        r = csv.reader(f)
        head = next(r, [])
        body = [row for row in r if row]
    if head == HEADER:
        return
    fixed = []
    for row in body:
        row = row[:len(head)]
        while len(row) < len(head):
            row.append("")
        rec = dict(zip(head, row))
        rec.setdefault("src", SRC_LIVE)
        rec["src"] = rec.get("src") or SRC_LIVE
        fixed.append([rec.get(k, "") for k in HEADER])
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(fixed)
    log(f"CSV 表头已迁移：新增 src 列，{len(fixed)} 行标记为 {SRC_LIVE}。")


# ---------------------------------------------------------------- 历史回填

def fetch_sge_daily(timeout=30):
    """上金所 Au99.99 日线，返回 [(date, open, close, low, high)]，按日期升序。"""
    req = urllib.request.Request(
        SGE_URL,
        data=f"instid={CFG['sge']['instid']}".encode(),
        method="POST",
        headers={
            "User-Agent": UA,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.sge.com.cn/sjzx/mrhq",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    obj = json.loads(raw)
    out = []
    for r in obj.get("time", []):
        try:
            out.append((r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])))
        except (IndexError, TypeError, ValueError):
            continue
    if not out:
        raise RuntimeError(f"上金所日线为空: {raw[:200]}")
    return out


# ---------------------------------------------------------------- 日内极值

def load_extremes():
    """读极值文件，返回 {日期: {open, close, low, high, sync}}。缺失/损坏一律返回空 dict。"""
    if not os.path.exists(EXTREMES_PATH):
        return {}
    try:
        with open(EXTREMES_PATH, encoding="utf-8") as f:
            obj = json.load(f)
    except Exception as e:  # noqa: BLE001
        log(f"极值文件读取失败（按空处理）：{type(e).__name__}: {e}")
        return {}
    days = obj.get("days") if isinstance(obj, dict) else None
    return days if isinstance(days, dict) else {}


def save_extremes(days):
    keep = int(CFG["sge"].get("keep_days", 400))
    trimmed = dict(sorted(days.items())[-keep:])
    os.makedirs(os.path.dirname(EXTREMES_PATH) or ".", exist_ok=True)
    with open(EXTREMES_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "days": trimmed}, f, ensure_ascii=False, indent=1)
    return trimmed


def sync_extremes(quiet=False):
    """把上金所日线的当日开/收/最高/最低落盘。

    尽力而为：网络失败只记日志，绝不抛给调用方——日内极值只是锦上添花，
    不能因为它把每小时一次的采集主流程拖垮。
    """
    try:
        daily = fetch_sge_daily()
    except Exception as e:  # noqa: BLE001
        log(f"日内极值同步失败（不影响采集）：{type(e).__name__}: {e}")
        return None
    days = load_extremes()
    stamped = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for d, op, cl, lo, hi in daily:
        days[d] = {"open": op, "close": cl, "low": lo, "high": hi, "sync": stamped}
    trimmed = save_extremes(days)
    if not quiet:
        log(f"日内极值已同步 {len(trimmed)} 个交易日 → {os.path.basename(EXTREMES_PATH)}")
    return trimmed


def extreme_rows(n=15):
    """最近 n 个有极值记录的交易日，倒序。

    返回 [(d, open, close, low, high)]，全部是 SGE Au99.99 的原始价格。

    2026-10-31：原先还返回到「中行估算买入价」的换算值，用 offset_buy 平移。
    但用户只关心实时金价，且 SGE 口径与源站基准金价本就接近（实测差 0 元），
    多一层平移反而制造了「两张图不同尺度」的困惑，故一律用原始价。
    """
    days = load_extremes()
    out = []
    for d in sorted(days, reverse=True)[:n]:
        info = days[d]
        try:
            op = float(info["open"])
            cl = float(info["close"])
            lo = float(info["low"])
            hi = float(info["high"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((d, op, cl, lo, hi))
    return out


def backfill(days=30):
    """用日线回填过去 N 个自然日的标准时点。

    09:32 取当日开盘价，其余时点取当日收盘价——都是日线里真实存在的数，
    不插值、不编造。所有行 src=sge_daily，报表会单独标注为「日线回溯」。
    """
    migrate_csv()
    daily = fetch_sge_daily()
    cal = CFG["calibration"]
    slots = CFG["slots"]
    today = date.today()
    floor = today - __import__("datetime").timedelta(days=days)

    rows = read_rows()
    have = {(r["d"], r["slot"]) for r in rows}

    added = skipped = 0
    for d, op, cl, _lo, _hi in daily:
        dd = date.fromisoformat(d)
        if dd < floor or dd >= today:       # 不含今天，今天留给实时采集
            continue
        if dd.weekday() >= 5:               # 周末不采，和实时口径一致
            continue
        for s in slots:
            if (d, s) in have:
                skipped += 1
                continue
            base = op if s == slots[0] else cl
            append_row({
                "ts": f"{d} {s}:00",
                "d": d,
                "slot": s,
                "cur": f"{base:.2f}",
                "sel": "", "buy": "", "acc": "",
                "est_buy": f"{base + cal['offset_buy']:.2f}",
                "est_sell": f"{base + cal['offset_sell']:.2f}",
                "status": "ok",
                "note": "日线回溯" + ("·开盘" if s == slots[0] else "·收盘"),
                "src": SRC_DAILY,
            })
            have.add((d, s))
            added += 1

    log(f"日线回填完成：新增 {added} 行，跳过已存在 {skipped} 行（区间 {floor} ~ {today}，不含今天）。")
    return added


def append_row(row):
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    row.setdefault("src", SRC_LIVE)
    new_file = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow(row)


def write_rows(rows):
    """整表重写（只在清理旧的补齐行时用；平时一律 append，CSV 是只追加的账本）。"""
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in HEADER})


def nominal_time(r):
    """这条记录「代表哪个时刻」。

    slot 字段本身就是 HH:MM（如历史遗留的 09:32、14:50）就用它——它才是语义上的时点；
    slot 是「手动」这类非时间值时，才退化为按实际采集时刻 r["ts"] 归位。
    两者混用会错位：11:32 那行的 ts 记的是 12:21（补采时刻），拿 ts 归位会跑到 12:00。
    """
    s = (r.get("slot") or "").strip()
    if len(s) == 5 and s[2] == ":":
        return s
    return r["ts"][11:16]


def real_slot_of(r, slots=None):
    """一条真实记录该落在哪个标准整点：09:32→09:00，14:50→14:00，22:43→22:00。

    早于首个整点（如 08:xx）返回 None。
    """
    slots = slots or CFG["slots"]
    if r.get("slot") in slots:
        return r["slot"]
    hhmm = nominal_time(r)
    past = [s for s in slots if s <= hhmm]
    return past[-1] if past else None


def pick_slot(dt):
    """当前时刻归属哪个标准时点：取「已到点」的最后一个整点。

    22:39 归 22:00，09:05 归 09:00；早于首点时点（如 08:30）返回 None。
    """
    hhmm = dt.strftime("%H:%M")
    if hhmm in CFG["slots"]:
        return hhmm
    past = [s for s in CFG["slots"] if s <= hhmm]
    return past[-1] if past else None


def collect(force=False, auto=False):
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    slots = CFG["slots"]

    if CFG.get("weekdays_only") and now.weekday() >= 5 and not force:
        log("今天是周末，按配置跳过采集。")
        return None

    start, end = CFG["trading_window"]
    grace = CFG.get("tail_grace", end)
    tail = slots[-1]
    in_window = start <= hhmm <= end

    # ---- 先算「该不该发请求」，再决定联网。
    # 只有 live / sge_daily 算真实记录；carry 行是补齐用的估算，不能拿来挡真实采集。
    rows = read_rows()
    real_slots = {r["slot"] for r in rows
                  if r["d"] == today_str and r.get("status") == "ok"
                  and r.get("src", SRC_LIVE) in REAL_SRCS}

    slot = pick_slot(now)

    # ---- 时段判定：窗口之外，只有「收盘后宽限」允许补最后一个时点
    if not in_window and not force:
        if hhmm < start:
            log(f"当前 {hhmm} 早于首个采集时点 {start}，跳过采集。")
            return None
        if not (end < hhmm <= grace and tail not in real_slots):
            log(f"当前 {hhmm} 不在采集窗口 {start}-{end}"
                f"（收尾补采宽限至 {grace}），跳过采集。")
            return None
        slot = tail
        log(f"{hhmm} 已过收盘，进入补采宽限（至 {grace}），只补最后一个时点 {tail}。")

    if slot is None:
        log(f"当前 {hhmm} 早于首个采集时点 {start}，跳过采集。")
        return None

    # ---- 一次真实请求只落一行真实数据，落在「已到的最后一个整点」上。
    # 中间没开机的时点不在采集时硬塞同一个价（那会造出一串假的分时点），
    # 改由 fill_today() 在报表层用「沿用最近报价」补，并明确标成 carry。
    dup = slot in real_slots
    if dup and auto:
        log(f"{slot} 时点今天已有真实记录，自动模式跳过（不发请求）。")
        return None
    book_slot = "手动" if dup else slot

    data, err = fetch_with_retry()
    cal = CFG["calibration"]
    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    if data is None:
        append_row({
            "ts": ts, "d": today_str, "slot": book_slot,
            "cur": "", "sel": "", "buy": "", "acc": "",
            "est_buy": "", "est_sell": "",
            "status": "失败", "note": f"{type(err).__name__}: {str(err)[:120]}",
            "src": SRC_LIVE,
        })
        log(f"采集失败（{book_slot}），已记录失败行：{type(err).__name__}: {str(err)[:120]}")
        return None

    est_buy = round(data["cur"] + cal["offset_buy"], 2)
    est_sell = round(data["cur"] + cal["offset_sell"], 2)
    row = {
        "ts": ts, "d": today_str, "slot": book_slot,
        "cur": f'{data["cur"]:.2f}',
        "sel": "" if data["sel"] is None else f'{data["sel"]:.2f}',
        "buy": "" if data["buy"] is None else f'{data["buy"]:.2f}',
        "acc": "" if data["accessories"] is None else f'{data["accessories"]:.2f}',
        "est_buy": f"{est_buy:.2f}",
        "est_sell": f"{est_sell:.2f}",
        "status": "ok",
        "note": "" if book_slot == hhmm else f"{hhmm} 取值",
        "src": SRC_LIVE,
    }
    append_row(row)
    # est_buy / est_sell 仍写进 CSV（历史列不能断档），但已不参与任何展示与指标，
    # 所以日志只报实时金价，免得看日志的人以为报表还在算银行价。
    log(f"已记录 {book_slot}（{hhmm} 实采）：实时金价 {data['cur']:.2f}")
    sync_extremes()
    return row


def fill_today(dry=False):
    """把今天缺失的标准时点用「最近的已知报价」补齐，写 src=carry。

    补值规则（2026-10-31 按用户要求定稿）：**用最近的一条真实样本**——

    * 该时点之前已有真实记录 → 沿用「之前最后一个」（前向沿用，note：沿用 HH:MM 报价）；
    * 该时点之前还没有任何真实记录 → 用「之后最早的那条」回填（note：回填 HH:MM 报价）。

    两种都标成 carry，报表里灰色显示、曲线上画空心，与实采区分。回填那一种本质是
    用后验数据补前段，所以 note 特意写作「回填」而不是「沿用」，一眼能看出性质不同。

    唯一不补的是**还没到的时点**（s > 当前时刻）——那会凭空写出未来价格。

    carry 行不是真实采样，日线/收盘价也不认它。
    """
    rows = read_rows()
    today = date.today().strftime("%Y-%m-%d")
    slots = CFG["slots"]

    old_carry = [r for r in rows if r["d"] == today and r.get("src") == SRC_CARRY]
    kept = [r for r in rows if not (r["d"] == today and r.get("src") == SRC_CARRY)]

    real = sorted([r for r in kept
                   if r["d"] == today and r.get("status") == "ok"
                   and r.get("src", SRC_LIVE) in REAL_SRCS],
                  key=lambda r: (nominal_time(r), r["ts"]))
    if not real:
        return 0

    # 真实记录按整点归位后占用的时点，不再补
    have = {s for s in (real_slot_of(r, slots) for r in real) if s}
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    now_hhmm = now.strftime("%H:%M")

    def plan_row(s, picked):
        return {
            "ts": now_str, "d": today, "slot": s,
            "cur": picked["cur"], "sel": picked.get("sel", ""),
            "buy": picked.get("buy", ""), "acc": picked.get("acc", ""),
            "est_buy": picked["est_buy"], "est_sell": picked["est_sell"],
            "status": "ok",
            "note": f"沿用 {nominal_time(picked)} 报价",
            "src": SRC_CARRY,
        }

    plan = []
    for s in slots:
        if s in have:
            continue
        if s > now_hhmm:
            # 还没到的时点一律不补。12:05 就把 13:00~23:00 填上，等于凭空写出未来价格，
            # 和「用未来价回填过去」是同一种错，只是方向相反。
            continue
        picked = None
        for r in real:                       # 前向沿用：取名义时刻 <= s 的最后一条
            if nominal_time(r) <= s:
                picked = r
            else:
                break
        if picked is None:
            # 早于当天第一条真实记录：往前没有可沿用的价，按用户要求用「最近的新数据」回填，
            # 也就是当天最早的那条真实记录（比此时点更晚）。
            # 这属于「用后验数据补前段」，note 里用「回填」而不是「沿用」把性质标清楚。
            picked = real[0]
            row = plan_row(s, picked)
            row["note"] = f"回填 {nominal_time(picked)} 报价"
            plan.append(row)
            continue
        plan.append(plan_row(s, picked))

    # ---- 与已有补齐行逐字段比对：完全一致就一个字节都不动。
    # 否则每次定时任务跑一遍都会重写 ts，CSV 指纹一直变，资料库会被无谓地推 14 次/天。
    def sig(r):
        return (r.get("slot"), r.get("cur"), r.get("est_buy"),
                r.get("est_sell"), r.get("note"))

    if old_carry and len(old_carry) == len(plan) \
            and {sig(r) for r in old_carry} == {sig(r) for r in plan}:
        log(f"今日补齐行无变化（{len(plan)} 个沿用点），跳过重写。")
        return 0

    if old_carry:
        if not dry:
            write_rows(kept)
        log(f"清理今日旧补齐行 {len(old_carry)} 条，按最新真实数据重算。")

    added = 0
    for row in plan:
        s = row["slot"]
        if dry:
            log(f"[dry] {s} ← {row['note']} 的 {row['cur']}")
        else:
            append_row(row)
        added += 1
    if added and not dry:
        log(f"今日补齐：{added} 个时点用最近报价填充（src=carry，非实采）。")
    elif not added:
        log("今日无缺失时点，无需补齐。")
    return added


# ---------------------------------------------------------------- 报表

def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt(x, nd=2):
    return "—" if x is None else f"{x:,.{nd}f}"


def label_indices(labels, max_labels=12):
    """挑横轴要标出来的下标。

    5 分钟粒度下 n 可能到 288，等间距取会得到 02:25 / 04:50 这种丑标签，
    所以优先只标整点（MM:00），整点太多时再隔几个取。日期型标签走等间距。
    """
    n = len(labels)
    if n <= max_labels:
        return set(range(n))
    hourly = [i for i, l in enumerate(labels) if l.endswith(":00")]
    if hourly:
        k = max(1, (len(hourly) + max_labels - 1) // max_labels)
        keep = set(hourly[::k])
        keep.add(n - 1)                      # 末尾一定标出来
        return keep
    step = max(1, (n + max_labels - 1) // max_labels)
    return set(range(0, n, step)) | {n - 1}


def svg_chart(points, ref=None, width=760, height=220, color="#e0b354", unit="元/克",
              hollow=None):
    """points: [(label, value|None)]；hollow: 同长度的布尔表，True 表示该点是补齐值。

    补齐点画空心灰环且不标数值——它不是采到的价，标上数字会让人误以为是实测。
    """
    pts = [(i, lbl, v) for i, (lbl, v) in enumerate(points) if v is not None]
    if len(pts) < 2:
        return ('<div class="empty">点数不足 2 个，暂不绘图。'
                '等积累到多个时点后自动出现。</div>')

    vals = [v for _, _, v in pts]
    lo, hi = min(vals), max(vals)
    if hi - lo < 0.02:          # 全平时给一点可视高度，避免退化成一条线
        lo, hi = lo - 0.5, hi + 0.5
    span = hi - lo
    lo -= span * 0.18
    hi += span * 0.18

    pad_l, pad_r, pad_t, pad_b = 58, 16, 16, 30
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(points)

    def x(i):
        return pad_l + (iw * i / max(n - 1, 1))

    def y(v):
        return pad_t + ih * (1 - (v - lo) / (hi - lo))

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" '
             f'preserveAspectRatio="xMidYMid meet" role="img">']
    for k in range(4):
        gv = lo + (hi - lo) * k / 3
        gy = y(gv)
        parts.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" '
                     f'y2="{gy:.1f}" stroke="#252a34" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{gy + 4:.1f}" class="ax" '
                     f'text-anchor="end">{gv:.2f}</text>')

    if ref is not None:
        if lo <= ref <= hi:
            ry = y(ref)
            parts.append(f'<line x1="{pad_l}" y1="{ry:.1f}" x2="{width - pad_r}" '
                         f'y2="{ry:.1f}" stroke="#7a6440" stroke-width="1.2" '
                         f'stroke-dasharray="5 4"/>')
            parts.append(f'<text x="{width - pad_r}" y="{ry - 6:.1f}" class="ax ref" '
                         f'text-anchor="end">目标 {ref:.2f}</text>')
        else:
            where = "高于" if ref > hi else "低于"
            parts.append(f'<text x="{width - pad_r}" y="{pad_t + 10}" class="ax ref" '
                         f'text-anchor="end">目标 {ref:.2f}（{where}本图范围）</text>')

    poly = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, _, v in pts)
    parts.append(f'<polyline points="{poly}" fill="none" stroke="{color}" '
                 f'stroke-width="2" stroke-linejoin="round"/>')
    hollow = hollow or []
    for i, lbl, v in pts:
        if i < len(hollow) and hollow[i]:
            parts.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="2.8" '
                         f'fill="{CFG_HOLLOW_FILL}" stroke="{color}" stroke-width="1.3" '
                         f'opacity=".6"/>')
            continue
        parts.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3.4" fill="{color}"/>')
        parts.append(f'<text x="{x(i):.1f}" y="{y(v) - 9:.1f}" class="val" '
                     f'text-anchor="middle">{v:.2f}</text>')

    keep = label_indices([lbl for lbl, _ in points])
    for i, lbl, _ in pts:
        if i not in keep:
            continue
        parts.append(f'<text x="{x(i):.1f}" y="{height - 8}" class="ax" '
                     f'text-anchor="middle">{lbl}</text>')

    parts.append(f'<text x="{pad_l}" y="{pad_t - 2}" class="ax">{unit}</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_multiline(labels, series, ref=None, width=760, height=240, unit="元/克",
                  hollow=None, band=None, empty=None):
    """多维度折线图：一个维度一条线、一种颜色，共用同一纵轴。

    labels : 横轴标签，长度即数据点数
    series : [(名称, 颜色, [值|None])]，一个维度一项
    hollow : 与 labels 等长的布尔表，True 表示该横轴点是沿用值 → 画空心灰环
    band   : (low_idx, high_idx, 填充色)，在两条线之间铺半透明色带（如日内最低~最高）
    每个点带 <title>，鼠标悬停可看到「维度 横轴：数值」。
    empty : 无数据可画时的占位文案，不传则用通用措辞。
    """
    n = len(labels)
    vals = [v for _, _, vs in series for v in vs if v is not None]
    if n < 2 or not vals:
        msg = empty or "点数不足或多维度均无数据，暂不绘图。"
        return f'<div class="empty">{msg}</div>'

    lo, hi = min(vals), max(vals)
    if hi - lo < 0.02:
        lo, hi = lo - 0.5, hi + 0.5
    span = hi - lo
    lo -= span * 0.18
    hi += span * 0.18

    pad_l, pad_r, pad_t, pad_b = 58, 16, 16, 30
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b

    def x(i):
        return pad_l + (iw * i / max(n - 1, 1))

    def y(v):
        return pad_t + ih * (1 - (v - lo) / (hi - lo))

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" '
             f'preserveAspectRatio="xMidYMid meet" role="img">']
    for k in range(4):
        gv = lo + (hi - lo) * k / 3
        gy = y(gv)
        parts.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" '
                     f'y2="{gy:.1f}" stroke="#252a34" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{gy + 4:.1f}" class="ax" '
                     f'text-anchor="end">{gv:.2f}</text>')

    if ref is not None:
        if lo <= ref <= hi:
            ry = y(ref)
            parts.append(f'<line x1="{pad_l}" y1="{ry:.1f}" x2="{width - pad_r}" '
                         f'y2="{ry:.1f}" stroke="#7a6440" stroke-width="1.2" '
                         f'stroke-dasharray="5 4"/>')
            parts.append(f'<text x="{width - pad_r}" y="{ry - 6:.1f}" class="ax ref" '
                         f'text-anchor="end">目标 {ref:.2f}</text>')
        else:
            where = "高于" if ref > hi else "低于"
            parts.append(f'<text x="{width - pad_r}" y="{pad_t + 10}" class="ax ref" '
                         f'text-anchor="end">目标 {ref:.2f}（{where}本图范围）</text>')

    # 区间色带：把「最低~最高」铺成一条带，看波动范围比看两条线更直观
    if band:
        li, hi_i, bc = band
        lowv = series[li][2]
        highv = series[hi_i][2]
        idx = [i for i in range(n)
               if lowv[i] is not None and highv[i] is not None]
        if len(idx) >= 2:
            pts = " ".join(f"{x(i):.1f},{y(highv[i]):.1f}" for i in idx)
            pts += " " + " ".join(f"{x(i):.1f},{y(lowv[i]):.1f}"
                                  for i in reversed(idx))
            parts.append(f'<polygon points="{pts}" fill="{bc}" opacity=".12"/>')

    hollow = hollow or []
    for name, color, vs in series:
        pts = [(i, v) for i, v in enumerate(vs) if v is not None]
        if len(pts) < 2:
            continue
        poly = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in pts)
        parts.append(f'<polyline points="{poly}" fill="none" stroke="{color}" '
                     f'stroke-width="1.9" stroke-linejoin="round" '
                     f'stroke-linecap="round"/>')
        for i, v in pts:
            tip = f"{name} {labels[i]}：{v:.2f}"
            if i < len(hollow) and hollow[i]:
                parts.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="2.6" '
                             f'fill="#0f1115" stroke="{color}" stroke-width="1.3" '
                             f'opacity=".65"><title>{tip}（沿用值）</title></circle>')
            else:
                parts.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3" '
                             f'fill="{color}"><title>{tip}</title></circle>')

    keep = label_indices(labels)
    for i, lbl in enumerate(labels):
        if i not in keep:
            continue
        parts.append(f'<text x="{x(i):.1f}" y="{height - 8}" class="ax" '
                     f'text-anchor="middle">{lbl}</text>')

    parts.append(f'<text x="{pad_l}" y="{pad_t - 2}" class="ax">{unit}</text>')
    parts.append("</svg>")

    items = []
    for name, color, vs in series:
        lastv = next((v for v in reversed(vs) if v is not None), None)
        vtxt = f"{lastv:.2f}" if lastv is not None else "—"
        items.append(f'<span class="lg"><i style="background:{color}"></i>'
                     f'{name} <b>{vtxt}</b></span>')
    return '<div class="legend">' + "".join(items) + "</div>" + "".join(parts)


REPORT_JS = """<script>
(function () {
  var btn = document.getElementById('btn-refresh');
  var toast = document.getElementById('toast');
  var meta = document.getElementById('meta');
  if (!btn) return;

  function say(msg, sticky) {
    toast.textContent = msg;
    toast.classList.add('show');
    if (!sticky) setTimeout(function () { toast.classList.remove('show'); }, 4500);
  }

  btn.addEventListener('click', function () {
    var u = new URL(location.href);
    u.searchParams.set('_', Date.now());          // 绕过 CDN / 浏览器缓存
    btn.disabled = true;
    say('正在获取云端最新版本…', true);

    fetch(u.toString(), { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      })
      .then(function (html) {
        var re = /生成于 (\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2})/;
        var m = html.match(re);
        var cur = (meta.textContent.match(re) || [])[1];
        if (m && m[1] !== cur) { location.replace(u.toString()); return; }
        btn.disabled = false;
        say(m ? ('已是最新：云端生成于 ' + m[1]) : '未取到新版本');
      })
      .catch(function (e) {
        btn.disabled = false;
        say('刷新失败：' + e.message + '（用 file:// 打开时无法联网，请改用 http 地址）');
      });
  });
})();
</script>"""


def build_report():
    rows = read_rows()
    target = CFG["target"]
    slots = CFG["slots"]
    names = CFG["slot_names"]
    step_min = int(CFG.get("slot_step_minutes") or 60)
    step_txt = f"每 {step_min} 分钟"
    repo_cfg = CFG.get("repo") or {}
    actions_url = (f"https://github.com/{repo_cfg['owner']}/{repo_cfg['name']}"
                   f"/actions/workflows/collect.yml") if repo_cfg else ""
    today = date.today().strftime("%Y-%m-%d")

    ok_rows = [r for r in rows if r.get("status") == "ok" and fnum(r.get("cur")) is not None]

    # ---- 今日：真实采集（live）与补齐（carry）严格分开，不能混在一起算「采集点数」
    day_rows_all = [r for r in ok_rows if r["d"] == today]
    today_rows = [r for r in day_rows_all if r.get("src", SRC_LIVE) != SRC_CARRY]
    today_rows.sort(key=lambda r: r["ts"])
    carry_map = {r["slot"]: r for r in day_rows_all if r.get("src") == SRC_CARRY}
    # 真实记录按整点归位占用槽位：09:32 归 09:00、14:50 归 14:00。
    # today_rows 已按 ts 升序，同一整点有多条时后写的（更新的）覆盖前者。
    today_map = {}
    for r in today_rows:
        s = real_slot_of(r, slots)
        if s:
            today_map[s] = r

    # 曲线横轴固定铺满标准整点（09:00…22:00），不再随采集时刻漂移。
    # 真实点实心、补齐点空心——一眼能看出哪个点是采出来的、哪个是沿用的。
    def slot_plot(s):
        r = today_map.get(s)
        if r and fnum(r["cur"]) is not None:
            return fnum(r["cur"]), False
        c = carry_map.get(s)
        if c and fnum(c["cur"]) is not None:
            return fnum(c["cur"]), True
        return None, False

    today_series = [(s, slot_plot(s)[0]) for s in slots]
    today_hollow = [slot_plot(s)[1] for s in slots]

    latest = today_rows[-1] if today_rows else (ok_rows[-1] if ok_rows else None)
    latest_is_today = bool(today_rows)

    # ---- 日线口径（2026-10-30 改）：当天<b>最后一个采集点</b>就是当日日线/收盘价。
    #      不再要求必须采到「尾盘时点」——否则漏采尾盘的日子会整段从日线图上消失。
    #      漏掉的中间时点会在下一次采集时用当时报价补齐，所以最后一条始终推进。
    tail_slot = CFG["slots"][-1]

    def pick_close(rows):
        """当天最后一个**真实**记录（按 ts 升序取末条）作为日线价。

        排除 src=carry 的补齐行：那是沿用前值的估算，拿来当收盘价等于凭空造数据。
        """
        if not rows:
            return None
        real = [r for r in rows if r.get("src", SRC_LIVE) != SRC_CARRY] or rows
        return sorted(real, key=lambda r: (r["ts"], r["slot"]))[-1]

    # ---- 昨日收盘（= 昨日最后一个采集点）
    prev_date = None
    for r in reversed(ok_rows):
        if r["d"] < today:
            prev_date = r["d"]
            break
    prev_rows = [r for r in ok_rows if r["d"] == prev_date] if prev_date else []
    prev_last = pick_close(prev_rows)
    prev_is_tail = bool(prev_last) and prev_last["slot"] == tail_slot

    # ---- 近 30 个交易日：日线价 = 当天最后一个采集点
    by_date = {}
    for r in ok_rows:
        by_date.setdefault(r["d"], []).append(r)
    dates = sorted(by_date)[-30:]
    daily_src = {dd: ("sge_daily" if all(r.get("src") == SRC_DAILY for r in by_date[dd])
                      else "live") for dd in dates}
    daily_pick = {}
    for dd in dates:
        cr = pick_close(by_date[dd])
        if cr:
            daily_pick[dd] = cr

    # 今日是否已定盘：采到尾盘时点才算收盘价，此前图上用的是「当前最新一条」，标 ~
    today_settled = bool(today_map.get(tail_slot))
    today_is_latest = (today in daily_pick) and (not today_settled)

    daily_series = [((dd[5:]
                      + ("*" if daily_src[dd] == "sge_daily" else "")
                      + ("~" if dd == today and today_is_latest else "")),
                     fnum(daily_pick[dd]["cur"])) for dd in dates if dd in daily_pick]

    # ---- 指标
    cur_price = fnum(latest["cur"]) if latest else None
    day_first = fnum(today_rows[0]["cur"]) if today_rows else None
    day_diff = (cur_price - day_first) if (cur_price is not None and day_first is not None) else None
    day_pct = (day_diff / day_first * 100) if (day_diff is not None and day_first) else None
    prev_price = fnum(prev_last["cur"]) if prev_last else None
    prev_diff = (cur_price - prev_price) if (cur_price is not None and prev_price is not None) else None
    prev_pct = (prev_diff / prev_price * 100) if (prev_diff is not None and prev_price) else None

    gap = (cur_price - target["price"]) if cur_price is not None else None

    # ---- 日内极值：上金所日线里当天<b>真实触及过</b>的最低/最高。
    #      比每小时若干次采样可靠得多——只要当天到过某个价，这里一定留痕。
    ex_rows = extreme_rows(15)
    today_ex = ex_rows[0] if (ex_rows and ex_rows[0][0] == today) else None
    ex_low = today_ex[3] if today_ex else None
    ex_high = today_ex[4] if today_ex else None
    ex_low_gap = (ex_low - target["price"]) if ex_low is not None else None
    ex_low_cls = "down" if (ex_low_gap is not None and ex_low_gap <= 0) else ""
    if today_ex:
        hit_txt = ("<b>已触及目标</b>" if (ex_low_gap or 1) <= 0
                   else f"距目标 +{ex_low_gap:.2f}")
        ex_low_sub = f"日内 {fmt(ex_low)} ~ {fmt(ex_high)} · {hit_txt}"
    else:
        ex_low_sub = "今日尚无极值记录（收盘后自动补齐）"

    ext_rows_html = []
    for d, op, cl, lo, hi in ex_rows:
        gp = lo - target["price"]
        hit = gp <= 0
        row_cls = ' class="hit"' if hit else ""
        today_tag = ' <span class="tag">今日</span>' if d == today else ""
        ext_rows_html.append(
            f"<tr{row_cls}><td>{d}{today_tag}</td>"
            f'<td class="num strong">{fmt(lo)}</td>'
            f'<td class="num">{fmt(hi)}</td>'
            f'<td class="num">{fmt(cl)}</td>'
            f'<td class="num {"down" if hit else "muted"}">{fmt(gp)}</td></tr>')
    ext_body = "".join(ext_rows_html)
    if not ext_body:
        ext_body = ('<tr><td colspan="5" class="muted">还没有极值记录，'
                    '跑一次 <code>python3 collect.py --sync-extremes</code> 拉取。</td></tr>')
    best_day = min(ex_rows, key=lambda x: x[3]) if ex_rows else None
    if best_day:
        b_gap = best_day[3] - target["price"]
        best_txt = (f"<b>{best_day[0]}</b> 是近 {len(ex_rows)} 个交易日里日内最低的一天："
                    f"<b>{fmt(best_day[3])}</b> 元/克，距目标 <b>+{b_gap:.2f}</b> 元。")
    else:
        best_txt = ""

    # 日内极值折线：最低 / 最高 / 收盘 三条线。ex_rows 是倒序（最新在前），这里转正序。
    # 配色沿用涨红跌绿：最高=红、最低=绿、收盘=金。
    ex_chrono = list(reversed(ex_rows))
    ex_labels = [d[5:] for d, *_ in ex_chrono]
    ex_chart = svg_multiline(
        ex_labels,
        [("日内最低", "#2f9e68", [x[3] for x in ex_chrono]),
         ("日内最高", "#e5484d", [x[4] for x in ex_chrono]),
         ("日线收盘", "#e0b354", [x[2] for x in ex_chrono])],
        ref=target["price"], height=240, band=(0, 1, "#8b949e"),
        empty="还没有极值记录，跑一次 "
              "<code>python3 collect.py --sync-extremes</code> 拉取。")

    # ---- 数据质量
    carry_rows = sorted(carry_map.values(), key=lambda r: r["slot"])
    fail_today = [r for r in rows if r["d"] == today and r.get("status") == "失败"]

    # 停更检测只看真实采集行：回填行的同日多点是同值，不能当停更证据
    live_rows = [r for r in ok_rows if r.get("src", SRC_LIVE) == SRC_LIVE]
    live_by_date = {}
    for r in live_rows:
        live_by_date.setdefault(r["d"], []).append(r)

    flat_days = 0
    for d in sorted(live_by_date, reverse=True):
        vals = [fnum(r["cur"]) for r in live_by_date[d] if fnum(r["cur"]) is not None]
        if len(set(vals)) == 1 and len(vals) >= 2:
            flat_days += 1
        else:
            break

    last_cur = fnum(live_rows[-1]["cur"]) if live_rows else None
    flat = 0
    if last_cur is not None:
        for r in reversed(live_rows):
            if fnum(r["cur"]) == last_cur:
                flat += 1
            else:
                break

    def cls(v):
        if v is None:
            return "muted"
        return "up" if v > 0 else ("down" if v < 0 else "muted")

    def arrow(v):
        if v is None:
            return ""
        return "▲" if v > 0 else ("▼" if v < 0 else "＝")

    # ---- 今日明细行：14 个整点全量铺开，真实 / 沿用 / 缺失 三态分明
    detail_rows = []
    shown = []                       # 已渲染的值，用于算「较上点」
    for s in slots:
        r = today_map.get(s)
        c = carry_map.get(s)
        if r:
            chg = ""
            if shown:
                d0 = fnum(r["cur"]) - shown[-1]
                chg = f'<span class="{cls(d0)}">{arrow(d0)} {d0:+.2f}</span>'
            shown.append(fnum(r["cur"]))
            nt = nominal_time(r)
            tsm = r["ts"][11:16]
            src_txt = f"{nt} 实采" if nt == tsm else f"{nt} · {tsm} 取值"
            detail_rows.append(
                f'<tr><td>{s}</td><td>{names.get(s, "")}</td>'
                f'<td class="num strong">{fmt(fnum(r["cur"]))}</td>'
                f'<td class="num">{chg}</td>'
                f'<td class="num muted">{src_txt}</td></tr>')
        elif c:
            v = fnum(c["cur"])
            chg = ""
            if shown and v is not None:
                d0 = v - shown[-1]
                chg = f'<span class="muted">{arrow(d0)} {d0:+.2f}</span>' if abs(d0) > 0.004 \
                    else '<span class="muted">＝</span>'
            if v is not None:
                shown.append(v)
            detail_rows.append(
                f'<tr class="carry"><td>{s}</td><td>{names.get(s, "")}</td>'
                f'<td class="num">{fmt(v)}</td>'
                f'<td class="num">{chg}</td>'
                f'<td class="num muted">{c.get("note") or "沿用"}</td></tr>')
        else:
            # 还没到点的时点留空，比编一个数诚实
            detail_rows.append(
                f'<tr class="miss"><td>{s}</td><td>{names.get(s, "")}</td>'
                f'<td class="num">—</td><td class="num">—</td>'
                f'<td class="num">—</td></tr>')

    # 归不到任何整点的记录（早于 09:00 的手动采集等）单独列在末尾
    for r in today_rows:
        if real_slot_of(r, slots) is not None:
            continue
        detail_rows.append(
            f'<tr class="extra"><td>{r["ts"][11:16]}</td><td>手动采集</td>'
            f'<td class="num strong">{fmt(fnum(r["cur"]))}</td>'
            f'<td class="num muted">—</td>'
            f'<td class="num muted">{r["ts"][11:]}</td></tr>')

    done_cnt = len([s for s in slots if s in today_map])

    # 今日明细折线：只画实时金价一条线（用户已不关心中行估算价）。
    detail_prices = []
    for s in slots:
        src = today_map.get(s) or carry_map.get(s)
        detail_prices.append(fnum(src["cur"]) if src else None)
    detail_chart = svg_multiline(
        list(slots),
        [("实时金价", "#e0b354", detail_prices)],
        ref=target["price"], height=250, hollow=today_hollow,
        empty="今天还没有采集到数据，首次采集后这里自动出现曲线。")

    carry_cnt = len([s for s in slots if s in carry_map])
    today_title = (f"今日分时 · 标准时点 {done_cnt}/{len(slots)} 实采"
                   + (f" · {carry_cnt} 个沿用" if carry_cnt else "")
                   + "（实时金价）"
                   if today_rows else "今日分时（尚未采集）")

    warn = []
    if not ok_rows:
        warn.append("还没有任何成功采集记录。先在交易时段跑一次 <code>python3 collect.py --force</code>。")
    if carry_rows:
        # 96 个时点全列出来会糊成一大段，只列前 10 个
        all_slots = [r["slot"] for r in carry_rows]
        shown = "、".join(all_slots[:10]) + (f" … 共 {len(all_slots)} 个" if len(all_slots) > 10 else "")
        # 区分「沿用」（该时点之前已有实采）和「回填」（之前还没有，用之后最早的样本）
        kinds = {}
        for r in carry_rows:
            k = (r.get("note") or "").split(" ")[0] or "补齐"
            kinds[k] = kinds.get(k, 0) + 1
        kinds_txt = "、".join(f"{k} {v} 个" for k, v in kinds.items())
        warn.append(
            f"今日有 <b>{len(carry_rows)}</b> 个时点没有真实采样（{shown}），"
            f"按「<b>最近的一条真实样本</b>」补齐（{kinds_txt}）："
            "<b>不是该时点的真实价格</b>，报表里灰色显示，日线与收盘价都不采用。"
            "想让这些点变成真值，只能在对应时点跑一次采集。")
    if fail_today:
        warn.append(f"今日有 {len(fail_today)} 次采集失败，明细见 CSV 的 note 列。")
    if today_is_latest:
        warn.append(f"今日尚未到 <b>{tail_slot}</b> 收尾时点，日线图上今天用的是"
                    f"<b>{today_rows[-1]['ts'][11:16]} 的最新实采价</b>（标 ~），还不是收盘价；"
                    f"{tail_slot} 采到后会自动推进为当日收盘价。")
    if flat >= 3:
        warn.append(f"基准金价已连续 {flat} 次未变化，源站可能未更新（该站在页面上不公布更新时间戳）。")
    if flat_days >= 2:
        warn.append(f"最近已有连续 {flat_days} 个交易日全天数值不变，值得怀疑数据源停更。")
    n_daily = sum(1 for r in ok_rows if r.get("src") == SRC_DAILY)
    n_daily_days = len({r["d"] for r in ok_rows if r.get("src") == SRC_DAILY})
    if n_daily:
        warn.append(
            f"历史区段含 <b>{n_daily}</b> 行日线回溯数据（共 {n_daily_days} 个交易日），"
            f"取自上海黄金交易所 Au99.99 日线：首点为当日<b>开盘</b>，其余时点为当日<b>收盘</b>。"
            f"这些点<b>不是分时实采</b>，同一天各时点数值相同属正常（均为当日收盘价），"
            f"勿读作「当天价格没动」。"
            f"图中带 <b>*</b> 的日期即回溯日。")
    if ex_rows:
        warn.append(
            "「日内极值」取自上海黄金交易所 Au99.99 日线，是原始价、未做任何换算；"
            + ("今日数据在收盘后由上金所更新，盘中看到的可能是截至当时的部分极值。"
               if today_ex else
               "今日暂无极值数据，收盘后自动补齐。"))

    warn_html = "".join(f"<li>{w}</li>" for w in warn)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>黄金实时金价报表 · {latest['d'] if latest else today}</title>
<style>
  :root{{--bg:#0f1115;--panel:#171a21;--panel2:#1d212a;--line:#252a34;
        --fg:#e9eaee;--muted:#98a0ad;--gold:#e0b354;--up:#e5484d;--down:#2f9e68;}}
  *{{box-sizing:border-box}}
  body{{margin:0;padding:28px 20px 48px;background:var(--bg);color:var(--fg);
       font:15px/1.65 -apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif;}}
  .wrap{{max-width:860px;margin:0 auto}}
  h1{{font-size:19px;margin:0 0 4px;font-weight:600;letter-spacing:.3px}}
  .sub{{color:var(--muted);font-size:13px;margin-bottom:22px}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}}
  .card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
  .card .k{{color:var(--muted);font-size:12px;letter-spacing:.4px}}
  .card .v{{font-size:26px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}}
  .card .d{{font-size:12.5px;color:var(--muted);margin-top:4px}}
  .up{{color:var(--up)}} .down{{color:var(--down)}} .muted{{color:var(--muted)}}
  section{{background:var(--panel);border:1px solid var(--line);border-radius:10px;
          padding:16px 18px;margin-bottom:16px}}
  h2{{font-size:14px;margin:0 0 12px;font-weight:600;color:var(--gold);letter-spacing:.4px}}
  .chart{{width:100%;height:auto;display:block}}
  .ax{{fill:var(--muted);font-size:11px}}
  .ax.ref{{fill:#b08d3f}}
  .val{{fill:var(--fg);font-size:11px}}
  table{{width:100%;border-collapse:collapse;font-size:13.5px}}
  th,td{{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left}}
  th{{color:var(--muted);font-weight:500;font-size:12px}}
  td.num{{text-align:right;font-variant-numeric:tabular-nums}}
  td.strong{{color:var(--gold);font-weight:600}}
  tr.miss td{{color:#6b7280}}
  tr.carry td{{color:#7f8797;background:#141821}}
  tr.extra td{{background:#1a1e26}}
  tr.hit td{{background:#16301f}}
  .tag{{background:#1f3a2b;color:#67d3a6;font-size:11px;padding:1px 5px;border-radius:4px}}
  tr:last-child td{{border-bottom:none}}
  ul{{margin:0;padding-left:18px;color:#c3c8d2;font-size:13.5px}}
  li{{margin:6px 0}}
  .empty{{color:var(--muted);font-size:13px;padding:22px 0;text-align:center}}
  .hint{{color:var(--muted);font-size:12px;margin:-6px 0 10px}}
  .legend{{display:flex;flex-wrap:wrap;gap:6px 16px;margin:0 0 10px;
          font-size:12px;color:var(--muted)}}
  .legend .lg{{display:inline-flex;align-items:center;gap:6px}}
  .legend i{{width:9px;height:9px;border-radius:2px;display:inline-block}}
  .legend b{{color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums}}
  details{{margin-top:12px;border-top:1px solid var(--line);padding-top:6px}}
  summary{{color:var(--muted);font-size:12.5px;cursor:pointer;padding:4px 0}}
  summary:hover{{color:var(--fg)}}
  .topbar{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap}}
  .acts{{display:flex;gap:8px;flex-wrap:wrap}}
  .btn{{font:inherit;font-size:13px;padding:7px 14px;border-radius:7px;cursor:pointer;
       border:1px solid var(--line);background:var(--panel2);color:var(--fg);
       text-decoration:none;display:inline-flex;align-items:center;gap:6px;transition:.15s}}
  .btn:hover{{border-color:var(--gold);color:var(--gold)}}
  .btn:disabled{{opacity:.55;cursor:default}}
  .btn.primary{{background:linear-gradient(180deg,#3b3120,#2a2418);
               border-color:#6b5327;color:var(--gold)}}
  .toast{{position:fixed;left:50%;bottom:26px;transform:translate(-50%,20px);opacity:0;
         pointer-events:none;background:#22262f;border:1px solid var(--line);color:var(--fg);
         font-size:13px;padding:9px 16px;border-radius:8px;transition:.2s;z-index:9;
         max-width:80vw;text-align:center}}
  .toast.show{{opacity:1;transform:translate(-50%,0)}}
  code{{background:var(--panel2);padding:1px 5px;border-radius:4px;font-size:12.5px}}
  footer{{color:#6b7280;font-size:12px;margin-top:24px;line-height:1.7}}
</style></head><body><div class="wrap">
<div class="topbar">
  <div>
    <h1>黄金实时金价</h1>
    <div class="sub" id="meta">数据日 {latest['d'] if latest else today} · 生成于 {datetime.now():%Y-%m-%d %H:%M} ·
来源 {CFG['source']['name']}</div>
  </div>
  <div class="acts">
    <button class="btn primary" id="btn-refresh" type="button">↻ 刷新到最新</button>
    {'<a class="btn" href="' + actions_url + '" target="_blank" rel="noopener">⚡ 立即采集</a>' if actions_url else ''}
  </div>
</div>
<div class="toast" id="toast"></div>

<div class="cards">
  <div class="card"><div class="k">实时金价（元/克）</div>
    <div class="v">{fmt(cur_price)}</div>
    <div class="d">{'今日' if latest_is_today else '最近一次'} · {latest['ts'][11:16] if latest else '—'} 采集</div></div>
  <div class="card"><div class="k">今日较首点</div>
    <div class="v {cls(day_diff)}">{arrow(day_diff)} {fmt(abs(day_diff) if day_diff is not None else None)}</div>
    <div class="d">{'—' if day_pct is None else f'{day_pct:+.2f}%'} · 首点 {fmt(day_first)}</div></div>
  <div class="card"><div class="k">较上一交易日</div>
    <div class="v {cls(prev_diff)}">{arrow(prev_diff)} {fmt(abs(prev_diff) if prev_diff is not None else None)}</div>
    <div class="d">{'—' if prev_pct is None else f'{prev_pct:+.2f}%'} · 上日{'收盘' if prev_is_tail else '末点'} {fmt(prev_price)}</div></div>
  <div class="card"><div class="k">距目标 {target['price']:.2f}</div>
    <div class="v {'up' if (gap or 0) > 0 else 'down'}">{fmt(gap)}</div>
    <div class="d">{'仍高于目标' if (gap or 0) > 0 else '已低于目标'} · 目标 {target['price']:.2f} 元/克</div></div>
  <div class="card"><div class="k">今日日内最低（实时）</div>
    <div class="v {ex_low_cls}">{fmt(ex_low)}</div>
    <div class="d">{ex_low_sub}</div></div>
</div>

<section><h2>{today_title}</h2>
<div class="hint">横轴固定为 <b>{slots[0]} ~ {tail_slot}</b> 的每一个 {step_txt} 时点，共 {len(slots)} 个。
金色<b>实心点</b>＝真实采集；<b>空心点</b>＝该时点没采到、按最近一条真实样本补的值（不标数值）。</div>
{svg_chart(today_series, ref=target['price'], hollow=today_hollow)}</section>

<section><h2>近 {len(daily_series)} 个交易日 · 日线（当天最后一个采集点即收盘价）</h2>
<div class="hint">每天<b>最后一个采集点</b>作为当日收盘价，漏掉的中间时点会被下一次采集自动补齐；采到 <b>{tail_slot}</b> 尾盘即视为定盘，此前用最新一条并标 <b>~</b>{' · 带 * 的日期为日线回溯（收盘价），非分时实采' if n_daily else ''}</div>
{svg_chart(daily_series, ref=target['price'], height=200)}</section>

<section><h2>日内极值 · 近 {len(ex_rows)} 个交易日</h2>
<div class="hint">取自上海黄金交易所 Au99.99 日线，是当天<b>真实触及过</b>的最低/最高价，原始价、不做换算。它比每 {step_min} 分钟一次采样可靠——哪怕低点只闪了一瞬间也会留痕。<br>
灰带＝当日<b>最低~最高</b>的波动区间，带子越宽当天波动越大；虚线为 {target['price']:.2f} 目标价。鼠标悬停任一点可看数值。<br>
（上金所日线与本表统计口径不同步，极端情况下可能出现「当前价低于今日日内最低」，以本表的日线口径为准。）{best_txt}</div>
{ex_chart}
<details><summary>展开逐日数值表（最低 / 最高 / 收盘 / 距目标）</summary>
<table><thead><tr><th>日期</th><th style="text-align:right">日内最低</th><th style="text-align:right">日内最高</th><th style="text-align:right">日线收盘</th><th style="text-align:right">最低距目标</th></tr></thead>
<tbody>{ext_body}</tbody></table></details></section>

<section><h2>今日明细 · {slots[0]} ~ {tail_slot} {step_txt}一个点</h2>
<div class="hint">金色线＝<b>实时金价</b>（源站基准价，元/克）；虚线为 {target['price']:.2f} 目标价。<br>
<b>实心点</b>＝该时点真实采集；<b>空心点</b>＝没采到、按最近一条真实样本补的，<b>不是该时点的真实价格</b>。
点太密时悬停任意一点仍可看数值。</div>
{detail_chart}
<details><summary>展开逐时点数值表（含来源与较上点）</summary>
<table><thead><tr><th>时点</th><th>名称</th><th style="text-align:right">实时金价</th>
<th style="text-align:right">较上点</th><th style="text-align:right">来源</th></tr></thead>
<tbody>{''.join(detail_rows)}</tbody></table></details></section>

<section><h2>数据质量与说明</h2><ul>{warn_html}</ul></section>

<footer>
价格来源：{CFG['source']['url']}（公开报价接口，每天 {slots[0]}~{tail_slot} {step_txt}采集，共 {len(slots)} 个时点）。<br>
报表展示的是源站<b>基准金价</b>原值，不做任何换算；日内极值来自上海黄金交易所 Au99.99 日线，两者口径可能略有差异。<br>
本报表仅供个人记录与复盘，不构成投资建议，也不保证数据源持续可用或数值准确。
</footer>
</div>{REPORT_JS}</body></html>
"""

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"报表已生成：{REPORT_PATH}")


def main():
    ap = argparse.ArgumentParser(description="积存金价格采集与报表")
    ap.add_argument("--force", action="store_true", help="忽略周末与交易时段判断")
    ap.add_argument("--auto", action="store_true",
                    help="自动模式：非标准时点且无缺口时静默跳过，不留「手动」行")
    ap.add_argument("--report", action="store_true", help="只重生成报表，不采集")
    ap.add_argument("--backfill", type=int, metavar="N",
                    help="用上金所日线回填过去 N 个自然日的标准时点（不含今天）")
    ap.add_argument("--sync-extremes", action="store_true",
                    help="只同步上金所日内开/收/最高/最低并重建报表，不做分时采集")
    ap.add_argument("--fill-today", action="store_true",
                    help="不采集，只把今天缺失的时点用最近一条真实样本补齐并重建报表")
    ap.add_argument("--no-fill", action="store_true",
                    help="采集后不自动补齐今日缺失时点")
    args = ap.parse_args()

    migrate_csv()
    if args.backfill:
        backfill(days=args.backfill)
    else:
        if args.sync_extremes:
            sync_extremes()
        elif not args.report and not args.fill_today:
            collect(force=args.force, auto=args.auto)
        if not args.no_fill:
            fill_today()
    build_report()


if __name__ == "__main__":
    sys.exit(main())
