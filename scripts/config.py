"""
统一路径常量与配置 (兼容 PyInstaller 打包)
所有模块应从此文件导入 BASE_DIR, DATA_DIR 等路径常量，避免重复定义。
"""
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

# 默认开关
DEFAULT_SETTINGS = {
    "auto_accept": True,
    "auto_ready": True,
    "auto_hex": True,
    "auto_apply_runes": False,
    "overlay_topmost": True,
}
