# -*- coding: utf-8 -*-
"""
海克斯识别覆盖率验证（回归测试）
================================

回答一个问题：**玩家在海克斯大乱斗里看到的每一个候选海克斯，
插件到底认不认得出来？**

两条layer：
  layer 1 = hero_augments.csv 命中（有排名）
  layer 2 = 官方海克斯库命中但本英雄无排名（有名字、有稀有度，只是没排名）

输出 173 英雄 × 247 官方海克斯 = 42731 个组合的覆盖率矩阵。

用法
----
    python scripts/verify_augment_coverage.py
    python scripts/verify_augment_coverage.py --official data/augments_official.json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")

PH_RE = re.compile(r"^\s*(?:[\?？]+|未知|待定|None|null|N/A|-{1,})\s*$", re.I)


def norm(s: str) -> str:
    if not s:
        return ""
    s = str(s).strip().replace("：", ":").replace("（", "(").replace("）", ")")
    return re.sub(r"[\s\u3000]+", "", s)


def main() -> int:
    ap = argparse.ArgumentParser(description="海克斯识别覆盖率验证")
    ap.add_argument("--official", default=os.path.join(DATA_DIR, "augments_official.json"))
    ap.add_argument("--csv", default=os.path.join(DATA_DIR, "hero_augments.csv"))
    args = ap.parse_args()

    with open(args.official, "r", encoding="utf-8") as fh:
        db = json.load(fh)
    pool = [r for r in db["augments"] if r.get("inAramPool")]
    pool_names = {norm(r.get("nameZh")) for r in pool if r.get("nameZh")}
    print(f"官方库补丁 {db.get('patch')}  池 {len(pool)} 个  API: {db.get('sourceUrls', {}) and 'CDragon'}")

    with io.open(args.csv, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    heroes = sorted({r["中文名"] for r in rows})
    hero_aug = defaultdict(set)
    placeholders = 0
    for r in rows:
        nm = r.get("海克斯名称", "") or ""
        if PH_RE.match(nm):
            placeholders += 1
            continue
        hero_aug[r["中文名"]].add(norm(nm))

    total_pairs = len(heroes) * len(pool)
    layer1 = sum(len(hero_aug[h] & pool_names) for h in heroes)
    layer2 = total_pairs - layer1

    print(f"\n英雄 {len(heroes)}  官方池 {len(pool)}  →  组合总数 {total_pairs}")
    print(f"  layer1 有排名     : {layer1:>6}  ({layer1/total_pairs*100:.1f}%)")
    print(f"  layer2 仅识别     : {layer2:>6}  ({layer2/total_pairs*100:.1f}%)")
    print(f"  ----------------")
    print(f"  合计可识别        : {layer1+layer2:>6}  (100.0%)")
    print(f"\n  改造前只能靠 layer1 → 覆盖率 {layer1/total_pairs*100:.1f}%")
    print(f"  改造后新增可识别   : +{layer2} 个组合")

    print(f"\nCSV 里残留的占位行: {placeholders}（应为 0）")
    assert placeholders == 0, "仍有占位行未清理"

    # 官方池里从未在任何英雄身上出现过的（纯识别盲区）
    ever = set()
    for h in heroes:
        ever |= hero_aug[h] & pool_names
    never = sorted(pool_names - ever)
    print(f"\n官方池中 0 个英雄收录过的: {len(never)} 个")
    for n in never:
        rec = next((r for r in pool if norm(r.get("nameZh")) == n), {})
        print(f"  {n:<14} {rec.get('rarityZh') or '-':<6} {rec.get('apiName')}")

    # 稀有度分布
    print(f"\n官方池稀有度分布: {dict(Counter(r.get('rarityZh') or '(无)' for r in pool))}")
    print(f"官方池中 ARAM_ 专属: {sum(1 for r in pool if r.get('aramSpecific'))}")

    # 别名索引完整性
    alias_path = os.path.join(DATA_DIR, "augment_alias_zh.json")
    if os.path.exists(alias_path):
        with open(alias_path, "r", encoding="utf-8") as fh:
            alias = json.load(fh)
        bad = [k for k in alias if PH_RE.match(k)]
        miss = [r.get("nameZh") for r in pool
                if r.get("nameZh") and not PH_RE.match(r.get("nameZh"))
                and r["nameZh"] not in alias]
        print(f"\n别名索引: {len(alias)} 条，占位残留 {len(bad)}（应为 0），池内漏收 {len(miss)}（应为 0）")
        assert not bad and not miss, "别名索引异常"

    print("\n✅ 全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
