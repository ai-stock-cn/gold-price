#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
push_library.py —— 把 report.html 覆盖推送到资料库的同一个页面节点。

设计要点
--------
1. **覆盖，不新增**：调 library skill 的 page/import_html.py 并传 --node-block-id，
   服务端更新该节点内容，node_id 与 /space/d/ 链接保持不变。
2. **幂等**：以 CSV 最后一条有效记录的 (ts, slot) 为数据指纹，与 data/push_state.json
   比对。没新数据就什么都不做，重复执行不会重复推送。
3. **环境要求**：资料库接口靠会话级 WORKBUDDY_CONFIG_DIR + RPC token 鉴权，
   只在 WorkBuddy 的 agent 环境里可用。LaunchAgent / cron 那种纯净环境会
   E_AUTH_FAILED —— 此时脚本明确报错退出，不假装成功。

用法
----
    python3 push_library.py            # 有新数据才推
    python3 push_library.py --force    # 不管指纹，强制推一次
    python3 push_library.py --dry-run  # 只报告要不要推，不真推
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "data" / "push_state.json"
LOG_PATH = BASE / "data" / "push.log"
HTML_PATH = BASE / "report.html"

SKILL_CANDIDATES = [
    "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/"
    "resources/plugins/workbuddy-builtin/skills/library",
]


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def find_skill_dir() -> str | None:
    """定位 library skill 目录：环境变量优先，其次已知路径，最后 glob 兜底。"""
    env_dir = os.environ.get("CODEBUDDY_SKILL_DIR", "").strip()
    if env_dir and (Path(env_dir) / "page" / "import_html.py").is_file():
        return env_dir
    for c in SKILL_CANDIDATES:
        if (Path(c) / "page" / "import_html.py").is_file():
            return c
    hits = glob.glob(
        "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/"
        "resources/plugins/*/*/skills/library"
    )
    for c in hits:
        if (Path(c) / "page" / "import_html.py").is_file():
            return c
    return None


def data_fingerprint(csv_path: Path) -> str:
    """以 CSV 最后一条有效记录的 (ts, slot) 作为数据版本指纹。"""
    if not csv_path.is_file():
        return ""
    last = ""
    with open(csv_path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status") == "ok":
                last = f"{r.get('ts', '')}|{r.get('slot', '')}"
    return last


def read_state() -> dict:
    if STATE_PATH.is_file():
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def push(skill_dir: str, node_id: str, file_name: str) -> dict:
    script = Path(skill_dir) / "page" / "import_html.py"
    cmd = [
        sys.executable, str(script), str(HTML_PATH),
        "--node-block-id", node_id,
        "--file-name", file_name,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()

    for line in out.splitlines():
        line = line.strip()
        if line.startswith("KS_IMPORT_OK"):
            raw = line[len("KS_IMPORT_OK"):].strip()
            try:
                return {"ok": True, **json.loads(raw)}
            except json.JSONDecodeError:
                return {"ok": True, "raw": raw}
        if line.startswith("{") and '"error"' in line:
            try:
                return {"ok": False, "error": json.loads(line).get("error", line)}
            except json.JSONDecodeError:
                return {"ok": False, "error": line}
    return {"ok": False, "error": (out or err or f"exit={proc.returncode}")[:300]}


def main() -> int:
    ap = argparse.ArgumentParser(description="推送积存金报表到资料库（覆盖同一节点）")
    ap.add_argument("--force", action="store_true", help="忽略数据指纹，强制推送一次")
    ap.add_argument("--dry-run", action="store_true", help="只判断要不要推，不真推")
    args = ap.parse_args()

    cfg = load_config()
    lib = cfg.get("library", {})
    if not lib.get("enabled"):
        log("library.enabled=false，跳过推送。")
        return 0

    node_id = (lib.get("node_block_id") or "").strip()
    if not node_id:
        log("未配置 library.node_block_id，无法推送。")
        return 1

    if not os.environ.get("WORKBUDDY_CONFIG_DIR", "").strip():
        log("当前不是 WorkBuddy agent 环境（缺 WORKBUDDY_CONFIG_DIR），资料库鉴权不可用，跳过。")
        return 1

    if not HTML_PATH.is_file():
        log(f"报表不存在：{HTML_PATH}")
        return 1

    skill_dir = find_skill_dir()
    if not skill_dir:
        log("找不到 library skill 目录（page/import_html.py），无法推送。")
        return 1

    fp = data_fingerprint(BASE / cfg["files"]["csv"])
    state = read_state()
    if not args.force and fp and state.get("last_fp") == fp:
        log(f"无新数据（指纹 {fp}），跳过推送。")
        return 0

    log(f"检测到新数据（指纹 {fp}），准备推送…")
    if args.dry_run:
        log("dry-run：不实际推送。")
        return 0

    res = push(skill_dir, node_id, lib.get("file_name") or "积存金.html")
    if not res.get("ok"):
        log(f"推送失败：{res.get('error')}")
        return 1

    got_id = (res.get("node_block_id") or "").strip()
    if got_id and got_id != node_id:
        log(f"⚠️ 服务端返回的 node_block_id={got_id} 与配置的 {node_id} 不一致，可能是新建而非覆盖。")
        return 1

    state.update({
        "last_fp": fp,
        "last_push": time.strftime("%Y-%m-%d %H:%M:%S"),
        "node_block_id": got_id or node_id,
        "url": res.get("url") or lib.get("url", ""),
    })
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    log(f"推送成功：节点 {got_id or node_id}  {state.get('url', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
