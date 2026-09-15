"""
WeGame 启动器：定位 wegame.exe + 一键启动。

定位顺序：
  1. 常见安装盘符路径探测
  2. 注册表 SOFTWARE\\WOW6432Node\\Tencent\\WeGame\\InstallPath
  3. 用户在 UI 手动填的路径（持久化到 settings.json 的 wegame_path）
"""
from __future__ import annotations

import os
import subprocess

EXE_NAME = "wegame.exe"

_CANDIDATES = (
    r"C:\Program Files (x86)\WeGame\wegame.exe",
    r"C:\Program Files\WeGame\wegame.exe",
    r"D:\Program Files (x86)\WeGame\wegame.exe",
    r"D:\Program Files\WeGame\wegame.exe",
    r"D:\WeGame\wegame.exe",
    r"E:\WeGame\wegame.exe",
    r"E:\Program Files (x86)\WeGame\wegame.exe",
)

_REG_KEYS = (
    r"SOFTWARE\WOW6432Node\Tencent\WeGame",
    r"SOFTWARE\Tencent\WeGame",
)


def find_wegame():
    """自动探测 wegame.exe，找到返回完整路径，否则 None。"""
    for p in _CANDIDATES:
        try:
            if os.path.isfile(p):
                return p
        except OSError:
            continue
    try:
        import winreg
        for sub in _REG_KEYS:
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(root, sub) as k:
                        val, _t = winreg.QueryValueEx(k, "InstallPath")
                        exe = os.path.join(str(val), EXE_NAME)
                        if os.path.isfile(exe):
                            return exe
                        if os.path.isdir(str(val)):
                            alt = os.path.join(str(val), "wegame.exe")
                            if os.path.isfile(alt):
                                return alt
                except OSError:
                    continue
    except Exception:
        pass
    return None


def launch(path: str) -> bool:
    """启动 WeGame。路径无效抛 FileNotFoundError。"""
    exe = (path or "").strip().strip('"')
    if not exe or not os.path.isfile(exe):
        raise FileNotFoundError(f"未找到 {EXE_NAME}：{exe or '(空)'}")
    subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True)
    return True
