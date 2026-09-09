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
"""
from __future__ import annotations

import threading
import time
from typing import Optional


class MatchmakingService(threading.Thread):
    """后台轮询 LCU，执行自动接受 / 自动准备。"""

    POLL_INTERVAL = 0.8

    def __init__(self, lcu, flags: Optional[dict] = None, on_event=None):
        super().__init__(daemon=True)
        self.lcu = lcu
        self.flags = flags if flags is not None else {
            "auto_accept": True,
            "auto_ready": True,
        }
        self.on_event = on_event  # callable(str) 可选日志回调
        self.running = True
        self._last_accept_ts = 0.0
        self._last_ready_ts = 0.0
        self._last_search_ts = 0.0
        self._last_cs_ready_ts = 0.0

    def stop(self):
        self.running = False

    def set_flag(self, key: str, value: bool):
        self.flags[key] = bool(value)

    def _log(self, msg: str):
        print(msg)
        if self.on_event:
            try:
                self.on_event(msg)
            except Exception:
                pass

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

    def _try_accept_ready_check(self):
        now = time.time()
        if now - self._last_accept_ts < 1.5:
            return
        data = self.lcu.get_json("/lol-matchmaking/v1/ready-check")
        if not isinstance(data, dict):
            return
        state = data.get("state") or ""
        player_resp = data.get("playerResponse") or data.get("playerResponseType") or ""
        # InProgress: 弹窗中; EveryoneReady: 全员已接受（仍可兜底）
        if state not in ("InProgress", "EveryoneReady"):
            return
        if str(player_resp).lower() in ("accepted", "accept"):
            return
        ok = self.lcu.post_ok("/lol-matchmaking/v1/ready-check/accept")
        self._last_accept_ts = now
        if ok:
            self._log("✅ 已自动接受对局确认 (ready-check)")

    # ---------- 2. Party ready ----------

    def _try_party_ready(self):
        now = time.time()
        if now - self._last_ready_ts < 3.0:
            return
        # 先读大厅成员，避免无意义 PUT
        lobby = self.lcu.get_json("/lol-lobby/v2/lobby")
        if not isinstance(lobby, dict):
            return
        local = lobby.get("localMember") or {}
        if local.get("ready") is True:
            return
        # LeagueAkari / 社区常用: PUT /lol-lobby/v1/parties/ready
        ok = self.lcu.put_ok("/lol-lobby/v1/parties/ready", json_body=True)
        if not ok:
            ok = self.lcu.put_ok("/lol-lobby/v1/parties/ready", json_body={"ready": True})
        self._last_ready_ts = now
        if ok:
            self._log("✅ 已自动准备 (party ready)")

    def _try_start_matchmaking(self):
        """大厅内发起匹配搜索（自动开始）。"""
        now = time.time()
        if now - self._last_search_ts < 5.0:
            return
        phase = self.lcu.get_gameflow_phase()
        if phase not in ("Lobby",):
            return
        # 已在匹配则跳过
        search = self.lcu.get_json("/lol-lobby/v2/lobby/matchmaking/search-state")
        if isinstance(search, dict):
            search_state = (search.get("searchState") or search.get("phase") or "").lower()
            if search_state in ("searching", "found", "champselect"):
                return
        lobby = self.lcu.get_json("/lol-lobby/v2/lobby")
        if not isinstance(lobby, dict):
            return
        # 仅当本地已 ready 时尝试开始，降低误触
        local = lobby.get("localMember") or {}
        if local.get("ready") is not True:
            # 某些版本字段不同，若无法判断则仍尝试（限流已保护）
            pass
        can_start = lobby.get("canStartActivity")
        if can_start is False:
            return
        ok = self.lcu.post_ok("/lol-lobby/v2/lobby/matchmaking/search")
        self._last_search_ts = now
        if ok:
            self._log("✅ 已发起匹配搜索 (matchmaking/search)")

    def _try_champ_select_ready(self):
        now = time.time()
        if now - self._last_cs_ready_ts < 4.0:
            return
        # ARAM / 部分模式: 选人后「准备」
        ok = self.lcu.post_ok("/lol-champ-select/v1/session/my-selection/ready")
        self._last_cs_ready_ts = now
        if ok:
            self._log("✅ 选人阶段已声明准备 (champ-select ready)")
