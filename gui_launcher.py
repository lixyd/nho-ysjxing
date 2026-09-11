"""
nho有手就行 - GUI 启动器
独立 EXE 入口点，提供图形化界面与系统托盘支持
"""
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import threading
import queue
import os
import sys
import io
import time
import datetime
import math
import traceback
import subprocess

# WeGame 注册表探测（Windows）；非 Windows 环境置空
try:
    import winreg
except ImportError:
    winreg = None

# ============ 路径初始化 (兼容 PyInstaller 打包) ============

from scripts.config import (
    get_base_dir, BASE_DIR, SETTINGS_FILE, DEFAULT_SETTINGS,
    AUTO_DELAY_CHOICES, load_settings, save_settings, normalize_delay,
)
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

# ============ 延迟导入 (需要 path 已设置) ============

from PIL import Image, ImageDraw
import pystray
import json

from scripts.matchmaking import MatchmakingService
from scripts.runes import RuneService
from scripts.auto_hex import AutoHexWatcher


# ============ 统一配色方案 ============

class Theme:
    """Apple 风格色板：浅灰底 + 白卡片 + 苹果蓝点缀。

    对应 Apple HIG：背景 #F5F5F7、分隔 #D2D2D7、正文 #1D1D1F、
    次要 #86868B、强调蓝 #0071E3、成功绿 #34C759、警示橙 #FF9500、错误红 #FF3B30。
    """
    MINT        = "#34C759"   # 成功绿（保留属性名，值换苹果绿）
    MINT_D      = "#1D1D1F"   # 标题黑
    SKY         = "#0071E3"   # 苹果蓝
    SKY_D       = "#0071E3"
    VIOLET      = "#5E5CE6"   # 靛蓝
    VIOLET_D    = "#5E5CE6"

    BG          = "#F5F5F7"   # Apple 浅灰底
    BG_CARD     = "#FFFFFF"   # 白卡片
    BG_INPUT    = "#FFFFFF"
    BG_SEG      = "#E8E8ED"   # 分段控件底
    ACCENT      = "#0071E3"   # 苹果蓝（主按钮）
    ACCENT_HVR  = "#0077ED"
    ACCENT_DIM  = "#D2D2D7"   # 分隔线
    SUCCESS     = "#34C759"
    WARNING     = "#FF9500"
    ERROR       = "#FF3B30"
    TEXT        = "#1D1D1F"
    TEXT_DIM    = "#86868B"
    BORDER      = "#D2D2D7"
    GOLD_GLOW   = "#0071E3"   # 打赏键同主色（苹果风不搞花哨金色）


# ================= 日志重定向 =================

class LogRedirector(io.TextIOBase):
    """将 stdout/stderr 重定向到 Queue, 供 GUI 日志面板使用"""

    def __init__(self, log_queue, original_stream=None):
        super().__init__()
        self.log_queue = log_queue
        self.original = original_stream

    def write(self, text):
        if text and text.strip():
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self.log_queue.put(f"[{ts}] {text.rstrip()}")
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


# ================= 后台控制器 (替代 InputController) =================

