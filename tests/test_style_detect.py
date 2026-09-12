# -*- coding: utf-8 -*-
"""海克斯流派识别单元测试。

不依赖截图 / OCR / 主程序 —— 直接测 scripts.style_detect 的纯函数。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.style_detect import (  # noqa: E402
    STYLE_PATTERNS, analyze_combo, dominant_style, format_hint, tag_augment,
)


# ---------- 单条海克斯打标 ----------

SAMPLES = [
    # (augment 记录, 期望的标签集合, 描述)
    ({"apiName": "ARAM_Archmage", "nameZh": "大法师",
      "descZh": "施放一个技能会返还另一个随机技能的花费"},
     {"ap"}, "大法师 → AP"),
    ({"apiName": "ARAM_Eureka", "nameZh": "尤里卡",
      "descZh": "获得相当于@APToHasteConversion*100@%法术强度的技能急速。"},
     {"ap"}, "尤里卡 → AP"),
    ({"apiName": "ARAM_DualWield", "nameZh": "双刀流",
      "descZh": "在攻击时，发射一个箭矢，它施加效能削减的{{ Item_Keyword_OnHit }}。"
                "获得@AttackSpeed*100@%总攻击速度。"},
     {"crit", "ad"}, "双刀流 → 暴击 + 攻速"),
    ({"apiName": "ARAM_DoubleTap", "nameZh": "双发快射",
      "descZh": "你的暴击施加一次额外的%i:OnHit%攻击特效。获得...暴击几率。"},
     {"crit"}, "双发快射 → 暴击"),
    ({"apiName": "ARAM_CelestialBody", "nameZh": "星界躯体",
      "descZh": "获得@Health@生命值，但你造成的伤害降低@DamageReduction*100@%。"},
     {"tank"}, "星界躯体 → 坦克"),
    ({"apiName": "ARAM_Dashing", "nameZh": "全凭身法",
      "descZh": "你的冲刺、跳跃、闪烁或传送类技能获得@Haste@技能急速。"},
     {"dash"}, "全凭身法 → 机动"),
    ({"apiName": "ARAM_CriticalHealing", "nameZh": "会心治疗",
      "descZh": "你的治疗和护盾可以暴击。"},
     {"heal", "crit"}, "会心治疗 → 续航 + 暴击（双标签）"),
    ({"apiName": "ARAM_BladeWaltz", "nameZh": "利刃华尔兹",
      "descZh": "获得利刃华尔兹作为一个召唤师技能。"},
     {"melee"}, "利刃华尔兹 → 近战"),
    # 无任何关键字 → 空集
    ({"apiName": "ARAM_ClownCollege", "nameZh": "小丑学院",
      "descZh": "获得召唤师技能欺诈魔术。"},
     set(), "小丑学院 → 无流派"),
    # 缺字段也不能崩
    ({"apiName": "ARAM_Archmage"}, {"ap"}, "仅 apiName → AP"),
    ({"nameZh": "大法师"}, {"ap"}, "仅 nameZh → AP"),
    ({}, set(), "空记录 → 空集"),
    (None, set(), "None → 空集"),
]


def case_tag():
    ok = 0
    for rec, expected, desc in SAMPLES:
        got = tag_augment(rec)
        good = got == expected
        ok += good
        print(f"{'✅' if good else '❌'} tag_augment | {desc}")
        if not good:
            print(f"      期望 {expected} / 实际 {got}")
    return ok


# ---------- dominant_style ----------

def _rec(name, api, desc=""):
    return {"apiName": api, "nameZh": name, "descZh": desc}


def _result(name, api, desc=""):
    """构造一个形如 GameAnalyzer.analyze() 的有效 result。"""
    return {
        "valid": True,
        "display": name,
        "official_rec": _rec(name, api, desc),
    }


def _three(*items):
    """三张牌：hex_1, hex_2, hex_3"""
    return {f"hex_{i+1}": v for i, v in enumerate(items)}


COMBO_CASES = [
    # 三张牌同流派 → 命中
    (
        _three(
            _result("大法师", "ARAM_Archmage", "施放一个技能会返还"),
            _result("尤里卡", "ARAM_Eureka", "获得相当于@APToHasteConversion*100@%法术强度"),
            _result("利刃华尔兹", "ARAM_BladeWaltz"),
        ),
        {"code": "ap", "min_count": 2},
        "AP × 2 + 近战 = AP 流主导",
    ),
    # 三张牌同一种——双发快射、双刀流都是 crit
    (
        _three(
            _result("双发快射", "ARAM_DoubleTap", "暴击施加额外攻击特效"),
            _result("双刀流", "ARAM_DualWield", "获得@AttackSpeed*100@%总攻击速度"),
            _result("会心治疗", "ARAM_CriticalHealing", "你的治疗和护盾可以暴击"),
        ),
        {"code": "crit", "min_count": 3},
        "三张全带 crit = 暴击流主导",
    ),
    # 只有 1 张同流派 → 不提示（默认阈值 2）
    (
        _three(
            _result("大法师", "ARAM_Archmage"),
            _result("星界躯体", "ARAM_CelestialBody"),
            _result("全凭身法", "ARAM_Dashing"),
        ),
        None,
        "三张各不同流派 → 不提示",
    ),
    # 有 invalid → 不参与统计
    (
        _three(
            {"valid": False, "display": "??", "official_rec": _rec("??", "?")},
            _result("大法师", "ARAM_Archmage"),
            _result("尤里卡", "ARAM_Eureka"),
        ),
        {"code": "ap", "min_count": 2},
        "1 张 invalid + 2 张 AP → 命中",
    ),
    # 空结果 → 不提示
    ({}, None, "空 dict"),
    (None, None, "None 输入"),
]


def case_dominant():
    ok = 0
    for results, expected, desc in COMBO_CASES:
        hint = dominant_style(results)
        if expected is None:
            good = hint is None
            print(f"{'✅' if good else '❌'} dominant_style | {desc}")
            if not good:
                print(f"      期望 None / 实际 {hint}")
        else:
            good = (hint is not None
                    and hint["code"] == expected["code"]
                    and hint["count"] >= expected["min_count"])
            print(f"{'✅' if good else '❌'} dominant_style | {desc}")
            if not good:
                print(f"      期望 code={expected['code']} count≥{expected['min_count']} / "
                      f"实际 {hint}")
        ok += good
    return ok


# ---------- format_hint ----------

def case_format():
    cases = [
        # (hint, 期望文本子串)
        ({"code": "ap", "label": "法术流", "count": 2,
          "names": ["大法师", "尤里卡"]},
         ["💡 本局可走法术流", "大法师 + 尤里卡"]),
        ({"code": "tank", "label": "坦克流", "count": 2, "names": ["星界躯体"]},
         ["💡 本局可走坦克流", "星界躯体"]),
        (None, [""]),
    ]
    ok = 0
    for hint, expected_list in cases:
        text = format_hint(hint)
        good = all(s in text for s in expected_list)
        ok += good
        print(f"{'✅' if good else '❌'} format_hint | hint={hint}")
        if not good:
            print(f"      期望包含 {expected_list} / 实际 {text!r}")
    return ok


# ---------- 一键组合 API ----------

def case_analyze_combo():
    results = _three(
        _result("大法师", "ARAM_Archmage"),
        _result("尤里卡", "ARAM_Eureka"),
        _result("星界躯体", "ARAM_CelestialBody"),
    )
    hint, text = analyze_combo(results)
    good = (hint is not None
            and hint["code"] == "ap"
            and "法术流" in text
            and "大法师" in text
            and "尤里卡" in text)
    print(f"{'✅' if good else '❌'} analyze_combo | 端到端")
    if not good:
        print(f"      hint={hint} text={text}")
    return 1 if good else 0


def main():
    total_ok = 0
    total_n = 0

    print("=" * 60)
    print("tag_augment")
    print("=" * 60)
    n = len(SAMPLES)
    ok = case_tag()
    total_ok += ok
    total_n += n

    print("\n" + "=" * 60)
    print("dominant_style")
    print("=" * 60)
    n = len(COMBO_CASES)
    ok = case_dominant()
    total_ok += ok
    total_n += n

    print("\n" + "=" * 60)
    print("format_hint")
    print("=" * 60)
    cases = [
        ({"code": "ap", "label": "法术流", "count": 2,
          "names": ["大法师", "尤里卡"]},
         ["💡 本局可走法术流", "大法师 + 尤里卡"]),
        ({"code": "tank", "label": "坦克流", "count": 2, "names": ["星界躯体"]},
         ["💡 本局可走坦克流", "星界躯体"]),
        (None, [""]),
    ]
    n = len(cases)
    ok = case_format()
    total_ok += ok
    total_n += n

    print("\n" + "=" * 60)
    print("analyze_combo (端到端)")
    print("=" * 60)
    n = 1
    ok = case_analyze_combo()
    total_ok += ok
    total_n += n

    print(f"\n通过 {total_ok}/{total_n}")
    sys.exit(0 if total_ok == total_n else 1)


if __name__ == "__main__":
    main()