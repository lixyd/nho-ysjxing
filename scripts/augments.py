"""
海克斯图鉴（官方数据，离线可用）

数据来源：data/augments_official.json
  = Riot 官方 CommunityDragon（补丁 16.18.1）+ LCU augment-lists / cherry-augments
  552 条定义，含中文名、稀有度（白银/黄金/棱彩/事件抉择）、是否 ARAM 池、
  是否 ARAM 专属。

说明（不美化、不编造）：
  · 官方只有 223 / 552 条带中文描述，其余在 UI 里如实标注「官方暂无中文描述」
  · 描述里的 @占位符@ / %i:图标% 是客户端的动态标记，这里清掉并标注
    「数值以游戏内为准」，不做任何推算
  · 官方不提供胜率数据，因此本图鉴不含胜率（胜率需第三方联网统计源）
"""
from __future__ import annotations

import json
import os
import re

TOKEN_RE = re.compile(r"%i:[^%]+%")
PLACEHOLDER_RE = re.compile(r"@[^@]+@")
MUSTACHE_RE = re.compile(r"\{\{([^}]+)\}\}")
MUSTACHE_MAP = {"SpellName": "指定技能"}
SPACE_RE = re.compile(r"[ \t]+")


def clean_desc(text):
    """清理官方描述里的动态标记；返回 (文本, 是否含动态数值)。"""
    if not text:
        return "", False
    approx = False
    t = TOKEN_RE.sub("", text)
    if MUSTACHE_RE.search(t):
        t = MUSTACHE_RE.sub(lambda m: MUSTACHE_MAP.get(m.group(1), ""), t)
        t = t.replace("【】", "")
    if PLACEHOLDER_RE.search(t):
        approx = True
        t = PLACEHOLDER_RE.sub("", t)
    t = t.replace("\r", "")
    t = "\n".join(SPACE_RE.sub(" ", ln).strip() for ln in t.split("\n"))
    t = "\n".join(ln for ln in t.split("\n") if ln)
    return t.strip(), approx


class AugmentEntry:
    __slots__ = ("api", "name", "name_en", "rarity", "desc", "approx",
                 "aram", "aram_specific")

    def __init__(self, raw):
        self.api = str(raw.get("apiName") or "")
        self.name = str(raw.get("nameZh") or raw.get("nameEn") or self.api)
        self.name_en = str(raw.get("nameEn") or "")
        self.rarity = str(raw.get("rarityZh") or "")
        self.desc, self.approx = clean_desc(raw.get("descZh") or "")
        self.aram = bool(raw.get("inAramPool"))
        self.aram_specific = bool(raw.get("aramSpecific"))

    @property
    def has_desc(self):
        return bool(self.desc)

    def search_text(self):
        return f"{self.name} {self.name_en} {self.api}".lower()


class AugmentLibrary:
    """加载 + 检索。数据文件缺失时不抛异常，只是空库。"""

    RARITY_ORDER = ("棱彩", "黄金", "白银", "事件抉择")

    def __init__(self, path):
        self.path = path
        self.patch = ""
        self.entries = []
        self.error = ""
        self._load()

    def _load(self):
        try:
            if not os.path.exists(self.path):
                self.error = "缺少数据文件 augments_official.json"
                return
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.patch = str(data.get("patch") or "")
            self.entries = [AugmentEntry(x) for x in (data.get("augments") or [])]
        except Exception as e:
            self.error = f"海克斯数据读取失败: {e}"

    # ---------- 检索 ----------

    def counts(self):
        out = {"total": len(self.entries), "aram": 0, "with_desc": 0}
        for e in self.entries:
            if e.aram:
                out["aram"] += 1
            if e.has_desc:
                out["with_desc"] += 1
        return out

    def rarities(self):
        present = {e.rarity for e in self.entries}
        return [r for r in self.RARITY_ORDER if r in present] + \
               sorted(r for r in present if r and r not in self.RARITY_ORDER)

    def search(self, query="", rarity="", aram_only=True, limit=None):
        """按名称/描述关键词 + 稀有度 + 是否 ARAM 池过滤。"""
        q = (query or "").strip().lower()
        out = []
        for e in self.entries:
            if aram_only and not e.aram:
                continue
            if rarity and e.rarity != rarity:
                continue
            if q:
                hay = (e.search_text() + " " + e.desc).lower()
                if q not in hay:
                    continue
            out.append(e)
        out.sort(key=lambda e: (self._rank(e), e.name))
        return out[:limit] if limit else out

    def _rank(self, e):
        try:
            return self.RARITY_ORDER.index(e.rarity)
        except ValueError:
            return len(self.RARITY_ORDER)
