"""
ARAM 符文推荐 + 可选一键套用 (LCU lol-perks)

数据: data/aram_runes.json（本地脚手架，可按英雄中文名覆盖）
套用流程 (社区通行):
  1. GET  /lol-perks/v1/currentpage
  2. 若可删: DELETE /lol-perks/v1/pages/{id}
     否则尝试 PUT 覆盖当前页
  3. POST /lol-perks/v1/pages  {name, primaryStyleId, subStyleId, selectedPerkIds, current:true}
  4. 可选 PUT /lol-perks/v1/currentpage  设为当前页
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

from scripts.config import ARAM_RUNES_FILE, CHAMPION_ID_FILE


class RuneService:
    """符文推荐与 LCU 套用。"""

    PAGE_PREFIX = "ARAM助手"

    def __init__(self, lcu=None, runes_path: Optional[str] = None):
        self.lcu = lcu
        self.runes_path = runes_path or ARAM_RUNES_FILE
        self.data: Dict[str, Any] = {}
        self.cn_to_en: Dict[str, str] = {}
        self._load()

    def set_lcu(self, lcu):
        self.lcu = lcu

    def _load(self):
        if os.path.exists(self.runes_path):
            try:
                with open(self.runes_path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception as e:
                print(f"[runes] 加载失败: {e}")
                self.data = {}
        else:
            print(f"[runes] 未找到 {self.runes_path}，将仅使用 default")
            self.data = {}

        if os.path.exists(CHAMPION_ID_FILE):
            try:
                with open(CHAMPION_ID_FILE, "r", encoding="utf-8") as f:
                    self.cn_to_en = json.load(f)
            except Exception:
                self.cn_to_en = {}

    def reload(self):
        self._load()

    def _guess_role(self, hero_cn: str) -> Optional[str]:
        en = (self.cn_to_en.get(hero_cn) or "").replace(" ", "").lower()
        hints = self.data.get("role_hints") or {}
        for role, names in hints.items():
            for n in names:
                n_norm = str(n).replace(" ", "").lower()
                if n_norm == en or n_norm == hero_cn.lower() or n_norm in hero_cn:
                    return role
        return None

    def recommend(self, hero_cn: str) -> Tuple[Dict[str, Any], str]:
        """
        返回 (符文页 dict, 来源说明)
        符文页至少含: name, primaryStyleId, subStyleId, selectedPerkIds, primary_cn, secondary_cn
        """
        champs = self.data.get("champions") or {}
        if hero_cn in champs:
            page = dict(champs[hero_cn])
            return page, f"英雄专属 ({hero_cn})"

        role = self._guess_role(hero_cn)
        roles = self.data.get("roles") or {}
        if role and role in roles:
            page = dict(roles[role])
            return page, f"角色兜底 ({role})"

        page = dict(self.data.get("default") or {})
        if not page:
            page = {
                "name": f"{self.PAGE_PREFIX}-通用",
                "primaryStyleId": 8200,
                "subStyleId": 8100,
                "selectedPerkIds": [8229, 8226, 8210, 8237, 8139, 8135, 5008, 5008, 5002],
                "primary_cn": "巫术 · 奥术彗星",
                "secondary_cn": "主宰",
                "note": "内置硬编码兜底",
            }
        return page, "通用默认"

    def format_summary(self, page: Dict[str, Any], source: str = "") -> str:
        primary = page.get("primary_cn") or f"主系 {page.get('primaryStyleId')}"
        secondary = page.get("secondary_cn") or f"副系 {page.get('subStyleId')}"
        name = page.get("name") or "推荐符文"
        ids = page.get("selectedPerkIds") or []
        has_ids = bool(ids) and all(isinstance(x, int) for x in ids)
        lines = [
            f"📜 {name}",
            f"   主系: {primary}",
            f"   副系: {secondary}",
            f"   Perk IDs: {ids if has_ids else '（缺失，仅展示）'}",
        ]
        if source:
            lines.append(f"   来源: {source}")
        if page.get("note"):
            lines.append(f"   备注: {page['note']}")
        return "\n".join(lines)

    def apply(self, page: Dict[str, Any]) -> Tuple[bool, str]:
        """通过 LCU 套用符文页。返回 (成功?, 消息)。"""
        if not self.lcu:
            return False, "LCU 未初始化"
        if not self.lcu.is_connected() and not self.lcu.connect():
            return False, "LCU 未连接，请先启动客户端"

        perk_ids = page.get("selectedPerkIds") or []
        primary = page.get("primaryStyleId")
        secondary = page.get("subStyleId")
        if not perk_ids or primary is None or secondary is None:
            return False, "符文页缺少 ID，无法套用（请完善 data/aram_runes.json）"

        name = page.get("name") or f"{self.PAGE_PREFIX}-推荐"
        body = {
            "name": name,
            "primaryStyleId": int(primary),
            "subStyleId": int(secondary),
            "selectedPerkIds": [int(x) for x in perk_ids],
            "current": True,
        }

        # 尝试更新当前页；失败则删旧建新
        current = self.lcu.get_json("/lol-perks/v1/currentpage")
        if isinstance(current, dict) and current.get("id") is not None:
            pid = current["id"]
            # 优先 PUT 覆盖
            put_ok = self.lcu.put_ok(f"/lol-perks/v1/pages/{pid}", json_body=body)
            if put_ok:
                self.lcu.put_ok("/lol-perks/v1/currentpage", json_body=pid)
                return True, f"已覆盖当前符文页: {name}"

            # 删除后重建（仅当可删）
            if current.get("isDeletable", True) and not current.get("isTemporary"):
                self.lcu.delete_ok(f"/lol-perks/v1/pages/{pid}")

        created = self.lcu.post_json("/lol-perks/v1/pages", json_body=body)
        if isinstance(created, dict) and created.get("id") is not None:
            self.lcu.put_ok("/lol-perks/v1/currentpage", json_body=created["id"])
            return True, f"已创建并套用符文页: {name}"

        # 最后再试一次 POST 结果布尔
        if self.lcu.post_ok("/lol-perks/v1/pages", json_body=body):
            return True, f"已套用符文页: {name}"

        return False, "套用失败（页数已满或客户端拒绝）。可手动删一页后重试。"
