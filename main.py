import time
import json
import csv
import os
import sys
import threading
import queue
import re
import tkinter as tk
import ctypes
import msvcrt  # 用于清除输入缓冲区
import numpy as np
from PIL import Image
import mss
import keyboard
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from thefuzz import process, fuzz
from scripts.config import BASE_DIR, DATA_DIR
from scripts.lcu_connector import LCUConnector, normalize_champion_key
from scripts import style_detect
from scripts import combo_recipes
from rapidocr_onnxruntime import RapidOCR

# 常见昵称 / 简称 → 中文称号
HERO_NICKNAMES = {
    "女警": "皮城女警",
    "ez": "探险家",
    "ezreal": "探险家",
    "老鼠": "瘟疫之源",
    "狗头": "荒漠屠夫",
    "剑圣": "无极剑圣",
    "男枪": "法外狂徒",
    "女枪": "赏金猎人",
    "卡莎": "虚空之女",
    "霞": "逆羽",
    "洛": "幻翎",
    "锤石": "魂锁典狱长",
    "提莫": "迅捷斥候",
    "亚索": "疾风剑豪",
    "永恩": "封魔剑魂",
    "乌鸦": "诺克萨斯统领",
    "小炮": "麦林炮手",
    "大嘴": "深渊巨口",
    "小鱼人": "潮汐海灵",
    "卡牌": "卡牌大师",
    "诺手": "诺克萨斯之手",
    "蛮王": "蛮族之王",
    "德玛": "德玛西亚之力",
    "盖伦": "德玛西亚之力",
    "皇子": "德玛西亚皇子",
    "赵信": "德邦总管",
    "悟空": "齐天大圣",
    "大圣": "齐天大圣",
    "牛头": "牛头酋长",
    "机器人": "蒸汽机器人",
    "布隆": "弗雷尔卓德之心",
    "男刀": "不祥之刃",
    "女刀": "不祥之刃",
    "刀妹": "刀锋舞者",
    "剑姬": "无双剑姬",
    "武器": "武器大师",
    "贾克斯": "武器大师",
    "瑞文": "放逐之刃",
    "阿卡丽": "离群之刺",
    "卡特": "不祥之刃",
    "妖姬": "诡术妖姬",
    "狐狸": "九尾妖狐",
    "光辉": "光辉女郎",
    "拉克丝": "光辉女郎",
    "火男": "复仇焰魂",
    "冰女": "冰霜女巫",
    "风女": "风暴之怒",
    "宝石": "暗黑元首",
    "蛇女": "魔蛇之拥",
    "螳螂": "虚空掠夺者",
    "螃蟹": "虚空遁地兽",
    "大虫": "虚空恐惧",
    "小法": "黑暗之女",
    "安妮": "黑暗之女",
    "飞机": "英勇投弹手",
    "人马": "战争之影",
    "猪女": "北地之怒",
    "寡妇": "痛苦之拥",
    "蜘蛛": "蜘蛛女皇",
    "瞎子": "盲僧",
    "李青": "盲僧",
    "vn": "暗夜猎手",
    "mf": "赏金猎人",
    "teemo": "迅捷斥候",
}

# ================= 配置与常量 =================

def get_regions():
    """根据当前屏幕分辨率动态计算海克斯文字截取区域 (以 2K 2560x1440 为基准等比缩放)"""
    with mss.mss() as sct:
        mon = sct.monitors[1]  # 主显示器
        W, H = mon['width'], mon['height']
    return {
        "hex_1": {'top': int(H * 0.375),  'left': int(W * 0.2539), 'width': int(W * 0.125), 'height': int(H * 0.0417)},
        "hex_2": {'top': int(H * 0.375),  'left': int(W * 0.4414), 'width': int(W * 0.125), 'height': int(H * 0.0417)},
        "hex_3": {'top': int(H * 0.375),  'left': int(W * 0.625),  'width': int(W * 0.125), 'height': int(H * 0.0417)},
    }

REGIONS = get_regions()

# --- OCR 性能 / 稳定性调参 ---------------------------------------------------
# 推理线程数：onnxruntime 默认吃满所有物理核，游戏在跑时会被抢 CPU → 卡顿。
# 压到 2 就够了（区域只有 320x60，2x 上采样后 640x120，计算量很小）。
OCR_INTRA_THREADS = 2
# 抓帧重试：卡牌有入场动画，动画途中截图会拿到空文本/半截文本。
# 同一张静态截图上 OCR 正确率是 100%，说明失败来自"抓帧时机"，所以对失败区域重抓。
OCR_RETRY_ATTEMPTS = 6      # 最多抓几轮（动画慢时也能等到文字稳定）
OCR_RETRY_DELAY = 0.16      # 每轮之间的等待（秒），逐轮递增
# UI 存在性探测（不是正式识别）：只有区域像素"像一张卡片"时才值得跑 OCR。
# 阈值来自实测标定（3 张 2K 截图 × 3 张牌 vs 15 个地形采样点）：
#   卡片区域  暗底 0.91~0.95，亮字 0.037~0.073
#   地形区域  亮字基本恒为 0.000（唯一两个 0.038/0.040 的样本暗底只有 0.37/0.46）
# 因此「暗底 ≥0.80 且 亮字 ≥0.02」可以干净分开全部样本。
UI_PROBE_MIN_INTERVAL = 2.0     # OCR 分支最短间隔（秒），防止病态循环
UI_PROBE_DARK_RATIO = 0.80      # 暗底占比下限
UI_PROBE_BRIGHT_RATIO = (0.02, 0.35)  # 亮字占比区间

COLORS = {
    "normal": "#00FF00",  # 绿色
    "best":   "#FFD700",  # 金色
    "status": "yellow",   # 黄色
    "error":  "#FF3333",  # 红色
    "bg":     "#000000"   # 背景黑
}

# ================= 1. 数据管理 (Model) =================

def normalize_augment_key(name):
    """海克斯名归一化：统一全角标点、去空白。用于跨来源匹配官方库与 CSV。"""
    if not name:
        return ""
    s = str(name).strip()
    s = s.replace("：", ":").replace("（", "(").replace("）", ")")
    return re.sub(r"[\s\u3000]+", "", s)


