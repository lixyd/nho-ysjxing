"""
英雄头像缓存（备战席瓦片用）

取图优先级：
  1. LCU 本地官方资源 /lol-game-data/assets/v1/champion-icons/{id}.png
     （客户端自带，离线可用，不爬第三方站）
  2. Data Dragon CDN（需要联网，用 Champions.json 的英文键做别名）
  3. 都没有 → 返回 None，UI 用字母占位图兜底

设计约束（重要）：
  Pillow 的 C 层不是线程安全的 —— 一旦工作线程与主线程同时执行
  Image.blend / resize 之类的 C 调用，会在 C 层死锁（实测主线程卡在
  ImageEnhance.Color.enhance → Image.blend 里再也出不来）。
  因此：**工作线程只做「下载原始字节 + 落盘」，一律不碰 PIL**；
  所有解码/缩放都放在主线程的 get() 里完成。

磁盘缓存：data/icon_cache/{id}.png（原子写入，避免半截文件）
"""
from __future__ import annotations

import os
import queue
import threading

ICON_PX = 128         # 缓存与解码尺寸（正方形头像；显示 104px，高 DPI 不糊）
LCU_ICON_PATHS = (
    "/lol-game-data/assets/v1/champion-icons/{cid}.png",
    "/lol-game-data/assets/v1/champion-square-icons/{cid}.png",
)
DDRAGON_VERSION_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_ICON = "https://ddragon.leagueoflegends.com/cdn/{ver}/img/champion/{alias}.png"
DDRAGON_CHAMPS = "https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/champion.json"


class ChampIconCache:
    """线程安全的英雄头像缓存。

    主线程调用 get(cid)：有图返回 PIL.Image，没有则入队拉取并返回 None。
    工作线程只下载字节写盘，不执行任何 PIL 操作。
    """

    def __init__(self, lcu, cache_dir, on_loaded=None):
        self.lcu = lcu
        self.cache_dir = cache_dir
        self.on_loaded = on_loaded          # callable(cid) —— 有新图可用时回调
        self._mem = {}                      # cid -> PIL.Image(RGBA, ICON_PX²)
        self._pending = set()
        self._failed = set()                # 拉不到的，别反复重试刷屏
        self._lock = threading.Lock()
        self._q = queue.Queue()
        self._ddragon_ver = None
        self._cid_map = None
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except Exception:
            pass
        threading.Thread(target=self._worker, daemon=True).start()

    # ---------- 对外（只在主线程调用） ----------

    def get(self, cid):
        """已缓存返回 PIL.Image，否则返回 None 并排队拉取。

        PIL 操作（open/resize）全部发生在调用者线程，即主线程。
        """
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            return None
        with self._lock:
            img = self._mem.get(cid)
        if img is not None:
            return img

        path = self._path(cid)
        if os.path.exists(path):
            img = self._load(path)
            if img is not None:
                with self._lock:
                    self._mem[cid] = img
                return img

        self._enqueue(cid)
        return None

    def peek(self, cid):
        """只查内存缓存，不入队。"""
        try:
            with self._lock:
                return self._mem.get(int(cid))
        except (TypeError, ValueError):
            return None

    # ---------- 内部 ----------

    def _path(self, cid):
        return os.path.join(self.cache_dir, f"{cid}.png")

    def _load(self, path):
        """解码 + 缩放到 ICON_PX²（主线程内执行）。"""
        try:
            from PIL import Image
            import io as _io
            with open(path, "rb") as f:
                data = f.read()
            if not data:
                return None
            img = Image.open(_io.BytesIO(data))
            img.load()
            return img.convert("RGBA").resize((ICON_PX, ICON_PX), Image.LANCZOS)
        except Exception:
            return None

    def _enqueue(self, cid):
        with self._lock:
            if cid in self._pending or cid in self._mem or cid in self._failed:
                return
            self._pending.add(cid)
        self._q.put(cid)

    # ---------- 工作线程：纯 IO，不碰 PIL ----------

    def _fetch_bytes(self, cid):
        """按优先级拿到原始 PNG 字节；失败返回 None。"""
        # 1) LCU 本地资源
        try:
            if self.lcu is not None:
                for tpl in LCU_ICON_PATHS:
                    raw = self.lcu.get_bytes(tpl.format(cid=cid))
                    if raw:
                        return raw
        except Exception:
            pass

        # 2) Data Dragon（用 en 别名）
        alias = self._alias(cid)
        ver = self._version()
        if alias and ver:
            try:
                import requests
                import urllib3
                urllib3.disable_warnings()
                r = requests.get(DDRAGON_ICON.format(ver=ver, alias=alias), timeout=6)
                if r.status_code == 200 and r.content:
                    return r.content
            except Exception:
                pass
        return None

    def _alias(self, cid):
        """cid → Data Dragon 英文键（图标文件名用的就是它）。

        优先走 LCU 映射（客户端在线时最准）；离线时用 Data Dragon 自带的
        champion.json（"key" 字段就是数字 championId）兜底，并落盘缓存放批量拉图。
        """
        try:
            cn = self.lcu.id_to_cn.get(cid) if self.lcu else None
            if cn:
                en = self.lcu.cn_to_en.get(cn)
                if en:
                    return en
        except Exception:
            pass
        try:
            return self._ddragon_cid_map().get(int(cid))
        except Exception:
            return None

    def _ddragon_cid_map(self):
        """{championId: 英文键}，来自 Data Dragon champion.json，带磁盘缓存。"""
        if self._cid_map is not None:
            return self._cid_map
        cache = os.path.join(self.cache_dir, "ddragon_champions.json")
        data = None
        try:
            if os.path.exists(cache):
                import json
                with open(cache, "r", encoding="utf-8") as f:
                    data = json.load(f)
        except Exception:
            data = None
        if not data:
            ver = self._version()
            if not ver:
                return {}
            try:
                import json
                import requests
                r = requests.get(DDRAGON_CHAMPS.format(ver=ver), timeout=8)
                if r.status_code == 200:
                    data = r.json().get("data") or {}
                    with open(cache, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
            except Exception:
                return {}
        m = {}
        for key, info in (data or {}).items():
            try:
                m[int(info.get("key"))] = key
            except (TypeError, ValueError):
                continue
        self._cid_map = m
        return m

    def _version(self):
        if self._ddragon_ver:
            return self._ddragon_ver
        ver_file = os.path.join(self.cache_dir, "ddragon_version.txt")
        try:
            if os.path.exists(ver_file):
                with open(ver_file, "r", encoding="utf-8") as f:
                    v = f.read().strip()
                if v:
                    self._ddragon_ver = v
                    return v
        except Exception:
            pass
        try:
            import requests
            r = requests.get(DDRAGON_VERSION_URL, timeout=6)
            if r.status_code == 200:
                self._ddragon_ver = r.json()[0]
                with open(ver_file, "w", encoding="utf-8") as f:
                    f.write(self._ddragon_ver)
                return self._ddragon_ver
        except Exception:
            pass
        return None

    def _save(self, cid, raw):
        """原子写盘：先写 .tmp 再替换，避免主线程读到半截文件。"""
        final = self._path(cid)
        tmp = final + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, final)
            return True
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return False

    def _worker(self):
        while True:
            cid = self._q.get()
            try:
                raw = self._fetch_bytes(cid)
                ok = bool(raw) and self._save(cid, raw)
            except Exception:
                ok = False
            with self._lock:
                self._pending.discard(cid)
                if not ok:
                    self._failed.add(cid)
            if ok and self.on_loaded:
                try:
                    self.on_loaded(cid)      # 只通知，不做 PIL
                except Exception:
                    pass