class GUIController(threading.Thread):
    """后台引擎: LCU 自动检测 + UI 按钮动作 + 匹配/海克斯自动化"""

    def __init__(self, overlay_queue, gui_queue, data_manager, analyzer, lcu_connector,
                 settings=None, rune_service=None):
        super().__init__(daemon=True)
        self.overlay_queue = overlay_queue
        self.gui_queue = gui_queue
        self.dm = data_manager
        self.analyzer = analyzer
        self.lcu = lcu_connector
        self.settings = settings if settings is not None else dict(DEFAULT_SETTINGS)
        self.rune_service = rune_service or RuneService(lcu_connector)
        self.current_hero = None
        self.running = True
        self._reset_requested = False  # UI「重置」→ 引擎线程处理
        self._last_phase = None
        self._last_hero_poll = 0.0
        self._fast_poll_until = 0.0  # 识别英雄 / ChampSelect 后加速轮询
        self._champ_select_locked = False  # championId>0 才算锁定
        self._runes_applied_on_lock = False
        self._hex_ingame = False  # InProgress + Live Client 后才允许 hex OCR / 显示 FAB
        self._last_hex_gate_poll = 0.0

        self.matchmaking = MatchmakingService(
            lcu_connector,
            flags={
                "auto_accept": self.settings.get("auto_accept", True),
                "auto_ready": self.settings.get("auto_ready", True),
                "auto_accept_delay": normalize_delay(
                    self.settings.get("auto_accept_delay", 5)
                ),
            },
            on_event=lambda m: self.gui_queue.put({"event": "log", "text": m}),
            on_countdown=lambda sec, label: self.gui_queue.put({
                "event": "countdown", "seconds": sec, "label": label,
            }),
        )
        self.auto_hex = AutoHexWatcher(
            lcu=lcu_connector,
            analyzer=analyzer,
            get_hero=lambda: self.current_hero,
            on_results=self._on_hex_results,
            on_status=lambda m: self.gui_queue.put({"event": "log", "text": m}),
            on_refresh_reminder=self._on_hex_refresh_reminder,
            on_invalidate=self._on_hex_invalidate,
            enabled=self.settings.get("auto_hex", True),
        )

    def run(self):
        """主循环: 自动检测 → 监听"""
        self.matchmaking.start()
        while self.running:
            self._auto_detect_phase()
            self._listening_phase()
        self.matchmaking.stop()

    def stop(self):
        self.running = False
        try:
            self.matchmaking.stop()
        except Exception:
            pass

    def update_setting(self, key, value):
        if key == "auto_accept_delay":
            delay = normalize_delay(value)
            self.settings[key] = delay
            self.matchmaking.set_flag(key, delay)
            return
        self.settings[key] = bool(value)
        if key in ("auto_accept", "auto_ready"):
            self.matchmaking.set_flag(key, value)
        elif key == "auto_hex":
            self.auto_hex.set_enabled(value)

    def _gui(self, **kwargs):
        """发送消息到 GUI"""
        self.gui_queue.put(kwargs)

    def _on_hex_results(self, results):
        self.overlay_queue.put({"cmd": "UPDATE", "data": results})
        self._gui(event="status", status="analyzed", hero=self.current_hero)
        self._gui(event="hex_results", data=self._format_hex_results(results))

    def _format_hex_results(self, results):
        """把海克斯识别结果格式化为主界面卡片文本（仅显示有效识别项）。"""
        if not results:
            return ""
        lines = []
        for key in ("hex_1", "hex_2", "hex_3"):
            v = results.get(key)
            if not v:
                continue
            if v.get("valid"):
                text = str(v.get("text", "")).replace("\n", " ").strip()
                rank = v.get("overall_rank") or v.get("t_rank")
                best = v.get("highlight")
                seg = f"● {text}"
                if rank:
                    seg += f"  (总No.{rank})"
                if best:
                    seg += "  ⭐推荐"
                lines.append(seg)
            elif v.get("error"):
                lines.append("○ 未识别")
        # 玩法提示：赌狗玩法（随机海克斯）> 热门路线（万剑归宗等）> 流派
        for skey in ("_gamble", "_route", "_combo"):
            info = results.get(skey)
            if info and info.get("text"):
                lines.append("")
                lines.append(str(info["text"]))
        return "\n".join(lines) if lines else ""

    def _on_hex_refresh_reminder(self):
        """选项刷新 / 手动「刷新识别」后的 UI 提醒。"""
        self._gui(event="hex_refresh_reminder")

    def _on_hex_invalidate(self):
        """选项已变化：立刻把遮罩上的旧推荐换成「识别中…」。

        不做这一步的话，从"检测到变化"到"重新识别完成"之间那段时间，
        遮罩上挂着的是**上一轮的名字** —— 看起来就像插件认错了牌。
        """
        try:
            self.overlay_queue.put({"cmd": "PENDING"})
        except Exception:
            pass

    def _is_hex_ocr_allowed(self) -> bool:
        """海克斯 OCR 硬门禁：InProgress + Live Client 真实玩家数据。"""
        if not self.lcu:
            return False
        try:
            if hasattr(self.lcu, "is_in_live_game"):
                return bool(self.lcu.is_in_live_game())
            if self.lcu.get_gameflow_phase() != "InProgress":
                return False
            return self.lcu.get_live_player_state() is not None
        except Exception:
            return False

    def _enter_hex_session(self):
        """对局已开：显示左上角刷新浮钮。"""
        if self._hex_ingame:
            return
        self._hex_ingame = True
        try:
            self.overlay_queue.put({"cmd": "FAB_SHOW"})
        except Exception:
            pass
        self._gui(event="log", text="🎮 已进入对局 — 可识别海克斯（右上角读秒出现后）")

    def _leave_hex_session(self, reason: str = "对局结束"):
        """离开对局：停 OCR、清遮罩推荐、隐藏 FAB。"""
        was = self._hex_ingame
        self._hex_ingame = False
        try:
            if self.auto_hex:
                self.auto_hex.reset_match()
        except Exception:
            pass
        try:
            self.overlay_queue.put({"cmd": "CLEAR"})
            self.overlay_queue.put({"cmd": "FAB_HIDE"})
            self.overlay_queue.put({"cmd": "RUNE_STRIP", "data": ""})
            self.overlay_queue.put({"cmd": "ITEM_STRIP", "data": ""})
        except Exception:
            pass
        self._gui(event="hex_results", data="")
        if was:
            self._gui(event="log", text=f"⏹ {reason}，已停止海克斯识别并清空推荐")

    def _sync_hex_ingame_gate(self):
        """同步门禁（限频）：进入则亮 FAB；离开则清推荐/藏 FAB。"""
        now = time.time()
        # 未在局内时稍慢探测；已在局内仍要及时发现 Live 断开
        interval = 0.5 if self._hex_ingame else 0.8
        if now - self._last_hex_gate_poll < interval:
            return
        self._last_hex_gate_poll = now
        allowed = self._is_hex_ocr_allowed()
        if allowed:
            self._enter_hex_session()
        elif self._hex_ingame:
            self._leave_hex_session("已离开对局")

    def _validate_hero(self, name):
        """验证英雄名是否在数据库中，尝试模糊映射"""
        return self.dm.validate_hero(name)

    def _try_auto_detect(self, verbose=False):
        if not self.lcu:
            if verbose:
                print("⚠ LCU 连接器未初始化")
            return None, ""
        
        # 先尝试连接
        if not self.lcu.is_connected():
            if verbose:
                print("尝试连接 LCU...")
            connected = self.lcu.connect()
            if verbose:
                if connected:
                    print(f"✅ LCU 已连接 (端口: {self.lcu.port})")
                else:
                    print("⚠ LCU 未连接 (客户端可能未启动或需要管理员权限)")
        
        hero, source = self.lcu.get_champion_auto()
        if verbose and not hero:
            phase = self.lcu.get_gameflow_phase() if self.lcu.is_connected() else None
            if phase:
                print(f"   当前阶段: {phase} (未检测到英雄)")
            else:
                print("   未获取到游戏阶段信息")
        
        if hero:
            validated = self._validate_hero(hero)
            if validated:
                return validated, source
            elif verbose:
                print(f"⚠ 英雄 [{hero}] 不在数据库中")
        return None, source

    def _clear_hero_dependent_state(self):
        """英雄切换时清空海克斯叠加层与自动海克斯选项缓存，并刷新符文提示。"""
        try:
            self.overlay_queue.put({"cmd": "CLEAR"})
        except Exception:
            pass
        if self.auto_hex:
            try:
                self.auto_hex._last_option_snapshot = None
                self.auto_hex._option_change_since = None
                self.auto_hex._ui_was_visible = False
                self.auto_hex._idle_until_next = False
                self.auto_hex._pending_cp = None
                self.auto_hex._pending_reason = None
                self.auto_hex._pick_cycle_active = False
                # 换英雄不清除本局检查点欠账 / _fired（仍由 reset_match 管）
            except Exception:
                pass
        self._gui(event="rune_info", text="", hero=self.current_hero)
        self._push_rune_strip(None)

    def _forget_match_hero(self, reason: str = "", *, force_ui: bool = False):
        """局间清理：丢掉上场英雄，强制下一轮重新识别（不需重启/手动重置）。"""
        had = self.current_hero
        self._champ_select_locked = False
        self._runes_applied_on_lock = False
        self._last_hero_poll = 0.0
        self._fast_poll_until = max(self._fast_poll_until, time.time() + 45)
        if not had and not force_ui:
            return False
        self.current_hero = None
        self._clear_hero_dependent_state()
        waiting = "等待识别本局英雄"
        self._gui(event="hero_cleared", label=waiting)
        self._gui(event="rune_info", text="符文: 等待识别本局英雄", hero=None, cleared=True)
        self._gui(event="status", status="waiting", hero=waiting)
        try:
            self.overlay_queue.put({"cmd": "CLEAR"})
            self.overlay_queue.put({
                "cmd": "STATUS",
                "data": f"{waiting}\n自动接受后将重新识别",
            })
        except Exception:
            pass
        if had:
            msg = f"已清除上场英雄 [{had}]，等待本局识别"
            if reason:
                msg = f"{reason} — {msg}"
            print(msg)
            self._gui(event="log", text=msg)
        return True

    def _apply_hero_change(self, hero, source, *, from_manual=False):
        """统一处理英雄变更：清状态 → 更新 → 刷符文。"""
        old = self.current_hero
        changed = hero != old
        if changed and old:
            print(f"英雄已切换 ({source}): {old} → {hero}")
            self._clear_hero_dependent_state()
        elif changed:
            self._clear_hero_dependent_state()
        self.current_hero = hero
        self._gui(event="hero_found", hero=hero, source=source or ("手动输入" if from_manual else ""))
        self.overlay_queue.put({"cmd": "STATUS", "data": f"当前: {hero}\n点击刷新识别"})
        self._maybe_show_runes(hero)
        return hero

    def set_hero(self, hero_name):
        """手动设置英雄 (供 GUI 调用)"""
        validated = self._validate_hero(hero_name)
        if validated:
            print(f"✅ 已手动锁定英雄: {validated}")
            return self._apply_hero_change(validated, "手动输入", from_manual=True)
        return None

    def trigger_analyze(self):
        """手动刷新识别（UI「刷新识别」按钮 / 左上角 FAB）"""
        if not self._is_hex_ocr_allowed():
            msg = "尚未进入对局"
            hint = "进入对局且右上角读秒出现后再识别海克斯"
            self.overlay_queue.put({"cmd": "STATUS", "data": f"⚠ {msg}\n{hint}"})
            self._gui(event="log", text=f"⚠ {msg} — {hint}")
            self._gui(event="status", status="idle")
            return False
        if not self.current_hero:
            self.overlay_queue.put({"cmd": "STATUS", "data": "⚠ 尚未锁定英雄\n请点击识别英雄"})
            self._gui(event="status", status="no_hero_warning")
            return False
        self._gui(event="status", status="analyzing", hero=self.current_hero)
        self.overlay_queue.put({"cmd": "STATUS", "data": f"🔎 分析 [{self.current_hero}]..."})
        print(f"正在分析: {self.current_hero}...")
        results = self.analyzer.analyze(self.current_hero)
        self.overlay_queue.put({"cmd": "UPDATE", "data": results})
        self._gui(event="status", status="analyzed", hero=self.current_hero)
        self._gui(event="hex_results", data=self._format_hex_results(results))
        print(f"分析完成: {self.current_hero}")
        if self.auto_hex:
            self.auto_hex.notify_manual_refresh(results or {}, skip_snapshot=True)
            self.auto_hex.mark_ui_gone_if_needed(results or {})
        return True

    def trigger_refresh_hero(self):
        """手动识别/刷新英雄（UI「识别英雄」按钮）"""
        now = time.time()
        self._fast_poll_until = now + 20
        self._last_hero_poll = 0  # 立刻再检
        self._gui(event="status", status="refreshing")
        self.overlay_queue.put({"cmd": "STATUS", "data": "刷新英雄..."})
        print("点击识别英雄: 正在从 LCU / Live Client 获取…")
        hero, source = self._try_auto_detect()
        if hero and hero != self.current_hero:
            self._apply_hero_change(hero, source)
            self.overlay_queue.put({"cmd": "STATUS", "data": f"已切换: {hero}\n点击刷新识别"})
        elif hero:
            self._gui(event="hero_confirmed", hero=hero)
            self.overlay_queue.put({"cmd": "STATUS", "data": f"当前: {hero}\n点击刷新识别"})
        else:
            self.overlay_queue.put({
                "cmd": "STATUS",
                "data": f"当前: {self.current_hero or '未知'}\n点击刷新识别",
            })
        return hero

    def request_reset(self):
        """请求重置：由引擎线程在循环中消费，重新进入自动检测。"""
        print("重置: 请求重新进入自动检测阶段")
        self._gui(event="status", status="resetting")
        self._reset_requested = True

    def apply_runes_for_current(self):
        if not self.current_hero:
            return False, "尚未锁定英雄"
        page, source = self.rune_service.recommend(self.current_hero)
        print(self.rune_service.format_summary(page, source))
        ok, msg = self.rune_service.apply(page)
        print(msg)
        self._gui(event="log", text=msg)
        return ok, msg

    def _push_rune_strip(self, hero=None):
        """Update click-through overlay strip from RuneService.recommend."""
        try:
            if not hero:
                self.overlay_queue.put({"cmd": "RUNE_STRIP", "data": ""})
                self.overlay_queue.put({"cmd": "ITEM_STRIP", "data": ""})
                return
            page, _src = self.rune_service.recommend(hero)
            line = self.rune_service.format_strip_line(page)
            self.overlay_queue.put({"cmd": "RUNE_STRIP", "data": line})
            # Items: no data/aram_items.json yet — keep strip cleared (do not fake).
            self.overlay_queue.put({"cmd": "ITEM_STRIP", "data": ""})
        except Exception as e:
            print(f"rune strip push: {e}")

    def _maybe_show_runes(self, hero, *, apply=None):

        """刷新符文展示；apply 默认跟随 auto_apply_runes 设置。"""
        page, source = self.rune_service.recommend(hero)
        summary = self.rune_service.format_summary(page, source)
        print(summary)
        self._gui(event="rune_info", text=summary, hero=hero)
        self._push_rune_strip(hero)
        do_apply = self.settings.get("auto_apply_runes") if apply is None else apply
        if do_apply:
            ok, msg = self.rune_service.apply(page)
            print(msg)
            self._gui(event="log", text=msg)

    def _on_champ_select_lock(self, hero):
        """检测到锁定（championId>0）：再套用一次符文；之后不再定时重套。"""
        if not hero:
            return
        if self._runes_applied_on_lock:
            return
        self._runes_applied_on_lock = True
        # 展示刷新；若开启自动套用则在锁定时再套一次（intent 阶段可能已套过）
        self._maybe_show_runes(hero)

    # ---------- 阶段1: 自动检测英雄 ----------

    def _auto_detect_phase(self):
        if not self.running:
            return
        self._gui(event="status", status="connecting")
        print("正在连接英雄联盟客户端...")
        self._fast_poll_until = time.time() + 30

        for attempt in range(40):  # ChampSelect 加速: ~0.75s * 40 ≈ 30s
            if not self.running:
                return

            # 第一次和每5次详细输出
            verbose = (attempt == 0 or attempt % 5 == 0)
            hero, source = self._try_auto_detect(verbose=verbose)
            if hero:
                print(f"✅ 自动识别到英雄: [{hero}] (来源: {source})")
                self._apply_hero_change(hero, source)
                return

            # UI「重置」可中断自动检测（随后会重新进入本阶段）
            if self._reset_requested:
                self._reset_requested = False
                self.current_hero = None
                self._clear_hero_dependent_state()
                break

            self._gui(event="status", status="waiting", attempt=attempt)
            # ChampSelect 未锁定时更快轮询 pick intent / 骰子
            sleep_s = 0.75
            try:
                if self.lcu and self.lcu.is_connected():
                    phase = self.lcu.get_gameflow_phase()
                    if phase == "ChampSelect":
                        _h, locked = self.lcu.get_champ_select_pick_state()
                        sleep_s = 0.55 if not locked else 0.9
                    elif phase in ("InProgress", "GameStart"):
                        sleep_s = 1.0
                    else:
                        sleep_s = 1.2
            except Exception:
                pass
            time.sleep(sleep_s)

        # 超时未检测到
        print("暂未检测到英雄，可在上方手动输入英雄名")
        print("提示: 如果客户端已打开，请尝试以管理员身份运行本程序")
        self._gui(event="status", status="idle")
        self.overlay_queue.put({"cmd": "STATUS", "data": "暂无英雄\n点击识别英雄或手动输入"})

    # ---------- 阶段2: 监听 / 自动化 ----------

    def _listening_phase(self):
        if not self.running:
            return
        self._gui(event="status", status="listening", hero=self.current_hero)
        print(f"引擎运行中... 当前英雄: {self.current_hero or '未指定'}（点击界面按钮操作）")

        while self.running:
            now = time.time()

            # UI「重置」→ 清空并回到自动检测
            if self._reset_requested:
                self._reset_requested = False
                print("重置: 重新进入自动检测阶段")
                self.current_hero = None
                self._leave_hex_session("重置")
                self._clear_hero_dependent_state()
                time.sleep(0.3)
                return  # 退出 listening_phase, 回到 auto_detect

            # 阶段变化: 进入新对局时重置自动海克斯；结束时清推荐/藏 FAB；局间丢掉上场英雄
            try:
                if self.lcu and self.lcu.is_connected():
                    phase = self.lcu.get_gameflow_phase()
                    if phase != self._last_phase:
                        prev = self._last_phase
                        if phase == "InProgress" and prev != "InProgress":
                            self.auto_hex.reset_match()
                        between_phases = (
                            "EndOfGame", "WaitingForStats", "Lobby", "None",
                            "Matchmaking", "ReadyCheck",
                        )
                        if phase in between_phases or phase in (
                            "ChampSelect", "GameStart",
                        ):
                            if prev == "InProgress" or self._hex_ingame:
                                self._leave_hex_session(
                                    "对局结束" if phase in ("EndOfGame", "WaitingForStats")
                                    else "已离开对局"
                                )
                        # 离开对局 / 回到大厅排队：清除 sticky current_hero
                        if phase in between_phases:
                            if prev in (
                                "InProgress", "GameStart", "ChampSelect",
                                "EndOfGame", "WaitingForStats",
                            ) or self.current_hero:
                                self._forget_match_hero("局间清理")
                        if phase == "ChampSelect":
                            self._fast_poll_until = max(self._fast_poll_until, now + 45)
                            self._champ_select_locked = False
                            self._runes_applied_on_lock = False
                            # 每次进入选人：本局重新识别，勿沿用上场英雄
                            self._forget_match_hero("进入选人", force_ui=True)
                        elif prev == "ChampSelect":
                            # 离开选人：停止按 intent 高频轮询 / 不再定时套符文
                            self._champ_select_locked = False
                            self._runes_applied_on_lock = False
                        self._last_phase = phase
            except Exception:
                pass

            # 硬门禁同步：InProgress+Live 才亮 FAB；Live 断开同样清场
            try:
                self._sync_hex_ingame_gate()
            except Exception as e:
                print(f"[hex_gate] {e}")

            # 周期性英雄重检：ChampSelect 未锁定更快；锁定/离选人后放慢；对局中跟上骰子
            try:
                phase = self._last_phase
                locked = self._champ_select_locked
                if phase == "ChampSelect" and not locked:
                    poll_every = 0.55
                elif phase == "ChampSelect" or now < self._fast_poll_until:
                    poll_every = 1.0
                elif phase in ("InProgress", "GameStart"):
                    poll_every = 2.0
                else:
                    poll_every = 4.0
                if now - self._last_hero_poll >= poll_every:
                    self._last_hero_poll = now
                    hero, source = self._try_auto_detect()
                    # 局间/选人：忽略 GameFlow/Live 上场残留，只认 ChampSelect
                    if phase in (
                        "EndOfGame", "WaitingForStats", "Lobby", "None",
                        "Matchmaking", "ReadyCheck", "ChampSelect",
                    ) and source in ("GameFlow", "Live API"):
                        hero, source = None, source
                    if hero and hero != self.current_hero:
                        self._apply_hero_change(hero, source)
                    # ChampSelect 返回 None 时保持「等待识别」UI（局间已 clear），
                    # 勿在短暂 API 空档把刚识别到的本局英雄清掉。

                    # 选人锁定边沿：championId>0；intent alone 不算锁定
                    if phase == "ChampSelect" and self.lcu and self.lcu.is_connected():
                        pick_hero, pick_locked = self.lcu.get_champ_select_pick_state()
                        if pick_hero and pick_hero != self.current_hero:
                            self._apply_hero_change(
                                pick_hero,
                                "ChampSelect锁定" if pick_locked else "ChampSelect",
                            )
                        if pick_locked and not self._champ_select_locked:
                            self._champ_select_locked = True
                            lock_hero = pick_hero or self.current_hero
                            if lock_hero and lock_hero != self.current_hero:
                                self._apply_hero_change(lock_hero, "ChampSelect锁定")
                            self._on_champ_select_lock(self.current_hero or lock_hero)
                        elif not pick_locked:
                            self._champ_select_locked = False
                            self._runes_applied_on_lock = False
            except Exception as e:
                print(f"[hero_poll] {e}")

            # 自动海克斯
            try:
                self.auto_hex.tick()
            except Exception as e:
                print(f"[auto_hex] {e}")

            time.sleep(0.05)


