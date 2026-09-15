"""
匹配 / 准备自动化 (LCU only，无注入、无键鼠模拟)

实现内容 (对标 LeagueAkari 常见能力):
1. 自动接受对局确认
   - 轮询 GET  /lol-matchmaking/v1/ready-check
   - 当 state ∈ {InProgress, EveryoneReady} 且本地尚未接受时
     POST /lol-matchmaking/v1/ready-check/accept

2. 自动准备 / 自动开始 (大厅)
   - PUT  /lol-lobby/v1/parties/ready           body: true / {"ready": true}
     （组队大厅「准备」）
   - POST /lol-lobby/v2/lobby/matchmaking/search
     （已在大厅且未在匹配时，尝试发起匹配 —— 「自动开始」）
   - POST /lol-champ-select/v1/session/my-selection/ready
     （选人阶段声明准备，ARAM 等模式可用）

不实现: 自定义房强制开始、英雄自动秒选/秒 ban（避免误操作）。

延迟: flags["auto_accept_delay"] ∈ {0,3,5,10}，执行前倒计时；
开关关闭或 running=False 时取消倒计时。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from scripts.config import normalize_delay


class MatchmakingService(threading.Thread):
    """后台轮询 LCU，执行自动接受 / 自动准备。"""

    POLL_INTERVAL = 0.8

    def __init__(self, lcu, flags: Optional[dict] = None, on_event=None, on_countdown=None):
        super().__init__(daemon=True)
        self.lcu = lcu
        self.flags = flags if flags is not None else {
            "auto_accept": True,
            "auto_ready": True,
            "auto_accept_delay": 5,
        }
        self.on_event = on_event  # callable(str) 可选日志回调
        # on_countdown(seconds_left|None, label:str) — None 表示清除 UI
        self.on_countdown: Optional[Callable] = on_countdown
        self.running = True
        self._last_accept_ts = 0.0
        self._last_ready_ts = 0.0
        self._last_search_ts = 0.0
        self._last_cs_ready_ts = 0.0
        self._manual_start = False
        self._countdown_lock = threading.Lock()

    def stop(self):
        self.running = False
        self._emit_countdown(None, "")

    def request_start(self):
        """UI「开始」按钮：下一轮 tick 立即尝试准备 + 开始匹配。"""
        self._manual_start = True

    def set_flag(self, key: str, value):
        if key == "auto_accept_delay":
            self.flags[key] = normalize_delay(value)
        else:
            self.flags[key] = bool(value)

    def get_delay(self) -> int:
        return normalize_delay(self.flags.get("auto_accept_delay", 5))

    def _log(self, msg: str):
        print(msg)
        if self.on_event:
            try:
                self.on_event(msg)
            except Exception:
                pass

    def _emit_countdown(self, seconds_left, label: str = ""):
        if not self.on_countdown:
            return
        try:
            self.on_countdown(seconds_left, label)
        except Exception:
            pass

    def _countdown_wait(
        self,
        flag_key: str,
        label: str,
        still_valid: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """
        按设置延迟 N 秒；返回 True 表示应继续执行动作。
        倒计时期间若开关关闭、服务停止、或 still_valid() 为假则取消。
        """
        seconds = self.get_delay()
        if seconds <= 0:
            return bool(self.running and self.flags.get(flag_key, True))

        with self._countdown_lock:
            self._log(f"⏳ {label} 将在 {seconds} 秒后执行…")
            for remaining in range(seconds, 0, -1):
                if not self.running or not self.flags.get(flag_key, True):
                    self._emit_countdown(None, "")
                    self._log(f"⏹ 已取消 {label} 倒计时")
                    return False
                if still_valid is not None:
                    try:
                        if not still_valid():
                            self._emit_countdown(None, "")
                            self._log(f"⏹ {label} 条件已失效，取消倒计时")
                            return False
                    except Exception:
                        self._emit_countdown(None, "")
                        return False

                self._emit_countdown(remaining, label)
                # 分片睡眠，便于及时响应取消
                end = time.time() + 1.0
                while time.time() < end:
                    if not self.running or not self.flags.get(flag_key, True):
                        self._emit_countdown(None, "")
                        self._log(f"⏹ 已取消 {label} 倒计时")
                        return False
                    time.sleep(0.05)

            self._emit_countdown(None, "")
            return bool(self.running and self.flags.get(flag_key, True))

    def run(self):
        self._log("匹配助手已启动 (自动接受/准备)")
        while self.running:
            try:
                self._tick()
            except Exception as e:
                # 安静重试，避免刷屏
                if time.time() % 30 < self.POLL_INTERVAL:
                    print(f"[matchmaking] tick error: {e}")
            time.sleep(self.POLL_INTERVAL)
        self._emit_countdown(None, "")
        self._log("匹配助手已停止")

    def _ensure_lcu(self) -> bool:
        if not self.lcu:
            return False
        if self.lcu.is_connected():
            return True
        return bool(self.lcu.connect())

    def _tick(self):
        if not self._ensure_lcu():
            return

        if self._manual_start:
            self._manual_start = False
            phase = self.lcu.get_gameflow_phase()
            if phase in ("Lobby", "None", None):
                self._log("▶ 手动开始匹配")
                self._try_party_ready()
                self._try_start_matchmaking()
            else:
                self._log(f"⚠ 当前阶段 {phase or '未知'}，无法开始匹配")

        if self.flags.get("auto_accept", True):
            self._try_accept_ready_check()

        if self.flags.get("auto_ready", True):
            phase = self.lcu.get_gameflow_phase()
            if phase in ("Lobby", "None", None):
                self._try_party_ready()
                self._try_start_matchmaking()
            elif phase == "ChampSelect":
                self._try_champ_select_ready()

    # ---------- 1. Ready-check ----------

    def _ready_check_needs_accept(self) -> bool:
        data = self.lcu.get_json("/lol-matchmaking/v1/ready-check")
        if not isinstance(data, dict):
            return False
        state = data.get("state") or ""
        player_resp = data.get("playerResponse") or data.get("playerResponseType") or ""
        if state not in ("InProgress", "EveryoneReady"):
            return False
        if str(player_resp).lower() in ("accepted", "accept"):
            return False
        return True

    def _try_accept_ready_check(self):
        now = time.time()
        if now - self._last_accept_ts < 1.5:
            return
        if not self._ready_check_needs_accept():
            return

        # 倒计时开始即占位，避免并行重复触发
        self._last_accept_ts = now
        if not self._countdown_wait(
            "auto_accept",
            "自动接受",
            still_valid=self._ready_check_needs_accept,
        ):
            return

        if not self._ready_check_needs_accept():
            return
        ok = self.lcu.post_ok("/lol-matchmaking/v1/ready-check/accept")
        self._last_accept_ts = time.time()
        if ok:
            self._log("✅ 已自动接受对局确认 (ready-check)")

    # ---------- 2. Party ready ----------

    def _party_needs_ready(self) -> bool:
        lobby = self.lcu.get_json("/lol-lobby/v2/lobby")
        if not isinstance(lobby, dict):
            return False
        local = lobby.get("localMember") or {}
        return local.get("ready") is not True

    def _try_party_ready(self):
        now = time.time()
        if now - self._last_ready_ts < 3.0:
            return
        if not self._party_needs_ready():
            return

        self._last_ready_ts = now
        if not self._countdown_wait(
            "auto_ready",
            "自动准备",
            still_valid=self._party_needs_ready,
        ):
            return

        if not self._party_needs_ready():
            return
        ok = self.lcu.put_ok("/lol-lobby/v1/parties/ready", json_body=True)
        if not ok:
            ok = self.lcu.put_ok("/lol-lobby/v1/parties/ready", json_body={"ready": True})
        self._last_ready_ts = time.time()
        if ok:
            self._log("✅ 已自动准备 (party ready)")

    def _can_start_matchmaking(self) -> bool:
        phase = self.lcu.get_gameflow_phase()
        if phase not in ("Lobby",):
            return False
        search = self.lcu.get_json("/lol-lobby/v2/lobby/matchmaking/search-state")
        if isinstance(search, dict):
            search_state = (search.get("searchState") or search.get("phase") or "").lower()
            if search_state in ("searching", "found", "champselect"):
                return False
        lobby = self.lcu.get_json("/lol-lobby/v2/lobby")
        if not isinstance(lobby, dict):
            return False
        can_start = lobby.get("canStartActivity")
        if can_start is False:
            return False
        return True

    def _try_start_matchmaking(self):
        """大厅内发起匹配搜索（自动开始）。"""
        now = time.time()
        if now - self._last_search_ts < 5.0:
            return
        if not self._can_start_matchmaking():
            return

        self._last_search_ts = now
        if not self._countdown_wait(
            "auto_ready",
            "自动开始",
            still_valid=self._can_start_matchmaking,
        ):
            return

        if not self._can_start_matchmaking():
            return
        ok = self.lcu.post_ok("/lol-lobby/v2/lobby/matchmaking/search")
        self._last_search_ts = time.time()
        if ok:
            self._log("✅ 已发起匹配搜索 (matchmaking/search)")

    def _try_champ_select_ready(self):
        now = time.time()
        if now - self._last_cs_ready_ts < 4.0:
            return

        self._last_cs_ready_ts = now
        if not self._countdown_wait("auto_ready", "选人准备"):
            return

        ok = self.lcu.post_ok("/lol-champ-select/v1/session/my-selection/ready")
        self._last_cs_ready_ts = time.time()
        if ok:
            self._log("✅ 选人阶段已声明准备 (champ-select ready)")
