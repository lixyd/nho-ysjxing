"""
nho有手就行 — customtkinter 版 UI（现代化重写）

设计系统依据 ui-ux-pro-max 规范：
  · 单一强调色（海克斯金），暖黑石墨底，中性灰带金微调
  · spacing scale 4 基准 / type scale 分层 / radius 统一
  · 组件状态：normal·hover·active·disabled·focus
  · WCAG AA 文本对比；empty/loading/error 状态显式覆盖
  · 禁用纯黑纯白、禁用蓝紫霓虹堆叠（避免廉价 AI 感）

逻辑层完全复用，不改：
  · scripts/*（matchmaking / runes / auto_hex / config / updater …）
  · gui_launcher.GuiController / TrayManager / UpdateDialog / RoundPowerButton / LogRedirector / Theme
  · main.DataManager / GameAnalyzer / OverlayApp（延迟导入）

只重写 UI 构建层：tk.* ttk.* → customtkinter.*
"""
import tkinter as tk
from tkinter import messagebox, filedialog
import customtkinter as ctk
import threading
import queue
import os
import sys
import io
import datetime
import traceback
import subprocess

try:
    import winreg
except ImportError:
    winreg = None

from scripts.config import (
    BASE_DIR, SETTINGS_FILE, DEFAULT_SETTINGS, AUTO_DELAY_CHOICES,
    load_settings, save_settings, normalize_delay,
)
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

from PIL import Image, ImageDraw
import pystray

# 复用逻辑层（顶部仅需 PIL + pystray，即可 import 成功）
from gui_launcher import (
    Theme, LogRedirector, RoundPowerButton,
    GUIController, TrayManager, UpdateDialog,
    _is_wegame_exe, _expand_wegame_candidates,
    _WEGAME_COMMON_PATHS, _list_fixed_drives, _list_dirs, _get_desktop_path,
)
from scripts.runes import RuneService


# ============================================================================
# 设计系统 (Design Tokens) — 应用 ui-ux-pro-max 规范
# ============================================================================

class Design:
    """色彩 / 间距 / 圆角 / 字体 token。CTk 控件按 token 取值，保证一致性。

    配色方向：薄荷墨绿（与宣传网页、游戏内推荐色 #7FE3C9 统一品牌）
      · 冷调深墨绿底，中性带青的灰阶，单一强调色薄荷绿
      · 强调色仅用于：焦点态、选中态、主按钮、状态点 —— 不做大面积铺色
    """

    # —— 色彩（薄荷墨绿：冷底 + 薄荷强调）——
    BG          = "#0C1116"   # 窗口底 冷调近黑
    BG_CARD     = "#141B21"   # 卡片
    BG_CARD_HVR = "#1A242C"   # 卡片悬停
    BG_INPUT    = "#1A242C"   # 输入框
    BG_SEG      = "#1E2A31"   # 分段底
    ACCENT      = "#7FE3C9"   # 薄荷绿 主强调（品牌色）
    ACCENT_HVR  = "#9BEDD8"   # 薄荷 悬停
    ACCENT_DIM  = "#243239"   # 弱分割
    SUCCESS     = "#5FD3A6"   # 成功（薄荷同族，略深以区分强调）
    WARNING     = "#E8C468"   # 警告 暖黄（冷底上唯一的暖色，仅用于警示）
    ERROR       = "#F0736A"   # 错误
    TEXT        = "#E4EDEA"   # 主文本（微青白，呼应冷调）
    TEXT_DIM    = "#7C8B92"   # 次文本
    BORDER      = "#212F36"   # 描边
    GOLD        = "#C9A15A"   # 打赏金（保留：打赏语义独立，暖色小面积点缀）
    ACCENT_TEXT = "#06231C"   # 薄荷底配深字（AA 对比 ≈ 11:1）

    # —— 间距 scale（4 基准）——
    S_XXS = 2; S_XS = 4; S_SM = 8; S_MD = 12; S_LG = 16; S_XL = 24; S_XXL = 32

    # —— 圆角 ——
    R_SM = 8; R_MD = 12; R_LG = 16; R_PILL = 999

    # —— 字体（中文桌面：Microsoft YaHei UI；等宽日志：Cascadia Code）——
    F_FAMILY = "Microsoft YaHei UI"
    F_MONO   = "Cascadia Code"

    # —— 开关（CTkSwitch）滑块：关闭态不用纯白，避免在冷底上刺眼 ——
    SWITCH_KNOB_OFF = "#5C6C74"   # 关闭：中性灰
    SWITCH_KNOB_HVR = "#8FA3AA"   # 悬停：提亮中性灰

    # —— 电源键配色（原为 gui_launcher 内硬编码金色，改为可注入以统一品牌）——
    POWER = {
        "idle_ring": "#7FE3C9", "idle_fill": "#0E2620", "idle_glyph": "#9BEDD8",
        "run_ring":  "#F0736A", "run_fill":  "#2A1614", "run_glyph":  "#F5A79F",
        "off_ring":  "#26343C", "off_fill":  "#151E24", "off_glyph":  "#46565E",
        "idle_hover": "#14332B", "run_hover": "#3A1D1A",
    }

    @classmethod
    def font(cls, key):
        """字号整体上调一档：桌面端 14px 正文才是舒适阅读尺寸，
        原 9~12px 在 1080p/2K 上都偏小。"""
        scale = {
            'hero':   (cls.F_FAMILY, 24, 'bold'),
            'title':  (cls.F_FAMILY, 19, 'bold'),
            'h2':     (cls.F_FAMILY, 15, 'bold'),
            'body':   (cls.F_FAMILY, 14, 'normal'),
            'btn':    (cls.F_FAMILY, 14, 'bold'),
            'small':  (cls.F_FAMILY, 12, 'normal'),
            'micro':  (cls.F_FAMILY, 11, 'normal'),
            'log':    (cls.F_MONO,   11, 'normal'),
        }
        return scale.get(key, scale['body'])

    # 控件尺寸（随字号同步放大，避免文字撑破控件）
    H_INPUT = 38; H_BTN = 34; H_BTN_SM = 32; H_FOOTER = 32
    LOGO_PX = 36

    @classmethod
    def color(cls, name):
        return getattr(cls, name, cls.TEXT)


# CTk 全局外观
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")  # 局部 fg_color 覆盖


# ============================================================================
# 主窗口（customtkinter 重写）
# ============================================================================

