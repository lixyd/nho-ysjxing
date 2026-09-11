# -*- coding: utf-8 -*-
"""
官方海克斯(augment)数据库构建脚本
=================================

设计原则
--------
不爬虫、不破解、不逆向。全部数据来自 **CommunityDragon**
（Riot 官方补丁文件的逐版本导出镜像），属于"直接读取官方映射"。

主数据源（LCU 官方游戏数据，自带本地化与稀有度，最干净）
--------------------------------------------------------
1. `plugins/rcp-be-lol-game-data/global/<locale>/v1/augment-lists.json`
   各模式的海克斯池。`modeName == "KIWI"` 即 ARAM 海克斯大乱斗
   （客户端代号 kiwi / ARAM Mayhem）。

2. `plugins/rcp-be-lol-game-data/global/<locale>/v1/cherry-augments.json`
   官方海克斯总表：`id` / `augmentNameId` / `nameTRA`(已本地化) / `rarity`
   / 图标路径。552 条，含全部 ARAM_ 条目。

3. `game/maps/modespecificdata/kiwi.bin.json`
   模式数据，提供 `AugmentData`：`DescriptionTra`(描述) / 图标 / `AugmentPlatformId`。

4. `game/<locale>/data/menu/en_us/lol.stringtable.json`
   客户端字符串表，用 `NameTra` / `DescriptionTra` 取中文描述。
   **注意：键是大小写不敏感的**（bin 里写 `Kiwi_ARAM_Archmage`，
   表里是 `kiwi_aram_archmage`），因此必须建小写索引。

输出
----
- `data/augments_official.json`      完整海克斯数据库（唯一真源）
- `data/augment_alias_zh.json`       中文名 -> apiName 索引（供 OCR 匹配/校验）
- `data/augments_rarity_map.json`    稀有度对照表

用法
----
    python scripts/build_augments_official.py                # 用缓存
    python scripts/build_augments_official.py --refresh      # 强制重新下载
    python scripts/build_augments_official.py --mode KIWI    # 指定模式池
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict

CDN = "https://raw.communitydragon.org/latest"
LCU = CDN + "/plugins/rcp-be-lol-game-data/global/{locale}/v1"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
DEFAULT_CACHE = os.path.join(REPO_ROOT, "_augdata")

# 官方 rarity 枚举 -> 中文
RARITY_ZH = {
    "kSilver": "白银",
    "kGold": "黄金",
    "kPrismatic": "棱彩",
    "kEventChoice": "事件抉择",
    "kUnknown": "",
}
# 旧版数值 rarity（kiwi.bin 里的 rarity 字段）-> 中文
NUM_RARITY_ZH = {0: "白银", 1: "黄金", 2: "棱彩", 3: "黄金", 4: "棱彩", 5: "棱彩"}

LCU_FILES = ("augment-lists.json", "cherry-augments.json")


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------
def download(url: str, dest: str, refresh: bool = False, tries: int = 4) -> str:
    if not refresh and os.path.exists(dest) and os.path.getsize(dest) > 1024:
        return dest

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    last = None
    for attempt in range(1, tries + 1):
        try:
            print(f"    [下载] {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "ARAMHelper/1.0"})
            got = 0
            with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 18)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    if got % (1 << 22) < (1 << 18):
                        print(f"           {got/1048576:.1f} MB", end="\r")
            if got > 0:
                json.load(open(tmp, encoding="utf-8"))  # 完整性校验，截断直接抛错
                os.replace(tmp, dest)
                print(f"           完成 {got/1048576:.2f} MB" + " " * 15)
                return dest
            last = RuntimeError("收到 0 字节")
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"           第 {attempt} 次失败: {exc}")
            if os.path.exists(tmp):
                os.remove(tmp)
            time.sleep(2 * attempt)
    raise RuntimeError(f"下载失败 {url}: {last}")


def download_optional(url: str, dest: str, refresh: bool = False):
    try:
        return download(url, dest, refresh=refresh, tries=2)
    except Exception as exc:  # noqa: BLE001
        print(f"    [可选源跳过] {exc}")
        return None


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# 解析工具
# --------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(text: str) -> str:
    if not text:
        return ""
    for a, b in (("<br>", "\n"), ("<br/>", "\n"), ("<br />", "\n"),
                 ("&nbsp;", " "), ("&amp;", "&")):
        text = text.replace(a, b)
    return _TAG_RE.sub("", text).strip()


def walk_dicts(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_dicts(item)


def collect_augment_data(obj) -> dict:
    """摘出全部 AugmentData，按 AugmentNameId 去重（保留字段最全的那份）。"""
    found = {}
    for node in walk_dicts(obj):
        if node.get("__type") == "AugmentData":
            key = node.get("AugmentNameId")
            if not key:
                continue
            if key not in found or len(node) > len(found[key]):
                found[key] = node
    return found


def path_to_api(path: str) -> str:
    return (path or "").rstrip("/").rsplit("/", 1)[-1]


def fetch_patch(cache: str | None = None) -> str:
    """取当前补丁号。优先用缓存，避免联网卡住构建。"""
    if cache:
        cached = os.path.join(cache, "versions.json")
        try:
            with open(cached, "r", encoding="utf-8") as fh:
                return json.load(fh)[0]
        except Exception:  # noqa: BLE001
            pass
    try:
        req = urllib.request.Request(
            "https://ddragon.leagueoflegends.com/api/versions.json",
            headers={"User-Agent": "ARAMHelper/1.0"},
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.load(resp)[0]
    except Exception:  # noqa: BLE001
        return "unknown"


# --------------------------------------------------------------------------
# 主构建
# --------------------------------------------------------------------------
def build(locale: str = "zh_cn", mode: str = "KIWI",
          cache: str = DEFAULT_CACHE, refresh: bool = False):
    os.makedirs(cache, exist_ok=True)

    print(">>> [1/5] 拉取官方源文件")
    lcu = {}
    for name in LCU_FILES:
        url = f"{LCU.format(locale=locale)}/{name}"
        lcu[name] = download(url, os.path.join(cache, name), refresh)

    kiwi_path = download(
        f"{CDN}/game/maps/modespecificdata/kiwi.bin.json",
        os.path.join(cache, "kiwi.bin.json"), refresh,
    )
    st_path = download(
        f"{CDN}/game/{locale}/data/menu/en_us/lol.stringtable.json",
        os.path.join(cache, "lol.stringtable.json"), refresh,
    )
    en_path = download_optional(
        f"{LCU.format(locale='default')}/cherry-augments.json",
        os.path.join(cache, "cherry-augments.en_us.json"), refresh,
    )

    print(f">>> [2/5] 读取 LCU 海克斯总表（{locale}）")
    total_tbl = {str(x.get("augmentNameId") or "").lower(): x
                 for x in load_json(lcu["cherry-augments.json"])}
    en_tbl = {}
    if en_path:
        en_tbl = {str(x.get("augmentNameId") or "").lower(): x
                  for x in load_json(en_path)}
    print(f"    LCU 总表 {len(total_tbl)} 条，英文名 {len(en_tbl)} 条")

    print(f">>> [3/5] 取模式池 modeName={mode}")
    pools = {}
    for item in load_json(lcu["augment-lists.json"]):
        pools[item.get("modeName") or "?"] = [
            path_to_api(p) for p in item.get("augmentList") or []
        ]
    # 支持逗号分隔取并集：国服是 KIWI_JADE 变体，只看 KIWI 会漏 24 个
    modes = [m.strip() for m in str(mode).split(",") if m.strip()]
    unknown = [m for m in modes if m not in pools]
    if unknown:
        raise SystemExit(f"❌ 模式池里没有 {unknown}，可选: {list(pools)}")
    pool_api = set()
    for m in modes:
        pool_api |= set(pools[m])
    print(f"    {mode} → 并集 {len(pool_api)} 个；全部模式: "
          + ", ".join(f"{k}({len(v)})" for k, v in pools.items()))

    print(">>> [4/5] 解析 kiwi.bin.json 与字符串表")
    aug_defs = collect_augment_data(load_json(kiwi_path))
    entries = load_json(st_path).get("entries", {})
    low = {k.lower(): v for k, v in entries.items()}   # 大小写不敏感索引
    print(f"    AugmentData {len(aug_defs)} 个，字符串表 {len(entries)} 条")

    print(">>> [5/5] 组装数据库")
    all_api = set(pool_api) | set(aug_defs) | {k for k in total_tbl if k}
    # total_tbl 的 key 是小写，还原官方大小写
    canon = {k.lower(): (total_tbl.get(k.lower(), {}).get("augmentNameId") or k)
             for k in all_api}
    all_api = {canon.get(k.lower(), k) for k in all_api}

    records, no_name, no_desc = [], [], []
    for api in sorted(all_api):
        key = api.lower()
        lcu_row = total_tbl.get(key, {})
        en_row = en_tbl.get(key, {})
        node = aug_defs.get(api) or aug_defs.get(key) or {}

        name_zh = strip_tags(lcu_row.get("nameTRA") or "") or strip_tags(
            low.get((node.get("NameTra") or "").lower()) or "")
        desc_zh = strip_tags(low.get((node.get("DescriptionTra") or "").lower()) or "")
        name_en = strip_tags(en_row.get("nameTRA") or "")

        if not name_zh:
            no_name.append(api)
        if not desc_zh:
            no_desc.append(api)

        rarity = lcu_row.get("rarity") or ""
        rarity_zh = RARITY_ZH.get(rarity, "")
        if not rarity_zh and node.get("rarity") is not None:
            rarity_zh = NUM_RARITY_ZH.get(node.get("rarity"), "")

        records.append({
            "apiName": api,
            "lcuId": lcu_row.get("id"),
            "platformId": node.get("AugmentPlatformId"),
            "nameZh": name_zh,
            "nameEn": name_en,
            "descZh": desc_zh,
            "rarity": rarity,
            "rarityZh": rarity_zh,
            "inAramPool": api in pool_api,
            "aramSpecific": api.startswith("ARAM_"),
            "iconSmall": (lcu_row.get("augmentSmallIconPath") or "").lstrip("/"),
            "iconLarge": (node.get("AugmentLargeIconPath") or "").replace(".tex", ".png"),
            "rootSpell": node.get("RootSpell") or "",
        })

    records.sort(key=lambda r: (not r["inAramPool"], r["apiName"]))
    pool_records = [r for r in records if r["inAramPool"]]

    return {
        "schema": "aramhelper.augments.official/v2",
        "source": "CommunityDragon（Riot 官方数据镜像）· LCU augment-lists + cherry-augments + kiwi.bin",
        "sourceUrls": {
            "augmentLists": f"{LCU.format(locale=locale)}/augment-lists.json",
            "cherryAugments": f"{LCU.format(locale=locale)}/cherry-augments.json",
            "kiwiBin": f"{CDN}/game/maps/modespecificdata/kiwi.bin.json",
            "stringtable": f"{CDN}/game/{locale}/data/menu/en_us/lol.stringtable.json",
        },
        "locale": locale,
        "modeName": mode,
        "patch": fetch_patch(cache),
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": {
            "definitions": len(records),
            "aramPool": len(pool_records),
            "aramSpecific": sum(1 for r in pool_records if r["aramSpecific"]),
            "shared": sum(1 for r in pool_records if not r["aramSpecific"]),
            "missingZhName": len(no_name),
            "missingDesc": len(no_desc),
        },
        "allModes": {k: len(v) for k, v in pools.items()},
        "missingZhName": sorted(no_name)[:80],
        "missingDesc": sorted(no_desc)[:80],
        "augments": records,
    }


def build_minimal(cache: str = DEFAULT_CACHE, locale: str = "zh_cn",
                  mode: str = "KIWI,KIWI_JADE", preserve_path: str | None = None):
    """轻量重建「识别层」——只拉两个小文件，约 140KB，几秒完成。

    不下载 12MB 的 kiwi.bin.json 和 31MB 的 lol.stringtable.json：
    那两个只用来补**描述文本**，对"认得出是什么海克斯"这件事没有影响。
    已有数据库里的描述 / 图标 / platformId 会按 apiName 原样保留。

    供定时自检使用，因此全程只依赖 CommunityDragon，不依赖任何自建仓库。
    """
    os.makedirs(cache, exist_ok=True)

    lists_path = download(f"{LCU.format(locale=locale)}/augment-lists.json",
                          os.path.join(cache, "augment-lists.json"))
    cherry_path = download(f"{LCU.format(locale=locale)}/cherry-augments.json",
                           os.path.join(cache, "cherry-augments.json"))
    en_path = download_optional(f"{LCU.format(locale='default')}/cherry-augments.json",
                                os.path.join(cache, "cherry-augments.en_us.json"))

    total_tbl = {str(x.get("augmentNameId") or "").lower(): x
                 for x in load_json(cherry_path)}
    en_tbl = {}
    if en_path:
        en_tbl = {str(x.get("augmentNameId") or "").lower(): x
                  for x in load_json(en_path)}

    pools = {}
    for item in load_json(lists_path):
        pools[item.get("modeName") or "?"] = [
            path_to_api(p) for p in item.get("augmentList") or []
        ]
    pool_api = set()
    for m in [m.strip() for m in str(mode).split(",") if m.strip()]:
        pool_api |= set(pools.get(m, []))

    # 保留旧的描述 / 图标 / platformId
    prev = {}
    if preserve_path and os.path.exists(preserve_path):
        try:
            for rec in load_json(preserve_path).get("augments", []):
                prev[rec.get("apiName")] = rec
        except Exception:  # noqa: BLE001
            prev = {}

    canon = {k.lower(): (total_tbl.get(k.lower(), {}).get("augmentNameId") or k)
             for k in (set(pool_api) | set(total_tbl) | set(prev))}

    records = []
    for api in sorted({canon.get(k.lower(), k) for k in canon}):
        lcu_row = total_tbl.get(api.lower(), {})
        old = prev.get(api, {})
        name_zh = strip_tags(lcu_row.get("nameTRA") or "") or old.get("nameZh", "")
        if not name_zh:
            continue
        rarity = lcu_row.get("rarity") or old.get("rarity") or ""
        records.append({
            "apiName": api,
            "lcuId": lcu_row.get("id"),
            "platformId": old.get("platformId"),
            "nameZh": name_zh,
            "nameEn": strip_tags(en_tbl.get(api.lower(), {}).get("nameTRA") or "")
                      or old.get("nameEn", ""),
            "descZh": old.get("descZh", ""),
            "rarity": rarity,
            "rarityZh": RARITY_ZH.get(rarity, "") or old.get("rarityZh", ""),
            "inAramPool": api in pool_api,
            "aramSpecific": api.startswith("ARAM_"),
            "iconSmall": (lcu_row.get("augmentSmallIconPath") or "").lstrip("/"),
            "iconLarge": old.get("iconLarge", ""),
            "rootSpell": old.get("rootSpell", ""),
        })

    records.sort(key=lambda r: (not r["inAramPool"], r["apiName"]))
    pool_records = [r for r in records if r["inAramPool"]]

    return {
        "schema": "aramhelper.augments.official/v2",
        "source": "CommunityDragon · LCU augment-lists + cherry-augments（轻量自检，无描述）",
        "sourceUrls": {
            "augmentLists": f"{LCU.format(locale=locale)}/augment-lists.json",
            "cherryAugments": f"{LCU.format(locale=locale)}/cherry-augments.json",
        },
        "locale": locale,
        "modeName": mode,
        "patch": fetch_patch(cache),
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": {
            "definitions": len(records),
            "aramPool": len(pool_records),
            "aramSpecific": sum(1 for r in pool_records if r["aramSpecific"]),
            "shared": sum(1 for r in pool_records if not r["aramSpecific"]),
            "missingZhName": 0,
            "missingDesc": sum(1 for r in pool_records if not r["descZh"]),
        },
        "allModes": {k: len(v) for k, v in pools.items()},
        "missingZhName": [],
        "missingDesc": [],
        "augments": records,
    }


def pool_signature(db: dict) -> str:
    """数据库内容指纹：池内 apiName + 中文名 + 稀有度。用于判断是否需要更新。"""
    import hashlib
    items = sorted(
        f"{r.get('apiName')}|{r.get('nameZh')}|{r.get('rarity')}"
        for r in db.get("augments", []) if r.get("inAramPool")
    )
    return hashlib.sha1("\n".join(items).encode("utf-8")).hexdigest()[:16]


def write_alias_index(db: dict, data_dir: str) -> str:
    alias = defaultdict(list)
    ph = re.compile(r"^[\?？\s]+$")
    for rec in db["augments"]:
        if not rec["inAramPool"]:
            continue
        for nm in {rec["nameZh"], rec["nameEn"]}:
            # 官方自己也留了占位名（如 ARAM_MissingPingAugment 的名字就是 ？？？），排除掉
            if nm and not ph.match(nm):
                alias[nm].append(rec["apiName"])
    out = {k: (v[0] if len(v) == 1 else sorted(v)) for k, v in sorted(alias.items())}
    path = os.path.join(data_dir, "augment_alias_zh.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    return path


def write_rarity_reference(db: dict, data_dir: str) -> str:
    dist = Counter(r["rarityZh"] or "(无)" for r in db["augments"] if r["inAramPool"])
    path = os.path.join(data_dir, "augments_rarity_map.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "note": "本模式海克斯池的稀有度分布（来自 LCU cherry-augments.json 的 rarity 字段）",
            "modeName": db.get("modeName"),
            "dist": dict(dist),
            "rarityZhMap": RARITY_ZH,
        }, fh, ensure_ascii=False, indent=2)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="构建官方海克斯数据库")
    ap.add_argument("--locale", default="zh_cn")
    ap.add_argument("--mode", default="KIWI,KIWI_JADE",
                    help="模式池名，逗号分隔取并集。ARAM 海克斯大乱斗 = KIWI，"
                         "国服变体 = KIWI_JADE（缺后者会漏 24 个海克斯）")
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "augments_official.json"))
    args = ap.parse_args()

    db = build(locale=args.locale, mode=args.mode, cache=args.cache, refresh=args.refresh)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(db, fh, ensure_ascii=False, indent=2)
    alias_path = write_alias_index(db, DATA_DIR)
    rarity_path = write_rarity_reference(db, DATA_DIR)

    c = db["counts"]
    print("\n=== 构建完成 ===")
    print(f"  补丁版本        : {db['patch']}")
    print(f"  模式池          : {db['modeName']}")
    print(f"  定义总数        : {c['definitions']}")
    print(f"  本模式海克斯池  : {c['aramPool']} (专属 {c['aramSpecific']} / 共用 {c['shared']})")
    print(f"  缺中文名        : {c['missingZhName']}   缺描述: {c['missingDesc']}")
    print(f"  -> {args.out}")
    print(f"  -> {alias_path}")
    print(f"  -> {rarity_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