class DataManager:
    """负责加载和管理静态数据"""
    def __init__(self):
        self.hero_data = {}
        # 拼音映射改为 defaultdict(list)，支持一个拼音对应多个英雄
        self.pinyin_map = defaultdict(list)
        self.cn_to_en = {}
        self.en_to_cn = {}
        self.en_names = []
        self.nicknames = dict(HERO_NICKNAMES)

        # ★ 官方海克斯库（"识别层"）—— 与 hero_data（"排名层"）职责分离：
        #   hero_data 答"这个英雄该选它排第几"，official_* 答"这到底是不是一个合法海克斯"。
        #   这样某英雄没排名时，也不会退化成"未识别"。
        self.official_augments = {}   # apiName -> 记录
        self.official_names = {}      # 归一化名称 -> 记录
        self.official_ready = False
        self.hero_norm = {}           # 英雄 -> {归一化海克斯名: (原名, 排名信息)}

        self.base_dir = BASE_DIR
        self.data_dir = DATA_DIR
        self._load_data()

    def _load_data(self):
        print("--- 正在加载数据资源 ---")



        # 2. 加载英雄数据 (CSV)
        csv_path = os.path.join(self.data_dir, 'hero_augments.csv')
        if not os.path.exists(csv_path):
            print(f"❌ 错误: 找不到文件 {csv_path}")
            print(f"   请确认该文件位于: {self.data_dir}")
        else:
            try:
                encoding = 'utf-8-sig'
                
                raw_hero_list = defaultdict(list)
                with open(csv_path, 'r', encoding=encoding) as f:
                    reader = csv.reader(f)
                    header = next(reader, None) # 跳过表头
                    is_new_format = header and "等级" in header
                    has_overall_rank = header and "总排名" in header
                    
                    for row in reader:
                        if not row: continue
                        hero = row[0].strip()
                        
                        if has_overall_rank and len(row) >= 6:
                            # 最新格式: 中文名,英文名,等级,总排名,等级内序号,海克斯名称
                            tier = row[2].strip()
                            try: overall_rank = int(row[3])
                            except (ValueError, IndexError): overall_rank = 999
                            try: t_rank = int(row[4])
                            except (ValueError, IndexError): t_rank = 999
                            name = row[5].strip()
                            
                            if hero not in self.hero_data: self.hero_data[hero] = {}
                            self.hero_data[hero][name] = {
                                "tier": tier,
                                "overall_rank": overall_rank,
                                "t_rank": t_rank
                            }
                        elif is_new_format and len(row) >= 5:
                            # 旧新格式: 中文名,英文名,等级,等级内序号,海克斯名称 (无总排名)
                            tier = row[2].strip()
                            try: t_rank = int(row[3])
                            except (ValueError, IndexError): t_rank = 999
                            name = row[4].strip()
                            
                            if hero not in self.hero_data: self.hero_data[hero] = {}
                            self.hero_data[hero][name] = {
                                "tier": tier,
                                "overall_rank": 999,
                                "t_rank": t_rank
                            }
                        elif not is_new_format and len(row) >= 4:
                            try: rank = int(row[2])
                            except (ValueError, IndexError): rank = 999
                            aug = row[3].strip()
                            raw_hero_list[hero].append((rank, aug))
                
                # 如果存在旧格式的数据，走旧的合并逻辑
                if raw_hero_list:
                    for hero, aug_list in raw_hero_list.items():
                        if hero in self.hero_data: continue # 跳过已被新格式处理的
                        aug_list.sort(key=lambda x: x[0])
                        counters = {"白银": 1, "黄金": 1, "棱彩": 1, "未知": 1}
                        h_dict = {}
                        for rank, name in aug_list:
                            tier = "未知"
                            h_dict[name] = {
                                "tier": tier, 
                                "overall_rank": 999,
                                "t_rank": counters.get(tier, 1)
                            }
                            if tier in counters: counters[tier] += 1
                        self.hero_data[hero] = h_dict
                
                print(f"✅ 英雄数据加载完毕: 共 {len(self.hero_data)} 个英雄")
            except Exception as e:
                print(f"❌ CSV 读取严重失败: {e}")

        # 3. 加载拼音映射 (构建一对多关系)
        pinyin_file = os.path.join(self.data_dir, 'pinyin_map.json')
        if os.path.exists(pinyin_file):
            try:
                with open(pinyin_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for cn, py in data.items():
                        if cn not in self.pinyin_map[py]:
                            self.pinyin_map[py].append(cn)
                        if cn not in self.pinyin_map[cn]:
                            self.pinyin_map[cn].append(cn)
            except Exception as e:
                print(f"⚠️ {pinyin_file} 加载异常: {e}")

        # 4. champions.json: EN ↔ CN（供英文输入 / 校验）
        champions_file = os.path.join(self.data_dir, 'champions.json')
        if os.path.exists(champions_file):
            try:
                with open(champions_file, 'r', encoding='utf-8') as f:
                    self.cn_to_en = json.load(f)
                for cn, en in self.cn_to_en.items():
                    if not en:
                        continue
                    self.en_to_cn[en.lower()] = cn
                    self.en_to_cn[normalize_champion_key(en)] = cn
                    self.en_names.append(en)
            except Exception as e:
                print(f"⚠️ {champions_file} 加载异常: {e}")

        # 5. 归一化排名索引：CSV 里可能有全角冒号/空格等差异，查询时统一成 key
        self.hero_norm = {}
        for _hero, _augs in self.hero_data.items():
            _m = {}
            for _name, _info in _augs.items():
                _k = normalize_augment_key(_name)
                if _k and _k not in _m:
                    _m[_k] = (_name, _info)
            self.hero_norm[_hero] = _m

        # 6. 官方海克斯库（由 scripts/build_augments_official.py 生成）
        #    作为"识别层"：判断 OCR 文本是不是一个 ARAM 海克斯大乱斗的合法海克斯。
        aug_file = os.path.join(self.data_dir, 'augments_official.json')
        if os.path.exists(aug_file):
            try:
                with open(aug_file, 'r', encoding='utf-8') as f:
                    db = json.load(f)
                for rec in db.get('augments', []):
                    if not rec.get('inAramPool'):
                        continue
                    api = rec.get('apiName')
                    if not api:
                        continue
                    # 名字本身是占位符的（Riot 自己的残留，如 ARAM_MissingPingAugment）跳过
                    nm = (rec.get('nameZh') or '').strip()
                    if not nm or re.fullmatch(r'[\?？\s]+', nm):
                        continue
                    self.official_augments[api] = rec
                    for candidate in (rec.get('nameZh'), rec.get('nameEn')):
                        if candidate:
                            self.official_names[normalize_augment_key(candidate)] = rec
                self.official_ready = bool(self.official_augments)
                print(f"✅ 官方海克斯库加载完毕: {len(self.official_augments)} 个"
                      f"（识别层就绪）")
            except Exception as e:
                print(f"⚠️ {aug_file} 加载异常，识别层降级为纯 CSV: {e}")
        else:
            print(f"ℹ️ 未找到 {aug_file}，识别层未启用（不影响排名层功能）")

        # 昵称仅保留数据库中存在的英雄
        self.nicknames = {
            k.lower() if k.isascii() else k: v
            for k, v in self.nicknames.items()
            if v in self.hero_data or v in self.cn_to_en
        }
        
        print("-> 数据初始化完成")

    def search_hero(self, query):
        """
        英雄搜索逻辑 (增强模糊匹配)
        接受: 精确中文 / 英文 / 拼音首字母 / 昵称 / thefuzz(CN+EN)
        返回: (匹配列表, 是否精确匹配)
        """
        raw = (query or "").strip()
        if not raw:
            return [], False
        q_lower = raw.lower()

        # 0. 昵称表
        nick_key = q_lower if raw.isascii() else raw
        nick = self.nicknames.get(nick_key) or self.nicknames.get(raw) or self.nicknames.get(q_lower)
        if nick and (nick in self.hero_data or not self.hero_data):
            if not self.hero_data or nick in self.hero_data:
                return [nick], True

        # 1. 拼音 / 中文直接
        if q_lower in self.pinyin_map:
            return self.pinyin_map[q_lower], True
        if raw in self.pinyin_map:
            return self.pinyin_map[raw], True

        # 2. 精确中文
        if raw in self.hero_data:
            return [raw], True

        # 3. 精确英文（含 normalize）
        en_hit = self.en_to_cn.get(q_lower) or self.en_to_cn.get(normalize_champion_key(raw))
        if en_hit and (en_hit in self.hero_data or not self.hero_data):
            if not self.hero_data or en_hit in self.hero_data:
                return [en_hit], True

        # 4. thefuzz: 中文称号 + 英文名
        if self.hero_data:
            cn_keys = list(self.hero_data.keys())
            result = process.extractOne(raw, cn_keys, scorer=fuzz.WRatio)
            if result and result[1] >= 60:
                return [result[0]], False
            if self.en_names:
                en_result = process.extractOne(raw, self.en_names, scorer=fuzz.WRatio)
                if en_result and en_result[1] >= 60:
                    cn = self.en_to_cn.get(en_result[0].lower()) or self.en_to_cn.get(
                        normalize_champion_key(en_result[0])
                    )
                    if cn and cn in self.hero_data:
                        return [cn], False

        return [], False

    def validate_hero(self, name, threshold=65):
        """验证英雄名是否在数据库中；精确 CN/EN/拼音/昵称 + 模糊(~60-70)。"""
        if not name:
            return None
        matches, _exact = self.search_hero(name)
        if matches:
            hit = matches[0]
            if not self.hero_data or hit in self.hero_data:
                return hit
        if name in self.hero_data:
            return name
        if not self.hero_data:
            return None
        result = process.extractOne(str(name), list(self.hero_data.keys()), scorer=fuzz.WRatio)
        if result and result[1] >= threshold:
            return result[0]
        return None

# ================= 2. 图像分析 (Core Logic) =================

class GameAnalyzer:
    """负责 OCR 和 图像处理"""
    TIER_PRIORITY = {"棱彩": 0, "黄金": 1, "白银": 2, "未知": 3}
    RARITY_PRIORITY = {"棱彩": 0, "黄金": 1, "事件抉择": 1, "白银": 2}

    def __init__(self, data_manager):
        self.dm = data_manager
        # OCR 引擎: 降低 det_limit_side_len (默认736→480→320)
        # 截取区域 2x 上采样后最大 640px。实测 det_limit=320 时单张 OCR 约 330ms(480 时约 580ms)。
        #
        # ★ 卡顿治理：onnxruntime 默认会吃满所有物理核。游戏在跑的时候被抢 CPU 就是卡顿感来源。
        #   这里显式把推理线程压到 2 / 1，把核心让给游戏本体。
        try:
            self.ocr = RapidOCR(
                use_angle_cls=False,
                det_limit_side_len=320,
                intra_op_num_threads=2,
                inter_op_num_threads=1,
            )
        except (KeyError, TypeError, Exception) as e:
            py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            print(f"\n❌ OCR 引擎初始化失败: {e}")
            print(f"   当前 Python 版本: {py_ver}")
            if sys.version_info >= (3, 13):
                print(f"   ⚠ rapidocr_onnxruntime 仅支持 Python <3.13，请使用 Python 3.10~3.12")
            else:
                print(f"   请尝试: pip install rapidocr_onnxruntime<=1.4.4")
            # 退路：老版本不接受线程参数时，退化成默认构造，至少不崩
            try:
                self.ocr = RapidOCR(use_angle_cls=False, det_limit_side_len=320)
                print("   ⚠ 已退化为默认 OCR 配置（线程未受限，可能影响游戏帧率）")
            except Exception:
                raise
        # 根据 CPU 逻辑核心数决定并发策略
        self._cpu_count = os.cpu_count() or 4
        # ★ 三个区域不再并发跑 OCR：并发 = 3 份 onnxruntime 同时抢核，是卡顿的主要来源。
        #   串行 3×~250ms 完全够用（选牌界面会停留好几秒），但 CPU 占用只有 1/3。
        self._use_parallel = False
        self.executor = ThreadPoolExecutor(max_workers=1)
        # UI 探测的退避状态
        self._probe_miss_streak = 0
        self._probe_last_ocr = 0.0
        self._total_cache = None
        # 预热 OCR 引擎 (消除首次推理的模型加载和内存分配延迟)
        self._warmup()

    @staticmethod
    def looks_like_card(arr):
        """极便宜的像素预筛：海克斯卡是「暗底 + 少量亮字」。

        只做两次 numpy 比较（320x60 隔点采样后约 4.8k 像素），微秒级。
        游戏本体画面落在同一坐标上是地形，几乎不含接近白色的像素 → 直接被拦掉，
        不会触发 OCR。这一步是消除"整局持续 OCR 导致卡顿"的关键。
        """
        if arr is None or getattr(arr, "size", 0) == 0:
            return False
        flat = arr[::2, ::2]
        total = flat.size
        if total == 0:
            return False
        dark = float(np.count_nonzero(flat < 60)) / total
        bright = float(np.count_nonzero(flat > 170)) / total
        lo, hi = UI_PROBE_BRIGHT_RATIO
        return dark >= UI_PROBE_DARK_RATIO and lo <= bright <= hi

    def probe_ui(self):
        """判断海克斯选牌 UI 是否出现。

        流程：抓中间区(便宜) → 像素预筛(几乎免费) → 通过才 OCR。
        正常对局中 99% 的 tick 会停在像素预筛那一步，OCR 完全不跑。
        """
        images = self.capture_all_regions(["hex_2"])
        arr = images.get("hex_2")
        if not self.looks_like_card(arr):
            self._probe_miss_streak = 0
            return False

        # 保险丝：像素像是卡片但 OCR 连续读不出字（比如某张特效卡面），
        # 退化成低频探测，避免变成新的空转源。
        now = time.time()
        if (self._probe_miss_streak >= 5
                and now - self._probe_last_ocr < UI_PROBE_MIN_INTERVAL):
            return False
        self._probe_last_ocr = now

        try:
            # 这里只需要"有没有字"，走快路径即可（14ms 而不是 180ms）
            txt = self.ocr_text(arr, use_det=False)
        except Exception as e:
            print(f"UI 探测 OCR 异常: {e}")
            return False
        hit = len(txt.strip()) >= 2
        self._probe_miss_streak = 0 if hit else self._probe_miss_streak + 1
        return hit

    def _warmup(self):
        """用小图预热 OCR 引擎, 消除首次手动刷新识别的冷启动延迟"""
        try:
            dummy = np.zeros((48, 320), dtype=np.uint8)
            self.ocr(dummy)
            print(f"OCR 引擎预热完成 (CPU: {self._cpu_count} 核, "
                  f"{'并发' if self._use_parallel else '串行'}模式, 推理线程 intra={OCR_INTRA_THREADS})")
        except Exception:
            pass

    def capture_all_regions(self, keys=None):
        """批量截图: 复用单个 mss 上下文, 避免重复初始化开销"""
        images = {}
        try:
            with mss.mss() as sct:
                for key in (keys or REGIONS.keys()):
                    if key not in REGIONS:
                        continue
                    region = REGIONS[key]
                    monitor = {
                        "top": int(region["top"]),
                        "left": int(region["left"]),
                        "width": int(region["width"]),
                        "height": int(region["height"]),
                        "mon": 0
                    }
                    raw = sct.grab(monitor)
                    img = Image.frombytes("RGB", raw.size, raw.rgb)
                    gray = img.convert("L")
                    w, h = gray.size
                    # 2倍上采样提高文字清晰度
                    resized = gray.resize((w * 2, h * 2), Image.BICUBIC)
                    images[key] = np.array(resized)
        except Exception as e:
            print(f"批量截图失败: {e}")
        return images

    @staticmethod
    def _match_cascade(clean, keys):
        """四级匹配兜底：精确 -> 完整比例 -> 局部比例 -> 包含关系。

        返回 (命中的名称, 命中的层级) 或 (None, None)。
        注意：keys 必须先转成 list —— 传 dict 会被 rapidfuzz 当成 Mapping，
        它会去比对 value（我们的 value 是字典），直接抛 "sentence must be a String"。

        阈值说明：官方池有 492 个候选名（中英双份），候选越多误判概率越高。
        实测量过：随口一句"这不是一个海克斯"对"海克斯核心"的 partial_ratio 恰好是 75，
        所以局部匹配门槛必须抬到 85，并加上长度带约束（长度悬殊时局部比例天然虚高）。
        """
        if not clean or not keys:
            return None, None
        keys = list(keys)
        n = max(1, len(clean))
        try:
            if clean in keys:
                return clean, "exact"
            # 2. 完整比例
            match, score = process.extractOne(clean, keys, scorer=fuzz.ratio)
            if match and score >= 70 and 0.5 <= len(match) / n <= 2.0:
                return match, "ratio"
            # 3. 局部比例（OCR 少读/多读一个字时更稳）
            match, score = process.extractOne(clean, keys, scorer=fuzz.partial_ratio)
            if match and score >= 85 and 0.6 <= len(match) / n <= 1.7:
                return match, "partial"
            # 4. 包含关系兜底（名称整段出现在 OCR 文本里，常带后缀描述）
            for name in keys:
                if len(name) >= 3 and (name in clean or clean in name):
                    if name in clean:
                        return name, "contain"
                    if len(clean) >= 2 and len(clean) / len(name) >= 0.7:
                        return name, "contain-close"
        except Exception as e:
            print(f"匹配异常: {e}")
        return None, None

    def _ranked(self, hero_cn, key, official_rec=None):
        """在"排名层"里查归一化后的名称，命中则返回带排名的结果。"""
        hero_norm = self.dm.hero_norm.get(hero_cn, {}) or {}
        if key and key in hero_norm:
            disp, info = hero_norm[key]
            return {"display": disp, "info": info,
                    "official_rec": official_rec or self.dm.official_names.get(key),
                    "official_only": False, "tier": info.get('tier', '未知')}
        return None

    def resolve_augment(self, clean, hero_cn):
        """两层解析：先认（官方库），再排（本英雄排名）。

        用名称"来源可信度"决定优先级，避免拿一个模糊的 CSV 猜测覆盖官方的精确判定：
          1. CSV 精确命中      —— 最可信，直接带排名
          2. 官方 精确命中     —— 权威；若本英雄有排名则带排名，否则仅识别
          3. 官方 模糊命中     —— 同上
          4. CSV 模糊命中      —— 兜底（保留旧行为）
          5. 全不中            —— 未识别

        独立成方法是为了能脱离 OCR 单测。

        Returns:
            dict | None
        """
        key = normalize_augment_key(clean)
        if not key:
            return None

        # 1) CSV 精确命中
        hit = self._ranked(hero_cn, key)
        if hit:
            return hit

        # 2) 官方精确命中
        rec = self.dm.official_names.get(key) if self.dm.official_ready else None
        if rec:
            canon = normalize_augment_key(rec.get('nameZh') or '')
            hit = self._ranked(hero_cn, canon, rec)
            if hit:
                return hit
            return {"display": rec.get('nameZh') or rec.get('nameEn') or '未知海克斯',
                    "info": None, "official_rec": rec,
                    "official_only": True, "tier": "未知"}

        # 3) 官方模糊命中
        if self.dm.official_ready:
            m, _stage = self._match_cascade(key, self.dm.official_names.keys())
            if m:
                rec = self.dm.official_names[m]
                canon = normalize_augment_key(rec.get('nameZh') or '')
                hit = self._ranked(hero_cn, canon, rec)
                if hit:
                    return hit
                return {"display": rec.get('nameZh') or rec.get('nameEn') or '未知海克斯',
                        "info": None, "official_rec": rec,
                        "official_only": True, "tier": "未知"}

        # 4) CSV 模糊兜底
        if self.dm.hero_norm.get(hero_cn):
            m, _stage = self._match_cascade(key, self.dm.hero_norm[hero_cn].keys())
            if m:
                disp, info = self.dm.hero_norm[hero_cn][m]
                return {"display": disp, "info": info,
                        "official_rec": self.dm.official_names.get(m),
                        "official_only": False, "tier": info.get('tier', '未知')}

        return None

    @staticmethod
    def _ocr_text_from(result):
        """把 RapidOCR 的返回拼成字符串（两种返回格式都兼容）。"""
        if not result:
            return ""
        parts = []
        for item in result:
            if not item:
                continue
            # use_det=True -> [box, text, score]；use_det=False -> [text, score]
            if len(item) >= 3 and isinstance(item[1], str):
                parts.append(item[1])
            elif isinstance(item[0], str):
                parts.append(item[0])
        return "".join(parts).replace(" ", "").replace(".", "")

    def ocr_text(self, img, use_det=True):
        """取一张区域的文本。异常时返回空串（调用方会走兜底）。"""
        try:
            result, _ = self.ocr(img, use_det=use_det)
        except Exception as e:
            print(f"OCR 异常 (use_det={use_det}): {e}")
            return ""
        return self._ocr_text_from(result)

    @staticmethod
    def clean_ocr_text(txt):
        """去首尾非中英文字符（可能混入等级角标/图标符号/空格点）"""
        s = (txt or "").strip()
        if s:
            s = re.sub(r'^[^一-鿿A-Za-z0-9]+|[^一-鿿A-Za-z0-9]+$', '', s)
        return s

    def _known_exact(self, hero_cn, clean):
        """归一化后是否精确命中官方池或本英雄排名表。"""
        if not clean:
            return False
        key = normalize_augment_key(clean)
        if key in self.dm.official_names:
            return True
        return key in (self.dm.hero_norm.get(hero_cn) or {})

    def _ocr_and_match(self, key, img, hero_cn):
        """对单张已截取的图片执行 OCR 识别 + 数据匹配。

        两层结构（这是修掉「？？？→未识别」的关键）：
          识别层 `official_names` —— 官方海克斯库里到底有没有这个东西；
          排名层 `hero_norm`     —— 这个英雄身上它排第几。
        先认、再排。官方认出来了但本英雄没排名时，显示"暂无排名"，
        而不是像以前那样退化成"未识别"。

        性能：这个区域本来就是**一行字**，所以先试"跳过文字检测"的快路径
        （实测 14ms vs 带检测 179ms，快约 13 倍）。快路径偶尔会漏字/多字，
        因此**只在归一化后完全相等时才采纳**——零精度风险，白赚速度；
        不相等就老老实实走完整路径。
        """
        try:
            if img is None:
                return {"key": key, "text": "截图错误", "error": True}

            res = {
                "key": key, "valid": False, "rank": 999,
                "text": "", "highlight": False, "error": False,
                "official_only": False,
            }

            clean = ""
            fast = self.clean_ocr_text(self.ocr_text(img, use_det=False))
            if self._known_exact(hero_cn, fast):
                clean = fast
            else:
                clean = self.clean_ocr_text(self.ocr_text(img, use_det=True)) or fast

            if not clean:
                res["text"] = "⚠ 无文字\n可点左上角「刷新识别」"
                res["error"] = True
                return res

            hit = self.resolve_augment(clean, hero_cn)
            if not hit:
                res["text"] = "⚠ 未识别\n可点左上角「刷新识别」"
                res["error"] = True
                return res

            fmt = self.format_hit(hit, hero_cn)
            res.update(fmt)
            res["valid"] = True
            return res

        except Exception as e:
            print(f"处理异常 ({key}): {e}")
            return {"key": key, "text": "Error", "error": True}

    def _hero_augment_total(self, hero_cn):
        """该英雄在排名表里的候选总数，用于把排名表达成「第N/总数」。"""
        n = getattr(self, "_total_cache", None)
        if n is None:
            n = self._total_cache = {}
        if hero_cn not in n:
            n[hero_cn] = len(self.dm.hero_norm.get(hero_cn) or {})
        return n[hero_cn]

    def format_hit(self, hit, hero_cn):
        """把解析结果格式化成遮罩上显示的文本（抽出来是为了能单测）。

        旧文案：`【术士果汁盒】总No.3 | 黄金 No.3`
          —— "No." 指代不明，两个 3 也看不出好坏。
        新文案：`【术士果汁盒】黄金 · 总榜 3 / 180`
          —— 一眼知道稀有度、全榜第几、一共多少个候选。
        """
        if not hit:
            return None
        display = hit.get("display") or "未知海克斯"
        info = hit.get("info")
        if info:
            tier = info.get('tier', '?')
            o_rank = info.get('overall_rank', 999)
            total = self._hero_augment_total(hero_cn)
            tail = f"总榜 {o_rank}"
            if total:
                tail += f" / {total}"
            return {"text": f"【{display}】\n{tier} · {tail}",
                    "tier": tier, "t_rank": info.get('t_rank', 999),
                    "overall_rank": o_rank, "official_only": False}
        rarity = (hit.get('official_rec') or {}).get('rarityZh') or ''
        head = rarity or '官方池内'
        return {"text": f"【{display}】\n{head} · 暂无排名",
                "tier": "未知", "t_rank": 999,
                "overall_rank": 999, "official_only": True}

    def analyze(self, hero_cn, only_keys=None, retry=True):
        """识别三张牌（或指定区域）。

        为什么要抓帧重试：OCR 本身没问题 —— 拿用户的静态截图重跑，6/6 全对。
        真正的问题是**卡牌有入场动画**，动画途中截图会拿到空文本/半截文本，
        表现成用户看到的「❌ 无文字 / ❌ 未识别 / 推荐迟迟不出来」。
        所以这里只对**抓失败的**区域短间隔重抓，直到三区都出文字或到上限。

        另外：不再并发跑三个区域。并发 = 3 份 onnxruntime 同时抢核，
        是游戏内卡顿的主要来源；串行总耗时只多几百毫秒，但 CPU 占用降到 1/3。
        """
        if not hero_cn:
            return {}
        keys = [k for k in (only_keys or REGIONS.keys()) if k in REGIONS]
        results = {}
        pending = list(keys)
        total_attempts = OCR_RETRY_ATTEMPTS if retry else 1

        for attempt in range(total_attempts):
            if not pending:
                break
            images = self.capture_all_regions(pending)
            last = attempt == total_attempts - 1
            still_pending = []
            for key in pending:
                data = self._ocr_and_match(key, images.get(key), hero_cn)
                if data.get("valid") or last:
                    results[key] = data
                else:
                    still_pending.append(key)
            pending = still_pending
            if pending:
                time.sleep(OCR_RETRY_DELAY * (attempt + 1))

        # 计算最优推荐：总排名优先（越小越好），其次等级、等级内序号，最后看稀有度。
        # 只高亮**一张** —— 之前并列会同时高亮多张，反而看不出该选哪个。
        valid_matches = [it for it in results.values() if it.get("valid")]
        if valid_matches:
            def sort_key(item):
                rec = item.get("official_rec") or {}
                return (
                    item.get('overall_rank', 999),
                    self.TIER_PRIORITY.get(item.get('tier', '未知'), 3),
                    item.get('t_rank', 999),
                    self.RARITY_PRIORITY.get(rec.get('rarityZh'), 9),
                )

            best = min(valid_matches, key=sort_key)
            results[best['key']]["highlight"] = True

        # 流派识别（三层中的第三层）：三张牌里 ≥2 张同流派时，给"本局可走 XX 流"提示。
        # 数据来源：augments_official.json 的 apiName / nameZh / descZh 关键词匹配，
        # 100% 覆盖率，不爬虫、不引入新文件。详见 scripts/style_detect.py。
        try:
            hint, text = style_detect.analyze_combo(results)
            if hint:
                results["_combo"] = {
                    "combo_only": True,
                    "code": hint["code"],
                    "label": hint["label"],
                    "count": hint["count"],
                    "names": hint["names"],
                    "text": text,
                }
        except Exception as e:
            print(f"流派识别异常（不影响主流程）: {e}")

        # 热门玩法路线（万剑归宗等，见 data/combo_recipes.json）+ 赌狗玩法
        # （质变/潘朵拉这类随机结果海克斯）。两条独立提示，识别到即挂到 results。
        try:
            api_names = set()
            for it in valid_matches:
                rec = it.get("official_rec") or {}
                if rec.get("apiName"):
                    api_names.add(rec["apiName"])
            if api_names:
                offers = combo_recipes.analyze_offers(api_names)
                if offers.get("gamble"):
                    results["_gamble"] = {"combo_only": True, **offers["gamble"]}
                if offers.get("route"):
                    results["_route"] = {"combo_only": True, **offers["route"]}
        except Exception as e:
            print(f"玩法推荐异常（不影响主流程）: {e}")

        return results

# ================= 3. UI 界面 (View) =================

class OverlayApp:
    def __init__(self, root, queue, on_manual_refresh=None):
        self.root = root
        self.queue = queue
        self.labels = {}
        self._combo_label = None  # 中间一张牌上方的「流派推荐」提示（半透明金色）
        self._last_good = {}      # 每张牌最近一次识别成功的 (text, fg)：防"好结果被未识别覆盖"
        self.hide_timer = None
        self.on_manual_refresh = on_manual_refresh
        self._refresh_fab = None
        self._fab_label = None
        self._rune_strip_label = None  # click-through text on main overlay
        self._item_strip_label = None  # reserved; no item data yet
        
        # 先隐藏窗口，避免配置透明前闪白框
        self.root.withdraw()
        self._setup_window()
        self._setup_labels()
        self.root.deiconify()

        # 左上角独立小窗按钮（不穿透）；主 overlay 仍保持鼠标穿透
        if self.on_manual_refresh:
            self._create_refresh_fab()
        
        # 启动队列消息监听
        self.root.after(100, self.process_queue)

    def set_manual_refresh(self, callback):
        """运行中绑定/更换手动刷新回调（CLI 可在 controller 就绪后调用）。"""
        self.on_manual_refresh = callback
        if callback and self._refresh_fab is None:
            self._create_refresh_fab()

    def _create_refresh_fab(self):
        """游戏画面左上角置顶「刷新」按钮，点击=手动刷新识别。"""
        try:
            fab = tk.Toplevel(self.root)
            fab.overrideredirect(True)
            fab.attributes("-topmost", True)
            try:
                fab.attributes("-alpha", 0.92)
            except Exception:
                pass
            fab.configure(bg="#1a1520")
            # 与主 overlay 同屏左上角，略偏内，避免贴边难点
            ox = getattr(self, "offset_x", 0) + 12
            oy = getattr(self, "offset_y", 0) + 12
            fab.geometry(f"+{ox}+{oy}")

            lbl = tk.Label(
                fab,
                text="🔄 刷新",
                font=("Microsoft YaHei", 9, "bold"),
                fg="#f0c75e",
                bg="#2b2433",
                padx=10,
                pady=5,
                cursor="hand2",
                relief="ridge",
                bd=1,
            )
            lbl.pack()
            lbl.bind("<Button-1>", self._on_fab_click)
            fab.bind("<Button-1>", self._on_fab_click)
            self._refresh_fab = fab
            self._fab_label = lbl
            # 默认隐藏：未进入对局（无右上角读秒）不显示浮钮
            try:
                fab.withdraw()
            except Exception:
                pass
        except Exception as e:
            print(f"刷新浮钮创建失败: {e}")
            self._refresh_fab = None
            self._fab_label = None

    def set_fab_visible(self, visible: bool):
        """对局内显示左上角「刷新」浮钮；离开对局隐藏。"""
        if not self._refresh_fab:
            if visible and self.on_manual_refresh:
                self._create_refresh_fab()
            if not self._refresh_fab:
                return
        try:
            if visible:
                self._refresh_fab.deiconify()
                self._refresh_fab.attributes("-topmost", True)
                self._refresh_fab.lift()
            else:
                self._refresh_fab.withdraw()
        except Exception as e:
            print(f"刷新浮钮显隐: {e}")

    def _on_fab_click(self, event=None):
        if not self.on_manual_refresh:
            return
        lbl = self._fab_label
        if lbl is not None:
            try:
                lbl.config(text="识别中…", fg="#ffffff", bg="#4a3f1a")
            except Exception:
                pass
        try:
            self.on_manual_refresh()
        except Exception as e:
            print(f"刷新浮钮: {e}")

        def _restore():
            if self._fab_label is None:
                return
            try:
                self._fab_label.config(text="🔄 刷新", fg="#f0c75e", bg="#2b2433")
            except Exception:
                pass
        try:
            self.root.after(1000, _restore)
        except Exception:
            pass

    def _strip_origin(self):
        """Top-left just right of FAB (FAB ~80px wide at +12,+12)."""
        ox = getattr(self, "offset_x", 0) + 100
        oy = getattr(self, "offset_y", 0) + 16
        return ox, oy

    def set_rune_strip(self, text: str):
        """Show/hide click-through rune recommendation line near FAB."""
        lbl = self._rune_strip_label
        if lbl is None:
            return
        try:
            text = (text or "").strip()
            if not text:
                lbl.place_forget()
                return
            ox, oy = self._strip_origin()
            lbl.config(text=text, fg="#f0c75e")
            # absolute screen coords relative to fullscreen overlay
            lbl.place(x=ox - getattr(self, "offset_x", 0), y=oy - getattr(self, "offset_y", 0))
            lbl.lift()
        except Exception as e:
            print(f"rune strip: {e}")

    def set_item_strip(self, text: str):
        """Optional item line under runes; hidden when empty (no data yet)."""
        lbl = self._item_strip_label
        if lbl is None:
            return
        try:
            text = (text or "").strip()
            if not text:
                lbl.place_forget()
                return
            ox, oy = self._strip_origin()
            lbl.config(text=text)
            lbl.place(
                x=ox - getattr(self, "offset_x", 0),
                y=oy - getattr(self, "offset_y", 0) + 22,
            )
            lbl.lift()
        except Exception as e:
            print(f"item strip: {e}")

    def ensure_fab_visible(self):
        """托盘/切窗后：仅当浮钮本应对局内可见时再置顶。"""
        if not self._refresh_fab:
            return
        try:
            # withdrawn 表示未进入对局，不要强行弹出
            state = str(self._refresh_fab.state())
            if state == "withdrawn":
                return
            self._refresh_fab.deiconify()
            self._refresh_fab.attributes("-topmost", True)
            self._refresh_fab.lift()
        except Exception:
            pass

    def _setup_window(self):
        self.root.title("ARAM Overlay")
        self.root.overrideredirect(True) # 无边框
        self.root.attributes("-topmost", True) # 置顶
        self.root.config(bg=COLORS["bg"])
        self.root.attributes("-transparentcolor", COLORS["bg"]) # 背景透明
        
        # 鼠标穿透设置 (Windows API)
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            old_style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            # WS_EX_LAYERED | WS_EX_TRANSPARENT
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, old_style | 0x80000 | 0x20)
        except Exception as e:
            print(f"穿透设置警告: {e}")

        # 获取主屏幕坐标，用于相对定位
        with mss.mss() as sct:
            m = sct.monitors[0]
            self.offset_x, self.offset_y = m['left'], m['top']
            self.root.geometry(f"{m['width']}x{m['height']}+{m['left']}+{m['top']}")

    def _setup_labels(self):
        font_style = ("Microsoft YaHei", 14, "bold")
        for key in REGIONS:
            lbl = tk.Label(self.root, text="", font=font_style, bg=COLORS["bg"], justify="left")
            self.labels[key] = lbl

        # 流派推荐 label（中间一张牌上方，品牌薄荷色，叠加在 hex_* label 之上不影响）
        # 默认无文本 = 不可见，避免空字符串也占像素。
        self._combo_label = tk.Label(
            self.root,
            text="",
            font=("Microsoft YaHei", 12, "bold"),
            fg="#7FE3C9",      # 薄荷绿（同宣传网页主色系，深色游戏画面上醒目）
            bg=COLORS["bg"],   # 与背景同色 → 透明感
            justify="center",
            anchor="center",
        )

        # Top strip next to FAB: rune names (mouse-penetrating via main overlay)
        self._rune_strip_label = tk.Label(
            self.root,
            text="",
            font=("Microsoft YaHei", 10, "bold"),
            fg="#f0c75e",
            bg=COLORS["bg"],
            justify="left",
            anchor="w",
        )
        # Items stub hidden until data/aram_items.json exists
        self._item_strip_label = tk.Label(
            self.root,
            text="",
            font=("Microsoft YaHei", 9),
            fg="#a89b7c",
            bg=COLORS["bg"],
            justify="left",
            anchor="w",
        )

    def process_queue(self):
        """主线程轮询：处理来自后台线程的指令"""
        try:
            while True:
                msg = self.queue.get_nowait()
                cmd = msg.get("cmd")
                data = msg.get("data")
                
                if cmd == "UPDATE":
                    self.update_display(data)
                elif cmd == "STATUS":
                    self.show_status(data)
                elif cmd == "CLEAR":
                    self._last_good.clear()
                    self.clear_display()
                    # Do not auto-clear rune strip here — hex CLEAR fires often.
                    # Engine sends RUNE_STRIP "" / ITEM_STRIP "" when hero/match clears.
                elif cmd == "PENDING":
                    self.show_pending()
                elif cmd == "FAB_SHOW":
                    self.set_fab_visible(True)
                elif cmd == "FAB_HIDE":
                    self.set_fab_visible(False)
                elif cmd == "RUNE_STRIP":
                    self.set_rune_strip(data if isinstance(data, str) else (data or ""))
                elif cmd == "ITEM_STRIP":
                    # Reserved: show only when non-empty (no fake item data)
                    self.set_item_strip(data if isinstance(data, str) else (data or ""))
        except queue.Empty:
            pass
        finally:
            self.root.after(50, self.process_queue)

    def clear_display(self):
        if self.hide_timer:
            self.root.after_cancel(self.hide_timer)
            self.hide_timer = None
        for lbl in self.labels.values():
            lbl.place_forget()
        if self._combo_label is not None:
            self._combo_label.place_forget()

    def show_status(self, text):
        self.clear_display()
        lbl = self.labels['hex_2']
        lbl.config(text=text, fg=COLORS["status"])
        lbl.place(relx=0.5, rely=0.5, anchor="center")
        # 状态提示2秒后消失
        self.hide_timer = self.root.after(2000, self.clear_display)

    def _place_hex_labels(self, texts):
        """把文本放到三张牌上方（与 update_display 同一套坐标）。"""
        base_y_abs = REGIONS['hex_1']['top']
        fixed_rel_y = base_y_abs - self.offset_y - 120
        for key, (text, fg) in texts.items():
            lbl = self.labels.get(key)
            if lbl is None or not text:
                continue
            lbl.config(text=text, fg=fg)
            lbl.place(x=REGIONS[key]['left'] - self.offset_x,
                      y=fixed_rel_y, anchor="nw")
            lbl.lift()

    def _place_combo_label(self, info):
        """渲染 / 隐藏「流派推荐」label（中间一张牌正上方，独立金色提示）。

        只在 info 是 dict 且含 text 时显示；其他情况立刻 hide。
        不与 hex_* 区域争夺屏幕空间：放在 hex_2 label 下方 30px，与三张牌
        的识别结果 label 完全错开——视觉上是"额外情报"，不是第四张牌。
        """
        lbl = self._combo_label
        if lbl is None:
            return
        if not info or not info.get("text"):
            try:
                lbl.place_forget()
            except Exception:
                pass
            return
        try:
            base_y_abs = REGIONS['hex_1']['top']
            fixed_rel_y = base_y_abs - self.offset_y - 120
            hex2_left = REGIONS['hex_2']['left']
            hex2_w = REGIONS['hex_2']['width']
            lbl.config(text=info["text"])
            # 横跨 hex_2 区域宽度做换行，最多 ~320 字符宽度
            lbl.place(
                x=hex2_left - self.offset_x + hex2_w // 2,
                y=fixed_rel_y + 36,
                anchor="n",
                width=hex2_w * 2,
            )
            lbl.lift()
        except Exception as e:
            print(f"combo label: {e}")

    def show_pending(self):
        """选项已变化 / 正在重新识别：先清掉旧结果。

        这一步专治「上一轮的名字还挂在新牌上」——旧行为在检测到选项变化后
        要等 debounce 才重新识别，这段时间遮罩上留着的是**上一轮的结果**，
        看起来就像"认错了"。现在改为立刻切成"识别中…"。
        """
        self.clear_display()
        # 选项已换：上轮的好结果缓存作废，避免把旧名字贴到新牌上
        self._last_good.clear()
        # combo label 也归 0；切下一轮识别时上一轮的流派提示也别留
        if self._combo_label is not None:
            try:
                self._combo_label.place_forget()
            except Exception:
                pass
        self._place_hex_labels(
            {k: ("⏳ 识别中…", COLORS["status"]) for k in ("hex_1", "hex_2", "hex_3")}
        )
        self.hide_timer = self.root.after(3000, self.clear_display)

    def update_display(self, results):
        self.clear_display()

        # 玩法提示三合一（优先级：赌狗 > 热门路线 > 流派），逐行合并显示。
        # _gamble / _route / _combo 都是"哨兵键"，不在 self.labels 里，
        # 下面的 hex_* 循环会自动跳过它们。
        hint_lines = []
        for key in ("_gamble", "_route", "_combo"):
            info = results.get(key) if isinstance(results, dict) else None
            if info and info.get("text"):
                hint_lines.append(info["text"])
        self._place_combo_label({"text": "\n".join(hint_lines)} if hint_lines else None)

        texts = {}
        for key, info in results.items():
            if key not in self.labels:  # _combo 不在 labels 里，自动跳过
                continue
            text = info.get("text")
            if not text:
                continue
            if info.get("error"):
                # 本轮抓帧失败：同一组选项若之前认出来过，保留上次的好结果，
                # 不把已给出的推荐降级成「未识别」（选项变化会走 PENDING 清缓存）。
                cached = self._last_good.get(key)
                if cached:
                    texts[key] = cached
                    continue
                fg = COLORS["error"]
            elif info.get("highlight"):
                fg = COLORS["best"]
            else:
                fg = COLORS["normal"]
            # 最佳推荐前面加星，一眼看出该选哪张
            if info.get("highlight") and not info.get("error"):
                text = "⭐ " + text
            if not info.get("error"):
                self._last_good[key] = (text, fg)
            texts[key] = (text, fg)

        self._place_hex_labels(texts)

        # 结果显示5秒后消失
        self.hide_timer = self.root.after(5000, self.clear_display)

