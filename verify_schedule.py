#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""排期回归测试：09:00–22:00 每小时采集 / 缺口按最近报价补齐 / 日线取当天最后一个真实点。

用法：
    python3 verify_schedule.py

完全隔离：自动在 /tmp 下复制一份项目副本再跑，不碰真实的 CSV 与报表。
时间用 monkeypatch 控制、报价固定返回，全程不发网络请求。
改完 config.json 的 slots / trading_window / tail_grace 之后跑一遍最有用。

当前语义（2026-10-30 改）：
  * 一次真实请求只落一行 live 数据，落在「已到的最后一个整点」上；
  * 中间没开机的整点由 fill_today() 用「前向沿用最近实采价」补成 carry 行；
  * carry 行只用于让曲线连续，日线/收盘价不认它。
"""

import csv
import datetime
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_QUOTE = 855.0            # 仿真用的固定基准金价
FAKE = None


def last_weekday():
    """最近一个工作日（周一~周五），保证不会被 weekdays_only 跳过。"""
    d = datetime.date.today()
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d.isoformat()


def make_sandbox():
    dst = tempfile.mkdtemp(prefix="gold_verify_")
    for name in ("collect.py", "config.json"):
        shutil.copy(os.path.join(HERE, name), dst)
    shutil.copytree(os.path.join(HERE, "data"), os.path.join(dst, "data"))
    return dst


SANDBOX = make_sandbox()
sys.path.insert(0, SANDBOX)
import collect  # noqa: E402

TODAY = last_weekday()


class FakeDT(datetime.datetime):
    """让 collect.py 里的 datetime.now() 返回受控时间。"""

    @classmethod
    def now(cls, tz=None):
        return FAKE


class FakeDate(datetime.date):
    @classmethod
    def today(cls):
        return datetime.date.fromisoformat(TODAY)


collect.datetime = FakeDT
collect.date = FakeDate
collect.fetch_quote = lambda timeout=20: {"cur": TEST_QUOTE, "sel": 871.0,
                                          "buy": 852.0, "accessories": 1198.0}
collect.sync_extremes = lambda quiet=False: None


def set_now(hhmm):
    global FAKE
    FAKE = datetime.datetime.strptime(f"{TODAY} {hhmm}", "%Y-%m-%d %H:%M")


def today_rows():
    with open(collect.CSV_PATH, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["d"] == TODAY]


def live_slots():
    return sorted(r["slot"] for r in today_rows()
                  if r.get("src", "live") in collect.REAL_SRCS)


def carry_slots():
    return sorted(r["slot"] for r in today_rows() if r.get("src") == "carry")


def clear_today():
    lines = [l for l in open(collect.CSV_PATH, encoding="utf-8").read().splitlines() if l]
    head, body = lines[0], [l for l in lines[1:] if not l.startswith(TODAY)]
    with open(collect.CSV_PATH, "w", encoding="utf-8") as f:
        f.write(head + "\n" + "\n".join(body) + ("\n" if body else ""))


def run(hhmm):
    set_now(hhmm)
    collect.collect(force=False, auto=True)
    return live_slots()


def head(t):
    print(f"\n--- {t} ---")


PROGRESS = []
slots = collect.CFG["slots"]
tail = slots[-1]

head(f"仿真日 {TODAY}（{datetime.date.fromisoformat(TODAY).strftime('%A')}）")
print(f"沙箱: {SANDBOX}")
print(f"标准时点: {slots[0]} … {tail} 共 {len(slots)} 个")

head(f"场景1：08:00 触发（早于首个时点 {slots[0]}）")
clear_today()
got = run("08:00")
PROGRESS.append(("开盘前不采集", got == [], got))

head("场景2：09:05 首次触发 → 落在 09:00")
got = run("09:05")
PROGRESS.append(("首点写入 09:00", got == ["09:00"], got))

head("场景3：10:05 触发 → 落在 10:00")
got = run("10:05")
PROGRESS.append(("逐小时推进写入 10:00", got == ["09:00", "10:00"], got))

head("场景4：10:40 重复触发（10:00 已有真实记录）")
before = len(today_rows())
got = run("10:40")
PROGRESS.append(("同一整点不重复写入", len(today_rows()) == before, len(today_rows())))

head("场景5：13:40 才开机（11:00/12:00/13:00 都没采）")
got = run("13:40")
PROGRESS.append(("只补当前整点 13:00，不伪造 11/12 点",
                 got == ["09:00", "10:00", "13:00"], got))

head("场景6：fill_today 补齐缺口")
collect.fill_today()
c = carry_slots()
PROGRESS.append(("11:00/12:00 被补成 carry",
                 {"11:00", "12:00"} <= set(c), c))
PROGRESS.append(("补齐行标记为 carry",
                 all(r.get("src") == "carry" for r in today_rows()
                     if r["slot"] in ("11:00", "12:00")), c))

head("场景7：fill_today 幂等（连跑两次结果不变）")
n1 = len(today_rows())
collect.fill_today()
PROGRESS.append(("重复补齐不产生新行", len(today_rows()) == n1, len(today_rows())))

head("场景8：15:05 / 22:05 继续推进")
run("15:05")
got = run("22:05")
PROGRESS.append((f"收尾时点 {tail} 可被采到", tail in got, got))
collect.fill_today()

head("场景9：22:40 再触发（收尾点已有、超出宽限）")
before = len(today_rows())
run("22:40")
PROGRESS.append(("收尾后不再写入", len(today_rows()) == before, len(today_rows())))

head("场景10：整天没开机，22:20 一次性补")
clear_today()
got = run("22:20")
PROGRESS.append(("整天只采一次也有收尾点", got == [tail], got))
collect.fill_today()
covered = {r["slot"] for r in today_rows()}
PROGRESS.append(("14 个整点全部有值", len(covered) == len(slots), sorted(covered)))

head("报表校验")
set_now("22:30")
collect.build_report()
html = open(collect.REPORT_PATH, encoding="utf-8").read()
intraday = re.search(r"<h2>今日分时.*?</svg>", html, re.S).group(0)
circles = len(re.findall(r"<circle", intraday))
PROGRESS.append(("今日曲线铺满 14 个整点", circles == len(slots), circles))
PROGRESS.append(("今日曲线区分实心/空心点",
                 len(re.findall(r'r="3.4"', intraday)) >= 1
                 and len(re.findall(r'r="2.8"', intraday)) >= 1,
                 (len(re.findall(r'r="3.4"', intraday)),
                  len(re.findall(r'r="2.8"', intraday)))))
det = re.search(r"<h2>今日明细.*?</tbody>", html, re.S).group(0)
n_detail = len([1 for m in re.findall(r'<tr(?: class="\w+")?>(.*?)</tr>', det, re.S)
                if len(re.findall(r"<td[^>]*>", m)) >= 7])
PROGRESS.append(("今日明细 14 行", n_detail == len(slots), n_detail))
PROGRESS.append(("日内极值区块已渲染",
                 bool(re.search(r"<h2>日内极值[^<]*</h2>", html)), True))
PROGRESS.append(("日线图包含当天", TODAY[5:] in html, TODAY[5:]))

head("结果")
ok = True
for name, passed, detail in PROGRESS:
    flag = "PASS" if passed else "FAIL"
    ok = ok and passed
    print(f"  [{flag}] {name}")
    if not passed:
        print(f"         实际: {detail}")

print("\n今日最终明细：")
for x in sorted(today_rows(), key=lambda r: r["slot"]):
    print(f"  {x['slot']:<6} src={x['src']:<8} est_buy={x['est_buy']:<8} {x['note']}")

try:
    shutil.rmtree(SANDBOX)
    print(f"\n已清理沙箱: {SANDBOX}")
except Exception as e:  # noqa: BLE001
    print(f"\n沙箱清理失败（可手动删）: {e}")

print("\n全部通过。" if ok else "\n有用例失败，请检查上面标注 FAIL 的项。")
sys.exit(0 if ok else 1)
