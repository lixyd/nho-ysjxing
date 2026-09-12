# -*- coding: utf-8 -*-
"""两层解析的端到端测试（不依赖截图与 OCR）。"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as app_main  # noqa: E402

dm = app_main.DataManager()
print(f"hero_data 英雄数     : {len(dm.hero_data)}")
print(f"official_ready       : {dm.official_ready}")
print(f"official_augments    : {len(dm.official_augments)}")
print(f"official_names(索引) : {len(dm.official_names)}")
print()

recognizer = app_main.GameAnalyzer.__new__(app_main.GameAnalyzer)
recognizer.dm = dm

cases = [
    # (OCR 文本, 英雄, 期望)
    ("贪欲束缚", "暗裔剑魔", "layer1_with_rank"),
    ("海克斯核心", "暗裔剑魔", "layer2_official_only"),
    ("升级：冥火之拥", "暗裔剑魔", "layer2_official_only"),
    ("升级:冥火之拥", "暗裔剑魔", "layer2_official_only"),
    ("复位", "九尾妖狐", "layer2_official_only"),
    ("大法师", "暗裔剑魔", "either"),
    ("升级冥火之拥", "暗裔剑魔", "layer2_official_only"),   # OCR 漏掉冒号
    ("海克斯核心啊", "暗裔剑魔", "layer2_official_only"),   # OCR 多读一个字
    # --- 负例：不能误判 ---
    ("这不是一个海克斯", "暗裔剑魔", "none"),
    ("完全无关的文本内容", "暗裔剑魔", "none"),
    ("双击666", "暗裔剑魔", "none"),
]

ok = 0
for text, hero, expect in cases:
    hit = recognizer.resolve_augment(text, hero)
    if hit is None:
        got = "none"
        shown = "❌ 未识别"
    else:
        got = "layer2_official_only" if hit["official_only"] else "layer1_with_rank"
        fmt = recognizer.format_hit(hit, hero)
        shown = fmt["text"].replace("\n", "  ｜  ")

    if expect == "either":
        good = got in ("layer1_with_rank", "layer2_official_only")
    else:
        good = got == expect
    ok += good
    print(f"{'✅' if good else '❌'} [{hero}] OCR={text!r}")
    print(f"      遮罩显示 → {shown}   (期望 {expect} / 实际 {got})")

print()
print(f"通过 {ok}/{len(cases)}")
sys.exit(0 if ok == len(cases) else 1)
