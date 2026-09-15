# -*- coding: utf-8 -*-
"""
窗口定位 —— 照搬 LeagueAkari 的 `repositionToAlignLeagueClientUx` 算法。

LA 的实现在 out/main/main.js，一共四条函数，这里一一对应：

    LA                                    本模块
    ───────────────────────────────────────────────────────────────────
    Vt()                          ->      get_league_client_rect()
        原生 addon 的 getLeagueClientWindowPlacementInfo()
        返回 {left, top, width, height}

    qS(win, corner)               ->      attach_window(win, corner)
        前置闸门：拿不到矩形就算了；客户端 width<200 且 height<50（最小化）也算了

    WS(win, clientRect, corner)   ->      （并进 attach_window）
        const {x, y} = GS(win.getBounds(), rect, corner); win.setPosition(x, y)

    GS(winBounds, rect, corner)   ->      calc_attach_pos()
        算贴角坐标，再夹到该显示器的 workArea 内

corner 指的是「客户端的哪个角」，窗口贴在它外侧：

    top-left       x = client.x - winW            y = client.y
    bottom-left    x = client.x - winW            y = client.y + clientH - winH
    top-right      x = client.x + clientW         y = client.y       ← 用户要的右侧顶部
    bottom-right   x = client.x + clientW         y = client.y + clientH - winH

夹取（LA 用 screen.getDisplayMatching(rect).workArea）：
    if (x < wa.x) x = wa.x
    else if (x + w > wa.x + wa.width) x = wa.x + wa.width - w
    y 同理

LA 的入口是 IPC `repositionToAlignLeagueClientUx`，由设置页手动触发，不是持续跟随。
"""
import ctypes
import os

try:
    from ctypes import wintypes
except Exception:      # pragma: no cover - 非 Windows
    wintypes = None

_IS_WIN = (os.name == "nt") and wintypes is not None

# LA: jt.getLeagueClientWindowPlacement.available === (process.platform === 'win32')
AVAILABLE = _IS_WIN

# 客户端窗口类名；RiotWindowClass 是游戏本体窗口，做第二选择
CLIENT_CLASSES = ("RCLIENT", "RiotWindowClass")

# LA: if (width < 200 && height < 50) return;
MIN_CLIENT_W = 200
MIN_CLIENT_H = 50

CORNERS = ("top-left", "bottom-left", "top-right", "bottom-right")
DEFAULT_CORNER = "top-right"


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", wintypes.RECT if wintypes else ctypes.c_int),
        ("rcWork", wintypes.RECT if wintypes else ctypes.c_int),
        ("dwFlags", ctypes.c_ulong),
    ]


def get_league_client_rect():
    """等价 LA 的 Vt()：客户端矩形 {'left','top','width','height'}，拿不到返回 None。"""
    if not AVAILABLE:
        return None
    try:
        u = ctypes.windll.user32
        r = wintypes.RECT()
        for cls in CLIENT_CLASSES:
            hwnd = u.FindWindowW(cls, None)
            if not hwnd:
                continue
            if u.IsIconic(hwnd) or not u.IsWindowVisible(hwnd):
                continue
            if not u.GetWindowRect(hwnd, ctypes.byref(r)):
                continue
            w, h = r.right - r.left, r.bottom - r.top
            # 照搬 LA 的「最小化 / 尺寸异常」判据，两个条件同时成立才放弃
            if w < MIN_CLIENT_W and h < MIN_CLIENT_H:
                continue
            return {"left": r.left, "top": r.top, "width": w, "height": h}
    except Exception:
        pass
    return None


def workarea_of(rect):
    """等价 Electron 的 screen.getDisplayMatching(rect).workArea。

    先 MonitorFromRect(MONITOR_DEFAULTTONEAREST) + GetMonitorInfo，
    失败再退回 SystemParametersInfo(SPI_GETWORKAREA) 主屏工作区。
    """
    if not AVAILABLE:
        return None
    try:
        u = ctypes.windll.user32
        r = wintypes.RECT(rect["left"], rect["top"],
                          rect["left"] + rect["width"], rect["top"] + rect["height"])
        hmon = u.MonitorFromRect(ctypes.byref(r), 2)   # MONITOR_DEFAULTTONEAREST
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if hmon and u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            wa = mi.rcWork
            return {"x": wa.left, "y": wa.top,
                    "width": wa.right - wa.left, "height": wa.bottom - wa.top}
    except Exception:
        pass
    try:
        u = ctypes.windll.user32
        wa = wintypes.RECT()
        if u.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0):   # SPI_GETWORKAREA
            return {"x": wa.left, "y": wa.top,
                    "width": wa.right - wa.left, "height": wa.bottom - wa.top}
    except Exception:
        pass
    return None


def calc_attach_pos(win_w, win_h, client, corner=DEFAULT_CORNER, workarea=None):
    """等价 LA 的 GS()：算贴角坐标并夹进 workArea。返回 (x, y)。"""
    if corner not in CORNERS:
        corner = "top-left"

    left, top = client["left"], client["top"]
    cw, ch = client["width"], client["height"]

    if corner == "top-left":
        x, y = left - win_w, top
    elif corner == "bottom-left":
        x, y = left - win_w, top + ch - win_h
    elif corner == "top-right":
        x, y = left + cw, top
    else:                                    # bottom-right
        x, y = left + cw, top + ch - win_h

    wa = workarea if workarea is not None else workarea_of(client)
    if wa:
        if x < wa["x"]:
            x = wa["x"]
        elif x + win_w > wa["x"] + wa["width"]:
            x = wa["x"] + wa["width"] - win_w
        if y < wa["y"]:
            y = wa["y"]
        elif y + win_h > wa["y"] + wa["height"]:
            y = wa["y"] + wa["height"] - win_h
    return int(x), int(y)


def attach_window(root, corner=DEFAULT_CORNER):
    """把 tk 窗口贴到客户端的某个角（LA 的 qS + WS）。成功返回 (x, y)，否则 None。"""
    if not AVAILABLE:
        return None
    client = get_league_client_rect()
    if not client:
        return None
    try:
        root.update_idletasks()
        w, h = root.winfo_width(), root.winfo_height()
    except Exception:
        return None
    if w < 10 or h < 10:
        return None
    x, y = calc_attach_pos(w, h, client, corner)
    try:
        root.geometry(f"+{x}+{y}")
    except Exception:
        return None
    return (x, y)
