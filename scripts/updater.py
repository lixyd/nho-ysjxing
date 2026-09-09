# --- START OF FILE updater.py ---

import json
import csv
import os
import requests
import sys
import re
import random
from pypinyin import lazy_pinyin 

# 1. 解决同级导入问题 (兼容直接运行和包导入)
try:
    from scripts import hero_scraper as crawler
    from scripts.config import DATA_DIR, CHAMPION_ID_FILE, PINYIN_FILE, CSV_FILE
except ImportError:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
    import hero_scraper as crawler
    from config import DATA_DIR, CHAMPION_ID_FILE, PINYIN_FILE, CSV_FILE

# GitHub 仓库地址 (用于在线下载)
GITHUB_RAW_BASE  = "https://raw.githubusercontent.com/Nyx0ra/lol-aram-mayhem-hextech-helper/main"

CSV_HEADER       =["中文名", "英文名", "等级", "总排名", "等级内序号", "海克斯名称"]

# ================= 1. 数据真理同步 =================
def sync_official_data():
    print(">>> [1/4] 正在同步官方英雄数据...")
    try:
        ver_url = "https://ddragon.leagueoflegends.com/api/versions.json"
        version = requests.get(ver_url).json()[0]
        print(f"    当前游戏版本: {version}")

        champ_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/zh_CN/champion.json"
        data = requests.get(champ_url).json()['data']

        official_en_to_cn = {}
        official_cn_to_en = {}
        for en_id, info in data.items():
            if en_id.startswith('Jade_'):
                continue
            cn_name = info['name']
            official_en_to_cn[en_id] = cn_name
            official_cn_to_en[cn_name] = en_id

        old_en_to_cn = {}
        if os.path.exists(CHAMPION_ID_FILE):
            with open(CHAMPION_ID_FILE, 'r', encoding='utf-8') as f:
                old_cn_to_en = json.load(f)
                old_en_to_cn = {en: cn for cn, en in old_cn_to_en.items()}

        with open(CHAMPION_ID_FILE, 'w', encoding='utf-8') as f:
            json.dump(official_cn_to_en, f, indent=4, ensure_ascii=False)
        
        new_champs = []
        renamed_champs =[]
        
        for en_id, cn_name in official_en_to_cn.items():
            if en_id not in old_en_to_cn:
                new_champs.append(en_id)
            elif old_en_to_cn[en_id] != cn_name:
                renamed_champs.append(en_id)
        
        print(f"    同步完成。共 {len(official_en_to_cn)} 个英雄。")
        if new_champs:
            print(f"    🌟 发现 {len(new_champs)} 个全新英雄: {', '.join([official_en_to_cn[en] for en in new_champs])}")
        if renamed_champs:
            print(f"    ✏️ 发现 {len(renamed_champs)} 个改名英雄: {', '.join([official_en_to_cn[en] for en in renamed_champs])}")
            
        return official_en_to_cn, official_cn_to_en, new_champs, renamed_champs

    except Exception as e:
        print(f"!!! 官方数据同步失败，请检查网络: {e}")
        return {}, {}, [],[]

# ================= 2. 拼音生成 =================
def update_pinyin_file(official_cn_to_en):
    print(">>>[2/4] 更新拼音检索文件...")
    pinyin_data = {}
    for cn_name in official_cn_to_en.keys():
        pinyin_list = lazy_pinyin(cn_name)
        initials = "".join([p[0].lower() for p in pinyin_list if p])
        pinyin_data[cn_name] = initials
    
    with open(PINYIN_FILE, 'w', encoding='utf-8') as f:
        json.dump(pinyin_data, f, indent=4, ensure_ascii=False)
    print("    拼音文件已更新。")

