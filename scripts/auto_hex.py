"""
海克斯自动识别调度器

触发信号（泉水/选牌窗口代理 + 等级检查点）:
  1. Live Client `isDead`（死亡后回泉水选牌；客户端不提供地图 XY，无法直接判定靠近泉水）
  2. Live Client 等级跨过检查点 1 / 7 / 11 / 15
  3. 现有 hex OCR 区域扫到疑似海克斯 UI 文字（能选牌的界面）

检查点「欠账」(owed):
  - 到达 Lv 检查点但选牌 UI 尚不可用（未死亡/未打开）时，只记欠账，不标完成、不狂刷 OCR
  - 之后死亡/泉水 或 选牌 UI 出现时兑现欠账：开始识别，并在 UI 仍打开时持续刷新
  - UI 消失（已选定）后清除欠账并标记该检查点完成；同一检查点只欠/兑现一次
  - 无欠账时的死亡选牌仍独立工作

选牌 UI 可见期间持续刷新:
  - 首次识别成功后，只要选牌 UI 仍在，按冷却间隔持续重跑 OCR/分析
  - UI 消失后停止高频刷新

刷新后重推荐:
  - 三张选项 OCR 文本相对上次快照变化 → debounce(~0.7s) 后立即重识别并提醒
  - 手动「刷新识别」按钮 → 立即重识别并提醒「刷新后已更新推荐」

保留手动「刷新识别」按钮，不依赖自动流程。
无游戏进程注入、无自动点击。
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Optional, Set, Tuple

from scripts.config import HEX_LEVEL_CHECKPOINTS

# 选项变化 debounce / 自动分析冷却（秒）
OPTION_CHANGE_DEBOUNCE = 0.7
AUTO_ANALYZE_COOLDOWN = 1.5
# 选牌 UI 仍在时持续刷新间隔（尊重冷却，避免 OCR 打满 CPU）
UI_KEEP_REFRESH_INTERVAL = 1.5
# 欠账等待期间轻量重试间隔（不挂 pending，避免全量 OCR 空转）
OWED_LIGHT_WAIT = 2.0
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
        self._owed: Set[int] = set()  # 欠账检查点（UI 未开时记下，稍后兑现）
        self._last_level = 0
        self._idle_until_next = False  # 检查点选完后等待下一检查点（仍可响应死亡/刷新）
        self._pending_cp: Optional[int] = None  # 正在兑现的检查点
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
        # 本轮选牌已出过有效推荐；UI 消失后结算欠账 / 结束持续刷新
        self._pick_cycle_active = False

    def set_enabled(self, value: bool):
        self.enabled = bool(value)

    def reset_match(self):
        """新对局开始时重置。"""
        self._fired.clear()
        self._owed.clear()
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
        self._pick_cycle_active = False

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

    def _owe_checkpoint(self, cp: int):
        """记录欠账；同一检查点只欠一次，已完成的不再欠。"""
        if cp in self._fired or cp in self._owed:
            return
        self._owed.add(cp)
        self._idle_until_next = False
        self._status(f"⚡ 到达海克斯检查点 Lv.{cp}，选牌窗口未开 — 已记欠账，待死亡/泉水或 UI 出现后兑现…")

    def _redeem_owed_if_ready(self, *, is_dead: bool, ui_visible: bool, source: str):
        """死亡/泉水或选牌 UI 出现时兑现欠账。"""
        if not self._owed:
            return False
        if not (is_dead or ui_visible):
            return False
        # 取最高未完成欠账（等级只增；多欠时先兑现最大）
        unpaid = [cp for cp in self._owed if cp not in self._fired]
        if not unpaid:
            self._owed.clear()
            return False
        cp = max(unpaid)
        self._pending_cp = cp
        self._idle_until_next = False
        self._retry_after = 0.0
        self._queue_reason("checkpoint")
        self._status(f"💎 兑现检查点 Lv.{cp}（{source}），开始识别…")
        return True

    def _settle_pick_cycle_on_ui_gone(self):
        """选牌 UI 消失：若本轮已出推荐，则完成欠账并停止持续刷新。"""
        if self._pending_reason == "ui_keep":
            self._pending_reason = None
        if not self._pick_cycle_active:
            return
        self._pick_cycle_active = False
        if self._pending_cp is not None:
            cp = self._pending_cp
            self._fired.add(cp)
            self._owed.discard(cp)
            self._status(f"✅ 检查点 Lv.{cp} 选牌完成（欠账已清）")
            self._pending_cp = None
            self._arm_idle_watch()
        elif self._owed and not self._pending_cp:
            # 死亡/UI 周期结束但无绑定检查点：不改 owed
            pass
        self._death_cycle_fired = True

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

        # ---- 死亡边沿：新一轮死亡 → 兑现欠账或独立死亡选牌 ----
        death_edge = is_dead and not self._was_dead
        if death_edge:
            self._death_cycle_fired = False
            self._pick_cycle_active = False
            if not self._redeem_owed_if_ready(is_dead=True, ui_visible=False, source="死亡/泉水"):
                self._status("💀 检测到死亡/回泉水窗口，准备自动识别海克斯…")
                self._queue_reason("death")
        elif not is_dead and self._was_dead:
            self._death_cycle_fired = False
        self._was_dead = is_dead

        # ---- 等级检查点：UI 未开则只记欠账，不狂刷 OCR ----
        newly = []
        for cp in self.checkpoints:
            if cp not in self._fired and cp not in self._owed and prev < cp <= level:
                newly.append(cp)
            if (
                cp not in self._fired
                and cp not in self._owed
                and level >= cp
                and prev == 0
                and cp == self.checkpoints[0]
            ):
                if cp not in newly:
                    newly.append(cp)
        if newly:
            cp = max(newly)
            self._owe_checkpoint(cp)
            # 已在死亡/泉水中则立刻尝试兑现（不等下一次死亡边沿）
            if is_dead:
                self._redeem_owed_if_ready(
                    is_dead=True, ui_visible=False, source="死亡/泉水"
                )

        # ---- OCR：仅在可能选牌时全量；欠账等待期用轻量抽查 ----
        awaiting_pick = bool(self._owed) or self._pending_reason is not None or self._pick_cycle_active
        want_full = (
            is_dead
            or self._pending_reason is not None
            or self._ui_was_visible
            or self._option_change_since is not None
            or self._last_option_snapshot is not None
            or self._pick_cycle_active
        )
        snapshot = None
        ui_visible = False
        if want_full:
            snapshot, ui_visible = self._snapshot_options()
        elif awaiting_pick:
            # 欠账中：偶尔轻量探测 UI，避免全量三区 OCR 空转
            if now >= self._retry_after and self._detect_hex_ui_quick():
                snapshot, ui_visible = self._snapshot_options()
            elif now >= self._retry_after:
                self._retry_after = now + OWED_LIGHT_WAIT
        else:
            if self._detect_hex_ui_quick():
                snapshot, ui_visible = self._snapshot_options()

        # UI 出现：兑现欠账；无欠账则普通 UI 触发
        if ui_visible and not self._ui_was_visible:
            if self._owed:
                self._redeem_owed_if_ready(
                    is_dead=is_dead, ui_visible=True, source="选牌 UI"
                )
            elif self._pending_reason is None:
                self._queue_reason("ui")
                self._status("🧿 检测到海克斯选牌 UI，准备自动识别…")
        self._ui_was_visible = ui_visible

        if ui_visible and snapshot:
            if self._last_option_snapshot is None:
                if self._pending_reason is None and not self._owed:
                    self._queue_reason("ui")
                elif self._owed and self._pending_reason is None:
                    self._redeem_owed_if_ready(
                        is_dead=is_dead, ui_visible=True, source="选牌 UI"
                    )
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
                self._settle_pick_cycle_on_ui_gone()

        # 欠账且本帧已能选牌，但尚未挂 pending：补一次兑现
        if self._owed and (is_dead or ui_visible) and self._pending_reason is None:
            self._redeem_owed_if_ready(
                is_dead=is_dead,
                ui_visible=ui_visible,
                source="死亡/泉水" if is_dead else "选牌 UI",
            )

        # 选牌 UI 仍在且无其它待办时，保持持续刷新（直到玩家选定）
        if ui_visible and self._pending_reason is None and (
            self._pick_cycle_active or self._pending_cp is not None
        ):
            self._queue_reason("ui_keep")
        elif ui_visible and self._pending_reason is None and not self._owed:
            # 无欠账的独立选牌 UI：同样持续刷新直至选定
            self._queue_reason("ui_keep")

        # ---- 执行排队的分析 ----
        if self._pending_reason is None:
            return
        if now < self._retry_after:
            return
        if now - self._last_analyze < AUTO_ANALYZE_COOLDOWN:
            return

        reason = self._pending_reason
        if reason == "checkpoint" and self._idle_until_next and not self._owed:
            self._pending_reason = None
            return

        if reason == "death" and self._death_cycle_fired:
            self._pending_reason = None
            return

        if reason in ("checkpoint", "death", "ui", "ui_keep"):
            ui_likely = is_dead or ui_visible or self._detect_hex_ui_quick()
            if not ui_likely:
                if reason == "ui_keep":
                    self._settle_pick_cycle_on_ui_gone()
                    return
                if reason == "checkpoint":
                    # UI 仍不可用：保持欠账，撤掉 pending，避免 OCR 空转
                    if self._pending_cp is not None:
                        self._owed.add(self._pending_cp)
                    self._pending_reason = None
                    self._retry_after = now + OWED_LIGHT_WAIT
                    return
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
            "ui_keep": "选牌持续刷新",
            "option_change": "选项刷新",
            "manual": "手动",
        }.get(reason, reason)
        self._last_analyze = now
        if reason != "ui_keep":
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
                # 尚未选定：不标 _fired；等 UI 消失再结算欠账
                self._status(f"✅ 检查点 Lv.{self._pending_cp} 推荐已更新（待选定）")
            elif reason == "death":
                self._death_cycle_fired = True
                self._status("✅ 自动识别完成 (死亡/泉水)")
            elif reason == "option_change":
                self._status("✅ 刷新后已更新推荐")
            elif reason == "ui_keep":
                pass
            else:
                self._status(f"✅ 自动识别完成 ({label})")
            self._pick_cycle_active = True
            # 有效识别后默认继续刷新；是否选完由后续 tick 观察 UI 消失再结算
            # （避免 analyze 成功但 post 快照偶发失败时误清欠账）
            self._continue_or_stop_refresh(
                post_ui=True if (post_ui or valid_count > 0) else False,
                now=now,
            )
        elif error_empty:
            if reason == "checkpoint":
                # 空结果且 UI 不可用：保持欠账，撤 pending，轻量等待（死亡中也不狂刷）
                if self._pending_cp is not None:
                    self._owed.add(self._pending_cp)
                if not post_ui:
                    self._pending_reason = None
                    self._retry_after = now + OWED_LIGHT_WAIT
                    return
            if not post_ui and reason == "ui_keep":
                self._settle_pick_cycle_on_ui_gone()
            else:
                self._retry_after = now + 1.0
        else:
            self.on_results(results or {})
            if remind:
                self._emit_reminder()
            if reason == "death":
                self._death_cycle_fired = True
            self._pick_cycle_active = True
            self._continue_or_stop_refresh(post_ui=bool(post_ui), now=now)

    def _continue_or_stop_refresh(self, *, post_ui: bool, now: float):
        """选牌 UI 仍在则排队持续刷新；UI 消失则停止该轮高频识别。"""
        if post_ui:
            self._pending_reason = "ui_keep"
            self._retry_after = now + UI_KEEP_REFRESH_INTERVAL
        else:
            self._pending_reason = None

    def _queue_reason(self, reason: str):
        """排队原因；选项变化 / 手动优先。"""
        priority = {
            "option_change": 5,
            "manual": 5,
            "death": 4,
            "checkpoint": 3,
            "ui": 2,
            "ui_keep": 1,
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
            self._pick_cycle_active = True
        # 选牌 UI 仍在：手动刷新后继续自动重识别，直到选完
        if ui:
            self._pending_reason = "ui_keep"
            self._retry_after = time.time() + UI_KEEP_REFRESH_INTERVAL

    def mark_ui_gone_if_needed(self, results: dict):
        """分析后若全无文字，视为选完。"""
        if not results:
            self._last_option_snapshot = None
            self._settle_pick_cycle_on_ui_gone()
            self._arm_idle_watch()
            return
        empty = all(
            (v.get("error") and "无文字" in str(v.get("text", "")))
            for v in results.values()
        )
        if empty:
            self._last_option_snapshot = None
            self._settle_pick_cycle_on_ui_gone()
            self._arm_idle_watch()

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
