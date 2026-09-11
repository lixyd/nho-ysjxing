# -*- coding: utf-8 -*-
"""
官方海克斯库 与 hero_augments.csv 对账脚本
==========================================

用途
----
用 `data/augments_official.json`（官方真源）去校验、补全 `data/hero_augments.csv`：

1. 命名校验：CSV 里的每个「海克斯名称」能否在官方池里找到（中文名或英文名）。
2. 覆盖检查：官方池里有、但 CSV 所有英雄都没出现过的海克斯 -> 属于"识别盲区"。
3. 占位/脏数据：空值、`???`、`？`、明显非海克斯文本。
4. `--fix`：把能唯一确定的别名（如简繁/空格差异）写回 CSV，并输出报告。

用法
----
    python scripts/reconcile_augments.py                # 只报告
    python scripts/reconcile_augments.py --fix          # 归一化命名差异
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
OFFICIAL_FILE = os.path.join(DATA_DIR, "augments_official.json")
CSV_FILE = os.path.join(DATA_DIR, "hero_augments.csv")
# 报告放 data/ 之外，避免被 build.py 打进用户安装包
REPORT_FILE = os.path.join(REPO_ROOT, "reports", "augment_reconcile_report.json")

CSV_HEADER = ["中文名", "英文名", "等级", "总排名", "等级内序号", "海克斯名称"]

PLACEHOLDER_RE = re.compile(r"^\s*(\?+|\?{3}|？+|未知|None|null|-+)\s*$", re.I)


def norm(name: str) -> str:
    """归一化海克斯名：去空白、统一全角冒号/括号、去装饰性符号。"""
    if not name:
        return ""
    s = name.strip()
    s = s.replace("：", ":").replace("（", "(").replace("）", ")")
    s = re.sub(r"[\s\u3000]+", "", s)
    return s


def load_official(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        db = json.load(fh)
    pool = [r for r in db["augments"] if r.get("inAramPool")]
    by_norm = defaultdict(list)
    for r in pool:
        for nm in (r.get("nameZh"), r.get("nameEn")):
            if nm:
                by_norm[norm(nm)].append(r)
    return db, pool, by_norm


def load_csv(path: str):
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="官方海克斯库 × hero_augments.csv 对账")
    ap.add_argument("--official", default=OFFICIAL_FILE)
    ap.add_argument("--csv", default=CSV_FILE)
    ap.add_argument("--fix", action="store_true", help="把可唯一确定的别名差异写回 CSV")
    args = ap.parse_args()

    if not os.path.exists(args.official):
        print(f"❌ 找不到官方库 {args.official}，先运行 scripts/build_augments_official.py")
        return 2

    db, pool, by_norm = load_official(args.official)
    rows = load_csv(args.csv)
    print(f">>> 官方库: {db['counts']}  补丁 {db.get('patch')}")
    print(f">>> CSV   : {len(rows)} 行")

    used = Counter()
    unknown = Counter()
    dirty = []
    fixes = {}
    hero_of_unknown = defaultdict(set)

    for row in rows:
        raw = row.get("海克斯名称", "") or ""
        n = norm(raw)
        if not n or PLACEHOLDER_RE.match(raw):
            dirty.append({"hero": row.get("中文名"), "value": raw, "reason": "空/占位符"})
            continue
        hits = by_norm.get(n)
        if hits:
            used[hits[0]["apiName"]] += 1
            continue
        # 长度>=2 的中文名，尝试"包含关系"识别（OCR/别名差异）
        fuzzy = [k for k in by_norm if len(k) >= 3 and (k in n or n in k)]
        if len(fuzzy) == 1:
            target = by_norm[fuzzy[0]][0]
            used[target["apiName"]] += 1
            fixes[(row.get("中文名"), raw)] = target["nameZh"]
            continue
        unknown[n] += 1
        hero_of_unknown[n].add(row.get("中文名"))

    pool_names = {r["apiName"] for r in pool}
    missing = sorted(pool_names - set(used))

    print("\n=== 1) 命名未匹配（CSV 有、官方池找不到）===")
    if unknown:
        for n, c in unknown.most_common(50):
            heroes = sorted(hero_of_unknown[n])[:4]
            print(f"  {n}  ×{c}  例: {', '.join(heroes)}")
    else:
        print("  ✅ 无")

    print(f"\n=== 2) 官方池覆盖盲区（官方有、CSV 从未出现）{len(missing)} 个 ===")
    for api in missing[:60]:
        rec = next(r for r in pool if r["apiName"] == api)
        print(f"  {api:<28} {rec.get('nameZh') or '-':<14} {rec.get('rarityZh') or '-'}")
    if len(missing) > 60:
        print(f"  ... 另有 {len(missing) - 60} 个")

    print(f"\n=== 3) 脏数据/占位 {len(dirty)} 条 ===")
    for d in dirty[:20]:
        print(f"  {d['hero']}: {d['value']!r} ({d['reason']})")

    print(f"\n=== 4) 可自动归一化的别名 {len(fixes)} 条 ===")
    for (hero, old), new in list(fixes.items())[:20]:
        print(f"  {hero}: {old} -> {new}")

    report = {
        "generatedAt": db.get("generatedAt"),
        "officialCounts": db.get("counts"),
        "csvRows": len(rows),
        "unmatchedNames": [{"name": k, "count": v, "heroes": sorted(hero_of_unknown[k])} for k, v in unknown.most_common()],
        "poolNotInCsv": missing,
        "dirty": dirty,
        "aliasFixes": [{"hero": k[0], "from": k[1], "to": v} for k, v in fixes.items()],
    }
    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"\n📄 报告: {REPORT_FILE}")

    if args.fix and fixes:
        for row in rows:
            key = (row.get("中文名"), row.get("海克斯名称"))
            if key in fixes:
                row["海克斯名称"] = fixes[key]
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
            writer.writeheader()
            writer.writerows(rows)
        print(f"✅ 已修正 {len(fixes)} 条别名写入 {args.csv}")
    elif args.fix:
        print("ℹ️ 无需修正")

    return 0


if __name__ == "__main__":
    sys.exit(main())
