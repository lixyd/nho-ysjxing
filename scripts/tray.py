"""
Windows 托盘图标 + 开机自启（零第三方依赖，纯 ctypes / winreg）

为什么不用 pystray / pywin32：
  本项目只有一个托盘需求，为此引入两个依赖（还要在 PyInstaller 里
  额外处理隐藏导入）不划算；ctypes 直接调 shell32/user32 最稳，
  打包时也不会因为缺 DLL 而运行时炸掉。

用法（主线程）：
    tray = TrayIcon(on_show=..., on_quit=...)
    ok = tray.start()      # False 表示托盘不可用（调用方降级为最小化到任务栏）
    ...
    tray.stop()
托盘线程只往 queue 里放事件，绝不碰 tkinter（tk 不是线程安全的）。
"""
from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
from ctypes import wintypes

IS_WIN = os.name == "nt"

# ---------------------------------------------------------------- 常量
WM_USER = 0x0400
WM_TRAY = WM_USER + 20
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_NULL = 0x0000

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE, LR_SHARED = 0x0010, 0x0040, 0x8000
IDI_APPLICATION = 32512

MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x0002, 0x0100, 0x0080

ID_SHOW, ID_QUIT = 1001, 1002

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_REG_NAME = "xiaobaiHelper"


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# ---------------------------------------------------------------- 开机自启

def default_target():
    """自启命令：打包后用 exe 本身，源码运行用 python + 脚本。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "ctk_launcher.py")
    script = os.path.abspath(script)
    return f'"{sys.executable}" "{script}"'


def is_autostart_enabled(key=RUN_KEY, name=APP_REG_NAME):
    if not IS_WIN:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            val, _ = winreg.QueryValueEx(k, name)
            return bool(val)
    except Exception:
        return False


def set_autostart(enabled, target=None, key=RUN_KEY, name=APP_REG_NAME):
    """写/删 HKCU\\...\\Run 项。返回是否成功。"""
    if not IS_WIN:
        return False
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0,
                                winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ,
                                  target or default_target())
            else:
                try:
                    winreg.DeleteValue(k, name)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 托盘

class TrayIcon:
    """托盘图标：左键显示主界面，右键菜单（显示 / 退出）。"""

    def __init__(self, on_show=None, on_quit=None, tooltip="xiaobai助手",
                 icon_path=None):
        self.on_show = on_show
        self.on_quit = on_quit
        self.tooltip = tooltip
        self.icon_path = icon_path
        self.events = queue.Queue()      # 主线程轮询：("show"|"quit")
        self._thread = None
        self._ready = threading.Event()
        self._ok = False
        self._hwnd = None
        self._nid = None

    # ---------- 对外 ----------

    def start(self, timeout=3.0):
        if not IS_WIN:
            return False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._ok

    def stop(self):
        try:
            if self._hwnd:
                ctypes.windll.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass

    def poll(self):
        """主线程调用：取出托盘事件（没有则返回 None）。"""
        try:
            return self.events.get_nowait()
        except queue.Empty:
            return None

    # ---------- 内部（托盘线程） ----------

    def _load_icon(self):
        u32 = ctypes.windll.user32
        if self.icon_path and os.path.exists(self.icon_path):
            h = u32.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0,
                               LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
        h = u32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))
        return h or 0

    def _run(self):
        try:
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            kernel32 = ctypes.windll.kernel32

            self._wndproc_ref = WNDPROC(self._wndproc)
            hinst = kernel32.GetModuleHandleW(None)
            cls_name = f"xiaobaiHelperTrayWnd_{os.getpid()}"

            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            if not user32.RegisterClassW(ctypes.byref(wc)):
                if ctypes.get_last_error() != 1410:   # 已注册
                    raise ctypes.WinError()
            hwnd = user32.CreateWindowExW(0, cls_name, cls_name, 0,
                                          0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                raise ctypes.WinError()
            self._hwnd = hwnd

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self._load_icon()
            nid.szTip = self.tooltip[:127]
            self._nid = nid
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                raise ctypes.WinError()
            self._ok = True
            self._ready.set()

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            self._ok = False
            self._ready.set()
        finally:
            try:
                if self._nid is not None:
                    ctypes.windll.shell32.Shell_NotifyIconW(
                        NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass
            try:
                if self._hwnd:
                    ctypes.windll.user32.DestroyWindow(self._hwnd)
            except Exception:
                pass

    def _menu(self):
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_SHOW, "显示主界面")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "退出")
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self._hwnd)
        cmd = user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
            pt.x, pt.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd == ID_SHOW:
            self.events.put("show")
        elif cmd == ID_QUIT:
            self.events.put("quit")
            ctypes.windll.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                if lparam == WM_LBUTTONUP:
                    self.events.put("show")
                elif lparam == WM_RBUTTONUP:
                    self._menu()
                return 0
            if msg == WM_CLOSE:
                ctypes.windll.user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                ctypes.windll.user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        # lparam 高位为 1 时会变成超过有符号 64 位的正整数，直接传会
        # ctypes.ArgumentError: int too long to convert。先折回有符号再传。
        lp = lparam & 0xFFFFFFFFFFFFFFFF
        if lp >= 1 << 63:
            lp -= 1 << 64
        try:
            return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lp)
        except Exception:
            return 0