class CTkLauncherApp:
    """nho有手就行 — CTk 版主界面。

    对外接口与原 LauncherApp 一致（controller / settings / 队列事件名不变），
    仅 UI 构建层换为 customtkinter 控件。
    """

    def __init__(self):
        self.root = ctk.CTk()
        self.root.title("nho有手就行 · 海克斯 / 匹配 / 符文")
        self._fit_window_to_screen()
        self.root.configure(fg_color=Design.BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._set_window_icon(self.root)

        # 状态变量
        self.engine_running = False
        self.controller = None
        self.overlay = None
        self.overlay_window = None
        self.dm = None
        self.analyzer = None
        self.lcu = None
        self.tray = TrayManager(self)
        self.settings = self._load_settings()
        self.rune_service = RuneService()
        self._toggle_vars = {}

        # 通信队列
        self.overlay_queue = queue.Queue()
        self.gui_queue = queue.Queue()
        self.log_queue = queue.Queue()

        # UI 变量
        self.hero_var = tk.StringVar(value="—")
        self.status_var = tk.StringVar(value="等待启动")
        self.countdown_var = tk.StringVar(value="")
        self.status_color = Design.TEXT_DIM
        self._pulse_state = 0
        self._logo_photo = None
        self._icon_photo = None

        # 日志重定向
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        sys.stdout = LogRedirector(self.log_queue, self._orig_stdout)
        sys.stderr = LogRedirector(self.log_queue, self._orig_stderr)

        self._build_ui()

        self.root.after(100, self._poll_queues)
        self.root.after(300, self._load_data)
        self.root.after(4000, self._auto_check_official_augments)

    def _fit_window_to_screen(self):
        """按屏幕实际可用区域决定窗口尺寸。

        不能写死高度：本机是 2560x1600 但系统缩放 200%，逻辑可用高度只有 800，
        写死 800 会正好顶满屏幕、标题栏和日志区被裁掉。
        这里按 tk 缩放比例反推逻辑分辨率，再留出任务栏/标题栏余量。
        """
        try:
            self.root.update_idletasks()
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            scaling = float(self.root.tk.call('tk', 'scaling')) or 1.0
            # winfo_screen* 返回物理像素，除以缩放得到逻辑可用尺寸
            logical_h = int(sh / scaling) if scaling > 1.2 else sh
            logical_w = int(sw / scaling) if scaling > 1.2 else sw

            w = min(720, max(640, logical_w - 80))
            # 顶部让出 ~60（标题栏+状态栏），底部让出 ~90（任务栏）
            h = min(760, max(560, logical_h - 150))
            x = max(0, (logical_w - w) // 2)
            y = max(20, (logical_h - h) // 3)
            self.root.geometry(f"{w}x{h}+{x}+{y}")
            self.root.minsize(620, min(640, h))
        except Exception:
            # 兜底：保守尺寸，确保不会超出常见 1080p 可用区
            self.root.geometry("700x680")
            self.root.minsize(620, 600)

    # ==================== UI 构建 ====================

    def _card(self, parent, title=None):
        """分组卡片：深色卡底 + 细描边 + 左侧 3px 金竖条 + 圆角。"""
        outer = ctk.CTkFrame(parent, fg_color=Design.BG_CARD,
                             border_color=Design.BORDER, border_width=1,
                             corner_radius=Design.R_MD)
        outer.pack(fill=ctk.X, pady=(0, Design.S_SM))
        # 左侧金竖条（height=0：CTkFrame 默认 desired 高 200，会被 pack 请求成
        # 300 物理像素，把整张卡撑出大片死空间——必须显式归零）
        bar = ctk.CTkFrame(outer, width=3, height=0, fg_color=Design.ACCENT,
                           corner_radius=0)
        bar.pack(side=ctk.LEFT, fill=ctk.Y, padx=(0, Design.S_SM))
        inner = ctk.CTkFrame(outer, fg_color="transparent")
        inner.pack(fill=ctk.BOTH, expand=True, padx=Design.S_MD, pady=Design.S_MD)
        if title:
            ctk.CTkLabel(inner, text=title, font=Design.font('h2'),
                         text_color=Design.ACCENT_HVR, anchor='w').pack(
                fill=ctk.X, pady=(0, Design.S_SM))
        return inner

    def _build_ui(self):
        main = ctk.CTkFrame(self.root, fg_color="transparent")
        main.pack(fill=ctk.BOTH, expand=True, padx=Design.S_LG, pady=(Design.S_LG, Design.S_MD))

        # —— 顶部品牌横幅 ——
        self._build_banner(main)

        # —— 状态卡片 ——
        status = self._card(main)
        row = ctk.CTkFrame(status, fg_color="transparent")
        row.pack(fill=ctk.X)
        ctk.CTkLabel(row, text="当前英雄", font=Design.font('micro'),
                     text_color=Design.ACCENT_HVR,
                     fg_color=Design.BG_SEG, corner_radius=Design.R_SM,
                     padx=8, pady=2).pack(side=ctk.LEFT)
        self.hero_label = ctk.CTkLabel(row, textvariable=self.hero_var,
                                       font=Design.font('hero'),
                                       text_color=Design.TEXT, anchor='w')
        self.hero_label.pack(side=ctk.LEFT, padx=(Design.S_MD, 0))
        self.status_dot = ctk.CTkLabel(row, text="●", font=("Segoe UI", 11),
                                       text_color=Design.TEXT_DIM)
        self.status_dot.pack(side=ctk.RIGHT, padx=(6, 2))
        self.status_label = ctk.CTkLabel(row, textvariable=self.status_var,
                                         font=Design.font('small'),
                                         text_color=Design.TEXT_DIM)
        self.status_label.pack(side=ctk.RIGHT)
        self.countdown_label = ctk.CTkLabel(status, textvariable=self.countdown_var,
                                            font=Design.font('title'),
                                            text_color=Design.ACCENT, anchor='w')
        self._countdown_packed = False

        # —— 对局卡片：电源键 + 延迟段 + 四开关 ——
        # 控制行用 grid：列 0 电源键 / 列 1 说明文字（弹性）/ 列 2 延迟标签 / 列 3 分段。
        # 之前用 pack(side=LEFT/RIGHT) 链，在 150% DPI 下 caption 会被挤到按钮下方。
        auto = self._card(main)
        ctrl = ctk.CTkFrame(auto, fg_color="transparent")
        ctrl.pack(fill=ctk.X, pady=(0, Design.S_SM))
        ctrl.grid_columnconfigure(1, weight=1)

        self.power_btn = RoundPowerButton(ctrl, command=self._toggle_engine,
                                          bg=Design.BG_CARD, palette=Design.POWER)
        self.power_btn.grid(row=0, column=0, sticky='w')
        self.power_caption = ctk.CTkLabel(ctrl, text="开始识别",
                                          font=Design.font('small'),
                                          text_color=Design.TEXT_DIM, anchor='w')
        self.power_caption.grid(row=0, column=1, sticky='w', padx=(Design.S_SM, 0))

        # 延迟分段控件（CTkSegmentedButton）
        self._delay_value = normalize_delay(self.settings.get("auto_accept_delay", 5))
        self._delay_seg = ctk.CTkSegmentedButton(
            ctrl, values=["立即", "3s", "5s", "10s"],
            command=self._on_delay_select_idx,
            fg_color=Design.BG_SEG, selected_color=Design.ACCENT,
            selected_hover_color=Design.ACCENT_HVR,
            text_color=Design.TEXT, font=Design.font('small'),
            height=30, corner_radius=Design.R_SM)
        self._delay_seg.set({0: "立即", 3: "3s", 5: "5s", 10: "10s"}[self._delay_value])
        self._delay_seg.grid(row=0, column=3, sticky='e')
        ctk.CTkLabel(ctrl, text="延迟", font=Design.font('small'),
                     text_color=Design.TEXT_DIM).grid(row=0, column=2, sticky='e',
                                                      padx=(0, Design.S_XS))
        self.start_btn = self.power_btn
        self.stop_btn = self.power_btn

        # 四开关
        toggles_row = ctk.CTkFrame(auto, fg_color="transparent")
        toggles_row.pack(fill=ctk.X)
        for key, label in [("auto_accept", "自动接受"), ("auto_ready", "自动准备"),
                          ("auto_apply_runes", "自动符文"), ("auto_hex", "海克斯识别")]:
            var = tk.BooleanVar(value=bool(self.settings.get(key, DEFAULT_SETTINGS.get(key, False))))
            self._toggle_vars[key] = var
            sw = ctk.CTkSwitch(
                toggles_row, text=label, variable=var, command=lambda k=key, v=var: self._on_toggle(k, v),
                font=Design.font('small'), text_color=Design.TEXT,
                progress_color=Design.ACCENT, fg_color=Design.ACCENT_DIM,
                button_color=Design.SWITCH_KNOB_OFF,
                button_hover_color=Design.SWITCH_KNOB_HVR,
                width=48, height=24)
            sw.pack(side=ctk.LEFT, padx=(0, Design.S_MD))

        # —— 英雄 / 符文卡片 ——
        hero_card = self._card(main)
        manual = ctk.CTkFrame(hero_card, fg_color="transparent")
        manual.pack(fill=ctk.X, pady=(0, Design.S_SM))
        self.hero_entry = ctk.CTkEntry(manual, placeholder_text="输入英雄名/拼音…",
                                       font=Design.font('body'), height=Design.H_INPUT,
                                       fg_color=Design.BG_INPUT, text_color=Design.TEXT,
                                       border_color=Design.BORDER, border_width=1,
                                       corner_radius=Design.R_SM)
        self.hero_entry.pack(side=ctk.LEFT, fill=ctk.X, expand=True, padx=(0, Design.S_SM))
        self.hero_entry.bind("<Return>", lambda e: self._manual_set_hero())
        self.manual_btn = ctk.CTkButton(manual, text="锁定英雄", font=Design.font('btn'),
                                        fg_color=Design.ACCENT, hover_color=Design.ACCENT_HVR,
                                        text_color=Design.ACCENT_TEXT, height=Design.H_INPUT,
                                        corner_radius=Design.R_SM,
                                        command=self._manual_set_hero)
        self.manual_btn.pack(side=ctk.RIGHT)

        action = ctk.CTkFrame(hero_card, fg_color="transparent")
        action.pack(fill=ctk.X, pady=(0, Design.S_XS))
        btn_kw = dict(font=Design.font('small'), height=Design.H_BTN, corner_radius=Design.R_SM,
                      fg_color=Design.BG_SEG, hover_color=Design.BG_CARD_HVR,
                      border_color=Design.BORDER, border_width=1, text_color=Design.TEXT)
        self.refresh_btn = ctk.CTkButton(action, text="🔄 刷新识别",
                                         command=self._manual_refresh_ocr, **btn_kw)
        self.refresh_btn.pack(side=ctk.LEFT, expand=True, fill=ctk.X, padx=(0, 4))
        self.detect_hero_btn = ctk.CTkButton(action, text="识别英雄",
                                             command=self._manual_refresh_hero, **btn_kw)
        self.detect_hero_btn.pack(side=ctk.LEFT, expand=True, fill=ctk.X, padx=(0, 4))
        self.reset_btn = ctk.CTkButton(action, text="重置",
                                       command=self._manual_reset, **btn_kw)
        self.reset_btn.pack(side=ctk.LEFT, expand=True, fill=ctk.X, padx=(0, 4))
        self.rune_btn = ctk.CTkButton(action, text="⚔ 套用符文",
                                      command=self._manual_apply_runes, **btn_kw)
        self.rune_btn.pack(side=ctk.LEFT, expand=True, fill=ctk.X)

        self.rune_info_var = tk.StringVar(value="符文: 锁定英雄后显示推荐")
        ctk.CTkLabel(hero_card, textvariable=self.rune_info_var,
                     font=Design.font('small'), text_color=Design.TEXT_DIM,
                     anchor='w', justify='left', wraplength=640).pack(fill=ctk.X)

        # —— 海克斯卡片（动态出现）——
        self._hex_card_parent = main
        self._hex_card = self._card(main)
        self.hex_banner_var = tk.StringVar(value="")
        self.hex_banner = ctk.CTkLabel(self._hex_card, textvariable=self.hex_banner_var,
                                       font=Design.font('h2'), text_color=Design.ACCENT,
                                       anchor='w')
        self.hex_banner.pack(fill=ctk.X)
        self._hex_banner_clear_after = None
        self.hex_result_var = tk.StringVar(value="")
        self.hex_result = ctk.CTkLabel(self._hex_card, textvariable=self.hex_result_var,
                                       font=Design.font('small'), text_color=Design.TEXT_DIM,
                                       anchor='w', justify='left', wraplength=640)
        self.hex_result.pack(fill=ctk.X, pady=(Design.S_XS, 0))
        self._hex_card.pack_forget()

        # —— 页脚 ——
        footer = ctk.CTkFrame(main, fg_color="transparent")
        footer.pack(fill=ctk.X, pady=(Design.S_SM, Design.S_XS))
        self._footer = footer

        # WeGame 路径提示（此前只更新了 StringVar，界面上没有载体，等于看不到）
        self.wegame_hint_var = tk.StringVar(value="")
        ctk.CTkLabel(footer, textvariable=self.wegame_hint_var,
                     font=Design.font('micro'), text_color=Design.TEXT_DIM,
                     anchor='w', justify='left', wraplength=640).pack(
            fill=ctk.X, pady=(0, Design.S_XS))
        self._update_wegame_hint()

        f_kw = dict(font=Design.font('small'), height=Design.H_FOOTER, corner_radius=Design.R_SM,
                    fg_color="transparent", hover_color=Design.BG_CARD_HVR,
                    text_color=Design.TEXT_DIM, border_color=Design.BORDER, border_width=1)
        # CTkButton 默认 width=200，五个按钮必溢出窗口——逐个给显式宽度
        self.update_btn = ctk.CTkButton(footer, text="数据更新", width=104, command=self._show_update_dialog, **f_kw)
        self.update_btn.pack(side=ctk.LEFT, padx=(0, 6))
        self.wegame_btn = ctk.CTkButton(footer, text="启动 WeGame", width=132, command=self._launch_wegame, **f_kw)
        self.wegame_btn.pack(side=ctk.LEFT, padx=(0, 6))
        self.wegame_path_btn = ctk.CTkButton(footer, text="路径…", width=72, command=self._choose_wegame_path, **f_kw)
        self.wegame_path_btn.pack(side=ctk.LEFT, padx=(0, 6))
        self.wegame_shortcut_btn = ctk.CTkButton(footer, text="快捷方式", width=104, command=self._create_wegame_shortcut, **f_kw)
        self.wegame_shortcut_btn.pack(side=ctk.LEFT, padx=(0, 6))
        self.tray_btn = ctk.CTkButton(footer, text="最小化", width=84, command=self._minimize_to_tray, **f_kw)
        self.tray_btn.pack(side=ctk.RIGHT)

        # —— 日志 ——
        self.log_text = ctk.CTkTextbox(main, font=Design.font('log'), height=100,
                                       fg_color=Design.BG_INPUT, text_color=Design.TEXT_DIM,
                                       border_color=Design.BORDER, border_width=1,
                                       corner_radius=Design.R_SM, wrap='word',
                                       state='disabled')
        self.log_text.pack(fill=ctk.BOTH, expand=True, pady=(Design.S_SM, 0))
        self.log_text.tag_config('success', foreground=Design.SUCCESS)
        self.log_text.tag_config('error', foreground=Design.ERROR)
        self.log_text.tag_config('warning', foreground=Design.WARNING)
        self.log_text.tag_config('info', foreground=Design.TEXT_DIM)

    def _build_banner(self, parent):
        """顶部品牌横幅：logo 头像 + 主副标题 + 右侧打赏胶囊按钮。"""
        bar = ctk.CTkFrame(parent, fg_color=Design.BG_CARD,
                           corner_radius=Design.R_MD, height=76)
        bar.pack(fill=ctk.X, pady=(0, Design.S_SM))
        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.pack(side=ctk.LEFT, fill=ctk.X, expand=True, padx=Design.S_MD, pady=Design.S_SM)

        # 品牌 logo：优先用圆形透明头像（assets/logo_round.png），
        # 之前这里是个 ⬡ 文字符号，多数系统字体渲染不出来 → 看起来"logo 没了"。
        logo_done = False
        try:
            logo = self._brand_logo()
            if logo is not None:
                ctk.CTkLabel(left, text="", image=logo).pack(side=ctk.LEFT)
                logo_done = True
        except Exception:
            pass
        if not logo_done:
            # 兜底：仅当图片加载失败才退回文字符号
            ctk.CTkLabel(left, text="⬡", font=("Segoe UI", 24),
                         text_color=Design.ACCENT_HVR, width=Design.LOGO_PX).pack(side=ctk.LEFT)

        titles = ctk.CTkFrame(left, fg_color="transparent")
        titles.pack(side=ctk.LEFT, padx=(Design.S_MD, 0))
        ctk.CTkLabel(titles, text="nho有手就行", font=Design.font('title'),
                     text_color=Design.TEXT, anchor='w').pack(anchor='w')
        ctk.CTkLabel(titles, text="自动化 · 符文 · 海克斯 · 玩法推荐",
                     font=Design.font('small'), text_color=Design.TEXT_DIM, anchor='w').pack(anchor='w')

        ctk.CTkButton(bar, text="打赏", font=Design.font('small'),
                      fg_color=Design.GOLD, hover_color="#DDB871",
                      text_color=Design.ACCENT_TEXT, width=76, height=32,
                      corner_radius=Design.R_PILL,
                      command=self._show_donate_dialog).pack(side=ctk.RIGHT, padx=Design.S_MD)

    def _brand_logo(self):
        """加载并缓存圆形品牌 logo（CTkImage）。"""
        if getattr(self, "_brand_logo_img", None) is not None:
            return self._brand_logo_img
        px = Design.LOGO_PX
        for name in ("logo_round_192.png", "logo_round.png", "logo.png", "icon.png"):
            path = self._asset_path(name)
            if not os.path.exists(path):
                continue
            try:
                img = Image.open(path).convert("RGBA")
                img = img.resize((px * 2, px * 2), Image.Resampling.LANCZOS)
                self._brand_logo_img = ctk.CTkImage(
                    light_image=img, dark_image=img, size=(px, px))
                return self._brand_logo_img
            except Exception:
                continue
        return None

    # ==================== 图标 ====================

    def _asset_path(self, *parts):
        return os.path.join(BASE_DIR, "assets", *parts)

    def _set_window_icon(self, window):
        ico = self._asset_path("icon.ico")
        png = self._asset_path("icon.png")
        logo = self._asset_path("logo.png")
        try:
            if os.path.exists(ico) and os.name == "nt":
                window.after(200, lambda: window.iconbitmap(ico))
        except Exception:
            pass
        for cand in (logo, png):
            if not cand or not os.path.exists(cand):
                continue
            try:
                img = Image.open(cand).convert("RGBA")
                img.thumbnail((64, 64), Image.Resampling.LANCZOS)
                photo = ctk.CTkImage(light_image=img, dark_image=img, size=(32, 32))
                window._icon_photo_ref = photo
                self._icon_photo = photo
                break
            except Exception:
                pass

    # ==================== 打赏对话框 ====================

    def _show_donate_dialog(self):
        dlg = ctk.CTkToplevel(self.root)
        dlg.title("打赏支持")
        dlg.configure(fg_color=Design.BG)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        self._set_window_icon(dlg)

        ctk.CTkLabel(dlg, text="支持作者，让工具更完美",
                     font=Design.font('h2'), text_color=Design.TEXT).pack(pady=(Design.S_LG, Design.S_SM))

        donate_path = self._asset_path("donate.jpg")
        if not os.path.exists(donate_path):
            donate_path = self._asset_path("donate.png")
        self._donate_photo = None
        if os.path.exists(donate_path):
            try:
                img = Image.open(donate_path)
                img.thumbnail((360, 360), Image.Resampling.LANCZOS)
                self._donate_photo = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
                ctk.CTkLabel(dlg, image=self._donate_photo, text="").pack(pady=(0, Design.S_SM))
            except Exception as e:
                ctk.CTkLabel(dlg, text=f"(无法加载打赏图片: {e})",
                             font=Design.font('small'), text_color=Design.TEXT_DIM).pack(pady=(0, Design.S_SM))
        else:
            ctk.CTkLabel(dlg, text="(未找到 assets/donate.jpg)",
                         font=Design.font('small'), text_color=Design.TEXT_DIM).pack(pady=(0, Design.S_SM))

        ctk.CTkLabel(dlg, text="感谢支持 · 打赏纯属自愿",
                     font=Design.font('small'), text_color=Design.TEXT_DIM).pack(pady=(0, Design.S_SM))
        ctk.CTkButton(dlg, text="关闭", font=Design.font('small'),
                      fg_color=Design.BG_SEG, hover_color=Design.BG_CARD_HVR,
                      text_color=Design.TEXT, corner_radius=Design.R_SM,
                      command=dlg.destroy).pack(fill=ctk.X, padx=Design.S_LG, pady=(0, Design.S_LG))

        dlg.update_idletasks()
        w = max(400, dlg.winfo_reqwidth())
        h = dlg.winfo_reqheight()
        x = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_y() + max(0, (self.root.winfo_height() - h)) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")

    # ==================== 延迟段 ====================

    def _on_delay_select_idx(self, choice):
        m = {"立即": 0, "3s": 3, "5s": 5, "10s": 10}
        self._on_delay_select(m.get(choice, 5))

    def _refresh_delay_buttons(self):
        m = {0: "立即", 3: "3s", 5: "5s", 10: "10s"}
        try:
            self._delay_seg.set(m.get(self._delay_value, "5s"))
        except Exception:
            pass

    def _on_delay_select(self, seconds):
        seconds = normalize_delay(seconds)
        self._delay_value = seconds
        self.settings["auto_accept_delay"] = seconds
        self._save_settings()
        self._refresh_delay_buttons()
        if self.controller and self.engine_running:
            self.controller.update_setting("auto_accept_delay", seconds)
        labels = {0: "立即", 3: "3秒", 5: "5秒", 10: "10秒"}
        self._log(f"⏱ 执行延迟已设为 {labels.get(seconds, str(seconds))}")

    def _set_countdown_ui(self, seconds, label=""):
        if seconds is None:
            self.countdown_var.set("")
            if self._countdown_packed:
                self.countdown_label.pack_forget()
                self._countdown_packed = False
            return
        self.countdown_var.set(f"⏳ {label}  「{int(seconds)}…」")
        if not self._countdown_packed:
            self.countdown_label.pack(fill=ctk.X, pady=(Design.S_SM, 0))
            self._countdown_packed = True

    # ==================== 数据加载 ====================

    def _load_data(self):
        def _load():
            try:
                from main import DataManager
                self.dm = DataManager()
                if self.dm.hero_data:
                    self._log(f"✅ 数据加载完毕: {len(self.dm.hero_data)} 个英雄")
                    self.gui_queue.put({"event": "data_loaded"})
                else:
                    self._log("❌ 未加载到英雄数据，请检查 data/ 目录")
                    self.gui_queue.put({"event": "data_error"})
            except Exception as e:
                self._log(f"❌ 数据加载失败: {e}")
                self.gui_queue.put({"event": "data_error"})
        self._log("正在加载数据资源...")
        threading.Thread(target=_load, daemon=True).start()

    # ==================== 引擎控制 ====================

    def _set_power_mode(self, mode):
        try:
            self.power_btn.set_mode(mode)
            self.power_caption.configure(text={
                "idle": "开始识别", "starting": "启动中…", "running": "点击停止",
            }.get(mode, ""))
        except Exception:
            pass

    def _toggle_engine(self):
        if self.engine_running:
            self._stop_engine()
        else:
            self._start_engine()

    def _start_engine(self):
        if self.engine_running:
            return
        if not self.dm or not self.dm.hero_data:
            messagebox.showwarning("提示", "数据尚未加载完成，请稍候")
            return
        self._set_power_mode("starting")
        self.start_btn.config(state=tk.DISABLED)
        self._set_status("启动中...", Design.WARNING)
        self._log("正在初始化 OCR 引擎...")

        def _init():
            try:
                from main import GameAnalyzer, OverlayApp
                from scripts.lcu_connector import LCUConnector
                self.analyzer = GameAnalyzer(self.dm)
                self._log("✅ OCR 引擎就绪")
                champions_json = os.path.join(self.dm.data_dir, 'champions.json')
                self.lcu = LCUConnector(champions_json)
                self.rune_service.set_lcu(self.lcu)
                self._log("✅ LCU 连接器就绪")
                self.gui_queue.put({"event": "create_overlay"})
            except Exception as e:
                self._log(f"❌ 引擎启动失败: {e}")
                traceback.print_exc()
                self.gui_queue.put({"event": "engine_error"})
        threading.Thread(target=_init, daemon=True).start()

    def _create_overlay_and_start(self):
        try:
            from main import OverlayApp
            self.overlay_window = tk.Toplevel(self.root)
            self.overlay = OverlayApp(self.overlay_window, self.overlay_queue,
                                      on_manual_refresh=self._manual_refresh_ocr)
            try:
                self.overlay_queue.put({"cmd": "FAB_HIDE"})
            except Exception:
                pass
            self.rune_service.set_lcu(self.lcu)
            self.controller = GUIController(
                self.overlay_queue, self.gui_queue,
                self.dm, self.analyzer, self.lcu,
                settings=self.settings, rune_service=self.rune_service)
            self.controller.start()
            self.engine_running = True
            self._set_power_mode("running")
            self.start_btn.config(state=tk.NORMAL)
            self._set_status("运行中", Design.SUCCESS)
            self._log("✅ 引擎已启动! 进入对局且右上角读秒出现后再识别海克斯；选人阶段仍可推荐/套用符文")
            if self.settings.get("overlay_topmost", True) and self.overlay_window:
                try:
                    self.overlay_window.attributes("-topmost", True)
                except Exception:
                    pass
            self._start_pulse()
            self.tray.start()
        except Exception as e:
            self._log(f"❌ Overlay 创建失败: {e}")
            traceback.print_exc()
            self._engine_cleanup()
            self._set_power_mode("idle")
            self.start_btn.config(state=tk.NORMAL)

    def _stop_engine(self):
        self._log("正在停止引擎...")
        self._engine_cleanup()
        self._set_power_mode("idle")
        self.start_btn.config(state=tk.NORMAL)
        self._set_status("已停止", Design.TEXT_DIM)
        self.hero_var.set("—")
        self._log("引擎已停止")

    def _engine_cleanup(self):
        self.engine_running = False
        if self.controller:
            self.controller.stop()
            self.controller = None
        if self.overlay_window:
            try:
                self.overlay_window.destroy()
            except Exception:
                pass
            self.overlay_window = None
            self.overlay = None
        self.tray.stop()

    # ==================== 数据更新 ====================

    def _show_update_dialog(self):
        UpdateDialog(self)

    def _run_update(self, mode, hero_names=None):
        self.update_btn.configure(state='disabled')
        mode_labels = {'spot_check': '🔍 抽样校验', 'smart': '🧠 智能增量',
                       'full': '🔄 全量更新', 'precise': '🎯 精确更新', 'github': '📥 GitHub 下载'}
        self._log(f"{mode_labels.get(mode, mode)} 开始...")

        def _run():
            try:
                if mode == 'github':
                    from scripts.updater import download_from_github
                    success = download_from_github(log_func=self._log_safe)
                elif mode == 'precise' and hero_names:
                    from scripts.updater import update_specific_heroes
                    success = update_specific_heroes(hero_names, log_func=self._log_safe)
                else:
                    from scripts.updater import run_update
                    success = run_update(mode=mode, log_func=self._log_safe)
                if success:
                    self._log("✅ 更新完成!")
                    self.gui_queue.put({"event": "reload_data"})
                else:
                    self._log("⚠ 更新完成 (部分失败)")
            except Exception as e:
                self._log(f"❌ 更新失败: {e}")
                traceback.print_exc()
            finally:
                self.gui_queue.put({"event": "update_done"})
        threading.Thread(target=_run, daemon=True).start()

    def _auto_check_official_augments(self):
        def _run():
            try:
                from scripts.updater import auto_check_official_augments
                if auto_check_official_augments(log_func=self._log_safe):
                    self.gui_queue.put({"event": "reload_data"})
            except Exception as e:
                try:
                    self._log_safe(f"⚠ 官方库自检异常（忽略）: {e}")
                except Exception:
                    pass
        threading.Thread(target=_run, daemon=True).start()

    # ==================== 托盘 ====================

    def _minimize_to_tray(self):
        if not self.engine_running:
            messagebox.showinfo("提示", "请先点击「开始识别」再最小化到托盘")
            return
        self.root.withdraw()
        self.tray.notify("nho有手就行", "程序已最小化到系统托盘，可从托盘恢复主界面")
        if self.overlay_window:
            self.root.after(100, self._ensure_overlay_visible)

    def _ensure_overlay_visible(self):
        if self.overlay_window:
            try:
                self.overlay_window.deiconify()
                self.overlay_window.attributes("-topmost", True)
            except Exception:
                pass
        if self.overlay:
            try:
                self.overlay.ensure_fab_visible()
            except Exception:
                pass

    def _restore_from_tray(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    # ==================== 队列轮询 ====================

    def _poll_queues(self):
        try:
            while True:
                self._handle_gui_message(self.gui_queue.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                self._append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queues)

    def _handle_gui_message(self, msg):
        event = msg.get("event", "")
        if event == "data_loaded":
            self.start_btn.config(state=tk.NORMAL)
        elif event == "data_error":
            self.start_btn.config(state=tk.DISABLED)
        elif event == "create_overlay":
            self._create_overlay_and_start()
        elif event == "engine_error":
            self._stop_engine()
        elif event == "hero_found":
            hero = msg.get("hero", ""); source = msg.get("source", "")
            self.hero_var.set(hero or "—")
            self._set_status("监听中", Design.SUCCESS)
            if hero:
                self.tray.notify("英雄已识别", f"当前英雄: {hero}")
        elif event == "hero_cleared":
            self.hero_var.set(msg.get("label") or "等待识别本局英雄")
            self.rune_info_var.set("符文: 等待识别本局英雄")
        elif event == "hero_confirmed":
            self.hero_var.set(msg.get("hero", "") or "—")
        elif event == "status":
            status = msg.get("status", "")
            if "hero" in msg:
                hero = msg.get("hero")
                self.hero_var.set(hero if hero else "等待识别本局英雄")
            m = {"connecting": ("连接客户端...", Design.WARNING),
                 "waiting": ("等待选取英雄...", Design.WARNING),
                 "listening": ("监听中", Design.SUCCESS),
                 "analyzing": ("分析中...", Design.ACCENT),
                 "analyzed": ("分析完成", Design.SUCCESS),
                 "refreshing": ("刷新英雄...", Design.WARNING),
                 "no_hero_warning": ("未锁定英雄", Design.ERROR),
                 "idle": ("运行中 (无英雄)", Design.TEXT_DIM),
                 "resetting": ("重置中...", Design.WARNING)}
            if status in m:
                self._set_status(*m[status])
        elif event == "tray_show":
            self._restore_from_tray()
        elif event == "tray_quit":
            self._quit_app()
        elif event == "update_done":
            self.update_btn.configure(state='normal')
        elif event == "reload_data":
            self._log("重新加载数据...")
            self._load_data()
        elif event == "hex_refresh_reminder":
            self._flash_hex_refresh_banner()
        elif event == "hex_results":
            self.hex_result_var.set(msg.get("data", "") or "")
            self._sync_hex_card_visibility()
        elif event == "log":
            self._log(msg.get("text", ""))
        elif event == "rune_info":
            info = msg.get("text", "")
            if msg.get("cleared"):
                self.rune_info_var.set(info or "符文: 等待识别本局英雄")
            else:
                first = info.splitlines()[0] if info else ""
                self.rune_info_var.set(first or "符文推荐已更新")
        elif event == "countdown":
            self._set_countdown_ui(msg.get("seconds"), msg.get("label") or "")

    # ==================== 手动英雄 ====================

    def _manual_set_hero(self):
        query = self.hero_entry.get().strip()
        if not query:
            return
        if not self.dm or not self.dm.hero_data:
            self._log("❌ 数据未加载")
            return
        matches, is_exact = self.dm.search_hero(query)
        if not matches:
            self._log(f"❌ 未找到英雄: {query}")
            return
        hero_name = matches[0]
        if self.controller and self.engine_running:
            result = self.controller.set_hero(hero_name)
            if result:
                self._log(f"{'✅ 已锁定' if is_exact else '已匹配'}: {result}")
                self.hero_entry.delete(0, ctk.END)
                self.root.focus()
            else:
                self._log(f"❌ 英雄 [{hero_name}] 不在数据库中")
        else:
            self._log("⚠ 请先点击「开始识别」")

    # ==================== 设置 / 开关 ====================

    def _load_settings(self):
        return load_settings(SETTINGS_FILE)

    def _save_settings(self):
        save_settings(self.settings, SETTINGS_FILE)

    def _on_toggle(self, key, var):
        value = bool(var.get())
        self.settings[key] = value
        self._save_settings()
        if self.controller and self.engine_running:
            self.controller.update_setting(key, value)
        labels = {"auto_accept": "自动接受", "auto_ready": "自动开始/准备",
                  "auto_hex": "自动海克斯识别", "auto_apply_runes": "套用符文"}
        self._log(f"{'✅ 开启' if value else '⏸ 关闭'} {labels.get(key, key)}")

    def _manual_refresh_ocr(self):
        if not self.controller or not self.engine_running:
            self._log("⚠ 请先点击「开始识别」"); return
        self._log("点击刷新识别…")
        threading.Thread(target=self.controller.trigger_analyze, daemon=True).start()

    def _manual_refresh_hero(self):
        if not self.controller or not self.engine_running:
            self._log("⚠ 请先点击「开始识别」"); return
        self._log("点击识别英雄…")
        threading.Thread(target=self.controller.trigger_refresh_hero, daemon=True).start()

    def _manual_reset(self):
        if not self.controller or not self.engine_running:
            self._log("⚠ 请先点击「开始识别」"); return
        self._log("点击重置：重新自动检测英雄…")
        self.controller.request_reset()

    def _manual_apply_runes(self):
        if not self.controller or not self.engine_running:
            hero = self.hero_var.get()
            if hero and hero != "—":
                page, source = self.rune_service.recommend(hero)
                self._log(self.rune_service.format_summary(page, source))
                ok, msg = self.rune_service.apply(page)
                self._log(msg)
            else:
                self._log("⚠ 请先锁定英雄并启动引擎")
            return

        def _run():
            ok, msg = self.controller.apply_runes_for_current()
            self.gui_queue.put({"event": "log", "text": ("✅ " if ok else "❌ ") + msg})
        threading.Thread(target=_run, daemon=True).start()

    # ==================== WeGame ====================

    def _find_wegame_path(self, scan_drives=False):
        saved = self.settings.get("wegame_path") or ""
        if saved and os.path.isfile(saved) and _is_wegame_exe(saved):
            return saved
        if winreg is not None:
            for hive_idx, subkey in self._WEGAME_REG_KEYS:
                try:
                    hive = winreg.HKEY_LOCAL_MACHINE if hive_idx == 0 else winreg.HKEY_CURRENT_USER
                    with winreg.OpenKey(hive, subkey) as k:
                        for val_name in self._WEGAME_REG_VALUES:
                            try:
                                v, _ = winreg.QueryValueEx(k, val_name)
                            except OSError:
                                continue
                            for cand in _expand_wegame_candidates(v):
                                if os.path.isfile(cand):
                                    return cand
                except OSError:
                    continue
        for p in _WEGAME_COMMON_PATHS:
            if os.path.isfile(p):
                return p
        if scan_drives:
            for drive in _list_fixed_drives():
                base = drive + "\\"
                for rel in ("WeGame\\wegame.exe", "wegame\\wegame.exe",
                            "游戏\\WeGame\\wegame.exe", "网络游戏\\WeGame\\wegame.exe",
                            "Program Files (x86)\\WeGame\\wegame.exe",
                            "Program Files\\WeGame\\wegame.exe"):
                    cand = os.path.join(base, rel)
                    if os.path.isfile(cand):
                        return cand
                for d in _list_dirs(base):
                    cand = os.path.join(d, "WeGame", "wegame.exe")
                    if os.path.isfile(cand):
                        return cand
        return None

    _WEGAME_REG_KEYS = [
        (0, r"SOFTWARE\WOW6432Node\Tencent\WeGame"),
        (0, r"SOFTWARE\Tencent\WeGame"),
        (1, r"Software\Tencent\WeGame"),
        (1, r"Software\Tencent\wegame"),
    ]
    _WEGAME_REG_VALUES = ("InstallPath", "Path", "InstallDir", "WeGamePath",
                          "WegamePath", "wegame.exe", "InstallDir64")

    def _start_wegame_exe(self, path):
        try:
            os.startfile(path)
            self._log(f"🚀 已启动 WeGame: {path}")
            self._set_status("WeGame 已启动", Design.SUCCESS)
        except Exception as e:
            self._log(f"❌ 启动 WeGame 失败: {e}")
            messagebox.showerror("启动失败", f"无法启动 WeGame:\n{e}")

    def _launch_wegame(self):
        path = self._find_wegame_path(scan_drives=False)
        if path:
            self._start_wegame_exe(path); return
        self._log("⏳ 快速查找未命中，正在扫描磁盘定位 WeGame…")

        def _worker():
            found = self._find_wegame_path(scan_drives=True)
            if found:
                try:
                    self.settings["wegame_path"] = found; self._save_settings()
                except Exception:
                    pass
                self.root.after(0, lambda: (self._update_wegame_hint(), self._start_wegame_exe(found)))
            else:
                self.root.after(0, self._prompt_manual_wegame)
        threading.Thread(target=_worker, daemon=True).start()

    def _prompt_manual_wegame(self):
        self._update_wegame_hint()
        if messagebox.askyesno("未找到 WeGame",
                "自动查找未找到 WeGame。\n\n是否手动选择 wegame.exe 的位置？"):
            self._choose_wegame_path(launch_after=True)

    def _choose_wegame_path(self, launch_after=False):
        initial = self.settings.get("wegame_path") or ""
        path = filedialog.askopenfilename(
            title="选择 WeGame 启动程序 (wegame.exe)",
            initialdir=os.path.dirname(initial) if initial else "C:\\",
            filetypes=[("WeGame 程序", "*.exe"), ("所有文件", "*.*")])
        if not path:
            return
        if not _is_wegame_exe(path):
            self._log("⚠ 选择的不是 wegame.exe，请重新选择")
            messagebox.showwarning("路径无效", "请选择 WeGame 的可执行文件 wegame.exe")
            return
        self.settings["wegame_path"] = path
        try:
            self._save_settings()
        except Exception:
            pass
        self._update_wegame_hint()
        self._log(f"✅ 已记录 WeGame 路径: {path}")
        if launch_after:
            self._start_wegame_exe(path)

    def _update_wegame_hint(self):
        p = self.settings.get("wegame_path") or ""
        if p and os.path.isfile(p):
            self.wegame_hint_var.set(f"WeGame 路径: {p}"); return
        saved = self._find_wegame_path(scan_drives=False)
        if saved:
            try:
                self.settings["wegame_path"] = saved; self._save_settings()
            except Exception:
                pass
            self.wegame_hint_var.set(f"已自动找到 WeGame: {saved}"); return
        self.wegame_hint_var.set("未找到 WeGame，点「启动 WeGame」自动查找或「选择路径…」手动指定")

    def _create_wegame_shortcut(self):
        path = self.settings.get("wegame_path") or ""
        if not (path and os.path.isfile(path)):
            path = self._find_wegame_path(scan_drives=True)
        if not path:
            if messagebox.askyesno("未找到 WeGame", "自动查找未找到 WeGame，是否手动选择 wegame.exe 位置？"):
                self._choose_wegame_path(launch_after=False)
                path = self.settings.get("wegame_path") or ""
        if not path or not os.path.isfile(path):
            self._log("⚠ 未设置 WeGame 路径，无法创建快捷方式"); return
        try:
            desktop = _get_desktop_path()
            lnk = os.path.join(desktop, "启动 WeGame.lnk")
            ps_cmd = ("$ws = New-Object -ComObject WScript.Shell; "
                      "$sc = $ws.CreateShortcut('{}'); $sc.TargetPath = '{}'; "
                      "$sc.WorkingDirectory = '{}'; $sc.Save()").format(
                lnk.replace("'", "''"), path.replace("'", "''"),
                os.path.dirname(path).replace("'", "''"))
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                           check=True, capture_output=True, timeout=30)
            self.settings["wegame_path"] = path
            try:
                self._save_settings()
            except Exception:
                pass
            self._update_wegame_hint()
            self._log(f"✅ 已创建桌面快捷方式: {lnk}")
            messagebox.showinfo("创建成功", f"已在桌面创建「启动 WeGame」快捷方式:\n{lnk}")
        except Exception as e:
            self._log(f"❌ 创建快捷方式失败: {e}")
            messagebox.showerror("创建失败", f"无法创建桌面快捷方式:\n{e}")

    # ==================== UI 辅助 ====================

    def _sync_hex_card_visibility(self):
        try:
            has = bool(self.hex_banner_var.get() or self.hex_result_var.get())
            visible = self._hex_card.winfo_manager() != ""
            if has and not visible:
                self._hex_card.pack(fill=ctk.X, pady=(0, Design.S_SM), before=self._footer)
            elif not has and visible:
                self._hex_card.pack_forget()
        except Exception:
            pass

    def _flash_hex_refresh_banner(self):
        msg = "刷新后已更新推荐"
        self._set_status(msg, Design.GOLD)
        self._log(f"✅ {msg}")
        try:
            self.hex_banner_var.set(f"✨ {msg}")
            self._sync_hex_card_visibility()
            if self._hex_banner_clear_after is not None:
                try:
                    self.root.after_cancel(self._hex_banner_clear_after)
                except Exception:
                    pass
            self._hex_banner_clear_after = self.root.after(
                3500, lambda: (self.hex_banner_var.set(""), self._sync_hex_card_visibility()))
        except Exception:
            pass
        try:
            if self.tray:
                self.tray.notify("海克斯推荐", msg)
        except Exception:
            pass

    def _set_status(self, text, color):
        self.status_var.set(text)
        self.status_label.configure(text_color=color)
        self.status_dot.configure(text_color=color)
        self.status_color = color

    def _start_pulse(self):
        """运行中状态点的呼吸动画：在 token 色的明暗之间往返，不改色相。"""
        def _pulse():
            if not self.engine_running:
                return
            self._pulse_state = (self._pulse_state + 1) % 20
            brightness = abs(self._pulse_state - 10) / 10.0
            if self.status_color == Design.SUCCESS:
                # 以 Design.SUCCESS 为基准整体提亮，避免硬编码 RGB 漂移到别的色相
                base = Design.SUCCESS.lstrip("#")
                r0, g0, b0 = (int(base[i:i + 2], 16) for i in (0, 2, 4))
                f = brightness * 0.35
                r = min(255, int(r0 + (255 - r0) * f))
                g = min(255, int(g0 + (255 - g0) * f))
                b = min(255, int(b0 + (255 - b0) * f))
                self.status_dot.configure(text_color=f"#{r:02x}{g:02x}{b:02x}")
            self.root.after(100, _pulse)
        _pulse()

    def _log(self, msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._append_log(f"[{ts}] {msg}")

    def _log_safe(self, msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {msg}")

    def _append_log(self, text):
        self.log_text.configure(state='normal')
        tag = "info"
        if "✅" in text or "成功" in text:
            tag = "success"
        elif "❌" in text or "失败" in text or "错误" in text:
            tag = "error"
        elif "⚠" in text or "警告" in text:
            tag = "warning"
        self.log_text.insert(ctk.END, text + "\n", tag)
        self.log_text.see(ctk.END)
        self.log_text.configure(state='disabled')

    # ==================== 窗口事件 ====================

    def _on_close(self):
        if self.engine_running:
            if messagebox.askyesno("关闭确认",
                    "引擎正在运行中。\n\n• 点击「是」关闭程序\n• 点击「否」最小化到托盘"):
                self._quit_app()
            else:
                self._minimize_to_tray()
        else:
            self._quit_app()

    def _quit_app(self):
        self._engine_cleanup()
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    def run(self):
        self.root.mainloop()


# ============================================================================
# 入口
# ============================================================================

def _check_admin():
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def make_std_streams_safe():
    import io
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


def safe_print(msg):
    try:
        print(msg)
    except Exception:
        pass


def main():
    try:
        make_std_streams_safe()
    except Exception:
        pass
    try:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:
            from scripts.config import lower_process_priority
            if lower_process_priority():
                safe_print("[OK] 已把本进程降为低优先级（让 CPU 优先给游戏）")
        except Exception as e:
            safe_print(f"[WARN] 降优先级步骤跳过: {e}")
        if not _check_admin():
            import ctypes
            result = messagebox.askyesno(
                "权限提示",
                "⚠ 当前未以管理员身份运行。\n\n以下功能可能无法正常工作:\n"
                "• 自动识别英雄联盟客户端 (LCU / lockfile)\n\n"
                "点击「是」以管理员身份重新启动\n"
                "点击「否」继续以普通用户运行")
            if result:
                try:
                    exe = sys.executable
                    ctypes.windll.shell32.ShellExecuteW(
                        None, "runas", exe,
                        " ".join(sys.argv) if getattr(sys, 'frozen', False) else f'"{sys.argv[0]}"',
                        None, 1)
                    sys.exit(0)
                except Exception:
                    pass
        app = CTkLauncherApp()
        app.run()
    except Exception as e:
        try:
            messagebox.showerror("nho有手就行 - 启动错误",
                                 f"程序启动时发生错误:\n\n{traceback.format_exc()}")
        except Exception:
            safe_print(f"FATAL: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
