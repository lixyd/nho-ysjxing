"""
海克斯自动识别调度器

触发信号（泉水/选牌窗口代理 + 等级检查点）:
  1. Live Client `isDead`（死亡后回泉水选牌；客户端不提供地图 XY，无法直接判定靠近泉水）
  2. Live Client 等级跨过检查点 1 / 7 / 11 / 15
  3. 现有 hex OCR 区域扫到疑似海克斯 UI 文字（能选牌的界面）

刷新后重推荐:
  - 三张选项 OCR 文本相对上次快照变化 → debounce(~0.7s) 后立即重识别并提醒
  - 手动「刷新识别」/ F6 → 立即重识别并提醒「刷新后已更新推荐」

保留手动「刷新识别」/ F6，不依赖自动流程。
无游戏进程注入、无自动点击。
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Optional, Set, Tuple

from scripts.config import HEX_LEVEL_CHECKPOINTS

# 选项变化 debounce / 自动分析冷却（秒）
OPTION_CHANGE_DEBOUNCE = 0.7
AUTO_ANALYZE_COOLDOWN = 1.5
POLL_INTERVAL = 0.55


class AutoHexWatcher:
    """轮询 Live 状态 + OCR 快照，到达选牌窗口或选项刷新时回调 analyze。"""

    def __init__(
        self,
        lcu,
        analyzer,
        get_hero: Callable[[], Optional[str]],
        on_results: Callable[[dict], None],
        on_status: Optional[Callable[[str], None]] = None,
        on_refresh_reminder: Optional[Callable[[], None]] = None,
        checkpoints=None,
        enabled: bool = True,
    ):
        self.lcu = lcu
        self.analyzer = analyzer
        self.get_hero = get_hero
        self.on_results = on_results
        self.on_status = on_status
        self.on_refresh_reminder = on_refresh_reminder
        self.checkpoints = tuple(checkpoints or HEX_LEVEL_CHECKPOINTS)
        self.enabled = enabled

        self._fired: Set[int] = set()
        self._last_level = 0
        self._idle_until_next = False  # 检查点选完后等待下一检查点（仍可响应死亡/刷新）
        self._pending_cp: Optional[int] = None
        self._pending_reason: Optional[str] = None
        self._last_poll = 0.0
        self._last_analyze = 0.0
        self._retry_after = 0.0
        self._in_game = False

        self._was_dead = False
        self._death_cycle_fired = False

        self._last_option_snapshot: Optional[str] = None
        self._option_change_since: Optional[float] = None
        self._ui_was_visible = False

    def set_enabled(self, value: bool):
        self.enabled = bool(value)

    def reset_match(self):
        """新对局开始时重置。"""
        self._fired.clear()
        self._last_level = 0
        self._idle_until_next = False
        self._pending_cp = None
        self._pending_reason = None
        self._retry_after = 0.0
        self._in_game = False
        self._was_dead = False
        self._death_cycle_fired = False
        self._last_option_snapshot = None
        self._option_change_since = None
        self._ui_was_visible = False

    def _status(self, msg: str):
        print(msg)
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _emit_reminder(self):
        if self.on_refresh_reminder:
            try:
                self.on_refresh_reminder()
            except Exception:
                pass

    def tick(self):
        """由控制器主循环频繁调用。"""
        if not self.enabled:
            return

        now = time.time()
        if now - self._last_poll < POLL_INTERVAL:
            return
        self._last_poll = now

        state = self.lcu.get_live_player_state() if self.lcu else None
        if not state:
            if self._in_game:
                self.reset_match()
            return

        self._in_game = True
        level = int(state.get("level") or 0)
        is_dead = bool(state.get("is_dead"))
        prev = self._last_level
        self._last_level = level

        # ---- 死亡边沿：新一轮死亡 → 可自动选牌（泉水代理）----
        if is_dead and not self._was_dead:
            self._death_cycle_fired = False
            self._status("💀 检测到死亡/回泉水窗口，准备自动识别海克斯…")
            self._queue_reason("death")
        elif not is_dead and self._was_dead:
            self._death_cycle_fired = False
        self._was_dead = is_dead

        # ---- 等级检查点 ----
        newly = []
        for cp in self.checkpoints:
            if cp not in self._fired and prev < cp <= level:
                newly.append(cp)
            if (
                cp not in self._fired
                and level >= cp
                and prev == 0
                and cp == self.checkpoints[0]
            ):
                if cp not in newly:
                    newly.append(cp)
        if newly:
            cp = max(newly)
            self._pending_cp = cp
            self._idle_until_next = False
            self._retry_after = 0.0
            self._queue_reason("checkpoint")
            self._status(f"⚡ 到达海克斯检查点 Lv.{cp}，准备自动识别…")

        # ---- OCR：选牌窗口探测 + 选项刷新检测 ----
        want_full = (
            is_dead
            or self._pending_reason is not None
            or self._ui_was_visible
            or self._option_change_since is not None
            or self._last_option_snapshot is not None
        )
        snapshot = None
        ui_visible = False
        if want_full:
            snapshot, ui_visible = self._snapshot_options()
        else:
            # 战斗中轻量抽查中间区；一旦出现 UI 再切全量快照
            if self._detect_hex_ui_quick():
                snapshot, ui_visible = self._snapshot_options()

        if ui_visible and not self._ui_was_visible:
            if self._pending_reason is None:
                self._queue_reason("ui")
                self._status("🧿 检测到海克斯选牌 UI，准备自动识别…")
        self._ui_was_visible = ui_visible

        if ui_visible and snapshot:
            if self._last_option_snapshot is None:
                if self._pending_reason is None:
                    self._queue_reason("ui")
            elif snapshot != self._last_option_snapshot:
                if self._option_change_since is None:
                    self._option_change_since = now
                    self._status("🔄 海克斯选项已变化，即将更新推荐…")
                elif now - self._option_change_since >= OPTION_CHANGE_DEBOUNCE:
                    self._queue_reason("option_change")
                    self._option_change_since = None
            else:
                self._option_change_since = None
        else:
            if not ui_visible:
                if self._last_option_snapshot is not None:
                    self._last_option_snapshot = None
                self._option_change_since = None

        # ---- 执行排队的分析 ----
        if self._pending_reason is None:
            return
        if now < self._retry_after:
            return
        if now - self._last_analyze < AUTO_ANALYZE_COOLDOWN:
            return

        reason = self._pending_reason
        if reason == "checkpoint" and self._idle_until_next:
            self._pending_reason = None
            return

        if reason == "death" and self._death_cycle_fired:
            self._pending_reason = None
            return

        if reason in ("checkpoint", "death", "ui"):
            ui_likely = is_dead or ui_visible or self._detect_hex_ui_quick()
            if not ui_likely:
                self._retry_after = now + 1.2
                return

        hero = self.get_hero()
        if not hero:
            self._status("⚠ 自动海克斯: 尚未锁定英雄，跳过")
            self._retry_after = now + 3.0
            return

        label = {
            "checkpoint": f"检查点 Lv.{self._pending_cp}",
            "death": "死亡/泉水",
            "ui": "选牌 UI",
            "option_change": "选项刷新",
            "manual": "手动",
        }.get(reason, reason)
        self._last_analyze = now
        self._status(f"🔎 自动识别海克斯 ({label} / {hero})…")
        try:
            results = self.analyzer.analyze(hero)
        except Exception as e:
            self._status(f"❌ 自动识别失败: {e}")
            self._retry_after = now + 2.0
            return

        post_snap, post_ui = self._snapshot_options()
        if post_ui and post_snap:
            self._last_option_snapshot = post_snap
        elif snapshot:
            self._last_option_snapshot = snapshot

        valid_count = sum(1 for v in (results or {}).values() if v.get("valid"))
        error_empty = (
            all(
                (
                    v.get("error")
                    and (
                        "无文字" in str(v.get("text", ""))
                        or "截图" in str(v.get("text", ""))
                    )
                )
                for v in (results or {}).values()
            )
            if results
            else True
        )

        remind = reason in ("option_change", "manual")

        if valid_count > 0:
            self.on_results(results)
            if remind:
                self._emit_reminder()
            if reason == "checkpoint" and self._pending_cp is not None:
                self._fired.add(self._pending_cp)
                self._status(f"✅ 自动识别完成 (检查点 Lv.{self._pending_cp})")
                self._pending_cp = None
                self._arm_idle_watch()
            elif reason == "death":
                self._death_cycle_fired = True
                self._status("✅ 自动识别完成 (死亡/泉水)")
            elif reason == "option_change":
                self._status("✅ 刷新后已更新推荐")
            else:
                self._status(f"✅ 自动识别完成 ({label})")
            self._pending_reason = None
        elif error_empty:
            if reason == "checkpoint":
                if self._pending_cp in self._fired or not is_dead:
                    self._arm_idle_watch()
            self._retry_after = now + 1.0
        else:
            self.on_results(results or {})
            if remind:
                self._emit_reminder()
            if reason == "checkpoint" and self._pending_cp is not None:
                self._fired.add(self._pending_cp)
                self._pending_cp = None
                self._arm_idle_watch()
            elif reason == "death":
                self._death_cycle_fired = True
            self._pending_reason = None

    def _queue_reason(self, reason: str):
        """排队原因；选项变化 / 手动优先。"""
        priority = {
            "option_change": 5,
            "manual": 5,
            "death": 4,
            "checkpoint": 3,
            "ui": 2,
        }
        cur = self._pending_reason
        if cur is None or priority.get(reason, 0) >= priority.get(cur, 0):
            self._pending_reason = reason
            if reason != "checkpoint":
                self._idle_until_next = False

    def _arm_idle_watch(self):
        """检查点选完后 idle 到下一检查点（死亡/刷新仍可触发）。"""
        self._idle_until_next = True

    def notify_manual_refresh(self, results: Optional[dict] = None):
        """手动刷新后：更新 OCR 快照基线，并提示「刷新后已更新推荐」。"""
        snap, ui = self._snapshot_options()
        if ui and snap:
            self._last_option_snapshot = snap
        self._option_change_since = None
        self._pending_reason = None
        has_signal = False
        if results:
            has_signal = any(
                (not v.get("error"))
                or ("无文字" not in str(v.get("text", "")))
                for v in results.values()
            )
        if has_signal or (ui and snap):
            self._emit_reminder()
            self._status("✅ 刷新后已更新推荐")

    def mark_ui_gone_if_needed(self, results: dict):
        """分析后若全无文字，视为选完。"""
        if not results:
            self._idle_until_next = True
            self._pending_cp = None
            self._last_option_snapshot = None
            return
        empty = all(
            (v.get("error") and "无文字" in str(v.get("text", "")))
            for v in results.values()
        )
        if empty:
            self._idle_until_next = True
            self._pending_cp = None
            self._last_option_snapshot = None

    def _snapshot_options(self) -> Tuple[Optional[str], bool]:
        """对三个 hex 区域 OCR，返回 (规范化拼接快照, UI是否可见)。"""
        try:
            images = self.analyzer.capture_all_regions()
            if not images:
                return None, False
            parts: Dict[str, str] = {}
            for key in sorted(images.keys()):
                img = images[key]
                res_ocr, _ = self.analyzer.ocr(img)
                txt = "".join([line[1] for line in res_ocr]) if res_ocr else ""
                txt = txt.replace(" ", "").replace(".", "").strip()
                parts[key] = txt
            texts = [parts.get(k, "") for k in ("hex_1", "hex_2", "hex_3")]
            if not any(texts):
                texts = [parts[k] for k in sorted(parts.keys())]
            nonempty = [t for t in texts if len(t) >= 2]
            if not nonempty:
                return None, False
            snap = "|".join(t if t else "_" for t in texts)
            return snap, True
        except Exception:
            return None, False

    def _detect_hex_ui_quick(self) -> bool:
        """轻量探测（仅中间区）。"""
        try:
            images = self.analyzer.capture_all_regions()
            if not images:
                return False
            key = "hex_2" if "hex_2" in images else next(iter(images))
            img = images[key]
            res_ocr, _ = self.analyzer.ocr(img)
            txt = "".join([line[1] for line in res_ocr]) if res_ocr else ""
            txt = txt.replace(" ", "").strip()
            return len(txt) >= 2
        except Exception:
            return False

    def _detect_hex_ui(self) -> bool:
        return self._detect_hex_ui_quick()
