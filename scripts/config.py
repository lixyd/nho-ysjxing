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

# 数据文件路径常量
CHAMPION_ID_FILE = os.path.join(DATA_DIR, "champions.json")
PINYIN_FILE      = os.path.join(DATA_DIR, "pinyin_map.json")
CSV_FILE         = os.path.join(DATA_DIR, "hero_augments.csv")

# 扩展功能数据
SETTINGS_FILE   = os.path.join(DATA_DIR, "settings.json")
ARAM_RUNES_FILE = os.path.join(DATA_DIR, "aram_runes.json")

# 海克斯大乱斗等级检查点
HEX_LEVEL_CHECKPOINTS = (1, 7, 11, 15)

# 自动接受 / 开始 可选延迟（秒）；0 = 立即执行
AUTO_DELAY_CHOICES = (0, 3, 5, 10)

# 默认开关
DEFAULT_SETTINGS = {
    "auto_accept": True,
    "auto_ready": True,
    "auto_hex": True,
    "auto_apply_runes": False,
    "overlay_topmost": True,
    "auto_accept_delay": 5,
}


def normalize_delay(value, default=5):
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
                else:
                    payload[key] = bool(settings[key])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"设置保存失败: {e}")
        return False