class TrayManager:
    """系统托盘图标管理"""

    def __init__(self, app):
        self.app = app
        self.icon = None
        self._thread = None

    def _create_tray_image(self):
        """托盘图标: 优先 assets/icon.png，否则程序化金色六边形"""
        icon_png = os.path.join(BASE_DIR, "assets", "icon.png")
        if os.path.exists(icon_png):
            try:
                img = Image.open(icon_png).convert("RGBA")
                img = img.resize((64, 64), Image.Resampling.LANCZOS)
                return img
            except Exception:
                pass
        size = 64
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        cx, cy = size // 2, size // 2
        r = size // 2 - 4
        points = []
        for i in range(6):
            angle = math.radians(60 * i - 30)
            points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
        draw.polygon(points, fill=(10, 14, 23, 255), outline=(200, 170, 110, 255))
        r2 = r * 0.55
        inner = []
        for i in range(6):
            angle = math.radians(60 * i - 30)
            inner.append((cx + r2 * math.cos(angle), cy + r2 * math.sin(angle)))
        draw.polygon(inner, fill=(200, 155, 60, 220))
        return img

    def start(self):
        """启动托盘图标 (后台线程)"""
        image = self._create_tray_image()
        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", self._on_show),
            pystray.MenuItem("退出程序", self._on_quit),
        )
        self.icon = pystray.Icon("nho有手就行", image, "nho有手就行", menu)
        self._thread = threading.Thread(target=self.icon.run, daemon=True)
        self._thread.start()

    def stop(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass

    def notify(self, title, message):
        """托盘气泡通知"""
        if self.icon:
            try:
                self.icon.notify(message, title)
            except Exception:
                pass

    def _on_show(self, icon=None, item=None):
        self.app.gui_queue.put({"event": "tray_show"})

    def _on_quit(self, icon=None, item=None):
        self.app.gui_queue.put({"event": "tray_quit"})


# ================= 更新选项对话框 =================

class UpdateDialog:
    """数据更新选项对话框"""

    BG       = Theme.BG
    BG_CARD  = Theme.BG_CARD
    ACCENT   = Theme.ACCENT
    SUCCESS  = Theme.SUCCESS
    TEXT     = Theme.TEXT
    TEXT_DIM = Theme.TEXT_DIM
    BORDER   = Theme.BORDER
    WARNING  = Theme.WARNING

    def __init__(self, app):
        self.app = app
        self.dlg = tk.Toplevel(app.root)
        self.dlg.title("数据更新")
        self.dlg.configure(bg=self.BG)
        self.dlg.transient(app.root)
        self.dlg.grab_set()

        # 设置图标
        icon_path = os.path.join(BASE_DIR, 'assets', 'icon.ico')
        png_path = os.path.join(BASE_DIR, 'assets', 'icon.png')
        try:
            if os.path.exists(icon_path) and os.name == "nt":
                self.dlg.iconbitmap(icon_path)
        except Exception:
            pass
        try:
            if os.path.exists(png_path):
                self._dlg_icon = tk.PhotoImage(file=png_path)
                self.dlg.iconphoto(True, self._dlg_icon)
        except Exception:
            pass

        self._build_ui()

        # 强制完成所有子组件渲染，精确获取自身需要的高度
        self.dlg.update()
        w = max(440, self.dlg.winfo_reqwidth())
        min_h = self.dlg.winfo_reqheight()
        
        x = app.root.winfo_x() + (app.root.winfo_width() - w) // 2
        y = app.root.winfo_y() + (app.root.winfo_height() - min_h) // 2
        
        self.dlg.geometry(f"{w}x{min_h}+{x}+{y}")
        # 在设定好绝对尺寸后再禁用缩放，防止 Windows 过早锁死窗口尺寸导致元素被截住
        self.dlg.resizable(False, False)

    def _build_ui(self):
        main = tk.Frame(self.dlg, bg=self.BG, padx=24, pady=20)
        main.pack(fill=tk.BOTH, expand=True)

        # 标题
        tk.Label(main, text="选择更新方式", font=("Microsoft YaHei", 16, "bold"),
                 fg=self.TEXT, bg=self.BG).pack(anchor="w", pady=(0, 4))

        # ---- 爬虫选项区 ----
        tk.Label(main, text="🌐 本地爬虫更新 (需要 Chrome 浏览器)",
                 font=("Microsoft YaHei", 9), fg=self.WARNING,
                 bg=self.BG).pack(anchor="w", pady=(8, 6))

        self._option_row(main,
            icon="🔍", title="抽样校验", tag="推荐",
            desc="随机3英雄比对，有差异自动全量更新",
            command=lambda: self._select('spot_check'))

        self._option_row(main,
            icon="🧠", title="智能增量", tag=None,
            desc="自动爬取新英雄 + 改名英雄 + 缺失英雄",
            command=lambda: self._select('smart'))

        self._option_row(main,
            icon="🔄", title="全量更新", tag=None,
            desc="强制重爬所有英雄，耗时较长",
            command=lambda: self._select('full'))

        self._option_row(main,
            icon="🎯", title="精确更新", tag=None,
            desc="手动指定英雄名称进行更新",
            command=self._precise_input)

        # ---- 分隔线 ----
        sep_frame = tk.Frame(main, bg=self.BG, pady=8)
        sep_frame.pack(fill=tk.X)
        tk.Frame(sep_frame, bg=self.BORDER, height=1).pack(fill=tk.X)

        # ---- GitHub 下载 ----
        tk.Label(main, text="📦 在线下载 (无需浏览器)",
                 font=("Microsoft YaHei", 9), fg=self.TEXT_DIM,
                 bg=self.BG).pack(anchor="w", pady=(0, 6))

        self._option_row(main,
            icon="📥", title="GitHub 下载", tag=None,
            desc="从仓库下载预处理数据 (取决于仓库更新时间)",
            command=lambda: self._select('github'))

        # ---- 底部: 帮助按钮 ----
        bottom = tk.Frame(main, bg=self.BG)
        bottom.pack(fill=tk.X, pady=(8, 0))

        help_btn = tk.Label(bottom, text=" ？", font=("Microsoft YaHei", 12, "bold"),
                            fg=self.TEXT_DIM, bg=self.BG, cursor="hand2",
                            width=3, relief=tk.FLAT,
                            highlightbackground=self.BORDER, highlightthickness=1)
        help_btn.pack(side=tk.RIGHT)
        help_btn.bind("<Enter>", lambda e: help_btn.config(fg=self.ACCENT))
        help_btn.bind("<Leave>", lambda e: help_btn.config(fg=self.TEXT_DIM))
        help_btn.bind("<Button-1>", lambda e: self._show_help())

    def _option_row(self, parent, icon, title, tag, desc, command):
        """创建一个可点击的选项行"""
        row = tk.Frame(parent, bg=self.BG_CARD, cursor="hand2",
                       highlightbackground=self.BORDER, highlightthickness=1)
        row.pack(fill=tk.X, pady=(0, 6))

        inner = tk.Frame(row, bg=self.BG_CARD, padx=14, pady=10)
        inner.pack(fill=tk.X)

        # 标题行
        title_row = tk.Frame(inner, bg=self.BG_CARD)
        title_row.pack(fill=tk.X)

        tk.Label(title_row, text=f"{icon}  {title}",
                 font=("Microsoft YaHei", 11, "bold"),
                 fg=self.TEXT, bg=self.BG_CARD).pack(side=tk.LEFT)

        if tag:
            tag_frame = tk.Frame(title_row, bg=self.ACCENT, padx=6, pady=1)
            tag_frame.pack(side=tk.RIGHT)
            tk.Label(tag_frame, text=tag, font=("Microsoft YaHei", 8),
                     fg="white", bg=self.ACCENT).pack()

        # 描述
        tk.Label(inner, text=desc, font=("Microsoft YaHei", 9),
                 fg=self.TEXT_DIM, bg=self.BG_CARD, anchor="w").pack(fill=tk.X, pady=(2, 0))

        # 绑定点击事件到所有子组件
        def _on_enter(e):
            row.config(highlightbackground=self.ACCENT)
        def _on_leave(e):
            row.config(highlightbackground=self.BORDER)
        def _on_click(e):
            command()

        for widget in [row, inner, title_row] + list(inner.winfo_children()) + list(title_row.winfo_children()):
            widget.bind("<Enter>", _on_enter)
            widget.bind("<Leave>", _on_leave)
            widget.bind("<Button-1>", _on_click)

    def _select(self, mode):
        """选择更新模式并关闭对话框"""
        self.dlg.destroy()
        self.app._run_update(mode)

    def _precise_input(self):
        """精确更新: 弹出输入框"""
        input_dlg = tk.Toplevel(self.dlg)
        input_dlg.title("精确更新 - 输入英雄名")
        input_dlg.geometry("360x150")
        input_dlg.resizable(False, False)
        input_dlg.configure(bg=self.BG)
        input_dlg.transient(self.dlg)
        input_dlg.grab_set()

        frame = tk.Frame(input_dlg, bg=self.BG, padx=20, pady=16)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(frame, text="输入英雄名称 (多个用逗号分隔)",
                 font=("Microsoft YaHei", 10), fg=self.TEXT,
                 bg=self.BG).pack(anchor="w", pady=(0, 8))

        entry = tk.Entry(frame, font=("Microsoft YaHei", 11),
                         bg=self.BG_CARD, fg=self.TEXT,
                         insertbackground=self.TEXT,
                         highlightbackground=self.BORDER,
                         highlightthickness=1, relief=tk.FLAT, borderwidth=6)
        entry.pack(fill=tk.X, pady=(0, 12))
        entry.focus_set()

        def _submit():
            names = [n.strip() for n in entry.get().split(",") if n.strip()]
            if names:
                input_dlg.destroy()
                self.dlg.destroy()
                self.app._run_update('precise', hero_names=names)

        entry.bind("<Return>", lambda e: _submit())

        ttk.Button(frame, text="开始更新", style='Accent.TButton',
                   command=_submit).pack(fill=tk.X)

    def _show_help(self):
        """显示帮助信息"""
        help_text = (
            "📖 更新方式说明\n\n"
            "━━ 本地爬虫 (需要 Chrome) ━━\n\n"
            "🔍 抽样校验 [推荐]\n"
            "  从所有英雄中随机选取3个，爬取最新数据与本地\n"
            "  比对。如果发现差异，自动触发全量更新。\n"
            "  适合游戏版本更新后快速检测数据是否过期。\n\n"
            "🧠 智能增量\n"
            "  自动检测并爬取: 新出的英雄、近期改名的英雄、\n"
            "  以及本地缺失数据的英雄。不会重复爬取已有数据。\n\n"
            "🔄 全量更新\n"
            "  强制重新爬取全部英雄的海克斯数据。\n"
            "  耗时较长 (约10-20分钟)，适合数据严重过期时使用。\n\n"
            "🎯 精确更新\n"
            "  手动输入英雄名称 (支持中文名/英文名)，\n"
            "  仅更新指定英雄的数据。\n\n"
            "━━ 在线下载 (无需 Chrome) ━━\n\n"
            "📥 GitHub 下载\n"
            "  从项目仓库直接下载预处理好的数据文件。\n"
            "  ⚠ 注意: 仓库数据由开发者手动更新推送，\n"
            "  时效性不一定能保证。如果需要最新数据，\n"
            "  建议优先使用爬虫方式。"
        )
        messagebox.showinfo("更新方式说明", help_text, parent=self.dlg)

# ================= 圆形电源键 =================

class RoundPowerButton(tk.Canvas):
    """圆形电源键：空闲 = ▶ 开始，运行中 = ■ 停止。

    ttk.Button 只能是方的，用户要求"换个形状放到对局自动化右边"，
    所以用 Canvas 画圆。同时刻意兼容原 start_btn / stop_btn 的调用方式
    （pack / pack_forget / config(state=...)），这样其它调用点不用改：
      - pack() 忽略 fill/expand，保持固定大小
      - pack_forget() 变成 no-op —— 它是常驻按钮，不参与显隐切换
      - config(state=...) 映射成禁用外观
    """

    SIZE = 60

    def __init__(self, parent, command, bg="#FFFFFF"):
        super().__init__(parent, width=self.SIZE, height=self.SIZE,
                         bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self._cmd = command
        self._mode = "idle"        # idle / starting / running
        self._disabled = True      # 数据没加载完之前不可点
        self._hover = False
        self._draw()
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw(hover=False))

    # ---------- 外观 ----------
    def _draw(self, hover=None):
        if hover is not None:
            self._hover = hover
        self.delete("all")
        s = self.SIZE
        pad = 4
        running = self._mode == "running"

        # Apple 风配色：蓝=开始，红=停止
        if self._disabled:
            ring, fill, glyph_fg, text = "#D2D2D7", "#F5F5F7", "#C7C7CC", "▶"
        elif running:
            ring, fill, glyph_fg, text = "#FF3B30", "#FFEDED", "#D70015", "■"
        else:
            ring, fill, glyph_fg, text = "#0071E3", "#EAF3FE", "#0071E3", "▶"

        if self._hover and not self._disabled:
            fill = "#FFDBDB" if running else "#D5E8FB"

        self.create_oval(pad, pad, s - pad, s - pad,
                         outline=ring, width=3, fill=fill)
        self.create_text(s / 2, s / 2 - 1, text=text,
                         fill=glyph_fg, font=("Segoe UI Symbol", 17, "bold"))

    # ---------- 交互 ----------
    def _on_click(self, _evt=None):
        if self._disabled:
            return
        try:
            self._cmd()
        except Exception as e:
            print(f"电源键回调异常: {e}")

    def set_mode(self, mode):
        """idle / starting / running"""
        self._mode = mode
        self._draw()

    # ---------- 兼容 ttk.Button 的调用方式 ----------
    def config(self, **kw):
        state = kw.pop("state", None)
        if state is not None:
            self._disabled = (str(state) == str(tk.DISABLED) or state == "disabled")
            self._draw()
        if kw:
            super().config(**kw)

    configure = config

    def pack(self, **kw):
        for k in ("fill", "expand", "pady", "padx", "side", "anchor"):
            kw.pop(k, None)
        super().pack(**kw)

    def pack_forget(self):
        # 常驻按钮，不参与显隐切换
        return None


# ================= 主 GUI 应用 =================

class LauncherApp:
    """nho有手就行 - 主界面"""

    # 配色方案 (引用统一主题)
    BG          = Theme.BG
    BG_CARD     = Theme.BG_CARD
    BG_INPUT    = Theme.BG_INPUT
    ACCENT      = Theme.ACCENT
    ACCENT_HVR  = Theme.ACCENT_HVR
    SUCCESS     = Theme.SUCCESS
    WARNING     = Theme.WARNING
    ERROR       = Theme.ERROR
    TEXT        = Theme.TEXT
    TEXT_DIM    = Theme.TEXT_DIM
    BORDER      = Theme.BORDER
    ACCENT_DIM  = Theme.ACCENT_DIM
    BG_SEG      = Theme.BG_SEG
    GOLD_GLOW   = Theme.GOLD_GLOW
    MINT_D      = Theme.MINT_D
    SKY_D       = Theme.SKY_D
    VIOLET_D    = Theme.VIOLET_D

    FONT_TITLE  = ("Microsoft YaHei", 18, "bold")
    FONT_SUB    = ("Microsoft YaHei", 10)
    FONT_HERO   = ("Microsoft YaHei", 22, "bold")
    FONT_STATUS = ("Microsoft YaHei", 11)
    FONT_BTN    = ("Microsoft YaHei", 11, "bold")
    FONT_LOG    = ("Consolas", 9)

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("nho有手就行 · 海克斯 / 匹配 / 符文")
        self.root.geometry("580x720")
        self.root.minsize(560, 660)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 设置窗口图标 (Windows .ico + 跨平台 PNG)
        self._set_window_icon(self.root)

        # ttk 主题
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self._configure_styles()

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
        self.status_color = self.TEXT_DIM
        self._pulse_state = 0
        self._logo_photo = None  # keep PhotoImage refs
        self._icon_photo = None

        # 重定向日志
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        sys.stdout = LogRedirector(self.log_queue, self._orig_stdout)
        sys.stderr = LogRedirector(self.log_queue, self._orig_stderr)

        # 构建 UI
        self._build_ui()

        # 启动队列轮询
        self.root.after(100, self._poll_queues)

        # 启动时加载数据
        self.root.after(300, self._load_data)

        # 启动后静默自检官方海克斯数据库（不阻塞 UI，失败不打扰）
        self.root.after(4000, self._auto_check_official_augments)

    # ==========================================
    # ttk 样式配置
    # ==========================================

    def _configure_styles(self):
        s = self.style

        # 主按钮 (薄荷绿，同宣传网页 CTA)
        s.configure('Accent.TButton',
                     background=self.ACCENT,
                     foreground='#FFFFFF',
                     font=self.FONT_BTN,
                     padding=(16, 10),
                     borderwidth=0)
        s.map('Accent.TButton',
              background=[('active', self.ACCENT_HVR), ('disabled', self.BORDER)])

        # 打赏按钮（淡紫，CTA 渐变尾色）
        s.configure('Donate.TButton',
                     background=self.GOLD_GLOW,
                     foreground='#FFFFFF',
                     font=self.FONT_BTN,
                     padding=(12, 6),
                     borderwidth=0)
        s.map('Donate.TButton',
              background=[('active', self.ACCENT_HVR), ('disabled', self.BORDER)])

        # 次要按钮 (白底黑字，紧凑)
        s.configure('Secondary.TButton',
                     background=self.BG_CARD,
                     foreground=self.TEXT,
                     font=("Microsoft YaHei", 9),
                     padding=(10, 4),
                     borderwidth=1)
        s.map('Secondary.TButton',
              background=[('active', '#F2F2F4'), ('disabled', self.BG_SEG)])

        # 停止按钮 (红色)
        s.configure('Danger.TButton',
                     background=self.ERROR,
                     foreground='white',
                     font=self.FONT_BTN,
                     padding=(16, 10),
                     borderwidth=0)
        s.map('Danger.TButton',
              background=[('active', '#D63A3F')])

        # 链接按钮 (无背景)
        s.configure('Link.TButton',
                     background=self.BG,
                     foreground=self.TEXT_DIM,
                     font=self.FONT_SUB,
                     padding=(8, 4),
                     borderwidth=0)
        s.map('Link.TButton',
              foreground=[('active', self.ACCENT)],
              background=[('active', self.BG)])

    def _asset_path(self, *parts):
        return os.path.join(BASE_DIR, "assets", *parts)

    def _set_window_icon(self, window):
        """Apply app icon to a Tk / Toplevel window (ico + png fallback)."""
        ico = self._asset_path("icon.ico")
        png = self._asset_path("icon.png")
        logo = self._asset_path("logo.png")
        try:
            if os.path.exists(ico) and os.name == "nt":
                window.iconbitmap(ico)
            elif os.path.exists(ico):
                try:
                    window.iconbitmap(ico)
                except Exception:
                    pass
        except Exception:
            pass
        # iconphoto: prefer small logo/png via PhotoImage (cross-platform)
        photo = None
        for candidate in (logo, png):
            if not candidate or not os.path.exists(candidate):
                continue
            try:
                photo = tk.PhotoImage(file=candidate)
                break
            except Exception:
                try:
                    from PIL import Image as PILImage, ImageTk
                    img = PILImage.open(candidate).convert("RGBA")
                    img.thumbnail((64, 64), PILImage.Resampling.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    break
                except Exception:
                    photo = None
        if photo is not None:
            try:
                window._icon_photo_ref = photo
                self._icon_photo = photo
                window.iconphoto(True, photo)
            except Exception:
                pass

    def _show_donate_dialog(self):
        """打赏 / tip modal showing QR or tip image."""
        dlg = tk.Toplevel(self.root)
        dlg.title("打赏支持")
        dlg.configure(bg=self.BG)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        self._set_window_icon(dlg)

        frame = tk.Frame(dlg, bg=self.BG, padx=20, pady=16)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            frame, text="支持作者，让工具更完美",
            font=("Microsoft YaHei", 13, "bold"),
            fg=self.MINT_D, bg=self.BG,
        ).pack(pady=(0, 10))

        donate_path = self._asset_path("donate.jpg")
        if not os.path.exists(donate_path):
            donate_path = self._asset_path("donate.png")

        self._donate_photo = None
        if os.path.exists(donate_path):
            try:
                from PIL import Image as PILImage, ImageTk
                img = PILImage.open(donate_path)
                img.thumbnail((360, 360), PILImage.Resampling.LANCZOS)
                self._donate_photo = ImageTk.PhotoImage(img)
                tk.Label(frame, image=self._donate_photo, bg=self.BG).pack(pady=(0, 12))
            except Exception as e:
                tk.Label(
                    frame, text=f"(无法加载打赏图片: {e})",
                    font=self.FONT_SUB, fg=self.TEXT_DIM, bg=self.BG,
                ).pack(pady=(0, 12))
        else:
            tk.Label(
                frame, text="(未找到 assets/donate.jpg)",
                font=self.FONT_SUB, fg=self.TEXT_DIM, bg=self.BG,
            ).pack(pady=(0, 12))

        tk.Label(
            frame, text="感谢支持 · 打赏纯属自愿",
            font=("Microsoft YaHei", 9), fg=self.TEXT_DIM, bg=self.BG,
        ).pack(pady=(0, 10))

        ttk.Button(frame, text="关闭", style="Secondary.TButton",
                   command=dlg.destroy).pack(fill=tk.X)

        dlg.update_idletasks()
        w = max(400, dlg.winfo_reqwidth())
        h = dlg.winfo_reqheight()
        x = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_y() + max(0, (self.root.winfo_height() - h) // 2)
        dlg.geometry(f"{w}x{h}+{x}+{y}")


    # ==========================================
    # 构建 UI
    # ==========================================

    def _build_ui(self):
        # ---- 固定一屏布局（Apple 风格：不滚动，控件分区紧凑）----
        main = tk.Frame(self.root, bg=self.BG, padx=18, pady=14)
        main.pack(fill=tk.BOTH, expand=True)

        # ---- 顶部工具栏 ----
        hdr = tk.Frame(main, bg=self.BG)
        hdr.pack(fill=tk.X, pady=(0, 10))

        # logo（assets/logo.png），失败回退六边形
        logo_path = self._asset_path("logo.png")
        if os.path.exists(logo_path):
            try:
                self._logo_photo = tk.PhotoImage(file=logo_path)
                try:
                    iw, ih = self._logo_photo.width(), self._logo_photo.height()
                    if iw > 36 or ih > 36:
                        factor = max(1, max(iw, ih) // 30)
                        self._logo_photo = self._logo_photo.subsample(factor, factor)
                except Exception:
                    pass
                tk.Label(hdr, image=self._logo_photo, bg=self.BG).pack(
                    side=tk.LEFT, padx=(0, 10)
                )
            except Exception:
                tk.Label(hdr, text="⬡", font=("Segoe UI", 22), fg=self.ACCENT,
                         bg=self.BG).pack(side=tk.LEFT, padx=(0, 10))
        else:
            tk.Label(hdr, text="⬡", font=("Segoe UI", 22), fg=self.ACCENT,
                     bg=self.BG).pack(side=tk.LEFT, padx=(0, 10))

        title_frame = tk.Frame(hdr, bg=self.BG)
        title_frame.pack(side=tk.LEFT)
        tk.Label(title_frame, text="nho有手就行",
                 font=("Microsoft YaHei", 16, "bold"), fg=self.TEXT, bg=self.BG).pack(anchor="w")
        tk.Label(title_frame, text="自动化 · 符文 · 海克斯 · 玩法推荐",
                 font=("Microsoft YaHei", 9), fg=self.TEXT_DIM, bg=self.BG).pack(anchor="w")

        # 打赏（主色胶囊）
        donate_btn = tk.Button(
            hdr, text="打赏",
            font=("Microsoft YaHei", 10, "bold"),
            fg="#FFFFFF", bg=self.ACCENT,
            activeforeground="#FFFFFF", activebackground=self.ACCENT_HVR,
            relief=tk.FLAT, padx=14, pady=5, cursor="hand2",
            command=self._show_donate_dialog,
        )
        donate_btn.pack(side=tk.RIGHT, padx=(6, 0))

        # ---- 状态条（单行式白卡：左英雄右状态）----
        status_card = self._make_card(main)
        hero_row = tk.Frame(status_card, bg=self.BG_CARD)
        hero_row.pack(fill=tk.X)
        tk.Label(hero_row, text="当前英雄", font=("Microsoft YaHei", 9),
                 fg=self.TEXT_DIM, bg=self.BG_CARD).pack(side=tk.LEFT)
        self.hero_label = tk.Label(hero_row, textvariable=self.hero_var,
                                   font=("Microsoft YaHei", 18, "bold"),
                                   fg=self.ACCENT, bg=self.BG_CARD)
        self.hero_label.pack(side=tk.LEFT, padx=(10, 0))
        self.status_dot = tk.Label(hero_row, text="●", font=("Segoe UI", 9),
                                   fg=self.TEXT_DIM, bg=self.BG_CARD)
        self.status_dot.pack(side=tk.RIGHT, padx=(0, 6))
        self.status_label = tk.Label(hero_row, textvariable=self.status_var,
                                     font=("Microsoft YaHei", 10), fg=self.TEXT_DIM,
                                     bg=self.BG_CARD)
        self.status_label.pack(side=tk.RIGHT)

        # 倒计时（激活时醒目）
        self.countdown_label = tk.Label(
            status_card, textvariable=self.countdown_var,
            font=("Microsoft YaHei", 15, "bold"),
            fg=self.ACCENT, bg=self.BG_CARD, pady=2,
        )
        # 默认不占位；有内容时再 pack
        self._countdown_packed = False

        # ========== 卡片: 对局（电源键 + 延迟 + 全部开关一行）=========
        auto_card = self._make_card(main)

        ctrl_row = tk.Frame(auto_card, bg=self.BG_CARD)
        ctrl_row.pack(fill=tk.X, pady=(0, 8))

        # 左：圆形电源键 + 说明
        power_box = tk.Frame(ctrl_row, bg=self.BG_CARD)
        power_box.pack(side=tk.LEFT)
        self.power_btn = RoundPowerButton(power_box, command=self._toggle_engine,
                                          bg=self.BG_CARD)
        self.power_btn.pack(side=tk.LEFT)
        self.power_caption = tk.Label(
            power_box, text="开始识别", font=("Microsoft YaHei", 9),
            fg=self.TEXT_DIM, bg=self.BG_CARD,
        )
        self.power_caption.pack(side=tk.LEFT, padx=(10, 0))

        # 右：执行延迟分段控件
        tk.Label(ctrl_row, text="延迟", font=("Microsoft YaHei", 9),
                 fg=self.TEXT_DIM, bg=self.BG_CARD).pack(side=tk.RIGHT, padx=(10, 0))
        seg = tk.Frame(ctrl_row, bg=self.BORDER, padx=1, pady=1)
        seg.pack(side=tk.RIGHT)
        seg_inner = tk.Frame(seg, bg=self.BG_SEG)
        seg_inner.pack()

        current_delay = normalize_delay(self.settings.get("auto_accept_delay", 5))
        self._delay_value = current_delay
        self._delay_btns = {}
        labels_map = {0: "立即", 3: "3s", 5: "5s", 10: "10s"}
        for sec in AUTO_DELAY_CHOICES:
            btn = tk.Label(
                seg_inner, text=labels_map[sec],
                font=("Microsoft YaHei", 9, "bold"),
                padx=10, pady=4, cursor="hand2",
            )
            btn.pack(side=tk.LEFT)
            btn.bind("<Button-1>", lambda e, s=sec: self._on_delay_select(s))
            self._delay_btns[sec] = btn
        self._refresh_delay_buttons()

        # 兼容原有调用点：start/stop 都指向同一个圆形键
        self.start_btn = self.power_btn
        self.stop_btn = self.power_btn

        # 开关一行四个（对局自动化 / 符文 / 海克斯 全部集中在这）
        toggles_row = tk.Frame(auto_card, bg=self.BG_CARD)
        toggles_row.pack(fill=tk.X)
        all_toggles = [
            ("auto_accept", "自动接受"),
            ("auto_ready", "自动准备"),
            ("auto_apply_runes", "自动符文"),
            ("auto_hex", "海克斯识别"),
        ]
        for key, label in all_toggles:
            var = tk.BooleanVar(value=bool(self.settings.get(key, DEFAULT_SETTINGS.get(key, False))))
            self._toggle_vars[key] = var
            cb = tk.Checkbutton(
                toggles_row, text=label, variable=var,
                font=("Microsoft YaHei", 9),
                fg=self.TEXT, bg=self.BG_CARD, activebackground=self.BG_CARD,
                activeforeground=self.TEXT, selectcolor="#FFFFFF",
                highlightthickness=0, bd=0,
                command=lambda k=key, v=var: self._on_toggle(k, v),
            )
            cb.pack(side=tk.LEFT, padx=(0, 14))

        # ========== 卡片: 英雄 / 符文 ==========
        hero_card = self._make_card(main)

        manual_frame = tk.Frame(hero_card, bg=self.BG_CARD)
        manual_frame.pack(fill=tk.X, pady=(0, 8))

        self.hero_entry = tk.Entry(manual_frame, font=("Microsoft YaHei", 10),
                                   bg=self.BG_INPUT, fg=self.TEXT,
                                   insertbackground=self.TEXT,
                                   highlightbackground=self.BORDER,
                                   highlightcolor=self.ACCENT,
                                   highlightthickness=1, relief=tk.FLAT,
                                   borderwidth=6)
        self.hero_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.hero_entry.insert(0, "输入英雄名/拼音...")
        self.hero_entry.config(fg=self.TEXT_DIM)
        self.hero_entry.bind("<FocusIn>", self._on_entry_focus_in)
        self.hero_entry.bind("<FocusOut>", self._on_entry_focus_out)
        self.hero_entry.bind("<Return>", lambda e: self._manual_set_hero())

        self.manual_btn = ttk.Button(manual_frame, text="锁定",
                                     style='Accent.TButton',
                                     command=self._manual_set_hero)
        self.manual_btn.pack(side=tk.RIGHT)

        # 操作按钮一行四个
        action_btns = tk.Frame(hero_card, bg=self.BG_CARD)
        action_btns.pack(fill=tk.X, pady=(0, 6))
        self.refresh_btn = ttk.Button(
            action_btns, text="🔄 刷新识别", style='Secondary.TButton',
            command=self._manual_refresh_ocr,
        )
        self.refresh_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))
        self.detect_hero_btn = ttk.Button(
            action_btns, text="识别英雄", style='Secondary.TButton',
            command=self._manual_refresh_hero,
        )
        self.detect_hero_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))
        self.reset_btn = ttk.Button(
            action_btns, text="重置", style='Secondary.TButton',
            command=self._manual_reset,
        )
        self.reset_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))
        self.rune_btn = ttk.Button(action_btns, text="⚔ 套用符文",
                                   style='Secondary.TButton',
                                   command=self._manual_apply_runes)
        self.rune_btn.pack(side=tk.LEFT, expand=True, fill=tk.X)

        self.rune_info_var = tk.StringVar(value="符文: 锁定英雄后显示推荐")
        tk.Label(hero_card, textvariable=self.rune_info_var, font=("Microsoft YaHei", 9),
                 fg=self.TEXT_DIM, bg=self.BG_CARD, justify="left", anchor="w",
                 wraplength=480).pack(fill=tk.X)

        # ========== 卡片: 海克斯（识别结果 + 玩法推荐）=========
        hex_card = self._make_card(main)

        self.hex_banner_var = tk.StringVar(value="")
        self.hex_banner = tk.Label(
            hex_card, textvariable=self.hex_banner_var,
            font=("Microsoft YaHei", 10, "bold"),
            fg=self.ACCENT, bg=self.BG_CARD, anchor="w",
        )
        self.hex_banner.pack(fill=tk.X)
        self._hex_banner_clear_after = None

        # 海克斯识别结果：展示最近一次识别出的三个选项与推荐
        self.hex_result_var = tk.StringVar(value="")
        self.hex_result = tk.Label(
            hex_card, textvariable=self.hex_result_var,
            font=("Microsoft YaHei", 9),
            fg=self.TEXT_DIM, bg=self.BG_CARD, anchor="w", justify="left",
        )
        self.hex_result.pack(fill=tk.X, pady=(4, 0))

        # 空状态不展示卡片，有内容时动态出现（保持首页整洁）
        self._hex_card = hex_card
        hex_card.pack_forget()

        # ---- 页脚：次级操作一行（小号链接按钮）----
        footer = tk.Frame(main, bg=self.BG)
        footer.pack(fill=tk.X, pady=(8, 4))
        self._footer = footer

        self.update_btn = ttk.Button(footer, text="数据更新",
                                     style='Secondary.TButton',
                                     command=self._show_update_dialog)
        self.update_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.wegame_btn = ttk.Button(footer, text="启动 WeGame",
                                     style='Secondary.TButton',
                                     command=self._launch_wegame)
        self.wegame_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.wegame_path_btn = ttk.Button(footer, text="路径…",
                                          style='Secondary.TButton',
                                          command=self._choose_wegame_path)
        self.wegame_path_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.wegame_shortcut_btn = ttk.Button(
            footer, text="快捷方式", style='Secondary.TButton',
            command=self._create_wegame_shortcut,
        )
        self.wegame_shortcut_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.tray_btn = ttk.Button(footer, text="最小化",
                                    style='Secondary.TButton',
                                    command=self._minimize_to_tray)
        self.tray_btn.pack(side=tk.RIGHT)

        self.wegame_hint_var = tk.StringVar(value="")
        self._update_wegame_hint()

        # ---- 日志（紧凑 4 行）----
        self.log_text = scrolledtext.ScrolledText(
            main, font=("Consolas", 8), bg=self.BG_CARD, fg=self.TEXT_DIM,
            insertbackground=self.TEXT_DIM, selectbackground=self.ACCENT_DIM,
            relief=tk.FLAT, borderwidth=0, height=4, wrap=tk.WORD, state=tk.DISABLED,
            highlightbackground=self.BORDER, highlightthickness=1
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.log_text.tag_configure("success", foreground=self.SUCCESS)
        self.log_text.tag_configure("error", foreground=self.ERROR)
        self.log_text.tag_configure("warning", foreground=self.WARNING)
        self.log_text.tag_configure("info", foreground=self.TEXT_DIM)

    def _make_card(self, parent, title=None):
        """白卡片（Apple 分组容器：白底 + 细描边 + 紧凑内边距）。"""
        outer = tk.Frame(
            parent, bg=self.BG_CARD, padx=14, pady=10,
            highlightbackground=self.BORDER, highlightthickness=1,
        )
        outer.pack(fill=tk.X, pady=(0, 8))
        if title:
            title_row = tk.Frame(outer, bg=self.BG_CARD)
            title_row.pack(fill=tk.X, pady=(0, 6))
            tk.Label(
                title_row, text=title,
                font=("Microsoft YaHei", 10, "bold"),
                fg=self.TEXT, bg=self.BG_CARD,
            ).pack(side=tk.LEFT)
        return outer

    def _refresh_delay_buttons(self):
        for sec, btn in self._delay_btns.items():
            if sec == self._delay_value:
                btn.config(bg=self.ACCENT, fg="#FFFFFF")
            else:
                btn.config(bg=self.BG_SEG, fg=self.TEXT_DIM)

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
        """更新醒目倒计时显示；seconds 为 None 时隐藏。"""
        if seconds is None:
            self.countdown_var.set("")
            if self._countdown_packed:
                self.countdown_label.pack_forget()
                self._countdown_packed = False
            return
        # 醒目倒计时，形如 「3…」「2…」「1…」
        self.countdown_var.set(f"⏳ {label}  「{int(seconds)}…」")
        if not self._countdown_packed:
            self.countdown_label.pack(fill=tk.X, pady=(8, 0))
            self._countdown_packed = True

    # ==========================================
    # 数据加载
    # ==========================================

    def _load_data(self):
        """后台加载数据"""
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

    # ==========================================
    # 引擎控制
    # ==========================================

    def _set_power_mode(self, mode):
        """更新圆形电源键的外观与说明文字。"""
        try:
            self.power_btn.set_mode(mode)
            self.power_caption.config(text={
                "idle": "开始识别",
                "starting": "启动中…",
                "running": "点击停止",
            }.get(mode, ""))
        except Exception:
            pass

    def _toggle_engine(self):
        """圆形电源键：未运行则启动，运行中则停止。"""
        if self.engine_running:
            self._stop_engine()
        else:
            self._start_engine()

    def _start_engine(self):
        """启动识别引擎"""
        if self.engine_running:
            return
        if not self.dm or not self.dm.hero_data:
            messagebox.showwarning("提示", "数据尚未加载完成，请稍候")
            return

        self._set_power_mode("starting")
        self.start_btn.config(state=tk.DISABLED)
        self._set_status("启动中...", self.WARNING)
        self._log("正在初始化 OCR 引擎...")

        def _init():
            try:
                from main import GameAnalyzer, OverlayApp
                from scripts.lcu_connector import LCUConnector

                # 初始化分析器 (加载 OCR 模型)
                self.analyzer = GameAnalyzer(self.dm)
                self._log("✅ OCR 引擎就绪")

                # 初始化 LCU 连接器
                champions_json = os.path.join(self.dm.data_dir, 'champions.json')
                self.lcu = LCUConnector(champions_json)
                self.rune_service.set_lcu(self.lcu)
                self._log("✅ LCU 连接器就绪")

                # 在主线程创建 overlay
                self.gui_queue.put({"event": "create_overlay"})

            except Exception as e:
                self._log(f"❌ 引擎启动失败: {e}")
                traceback.print_exc()
                self.gui_queue.put({"event": "engine_error"})

        threading.Thread(target=_init, daemon=True).start()

    def _create_overlay_and_start(self):
        """在主线程中创建 overlay 窗口并启动控制器"""
        try:
            from main import OverlayApp

            # 创建 overlay 作为 Toplevel
            self.overlay_window = tk.Toplevel(self.root)
            # 浮钮回调与控制面板「刷新识别」相同；主 overlay 仍鼠标穿透
            self.overlay = OverlayApp(
                self.overlay_window,
                self.overlay_queue,
                on_manual_refresh=self._manual_refresh_ocr,
            )
            # 浮钮默认隐藏，真正进入对局（InProgress+Live）后再显示
            try:
                self.overlay_queue.put({"cmd": "FAB_HIDE"})
            except Exception:
                pass

            # 启动后台控制器
            self.rune_service.set_lcu(self.lcu)
            self.controller = GUIController(
                self.overlay_queue, self.gui_queue,
                self.dm, self.analyzer, self.lcu,
                settings=self.settings,
                rune_service=self.rune_service,
            )
            self.controller.start()

            self.engine_running = True
            self._set_power_mode("running")
            self.start_btn.config(state=tk.NORMAL)
            self._set_status("运行中", self.SUCCESS)
            self._log("✅ 引擎已启动! 进入对局且右上角读秒出现后再识别海克斯；选人阶段仍可推荐/套用符文")
            if self.settings.get("overlay_topmost", True) and self.overlay_window:
                try:
                    self.overlay_window.attributes("-topmost", True)
                except Exception:
                    pass
            self._start_pulse()

            # 启动托盘
            self.tray.start()

        except Exception as e:
            self._log(f"❌ Overlay 创建失败: {e}")
            traceback.print_exc()
            self._engine_cleanup()
            self._set_power_mode("idle")
            self.start_btn.config(state=tk.NORMAL)

    def _stop_engine(self):
        """停止识别引擎"""
        self._log("正在停止引擎...")
        self._engine_cleanup()
        self._set_power_mode("idle")
        self.start_btn.config(state=tk.NORMAL)
        self._set_status("已停止", self.TEXT_DIM)
        self.hero_var.set("—")
        self._log("引擎已停止")

    def _engine_cleanup(self):
        """清理引擎资源"""
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

    # ==========================================
    # 数据更新
    # ==========================================

    def _show_update_dialog(self):
        """显示更新选项对话框"""
        UpdateDialog(self)

    def _run_update(self, mode, hero_names=None):
        """执行更新操作 (后台线程)"""
        self.update_btn.config(state=tk.DISABLED)

        mode_labels = {
            'spot_check': '🔍 抽样校验',
            'smart':      '🧠 智能增量',
            'full':       '🔄 全量更新',
            'precise':    '🎯 精确更新',
            'github':     '📥 GitHub 下载',
        }
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
        """后台静默自检官方海克斯数据库；有更新则触发数据重载。"""
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

    # ==========================================
    # 系统托盘
    # ==========================================

    def _minimize_to_tray(self):
        """最小化到系统托盘"""
        if not self.engine_running:
            messagebox.showinfo("提示", "请先点击「开始识别」再最小化到托盘")
            return
        self.root.withdraw()
        self.tray.notify("nho有手就行", "程序已最小化到系统托盘，可从托盘恢复主界面点击操作")
        # 确保 overlay 仍然可见
        if self.overlay_window:
            self.root.after(100, self._ensure_overlay_visible)

    def _ensure_overlay_visible(self):
        """确保 overlay 在 root 隐藏后仍然可见"""
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
        """从托盘恢复窗口"""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    # ==========================================
    # 队列消息处理
    # ==========================================

    def _poll_queues(self):
        """轮询所有消息队列"""
        # GUI 队列
        try:
            while True:
                msg = self.gui_queue.get_nowait()
                self._handle_gui_message(msg)
        except queue.Empty:
            pass

        # 日志队列
        try:
            while True:
                text = self.log_queue.get_nowait()
                self._append_log(text)
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
            hero = msg.get("hero", "")
            source = msg.get("source", "")
            self.hero_var.set(hero or "—")
            if source == "手动输入":
                self._set_status("监听中", self.SUCCESS)
            else:
                self._set_status("监听中", self.SUCCESS)
            if hero:
                self.tray.notify("英雄已识别", f"当前英雄: {hero}")

        elif event == "hero_cleared":
            label = msg.get("label") or "等待识别本局英雄"
            self.hero_var.set(label)
            self.rune_info_var.set("符文: 等待识别本局英雄")

        elif event == "hero_confirmed":
            self.hero_var.set(msg.get("hero", "") or "—")

        elif event == "status":
            status = msg.get("status", "")
            if "hero" in msg:
                hero = msg.get("hero")
                self.hero_var.set(hero if hero else "等待识别本局英雄")
            status_map = {
                "connecting":       ("连接客户端...", self.WARNING),
                "waiting":          ("等待选取英雄...", self.WARNING),
                "listening":        ("监听中", self.SUCCESS),
                "analyzing":        ("分析中...", self.ACCENT),
                "analyzed":         ("分析完成", self.SUCCESS),
                "refreshing":       ("刷新英雄...", self.WARNING),
                "no_hero_warning":  ("未锁定英雄", self.ERROR),
                "idle":             ("运行中 (无英雄)", self.TEXT_DIM),
                "resetting":        ("重置中...", self.WARNING),
            }
            if status in status_map:
                text, color = status_map[status]
                self._set_status(text, color)

        elif event == "tray_show":
            self._restore_from_tray()

        elif event == "tray_quit":
            self._quit_app()

        elif event == "update_done":
            self.update_btn.config(state=tk.NORMAL)

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
                # 单行摘要
                first = info.splitlines()[0] if info else ""
                self.rune_info_var.set(first or "符文推荐已更新")

        elif event == "countdown":
            self._set_countdown_ui(msg.get("seconds"), msg.get("label") or "")

    # ==========================================
    # 手动英雄输入
    # ==========================================

    def _on_entry_focus_in(self, event):
        if self.hero_entry.get() == "输入英雄名/拼音...":
            self.hero_entry.delete(0, tk.END)
            self.hero_entry.config(fg=self.TEXT)

    def _on_entry_focus_out(self, event):
        if not self.hero_entry.get().strip():
            self.hero_entry.insert(0, "输入英雄名/拼音...")
            self.hero_entry.config(fg=self.TEXT_DIM)

    def _manual_set_hero(self):
        """手动输入英雄名并锁定"""
        query = self.hero_entry.get().strip()
        if not query or query == "输入英雄名/拼音...":
            return

        if not self.dm or not self.dm.hero_data:
            self._log("❌ 数据未加载")
            return

        # 搜索英雄（精确 CN/EN/拼音/昵称 + 模糊）
        matches, is_exact = self.dm.search_hero(query)

        if not matches:
            self._log(f"❌ 未找到英雄: {query}")
            return

        # 取第一个匹配
        hero_name = matches[0]

        # 如果控制器正在运行，通过控制器设置
        if self.controller and self.engine_running:
            result = self.controller.set_hero(hero_name)
            if result:
                if is_exact:
                    self._log(f"✅ 已锁定: {result}")
                else:
                    self._log(f"已匹配: {result}")
                    self._set_status(f"已匹配: {result}", self.SUCCESS)
                self.hero_entry.delete(0, tk.END)
                self.hero_entry.insert(0, "输入英雄名/拼音...")
                self.hero_entry.config(fg=self.TEXT_DIM)
                self.root.focus()
            else:
                self._log(f"❌ 英雄 [{hero_name}] 不在数据库中")
        else:
            self._log(f"⚠ 请先点击「开始识别」")

    # ==========================================
    # 设置 / 开关 / 符文
    # ==========================================

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
        labels = {
            "auto_accept": "自动接受",
            "auto_ready": "自动开始/准备",
            "auto_hex": "自动海克斯识别",
            "auto_apply_runes": "套用符文",
        }
        self._log(f"{'✅ 开启' if value else '⏸ 关闭'} {labels.get(key, key)}")

    def _manual_refresh_ocr(self):
        if not self.controller or not self.engine_running:
            self._log("⚠ 请先点击「开始识别」")
            return
        self._log("点击刷新识别…")
        threading.Thread(target=self.controller.trigger_analyze, daemon=True).start()

    def _manual_refresh_hero(self):
        if not self.controller or not self.engine_running:
            self._log("⚠ 请先点击「开始识别」")
            return
        self._log("点击识别英雄…")
        threading.Thread(target=self.controller.trigger_refresh_hero, daemon=True).start()

    def _manual_reset(self):
        if not self.controller or not self.engine_running:
            self._log("⚠ 请先点击「开始识别」")
            return
        self._log("点击重置：重新自动检测英雄…")
        self.controller.request_reset()

    def _manual_apply_runes(self):
        if not self.controller or not self.engine_running:
            # 仍可展示推荐
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

    # ==========================================
    # WeGame 启动器
    # ==========================================

    _WEGAME_REG_KEYS = [
        (0, r"SOFTWARE\WOW6432Node\Tencent\WeGame"),   # HKLM (32位视角)
        (0, r"SOFTWARE\Tencent\WeGame"),               # HKLM
        (1, r"Software\Tencent\WeGame"),               # HKCU
        (1, r"Software\Tencent\wegame"),               # HKCU (小写变体)
    ]
    _WEGAME_REG_VALUES = (
        "InstallPath", "Path", "InstallDir", "WeGamePath",
        "WegamePath", "wegame.exe", "InstallDir64",
    )

    def _find_wegame_path(self, scan_drives=False):
        """自动查找 wegame.exe 路径，找不到返回 None。

        scan_drives=False 仅查: 记忆路径 -> 注册表 -> 常见安装路径(毫秒级)
        scan_drives=True  追加: 各盘浅层目录扫描(适配网吧路径不固定)
        """
        # 1) 记忆路径
        saved = self.settings.get("wegame_path") or ""
        if saved and os.path.isfile(saved) and _is_wegame_exe(saved):
            return saved
        # 2) 注册表
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
        # 3) 常见安装路径
        for p in _WEGAME_COMMON_PATHS:
            if os.path.isfile(p):
                return p
        # 4) 各盘浅层扫描（网吧: WeGame 常装在 D:/E: 根目录或一级子目录下）
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
                # 一级子目录下形如 <dir>\WeGame\wegame.exe
                for d in _list_dirs(base):
                    cand = os.path.join(d, "WeGame", "wegame.exe")
                    if os.path.isfile(cand):
                        return cand
        return None

    def _start_wegame_exe(self, path):
        """启动 wegame.exe（os.startfile 自动处理运行权限）。"""
        try:
            os.startfile(path)
            self._log(f"🚀 已启动 WeGame: {path}")
            self._set_status("WeGame 已启动", self.SUCCESS)
        except Exception as e:
            self._log(f"❌ 启动 WeGame 失败: {e}")
            messagebox.showerror("启动失败", f"无法启动 WeGame:\n{e}")

    def _launch_wegame(self):
        """一键启动 WeGame: 快速查找 -> 未命中则磁盘扫描 -> 手动选择兜底。"""
        path = self._find_wegame_path(scan_drives=False)
        if path:
            self._start_wegame_exe(path)
            return
        self._log("⏳ 快速查找未命中，正在扫描磁盘定位 WeGame…")

        def _worker():
            found = self._find_wegame_path(scan_drives=True)
            if found:
                try:
                    self.settings["wegame_path"] = found
                    self._save_settings()
                except Exception:
                    pass
                self.root.after(
                    0, lambda: (self._update_wegame_hint(), self._start_wegame_exe(found))
                )
            else:
                self.root.after(0, self._prompt_manual_wegame)

        threading.Thread(target=_worker, daemon=True).start()

    def _prompt_manual_wegame(self):
        self._update_wegame_hint()
        if messagebox.askyesno(
            "未找到 WeGame",
            "自动查找未找到 WeGame。\n\n是否手动选择 wegame.exe 的位置？\n"
            "(网吧/绿色版可自行定位到可执行文件)",
        ):
            self._choose_wegame_path(launch_after=True)

    def _choose_wegame_path(self, launch_after=False):
        """手动选择 wegame.exe 并记住路径。"""
        initial = self.settings.get("wegame_path") or ""
        path = filedialog.askopenfilename(
            title="选择 WeGame 启动程序 (wegame.exe)",
            initialdir=os.path.dirname(initial) if initial else "C:\\",
            filetypes=[("WeGame 程序", "*.exe"), ("所有文件", "*.*")],
        )
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
        """刷新界面上的 WeGame 路径状态提示。"""
        p = self.settings.get("wegame_path") or ""
        if p and os.path.isfile(p):
            self.wegame_hint_var.set(f"WeGame 路径: {p}")
            return
        saved = self._find_wegame_path(scan_drives=False)
        if saved:
            try:
                self.settings["wegame_path"] = saved
                self._save_settings()
            except Exception:
                pass
            self.wegame_hint_var.set(f"已自动找到 WeGame: {saved}")
            return
        self.wegame_hint_var.set("未找到 WeGame，点「启动 WeGame」自动查找或「选择路径…」手动指定")

    def _create_wegame_shortcut(self):
        """在桌面创建指向 wegame.exe 的快捷方式（网吧桌面常缺 WeGame 入口）。"""
        path = self.settings.get("wegame_path") or ""
        if not (path and os.path.isfile(path)):
            path = self._find_wegame_path(scan_drives=True)
        if not path:
            if messagebox.askyesno(
                "未找到 WeGame",
                "自动查找未找到 WeGame，是否手动选择 wegame.exe 位置？",
            ):
                self._choose_wegame_path(launch_after=False)
                path = self.settings.get("wegame_path") or ""
        if not path or not os.path.isfile(path):
            self._log("⚠ 未设置 WeGame 路径，无法创建快捷方式")
            return
        try:
            desktop = _get_desktop_path()
            lnk = os.path.join(desktop, "启动 WeGame.lnk")
            ps_cmd = (
                "$ws = New-Object -ComObject WScript.Shell; "
                "$sc = $ws.CreateShortcut('{}'); "
                "$sc.TargetPath = '{}'; "
                "$sc.WorkingDirectory = '{}'; "
                "$sc.Save()"
            ).format(
                lnk.replace("'", "''"),
                path.replace("'", "''"),
                os.path.dirname(path).replace("'", "''"),
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                check=True, capture_output=True, timeout=30,
            )
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

    # ==========================================
    # UI 辅助方法
    # ==========================================

    def _sync_hex_card_visibility(self):
        """海克斯卡片：有横幅或识别结果时才显示，否则收起。"""
        try:
            has_content = bool(
                self.hex_banner_var.get() or self.hex_result_var.get()
            )
            visible = self._hex_card.winfo_manager() != ""
            if has_content and not visible:
                self._hex_card.pack(fill=tk.X, pady=(0, 8),
                                    before=self._footer)
            elif not has_content and visible:
                self._hex_card.pack_forget()
        except Exception:
            pass

    def _flash_hex_refresh_banner(self):
        """状态条 + 海克斯卡片横幅 + 托盘气泡：刷新后已更新推荐。"""
        msg = "刷新后已更新推荐"
        self._set_status(msg, self.GOLD_GLOW)
        self._log(f"✅ {msg}")
        try:
            if hasattr(self, "hex_banner_var"):
                self.hex_banner_var.set(f"✨ {msg}")
                self._sync_hex_card_visibility()
                if self._hex_banner_clear_after is not None:
                    try:
                        self.root.after_cancel(self._hex_banner_clear_after)
                    except Exception:
                        pass
                self._hex_banner_clear_after = self.root.after(
                    3500, lambda: (self.hex_banner_var.set(""),
                                   self._sync_hex_card_visibility())
                )
        except Exception:
            pass
        try:
            if self.tray:
                self.tray.notify("海克斯推荐", msg)
        except Exception:
            pass
        # 短暂闪烁状态点
        try:
            self.status_dot.config(fg=self.GOLD_GLOW)
            self.root.after(200, lambda: self.status_dot.config(fg=self.SUCCESS))
            self.root.after(400, lambda: self.status_dot.config(fg=self.GOLD_GLOW))
            self.root.after(700, lambda: self.status_dot.config(fg=self.SUCCESS))
        except Exception:
            pass

    def _set_status(self, text, color):
        self.status_var.set(text)
        self.status_label.config(fg=color)
        self.status_dot.config(fg=color)
        self.status_color = color

    def _start_pulse(self):
        """启动状态指示灯脉冲动画"""
        def _pulse():
            if not self.engine_running:
                return
            self._pulse_state = (self._pulse_state + 1) % 20
            # 呼吸灯效果
            brightness = abs(self._pulse_state - 10) / 10.0
            if self.status_color == self.SUCCESS:
                r = int(63 + brightness * 30)
                g = int(185 + brightness * 50)
                b = int(80 + brightness * 30)
                self.status_dot.config(fg=f"#{r:02x}{g:02x}{b:02x}")
            self.root.after(100, _pulse)
        _pulse()

    def _log(self, msg):
        """线程安全的日志方法 (主线程调用)"""
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._append_log(f"[{ts}] {msg}")

    def _log_safe(self, msg):
        """线程安全的日志方法 (后台线程调用)"""
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {msg}")

    def _append_log(self, text):
        """向日志面板追加文本"""
        self.log_text.config(state=tk.NORMAL)

        # 根据内容选择颜色
        tag = "info"
        if "✅" in text or "成功" in text:
            tag = "success"
        elif "❌" in text or "失败" in text or "错误" in text:
            tag = "error"
        elif "⚠" in text or "警告" in text:
            tag = "warning"

        self.log_text.insert(tk.END, text + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    # ==========================================
    # 窗口事件
    # ==========================================

    def _on_close(self):
        """点击关闭按钮"""
        if self.engine_running:
            if messagebox.askyesno("关闭确认",
                    "引擎正在运行中。\n\n"
                    "• 点击「是」关闭程序\n"
                    "• 点击「否」最小化到托盘"):
                self._quit_app()
            else:
                self._minimize_to_tray()
        else:
            self._quit_app()

    def _quit_app(self):
        """完全退出程序"""
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


# ================= 入口点 =================

def _check_admin():
    """检查是否以管理员身份运行"""
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def make_std_streams_safe():
    """把 stdout / stderr 切成"UTF-8 + 编码失败不抛异常"。

    为什么必须做：这个程序的日志里到处是 emoji（✅ ⚠ 🔎 ⏹…）。
    如果进程带着一个 **GBK 编码的控制台**（从 .bat / cmd / 重定向启动就会这样），
    print 一个 emoji 就会抛 UnicodeEncodeError；而异常处理里再 print 一个 emoji
    又会再抛一次，于是启动流程被彻底打断，弹"程序启动时发生错误"。
    打包成 windowed exe 时 stdout 可能是 None（print 会静默丢弃），
    但一旦有控制台/重定向就会变成真实的 GBK 流 —— 所以不能赌。
    """
    import io
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        # 首选：直接改编码
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            continue
        except Exception:
            pass
        # 退路：套一层可容错的 TextIOWrapper（老版本或非标准流）
        try:
            buffer = getattr(stream, "buffer", None)
            if buffer is not None:
                setattr(sys, name, io.TextIOWrapper(
                    buffer, encoding="utf-8", errors="replace", line_buffering=True))
        except Exception:
            pass


def safe_print(msg):
    """无论如何都不抛异常的 print（控制台可能已被关闭/不可写）。"""
    try:
        print(msg)
    except Exception:
        pass


def main():
    # 第一件事：让 stdout/stderr 不会因为 emoji 而炸掉启动流程
    try:
        make_std_streams_safe()
    except Exception:
        pass

    try:
        # 高 DPI 适配
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        # 把本进程降到"低于正常"优先级 —— 让游戏本体优先拿 CPU，消除游戏内卡顿
        try:
            from scripts.config import lower_process_priority
            if lower_process_priority():
                safe_print("[OK] 已把本进程降为低优先级（让 CPU 优先给游戏）")
        except Exception as e:
            safe_print(f"[WARN] 降优先级步骤跳过: {e}")

        # 管理员权限检查
        if not _check_admin():
            # 非管理员: 弹出提示但仍允许运行
            import ctypes
            result = messagebox.askyesno(
                "权限提示",
                "⚠ 当前未以管理员身份运行。\n\n"
                "以下功能可能无法正常工作:\n"
                "• 自动识别英雄联盟客户端 (LCU / lockfile)\n\n"
                "点击「是」以管理员身份重新启动\n"
                "点击「否」继续以普通用户运行"
            )
            if result:
                # 以管理员重启
                try:
                    exe = sys.executable
                    ctypes.windll.shell32.ShellExecuteW(
                        None, "runas", exe,
                        " ".join(sys.argv) if getattr(sys, 'frozen', False) else f'"{sys.argv[0]}"',
                        None, 1
                    )
                    sys.exit(0)
                except Exception:
                    pass  # 用户取消 UAC, 继续普通运行

        app = LauncherApp()
        app.run()

    except Exception as e:
        try:
            messagebox.showerror("nho有手就行 - 启动错误",
                                 f"程序启动时发生错误:\n\n{traceback.format_exc()}")
        except Exception:
            safe_print(f"FATAL: {e}")
            try:
                traceback.print_exc()
            except Exception:
                pass
        sys.exit(1)


# ==========================================
# WeGame 启动器 - 模块级辅助函数
# ==========================================

def _is_wegame_exe(path):
    """判断是否为 wegame.exe（大小写不敏感）。"""
    return bool(path) and os.path.basename(str(path)).lower() == "wegame.exe"


def _expand_wegame_candidates(value):
    """注册表值可能是 exe 路径、安装目录或目录下的子目录，展开为候选 exe 列表。"""
    if not value:
        return []
    s = str(value).strip().strip('"')
    if not s:
        return []
    out = []
    if s.lower().endswith(".exe"):
        out.append(s)
    else:
        out.append(os.path.join(s, "wegame.exe"))
        out.append(os.path.join(s, "WeGame", "wegame.exe"))
        out.append(os.path.join(s, "wegame", "wegame.exe"))
        out.append(os.path.join(s, "Tencent", "wegame", "wegame.exe"))
    return out


# 常见安装路径（快速查找用）
_WEGAME_COMMON_PATHS = (
    r"C:\Program Files (x86)\WeGame\wegame.exe",
    r"C:\Program Files\WeGame\wegame.exe",
    r"C:\WeGame\wegame.exe",
    r"C:\Program Files (x86)\Tencent\WeGame\wegame.exe",
    r"D:\WeGame\wegame.exe",
    r"E:\WeGame\wegame.exe",
    r"F:\WeGame\wegame.exe",
)


def _list_fixed_drives():
    """枚举固定盘符列表（如 ['C:', 'D:', 'E:']）。"""
    if os.name != "nt":
        return []
    drives = []
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        ctypes.windll.kernel32.GetLogicalDriveStringsW(260, buf)
        for d in buf.value.split("\x00"):
            if len(d) >= 3 and d[1:3] == ":\\":
                drives.append(d[:2])
    except Exception:
        for letter in "CDEFGH":
            p = f"{letter}:\\"
            if os.path.exists(p):
                drives.append(f"{letter}:")
    return drives


def _list_dirs(path):
    """列出目录下的一级子目录绝对路径；失败返回空列表。"""
    try:
        return [
            os.path.join(path, n)
            for n in os.listdir(path)
            if os.path.isdir(os.path.join(path, n))
        ]
    except OSError:
        return []


def _get_desktop_path():
    """获取当前用户桌面绝对路径（兼容 OneDrive 桌面重定向）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-Command", "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, timeout=10,
        )
        p = r.stdout.strip()
        if p and os.path.isdir(p):
            return p
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


if __name__ == '__main__':
    main()
