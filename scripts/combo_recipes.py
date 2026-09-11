# -*- coding: utf-8 -*-
"""热门玩法推荐 + 赌狗玩法提示。

数据来自 `data/combo_recipes.json`（社区攻略整理，用户可自行增删），按
「识别到的三张海克斯」匹配，**两个独立概念**：

- **routes（热门玩法路线）**：万剑归宗这类配装流。
  - core 命中：牌里出现核心海克斯（易损/暴击飞弹）→ 强提示「看到必拿」。
  - linked 命中：≥2 张联动海克斯 → 弱提示「可往 XX 方向靠」。
- **gambleAugments（赌狗玩法）**：质变/潘朵拉这类**随机结果**海克斯。
  选下去不知道变成什么——识别到就直接给「🎲 赌狗玩法」提示。

所有异常静默吞掉，绝不影响识别主流程。
"""
from __future__ import annotations

import json
import os
import random

try:
    from scripts.config import DATA_DIR
except ImportError:  # 直接运行本文件时
    from config import DATA_DIR

RECIPES_FILE = os.path.join(DATA_DIR, "combo_recipes.json")

_CACHE = {"mtime": 0, "data": {}}


def _load(path: str | None = None) -> dict:
    path = path or RECIPES_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _CACHE["mtime"] == mtime and _CACHE["data"]:
        return _CACHE["data"]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _CACHE["mtime"] = mtime
        _CACHE["data"] = data
        return data
    except Exception:
        return {}


def _api_names(items) -> set[str]:
    """routes 里 core/linked 的元素可能是 {'apiName': ..} 或纯字符串。"""
    out = set()
    for it in items or []:
        if isinstance(it, dict):
            api = it.get("apiName")
            if api:
                out.add(api)
        elif isinstance(it, str):
            out.add(it)
    return out


def _zh_names(items) -> dict[str, str]:
    """apiName -> 中文名（提示文案里用）。"""
    out = {}
    for it in items or []:
        if isinstance(it, dict) and it.get("apiName"):
            out[it["apiName"]] = it.get("nameZh") or it["apiName"]
    return out


# ---------------------------------------------------------------------
# 热门玩法路线（routes）
# ---------------------------------------------------------------------
def match_routes(api_names) -> list[dict]:
    """把识别到的海克斯 apiName 集合对到玩法路线上。

    Returns:
        list[dict]，每项 {"route", "level"("core"|"linked"), "hits"([apiName...])}，
        core 优先、命中多优先。
    """
    if not api_names:
        return []
    offered = {str(a) for a in api_names if a}
    if not offered:
        return []
    matches = []
    for route in _load().get("routes", []):
        if not isinstance(route, dict):
            continue
        core = _api_names(route.get("core"))
        linked = _api_names(route.get("linked"))
        core_hits = offered & core
        linked_hits = offered & linked
        if core_hits:
            matches.append({"route": route, "level": "core", "hits": sorted(core_hits)})
        elif len(linked_hits) >= 2:
            matches.append({"route": route, "level": "linked", "hits": sorted(linked_hits)})
    matches.sort(key=lambda m: (0 if m["level"] == "core" else 1, -len(m["hits"]),
                                m["route"].get("title", "")))
    return matches


def format_route_hint(match: dict) -> str:
    """把一条 route match 格式化成提示文本（hintCore/hintLinked 模板）。"""
    route = match["route"]
    level = match["level"]
    zh = _zh_names(route.get("core")) if level == "core" else _zh_names(route.get("linked"))
    names = "、".join(zh.get(a, a) for a in match["hits"]) or "?"
    tmpl = route.get("hintCore" if level == "core" else "hintLinked") or ""
    if not tmpl:
        return f"⚔ {route.get('title', '热门玩法')}：{names}"
    return (tmpl.replace("{hit}", names)
                .replace("{names}", names)
                .replace("{title}", route.get("title", "")))


def pick_route_hint(api_names, rng: random.Random | None = None):
    """匹配路线；多条 core 同级命中时随机挑一条（避免刷屏）。

    Returns:
        None 或 {"id", "title", "tagline", "level", "hits", "text", "tips"}
    """
    matches = match_routes(api_names)
    if not matches:
        return None
    rng = rng or random
    core_matches = [m for m in matches if m["level"] == "core"]
    pool = core_matches or matches
    chosen = rng.choice(pool) if len(pool) > 1 else pool[0]
    route = chosen["route"]
    return {
        "id": route.get("id", ""),
        "title": route.get("title", ""),
        "tagline": route.get("tagline", ""),
        "level": chosen["level"],
        "hits": chosen["hits"],
        "text": format_route_hint(chosen),
        "tips": route.get("tips", ""),
    }


# ---------------------------------------------------------------------
# 赌狗玩法（随机结果海克斯）
# ---------------------------------------------------------------------
def detect_gamble(api_names) -> dict | None:
    """检查识别到的海克斯里有没有「随机结果」类（质变/潘朵拉等）。

    Returns:
        None 或 {"names": [...中文名], "apis": [...], "text": 提示文本}
    """
    if not api_names:
        return None
    offered = {str(a) for a in api_names if a}
    if not offered:
        return None
    gamble_list = _load().get("gambleAugments", [])
    zh = _zh_names(gamble_list)
    apis = _api_names(gamble_list)
    hits = offered & apis
    if not hits:
        return None
    names = [zh.get(a, a) for a in sorted(hits)]
    tmpl = _load().get("gambleHint") or "🎲 赌狗玩法：「{names}」开出来是随机的！"
    text = tmpl.replace("{names}", "、".join(names))
    return {"names": names, "apis": sorted(hits), "text": text}


# ---------------------------------------------------------------------
# 一键组合（供 main.py 调用）
# ---------------------------------------------------------------------
def analyze_offers(api_names, rng: random.Random | None = None) -> dict:
    """对三张牌的 apiName 集合做全套分析。

    Returns:
        {"gamble": detect_gamble 结果 or None,
         "route": pick_route_hint 结果 or None}
    """
    return {
        "gamble": detect_gamble(api_names),
        "route": pick_route_hint(api_names, rng=rng),
    }


__all__ = [
    "RECIPES_FILE",
    "match_routes",
    "format_route_hint",
    "pick_route_hint",
    "detect_gamble",
    "analyze_offers",
]