# ================= 4. 控制逻辑 (Controller) =================

class InputController(threading.Thread):
    def __init__(self, app_queue, data_manager, analyzer, lcu_connector=None):
        super().__init__(daemon=True)
        self.queue = app_queue
        self.dm = data_manager
        self.analyzer = analyzer
        self.lcu = lcu_connector
        self.current_hero = None
        self._last_phase = None
        self._last_f6 = 0
        self._last_f7 = 0
        self._last_f8 = 0

    def run(self):
        while True:
            self.select_hero_phase()
            self.listening_phase()

    def flush_input(self):
        """强制清空标准输入缓冲区"""
        while msvcrt.kbhit():
            msvcrt.getch()

    def _validate_hero(self, name):
        """验证英雄名是否在数据库中，尝试模糊映射"""
        return self.dm.validate_hero(name)

    def _try_auto_detect(self):
        """使用 LCU 统一接口自动获取英雄，返回 (英雄中文名|None, 来源)"""
        if not self.lcu:
            return None, ""
        hero, source = self.lcu.get_champion_auto()
        if hero:
            validated = self._validate_hero(hero)
            if validated:
                return validated, source
        return None, source

    # ==========================================
    # 阶段1: 选择英雄 (自动轮询 + 手动备用)
    # ==========================================

    def select_hero_phase(self):
        self.queue.put({"cmd": "CLEAR"})
        self.show_console_window()

        time.sleep(0.1)
        os.system('cls')
        self.flush_input()

        print("=== ARAM Hextech Helper ===")
        print("    [控制台] F6=分析 | F7=刷新英雄 | F8=手动输入\n"
          "    (GUI 请用界面「刷新识别 / 识别英雄 / 重置」)\n")

        # ====== 尝试自动检测 (轮询最多30秒) ======
        if self.lcu:
            print("[Auto] 正在连接英雄联盟客户端...")

            for attempt in range(15):  # 每2秒检测，共30秒
                hero, source = self._try_auto_detect()
                if hero:
                    self.current_hero = hero
                    print(f"\n>>> 自动识别到英雄: [{hero}] (数据源: {source})")
                    print(f">>> F6=分析 | F7=刷新 | F8=手动")
                    self.queue.put({"cmd": "STATUS", "data": f"当前: {hero}\n按 F6 分析 | F7 刷新"})
                    self.hide_console_window()
                    return

                # F8 跳过自动检测并手动输入
                if keyboard.is_pressed('f8'):
                    print("\n[F8] 切换至手动输入...")
                    time.sleep(0.5)
                    break

                dots = "." * ((attempt % 3) + 1)
                print(f"\r[Auto] 等待选取英雄{dots}   ", end="", flush=True)
                time.sleep(2)
            else:
                # 30秒后没搜到，不锁在死循环里，直接进入监听模式
                print("\n[Auto] 暂未自动识别到英雄。将切入后台继续运行。")
                print(">>> 随时按 [F7] 重新获取，或按 [F8] 呼出控制台手动输入。\n")
                self.current_hero = None
                self.queue.put({"cmd": "STATUS", "data": "暂无英雄\n按 F7 自动获取本局英雄"})
                self.hide_console_window()
                return

        # ====== 手动输入 (仅在按下 F8 时，或 LCU 完全异常时进入) ======
        print(">>> 请输入英雄名称 (拼音/中文):")

        while True:
            try:
                self.flush_input()
                raw = input("Input: ").strip()
            except EOFError:
                continue
            if not raw:
                continue

            matches, is_exact = self.dm.search_hero(raw)
            selected_name = None

            if not matches:
                print("❌ 未找到，请重试")
                continue

            if len(matches) > 1:
                print(f"🤔 发现多个匹配项，请选择:")
                for idx, name in enumerate(matches):
                    print(f"   {idx + 1}. {name}")
                print(">>> 请输入序号:")
                self.flush_input()
                try:
                    idx = int(input("Select: ").strip()) - 1
                    if 0 <= idx < len(matches):
                        selected_name = matches[idx]
                    else:
                        print("无效选项，请重试")
                        continue
                except ValueError:
                    print("无效输入，请重试")
                    continue
            else:
                candidate = matches[0]
                if is_exact:
                    selected_name = candidate
                else:
                    print(f"   猜你是: {candidate}? (Enter确认 / n重输)")
                    self.flush_input()
                    if input().strip().lower() == 'n':
                        continue
                    selected_name = candidate

            if selected_name:
                validated = self._validate_hero(selected_name)
                if not validated:
                    print(f"数据库暂无【{selected_name}】的数据")
                    continue
                self.current_hero = validated
                print(f">>> 已锁定: {validated}")
                print(f">>> F6=分析 | F7=刷新 | F8=手动")
                self.queue.put({"cmd": "STATUS", "data": f"当前: {validated}\n按 F6 分析 | F7 刷新"})
                self.hide_console_window()
                break

    # ==========================================
    # 阶段2: 监听热键
    # ==========================================

    def _forget_match_hero(self, reason: str = ""):
        """局间清理 sticky 英雄，避免上场名残留到下一局。"""
        had = self.current_hero
        if not had:
            return
        self.current_hero = None
        msg = f"已清除上场英雄 [{had}]，等待本局识别"
        if reason:
            msg = f"{reason} — {msg}"
        print(f">>> {msg}")
        self.queue.put({"cmd": "STATUS", "data": "等待识别本局英雄\n按 F7 刷新"})

    def listening_phase(self):
        self.flush_input()
        print(f"[监听中...] 当前英雄: {self.current_hero} | F6分析 / F7刷新 / F8手动")

        while True:
            now = time.time()

            # 局间阶段变化：丢掉上场英雄，下一局重新识别
            try:
                if self.lcu and self.lcu.is_connected():
                    phase = self.lcu.get_gameflow_phase()
                    if phase != self._last_phase:
                        prev = self._last_phase
                        between = (
                            "EndOfGame", "WaitingForStats", "Lobby", "None",
                            "Matchmaking", "ReadyCheck",
                        )
                        if phase in between:
                            if prev in (
                                "InProgress", "GameStart", "ChampSelect",
                                "EndOfGame", "WaitingForStats",
                            ) or self.current_hero:
                                self._forget_match_hero("局间清理")
                        if phase == "ChampSelect":
                            self._forget_match_hero("进入选人")
                            if not self.current_hero:
                                self.queue.put({
                                    "cmd": "STATUS",
                                    "data": "等待识别本局英雄\n按 F7 刷新",
                                })
                        self._last_phase = phase
            except Exception:
                pass

            if keyboard.is_pressed('f6') and now - self._last_f6 > 1.0:
                self._last_f6 = now
                # 硬门禁：未进入对局（InProgress + Live）不 OCR
                in_live = False
                try:
                    if self.lcu and hasattr(self.lcu, "is_in_live_game"):
                        in_live = bool(self.lcu.is_in_live_game())
                    elif self.lcu:
                        in_live = (
                            self.lcu.get_gameflow_phase() == "InProgress"
                            and self.lcu.get_live_player_state() is not None
                        )
                except Exception:
                    in_live = False
                if not in_live:
                    self.queue.put({
                        "cmd": "STATUS",
                        "data": "⚠ 尚未进入对局\n进入对局且右上角读秒出现后再识别海克斯",
                    })
                    continue
                if not self.current_hero:
                    self.queue.put({"cmd": "STATUS", "data": "⚠ 尚未锁定英雄\n请按 F7 自动获取或 F8 手动输入"})
                    continue
                
                self.queue.put({"cmd": "STATUS", "data": f"🔎 正在分析 [{self.current_hero}]..."})
                results = self.analyzer.analyze(self.current_hero)
                self.queue.put({"cmd": "UPDATE", "data": results})

            if keyboard.is_pressed('f7') and now - self._last_f7 > 1.0:
                self._last_f7 = now
                # F7: 全阶段刷新英雄 (ChampSelect / InProgress / LiveAPI)
                self.queue.put({"cmd": "STATUS", "data": "刷新英雄..."})
                hero, source = self._try_auto_detect()
                # 局间勿把 GameFlow/Live 上场残留当成当前英雄
                phase = self._last_phase
                if (
                    phase in (
                        "EndOfGame", "WaitingForStats", "Lobby", "None",
                        "Matchmaking", "ReadyCheck", "ChampSelect",
                    )
                    and source in ("GameFlow", "Live API")
                ):
                    hero, source = None, source
                if hero and hero != self.current_hero:
                    old = self.current_hero
                    self.current_hero = hero
                    print(f">>> 英雄已切换 ({source}): {old} -> {hero}")
                    self.queue.put({"cmd": "STATUS", "data": f"已切换: {hero}\n按 F6 分析"})
                elif hero:
                    self.queue.put({"cmd": "STATUS", "data": f"当前英雄: {hero}\n按 F6 分析"})
                else:
                    label = self.current_hero or "等待识别本局英雄"
                    self.queue.put({"cmd": "STATUS", "data": f"当前: {label}\n按 F7 刷新"})


            if keyboard.is_pressed('f8') and now - self._last_f8 > 1.0:
                self._last_f8 = now
                time.sleep(0.5)
                return  # 退出监听，回到 select_hero_phase

            time.sleep(0.05)


    @staticmethod
    def show_console_window():
        try:
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    @staticmethod
    def hide_console_window():
        try:
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:
            pass

