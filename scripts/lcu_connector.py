"""
LCU Connector - 英雄联盟客户端本地 API 连接器

通过读取 LeagueClientUx.exe 的进程信息或 lockfile，
连接客户端本地 API 以自动获取当前英雄（全生命周期覆盖）。

支持三种获取模式:
  1. ChampSelect 阶段: /lol-champ-select/v1/session
  2. InProgress  阶段: /lol-gameflow/v1/session (gameData)
  3. InGame     备用:  Live Client Data API (端口 2999)
"""
import json
import os
import re

import psutil
import requests
import urllib3
from thefuzz import process as fuzz_process, fuzz as fuzz_scorer

# 禁用 SSL 自签名证书警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# LCU alias / Live Client 名 → champions.json 英文键的常见别名（normalize 后）
_ALIAS_ALIASES = {
    "renataglasc": "renata",
    "nunuwillump": "nunu",
    "wukong": "monkeyking",
    "chogath": "chogath",
    "belveth": "belveth",
    "kaisa": "kaisa",
    "khazix": "khazix",
    "leblanc": "leblanc",
    "velkoz": "velkoz",
    "reksai": "reksai",
    "monkeyking": "monkeyking",
}


def normalize_champion_key(value):
    """Robust key: lower, strip spaces/underscores/punctuation; keep CJK."""
    if value is None:
        return ""
    s = str(value).strip().lower()
    if not s:
        return ""
    # drop spaces, underscores, hyphens, apostrophes, dots, ampersands
    s = re.sub(r"[\s_\-'\.&]+", "", s)
    # keep letters, digits, CJK
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s)
    return s

# 常见的英雄联盟安装路径（用于 lockfile 备选读取）
COMMON_INSTALL_PATHS = [
    r"C:\Riot Games\League of Legends",
    r"D:\Riot Games\League of Legends",
    r"E:\Riot Games\League of Legends",
    r"C:\Program Files\Riot Games\League of Legends",
    r"D:\Program Files\Riot Games\League of Legends",
    r"C:\Riot Games\英雄联盟",
    r"D:\Riot Games\英雄联盟",
    r"D:\WeGameApps\英雄联盟",
    r"D:\WeGame\英雄联盟",
    r"E:\WeGame\英雄联盟",
    r"D:\腾讯游戏\英雄联盟",
    r"E:\腾讯游戏\英雄联盟",
]


