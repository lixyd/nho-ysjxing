"""
备战席提前选取 (Bench Snipe)

极地大乱斗 / 海克斯大乱斗选人阶段：备战席（bench / 备选池）英雄带解锁读秒，
读秒结束客户端才接受「换上」请求。本模块做两件事：

1. 轮询 /lol-champ-select/v1/session，把备战席英雄（含解锁读秒）推给 UI；
2. 用户点击某个英雄设为目标后，在其解锁前 N 秒（默认 2s，可设 1/2/3s）
   进入高频抢购窗口，以 ~10 次/秒的频率重试 bench/swap，
   服务端一放行立刻换上 —— 比肉眼盯读秒手点快。

LCU 接口（均为官方本地 API，无注入）:
  GET  /lol-champ-select/v1/session
       - benchEnabled / benchChampions: [{championId, isNominated, ...}]
       - 解锁读秒字段各版本不统一，做防御性解析；解析不到就读秒视为未知，
         改为「点击即开始中频尝试、收到目标后持续重试」的兜底策略。
  POST /lol-champ-select/v1/session/bench/swap/{championId}
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional


def extract_lock_seconds(entry: dict) -> Optional[float]:
    """从备战席条目里防御性提取解锁读秒（秒）。

    不同版本字段名不统一（lockTime / unlockSeconds / timeUntilAllowed ...），
    按"字段名含关键词 + 数值范围合理"匹配。找不到返回 None。
    """
    if not isinstance(entry, dict):
        return None
    keywords = ("lock", "unlock", "timer", "remain", "until", "second", "countdown")
    for k, v in entry.items():
        try:
            kl = str(k).lower()
        except Exception:
            continue
        if not any(t in kl for t in keywords) or "nominated" in kl:
            continue
        if isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        # 合理读秒范围：0 ~ 10 分钟
        if 0.0 <= f <= 600.0:
            return f
    return None


class BenchPickService(threading.Thread):
    """备战席监听 + 目标英雄提前抢换。随引擎启停，不主动轮询时零开销。"""

    POLL_INTERVAL = 0.4     # 选人阶段 session 轮询间隔
    GRAB_INTERVAL = 0.10    # 抢购窗口内的 swap 重试间隔
    IDLE_TRY_INTERVAL = 0.6 # 读秒未知时的兜底尝试间隔

    def __init__(self, lcu, on_event=None, on_bench=None):
        super().__init__(daemon=True)
        self.lcu = lcu
        self.on_event = on_event            # callable(str) 日志
        self.on_bench = on_bench            # callable(list[dict]) UI 推送
        self.running = True

        self.lead_seconds = 2.0             # 提前量（内部默认，不在 UI 展示）
        self._target_cid: Optional[int] = None
        self._target_name = ""
        self._grabbing = False
        self._last_try_ts = 0.0
        self._available = False             # 当前是否有备战席（＝ session.benchEnabled）
        # 「能换上」的英雄 id 集合：客户端官方 pickable 列表优先，拥有列表兜底。
        # None = 还没拿到（UI 不置灰），每 3 秒自动重试一次，全程无需用户干预。
        self._swappable_ids: Optional[set] = None
        self._swappable_ts = 0.0

    # ---------- 对 UI 的接口 ----------

    def stop(self):
        self.running = False

    def set_lead(self, seconds):
        try:
            self.lead_seconds = max(0.0, min(10.0, float(seconds)))
        except (TypeError, ValueError):
            pass

    def set_target(self, champion_id: int, name: str):
        """UI 点击：设为抢购目标（再次点击同一个 = 取消）。"""
        if self._target_cid == champion_id:
            self.clear_target()
            return
        self._target_cid = int(champion_id)
        self._target_name = name or str(champion_id)
        self._grabbing = True   # 点击即进入尝试状态；读秒未知时靠兜底重试
        self._log(f"🎯 已锁定目标: {self._target_name}，解锁前 {self.lead_seconds:g}s 开始抢")

    def clear_target(self, reason: str = ""):
        if self._target_cid is None and not self._grabbing:
            return
        self._target_cid = None
        self._target_name = ""
        self._grabbing = False
        if reason:
            self._log(reason)

    # ---------- 内部 ----------

    def _log(self, msg: str):
        print(msg)
        if self.on_event:
            try:
                self.on_event(msg)
            except Exception:
                pass

    def _push(self, items, note: str = "", available: bool = True):
        """available=False 表示「当前没有备战席」。

        判据与 LeagueAkari 完全一致：只看 champ-select session 的 benchEnabled，
        不用 gameflow 的 phase 字符串去猜（LA 的 auxWindow 里就是
        `if (!session?.benchEnabled) return null`）。
        """
        if not self.on_bench:
            return
        try:
            self.on_bench({"items": items, "target": self._target_cid,
                           "grabbing": self._grabbing, "note": note,
                           "available": available})
        except Exception:
            pass

    def _parse_bench(self, session) -> list:
        """session → [{championId, name, lock_seconds|None, swappable: True/False/None}]"""
        if not isinstance(session, dict):
            return []
        if not session.get("benchEnabled"):
            return []
        allowed = self._swappable_ids
        items = []
        for entry in session.get("benchChampions") or []:
            try:
                cid = int(entry.get("championId", 0) or 0)
            except (TypeError, ValueError):
                continue
            if cid <= 0:
                continue
            items.append({
                "championId": cid,
                "name": self.lcu.id_to_cn.get(cid) or f"英雄{cid}",
                "lock_seconds": extract_lock_seconds(entry),
                "swappable": None if allowed is None else (cid in allowed),
            })
        return items

    def _try_swap(self, cid: int) -> bool:
        return self.lcu.post_ok(f"/lol-champ-select/v1/session/bench/swap/{cid}")

    def _load_swappable(self, force=False):
        """拉取「本局能换上的英雄」：客户端 pickable 列表优先，拥有列表兜底。

        拿不到就保持 None（UI 不置灰，宁可不标也不标错），3 秒后自动重试。
        """
        now = time.time()
        if not force:
            if self._swappable_ids is not None:
                return
            if now - self._swappable_ts < 3.0:
                return
        self._swappable_ts = now
        ids = None
        try:
            ids = self.lcu.get_pickable_champion_ids()
        except Exception:
            ids = None
        if ids is None:
            try:
                ids = self.lcu.get_owned_champion_ids()
            except Exception:
                ids = None
        if ids is not None:
            self._swappable_ids = ids

    # ---------- 主循环 ----------

    def run(self):
        while self.running:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(self.POLL_INTERVAL)

    def _tick(self):
        if not self.lcu or not self.lcu.is_connected():
            if self._available:
                self._available = False
                self.clear_target("⏹ 客户端已断开，取消抢购目标")
                self._push([], available=False, note="")
            return

        # —— 判据与 LeagueAkari 一致 ——
        # LA 的 auxWindow 浮窗里就是一句：`if (!session?.benchEnabled) return null`
        # 没进选人时客户端对 /lol-champ-select/v1/session 直接 404，session 为 None，
        # 于是「刚登录 / 大厅 / 匹配中 / 游戏中」天然就是「没有备战席」。
        # 不需要自己再拿 gameflow 的 phase 字符串去猜。
        session = self.lcu.get_json("/lol-champ-select/v1/session")
        available = isinstance(session, dict) and bool(session.get("benchEnabled"))

        if not available:
            if self._available:
                self._available = False
                self.clear_target("⏹ 备战席已关闭，取消抢购目标")
                self._push([], available=False, note="")
            return

        if not self._available:
            # 备战席刚开（大乱斗选人进入 BAN_PICK 才有 bench）→ 每局重新取可换列表
            self._available = True
            self._swappable_ids = None
            self._load_swappable(force=True)

        self._load_swappable()                  # 没拿到就每 3 秒重试

        items = self._parse_bench(session)

        # 目标被别人换走 / 已不在备战席 → 取消
        if self._target_cid is not None:
            if items and not any(it["championId"] == self._target_cid for it in items):
                self.clear_target(f"⚠ {self._target_name} 已不在备战席，取消目标")

        self._push(items, available=True)

        if self._target_cid is None or not self._grabbing:
            return

        # 抢购时机：读秒已知 → 提前量进入高频窗口；未知 → 中频兜底尝试
        target = next((it for it in items if it["championId"] == self._target_cid), None)
        if target is not None and target.get("swappable") is False:
            self.clear_target(
                f"⚠ {self._target_name} 换不了（未拥有 / 不在周免），取消目标")
            return
        now = time.time()
        if target and target["lock_seconds"] is not None:
            lock = target["lock_seconds"]
            if lock <= 0:
                interval = self.GRAB_INTERVAL
            elif lock <= self.lead_seconds:
                if not self._grabbing or now - self._last_try_ts >= 2.0:
                    self._log(f"⚡ {self._target_name} 解锁读秒 {lock:.1f}s，开始抢")
                interval = self.GRAB_INTERVAL
            else:
                # 还早，低频确认存活即可
                if now - self._last_try_ts < 1.5:
                    return
                self._last_try_ts = now
                return
        else:
            interval = self.IDLE_TRY_INTERVAL

        if now - self._last_try_ts < interval:
            return
        self._last_try_ts = now

        if self._try_swap(self._target_cid):
            lead_txt = f"（提前 {self.lead_seconds:g}s 窗口）" if interval == self.GRAB_INTERVAL else ""
            self._log(f"✅ 已抢到 {self._target_name} {lead_txt}")
            self._target_cid = None
            self._target_name = ""
            self._grabbing = False
            self._push(items, note="picked")
