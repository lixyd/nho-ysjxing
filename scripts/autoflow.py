"""
自动流程（全部走 LCU 官方接口，无注入、无模拟点击）

接口与 LeagueAkari 同源：
  · 自动回到房间   POST /lol-lobby/v2/play-again
                   结算阶段自动点「再次游戏」，回到房间继续排
  · 自动重连       POST /lol-gameflow/v1/reconnect
                   阶段变成 Reconnect（掉线）时自动重新连回对局
  · 自动接受邀请   GET  /lol-lobby/v2/received-invitations
                   POST /lol-lobby/v2/received-invitations/{invitationId}/accept

设计约束：
  · 所有动作幂等 + 冷却，绝不刷接口（回到房间 20s、重连 8s、邀请 2.5s）
  · 开关实时生效：每轮 tick 都从 get_flags() 重新读，UI 一改立刻生效
  · 任何异常都吞掉并只记一次日志，不让自动化线程把界面拖死
"""
from __future__ import annotations

import threading
import time

# 结算阶段（自动回到房间在这里触发）
EOG_PHASES = {"PreEndOfGame", "WaitingForStats", "EndOfGame"}


class AutoFlowService(threading.Thread):
    """随引擎启停：回房 / 重连 / 收邀请。"""

    TICK = 0.5
    PLAY_AGAIN_COOLDOWN = 20.0
    RECONNECT_COOLDOWN = 8.0
    INVITE_COOLDOWN = 2.5

    def __init__(self, lcu, get_flags=None, on_event=None):
        super().__init__(daemon=True)
        self.lcu = lcu
        self.get_flags = get_flags or (lambda: {})
        self.on_event = on_event
        self.running = True

        self._last_play_again = 0.0
        self._last_reconnect = 0.0
        self._last_invite = 0.0
        self._eog_armed = False          # 本局是否还没点过回房
        self._handled_invites = set()    # 已处理的邀请 id
        self._warned = set()

    # ---------- 对外 ----------

    def stop(self):
        self.running = False

    def flag(self, key, default=False):
        try:
            return bool(self.get_flags().get(key, default))
        except Exception:
            return default

    def _log(self, msg):
        if not self.on_event:
            return
        try:
            self.on_event(msg)
        except Exception:
            pass

    def _log_once(self, key, msg):
        if key in self._warned:
            return
        self._warned.add(key)
        self._log(msg)

    # ---------- 主循环 ----------

    def run(self):
        while self.running:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(self.TICK)

    def _tick(self):
        if not self.lcu or not self.lcu.is_connected():
            return
        phase = self.lcu.get_gameflow_phase()
        now = time.time()

        # ---- ① 自动重连 ----
        try:
            if phase == "Reconnect" and self.flag("auto_reconnect", True):
                if now - self._last_reconnect >= self.RECONNECT_COOLDOWN:
                    self._last_reconnect = now
                    if self.lcu.post_ok("/lol-gameflow/v1/reconnect"):
                        self._log("✅ 检测到掉线，已发起自动重连")
                    else:
                        self._log_once("reconnect-fail", "⚠ 自动重连失败（客户端拒绝）")
        except Exception:
            pass

        # ---- ② 自动回到房间 ----
        try:
            if phase in EOG_PHASES:
                if not self._eog_armed:
                    self._eog_armed = True
                    self._last_play_again = 0.0
                if self.flag("auto_play_again", False) and \
                        now - self._last_play_again >= self.PLAY_AGAIN_COOLDOWN:
                    self._last_play_again = now
                    if self.lcu.post_ok("/lol-lobby/v2/play-again"):
                        self._log("✅ 本局结束，已自动回到房间")
                    else:
                        self._log_once("playagain-fail", "⚠ 自动回到房间失败（客户端拒绝）")
            else:
                self._eog_armed = False
                self._warned.discard("playagain-fail")
        except Exception:
            pass

        # ---- ③ 自动接受房间邀请 ----
        try:
            if self.flag("auto_accept_invite", False) and \
                    now - self._last_invite >= self.INVITE_COOLDOWN:
                self._last_invite = now
                self._accept_invites()
        except Exception:
            pass

    # ---------- 邀请 ----------

    def _accept_invites(self):
        data = self.lcu.get_json("/lol-lobby/v2/received-invitations")
        if not isinstance(data, list):
            return
        for inv in data:
            if not isinstance(inv, dict):
                continue
            iid = inv.get("invitationId") or inv.get("id")
            if not iid or iid in self._handled_invites:
                continue
            state = str(inv.get("state") or "Pending")
            if state.lower() not in ("pending", "invited"):
                continue
            self._handled_invites.add(iid)
            who = self._inviter_name(inv)
            try:
                ok = self.lcu.post_ok(
                    f"/lol-lobby/v2/received-invitations/{iid}/accept")
            except Exception:
                ok = False
            if ok:
                self._log(f"✅ 已自动接受 {who} 的房间邀请")
            else:
                self._log(f"⚠ 接受 {who} 的邀请失败（可能已过期）")

    @staticmethod
    def _inviter_name(inv):
        for key in ("partyOwner", "inviter", "fromSummoner"):
            who = inv.get(key)
            if isinstance(who, dict):
                name = who.get("gameName") or who.get("displayName") or \
                    who.get("summonerName")
                if name:
                    return name
        return "好友"
