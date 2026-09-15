"""
统一路径常量与配置 (兼容 PyInstaller 打包)
所有模块应从此文件导入 BASE_DIR, DATA_DIR 等路径常量，避免重复定义。
"""
import json
import os
import sys


def get_base_dir():
    """获取应用根目录 (兼容 PyInstaller 打包)"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    # scripts/config.py -> scripts/ -> 项目根目录
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = get_base_dir()
DATA_DIR = os.path.join(BASE_DIR, 'data')


def lower_process_priority():
    """把本进程降到"低于正常"优先级，让游戏本体优先拿 CPU。

    这是消除游戏内卡顿最直接的一招：OCR 再省也还是会占用 CPU，
    只要让操作系统在争抢时优先满足游戏，卡顿感就基本消失。
    失败时静默返回（例如权限不足），不影响任何功能。
    """
    try:
        import psutil
        p = psutil.Process()
        p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        try:
            # 同时也降低 IO 优先级，避免截图读写抢磁盘
            p.ionice(psutil.IOPRIO_CLASS_BE, 5)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"⚠ 进程降优先级失败（不影响功能）: {e}")
        return False

# 数据文件路径常量
CHAMPION_ID_FILE = os.path.join(DATA_DIR, "champions.json")
PINYIN_FILE      = os.path.join(DATA_DIR, "pinyin_map.json")
CSV_FILE         = os.path.join(DATA_DIR, "hero_augments.csv")

# 官方海克斯数据库（由 scripts/build_augments_official.py 生成，CommunityDragon 官方数据）
AUGMENTS_FILE    = os.path.join(DATA_DIR, "augments_official.json")
AUGMENT_ALIAS_FILE = os.path.join(DATA_DIR, "augment_alias_zh.json")

# 扩展功能数据
SETTINGS_FILE   = os.path.join(DATA_DIR, "settings.json")

# 海克斯大乱斗等级检查点
HEX_LEVEL_CHECKPOINTS = (1, 7, 11, 15)

# 自动接受可选延迟（秒）——按手绘稿固定 1 / 3 / 5
AUTO_DELAY_CHOICES = (1, 3, 5)

# 默认开关（纯净版：仅匹配自动化 + 备战席抢英雄 + WeGame 启动）
DEFAULT_SETTINGS = {
    "auto_accept": True,
    "auto_ready": True,
    "auto_accept_delay": 3,
    # 备战席抢英雄：解锁前提前量（秒），内部默认值，不在 UI 展示
    "bench_lead": 2,
    # WeGame 启动器：用户手动指定或自动探测到的 wegame.exe 路径（字符串，可为空）
    "wegame_path": "",
    # 自动回到房间：一局结束后自动点「再次游戏」回到房间
    "auto_play_again": False,
    # 自动重连：客户端掉线（Reconnect 阶段）自动重新连接回对局
    "auto_reconnect": True,
    # 自动接受房间邀请（组队被拉）
    "auto_accept_invite": False,
    # 开机自启（写 HKCU\...\Run）
    "autostart": False,
    # 点关闭时最小化到托盘而不是退出
    "close_to_tray": False,
    # 对局中自动隐藏窗口，回到房间自动弹出（照搬 LeagueAkari 小窗的 autoShow 三态机）
    "auto_hide_in_game": True,
    # 自动吸附到英雄联盟客户端右上角（照搬 LA 的 repositionToAlignLeagueClientUx）
    "snap_to_client": True,
}


def normalize_delay(value, default=3):
    """将延迟秒数规范到 AUTO_DELAY_CHOICES。"""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return default
    if iv in AUTO_DELAY_CHOICES:
        return iv
    # 容错：夹到最近可选值
    return min(AUTO_DELAY_CHOICES, key=lambda x: abs(x - iv))


def load_settings(path=None):
    """从 data/settings.json 加载设置，缺失项用 DEFAULT_SETTINGS 补齐。"""
    path = path or SETTINGS_FILE
    data = dict(DEFAULT_SETTINGS)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                for key, default in DEFAULT_SETTINGS.items():
                    if key not in loaded:
                        continue
                    raw = loaded[key]
                    if key == "auto_accept_delay":
                        data[key] = normalize_delay(raw, default=default)
                    elif key == "bench_lead":
                        try:
                            data[key] = max(1, min(10, int(float(raw))))
                        except (TypeError, ValueError):
                            data[key] = default
                    elif key == "wegame_path":
                        data[key] = str(raw) if raw else ""
                    else:
                        data[key] = bool(raw)
    except Exception as e:
        print(f"设置加载失败: {e}")
    return data


def save_settings(settings, path=None):
    """持久化设置到 data/settings.json。"""
    path = path or SETTINGS_FILE
    try:
        payload = dict(DEFAULT_SETTINGS)
        if isinstance(settings, dict):
            for key, default in DEFAULT_SETTINGS.items():
                if key not in settings:
                    continue
                if key == "auto_accept_delay":
                    payload[key] = normalize_delay(settings[key], default=default)
                elif key == "bench_lead":
                    try:
                        payload[key] = max(1, min(10, int(float(settings[key]))))
                    except (TypeError, ValueError):
                        payload[key] = default
                elif key == "wegame_path":
                    payload[key] = str(settings[key]) if settings[key] else ""
                else:
                    payload[key] = bool(settings[key])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"设置保存失败: {e}")
        return False
