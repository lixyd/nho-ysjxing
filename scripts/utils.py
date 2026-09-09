"""
拼音映射生成工具 (独立运行入口)
运行: python -m scripts.utils
功能与 updater.update_pinyin_file() 相同，可独立使用。
"""
import sys
import os

# 兼容直接运行和包导入
try:
    from scripts.updater import update_pinyin_file, sync_official_data
except ImportError:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    from scripts.updater import update_pinyin_file, sync_official_data


def main():
    """同步官方数据并重新生成拼音映射文件"""
    _, cn_to_en, _, _ = sync_official_data()
    if cn_to_en:
        update_pinyin_file(cn_to_en)
        print("✅ 拼音映射已更新")
    else:
        print("❌ 官方数据同步失败，请检查网络")


if __name__ == "__main__":
    main()