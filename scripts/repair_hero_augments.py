# -*- coding: utf-8 -*-
"""
hero_augments.csv 修复脚本
==========================

做三件事：
1. **清占位**：删掉「海克斯名称」是 `？？？`/`???`/`未知` 之类的行。
   这类行有等级和排名，但名字丢了，无法反推是哪个海克斯 —— 留着只会污染 OCR 匹配。
2. **标记非本模式残留**：CSV 里有些名字在官方 552 总表里、但不在 ARAM 海克斯大乱斗
   的池子里（例如魄罗大乱斗的「魄罗之王的弹跳」）。默认只报告不动它 ——
   删数据是破坏性操作，池子判断万一有偏差就会误伤。
3. **排名完整性自检**：报告等级内序号是否连续，方便判断是否需要重爬。

命名归一化差异（全角冒号/空格/括号）会自动修掉。

用法
----
    python scripts/repair_hero_augments.py                # 预览（不写文件）
    python scripts/repair_hero_augments.py --apply        # 实际写入（自动备份）
    python scripts/repair_hero_augments.py --apply --drop-polluted   # 连非本模式残留一起删
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
CSV_FILE = os.path.join(DATA_DIR, "hero_augments.csv")
OFFICIAL_FILE = os.path.join(DATA_DIR, "augments_official.json")
# 报告与备份都放在 data/ 之外 —— build.py 会整目录复制 data/，
# 放进 data/ 会被打进用户安装包（1.5MB 的备份 + 一堆报告，纯噪音）。
REPORT_DIR = os.path.join(REPO_ROOT, "reports")
BACKUP_DIR = os.path.join(REPO_ROOT, "_backup")
REPORT_FILE = os.path.join(REPORT_DIR, "repair_report.json")

CSV_HEADER = ["中文名", "英文名", "等级", "总排名", "等级内序号", "海克斯名称"]

# 全角半角都要覆盖 —— 第一轮只搜半角 ??? 才误判成"已修好"
PLACEHOLDER_RE = re.compile(r"^\s*(?:[\?？]+|未知|待定|None|null|N/A|-{1,})\s*$", re.I)


def norm_name(name: str) -> str:
    if not name:
        return ""
    s = name.strip()
    s = s.replace("：", ":").replace("（", "(").replace("）", ")")
    s = re.sub(r"[\s\u3000]+", "", s)
    return s


def load_official():
    with open(OFFICIAL_FILE, "r", encoding="utf-8") as fh:
        db = json.load(fh)
    pool = [r for r in db["augments"] if r.get("inAramPool")]
    pool_norm = {}
    for r in pool:
        for nm in (r.get("nameZh"), r.get("nameEn")):
            if nm:
                pool_norm[norm_name(nm)] = r
    total_norm = {}
    for r in db["augments"]:
        for nm in (r.get("nameZh"), r.get("nameEn")):
            if nm:
                total_norm[norm_name(nm)] = r
    return db, pool_norm, total_norm


def main() -> int:
    ap = argparse.ArgumentParser(description="修复 hero_augments.csv")
    ap.add_argument("--csv", default=CSV_FILE)
    ap.add_argument("--official", default=OFFICIAL_FILE)
    ap.add_argument("--apply", action="store_true", help="实际写入（默认只预览）")
    ap.add_argument("--drop-polluted", action="store_true",
                    help="同时删除「在官方总表但不在本模式池」的行")
    args = ap.parse_args()

    db, pool_norm, total_norm = load_official()
    pool_apis = {r["apiName"] for r in db["augments"] if r.get("inAramPool")}
    print(f">>> 官方库: 定义 {db['counts']['definitions']}，"
          f"本模式池 {db['counts']['aramPool']}，补丁 {db.get('patch')}")

    with io.open(args.csv, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    print(f">>> CSV: {len(rows)} 行")

    kept, dropped_placeholder, dropped_polluted = [], [], []
    renamed = []
    pool_hit = 0
    total_only = Counter()
    total_only_heroes = defaultdict(set)

    for row in rows:
        raw = row.get("海克斯名称", "") or ""
        if PLACEHOLDER_RE.match(raw):
            dropped_placeholder.append(row)
            continue

        n = norm_name(raw)
        hit = pool_norm.get(n)
        if hit:
            pool_hit += 1
            if hit["nameZh"] and hit["nameZh"] != raw:
                renamed.append((raw, hit["nameZh"]))
                row["海克斯名称"] = hit["nameZh"]
            kept.append(row)
            continue

        other = total_norm.get(n)
        if other:
            # 在官方总表里但不在本模式池 —— 别的模式/旧版本残留
            total_only[n] += 1
            total_only_heroes[n].add(row.get("中文名", ""))
            if args.drop_polluted:
                dropped_polluted.append(row)
                continue
            kept.append(row)
            continue

        # 既不在池也不在总表 —— 保留但记录下来
        total_only[n] += 1
        total_only_heroes[n].add(row.get("中文名", ""))
        kept.append(row)

    # 排名完整性
    gaps = []
    by_hero_tier = defaultdict(list)
    for row in kept:
        by_hero_tier[(row.get("中文名"), row.get("等级"))].append(row)
    for (hero, tier), grp in by_hero_tier.items():
        seq = sorted(int(r["等级内序号"]) for r in grp
                     if str(r.get("等级内序号", "")).strip().isdigit())
        if seq and seq != list(range(1, len(seq) + 1)):
            gaps.append({"hero": hero, "tier": tier, "n": len(seq),
                         "min": seq[0], "max": seq[-1]})

    print(f"\n=== 1) 占位行（将删除）{len(dropped_placeholder)} 条 ===")
    hero_cnt = Counter(r.get("中文名") for r in dropped_placeholder)
    print(f"    涉及 {len(hero_cnt)} 个英雄，例: "
          + "、".join(f"{h}({c})" for h, c in hero_cnt.most_common(6)))

    print(f"\n=== 2) 官方池匹配成功 {pool_hit} 条 ===")
    print(f"    自动归一化命名 {len(set(renamed))} 种")
    for old, new in sorted(set(renamed))[:10]:
        print(f"      {old} -> {new}")

    print(f"\n=== 3) 非本模式池的名字 {len(total_only)} 种 ===")
    for name, cnt in total_only.most_common(20):
        heroes = sorted(total_only_heroes[name])
        print(f"      {name}  ×{cnt}  例: {', '.join(heroes[:3])}")
    if args.drop_polluted:
        print(f"    → 其中 {len(dropped_polluted)} 行将被删除")

    print(f"\n=== 4) 等级内序号不连续的分组 {len(gaps)} 个 ===")
    for g in gaps[:8]:
        print(f"      {g['hero']} / {g['tier']}: {g['n']} 条, 序号 {g['min']}~{g['max']}")

    print(f"\n=== 结果 ===")
    print(f"    {len(rows)} 行 → {len(kept)} 行"
          f"（删除占位 {len(dropped_placeholder)}"
          + (f" + 残留 {len(dropped_polluted)}" if args.drop_polluted else "") + "）")

    report = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "officialPatch": db.get("patch"),
        "rowsBefore": len(rows),
        "rowsAfter": len(kept),
        "droppedPlaceholder": len(dropped_placeholder),
        "droppedPlaceholderHeroes": sorted(hero_cnt),
        "droppedPolluted": len(dropped_polluted),
        "poolMatched": pool_hit,
        "nonPoolNames": [{"name": k, "count": v, "heroes": sorted(total_only_heroes[k])}
                         for k, v in total_only.most_common()],
        "renamed": [{"from": a, "to": b} for a, b in sorted(set(renamed))],
        "rankGaps": gaps,
    }
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"    📄 {REPORT_FILE}")

    if not args.apply:
        print("\n（预览模式，未写入。加 --apply 才实际修改）")
        return 0

    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup = os.path.join(
        BACKUP_DIR,
        os.path.basename(args.csv) + ".bak." + time.strftime("%Y%m%d_%H%M%S"),
    )
    shutil.copy2(args.csv, backup)
    with io.open(args.csv, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(kept)
    print(f"\n✅ 已写入 {args.csv}")
    print(f"   备份 {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
