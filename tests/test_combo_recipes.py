# -*- coding: utf-8 -*-
"""热门玩法路线 + 赌狗玩法提示的单元测试（不依赖 GUI / OCR）。"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import combo_recipes  # noqa: E402

# 来自 data/combo_recipes.json 的关键 apiName
WANJIAN_CORE = {"ARAM_Vulnerability", "CriticalMissile"}
GAMBLE_API = "ARAM_TransmutePrismatic"


def case_routes():
    cases = [
        # (输入 apiNames, 期望 level 或 None, 期望 id 或 None, 描述)
        # 注意：干扰牌统一用 ARAM_Erosion（侵蚀）——它不在任何路线里；
        # 用 ARAM_Archmage 会误触 infinite_skills 的 core。
        ({"ARAM_Vulnerability", "ARAM_Erosion", "ARAM_Deft"},
         "core", "wanjian_guizong", "易损出现 → 万剑归宗 core"),
        ({"CriticalMissile", "ARAM_Erosion", "ARAM_Deft"},
         "core", "wanjian_guizong", "暴击飞弹出现 → 万剑归宗 core"),
        ({"ARAM_FanTheHammer", "ARAM_Typhoon", "ARAM_Erosion"},
         "linked", "wanjian_guizong", "两张联动 → 万剑归宗 linked"),
        ({"ARAM_FanTheHammer", "ARAM_Erosion", "ARAM_Deft"},
         None, None, "只一张联动 → 不提示"),
        ({"ARAM_Archmage", "ARAM_Purist_Caster", "ARAM_Erosion"},
         "core", "infinite_skills", "大法师+纯粹主义者 → 无限技能流 core"),
        ({"ARAM_InfernalConduit", "ARAM_Erosion", "ARAM_Deft"},
         "core", "infernal_conduit", "炼狱导管 → 炼狱导管流 core"),
        ({"ARAM_Erosion", "ARAM_Deft", "ARAM_BluntForce"},
         None, None, "无关联牌 → 不提示"),
        (set(), None, None, "空集合"),
        (None, None, None, "None"),
    ]
    ok = 0
    for apis, want_level, want_id, desc in cases:
        hint = combo_recipes.pick_route_hint(apis, rng=random.Random(42))
        if want_level is None:
            good = hint is None
            print(f"{'✅' if good else '❌'} route | {desc}")
            if not good:
                print(f"      期望 None / 实际 {hint}")
        else:
            good = (hint is not None and hint["level"] == want_level
                    and hint["id"] == want_id)
            print(f"{'✅' if good else '❌'} route | {desc}")
            if not good:
                print(f"      期望 {want_id}/{want_level} / 实际 {hint}")
        ok += good
    return ok, len(cases)


def case_route_text():
    hint = combo_recipes.pick_route_hint(
        {"ARAM_Vulnerability", "ARAM_Archmage", "ARAM_Erosion"},
        rng=random.Random(42),
    )
    good = hint is not None and "万剑归宗" in hint["text"] and "易损" in hint["text"]
    print(f"{'✅' if good else '❌'} route text | core 文案含标题+命中名")
    if not good:
        print(f"      实际 {hint}")
    return (1 if good else 0), 1


def case_gamble():
    cases = [
        ({GAMBLE_API, "ARAM_Archmage", "ARAM_Erosion"}, "质变", "棱彩质变 → 赌狗"),
        ({"ARAM_PandorasBox", "ARAM_Archmage", "ARAM_Erosion"}, "潘朵拉", "潘朵拉 → 赌狗"),
        ({"ARAM_TransmuteGold", "ARAM_TransmuteChaos", "ARAM_Erosion"},
         "质变", "两个质变 → 赌狗（合并名字）"),
        ({"ARAM_Vulnerability", "ARAM_Archmage", "ARAM_Erosion"}, None, "无随机牌 → 不提示"),
        (set(), None, "空集合"),
    ]
    ok = 0
    for apis, want_sub, desc in cases:
        g = combo_recipes.detect_gamble(apis)
        if want_sub is None:
            good = g is None
            print(f"{'✅' if good else '❌'} gamble | {desc}")
            if not good:
                print(f"      期望 None / 实际 {g}")
        else:
            good = g is not None and want_sub in g["text"] and "🎲" in g["text"]
            print(f"{'✅' if good else '❌'} gamble | {desc}")
            if not good:
                print(f"      实际 {g}")
        ok += good
    return ok, len(cases)


def case_analyze_offers():
    # 易损 + 质变棱彩同时在场 → gamble 和 route 同时返回
    offers = combo_recipes.analyze_offers(
        {"ARAM_Vulnerability", GAMBLE_API, "ARAM_Erosion"},
    )
    good = (offers.get("gamble") is not None
            and offers.get("route") is not None
            and offers["route"]["id"] == "wanjian_guizong")
    print(f"{'✅' if good else '❌'} analyze_offers | 赌狗+路线并存")
    if not good:
        print(f"      实际 {offers}")
    return (1 if good else 0), 1


def case_data_sanity():
    """策划表里的 apiName 必须真实存在于官方库（防止手滑写错）。"""
    import json
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    official = os.path.join(here, "data", "augments_official.json")
    known = set()
    with open(official, "r", encoding="utf-8") as fh:
        for rec in json.load(fh).get("augments", []):
            known.add(rec.get("apiName"))

    bad = []
    data = {}
    with open(combo_recipes.RECIPES_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    for route in data.get("routes", []):
        for section in ("core", "linked"):
            for it in route.get(section) or []:
                api = it.get("apiName") if isinstance(it, dict) else it
                if api and api not in known:
                    bad.append(f"route[{route.get('id')}].{section}: {api}")
    for it in data.get("gambleAugments", []):
        api = it.get("apiName")
        if api and api not in known:
            bad.append(f"gamble: {api}")

    good = not bad
    print(f"{'✅' if good else '❌'} sanity | 策划表 apiName 全部存在于官方库")
    if not good:
        for b in bad:
            print(f"      ⚠ {b}")
    return (1 if good else 0), 1


def main():
    total_ok = total_n = 0
    for fn in (case_routes, case_route_text, case_gamble,
               case_analyze_offers, case_data_sanity):
        print("=" * 60)
        print(fn.__name__)
        print("=" * 60)
        ok, n = fn()
        total_ok += ok
        total_n += n
        print()
    print(f"通过 {total_ok}/{total_n}")
    sys.exit(0 if total_ok == total_n else 1)


if __name__ == "__main__":
    main()