"""
本轮新功能回归（全部用桩，不需要真实客户端）：
  A. AutoFlowService：自动重连 / 自动回到房间 / 自动接受邀请（含冷却、去重、开关实时生效）
  B. AugmentLibrary：官方海克斯数据加载、搜索、稀有度过滤、描述清洗
  C. 开机自启：注册表写入 / 读取 / 删除（用临时子键，不污染 Run 键）

运行： python _test_features.py
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)

from scripts.autoflow import AutoFlowService
from scripts.augments import AugmentLibrary, clean_desc
from scripts.config import AUGMENTS_FILE
from scripts import tray as tray_mod

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("  ✅ " if cond else "  ❌ ") + name)


# ============================================================ A. AutoFlow
class StubLCU:
    def __init__(self, phase="Lobby"):
        self.phase = phase
        self.calls = []
        self.invitations = []

    def is_connected(self):
        return True

    def get_gameflow_phase(self):
        return self.phase

    def post_ok(self, endpoint, json_body=None):
        self.calls.append(endpoint)
        return True

    def get_json(self, endpoint):
        if endpoint == "/lol-lobby/v2/received-invitations":
            return self.invitations
        return None


def test_autoflow():
    print("[A] AutoFlowService")

    # ① 自动重连：Reconnect 阶段触发一次，冷却内不重复
    lcu = StubLCU("Reconnect")
    svc = AutoFlowService(lcu, get_flags=lambda: {"auto_reconnect": True})
    svc._tick(); svc._tick()
    check("掉线阶段触发一次重连",
          lcu.calls.count("/lol-gameflow/v1/reconnect") == 1)

    # ② 关掉开关就不再重连
    lcu2 = StubLCU("Reconnect")
    svc2 = AutoFlowService(lcu2, get_flags=lambda: {"auto_reconnect": False})
    svc2._tick()
    check("开关关闭时不重连", lcu2.calls == [])

    # ③ 自动回到房间：结算阶段触发；非结算阶段不触发
    lcu3 = StubLCU("EndOfGame")
    svc3 = AutoFlowService(lcu3, get_flags=lambda: {"auto_play_again": True})
    svc3._tick()
    check("结算阶段自动回到房间",
          lcu3.calls.count("/lol-lobby/v2/play-again") == 1)
    svc3._tick()
    check("20s 冷却内不重复回房",
          lcu3.calls.count("/lol-lobby/v2/play-again") == 1)

    lcu4 = StubLCU("InProgress")
    svc4 = AutoFlowService(lcu4, get_flags=lambda: {"auto_play_again": True})
    svc4._tick()
    check("游戏中不触发回房", lcu4.calls == [])

    lcu5 = StubLCU("EndOfGame")
    svc5 = AutoFlowService(lcu5, get_flags=lambda: {"auto_play_again": False})
    svc5._tick()
    check("回房开关关闭时不动作", lcu5.calls == [])

    # ④ 自动接受邀请：只接受 Pending，同一邀请只处理一次
    lcu6 = StubLCU("Lobby")
    lcu6.invitations = [
        {"invitationId": "a1", "state": "Pending",
         "partyOwner": {"gameName": "小白"}},
        {"invitationId": "a2", "state": "Declined"},
    ]
    svc6 = AutoFlowService(lcu6, get_flags=lambda: {"auto_accept_invite": True})
    svc6._last_invite = 0.0
    svc6._tick()
    accepted = [c for c in lcu6.calls if "/received-invitations/a1/accept" in c]
    declined = [c for c in lcu6.calls if "a2" in c]
    check("接受 Pending 邀请", len(accepted) == 1)
    check("忽略非 Pending 邀请", declined == [])
    svc6._last_invite = 0.0
    svc6._tick()
    check("同一邀请不重复接受",
          len([c for c in lcu6.calls if "a1/accept" in c]) == 1)

    # ⑤ 未连接时不动作
    class Off(StubLCU):
        def is_connected(self):
            return False
    lcu7 = Off("Reconnect")
    svc7 = AutoFlowService(lcu7, get_flags=lambda: {"auto_reconnect": True})
    svc7._tick()
    check("客户端未连接时不动作", lcu7.calls == [])


# ============================================================ B. Augments
def test_augments():
    print("[B] AugmentLibrary")
    lib = AugmentLibrary(AUGMENTS_FILE)
    check("数据文件可加载", lib.error == "" and len(lib.entries) > 500)

    cnt = lib.counts()
    check("ARAM 池筛选可用", 0 < cnt["aram"] < cnt["total"])

    aram = lib.search(aram_only=True)
    check("默认只看 ARAM 池", all(e.aram for e in aram) and len(aram) == cnt["aram"])

    allrows = lib.search(aram_only=False)
    check("取消 ARAM 过滤后条目更多", len(allrows) == cnt["total"])

    hit = lib.search("物理转魔法", aram_only=False)
    check("按中文名搜索命中", any(e.name == "物理转魔法" for e in hit))

    desc_hit = lib.search("法术强度", aram_only=False)
    check("按效果关键词搜索命中", len(desc_hit) > 0)

    silver = lib.search(rarity="白银", aram_only=False)
    check("按稀有度过滤", silver and all(e.rarity == "白银" for e in silver))

    check("排序为 棱彩→黄金→白银",
          [e.rarity for e in allrows[:3]] == ["棱彩", "棱彩", "棱彩"])

    d, approx = clean_desc("获得@APAmp*100@%法术强度。剩余%i:cooldown%冷却")
    check("清理动态占位符", "@" not in d and "%i:" not in d and approx)

    d2, approx2 = clean_desc("你的治疗和护盾会变强。")
    check("普通描述不受影响", d2 == "你的治疗和护盾会变强。" and not approx2)

    nod = [e for e in allrows if not e.has_desc]
    check("缺描述条目如实标注（不编造）", len(nod) > 0)


# ============================================================ C. 自启
def test_autostart():
    print("[C] 开机自启（临时子键）")
    test_key = r"Software\xiaobaiHelperTest"
    name = "testEntry"
    ok = tray_mod.set_autostart(True, target='"C:\\fake\\app.exe"',
                               key=test_key, name=name)
    check("写入自启项", ok and tray_mod.is_autostart_enabled(test_key, name))
    ok2 = tray_mod.set_autostart(False, key=test_key, name=name)
    check("删除自启项", ok2 and not tray_mod.is_autostart_enabled(test_key, name))
    try:
        import winreg
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, test_key)
    except Exception:
        pass
    check("自启目标命令可生成", "xiaobai" in tray_mod.default_target().lower()
          or "ctk_launcher" in tray_mod.default_target())


if __name__ == "__main__":
    t0 = time.time()
    test_autoflow()
    test_augments()
    test_autostart()
    bad = [n for n, ok in PASS if not ok]
    print(f"\n{'=' * 46}")
    print(f"  通过 {len(PASS) - len(bad)}/{len(PASS)}  ({time.time() - t0:.1f}s)")
    if bad:
        print("  失败:")
        for n in bad:
            print("   -", n)
        sys.exit(1)
    print("  ALL_PASS")