# ================= 3. 数据保护逻辑 (读CSV) =================
def load_csv_history():
    print(">>> [3/4] 读取本地历史数据 (数据保护)...")
    history = {}
    if not os.path.exists(CSV_FILE):
        return history

    try:
        with open(CSV_FILE, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            is_old_format = reader.fieldnames and "序号" in reader.fieldnames and "等级" not in reader.fieldnames
            has_overall_rank = reader.fieldnames and "总排名" in reader.fieldnames
            
            for row in reader:
                en_name = row.get('英文名')
                if en_name:
                    if en_name not in history:
                        history[en_name] = []
                    
                    if is_old_format:
                        adapted_row = {
                            "中文名": row.get("中文名", ""),
                            "英文名": en_name,
                            "等级": "未知",
                            "总排名": 999,
                            "等级内序号": 999,
                            "海克斯名称": row.get("海克斯名称", "")
                        }
                        history[en_name].append(adapted_row)
                    elif not has_overall_rank:
                        # 旧新格式：有等级但无总排名
                        adapted_row = {
                            "中文名": row.get("中文名", ""),
                            "英文名": en_name,
                            "等级": row.get("等级", "未知"),
                            "总排名": 999,
                            "等级内序号": row.get("等级内序号", 999),
                            "海克斯名称": row.get("海克斯名称", "")
                        }
                        history[en_name].append(adapted_row)
                    else:
                        history[en_name].append(row)
        print(f"    已加载 {len(history)} 个英雄的历史数据。")
    except Exception as e:
        print(f"⚠️ 读取历史CSV时出错 (可能是空文件): {e}")
    
    return history

# ================= 4. 合并与保存 =================
def merge_and_save(official_en_to_cn, history_data, new_crawl_data):
    print("\n>>> [4/4] 执行数据合并与持久化...")
    final_rows = []
    missing_data_champions =[]

    official_cn_to_en = {cn: en for en, cn in official_en_to_cn.items()}
    crawl_by_en = {official_cn_to_en.get(cn, cn): data for cn, data in new_crawl_data.items()}

    for en_name, cn_name in official_en_to_cn.items():
        rows_to_write =[]

        if en_name in crawl_by_en:
            for item in crawl_by_en[en_name]:
                rows_to_write.append({
                    "中文名": cn_name,
                    "英文名": en_name,
                    "等级": item['tier'],
                    "总排名": item['overall_rank'],
                    "等级内序号": item['t_rank'],
                    "海克斯名称": item['name']
                })
        elif en_name in history_data:
            # 深拷贝避免修改原始 history_data
            rows_to_write = [dict(row) for row in history_data[en_name]]
            for row in rows_to_write:
                row['中文名'] = cn_name
        else:
            missing_data_champions.append(cn_name)
        
        if rows_to_write:
            final_rows.extend(rows_to_write)

    try:
        with open(CSV_FILE, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
            writer.writeheader()
            writer.writerows(final_rows)
        print(f"✅ 写入完成！主文件: {CSV_FILE} (共 {len(final_rows)} 条数据)")
    except Exception as e:
        print(f"❌ 写入主文件失败: {e}")
        
    if missing_data_champions:
        print(f"\n⚠️ 注意: 有 {len(missing_data_champions)} 个英雄完全没有任何数据: {', '.join(missing_data_champions)}")

# ================= 5. 抽样比对检查 =================
def compare_hero_data(history_rows, crawled_items):
    """比对单个英雄的本地历史数据与线上爬取数据，返回是否有差异"""
    # 将历史数据转为可比较的集合
    local_set = set()
    for row in history_rows:
        key = (row.get('海克斯名称', ''), row.get('等级', ''), str(row.get('总排名', '')), str(row.get('等级内序号', '')))
        local_set.add(key)
    
    # 将爬取数据转为可比较的集合
    remote_set = set()
    for item in crawled_items:
        key = (item['name'], item['tier'], str(item['overall_rank']), str(item['t_rank']))
        remote_set.add(key)
    
    return local_set != remote_set

def spot_check_and_update(official_en_to_cn, history_data, sample_size=3):
    """随机抽取英雄进行抽样比对，一旦发现有差异立即触发全量更新"""
    all_en_names = list(official_en_to_cn.keys())
    # 优先从有历史数据的英雄中抽样，这样比对才有意义
    candidates = [en for en in all_en_names if en in history_data]
    if len(candidates) < sample_size:
        candidates = all_en_names
    
    sampled = random.sample(candidates, min(sample_size, len(candidates)))
    sample_list = [(official_en_to_cn[en], en) for en in sampled]
    
    print(f"\n>>> 🎲 抽样比对: 随机选取 {len(sample_list)} 个英雄进行线上数据校验...")
    print(f"    抽中: {', '.join([cn for cn, _ in sample_list])}")
    
    official_cn_to_en = {cn: en for en, cn in official_en_to_cn.items()}
    has_diff = False

    def check_diff_callback(cn_name, crawled_items):
        nonlocal has_diff
        en_name = official_cn_to_en.get(cn_name, cn_name)
        local_rows = history_data.get(en_name, [])
        
        if not local_rows:
            print(f"    ⚡ [{cn_name}] 本地无数据 → 存在差异")
            has_diff = True
            return True  # 触发提前结束
            
        if compare_hero_data(local_rows, crawled_items):
            print(f"    ⚡ [{cn_name}] 数据有变动 → 存在差异")
            has_diff = True
            return True  # 触发提前结束
        else:
            print(f"    ✅ [{cn_name}] 数据一致")
            return False

    sample_data, failed = crawler.crawl_champions(sample_list, early_stop_func=check_diff_callback)
    
    if failed:
        print(f"\n⚠️ 抽样爬取失败的英雄: {failed}，跳过失败英雄继续比对。")
        # 不丢弃已成功的数据，只跳过失败的
    
    return has_diff, sample_data

# ================= 主程序 (命令行入口) =================
def main():
    print("=== ARAM 数据自动维护管理器 v8.1 ===\n")

    # 1. 自动执行基础设施同步（每次必执行，速度很快）
    official_en_to_cn, official_cn_to_en, new_champs, renamed_champs = sync_official_data()
    if not official_en_to_cn:
        return

    update_pinyin_file(official_cn_to_en)

    # 2. 核心菜单选择
    print("\n请选择要执行的任务:")
    print("   [1] 英雄数据：智能增量 (自动爬取: 全新英雄 + 改名英雄 + 本地无数据的英雄)")
    print("   [2] 英雄数据：全量更新 (强制重新爬取所有英雄，耗时较长)")
    print("   [3] 英雄数据：极速补漏 (仅爬取本地无数据的英雄)")
    print("   [4] 英雄数据：精确打击 (手动输入指定英雄名称进行更新)")
    print("   [5] 英雄数据：抽样校验 (随机抽取3个英雄比对，有差异则自动全量更新)")
    
    choice = input("\n请输入选项 (默认1): ").strip()
    if not choice:
        choice = '1'

    official_data = (official_en_to_cn, official_cn_to_en, new_champs, renamed_champs)

    if choice == '4':
        # 精确打击：手动输入英雄名
        user_input = input("请输入要更新的英雄名、拼音缩写或英文ID (多个用逗号或空格分隔): ").strip()
        names = [n.strip() for n in re.split(r'[,，\s]+', user_input) if n.strip()]
        if names:
            update_specific_heroes(names)
        else:
            print("未输入有效英雄名")
    else:
        mode_map = {'1': 'smart', '2': 'full', '3': 'patch', '5': 'spot_check'}
        mode = mode_map.get(choice, 'smart')
        run_update(mode=mode, official_data=official_data)

    print("\n✅ 任务结束。")


# ================= GUI API 接口 =================

def run_update(mode='smart', log_func=None, official_data=None):
    """
    供 GUI 和 CLI 调用的统一更新接口。
    
    Args:
        mode: 'smart' | 'full' | 'patch' | 'spot_check'
        log_func: 日志回调函数 log_func(message: str)
        official_data: (英文到中文, 中文到英文, 新英雄, 改名英雄) 元组，
                       如已提前同步可传入避免重复请求
    
    Returns:
        bool: 是否成功
    """
    _log = log_func or print
    
    try:
        # 1. 同步官方数据 (或使用已有数据)
        if official_data:
            official_en_to_cn, official_cn_to_en, new_champs, renamed_champs = official_data
        else:
            _log("正在同步官方英雄数据...")
            official_en_to_cn, official_cn_to_en, new_champs, renamed_champs = sync_official_data()
            if not official_en_to_cn:
                _log("❌ 官方数据同步失败")
                return False
            _log(f"✅ 同步完成: {len(official_en_to_cn)} 个英雄")
            update_pinyin_file(official_cn_to_en)
            _log("✅ 拼音文件已更新")
        
        # 2. 加载历史数据
        history_data = load_csv_history()
        missing_champs = [en for en in official_en_to_cn if en not in history_data]
        target_list = []
        new_crawl_data = {}
        
        # 3. 根据模式构建目标列表
        if mode == 'full':
            _log("模式: 全量更新")
            target_list = [(cn, en) for en, cn in official_en_to_cn.items()]
        
        elif mode == 'spot_check':
            _log("模式: 抽样校验")
            has_diff, sample_data = spot_check_and_update(official_en_to_cn, history_data)
            if has_diff:
                _log("🔄 检测到数据差异，触发全量更新...")
                target_list = [(cn, en) for en, cn in official_en_to_cn.items()]
            else:
                _log("✅ 抽样数据与本地一致，无需更新")
                return True
        
        elif mode == 'patch':
            _log("模式: 极速补漏")
            target_list = [(official_en_to_cn[en], en) for en in missing_champs]
        
        else:  # smart
            _log("模式: 智能增量")
            targets = set(new_champs + renamed_champs + missing_champs)
            target_list = [(official_en_to_cn[en], en) for en in targets]
        
        # 4. 执行爬取
        if target_list:
            _log(f"准备爬取 {len(target_list)} 个英雄...")
            new_crawl_data, failed_list = crawler.crawl_champions(target_list)
            if failed_list:
                _log(f"⚠ 爬取失败的英雄: {', '.join(failed_list)}")
        elif not new_crawl_data:
            _log("无需爬取")
        
        # 5. 合并保存
        merge_and_save(official_en_to_cn, history_data, new_crawl_data)
        _log("✅ 数据合并保存完成")
        return True
        
    except Exception as e:
        _log(f"❌ 更新失败: {e}")
        return False


def download_from_github(log_func=None):
    """
    从 GitHub 仓库下载最新数据文件。
    
    Args:
        log_func: 日志回调函数
    
    Returns:
        bool: 是否全部成功
    """
    _log = log_func or print
    
    files_to_download = [
        ("data/hero_augments.csv", CSV_FILE, "英雄海克斯数据"),
        ("data/champions.json", CHAMPION_ID_FILE, "英雄名称映射"),
        ("data/pinyin_map.json", PINYIN_FILE, "拼音检索索引"),
    ]
    
    success_count = 0
    total = len(files_to_download)
    
    for idx, (remote_path, local_path, desc) in enumerate(files_to_download, 1):
        url = f"{GITHUB_RAW_BASE}/{remote_path}"
        _log(f"下载中 [{idx}/{total}]: {desc}...")
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, 'wb') as f:
                    f.write(resp.content)
                _log(f"✅ {desc} 下载成功 ({len(resp.content)} bytes)")
                success_count += 1
            else:
                _log(f"❌ {desc} 下载失败 (HTTP {resp.status_code})")
        except Exception as e:
            _log(f"❌ {desc} 下载异常: {e}")
    
    _log(f"下载完成: {success_count}/{total} 成功")
    return success_count == total


def update_specific_heroes(hero_names, log_func=None):
    """
    精确更新指定英雄的数据。
    
    Args:
        hero_names: 英雄名称列表 (中文或英文)
        log_func: 日志回调函数
    
    Returns:
        bool: 是否成功
    """
    _log = log_func or print
    
    try:
        _log("正在同步官方英雄数据...")
        official_en_to_cn, official_cn_to_en, _, _ = sync_official_data()
        if not official_en_to_cn:
            _log("❌ 官方数据同步失败")
            return False
        
        # 解析英雄名 (支持中文和英文)
        target_list = []
        for name in hero_names:
            name = name.strip()
            if name in official_cn_to_en:
                # 中文名
                en = official_cn_to_en[name]
                target_list.append((name, en))
                _log(f"  ✓ {name} ({en})")
            elif name in official_en_to_cn:
                # 英文名
                cn = official_en_to_cn[name]
                target_list.append((cn, name))
                _log(f"  ✓ {cn} ({name})")
            else:
                # 模糊匹配
                try:
                    from thefuzz import process
                    result_cn = process.extractOne(name, list(official_cn_to_en.keys()))
                    result_en = process.extractOne(name, list(official_en_to_cn.keys()))
                    candidates = [r for r in [result_cn, result_en] if r]
                    best = max(candidates, key=lambda x: x[1]) if candidates else None
                    if best and best[1] > 60:
                        matched = best[0]
                        if matched in official_cn_to_en:
                            en = official_cn_to_en[matched]
                            target_list.append((matched, en))
                            _log(f"  ✓ {matched} ({en}) [模糊匹配 '{name}']")
                        else:
                            cn = official_en_to_cn[matched]
                            target_list.append((cn, matched))
                            _log(f"  ✓ {cn} ({matched}) [模糊匹配 '{name}']")
                    else:
                        _log(f"  ✗ 未找到: {name}")
                except ImportError:
                    _log(f"  ✗ 未找到: {name} (提示: 安装 thefuzz 可启用模糊匹配)")
        
        if not target_list:
            _log("❌ 没有有效的英雄")
            return False
        
        _log(f"准备爬取 {len(target_list)} 个英雄...")
        history_data = load_csv_history()
        new_crawl_data, failed_list = crawler.crawl_champions(target_list)
        
        if failed_list:
            _log(f"⚠ 爬取失败: {', '.join(failed_list)}")
        
        merge_and_save(official_en_to_cn, history_data, new_crawl_data)
        _log("✅ 精确更新完成")
        return True
        
    except Exception as e:
        _log(f"❌ 更新失败: {e}")
        return False


if __name__ == "__main__":
    main()