"""
xiaobai助手 · 极简版 — 匹配自动化 + 备战席抢英雄 + WeGame 启动

布局（严格按手绘稿，自上而下）：
  ┌ 顶栏 ── 品牌 / 连接状态
  ├ 英雄墙 ─ 备战席 10 格，两排 × 5 列，头像 100% 铺满（无文字/无倒计时）
  ├ 按钮排 ─ 启动自动化 · 开始 · 自动接受(1s/3s/5s) + 状态提示
  └ WeGame ─ 地址 + 启动（最底部，软件到此为止，其余依靠客户端）

交互与 LeagueAkari 对齐：
  备战席头像点了就锁定目标，到点自动换，不在界面上提示秒数。

依赖：
  scripts/config.py         配置持久化
  scripts/lcu_connector.py  LCU 本地 API（含二进制资源）
  scripts/matchmaking.py    自动接受/准备/开始
  scripts/bench_pick.py     备战席提前选取
  scripts/champ_icons.py    英雄头像（LCU 本地资源优先）
  scripts/wegame.py         WeGame 定位与启动
"""
import tkinter as tk
from tkinter import messagebox, filedialog
import customtkinter as ctk
import threading
import queue
import os
import sys
import io
import time
import datetime
import traceback

from scripts.config import (
    BASE_DIR, DATA_DIR, DEFAULT_SETTINGS, AUGMENTS_FILE,
    load_settings, save_settings, normalize_delay,
)
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

from PIL import Image, ImageDraw, ImageFont
from scripts.lcu_connector import LCUConnector
from scripts.matchmaking import MatchmakingService
from scripts.bench_pick import BenchPickService
from scripts.autoflow import AutoFlowService
from scripts.champ_icons import ChampIconCache, ICON_PX
from scripts.augments import AugmentLibrary
from scripts import wegame
from scripts import tray as tray_mod
from scripts import winpos

CHAMPIONS_FILE = os.path.join(DATA_DIR, "champions.json")
_IS_WIN = os.name == "nt"


# ============================================================================
# 自动显隐 —— 照搬 LeagueAkari 小窗 autoShow 的三态机（out/main/main.js）
#
#   LA 原版：
#     if (!settings.autoShow || !state.ready) return 'ignore'
#     switch (gameflow.phase) {
#       case 'ChampSelect': if (isSpectating) return 'ignore'
#       case 'Lobby': case 'Matchmaking': case 'ReadyCheck': return 'show'
#     }
#     return 'hide'                       // 其余一律隐藏
#   reaction: e !== 'ignore' && (e === 'show' ? showOrRestore() : hide())
#
#   我们是「主窗口」而 LA 是「辅助小窗」，小窗消失无所谓、主窗消失等于软件没了，
#   所以只加一条 LA 没有的闸门：没连上客户端（phase 为空）→ ignore，保持现状。
# ============================================================================

AUTO_SHOW_PHASES = ("Lobby", "Matchmaking", "ReadyCheck", "ChampSelect")

# 隐藏：GameStart / InProgress / WaitingForStats / PreEndOfGame / EndOfGame / Reconnect


def auto_visibility(phase, enabled=True, connected=True):
    """返回 'ignore' | 'show' | 'hide'。"""
    if not enabled:
        return "ignore"
    if not connected or not phase:
        return "ignore"      # 主窗口专属闸门：客户端没开就别乱动
    return "show" if phase in AUTO_SHOW_PHASES else "hide"


# ============================================================================
# 设计系统 — 浅色 · Apple 审美
#   底 #F5F5F7 / 卡片纯白 / 强调 Apple 蓝 #0071E3 / 文字 #1D1D1F
#   层级靠「极淡分隔线 + 留白」而非重边框；圆角大、字重克制。
# ============================================================================

class Design:
    # 背景层级
    BG          = "#F5F5F7"     # Apple 经典浅灰底
    BG_BAR      = "#FFFFFF"     # 顶栏 / 工具条
    BG_CARD     = "#FFFFFF"
    BG_INPUT    = "#FFFFFF"
    BG_SEG      = "#F2F2F7"     # 分段控件槽
    BG_SEG_SEL  = "#FFFFFF"     # 分段控件选中块
    BG_HOVER    = "#E8E8ED"
    BG_WASH     = "#FAFAFC"     # 极淡底纹

    # 文字
    TEXT        = "#1D1D1F"     # Apple 近黑
    TEXT_DIM    = "#6E6E73"     # 次要
    TEXT_MUTE   = "#A1A1A6"     # 弱化
    TEXT_INV    = "#FFFFFF"

    # 强调 / 语义
    ACCENT      = "#0071E3"     # Apple 蓝
    ACCENT_HVR  = "#0077ED"
    ACCENT_DOWN = "#0062C4"
    ACCENT_DIM  = "#E8F2FE"     # 浅蓝底
    ACCENT_TEXT = "#FFFFFF"

    SUCCESS     = "#248A3D"
    SUCCESS_BG  = "#E4F6E9"
    WARNING     = "#C93400"
    WARNING_BG  = "#FFF0E6"
    ERROR       = "#D70015"
    ERROR_BG    = "#FFEBEA"

    # 线
    BORDER      = "#E5E5EA"
    LINE        = "#D2D2D7"

    S_XXS = 2; S_XS = 4; S_SM = 8; S_MD = 12; S_LG = 16; S_XL = 20; S_XXL = 26
    R_XS = 6; R_SM = 10; R_MD = 12; R_LG = 16; R_TILE = 16; R_PILL = 999

    F_FAMILY = "Microsoft YaHei UI"
    F_MONO   = "Cascadia Mono"

    TILE_PX  = 108              # 头像显示边长（铺满瓦片）
    TILE_GAP = 14
    WALL_COLS = 5               # 手绘稿：两排 × 5 列，共 10 格
    WALL_ROWS = 2
    ICON_CACHE_PX = ICON_PX     # 解码/缓存尺寸（128，高 DPI 也不糊）

    BTN_H  = 54                 # 大按钮（用户反馈：原来的太小）
    BTN_H2 = 36                 # 次级按钮

    # ---- 整体缩小 1/3 ----
    # 布局（间距 / 圆角 / 瓦片 / 按钮高 / 窗口尺寸）一律 × SCALE = 2/3。
    # 字号单独用 FONT_SCALE 并设 10px 下限：中文字号线性缩到 2/3 只剩 7~8px，
    # 在 Windows 上已经不可读，所以字号收得比布局少一点、并兜一个可读下限。
    SCALE = 0.667
    FONT_SCALE = 0.82
    FONT_MIN = 10

    @classmethod
    def sc(cls, value):
        """尺寸缩放：布局类硬编码数值统一走这里。"""
        return max(1, int(round(value * cls.SCALE)))

    @classmethod
    def font(cls, key):
        scale = {
            'brand':  (cls.F_FAMILY, 22, 'bold'),
            'h2':     (cls.F_FAMILY, 15, 'bold'),
            'body':   (cls.F_FAMILY, 14, 'normal'),
            'bodyb':  (cls.F_FAMILY, 14, 'bold'),
            'btn':    (cls.F_FAMILY, 16, 'bold'),
            'small':  (cls.F_FAMILY, 12, 'normal'),
            'micro':  (cls.F_FAMILY, 11, 'normal'),
        }
        fam, size, weight = scale.get(key, scale['body'])
        return (fam, max(cls.FONT_MIN, int(round(size * cls.FONT_SCALE))), weight)


