#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""排期回归测试：24 小时逐时采集 / 缺口按最近报价补齐 / 日线取当天最后一个真实点。

用法：
    python3 verify_schedule.py

完全隔离：自动在 /tmp 下复制一份项目副本再跑，不碰真实的 CSV 与报表。
时间用 monkeypatch 控制、报价固定返回，全程不发网络请求。
改完 config.json 的 slots / trading_window / tail_grace / weekdays_only 之后跑一遍最有用。

当前语义（2026-10-31 改，24/7 口径）：
  * 时点覆盖 00:00-23:00 共 24 个整点，**含周末**（weekdays_only=false）；
  * 一次真实请求只落一行 live 数据，落在「已到的最后一个整点」上；
  * 中间没采到的整点由 fill_today() 用「前向沿用最近实采价」补成 carry 行；
  * carry 行只用于让曲线连续，日线/收盘价不认它。

所有场景都跑在一个**周六**上，所以每个用例同时也验证了「周末照样采集」。
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


def last_saturday():
    """最近一个周六。用它当仿真日，顺手验证 weekdays_only=false。"""
    d = datetime.date.today()
    while d.weekday() != 5:
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

TODAY = last_saturday()


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


def at(slot, plus_min=5):
    """把 MM:HH 往后推几分钟，模拟「整点过 5 分才触发」。"""
    h, m = map(int, slot.split(":"))
    t = (h * 60 + m + plus_min) % (24 * 60)
    return f"{t // 60:02d}:{t % 60:02d}"


def head(t):
    print(f"\n--- {t} ---")


PROGRESS = []
slots = collect.CFG["slots"]
tail = slots[-1]
mid = len(slots) // 2

head(f"仿真日 {TODAY}（{datetime.date.fromisoformat(TODAY).strftime('%A')}，周末）")
print(f"沙箱: {SANDBOX}")
print(f"标准时点: {slots[0]} … {tail} 共 {len(slots)} 个")
print(f"weekdays_only={collect.CFG.get('weekdays_only')}  "
      f"trading_window={collect.CFG['trading_window']}  tail_grace={collect.CFG['tail_grace']}")

head(f"场景1：凌晨 {at(slots[3])} 触发 → 落在 {slots[3]}（周末 + 凌晨都要能采）")
clear_today()
got = run(at(slots[3]))
PROGRESS.append((f"周末凌晨也能采集，写入 {slots[3]}", got == [slots[3]], got))

head(f"场景2：{at(slots[3], 40)} 重复触发（{slots[3]} 已有真实记录）")
before = len(today_rows())
run(at(slots[3], 40))
PROGRESS.append(("同一整点不重复写入", len(today_rows()) == before, len(today_rows())))

head(f"场景3：跳到 {at(slots[mid])} 才触发（中间 {len(slots[4:mid])} 个整点都没采）")
got = run(at(slots[mid]))
PROGRESS.append((f"只写当前整点 {slots[mid]}，不伪造中间点",
                 got == [slots[3], slots[mid]], got))

head("场景4：fill_today 补齐缺口（只能前向，不能回头用未来的价）")
collect.fill_today()
c = carry_slots()
expect_gap = set(slots[4:mid])
PROGRESS.append((f"中间 {len(expect_gap)} 个整点被补成 carry",
                 expect_gap <= set(c), sorted(expect_gap - set(c))))
PROGRESS.append(("补齐行标记为 carry",
                 all(r.get("src") == "carry" for r in today_rows()
                     if r["slot"] in expect_gap), c))
early = set(slots[:3])          # 00:00 / 01:00 / 02:00，都早于首条真实记录 03:00
PROGRESS.append(("早于当天首条真实记录的时点留空，不用未来价回填",
                 not (early & set(c)), sorted(early & set(c))))

head("场景5：fill_today 幂等（连跑两次结果不变）")
n1 = len(today_rows())
collect.fill_today()
PROGRESS.append(("重复补齐不产生新行", len(today_rows()) == n1, len(today_rows())))

head(f"场景6：{at(tail)} 触发 → 收尾时点 {tail}")
got = run(at(tail))
PROGRESS.append((f"收尾时点 {tail} 可被采到", tail in got, got))

head("场景7：收尾点已有记录后再触发")
before = len(today_rows())
run(at(tail, 40))
PROGRESS.append(("同一收尾点不重复写入", len(today_rows()) == before, len(today_rows())))

head("场景8：整天只在最后一小时采过一次")
clear_today()
got = run(at(tail, 10))
PROGRESS.append(("整天只采一次也有收尾点", got == [tail], got))
collect.fill_today()
covered = sorted({r["slot"] for r in today_rows()})
# 只有 23:00 一条真实记录，它前面 23 个整点没有任何「已知的过去价」可沿用，
# 因此全部留空——这是正确的，拿 23:00 的价回填全天等于用未来信息造一条平线。
PROGRESS.append(("唯一的样本在收尾点，前面的整点全部留空",
                 covered == [tail], covered))

head("场景9：凌晨先采到，之后整天的缺口才允许前向补齐")
clear_today()
run(at(slots[0], 5))              # 00:05 → 00:00
run(at(slots[4], 5))              # 04:05 → 04:00
collect.fill_today()
filled = sorted({r["slot"] for r in today_rows()})
PROGRESS.append((f"00:00 之后到 04:00 之间的缺口被补齐（共 {len(filled)} 个整点）",
                 set(slots[:5]) <= set(filled), filled))

head("报表校验")
set_now(at(tail, 20))
collect.build_report()
html = open(collect.REPORT_PATH, encoding="utf-8").read()
intraday = re.search(r"<h2>今日分时.*?</svg>", html, re.S).group(0)
circles = len(re.findall(r"<circle", intraday))
PROGRESS.append((f"今日曲线铺满 {len(slots)} 个整点", circles == len(slots), circles))
PROGRESS.append(("今日曲线区分实心/空心点",
                 len(re.findall(r'r="3.4"', intraday)) >= 1
                 and len(re.findall(r'r="2.8"', intraday)) >= 1,
                 (len(re.findall(r'r="3.4"', intraday)),
                  len(re.findall(r'r="2.8"', intraday)))))
det = re.search(r"<h2>今日明细.*?</tbody>", html, re.S).group(0)
n_detail = len([1 for m in re.findall(r'<tr(?: class="\w+")?>(.*?)</tr>', det, re.S)
                if len(re.findall(r"<td[^>]*>", m)) >= 7])
PROGRESS.append((f"今日明细 {len(slots)} 行", n_detail == len(slots), n_detail))
PROGRESS.append(("日内极值区块已渲染",
                 bool(re.search(r"<h2>日内极值[^<]*</h2>", html)), True))
PROGRESS.append((f"今日明细标题覆盖 {slots[0]} ~ {tail}",
                 f"{slots[0]} ~ {tail}" in html, ""))
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
