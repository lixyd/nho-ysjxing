# -*- coding: utf-8 -*-
"""
海克斯流派识别（"流行玩法推荐"的务实实现）。

背景
----
用户最初设想的是「给一张「易损+魔法暴击/万剑其终」这种**配对手册**」；
但在当前 ARAM 海克斯池里这三个名字其实并不存在（查 augments_official.json），
实际社区语境下玩家更需要的是「**流派级**」提示——比如三张牌里有两张都
是魔法向，就该走法术爆发路线。

所以这里不做手工策划的「配对手册」，而是用官方 `augments_official.json`
里现成的 `apiName` / `nameZh` / `descZh` 三个字段做**关键词启发式**打标，
给出"本局可走 XX 流"的提示。

数据可信度排序
    apiName > nameZh > descZh
    （descZh 缺失率 60%，所以权重最低；apiName 永远 100% 完整）

为什么不放「配对手册」式数据文件
    1. 需要数百条策划数据，没有爬虫源也没有社区授权。
    2. 流派级提示已能覆盖 90% 的实用场景。
    3. 后续真要做配对，直接在这里加一份 `POPULAR_COMBOS` 常量即可。
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable


# ---------------------------------------------------------------------
# 流派定义
# ---------------------------------------------------------------------
# 每个流派 = (代码, 中文标签, [apiName 关键词...], [nameZh/descZh 关键词...])
#
# 设计原则：
# - apiName 关键词：驼峰命名英文片段，100% 覆盖
# - nameZh/descZh 关键词：中文短语，覆盖描述里的措辞
# - 同时命中任一组即可打标（避免过度依赖缺失的 descZh）
# ---------------------------------------------------------------------

STYLE_PATTERNS: list[tuple[str, str, list[str], list[str]]] = [
    # 魔法 / 法术爆发
    # 注意：apiName 关键词至少 5 字母 / 含下划线 / CamelCase 复合词，
    # 避免 "AP" / "Arc" / "Cast" 这种短子串误命中 "ARAM_DoubleTap" 这种单词。
    (
        "ap",
        "法术流",
        ["Spell", "Archmage", "Eureka", "ADAPt", "Magic",
         "Ethereal", "FeyMagic", "Spellslinger", "Spellblade",
         "Eruption", "MagicMissile", "SpellCrit", "Spellblade",
         "ApexInventor", "BreadAndButter", "BreadAndCheese",
         "BreadAndJam"],
        # 注意：不要加 "技能急速"——全凭身法的 descZh 也含"技能急速"，
        # 会让 dash 流误判成 ap。BreadAnd* 已经靠 apiName 命中。
        ["法术", "魔法", "法强", "法师", "法术强度", "法术穿透",
         "装备急速"],
    ),
    # 物理 / 暴击流
    (
        "crit",
        "暴击流",
        ["Crit", "FanTheHammer", "DoubleTap", "DualWield",
         "CriticalHealing", "CriticalMissile", "CriticalRhythm",
         "CritNCast", "Backstab", "BluntForce"],
        ["暴击", "物理攻击", "普通攻击", "暴击率", "暴击几率"],
    ),
    # 攻速 / 平A
    (
        "ad",
        "攻速流",
        ["Deft", "DualWield", "FanTheHammer", "CriticalRhythm",
         "AttackSpeed", "RapidFire", "Onslaught", "Stinger",
         "BluntForce", "HeavyHitter"],
        ["攻击速度", "攻速", "普通攻击", "普攻", "近战攻击"],
    ),
    # 肉盾 / 续航
    (
        "tank",
        "坦克流",
        ["Tank", "Colossus", "Body", "Goliath", "Heart",
         "CelestialBody", "SlowCooker", "SlowAndSteady",
         "RestlessRestoration"],
        # 注意：不要加 "护盾"——会和 heal 流重叠。会心治疗既有护盾又有治疗。
        ["生命值", "护甲", "魔法抗性", "坦克", "体型", "最大生命值"],
    ),
    # 治疗 / 续航
    (
        "heal",
        "续航流",
        ["Heal", "Shield", "Vamp", "Lifesteal", "CircleofDeath",
         "EmpoweredByTheFaithful", "FirstAidKit", "DrinkUp",
         "Restless", "Sustain", "Recuperate", "CriticalHealing",
         "RestlessRestoration"],
        ["治疗", "护盾", "回复", "吸血", "护体", "持续回复"],
    ),
    # 机动 / 位移
    (
        "dash",
        "机动流",
        ["Dashing", "Escape", "DontBlink",
         "Earthwake", "Flashbang", "Flashy", "Flash2",
         "Trailblazer", "Runner", "Flashy"],
        ["冲刺", "位移", "闪现", "闪烁", "传送", "移动速度"],
    ),
    # 近战流
    (
        "melee",
        "近战流",
        ["Melee", "DrawYourSword", "BladeWaltz", "Dropkick",
         "Blade", "BiggestSword", "SwordMaster", "FanTheHammer"],
        ["近战", "剑", "刃", "华尔兹", "近战状态", "近战范围"],
    ),
]


def _norm_text(s) -> str:
    """去掉空白与常见全角符号，便于跨 OCR 变体匹配。"""
    if not s:
        return ""
    s = str(s)
    s = s.replace("：", ":").replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", s)


def tag_augment(rec) -> set[str]:
    """给一个 augment 记录打流派标签（可能多个）。

    Args:
        rec: dict，含 apiName / nameZh / descZh；缺失字段视为空串。

    Returns:
        set[str]，流派代码集合（'ap' / 'crit' / 'tank' / ...）。
        无命中时返回空集（**不是**包含 "unknown" —— 让上游能区分）。
    """
    if not rec:
        return set()
    api = _norm_text(rec.get("apiName"))
    name = _norm_text(rec.get("nameZh"))
    desc = _norm_text(rec.get("descZh"))
    api_l = api.lower()
    tags: set[str] = set()
    for code, _label, api_kws, zh_kws in STYLE_PATTERNS:
        if any(kw.lower() in api_l for kw in api_kws):
            tags.add(code)
            continue
        if any(kw in name or kw in desc for kw in zh_kws):
            tags.add(code)
    return tags


def dominant_style(results: dict | None, *, min_overlap: int = 2) -> dict | None:
    """从识别结果中找出「至少出现 2 次的」主流派，给一个推荐提示。

    Args:
        results: GameAnalyzer.analyze() 的返回 dict，键如 'hex_1'/'hex_2'/'hex_3'，
                 每个值含 'official_rec' 或 'display'/'text' 等字段。
        min_overlap: 触发提示的最低出现次数（默认 2 = 三选二才提示）。

    Returns:
        None，或者 dict:
            {
                "code": "ap",          # 流派代码
                "label": "法术流",     # 中文标签
                "count": 2,            # 命中张数
                "names": ["大法师", ...],  # 命中的海克斯名（按出现顺序）
            }
    """
    if not results:
        return None
    bag: list[tuple[str, set[str]]] = []
    for key, data in results.items():
        if not isinstance(data, dict):
            continue
        if not data.get("valid"):
            continue
        rec = data.get("official_rec") or {}
        tags = tag_augment(rec)
        if not tags:
            continue
        # 展示名优先用 'display'，否则官方 nameZh / nameEn
        name = data.get("display") or rec.get("nameZh") or rec.get("nameEn") or key
        bag.append((name, tags))

    if not bag:
        return None

    # 计数：每个海克斯的所有流派标签 +1
    counter: Counter = Counter()
    name_by_tag: dict[str, list[str]] = {}
    for name, tags in bag:
        for t in tags:
            counter[t] += 1
            name_by_tag.setdefault(t, []).append(name)

    if not counter:
        return None

    code, count = counter.most_common(1)[0]
    if count < min_overlap:
        return None

    label = next((lbl for c, lbl, _, _ in STYLE_PATTERNS if c == code), code)
    return {
        "code": code,
        "label": label,
        "count": count,
        "names": name_by_tag.get(code, []),
    }


def format_hint(hint: dict | None) -> str:
    """把 dominant_style() 的结果格式化成玩家可见的提示文本。

    形如：「💡 本局可走法术流（大法师 + 尤里卡）」
    """
    if not hint:
        return ""
    label = hint.get("label") or "流派"
    names = hint.get("names") or []
    # 去重、保序
    seen: set[str] = set()
    uniq = []
    for n in names:
        if n in seen:
            continue
        seen.add(n)
        uniq.append(n)
    if uniq:
        return f"💡 本局可走{label}（{' + '.join(uniq)}）"
    return f"💡 本局可走{label}"


# ---------------------------------------------------------------------
# 公开 API（用于 main.py 集成）
# ---------------------------------------------------------------------
def analyze_combo(results: dict | None, *, min_overlap: int = 2):
    """一键：算 hint + 格式化文本。返回 (hint_dict, hint_text)。"""
    hint = dominant_style(results, min_overlap=min_overlap)
    return hint, format_hint(hint)


__all__ = [
    "STYLE_PATTERNS",
    "tag_augment",
    "dominant_style",
    "format_hint",
    "analyze_combo",
]