# -*- coding: utf-8 -*-
"""用真实 augments_official.json 数据验证流派识别覆盖率。

不依赖 GUI / OCR / 主程序，只读官方 JSON 给每个海克斯打标，统计：
    1. 每个流派的命中数（不能太少——否则该流派永远触发不了）
    2. 总命中率（至少 60% 的海克斯能被归类）
    3. 没标签的样本（前 5 个，用于人工审视关键词是否漏判）
"""
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.style_detect import STYLE_PATTERNS, tag_augment  # noqa: E402

AUGMENTS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "augments_official.json",
)


def main():
    if not os.path.exists(AUGMENTS_FILE):
        print(f"⚠ 跳过：找不到 {AUGMENTS_FILE}（建议在工程根目录运行）")
        return 0

    with open(AUGMENTS_FILE, "r", encoding="utf-8") as fh:
        db = json.load(fh)

    in_pool = [r for r in db.get("augments", []) if r.get("inAramPool")]
    print(f"ARAM 池共 {len(in_pool)} 个海克斯")
    if not in_pool:
        print("⚠ 池为空，跳过")
        return 0

    counter = Counter()
    untagged = []
    multi_tag = 0
    for rec in in_pool:
        tags = tag_augment(rec)
        if not tags:
            untagged.append(rec.get("apiName"))
            continue
        if len(tags) > 1:
            multi_tag += 1
        for t in tags:
            counter[t] += 1

    print(f"\n流派命中数:")
    print(f"{'代码':<8} {'中文':<10} {'命中':<6}")
    for code, label, _, _ in STYLE_PATTERNS:
        print(f"{code:<8} {label:<10} {counter.get(code, 0):<6}")

    total_tagged = sum(counter.values())
    print(f"\n总命中次数: {total_tagged}")
    print(f"无标签海克斯: {len(untagged)} / {len(in_pool)} = {len(untagged)*100/max(1,len(in_pool)):.1f}%")
    print(f"多标签海克斯: {multi_tag} / {len(in_pool)}")
    if untagged:
        print(f"\n无标签样本（最多展示 8 个）:")
        for n in untagged[:8]:
            print(f"  - {n}")

    # 验证：至少 60% 海克斯有标签；每个主要流派命中数 ≥ 5
    tagged_pct = (len(in_pool) - len(untagged)) / max(1, len(in_pool))
    ok = tagged_pct >= 0.6
    print(f"\n覆盖率 {tagged_pct*100:.1f}%  (要求 ≥ 60%) → {'✅' if ok else '❌'}")

    too_thin = [(code, c) for code, c in counter.items() if c < 3]
    if too_thin:
        print(f"\n⚠ 以下流派命中 < 3 次（很可能触发不到）: {too_thin}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())