# ================= 5. 主入口 =================

def main():
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)
    
    # 强制设置工作目录为应用根目录 (兼容打包)
    os.chdir(BASE_DIR)
    os.system('title ARAM 海克斯助手')
    os.system('chcp 65001 >nul')
    print(f"Working Directory: {BASE_DIR}")

    # 1. 初始化核心数据与逻辑
    dm = DataManager()
    
    if not dm.hero_data:
        print("❌ 警告: 未加载到任何英雄数据，请检查CSV文件。")
        input("按任意键退出...")
        return

    analyzer = GameAnalyzer(dm)
    
    # 2. 初始化 LCU 客户端连接器
    champions_json = os.path.join(dm.data_dir, 'champions.json')
    lcu = LCUConnector(champions_json)
    
    # 3. 初始化 UI 与 通信队列
    root = tk.Tk()
    msg_queue = queue.Queue()
    app = OverlayApp(root, msg_queue)
    
    # 4. 启动后台控制线程
    controller = InputController(msg_queue, dm, analyzer, lcu_connector=lcu)
    controller.start()

    def _overlay_manual_refresh():
        def _run():
            if not controller.current_hero:
                msg_queue.put({"cmd": "STATUS", "data": "⚠ 尚未锁定英雄\n按 F7 / F8"})
                return
            msg_queue.put({"cmd": "STATUS", "data": f"🔎 分析 [{controller.current_hero}]..."})
            results = analyzer.analyze(controller.current_hero)
            msg_queue.put({"cmd": "UPDATE", "data": results})
        threading.Thread(target=_run, daemon=True).start()

    app.set_manual_refresh(_overlay_manual_refresh)
    
    # 4. 进入 UI 主循环
    print("程序已启动...")
    try:
        root.mainloop()
    except KeyboardInterrupt:
        os._exit(0)

if __name__ == "__main__":
    main()