def _apply_design_scale(cls):
    """把 Design 里的数值 token 统一按 SCALE 缩小（R_PILL 是「胶囊」哨兵值，不动）。"""
    if abs(cls.SCALE - 1.0) < 1e-6:
        return
    for name in ("S_XXS", "S_XS", "S_SM", "S_MD", "S_LG", "S_XL", "S_XXL",
                 "R_XS", "R_SM", "R_MD", "R_LG", "R_TILE",
                 "TILE_PX", "TILE_GAP", "BTN_H", "BTN_H2"):
        setattr(cls, name, cls.sc(getattr(cls, name)))


_apply_design_scale(Design)

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")


# ============================================================================
# 图像：头像 100% 铺满 + 圆角 + 状态标记
#   只用 convert / point / merge / paste —— 避开 Image.blend、ImageChops、
#   alpha_composite。Pillow 的混合类 C 调用在跨线程并发时会死锁。
#   瓦片上不放任何文字（对齐 LeagueAkari：点头像即抢，不提示秒数）。
# ============================================================================

def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _load_font(size, bold=False):
    root = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    names = ("msyhbd.ttc", "msyh.ttc") if bold else ("msyh.ttc", "msyhbd.ttc")
    for name in names + ("seguisb.ttf", "arialbd.ttf", "arial.ttf"):
        p = os.path.join(root, name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _round(img, radius=Design.R_TILE):
    """圆角遮罩（4 倍超采样抗锯齿）。"""
    px = img.size[0]
    ss = 4
    mask = Image.new("L", (px * ss, px * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, px * ss - 1, px * ss - 1), radius=radius * ss, fill=255)
    mask = mask.resize((px, px), Image.LANCZOS)
    out = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _dim(img, strength=0.42):
    """锁定态（未到解锁时间）：去色 + 提亮，浅色主题下的「不可用」观感。
    纯 LUT 路线，不用 blend。"""
    img = img.convert("RGBA")
    _r, _g, _b, a = img.split()
    gray = img.convert("L")
    lut = [min(255, int(255 * strength + v * (1 - strength))) for v in range(256)]
    gray = gray.point(lut)
    return Image.merge("RGBA", (gray, gray, gray, a))


def _ring(img, color=Design.ACCENT, width=4, radius=Design.R_TILE):
    """目标态：白圈垫底 + Apple 蓝环，任何头像上都醒目。"""
    img = img.convert("RGBA")
    px = img.size[0]
    ss = 4
    layer = Image.new("RGBA", (px * ss, px * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    box = (1, 1, px * ss - 2, px * ss - 2)
    d.rounded_rectangle(box, radius=radius * ss,
                        outline=(255, 255, 255, 255), width=(width + 3) * ss)
    d.rounded_rectangle(box, radius=radius * ss,
                        outline=_hex_rgb(color) + (255,), width=width * ss)
    layer = layer.resize((px, px), Image.LANCZOS)
    out = img.copy()
    out.paste(layer, (0, 0), layer)
    return out


def _short(text, limit=44):
    t = (text or "").strip()
    return t if len(t) <= limit else t[:limit] + "…"


def _placeholder_icon(name, px=Design.TILE_PX):
    """没拿到头像时的占位：浅灰底 + 名字首字。"""
    img = Image.new("RGBA", (px, px), _hex_rgb(Design.BG_SEG) + (255,))
    d = ImageDraw.Draw(img)
    txt = (name or "?")[0]
    try:
        d.text((px / 2, px / 2), txt, font=_load_font(int(px * 0.40), bold=True),
               fill=_hex_rgb(Design.TEXT_MUTE) + (255,), anchor="mm")
    except Exception:
        pass
    return img


def _tile_image(base, name, state):
    """合成一张瓦片图：满铺头像 → (锁定)去色提亮 → 圆角 → (目标)蓝环。"""
    px = Design.TILE_PX
    img = base.convert("RGBA").resize((px, px), Image.LANCZOS)
    if state == "locked":
        img = _dim(img)
    img = _round(img)
    if state == "target":
        img = _ring(img, Design.ACCENT, 4)
    return img


# ============================================================================
# 基础组件
# ============================================================================

class LogRedirector(io.TextIOBase):
    def __init__(self, log_queue, original_stream=None):
        super().__init__()
        self.log_queue = log_queue
        self.original = original_stream

    def write(self, text):
        if text and text.strip():
            self.log_queue.put(text.rstrip())
        if self.original:
            try:
                self.original.write(text)
                self.original.flush()
            except Exception:
                pass
        return len(text) if text else 0

    def flush(self):
        if self.original:
            try:
                self.original.flush()
            except Exception:
                pass


class BenchTile(ctk.CTkFrame):
    """备战席英雄瓦片：头像 100% 铺满整块，无文字。

    state: free（正常） / locked（去色提亮=未解锁） / target（Apple 蓝环=抢购目标）
    """

    def __init__(self, parent, item, icons, on_click):
        super().__init__(parent, fg_color="transparent",
                         width=Design.TILE_PX, height=Design.TILE_PX,
                         corner_radius=0)
        self.pack_propagate(False)
        self.grid_propagate(False)

        self.cid = int(item["championId"])
        self.name = item["name"] or f"英雄{self.cid}"
        self._icons = icons
        self._on_click = on_click
        self._state = None
        self._img_ref = None

        self.pic = ctk.CTkLabel(self, text="", fg_color="transparent",
                                width=Design.TILE_PX, height=Design.TILE_PX,
                                corner_radius=0)
        self.pic.pack(fill=tk.BOTH, expand=True)

        targets = [self, self.pic]
        for attr in ("_canvas", "_text_label"):
            w = getattr(self.pic, attr, None)
            if w is not None:
                targets.append(w)
        for w in targets:
            try:
                w.bind("<Button-1>", lambda e: self._on_click(self.cid, self.name))
                w.configure(cursor="hand2")
            except Exception:
                pass

        self.set_state("free", force=True)

    # ---------- 状态 ----------

    def set_state(self, state, force=False):
        changed = (state != self._state) or force
        self._state = state
        if changed:
            self._refresh()

    def on_icon_loaded(self):
        self._refresh()

    def _refresh(self):
        base = None
        if self._icons is not None:
            base = self._icons.get(self.cid)
        if base is None:
            base = _placeholder_icon(self.name)
        img = _tile_image(base, self.name, self._state)
        try:
            self._img_ref = ctk.CTkImage(light_image=img, dark_image=img,
                                         size=(Design.TILE_PX, Design.TILE_PX))
            self.pic.configure(image=self._img_ref, text="")
        except Exception:
            pass


# ============================================================================
# 引擎线程
# ============================================================================

class EngineWorker(threading.Thread):
    """随「启动自动化」启停；LCU 断线自动重连，两个服务各自轮询。"""

    def __init__(self, lcu, settings, gui_queue):
        super().__init__(daemon=True)
        self.lcu = lcu
        self.gui_queue = gui_queue
        self.running = True

        self.matchmaking = MatchmakingService(
            lcu,
            flags={
                "auto_accept": bool(settings.get("auto_accept", True)),
                "auto_ready": bool(settings.get("auto_ready", True)),
                "auto_accept_delay": normalize_delay(settings.get("auto_accept_delay", 5)),
            },
            on_event=lambda m: gui_queue.put({"event": "log", "text": m}),
            on_countdown=lambda sec, label: gui_queue.put({
                "event": "countdown", "seconds": sec, "label": label}),
        )
        self.bench = BenchPickService(
            lcu,
            on_event=lambda m: gui_queue.put({"event": "log", "text": m}),
            on_bench=lambda data: gui_queue.put({"event": "bench", "data": data}),
        )
        try:
            self.bench.set_lead(settings.get("bench_lead", 2))
        except Exception:
            pass

        # 自动回到房间 / 自动重连 / 自动接受邀请（开关实时从 settings 读）
        self.autoflow = AutoFlowService(
            lcu, get_flags=lambda: settings,
            on_event=lambda m: gui_queue.put({"event": "log", "text": m}))

    def stop(self):
        self.running = False
        self.matchmaking.stop()
        self.bench.stop()
        try:
            self.autoflow.stop()
        except Exception:
            pass

    def run(self):
        self.matchmaking.start()
        self.bench.start()
        self.autoflow.start()
        while self.running:
            try:
                connected = self.lcu.is_connected() or bool(self.lcu.connect())
                phase = self.lcu.get_gameflow_phase() if connected else None
                self.gui_queue.put({"event": "phase",
                                    "phase": phase, "connected": connected})
            except Exception as e:
                self.gui_queue.put({"event": "log", "text": f"[引擎] {e}"})
            for _ in range(10):
                if not self.running:
                    break
                time.sleep(0.1)
        self.matchmaking.stop()
        self.bench.stop()
        try:
            self.autoflow.stop()
        except Exception:
            pass


PHASE_LABEL = {
    None: "未连接客户端", "None": "未连接客户端",
    "Lobby": "大厅", "Matchmaking": "匹配中", "ReadyCheck": "等待接受",
    "ChampSelect": "选人阶段", "GameStart": "游戏加载中",
    "InProgress": "游戏中", "WaitingForStats": "结算中", "EndOfGame": "结算",
}


# ============================================================================
# 主窗口 — 按手绘稿排布：头像墙 → 按钮 → WeGame（最底）
# ============================================================================

class CTkLauncherApp:
    def __init__(self):
        self.root = ctk.CTk()
        self.root.title("xiaobai助手 · 极简版")
        self._fit_window_to_screen()
        self.root.configure(fg_color=Design.BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_window_icon()

        self.settings = load_settings()
        self.lcu = LCUConnector(CHAMPIONS_FILE)
        self.engine = None
        self.engine_running = False

        # 海克斯图鉴（官方离线数据）
        try:
            self.augments = AugmentLibrary(AUGMENTS_FILE)
        except Exception:
            self.augments = None

        # 托盘（ctypes 自建，失败则降级）
        self.tray = None
        self._settings_win = None
        self._aug_win = None
        self._aug_job = None
        self._quitting = False

        self.gui_queue = queue.Queue()
        self.log_queue = queue.Queue()

        self._phase = None
        self._connected = False
        self._bench_items = []
        self._bench_available = False   # 服务线程给的口径：session.benchEnabled
        self._bench_ts = 0.0
        self._bench_note = ""
        self._tiles = {}            # cid -> BenchTile
        self._rendered_ids = ()
        self._rendering = False     # 防重入（update_idletasks 会派发事件）

        # 状态条（一行提示，替代原控制台）
        self._status_text = ""
        self._status_color = Design.TEXT_DIM
        self._countdown_active = False

        # 自动显隐（LA 三态机的当前指令，去重用）
        self._auto_vis = "ignore"
        # 用户自己收进托盘后，就不再自动弹出来抢戏
        self._user_hidden = False

        try:
            self.icons = ChampIconCache(
                self.lcu, os.path.join(DATA_DIR, "icon_cache"),
                on_loaded=lambda cid: self.gui_queue.put(
                    {"event": "icon", "cid": cid}))
        except Exception:
            self.icons = None

        sys.stdout = LogRedirector(self.log_queue, sys.__stdout__)
        sys.stderr = LogRedirector(self.log_queue, sys.__stderr__)

        self._build_ui()
        self._sync_autostart_state()
        self._render_bench(force=True)
        self.root.after(80, self._poll_queues)
        self.root.after(150, self._tick_tiles)

    def _sync_autostart_state(self):
        """以注册表实际状态为准，避免用户在别处删掉自启后界面还显示开着。"""
        try:
            actual = tray_mod.is_autostart_enabled()
            if actual != bool(self.settings.get("autostart", False)):
                self.settings["autostart"] = actual
                save_settings(self.settings)
        except Exception:
            pass

    # ---------- 窗体 ----------

    def _fit_window_to_screen(self):
        try:
            self.root.update_idletasks()
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            scaling = float(self.root.tk.call('tk', 'scaling')) or 1.0
            logical_w = int(sw / scaling) if scaling > 1.2 else sw
            logical_h = int(sh / scaling) if scaling > 1.2 else sh
            w = min(Design.sc(840), max(Design.sc(700), logical_w - Design.sc(140)))
            h = min(Design.sc(566), max(Design.sc(520), logical_h - Design.sc(320)))
            x = max(0, (logical_w - w) // 2)
            y = max(16, (logical_h - h) // 4)
            self.root.geometry(f"{w}x{h}+{x}+{y}")
            self.root.minsize(Design.sc(680), Design.sc(520))
        except Exception:
            self.root.geometry(f"{Design.sc(800)}x{Design.sc(580)}")
            self.root.minsize(Design.sc(680), Design.sc(520))

    def _set_window_icon(self):
        try:
            ico = os.path.join(BASE_DIR, "assets", "icon.ico")
            if os.path.exists(ico) and _IS_WIN:
                self.root.after(200, lambda: self.root.iconbitmap(ico))
        except Exception:
            pass

    def _hairline(self, parent, pady=(0, 0)):
        ctk.CTkFrame(parent, fg_color=Design.BORDER, height=1,
                     corner_radius=0).pack(fill=tk.X, pady=pady)

    # ---------- 组装（手绘稿顺序） ----------

    def _build_ui(self):
        self._build_topbar(self.root)
        self._build_wall(self.root)       # ① 十格头像（两排×5）
        self._build_actions(self.root)    # ② 启动 / 开始 / 接受(1.3.5)
        self._build_wegame(self.root)     # ③ WeGame 地址 + 启动（最底部）

    def _build_topbar(self, parent):
        bar = ctk.CTkFrame(parent, fg_color=Design.BG_BAR, corner_radius=0)
        bar.pack(fill=tk.X)

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.pack(side=tk.LEFT, padx=(Design.S_XL, 0), pady=Design.S_MD)
        ctk.CTkLabel(left, text="xiaobai", font=Design.font('brand'),
                     text_color=Design.ACCENT).pack(side=tk.LEFT)
        ctk.CTkLabel(left, text="助手", font=Design.font('bodyb'),
                     text_color=Design.TEXT).pack(side=tk.LEFT, padx=(6, 0), pady=(4, 0))
        ctk.CTkLabel(left, text="· 极地大乱斗 / 海克斯大乱斗",
                     font=Design.font('small'),
                     text_color=Design.TEXT_MUTE).pack(side=tk.LEFT, padx=(8, 0),
                                                       pady=(4, 0))

        pill = ctk.CTkFrame(bar, fg_color=Design.BG_SEG, corner_radius=Design.R_PILL,
                            border_width=1, border_color=Design.BORDER)
        pill.pack(side=tk.RIGHT, padx=(0, Design.S_XL), pady=Design.S_MD)
        self.conn_dot = ctk.CTkLabel(pill, text="● 未连接", font=Design.font('small'),
                                     text_color=Design.TEXT_DIM)
        self.conn_dot.pack(padx=Design.sc(13), pady=Design.sc(6))

        ctk.CTkButton(bar, text="⚙", font=Design.font('body'),
                      width=Design.sc(34), height=Design.sc(30),
                      corner_radius=Design.R_PILL,
                      fg_color=Design.BG_SEG, hover_color=Design.BG_HOVER,
                      text_color=Design.TEXT_DIM,
                      command=self._open_settings).pack(
            side=tk.RIGHT, padx=(0, Design.S_SM), pady=Design.S_MD)

        ctk.CTkButton(bar, text="海克斯图鉴", font=Design.font('small'),
                      width=Design.sc(92), height=Design.sc(30), corner_radius=Design.R_PILL,
                      fg_color=Design.BG_SEG, hover_color=Design.BG_HOVER,
                      text_color=Design.TEXT_DIM,
                      command=self._open_augments).pack(
            side=tk.RIGHT, padx=(0, Design.S_SM), pady=Design.S_MD)

        self._hairline(parent)

    # ---------- ① 英雄墙：备战席 10 格，头像 100% ----------

    def _build_wall(self, parent):
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(fill=tk.BOTH, expand=True, padx=Design.S_XL,
                  pady=(Design.S_LG, Design.S_MD))

        head = ctk.CTkFrame(wrap, fg_color="transparent")
        head.pack(fill=tk.X)

        ctk.CTkLabel(head, text="备战席", font=Design.font('h2'),
                     text_color=Design.TEXT).pack(side=tk.LEFT)
        self.bench_status_var = tk.StringVar(value="")
        ctk.CTkLabel(head, textvariable=self.bench_status_var,
                     font=Design.font('small'),
                     text_color=Design.TEXT_MUTE).pack(side=tk.LEFT, padx=(8, 0),
                                                       pady=(3, 0))
        ctk.CTkLabel(head, text="彩色=可换 · 灰色=换不了（未拥有且不在周免）",
                     font=Design.font('small'),
                     text_color=Design.TEXT_MUTE).pack(side=tk.RIGHT, pady=(3, 0))

        self.wall = ctk.CTkFrame(wrap, fg_color="transparent")
        self.wall.pack(fill=tk.BOTH, expand=True, pady=(Design.S_MD, 0))

    # ---------- ② 按钮排：启动自动化 / 开始 / 接受(1s 3s 5s) ----------

    def _build_actions(self, parent):
        bar = ctk.CTkFrame(parent, fg_color=Design.BG, corner_radius=0)
        bar.pack(fill=tk.X)
        row = ctk.CTkFrame(bar, fg_color="transparent")
        row.pack(fill=tk.X, padx=Design.S_XL, pady=Design.S_MD)

        self.btn_engine = ctk.CTkButton(
            row, text="▶   启动自动化", font=Design.font('btn'),
            width=Design.sc(178), height=Design.BTN_H, corner_radius=Design.R_MD,
            fg_color=Design.ACCENT, hover_color=Design.ACCENT_HVR,
            text_color=Design.ACCENT_TEXT, command=self._toggle_engine)
        self.btn_engine.pack(side=tk.LEFT)

        self.btn_start = ctk.CTkButton(
            row, text="开始", font=Design.font('btn'),
            width=Design.sc(140), height=Design.BTN_H, corner_radius=Design.R_MD,
            fg_color=Design.BG_SEG, hover_color=Design.ACCENT_DIM,
            border_width=1, border_color=Design.BORDER,
            text_color=Design.TEXT_DIM, command=self._on_start_toggle)
        self.btn_start.pack(side=tk.LEFT, padx=(Design.S_SM, 0))

        self.btn_accept = ctk.CTkButton(
            row, text="接受", font=Design.font('btn'),
            width=Design.sc(140), height=Design.BTN_H, corner_radius=Design.R_MD,
            fg_color=Design.BG_SEG, hover_color=Design.ACCENT_DIM,
            border_width=1, border_color=Design.BORDER,
            text_color=Design.TEXT_DIM, command=self._on_accept_toggle)
        self.btn_accept.pack(side=tk.LEFT, padx=(Design.S_SM, 0))

        self._delay_value = normalize_delay(self.settings.get("auto_accept_delay", 3))
        self._delay_seg = ctk.CTkSegmentedButton(
            row, values=["1s", "3s", "5s"], command=self._on_delay_select,
            fg_color=Design.BG_SEG, selected_color=Design.ACCENT,
            selected_hover_color=Design.ACCENT_HVR, unselected_color=Design.BG_SEG,
            unselected_hover_color=Design.BG_HOVER,
            text_color=Design.TEXT, font=Design.font('small'),
            height=Design.BTN_H, corner_radius=Design.R_MD)
        self._delay_seg.set(f"{self._delay_value}s")
        self._delay_seg.pack(side=tk.LEFT, padx=(Design.S_SM, 0))

        # 右侧：一行状态提示（替代原控制台）
        self.status_var = tk.StringVar(value="就绪")
        self.status_lbl = ctk.CTkLabel(row, textvariable=self.status_var,
                                       font=Design.font('small'),
                                       text_color=Design.TEXT_DIM, anchor="e")
        self.status_lbl.pack(side=tk.RIGHT, padx=(Design.S_LG, 0), expand=True,
                             fill=tk.X)

        self._sync_action_buttons()
        self._hairline(parent)

    def _sync_action_buttons(self):
        """按当前设置刷新 启动 / 开始 / 接受 三个按钮的样式。"""
        try:
            if self.engine_running:
                self.btn_engine.configure(
                    text="■   停止自动化", fg_color=Design.ACCENT_DIM,
                    hover_color=Design.BG_HOVER, border_width=1,
                    border_color=Design.ACCENT, text_color=Design.ACCENT)
            else:
                self.btn_engine.configure(
                    text="▶   启动自动化", fg_color=Design.ACCENT,
                    hover_color=Design.ACCENT_HVR, border_width=0,
                    border_color=Design.ACCENT, text_color=Design.ACCENT_TEXT)

            auto_ready = bool(self.settings.get("auto_ready", True))
            self.btn_start.configure(
                text="✓ 自动开始" if auto_ready else "开始",
                fg_color=Design.ACCENT_DIM if auto_ready else Design.BG_SEG,
                border_color=Design.ACCENT if auto_ready else Design.BORDER,
                text_color=Design.ACCENT if auto_ready else Design.TEXT_DIM)

            auto_acc = bool(self.settings.get("auto_accept", True))
            self.btn_accept.configure(
                text="✓ 自动接受" if auto_acc else "接受",
                fg_color=Design.ACCENT_DIM if auto_acc else Design.BG_SEG,
                border_color=Design.ACCENT if auto_acc else Design.BORDER,
                text_color=Design.ACCENT if auto_acc else Design.TEXT_DIM)
        except Exception:
            pass

    # ---------- 状态条 ----------

    def _set_status(self, text, color=None):
        self._status_text = text or ""
        self._status_color = color or Design.TEXT_DIM
        if not self._countdown_active:
            self._apply_status()

    def _apply_status(self):
        try:
            self.status_var.set(_short(self._status_text) or "就绪")
            self.status_lbl.configure(text_color=self._status_color)
        except Exception:
            pass

    def _append_log(self, text, tag="info"):
        for mark, t in (("✅", "success"), ("🎯", "success"),
                        ("⚠", "warning"), ("⚡", "warning"),
                        ("❌", "error")):
            if mark in text:
                tag = t
                break
        color = {"success": Design.SUCCESS, "error": Design.ERROR,
                 "warning": Design.WARNING}.get(tag, Design.TEXT_DIM)
        self._set_status(text, color)

    # ---------- 队列 ----------

    def _poll_queues(self):
        try:
            while True:
                self._append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                self._handle_event(self.gui_queue.get_nowait())
        except queue.Empty:
            pass
        self._poll_tray()
        self.root.after(80, self._poll_queues)

    def _handle_event(self, evt):
        kind = evt.get("event")
        if kind == "log":
            self._append_log(evt.get("text", ""))
        elif kind == "icon":
            tile = self._tiles.get(int(evt.get("cid", 0)))
            if tile is not None:
                tile.on_icon_loaded()
        elif kind == "countdown":
            sec, label = evt.get("seconds"), evt.get("label", "")
            if sec is None:
                self._countdown_active = False
                self._apply_status()
            else:
                self._countdown_active = True
                try:
                    self.status_var.set(f"⏳ {label} · {sec}s")
                    self.status_lbl.configure(text_color=Design.ACCENT)
                except Exception:
                    pass
        elif kind == "phase":
            self._connected = bool(evt.get("connected"))
            self._phase = evt.get("phase")
            self._update_conn_pill()
            if not self._connected:
                self._bench_items = []
                self._bench_available = False
                self.bench_status_var.set("")
                self._render_bench(force=True)
            elif self._phase != "ChampSelect":
                # 安全网：备战席口径由服务线程给，万一推送断流就用阶段兜底清空
                if self._bench_items or self._bench_available:
                    self._bench_items = []
                    self._bench_available = False
                    self.bench_status_var.set("")
                self._render_bench()
            self._apply_auto_visibility()
        elif kind == "bench":
            data = evt.get("data") or {}
            self._bench_items = data.get("items") or []
            self._bench_available = bool(data.get("available", False))
            self._bench_ts = time.time()
            self._bench_note = data.get("note") or ""
            self._render_bench()
            shown = self._visible_bench_items()
            grabbable = sum(1 for it in shown
                            if it.get("swappable") is not False)
            grabbing = " · 抢购中…" if data.get("grabbing") else ""
            self.bench_status_var.set(
                f"· 可换 {grabbable}/{len(shown)}{grabbing}" if shown else "")

    # ---------- 自动显隐 / 吸附客户端（照搬 LA） ----------

    def _apply_auto_visibility(self):
        """LA 的 reaction(() => t.get(), e => ...)：ignore 不动，show→弹出，hide→收起。"""
        cmd = auto_visibility(
            self._phase,
            bool(self.settings.get("auto_hide_in_game", True)),
            self._connected,
        )
        if cmd == "ignore" or cmd == self._auto_vis:
            return
        self._auto_vis = cmd
        if cmd == "show":
            if self._user_hidden:
                return                      # 用户自己收的，别抢戏
            self._show_window()
            if bool(self.settings.get("snap_to_client", True)):
                self._snap_to_client()
        else:
            # 自动收起前先把托盘备好，否则窗口从任务栏消失就没路回来了
            self._ensure_tray()
            try:
                self.root.withdraw()
            except Exception:
                pass

    def _snap_to_client(self):
        """贴到客户端右上角（LA 的 qS(win, 'top-right')）。"""
        if not winpos.AVAILABLE:
            return
        try:
            if winpos.attach_window(self.root, winpos.DEFAULT_CORNER) is None:
                pass    # 客户端没开 / 最小化，静默跳过
        except Exception:
            pass

    # ---------- 按钮 / 自动化 ----------

    def _toggle_engine(self):
        self._stop_engine() if self.engine_running else self._start_engine()

    def _start_engine(self):
        self.engine_running = True
        self.engine = EngineWorker(self.lcu, self.settings, self.gui_queue)
        self.engine.start()
        self._sync_action_buttons()
        self._append_log("✅ 自动化已开启：自动接受/开始 + 备战席抢英雄", "success")
        self.bench_status_var.set("· 等待检测客户端")
        self._render_bench()

    def _stop_engine(self):
        if self.engine:
            self.engine.stop()
            self.engine = None
        self.engine_running = False
        self._sync_action_buttons()
        self._bench_items = []
        self._bench_available = False
        self._render_bench(force=True)
        self.bench_status_var.set("")
        self._append_log("⏹ 自动化已停止", "warning")

    def _set_flag(self, key, value):
        self.settings[key] = value
        if self.engine:
            self.engine.matchmaking.set_flag(key, value)
        save_settings(self.settings)

    def _on_start_toggle(self):
        """「开始」：开 = 自动准备+开始匹配（顺带立刻试一次）；关 = 关闭自动。"""
        new = not bool(self.settings.get("auto_ready", True))
        self._set_flag("auto_ready", new)
        self._sync_action_buttons()
        if new and self.engine:
            self.engine.matchmaking.request_start()
        self._append_log(f"{'▶' if new else '⏸'} 自动开始匹配 "
                         f"{'已开启' if new else '已关闭'}")

    def _on_accept_toggle(self):
        new = not bool(self.settings.get("auto_accept", True))
        self._set_flag("auto_accept", new)
        self._sync_action_buttons()
        self._append_log(f"{'☑' if new else '☐'} 自动接受 "
                         f"{'已开启' if new else '已关闭'}")

    def _on_delay_select(self, value):
        mapping = {"1s": 1, "3s": 3, "5s": 5}
        self.settings["auto_accept_delay"] = normalize_delay(mapping.get(value, 3))
        if self.engine:
            self.engine.matchmaking.set_flag("auto_accept_delay",
                                             self.settings["auto_accept_delay"])
        save_settings(self.settings)
        self._append_log(f"⏱ 自动接受延迟 {self.settings['auto_accept_delay']}s")

    # ---------- ③ WeGame（最底部） ----------

    def _build_wegame(self, parent):
        self._hairline(parent)
        bar = ctk.CTkFrame(parent, fg_color=Design.BG_BAR, corner_radius=0)
        bar.pack(fill=tk.X, side=tk.BOTTOM)

        ctk.CTkLabel(bar, text="WeGame", font=Design.font('bodyb'),
                     text_color=Design.TEXT).pack(side=tk.LEFT,
                                                  padx=(Design.S_XL, Design.S_SM),
                                                  pady=Design.S_MD)

        self.wegame_var = tk.StringVar(
            value=str(self.settings.get("wegame_path", "") or ""))
        self.wegame_entry = ctk.CTkEntry(
            bar, textvariable=self.wegame_var, font=Design.font('small'),
            height=Design.sc(38), corner_radius=Design.R_SM,
            fg_color=Design.BG_INPUT, border_color=Design.BORDER,
            border_width=1, text_color=Design.TEXT,
            placeholder_text="wegame.exe 路径（可点「浏览」自动选）")
        self.wegame_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=Design.S_MD)

        ctk.CTkButton(bar, text="浏览", font=Design.font('small'),
                      width=Design.sc(68), height=Design.sc(38),
                      corner_radius=Design.R_SM,
                      fg_color=Design.BG_SEG, hover_color=Design.BG_HOVER,
                      border_width=1, border_color=Design.BORDER,
                      text_color=Design.TEXT_DIM,
                      command=self._browse_wegame).pack(side=tk.LEFT,
                                                        padx=(Design.S_SM, 0),
                                                        pady=Design.S_MD)
        ctk.CTkButton(bar, text="启动 WeGame", font=Design.font('bodyb'),
                      width=Design.sc(132), height=Design.sc(38),
                      corner_radius=Design.R_SM,
                      fg_color=Design.ACCENT, hover_color=Design.ACCENT_HVR,
                      text_color=Design.ACCENT_TEXT,
                      command=self._launch_wegame).pack(
            side=tk.LEFT, padx=(Design.S_SM, Design.S_XL), pady=Design.S_MD)

    def _browse_wegame(self):
        try:
            init = os.path.dirname(self.wegame_var.get().strip().strip('"') or "C:\\")
            path = filedialog.askopenfilename(
                title="选择 wegame.exe", initialdir=init if os.path.isdir(init) else "C:\\",
                filetypes=[("WeGame", "wegame.exe"), ("可执行文件", "*.exe")])
        except Exception:
            return
        if path:
            self.wegame_var.set(path.replace("/", "\\"))
            self.settings["wegame_path"] = self.wegame_var.get()
            save_settings(self.settings)

    def _launch_wegame(self):
        path = self.wegame_var.get().strip().strip('"')
        try:
            wegame.launch(path)
            self._append_log(f"✅ WeGame 已启动: {path}", "success")
            self.settings["wegame_path"] = path
            save_settings(self.settings)
        except FileNotFoundError as e:
            self._append_log(f"❌ {e}", "error")
            found = wegame.find_wegame()
            if found:
                self.wegame_var.set(found)
                self._append_log(f"💡 已自动定位到 {found}，再点一次启动即可", "warning")
        except Exception as e:
            self._append_log(f"❌ WeGame 启动失败: {e}", "error")

    # ---------- 备战席 ----------

    def _update_conn_pill(self):
        if self._connected:
            label = PHASE_LABEL.get(self._phase, self._phase or "")
            self.conn_dot.configure(text=f"● 已连接 · {label}",
                                    text_color=Design.SUCCESS)
        else:
            self.conn_dot.configure(text="● 未连接", text_color=Design.TEXT_DIM)

    def _visible_bench_items(self):
        """只有「备战席真的存在」时才允许显示英雄。

        判据跟 LeagueAkari 一致，直接采用服务线程关于
        `champ-select session.benchEnabled` 的结论（LA 的 auxWindow 就是
        `if (!session?.benchEnabled) return null`），而不是自己拿 gameflow 的
        phase 字符串去猜。没进选人时客户端对该 session 返回 404，天然就没有备战席。

        额外要求 `_connected`：跨线程推送可能因异常而断流，避免残留上一局头像。
        """
        if not self._connected or not self._bench_available:
            return []
        return self._bench_items

    def _render_bench(self, force=False):
        if self._rendering:
            return
        items = self._visible_bench_items()
        # 空态时把文案也算进签名，否则「引擎刚起 / 刚进大厅」这类变化不会刷新文案
        sig = (tuple(it["championId"] for it in items) if items
               else ("<empty>",) + self._empty_text())
        if not force and sig == self._rendered_ids:
            self._update_tiles()
            return
        self._rendering = True
        try:
            self._render_bench_inner(sig, items)
        finally:
            self._rendering = False

    def _wall_width(self):
        """英雄墙可用宽度；布局未完成时用窗口宽度兜底（winfo_width 未映射前是 1）。"""
        w = self.wall.winfo_width()
        if w and w > 10:
            return w
        try:
            gw = int(str(self.root.geometry()).split("x")[0])
            if gw > 10:
                return gw - Design.S_XL * 2
        except Exception:
            pass
        return 720

    def _render_bench_inner(self, sig, items):
        self._rendered_ids = sig
        for tile in self._tiles.values():
            tile.destroy()
        self._tiles = {}
        for w in self.wall.winfo_children():
            w.destroy()

        if not items:
            self._render_empty()
            return

        # 手绘稿：两排 × 5 列
        cols = min(Design.WALL_COLS, max(1, len(items)))
        step = Design.TILE_PX + Design.TILE_GAP
        used = cols * Design.TILE_PX + (cols - 1) * Design.TILE_GAP
        pad_left = max(0, (self._wall_width() - used) // 2)

        for start in range(0, len(items), cols):
            chunk = items[start:start + cols]
            row = ctk.CTkFrame(self.wall, fg_color="transparent")
            row.pack(anchor='w', padx=(pad_left, 0), pady=(0, Design.TILE_GAP))
            for i, it in enumerate(chunk):
                tile = BenchTile(row, it, self.icons, self._on_tile_click)
                tile.pack(side=tk.LEFT,
                          padx=(0, Design.TILE_GAP) if i < len(chunk) - 1 else 0)
                self._tiles[int(it["championId"])] = tile

        self._update_tiles(force=True)

    def _render_empty(self):
        msg, sub = self._empty_text()
        box = ctk.CTkFrame(self.wall, fg_color="transparent")
        box.pack(expand=True)
        ctk.CTkLabel(box, text=msg, font=Design.font('bodyb'),
                     text_color=Design.TEXT_DIM).pack()
        if sub:
            ctk.CTkLabel(box, text=sub, font=Design.font('small'),
                         text_color=Design.TEXT_MUTE).pack(pady=(Design.sc(5), 0))

    def _empty_text(self):
        if self._bench_note == "picked":
            return "已换上目标英雄", "可以继续点头像换下一个目标"
        if not self.engine_running:
            return "备战席空闲", "点「启动自动化」，进入选人阶段后这里会出现英雄头像"
        if not self._connected:
            return "未检测到客户端", "请先登录英雄联盟客户端"
        if self._phase == "ChampSelect":
            return "备战席暂无英雄", "重开 / 掷骰子后会刷新"
        return "尚未进入选人阶段", "进入选人后这里会出现备战席英雄"

    def _update_tiles(self, force=False):
        """灰色 = 客户端不允许换上（未拥有且不在周免）；蓝环 = 抢购目标。

        可换性直接取客户端官方 pickable 列表，拿不到就不置灰（宁可不标也不标错）。
        """
        target = self.engine.bench._target_cid if self.engine else None
        for it in self._visible_bench_items():
            cid = int(it["championId"])
            tile = self._tiles.get(cid)
            if tile is None:
                continue
            if cid == target:
                state = "target"
            elif it.get("swappable") is False:
                state = "locked"
            else:
                state = "free"
            tile.set_state(state, force=force)

    def _tick_tiles(self):
        if self._tiles:
            self._update_tiles()
        self.root.after(150, self._tick_tiles)

    def _on_tile_click(self, cid, name):
        if not self.engine_running or not self.engine:
            self._append_log("⚠ 请先「启动自动化」，再点头像设抢购目标", "warning")
            return
        item = next((it for it in self._bench_items
                     if int(it["championId"]) == cid), None)
        if item is not None and item.get("swappable") is False:
            self._append_log(f"⚠ {name} 换不了（未拥有 / 不在周免）", "warning")
            return
        self.engine.bench.set_target(cid, name)
        self._update_tiles(force=True)

    # ---------- 设置弹层 ----------

    SETTING_ROWS = (
        ("auto_play_again", "自动回到房间",
         "一局结束后自动点「再次游戏」，回房继续排"),
        ("auto_reconnect", "自动重连",
         "掉线时自动连回正在进行的对局"),
        ("auto_accept_invite", "自动接受房间邀请",
         "好友 / 队友拉你进房时自动同意"),
        ("autostart", "开机自启",
         "登录 Windows 后自动启动本工具"),
        ("close_to_tray", "关闭时最小化到托盘",
         "点 × 不退出，收进托盘继续工作"),
        ("auto_hide_in_game", "对局中自动隐藏",
         "进对局自动收起，回到房间自动弹出（含选人阶段）"),
        ("snap_to_client", "吸附客户端右上角",
         "弹出时自动贴到英雄联盟客户端右上角"),
    )

    def _open_settings(self):
        w = self._settings_win
        if w is not None and w.winfo_exists():
            w.deiconify()
            w.lift()
            w.focus_force()
            return

        win = ctk.CTkToplevel(self.root)
        self._settings_win = win
        win.title("设置")
        win.configure(fg_color=Design.BG)
        # 高度随开关行数自适应。行高用实测值 86px：CTk 控件有最小高度，
        # 缩放后并不严格等于 SCALE × 原行高，按缩放值排会裁掉最后一行。
        n_rows = len(self.SETTING_ROWS)
        win_h = 145 + n_rows * 86
        win.geometry(f"{Design.sc(520)}x{win_h}"
                     f"+{self.root.winfo_x() + Design.sc(160)}"
                     f"+{self.root.winfo_y() + Design.sc(40)}")
        win.resizable(False, False)
        try:
            win.transient(self.root)
        except Exception:
            pass

        head = ctk.CTkFrame(win, fg_color=Design.BG_BAR, corner_radius=0)
        head.pack(fill=tk.X)
        ctk.CTkLabel(head, text="设置", font=Design.font('brand'),
                     text_color=Design.TEXT).pack(side=tk.LEFT,
                                                  padx=Design.S_XL, pady=Design.S_MD)

        card = ctk.CTkFrame(win, fg_color=Design.BG_CARD, corner_radius=Design.R_LG,
                            border_width=1, border_color=Design.BORDER)
        card.pack(fill=tk.BOTH, expand=True, padx=Design.S_LG, pady=Design.S_LG)

        self._setting_switches = {}
        for i, (key, title, desc) in enumerate(self.SETTING_ROWS):
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill=tk.X, padx=Design.S_LG,
                     pady=(Design.S_MD if i else Design.S_LG, 0))

            left = ctk.CTkFrame(row, fg_color="transparent")
            left.pack(side=tk.LEFT, fill=tk.X, expand=True)
            ctk.CTkLabel(left, text=title, font=Design.font('bodyb'),
                         text_color=Design.TEXT, anchor="w").pack(fill=tk.X)
            ctk.CTkLabel(left, text=desc, font=Design.font('small'),
                         text_color=Design.TEXT_MUTE, anchor="w").pack(fill=tk.X)

            var = tk.BooleanVar(value=bool(self.settings.get(key, False)))
            sw = ctk.CTkSwitch(row, text="", variable=var, width=Design.sc(46),
                               progress_color=Design.ACCENT,
                               button_color=Design.TEXT_INV,
                               button_hover_color=Design.TEXT_INV,
                               fg_color=Design.LINE,
                               command=lambda k=key, v=var: self._on_setting_toggle(k, v))
            sw.pack(side=tk.RIGHT, padx=(Design.S_MD, 0))
            self._setting_switches[key] = (sw, var)

            if i < len(self.SETTING_ROWS) - 1:
                ctk.CTkFrame(card, fg_color=Design.BORDER, height=1,
                             corner_radius=0).pack(fill=tk.X, padx=Design.S_LG,
                                                   pady=(Design.S_MD, 0))

        foot = ctk.CTkFrame(win, fg_color="transparent")
        foot.pack(fill=tk.X, padx=Design.S_LG, pady=(0, Design.S_LG))
        ctk.CTkLabel(foot, text="开关即时生效并自动保存", font=Design.font('small'),
                     text_color=Design.TEXT_MUTE).pack(side=tk.LEFT)
        ctk.CTkButton(foot, text="完成", font=Design.font('bodyb'),
                      width=Design.sc(96), height=Design.sc(36),
                      corner_radius=Design.R_SM,
                      fg_color=Design.ACCENT, hover_color=Design.ACCENT_HVR,
                      text_color=Design.ACCENT_TEXT,
                      command=win.destroy).pack(side=tk.RIGHT)

        win.protocol("WM_DELETE_WINDOW", win.destroy)

    def _on_setting_toggle(self, key, var):
        value = bool(var.get())
        self.settings[key] = value
        save_settings(self.settings)

        if key == "autostart":
            if tray_mod.set_autostart(value):
                self._append_log(
                    f"✅ 已{'开启' if value else '关闭'}开机自启", "success")
            else:
                self._append_log("⚠ 开机自启设置失败（注册表写入被拒）", "warning")
            return

        if key == "close_to_tray" and value:
            self._ensure_tray()

        if key == "snap_to_client" and value:
            self._snap_to_client()          # 打开就立刻贴一次，免得以为没生效

        if key == "auto_hide_in_game" and not value:
            # 关掉自动隐藏：如果当前正被自动收着，立刻放出来
            if self._auto_vis == "hide":
                self._auto_vis = "ignore"
                self._show_window()

        label = dict((k, t) for k, t, _d in self.SETTING_ROWS).get(key, key)
        self._append_log(f"{'☑' if value else '☐'} {label} "
                         f"{'已开启' if value else '已关闭'}")

    # ---------- 海克斯图鉴 ----------

    RARITY_STYLE = {
        "棱彩":   ("#F3E8FA", "#7A2FA8"),
        "黄金":   ("#FFF3DF", "#9A6209"),
        "白银":   ("#EFEFF2", "#5C5C61"),
        "事件抉择": ("#E1F4F6", "#0B7285"),
    }
    AUG_LIMIT = 80

    def _open_augments(self):
        w = self._aug_win
        if w is not None and w.winfo_exists():
            w.deiconify()
            w.lift()
            w.focus_force()
            return
        if not self.augments or self.augments.error:
            msg = (self.augments.error if self.augments else "海克斯数据不可用")
            self._append_log(f"⚠ {msg}", "warning")
            return

        win = ctk.CTkToplevel(self.root)
        self._aug_win = win
        win.title("海克斯图鉴")
        win.configure(fg_color=Design.BG)
        win.geometry(f"{Design.sc(760)}x{Design.sc(560)}"
                     f"+{max(0, self.root.winfo_x() - Design.sc(60))}"
                     f"+{self.root.winfo_y() + Design.sc(40)}")
        try:
            win.transient(self.root)
        except Exception:
            pass

        head = ctk.CTkFrame(win, fg_color=Design.BG_BAR, corner_radius=0)
        head.pack(fill=tk.X)
        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side=tk.LEFT, padx=Design.S_XL, pady=Design.S_MD)
        ctk.CTkLabel(left, text="海克斯图鉴", font=Design.font('brand'),
                     text_color=Design.TEXT).pack(side=tk.LEFT)
        cnt = self.augments.counts()
        ctk.CTkLabel(left, text=f"· Riot 官方数据 补丁 {self.augments.patch}"
                                f" · 共 {cnt['total']} 条（大乱斗池 {cnt['aram']}）",
                     font=Design.font('small'),
                     text_color=Design.TEXT_MUTE).pack(side=tk.LEFT,
                                                       padx=(8, 0), pady=(4, 0))

        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill=tk.X, padx=Design.S_XL, pady=(Design.S_MD, Design.S_SM))
        self._aug_query = tk.StringVar(value="")
        entry = ctk.CTkEntry(bar, textvariable=self._aug_query,
                             placeholder_text="搜索海克斯名称或效果关键词…",
                             font=Design.font('body'), height=Design.sc(38),
                             corner_radius=Design.R_SM, fg_color=Design.BG_INPUT,
                             border_color=Design.BORDER, border_width=1,
                             text_color=Design.TEXT)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._aug_rarity = "全部"
        seg = ctk.CTkSegmentedButton(
            bar, values=["全部"] + self.augments.rarities(),
            command=self._on_aug_rarity, fg_color=Design.BG_SEG,
            selected_color=Design.ACCENT, selected_hover_color=Design.ACCENT_HVR,
            unselected_color=Design.BG_SEG, unselected_hover_color=Design.BG_HOVER,
            text_color=Design.TEXT, font=Design.font('small'),
            height=Design.sc(38), corner_radius=Design.R_SM)
        seg.set("全部")
        seg.pack(side=tk.LEFT, padx=(Design.S_SM, 0))
        self._aug_seg = seg

        opt = ctk.CTkFrame(win, fg_color="transparent")
        opt.pack(fill=tk.X, padx=Design.S_XL)
        self._aug_aram_only = tk.BooleanVar(value=True)
        ctk.CTkSwitch(opt, text="只看大乱斗可用", variable=self._aug_aram_only,
                      font=Design.font('small'), text_color=Design.TEXT_DIM,
                      progress_color=Design.ACCENT, button_color=Design.TEXT_INV,
                      fg_color=Design.LINE, width=Design.sc(42),
                      command=self._render_augments).pack(side=tk.LEFT)
        self._aug_count = tk.StringVar(value="")
        ctk.CTkLabel(opt, textvariable=self._aug_count, font=Design.font('small'),
                     text_color=Design.TEXT_MUTE).pack(side=tk.RIGHT)

        self._aug_list = ctk.CTkScrollableFrame(win, fg_color="transparent",
                                                corner_radius=0)
        self._aug_list.pack(fill=tk.BOTH, expand=True,
                            padx=Design.S_LG, pady=(Design.S_SM, Design.S_SM))

        entry.bind("<KeyRelease>", self._on_aug_key)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self._render_augments()

    def _on_aug_key(self, _evt):
        try:
            if self._aug_job:
                self.root.after_cancel(self._aug_job)
        except Exception:
            pass
        self._aug_job = self.root.after(240, self._render_augments)

    def _on_aug_rarity(self, value):
        self._aug_rarity = value
        self._render_augments()

    def _render_augments(self):
        if not self._aug_win or not self._aug_win.winfo_exists():
            return
        for w in self._aug_list.winfo_children():
            w.destroy()

        rarity = "" if self._aug_rarity == "全部" else self._aug_rarity
        rows = self.augments.search(
            self._aug_query.get(), rarity,
            aram_only=bool(self._aug_aram_only.get()))
        total = len(rows)
        shown = rows[:self.AUG_LIMIT]
        self._aug_count.set(f"共 {total} 条" + (f"，显示前 {self.AUG_LIMIT} 条"
                                              if total > self.AUG_LIMIT else ""))

        if not rows:
            ctk.CTkLabel(self._aug_list, text="没有匹配的海克斯",
                         font=Design.font('body'), text_color=Design.TEXT_MUTE).pack(
                pady=Design.S_XL)
            return

        for e in shown:
            card = ctk.CTkFrame(self._aug_list, fg_color=Design.BG_CARD,
                                corner_radius=Design.R_MD, border_width=1,
                                border_color=Design.BORDER)
            card.pack(fill=tk.X, pady=(0, Design.S_SM))
            line = ctk.CTkFrame(card, fg_color="transparent")
            line.pack(fill=tk.X, padx=Design.S_MD, pady=(Design.S_MD, 0))
            ctk.CTkLabel(line, text=e.name, font=Design.font('bodyb'),
                         text_color=Design.TEXT).pack(side=tk.LEFT)
            bg, fg = self.RARITY_STYLE.get(e.rarity, ("#EFEFF2", "#5C5C61"))
            if e.rarity:
                ctk.CTkLabel(line, text=e.rarity, font=Design.font('micro'),
                             fg_color=bg, text_color=fg, corner_radius=Design.R_PILL,
                             height=Design.sc(20)).pack(
                    side=tk.LEFT, padx=(Design.sc(8), 0))
            if e.aram_specific:
                ctk.CTkLabel(line, text="大乱斗专属", font=Design.font('micro'),
                             fg_color=Design.BG_SEG, text_color=Design.TEXT_DIM,
                             corner_radius=Design.R_PILL,
                             height=Design.sc(20)).pack(
                    side=tk.LEFT, padx=(Design.sc(6), 0))

            if e.has_desc:
                text = e.desc + ("（数值以游戏内为准）" if e.approx else "")
                color = Design.TEXT_DIM
            else:
                text = "官方暂无中文描述（游戏内可看完整效果）"
                color = Design.TEXT_MUTE
            ctk.CTkLabel(card, text=text, font=Design.font('small'),
                         text_color=color, justify="left", anchor="w",
                         wraplength=Design.sc(680)).pack(
                fill=tk.X, padx=Design.S_MD, pady=(Design.sc(4), Design.S_MD))

    # ---------- 托盘 ----------

    def _ensure_tray(self):
        if self.tray is not None:
            return True
        try:
            t = tray_mod.TrayIcon(
                tooltip="xiaobai助手",
                icon_path=os.path.join(BASE_DIR, "assets", "icon.ico"))
            if t.start():
                self.tray = t
                self._append_log("✅ 托盘已启用：左键显示主界面，右键退出", "success")
                return True
        except Exception:
            pass
        self._append_log("⚠ 托盘不可用（已降级为最小化到任务栏）", "warning")
        return False

    def _poll_tray(self):
        if self.tray is None:
            return
        ev = self.tray.poll()
        if ev == "show":
            self._show_window()
        elif ev == "quit":
            self._quit()

    def _show_window(self):
        self._user_hidden = False
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    # ---------- 退出 ----------

    def _on_close(self):
        if self._quitting:
            self.root.destroy()
            return
        if self.settings.get("close_to_tray") and self._ensure_tray():
            self._user_hidden = True
            try:
                self.root.withdraw()
            except Exception:
                pass
            self._set_status("已收进托盘，左键托盘图标可再次打开")
            return
        self._quit()

    def _quit(self):
        if self._quitting:
            return
        self._quitting = True
        try:
            if self.engine:
                self.engine.stop()
        except Exception:
            pass
        try:
            if self.tray is not None:
                self.tray.stop()
        except Exception:
            pass
        self.root.destroy()


# ============================================================================
# 入口
# ============================================================================

def make_std_streams_safe():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            continue
        except Exception:
            pass
        try:
            buffer = getattr(stream, "buffer", None)
            if buffer is not None:
                setattr(sys, name, io.TextIOWrapper(
                    buffer, encoding="utf-8", errors="replace", line_buffering=True))
        except Exception:
            pass


def _enable_high_dpi():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def main():
    try:
        make_std_streams_safe()
    except Exception:
        pass
    _enable_high_dpi()
    try:
        app = CTkLauncherApp()
    except Exception:
        err = traceback.format_exc()
        try:
            messagebox.showerror("启动失败", err[-1500:])
        except Exception:
            print(err)
        raise
    app.root.mainloop()


if __name__ == "__main__":
    main()
