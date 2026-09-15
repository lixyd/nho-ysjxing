# -*- coding: utf-8 -*-
"""验证本轮三项：整体缩小 1/3 · 吸附客户端右上角 · 对局中自动隐藏。

判据全部对齐 LeagueAkari（out/main/main.js）：
  贴角  -> qS()/WS()/GS() 三条函数
  显隐  -> _watchAuxWindow() 的三态机
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from scripts import winpos
import ctk_launcher as app
from scripts.config import DEFAULT_SETTINGS

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' -> ' + str(extra)) if extra else ''}")


print("=" * 70)
print("A. 整体缩小 1/3")
print("=" * 70)
D = app.Design
check("SCALE = 2/3", abs(D.SCALE - 0.667) < 1e-6, D.SCALE)
check("间距已缩放 S_XL(20)->13", D.S_XL == 13, D.S_XL)
check("瓦片已缩放 TILE_PX(108)->72", D.TILE_PX == 72, D.TILE_PX)
check("按钮高已缩放 BTN_H(54)->36", D.BTN_H == 36, D.BTN_H)
check("圆角已缩放 R_TILE(16)->11", D.R_TILE == 11, D.R_TILE)
check("R_PILL 不被缩放(哨兵值)", D.R_PILL == 999, D.R_PILL)
for key, old in (('brand', 22), ('body', 14), ('small', 12), ('micro', 11)):
    fam, size, _w = D.font(key)
    check(f"字号 {key} {old}-> {size} (>=10 可读下限)", size >= 10 and size < old, size)
check("窗口基准宽 840->560", D.sc(840) == 560, D.sc(840))
check("窗口最小 680->454", D.sc(680) == 454, D.sc(680))
check("主窗尺寸已按缩放(~560x378)",
      D.sc(840) == 560 and D.sc(566) == 378, f"{D.sc(840)}x{D.sc(566)}")

print()
print("=" * 70)
print("B. 吸附客户端右上角 —— 对齐 LA 的 GS()/qS()")
print("=" * 70)
CLIENT = {"left": 100, "top": 50, "width": 1280, "height": 720}
WA_BIG = {"x": 0, "y": 0, "width": 1920, "height": 1080}
WA_TIGHT = {"x": 0, "y": 0, "width": 1400, "height": 1080}
# 足够大的工作区，用来验证「不夹取」时的原始贴角公式
WA_HUGE = {"x": -1000, "y": -1000, "width": 4000, "height": 3000}

# LA: case 'top-right': a = t.x + t.width, o = t.y
check("top-right 贴右侧顶部 (x=client.right, y=client.top)",
      winpos.calc_attach_pos(300, 400, CLIENT, "top-right", WA_BIG) == (1380, 50),
      winpos.calc_attach_pos(300, 400, CLIENT, "top-right", WA_BIG))
check("top-left 贴左侧顶部 (x=client.left-winW)",
      winpos.calc_attach_pos(300, 400, CLIENT, "top-left", WA_HUGE) == (-200, 50),
      winpos.calc_attach_pos(300, 400, CLIENT, "top-left", WA_HUGE))
check("bottom-left 底部对齐 (y=client.bottom-winH)",
      winpos.calc_attach_pos(300, 400, CLIENT, "bottom-left", WA_HUGE) == (-200, 370),
      winpos.calc_attach_pos(300, 400, CLIENT, "bottom-left", WA_HUGE))
check("bottom-right 右下",
      winpos.calc_attach_pos(300, 400, CLIENT, "bottom-right", WA_BIG) == (1380, 370),
      winpos.calc_attach_pos(300, 400, CLIENT, "bottom-right", WA_BIG))
check("默认角就是 top-right", winpos.DEFAULT_CORNER == "top-right")

# 越界夹取，和 LA 一个顺序：先夹 x 再夹 y
check("x 越右夹回 workArea 内",
      winpos.calc_attach_pos(300, 400, CLIENT, "top-right", WA_TIGHT) == (1100, 50),
      winpos.calc_attach_pos(300, 400, CLIENT, "top-right", WA_TIGHT))
check("x 越左夹回 workArea 内",
      winpos.calc_attach_pos(300, 400, CLIENT, "top-left", WA_BIG) == (0, 50),
      winpos.calc_attach_pos(300, 400, CLIENT, "top-left", WA_BIG))
check("y 越下夹回 workArea 内",
      winpos.calc_attach_pos(300, 400, CLIENT, "bottom-right",
                             {"x": 0, "y": 0, "width": 1920, "height": 400}) == (1380, 0),
      winpos.calc_attach_pos(300, 400, CLIENT, "bottom-right",
                             {"x": 0, "y": 0, "width": 1920, "height": 400}))
check("workArea 带偏移(任务栏在左)",
      winpos.calc_attach_pos(300, 400, CLIENT, "top-right",
                             {"x": 40, "y": 30, "width": 1920, "height": 1080}) == (1380, 50),
      winpos.calc_attach_pos(300, 400, CLIENT, "top-right",
                             {"x": 40, "y": 30, "width": 1920, "height": 1080}))
check("非法角回退 top-left",
      winpos.calc_attach_pos(300, 400, CLIENT, "nowhere", WA_BIG)
      == winpos.calc_attach_pos(300, 400, CLIENT, "top-left", WA_BIG))
# LA 的闸门：width<200 且 height<50 才放弃
check("客户端最小化闸门常量 (200/50)",
      winpos.MIN_CLIENT_W == 200 and winpos.MIN_CLIENT_H == 50)
check("winpos 在 Windows 可用", winpos.AVAILABLE is True)
check("窗口类名含 RCLIENT", "RCLIENT" in winpos.CLIENT_CLASSES, winpos.CLIENT_CLASSES)

print()
print("=" * 70)
print("C. 对局中自动隐藏 —— 对齐 LA 的 _watchAuxWindow()")
print("=" * 70)
av = app.auto_visibility
check("LA 的 show 集合: Lobby/Matchmaking/ReadyCheck/ChampSelect",
      app.AUTO_SHOW_PHASES == ("Lobby", "Matchmaking", "ReadyCheck", "ChampSelect"),
      app.AUTO_SHOW_PHASES)
for ph in ("Lobby", "Matchmaking", "ReadyCheck", "ChampSelect"):
    check(f"  {ph} -> show", av(ph, True, True) == "show", av(ph, True, True))
for ph in ("GameStart", "InProgress", "WaitingForStats",
           "PreEndOfGame", "EndOfGame", "Reconnect"):
    check(f"  {ph} -> hide", av(ph, True, True) == "hide", av(ph, True, True))
check("开关关闭 -> ignore(LA: !autoShow)", av("InProgress", False, True) == "ignore")
check("未连接 -> ignore(主窗专属闸门)", av("InProgress", True, False) == "ignore")
check("phase 为空 -> ignore(主窗专属闸门)", av(None, True, True) == "ignore")

print()
print("=" * 70)
print("D. 设置项")
print("=" * 70)
check("auto_hide_in_game 默认开", DEFAULT_SETTINGS.get("auto_hide_in_game") is True)
check("snap_to_client 默认开", DEFAULT_SETTINGS.get("snap_to_client") is True)
titles = dict((k, t) for k, t, _d in app.CTkLauncherApp.SETTING_ROWS)
check("设置面板有「对局中自动隐藏」", "auto_hide_in_game" in titles, list(titles))
check("设置面板有「吸附客户端右上角」", "snap_to_client" in titles, list(titles))

print()
print("=" * 70)
print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ALL PASS")