class LCUConnector:
    """英雄联盟客户端 LCU API 连接器（全生命周期）"""

    LCU_TIMEOUT = 3       # LCU API 请求超时 (秒)
    LIVE_API_TIMEOUT = 2  # Live Client Data API 超时 (秒)

    def __init__(self, champions_json_path):
        self.port = None
        self.auth_token = None
        self.base_url = None
        self._connected = False
        self._summoner_id = None  # 缓存当前召唤师ID

        # 加载 champions.json: 中文名 -> 英文名
        self.cn_to_en = {}
        # 反向映射: 英文名(小写) -> 中文名
        self.en_to_cn = {}
        # normalize(en/alias/cn) -> 中文名
        self.norm_to_cn = {}
        self._load_champions_map(champions_json_path)

        # 英雄 ID -> 中文名映射 (连接后构建)
        self.id_to_cn = {}

    def _load_champions_map(self, path):
        """加载 champions.json 构建中英文映射（含 normalize / alias）"""
        if not os.path.exists(path):
            print(f"   [WARN] LCU: champions.json not found: {path}")
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                self.cn_to_en = json.load(f)
            for cn, en in self.cn_to_en.items():
                if not en:
                    continue
                self.en_to_cn[en.lower()] = cn
                self.en_to_cn[normalize_champion_key(en)] = cn
                self.norm_to_cn[normalize_champion_key(en)] = cn
                self.norm_to_cn[normalize_champion_key(cn)] = cn
            # 别名表指向已有英文键
            for alias_norm, canon_norm in _ALIAS_ALIASES.items():
                cn = self.norm_to_cn.get(canon_norm)
                if cn:
                    self.norm_to_cn[alias_norm] = cn
                    self.en_to_cn[alias_norm] = cn
            print(f"   [OK] heroes loaded: {len(self.cn_to_en)}")
        except Exception as e:
            print(f"   [WARN] champions.json error: {e}")

    def resolve_to_cn(self, raw_name):
        """将任意 EN/CN/带空格/别名 解析为中文名。"""
        if not raw_name:
            return None
        name = str(raw_name).strip()
        if not name:
            return None
        # 已是中文且在表中
        if name in self.cn_to_en:
            return name
        # 直接小写 EN
        cn = self.en_to_cn.get(name.lower())
        if cn:
            return cn
        norm = normalize_champion_key(name)
        if not norm:
            return None
        cn = self.norm_to_cn.get(norm) or self.en_to_cn.get(norm)
        if cn:
            return cn
        # alias 再跳一次
        canon = _ALIAS_ALIASES.get(norm)
        if canon:
            cn = self.norm_to_cn.get(canon)
            if cn:
                return cn
        # 若 normalize 结果本身是中文名
        if norm in self.cn_to_en:
            return norm
        return None

    def _fuzzy_en_to_cn(self, raw_name, threshold=70):
        """对 en_to_cn / champions 英文键做模糊匹配。"""
        if not raw_name or not self.en_to_cn:
            return None
        keys = list({k for k in self.en_to_cn.keys() if k and k.isascii()})
        if not keys:
            return None
        try:
            hit = fuzz_process.extractOne(
                normalize_champion_key(raw_name) or str(raw_name),
                keys,
                scorer=fuzz_scorer.WRatio,
            )
        except Exception:
            hit = None
        if hit and hit[1] >= threshold:
            return self.en_to_cn.get(hit[0])
        return None

    # ==========================================
    # 连接方法
    # ==========================================

    def connect(self):
        """尝试连接到 League 客户端。返回 bool"""
        if self._connect_via_process() or self._connect_via_lockfile():
            return self._finalize_connection()
        self._connected = False
        return False

    def _connect_via_process(self):
        """通过扫描进程命令行参数获取连接信息"""
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if proc.info['name'] and proc.info['name'].lower() == 'leagueclientux.exe':
                        cmdline = proc.info.get('cmdline', [])
                        if not cmdline:
                            continue
                        port = token = None
                        for arg in cmdline:
                            if '--app-port=' in arg:
                                port = arg.split('=', 1)[1]
                            elif '--remoting-auth-token=' in arg:
                                token = arg.split('=', 1)[1]
                        if port and token:
                            self.port = port
                            self.auth_token = token
                            self.base_url = f"https://127.0.0.1:{port}"
                            return True
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception:
            pass
        return False

    def _connect_via_lockfile(self):
        """通过读取 lockfile 获取连接信息"""
        for base_path in COMMON_INSTALL_PATHS:
            lockfile_path = os.path.join(base_path, 'lockfile')
            if os.path.exists(lockfile_path):
                try:
                    with open(lockfile_path, 'r') as f:
                        content = f.read().strip()
                    parts = content.split(':')
                    if len(parts) >= 5:
                        self.port = parts[2]
                        self.auth_token = parts[3]
                        self.base_url = f"https://127.0.0.1:{self.port}"
                        return True
                except Exception:
                    continue
        return False

    def _finalize_connection(self):
        """连接成功后，构建英雄 ID 映射 + 缓存召唤师ID"""
        self._connected = True
        self._build_champion_id_map()
        self._cache_summoner_id()
        print(f"   [OK] LCU connected (port: {self.port})")
        return True

    def _request(self, method, endpoint, **kwargs):
        """向 LCU API 发送请求"""
        if not self.base_url or not self.auth_token:
            return None
        try:
            resp = requests.request(
                method, f"{self.base_url}{endpoint}",
                auth=('riot', self.auth_token),
                verify=False, timeout=self.LCU_TIMEOUT, **kwargs
            )
            return resp
        except requests.exceptions.ConnectionError:
            self._connected = False
            return None
        except Exception:
            return None

    def is_connected(self):
        return self._connected

    # ==========================================
    # 英雄 ID 映射 + 召唤师信息
    # ==========================================

    def _build_champion_id_map(self):
        """从 LCU API 获取英雄数据，构建 ID -> 中文名映射（alias/name/中文灵活匹配）"""
        resp = self._request('GET', '/lol-game-data/assets/v1/champion-summary.json')
        if not resp or resp.status_code != 200:
            return
        try:
            champions = resp.json()
            for champ in champions:
                cid = champ.get('id')
                if cid is None or cid == -1:
                    continue
                alias = champ.get('alias', '') or ''
                name = champ.get('name', '') or ''
                cn_name = None
                # LCU 国服 name 常为中文，直接可用
                if name in self.cn_to_en:
                    cn_name = name
                if not cn_name:
                    cn_name = self.resolve_to_cn(alias)
                if not cn_name:
                    cn_name = self.resolve_to_cn(name)
                if not cn_name and alias:
                    # 扫描 champions.json 英文值做 normalize 比对
                    alias_norm = normalize_champion_key(alias)
                    for cn, en in self.cn_to_en.items():
                        if normalize_champion_key(en) == alias_norm:
                            cn_name = cn
                            break
                if not cn_name and alias:
                    cn_name = self._fuzzy_en_to_cn(alias, threshold=85)
                if cn_name:
                    self.id_to_cn[cid] = cn_name
            print(f"   [OK] ID map: {len(self.id_to_cn)} champions")
        except Exception:
            pass

    def _cache_summoner_id(self):
        """缓存当前登录玩家的召唤师 ID"""
        resp = self._request('GET', '/lol-summoner/v1/current-summoner')
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                self._summoner_id = data.get('summonerId')
            except Exception:
                pass

    # ==========================================
    # 核心: 获取当前阶段 + 获取英雄
    # ==========================================

    def get_gameflow_phase(self):
        """
        获取当前 gameflow 阶段。

        Returns:
            str | None: "None", "Lobby", "ChampSelect", "GameStart",
                        "InProgress", "WaitingForStats", etc.
        """
        if not self._connected:
            return None
        resp = self._request('GET', '/lol-gameflow/v1/gameflow-phase')
        if resp and resp.status_code == 200:
            try:
                return resp.json()  # 返回字符串，如 "ChampSelect"
            except Exception:
                pass
        return None

    def get_champ_select_champion(self):
        """
        选人阶段获取英雄 (ChampSelect)。
        ARAM: 除 championId 外，还读 championPickIntent / selectedSkinId，
        并轮询 myTeam 本地玩家；championId 为 0 时回退 intent。
        """
        if not self._connected:
            return None
        resp = self._request('GET', '/lol-champ-select/v1/session')
        if not resp or resp.status_code != 200:
            return None
        try:
            data = resp.json()
            local_cell_id = data.get('localPlayerCellId')
            my_team = data.get('myTeam') or []
            local_player = None
            if local_cell_id is not None:
                for player in my_team:
                    if player.get('cellId') == local_cell_id:
                        local_player = player
                        break
            # 兜底：标记 is localPlayer / summoner 字段
            if local_player is None:
                for player in my_team:
                    if player.get('isLocalPlayer') or player.get('playerType') == 'LOCAL':
                        local_player = player
                        break
            if local_player is None and len(my_team) == 1:
                local_player = my_team[0]
            if not local_player:
                return None

            def _cid_from_player(p):
                cid = p.get('championId', 0) or 0
                try:
                    cid = int(cid)
                except (TypeError, ValueError):
                    cid = 0
                if cid > 0:
                    return cid
                intent = p.get('championPickIntent', 0) or 0
                try:
                    intent = int(intent)
                except (TypeError, ValueError):
                    intent = 0
                if intent > 0:
                    return intent
                skin = p.get('selectedSkinId', 0) or 0
                try:
                    skin = int(skin)
                except (TypeError, ValueError):
                    skin = 0
                # skinId ≈ championId * 1000 + skinIndex
                if skin >= 1000:
                    return skin // 1000
                return 0

            cid = _cid_from_player(local_player)
            if cid > 0:
                return self.id_to_cn.get(cid)
        except Exception:
            pass
        return None

    def get_gameflow_champion(self):
        """
        加载/游戏阶段获取英雄 (InProgress/GameStart)。
        通过 /lol-gameflow/v1/session 的 gameData 字段。
        """
        if not self._connected:
            return None
        resp = self._request('GET', '/lol-gameflow/v1/session')
        if not resp or resp.status_code != 200:
            return None
        try:
            data = resp.json()
            game_data = data.get('gameData', {})

            # 在 teamOne 和 teamTwo 中查找自己的 summonerId
            all_players = game_data.get('teamOne', []) + game_data.get('teamTwo', [])
            for player in all_players:
                if player.get('summonerId') == self._summoner_id:
                    cid = player.get('championId', 0)
                    if cid and cid > 0:
                        return self.id_to_cn.get(cid)

            # 如果 summonerId 匹配失败，尝试用 playerChampionSelections
            selections = game_data.get('playerChampionSelections', [])
            if selections and self._summoner_id:
                for sel in selections:
                    if sel.get('summonerId') == self._summoner_id:
                        cid = sel.get('championId', 0)
                        if cid and cid > 0:
                            return self.id_to_cn.get(cid)

        except Exception:
            pass
        return None

    def get_ingame_champion(self):
        """
        通过 Live Client Data API 获取游戏内英雄 (端口 2999, 免密)。
        championName 可能是中文或带空格/撇号的英文，做灵活映射。
        """
        try:
            resp = requests.get(
                'https://127.0.0.1:2999/liveclientdata/activeplayer',
                verify=False, timeout=self.LIVE_API_TIMEOUT
            )
            if resp.status_code == 200:
                raw = resp.json().get('championName', '') or ''
                if not raw:
                    return None
                # 已是中文
                if raw in self.cn_to_en:
                    return raw
                cn = self.resolve_to_cn(raw)
                if cn:
                    return cn
                # 模糊匹配 EN 键
                return self._fuzzy_en_to_cn(raw, threshold=70)
        except requests.exceptions.ConnectionError:
            pass
        except Exception:
            pass
        return None

    # ==========================================
    # 统一接口: 自动检测英雄 (全阶段)
    # ==========================================

    def get_champion_auto(self):
        """
        全生命周期自动获取当前英雄。

        不依赖 phase 卡死：始终按
          ChampSelect → GameFlow → Live → ChampSelect again
        尝试；骰子换人后也不会永久粘在旧英雄上（由上层轮询刷新）。

        Returns:
            (str | None, str): (英雄中文名, 数据来源)
        """
        if not self._connected:
            if not self.connect():
                hero = self.get_ingame_champion()
                return (hero, "Live API") if hero else (None, "")

        # 1) ChampSelect（含 intent / reroll）
        hero = self.get_champ_select_champion()
        if hero:
            return hero, "ChampSelect"

        # 2) GameFlow session
        hero = self.get_gameflow_champion()
        if hero:
            return hero, "GameFlow"

        # 3) Live Client
        hero = self.get_ingame_champion()
        if hero:
            return hero, "Live API"

        # 4) 再试 ChampSelect（phase 延迟 / 骰子后短暂不同步）
        hero = self.get_champ_select_champion()
        if hero:
            return hero, "ChampSelect"

        phase = self.get_gameflow_phase()
        return None, phase or ""

    # ==========================================
    # 通用 HTTP 封装 (供 matchmaking / runes 使用)
    # ==========================================

    def get_json(self, endpoint):
        """GET 并返回 JSON；失败返回 None。"""
        resp = self._request('GET', endpoint)
        if resp is None:
            return None
        if resp.status_code == 204:
            return {}
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except Exception:
            return None

    def post_ok(self, endpoint, json_body=None):
        kwargs = {}
        if json_body is not None:
            kwargs['json'] = json_body
        resp = self._request('POST', endpoint, **kwargs)
        return bool(resp is not None and resp.status_code in (200, 201, 204))

    def put_ok(self, endpoint, json_body=None):
        kwargs = {}
        if json_body is not None:
            kwargs['json'] = json_body
        resp = self._request('PUT', endpoint, **kwargs)
        return bool(resp is not None and resp.status_code in (200, 201, 204))

    def delete_ok(self, endpoint):
        resp = self._request('DELETE', endpoint)
        return bool(resp is not None and resp.status_code in (200, 204))

    def post_json(self, endpoint, json_body=None):
        kwargs = {}
        if json_body is not None:
            kwargs['json'] = json_body
        resp = self._request('POST', endpoint, **kwargs)
        if resp is None or resp.status_code not in (200, 201):
            return None
        try:
            return resp.json()
        except Exception:
            return {}

    # ==========================================
    # Live Client Data: 等级 / 死亡状态
    # ==========================================

    def get_live_player_state(self):
        """
        读取 Live Client Data，返回选牌相关信号:
          {
            "level": int,
            "is_dead": bool,           # playerlist.isDead / 血量<=0
            "respawn_timer": float|None,
            "current_health": float|None,
            "champion": str,
            "near_fountain": None,     # Live Client 不提供地图 XY，无法直接判定靠近泉水
            "pick_window_likely": bool # 死亡（回泉水选牌代理）；真正选牌 UI 仍靠 OCR
          }
        不在游戏内时返回 None。
        """
        # 优先 activeplayer
        try:
            resp = requests.get(
                'https://127.0.0.1:2999/liveclientdata/activeplayer',
                verify=False, timeout=self.LIVE_API_TIMEOUT
            )
            if resp.status_code == 200:
                data = resp.json()
                level = data.get('level')
                if level is None:
                    stats = data.get('championStats') or {}
                    level = stats.get('level')
                health = None
                stats = data.get('championStats') or {}
                if 'currentHealth' in stats:
                    health = stats.get('currentHealth')
                elif 'currentHealth' in data:
                    health = data.get('currentHealth')
                is_dead = False
                if health is not None:
                    try:
                        is_dead = float(health) <= 0
                    except (TypeError, ValueError):
                        is_dead = False
                respawn_timer = None
                # playerlist 补充 isDead / respawnTimer（更可靠）
                try:
                    pl = requests.get(
                        'https://127.0.0.1:2999/liveclientdata/playerlist',
                        verify=False, timeout=self.LIVE_API_TIMEOUT
                    )
                    if pl.status_code == 200:
                        my_name = (data.get('summonerName') or '').strip()
                        my_riot = (data.get('riotId') or '').strip()
                        for p in pl.json():
                            name_ok = my_name and p.get('summonerName') == my_name
                            riot_ok = my_riot and (
                                p.get('riotId') == my_riot
                                or p.get('riotIdGameName') == data.get('riotIdGameName')
                            )
                            if name_ok or riot_ok:
                                if 'isDead' in p:
                                    is_dead = bool(p.get('isDead'))
                                if level is None and p.get('level') is not None:
                                    level = p.get('level')
                                if p.get('respawnTimer') is not None:
                                    try:
                                        respawn_timer = float(p.get('respawnTimer'))
                                    except (TypeError, ValueError):
                                        respawn_timer = None
                                break
                except Exception:
                    pass
                # Live Client 无地图坐标；死亡 ≈ 回泉水/可开选牌窗口的代理信号
                pick_window_likely = bool(is_dead)
                return {
                    "level": int(level or 0),
                    "is_dead": bool(is_dead),
                    "respawn_timer": respawn_timer,
                    "current_health": health,
                    "champion": data.get('championName') or '',
                    "near_fountain": None,
                    "pick_window_likely": pick_window_likely,
                }
        except requests.exceptions.ConnectionError:
            return None
        except Exception:
            return None
        return None
