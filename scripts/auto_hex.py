"""
海克斯自动识别调度器

硬门禁（必须先满足，否则 tick 完全空转，不截图、不 OCR）:
  - LCU gameflow phase == InProgress（GameStart 加载中不够）
  - 且 Live Client Data 返回真实玩家数据
  - 等价于「已进入对局且右上角读秒出现」；大厅/选人/客户端主页绝不识别

触发信号（泉水/选牌窗口代理 + 等级检查点；均在硬门禁之后）:
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
# 死亡抑制窗口（秒）：死亡期间 + 死亡状态解除后的这段窗口内，禁止任何 UI 探测。
# LCU 的 is_dead 在死亡瞬间存在更新滞后（health/playerlist 刷新延迟），
# 而泉水画面（暗底 + 复活倒计时亮字）恰好满足像素预筛，会被误判成选牌 UI 而误弹提示。
# 用窗口兜底：死亡时持续刷新，死亡解除后仍抑制一小段，避免泉水画面误触发。
DEAD_SUPPRESS_WINDOW = 2.5


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
        on_invalidate: Optional[Callable[[], None]] = None,
        checkpoints=None,
        enabled: bool = True,
    ):
        self.lcu = lcu
        self.analyzer = analyzer
        self.get_hero = get_hero
        self.on_results = on_results
        self.on_status = on_status
        self.on_refresh_reminder = on_refresh_reminder
        # 选项变化时立刻回调：让界面把旧推荐清掉，别让上一轮的名字挂在新牌上
        self.on_invalidate = on_invalidate
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
        # 死亡抑制截止时刻：死亡期间 + 解除后窗口内禁止 UI 探测（防泉水画面误判）
        self._dead_until = 0.0

        self._last_option_snapshot: Optional[str] = None
        self._option_change_since: Optional[float] = None
        self._ui_was_visible = False
        # 本轮选牌已出过有效推荐；UI 消失后结算欠账 / 结束持续刷新
        self._pick_cycle_active = False
        # 未匹配抑制：识别到文字但未命中数据库时，死亡持续期间不反复自动重试
        self._suppress_retry = False
        # OCR 快照/轻量探测 整帧变化缓存
        self._snap_cache_key = None
        self._snap_cache = None
        self._quick_cache_key = None
        self._quick_cache = None

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
        self._dead_until = 0.0
        self._last_option_snapshot = None
        self._option_change_since = None
        self._ui_was_visible = False
        self._pick_cycle_active = False
        self._suppress_retry = False
        self._snap_cache_key = None
        self._snap_cache = None
        self._quick_cache_key = None
        self._quick_cache = None

    def _status(self, msg: str):
        print(msg)
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _notify_invalidate(self):
        """告知界面：当前推荐已失效，请先清掉（避免显示上一轮的名字）。"""
        if not self.on_invalidate:
            return
        try:
            self.on_invalidate()
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

    def _gate_in_live_game(self) -> bool:
        """InProgress + Live Client 真实玩家数据；否则禁止一切 hex OCR。"""
        if not self.lcu:
            return False
        try:
            if hasattr(self.lcu, "is_in_live_game"):
                return bool(self.lcu.is_in_live_game())
            phase = self.lcu.get_gameflow_phase()
            if phase != "InProgress":
                return False
            return self.lcu.get_live_player_state() is not None
        except Exception:
            return False

    def tick(self):
        """由控制器主循环频繁调用。未进入对局时完全 no-op（不截图、不 OCR）。"""
        if not self.enabled:
            return

        now = time.time()
        if now - self._last_poll < POLL_INTERVAL:
            return
        self._last_poll = now

        # 硬门禁：大厅 / ChampSelect / GameStart / Live 未就绪 → 空转
        if not self._gate_in_live_game():
            if self._in_game:
                self.reset_match()
                self._status("⏹ 已离开对局，停止海克斯识别")
            return

        state = self.lcu.get_live_player_state() if self.lcu else None
        if not state:
            if self._in_game:
                self.reset_match()
                self._status("⏹ Live Client 已断开，停止海克斯识别")
            return

        self._in_game = True
        level = int(state.get("level") or 0)
        is_dead = bool(state.get("is_dead"))
        prev = self._last_level
        self._last_level = level

        # 死亡抑制：死亡期间持续刷新窗口（覆盖 LCU is_dead 滞后/抖动）；
        # 同时清掉上一轮选牌的 OCR 快照残留，避免泉水画面被误判成"选项变化"而触发识别。
        if is_dead:
            self._dead_until = now + DEAD_SUPPRESS_WINDOW
            self._last_option_snapshot = None
            self._option_change_since = None
            self._ui_was_visible = False
        # 死亡期间 + 解除后窗口内：一律禁止 UI 探测（防泉水画面误判选牌 UI）
        dead_suppressed = now < self._dead_until

        # ---- 死亡边沿：新一轮死亡 → 兑现欠账或独立死亡选牌 ----
        death_edge = is_dead and not self._was_dead
        if death_edge:
            self._death_cycle_fired = False
            self._pick_cycle_active = False
            self._suppress_retry = False  # 新一轮死亡可重新尝试识别
            # 双条件：仅当已到达等级检查点(有欠账)且死亡/泉水时才自动识别；
            # 未到新检查点不自动触发（完全静默，不弹提示、不做无谓 OCR；仍可手动「刷新识别」）
            self._redeem_owed_if_ready(is_dead=True, ui_visible=False, source="死亡/泉水")
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
        # 死亡不再无条件全量 OCR：未到检查点(无欠账)的泉水等待只会空转 + 误判；
        # 仅在仍有欠账(到过检查点)或已有识别状态时才全量，等待兑现识别。
        want_full = (
            self._pending_reason is not None
            or self._ui_was_visible
            or self._option_change_since is not None
            or self._last_option_snapshot is not None
            or self._pick_cycle_active
            or (is_dead and bool(self._owed))
        )
        snapshot = None
        ui_visible = False
        if want_full:
            snapshot, ui_visible = self._snapshot_options()
        elif awaiting_pick:
            # 欠账中：偶尔轻量探测 UI，避免全量三区 OCR 空转
            if (
                now >= self._retry_after
                and not dead_suppressed
                and self._detect_hex_ui_quick()
            ):
                snapshot, ui_visible = self._snapshot_options()
            elif now >= self._retry_after:
                self._retry_after = now + OWED_LIGHT_WAIT
        else:
            # 未到检查点(无欠账)时不探测 UI：泉水/死亡画面(暗底+复活倒计时亮字)
            # 容易被像素预筛误判成选牌卡，探测了也只会弹无意义的「识别提示」。
            # 死亡抑制窗口（含死亡状态本身）内一律不探测，兜底 LCU is_dead 滞后。
            if not dead_suppressed and self._detect_hex_ui_quick():
                snapshot, ui_visible = self._snapshot_options()

        # UI 出现：兑现欠账；无欠账则普通 UI 触发
        if ui_visible and not self._ui_was_visible:
            if self._owed:
                self._suppress_retry = False  # 选牌 UI 重新出现，允许再次识别
                self._redeem_owed_if_ready(
                    is_dead=is_dead, ui_visible=True, source="选牌 UI"
                )
            elif self._pending_reason is None:
                # 未到等级检查点的裸选牌 UI：不自动识别。
                # 完全静默——不再弹「识别提示」打扰（仍可手动「刷新识别」）。
                pass
        self._ui_was_visible = ui_visible

        if ui_visible and snapshot:
            if self._last_option_snapshot is None:
                if self._owed and self._pending_reason is None:
                    self._redeem_owed_if_ready(
                        is_dead=is_dead, ui_visible=True, source="选牌 UI"
                    )
            elif snapshot != self._last_option_snapshot:
                if self._option_change_since is None:
                    self._option_change_since = now
                    # 立刻清掉旧推荐：旧行为要等 debounce 才重识别，
                    # 这段时间遮罩上挂着的是上一轮的名字，看起来像"认错了"。
                    self._notify_invalidate()
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

        # 欠账且本帧已能选牌，但尚未挂 pending：补一次兑现（未匹配抑制期内不重复）
        if self._owed and (is_dead or ui_visible) and self._pending_reason is None and not self._suppress_retry:
            self._redeem_owed_if_ready(
                is_dead=is_dead,
                ui_visible=ui_visible,
                source="死亡/泉水" if is_dead else "选牌 UI",
            )

        # 选牌 UI 仍在且已出过推荐/正在兑现时，保持持续刷新（直到玩家选定）；
        # 未到等级检查点(无欠账)的裸选牌 UI 不再自动持续刷新
        if (
            ui_visible
            and self._pending_reason is None
            and not self._suppress_retry
            and (self._pick_cycle_active or self._pending_cp is not None)
        ):
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
            ui_likely = (
                is_dead
                or ui_visible
                or (not dead_suppressed and self._detect_hex_ui_quick())
            )
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
            # 有文字但未匹配：不向界面推送「未识别」卡片刷屏，仅日志提示一次；
            # 停止持续刷新并抑制重试，避免死亡持续期间反复空转
            self._suppress_retry = True
            if reason != "ui_keep":
                self._status("🔍 识别到海克斯文字但未匹配数据库 — 未出推荐（可点「刷新识别」重试）")
            self._pick_cycle_active = False
            self._continue_or_stop_refresh(post_ui=False, now=now)

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

    def notify_manual_refresh(self, results: Optional[dict] = None, *, skip_snapshot: bool = False):
        """手动刷新后：更新 OCR 快照基线，并提示「刷新后已更新推荐」。

        skip_snapshot=True 时（GUI「刷新识别」路径），调用方刚执行完 analyze(含 3 区 OCR)，
        直接依据 results 判断 UI 是否仍在，避免再同步 OCR 一次造成明显卡顿。
        """
        if not self._gate_in_live_game():
            self._status("⚠ 尚未进入对局 — 进入对局且右上角读秒出现后再识别海克斯")
            return
        self._suppress_retry = False  # 手动刷新视为用户主动重试
        if skip_snapshot:
            # 复用 analyze 结果推断 UI 状态: 有文字(含未匹配) 即视为选牌 UI 仍在
            ui = False
            if results:
                ui = any(
                    (not v.get("error"))
                    or ("无文字" not in str(v.get("text", "")))
                    for v in results.values()
                )
            snap = self._last_option_snapshot
        else:
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

    def _snapshot_options(self, use_cache: bool = True) -> Tuple[Optional[str], bool]:
        """对三个 hex 区域 OCR，返回 (规范化拼接快照, UI是否可见)。

        use_cache=True 时先做整帧字节级变化检测：三区域画面与上次完全一致则直接复用
        上次 OCR 结果，避免选牌 UI 静止期间反复跑全量 OCR(每次 ~0.9s) 拖慢主循环。
        画面任何像素变化(刷新/高亮/选中)都会触发真实 OCR，不会漏检。
        """
        try:
            images = self.analyzer.capture_all_regions()
            if not images:
                return None, False
            if use_cache:
                cache_key = tuple(
                    (img.shape, img.dtype, img.tobytes()) for img in images.values()
                )
                if (
                    getattr(self, "_snap_cache_key", None) is not None
                    and cache_key == self._snap_cache_key
                    and getattr(self, "_snap_cache", None) is not None
                ):
                    return self._snap_cache
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
                if use_cache:
                    self._snap_cache_key = cache_key
                    self._snap_cache = (None, False)
                return None, False
            snap = "|".join(t if t else "_" for t in texts)
            result = (snap, True)
            if use_cache:
                self._snap_cache_key = cache_key
                self._snap_cache = result
            return result
        except Exception:
            return None, False

    def _detect_hex_ui_quick(self) -> bool:
        """轻量探测：抓中间区 → 像素预筛（几乎免费）→ 通过才 OCR。

        以前这里**每个 tick（0.55s）都跑一次单区 OCR，整局不停**，
        而缓存键是原始像素 —— 对局里画面一直在动，永远不命中，
        等于整场比赛持续 1.8 次/秒的 ONNX 推理。这是游戏内卡顿的主因。
        现在交给 analyzer.probe_ui()：正常对局中像素预筛会拦掉绝大多数帧，OCR 基本不跑。
        """
        try:
            return self.analyzer.probe_ui()
        except Exception:
            return False

    def _detect_hex_ui(self) -> bool:
        return self._detect_hex_ui_quick()
