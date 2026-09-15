# -*- coding: utf-8 -*-
"""验证「没有备战席时不显示任何英雄」——判据照搬 LeagueAkari。

LA 的 auxWindow 浮窗里就一句：`if (!session?.benchEnabled) return null`
所以判据只有一个：**champ-select session 的 benchEnabled**。
本测试同时守住这一点：服务线程不许再去读 gameflow 的 phase。

A 段：服务层（不依赖真实客户端，也不开窗口）
B 段：UI 层 + 截图
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)

OUT_DIR = os.path.join(os.path.dirname(BASE), "deliverables")

from scripts.bench_pick import BenchPickService

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  <- " + detail) if detail else ""))


# ============================================================================
# A 段：服务层 —— session.benchEnabled 是唯一判据
# ============================================================================

class StubLCU:
    """假 LCU：session 返回什么由用例决定；并记录有没有人偷看 gameflow phase。"""

    def __init__(self, session):
        self._session = session
        self.phase_calls = 0
        self.id_to_cn = {145: "卡莎", 24: "贾克斯", 157: "黛安娜"}

    def is_connected(self):
        return True

    def get_json(self, path):
        if path == "/lol-champ-select/v1/session":
            return self._session
        return None

    def post_ok(self, path):
        return True

    def get_pickable_champion_ids(self):
        return {145, 24}

    def get_owned_champion_ids(self):
        return {145, 24, 157}

    def get_gameflow_phase(self):
        self.phase_calls += 1
        return "ChampSelect"     # 故意返回 ChampSelect，看服务会不会被它带偏


BENCH = [{"championId": 145}, {"championId": 24}, {"championId": 157}]


def run_tick(session):
    """跑一次 _tick，返回 (payload, lcu)。"""
    box = []
    lcu = StubLCU(session)
    svc = BenchPickService(lcu, lambda m: None, box.append)
    svc._tick()
    return (box[-1] if box else None), lcu


def part_a():
    print("\n[A] 服务层：判据 = champ-select session.benchEnabled（照搬 LA）")

    box = []
    svc0 = BenchPickService(StubLCU(None), lambda m: None, box.append)

    # 1) 没进选人 —— 客户端对该 session 返回 404，get_json 得到 None
    check("无 session → _parse_bench 出 0 个英雄", svc0._parse_bench(None) == [],
          repr(svc0._parse_bench(None)))

    # 2) 有 session 但备战席没开（大乱斗 ban 阶段，还没有 bench）
    off = {"benchEnabled": False, "benchChampions": BENCH}
    check("benchEnabled=False → _parse_bench 出 0 个英雄",
          svc0._parse_bench(off) == [], repr(svc0._parse_bench(off)))

    # 3) 从未有过备战席 → 服务不推送事件，UI 默认态本就是空（不发噪音）
    p, lcu = run_tick(None)
    check("未进选人时不推送噪音事件（UI 默认即空）", p is None, repr(p))
    p_off, _ = run_tick(off)
    check("benchEnabled=False 时同样不推送", p_off is None, repr(p_off))

    # 4) 备战席开了
    p3, _ = run_tick({"benchEnabled": True, "benchChampions": BENCH})
    check("benchEnabled=True → available=True", p3 and p3["available"] is True, repr(p3))
    check("benchEnabled=True → 3 个英雄", p3 and len(p3["items"]) == 3,
          repr(p3 and len(p3["items"])))

    # 5) 关键：服务线程一次都没有查 gameflow phase（不再自己发明判据）
    check("服务全程未调用 get_gameflow_phase", lcu.phase_calls == 0,
          "phase_calls=%d" % lcu.phase_calls)

    # 6) 从「有备战席」到「没有」的边界：必须补发 available=False 让 UI 清空
    box = []
    lcu2 = StubLCU({"benchEnabled": True, "benchChampions": BENCH})
    svc = BenchPickService(lcu2, lambda m: None, box.append)
    svc._tick()
    lcu2._session = None                     # 离开选人
    svc._tick()
    check("离开选人后补发 available=False", box[-1]["available"] is False, repr(box[-1]))
    check("离开选人后 items 清空", box[-1]["items"] == [], repr(box[-1]["items"]))


# ============================================================================
# B 段：UI 层
# ============================================================================

def part_b():
    import ctk_launcher as L
    from _ui_shot import grab, hwnd_of

    print("\n[B] UI 层：拿不到备战席就一张头像都不显示")

    app = L.CTkLauncherApp()

    class FakeBench:
        _target_cid = None

    class FakeEngine:
        bench = FakeBench()

    app.engine = FakeEngine()
    app.engine_running = True

    def pump():
        for _ in range(3):
            app.root.update_idletasks()
            app.root.update()
            time.sleep(0.05)

    def set_phase(phase, connected=True):
        app._handle_event({"event": "phase", "phase": phase, "connected": connected})
        pump()

    def push(items, available):
        app._handle_event({"event": "bench",
                           "data": {"items": items, "available": available,
                                    "note": "", "grabbing": False}})
        pump()

    def wall_text():
        out = []
        for w in app.wall.winfo_children():
            for c in w.winfo_children():
                try:
                    t = c.cget("text")
                    if t:
                        out.append(str(t))
                except Exception:
                    pass
        return " | ".join(out)

    THREE = [
        {"championId": 145, "name": "卡莎", "lock_seconds": None, "swappable": True},
        {"championId": 24, "name": "贾克斯", "lock_seconds": None, "swappable": True},
        {"championId": 157, "name": "黛安娜", "lock_seconds": None, "swappable": False},
    ]

    pump()
    check("初始无头像", len(app._tiles) == 0, "tiles=%d" % len(app._tiles))

    set_phase("Lobby")
    push(THREE, available=False)
    check("available=False 时 tiles == 0", len(app._tiles) == 0, "tiles=%d" % len(app._tiles))
    check("空态文案是「尚未进入选人阶段」", "尚未进入选人阶段" in wall_text(), wall_text())
    check("状态条不显示「可换」", "可换" not in app.bench_status_var.get(),
          repr(app.bench_status_var.get()))
    check("墙体无英雄名", all(n not in wall_text() for n in ("卡莎", "贾克斯", "黛安娜")),
          wall_text())

    set_phase("ChampSelect")
    push(THREE, available=True)
    check("available=True 时 tiles == 3", len(app._tiles) == 3, "tiles=%d" % len(app._tiles))
    check("可换计数正确 2/3", "2/3" in app.bench_status_var.get(),
          repr(app.bench_status_var.get()))

    push(THREE, available=False)
    check("备战席关闭后立刻清空", len(app._tiles) == 0, "tiles=%d" % len(app._tiles))

    set_phase("ChampSelect")
    push(THREE, available=True)
    set_phase("InProgress")
    check("离开选人（安全网）清空", len(app._tiles) == 0, "tiles=%d" % len(app._tiles))

    set_phase("ChampSelect", connected=False)
    push(THREE, available=True)
    check("断线时清空", len(app._tiles) == 0, "tiles=%d" % len(app._tiles))

    set_phase("Lobby")
    push(THREE, available=False)
    check("截图前确认为空态", len(app._tiles) == 0 and "尚未进入选人阶段" in wall_text(),
          wall_text())
    pump()

    def shoot(n=0):
        path = os.path.join(OUT_DIR, "ui-idle.png")
        ok = False
        try:
            ok = grab(hwnd_of(app.root), path)
        except Exception as e:
            print("  grab error:", e)
        if ok:
            print("  SHOT_OK", path)
            check("大厅态截图成功", True)
        elif n < 6:
            app.root.after(900, lambda: shoot(n + 1))
            return
        else:
            print("  SHOT_FAIL")
            check("大厅态截图成功", False)

        print("\nRESULT: PASS=%d FAIL=%d" % (len(PASS), len(FAIL)))
        print("ALL_PASS" if not FAIL else ("FAILED: %s" % FAIL))
        try:
            app._quitting = True
            app.root.destroy()
        except Exception:
            pass

    app.root.after(800, shoot)
    app.root.mainloop()


if __name__ == "__main__":
    part_a()
    part_b()
