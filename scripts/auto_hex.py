"""
海克斯自动识别调度器

触发条件:
  - Live Client Data 读到本地等级跨过检查点 1 / 7 / 11 / 15
  - 且 (玩家死亡 或 OCR 区域能扫到疑似海克斯 UI 文字)
  - 每个检查点只自动触发一次；选完后（识别区无有效文字）进入 idle，等待下一检查点

保留手动「刷新识别」/ F6，不依赖自动流程。
无游戏进程注入、无自动点击。
"""
from __future__ import annotations

import time
from typing import Callable, Optional, Set

from scripts.config import HEX_LEVEL_CHECKPOINTS


class AutoHexWatcher:
    """在游戏内轮询等级，到达检查点时回调 analyze。"""

    def __init__(
        self,
        lcu,
        analyzer,
        get_hero: Callable[[], Optional[str]],
        on_results: Callable[[dict], None],
        on_status: Optional[Callable[[str], None]] = None,
        checkpoints=None,
        enabled: bool = True,
    ):
        self.lcu = lcu
        self.analyzer = analyzer
        self.get_hero = get_hero
        self.on_results = on_results
        self.on_status = on_status
        self.checkpoints = tuple(checkpoints or HEX_LEVEL_CHECKPOINTS)
        self.enabled = enabled

        self._fired: Set[int] = set()
        self._last_level = 0
        self._idle_until_next = False  # UI 消失后等待下一检查点
        self._pending_cp: Optional[int] = None
        self._last_poll = 0.0
        self._last_analyze = 0.0
        self._retry_after = 0.0
        self._in_game = False

    def set_enabled(self, value: bool):
        self.enabled = bool(value)

    def reset_match(self):
        """新对局开始时重置。"""
        self._fired.clear()
        self._last_level = 0
        self._idle_until_next = False
        self._pending_cp = None
        self._retry_after = 0.0
        self._in_game = False

    def _status(self, msg: str):
        print(msg)
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def tick(self):
        """由控制器主循环频繁调用。"""
        if not self.enabled:
            return

        now = time.time()
        if now - self._last_poll < 0.7:
            return
        self._last_poll = now

        state = self.lcu.get_live_player_state() if self.lcu else None
        if not state:
            if self._in_game:
                # 离开对局
                self.reset_match()
            return

        self._in_game = True
        level = int(state.get("level") or 0)
        is_dead = bool(state.get("is_dead"))
        prev = self._last_level
        self._last_level = level

        # 检测新跨过的检查点
        newly = []
        for cp in self.checkpoints:
            if cp not in self._fired and prev < cp <= level:
                newly.append(cp)
            # 开局已在检查点（如 level 1）
            if cp not in self._fired and level >= cp and prev == 0 and cp == self.checkpoints[0]:
                if cp not in newly:
                    newly.append(cp)

        if newly:
            cp = max(newly)
            self._pending_cp = cp
            self._idle_until_next = False
            self._retry_after = 0.0
            self._status(f"⚡ 到达海克斯检查点 Lv.{cp}，准备自动识别…")

        if self._pending_cp is None or self._idle_until_next:
            return

        if now < self._retry_after:
            return
        if now - self._last_analyze < 2.0:
            return

        # 优先死亡时弹窗；否则尝试探测 UI 文字
        ui_likely = is_dead or self._detect_hex_ui()
        if not ui_likely:
            # 稍后再试（可能刚升级、UI 尚未弹出）
            self._retry_after = now + 1.5
            return

        hero = self.get_hero()
        if not hero:
            self._status("⚠ 自动海克斯: 尚未锁定英雄，跳过")
            self._retry_after = now + 3.0
            return

        self._last_analyze = now
        self._status(f"🔎 自动识别海克斯 (Lv.{self._pending_cp} / {hero})…")
        try:
            results = self.analyzer.analyze(hero)
        except Exception as e:
            self._status(f"❌ 自动识别失败: {e}")
            self._retry_after = now + 2.0
            return

        valid_count = sum(1 for v in (results or {}).values() if v.get("valid"))
        error_empty = all(
            (v.get("error") and ("无文字" in str(v.get("text", "")) or "截图" in str(v.get("text", ""))))
            for v in (results or {}).values()
        ) if results else True

        if valid_count > 0:
            self.on_results(results)
            self._fired.add(self._pending_cp)
            self._status(f"✅ 自动识别完成 (检查点 Lv.{self._pending_cp})，等待选择后空闲")
            # 选完后 UI 会消失；进入监控 idle
            self._idle_until_next = False
            self._pending_cp = None
            # 短暂后再确认 UI 是否消失，消失则真正 idle
            self._arm_idle_watch()
        elif error_empty:
            # UI 可能已关掉
            if self._pending_cp in self._fired or is_dead is False:
                self._arm_idle_watch()
            self._retry_after = now + 1.2
        else:
            # 有文字但匹配失败，仍展示给用户
            self.on_results(results or {})
            self._fired.add(self._pending_cp)
            self._pending_cp = None
            self._arm_idle_watch()

    def _arm_idle_watch(self):
        """标记：下一轮若 UI 无文字则 idle 到下一检查点。"""
        self._idle_until_next = True

    def notify_manual_refresh(self):
        """手动刷新后，不改变检查点 fired 集合。"""
        pass

    def mark_ui_gone_if_needed(self, results: dict):
        """分析后若全无文字，视为选完，idle。"""
        if not results:
            self._idle_until_next = True
            self._pending_cp = None
            return
        empty = all(
            (v.get("error") and "无文字" in str(v.get("text", "")))
            for v in results.values()
        )
        if empty:
            self._idle_until_next = True
            self._pending_cp = None

    def _detect_hex_ui(self) -> bool:
        """轻量探测: 对三个区域做 OCR，任一有较长中文/文字则认为 UI 存在。"""
        try:
            images = self.analyzer.capture_all_regions()
            if not images:
                return False
            # 只抽查中间区域，降低开销
            key = "hex_2" if "hex_2" in images else next(iter(images))
            img = images[key]
            res_ocr, _ = self.analyzer.ocr(img)
            txt = "".join([line[1] for line in res_ocr]) if res_ocr else ""
            txt = txt.replace(" ", "").strip()
            return len(txt) >= 2
        except Exception:
            return False
