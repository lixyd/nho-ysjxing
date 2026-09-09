import json, os, copy
ROOT = "/workspace/aram-helper/lol-aram-mayhem-hextech-helper"
UGG = "/workspace/aram-helper/ugg-extract/package"
PERKSTYLES = "/workspace/aram-helper/perkstyles_14.22.json"
DDRAGON = "/workspace/aram-helper/champion_en.json"
OUT = ROOT + "/data/aram_runes.json"
STYLE_CN = {8000: "精密", 8100: "主宰", 8200: "巫术", 8400: "坚决", 8300: "启迪"}
KEYSTONE_CN = {
    8005: "强攻", 8008: "致命节奏", 8021: "迅捷步法", 8010: "征服者",
    8112: "电刑", 8128: "黑暗收割", 9923: "冰雹之刃",
    8214: "召唤：艾黎", 8229: "奥术彗星", 8230: "风暴骑手的涌动",
    8437: "不灭之握", 8439: "余震", 8465: "守护者",
    8351: "冰川增幅", 8360: "启封的秘籍", 8369: "先攻",
}

ROLE_TEMPLATES = {
    "mage": {"primaryStyleId": 8200, "subStyleId": 8100, "selectedPerkIds": [8229, 8226, 8210, 8237, 8139, 8135, 5008, 5008, 5001], "primary_cn": "巫术 · 奥术彗星", "secondary_cn": "主宰"},
    "marksman": {"primaryStyleId": 8000, "subStyleId": 8100, "selectedPerkIds": [8008, 9111, 9104, 8014, 8139, 8135, 5005, 5008, 5001], "primary_cn": "精密 · 致命节奏", "secondary_cn": "主宰"},
    "fighter": {"primaryStyleId": 8000, "subStyleId": 8400, "selectedPerkIds": [8010, 9111, 9105, 8014, 8473, 8242, 5008, 5008, 5001], "primary_cn": "精密 · 征服者", "secondary_cn": "坚决"},
    "tank": {"primaryStyleId": 8400, "subStyleId": 8000, "selectedPerkIds": [8439, 8446, 8473, 8451, 9111, 9105, 5007, 5001, 5001], "primary_cn": "坚决 · 余震", "secondary_cn": "精密"},
    "assassin": {"primaryStyleId": 8100, "subStyleId": 8000, "selectedPerkIds": [8112, 8143, 8138, 8135, 9111, 8014, 5008, 5008, 5001], "primary_cn": "主宰 · 电刑", "secondary_cn": "精密"},
    "support": {"primaryStyleId": 8400, "subStyleId": 8300, "selectedPerkIds": [8465, 8463, 8473, 8453, 8345, 8347, 5007, 5001, 5001], "primary_cn": "坚决 · 守护者", "secondary_cn": "启迪"},
}
HAND = {
    "Kayn": {"primaryStyleId": 8100, "subStyleId": 8000, "selectedPerkIds": [8128, 8143, 8138, 8135, 9111, 8014, 5008, 5008, 5001], "primary_cn": "主宰 · 黑暗收割", "secondary_cn": "精密", "note": "heuristic (missing from u.gg-aram)"},
    "Mel": {"primaryStyleId": 8200, "subStyleId": 8100, "selectedPerkIds": [8229, 8226, 8210, 8237, 8139, 8135, 5008, 5008, 5001], "primary_cn": "巫术 · 奥术彗星", "secondary_cn": "主宰", "note": "heuristic (new champ)"},
    "Locke": {"primaryStyleId": 8000, "subStyleId": 8100, "selectedPerkIds": [8008, 9111, 9104, 8014, 8139, 8135, 5005, 5008, 5001], "primary_cn": "精密 · 致命节奏", "secondary_cn": "主宰", "note": "heuristic (new champ)"},
    "Yunara": {"primaryStyleId": 8000, "subStyleId": 8100, "selectedPerkIds": [8008, 9111, 9104, 8014, 8139, 8135, 5005, 5008, 5001], "primary_cn": "精密 · 致命节奏", "secondary_cn": "主宰", "note": "heuristic (new champ)"},
    "Zaahen": {"primaryStyleId": 8000, "subStyleId": 8400, "selectedPerkIds": [8010, 9111, 9105, 8014, 8473, 8242, 5008, 5008, 5001], "primary_cn": "精密 · 征服者", "secondary_cn": "坚决", "note": "heuristic (new champ)"},
}
TAG_PRIORITY = ["Marksman", "Assassin", "Mage", "Support", "Tank", "Fighter"]
TAG_TO_ROLE = {"Mage": "mage", "Marksman": "marksman", "Fighter": "fighter", "Tank": "tank", "Assassin": "assassin", "Support": "support"}

def norm(s):
    return "".join(c for c in s if c.isalnum()).lower()

cn_to_en = json.load(open(ROOT + "/data/champions.json", encoding="utf-8"))
ugg_idx = {}
for fn in os.listdir(UGG):
    if fn.endswith(".json") and fn not in ("index.json", "package.json"):
        ugg_idx[norm(fn[:-5])] = fn[:-5]

style_slots = {}
ps = json.load(open(PERKSTYLES))
for style in ps["styles"]:
    sid = int(style["id"])
    style_slots[sid] = [[int(x) for x in slot.get("perks") or []] for slot in (style.get("slots") or [])[:4]]

en_tags = {}
dd = json.load(open(DDRAGON))
for cid, info in dd["data"].items():
    en_tags[norm(cid)] = list(info.get("tags") or [])

def find_stem(en):
    return ugg_idx.get(norm(en))

def role_from_tags(tags):
    for t in TAG_PRIORITY:
        if t in tags:
            return TAG_TO_ROLE[t]
    return "mage"

def reorder(selected, primary, sub):
    def order(ids, style_id, n):
        slots = style_slots.get(style_id) or []
        if not slots:
            return ids[:n]
        ordered, rem = [], list(ids)
        for sp in slots:
            ss = set(sp)
            hit = [x for x in rem if x in ss]
            if hit:
                ordered.append(hit[0]); rem.remove(hit[0])
        ordered.extend(rem)
        return ordered[:n]
    runes, shards = list(selected[:6]), list(selected[6:9])
    prim_set = set()
    for sp in (style_slots.get(primary) or []):
        prim_set |= set(sp)
    sec_set = set()
    for sp in (style_slots.get(sub) or []):
        sec_set |= set(sp)
    primary_ids = [x for x in runes if x in prim_set]
    secondary_ids = [x for x in runes if x in sec_set and x not in prim_set]
    leftover = [x for x in runes if x not in primary_ids and x not in secondary_ids]
    primary_ids = (primary_ids + leftover)[:4]
    secondary_ids = secondary_ids[:2]
    while len(primary_ids) < 4:
        primary_ids.append(primary_ids[-1] if primary_ids else 0)
    while len(secondary_ids) < 2:
        secondary_ids.append(secondary_ids[-1] if secondary_ids else 0)
    return order(primary_ids, primary, 4) + order(secondary_ids, sub, 2) + shards

def primary_cn(psid, ks):
    return "%s · %s" % (STYLE_CN.get(psid, psid), KEYSTONE_CN.get(ks, ks))

champions = {}
en_to_role = {}
stats = {"ugg": 0, "hand": 0, "heuristic": 0}
gaps = []

for cn, en in cn_to_en.items():
    tags = en_tags.get(norm(en), [])
    role = role_from_tags(tags)
    en_to_role[en] = role
    stem = find_stem(en)
    page = None
    if stem:
        data = json.load(open("%s/%s.json" % (UGG, stem)))
        entry = data[0]
        r = entry["runes"][0]
        primary, sub = int(r["primaryStyleId"]), int(r["subStyleId"])
        selected = reorder([int(x) for x in r["selectedPerkIds"]], primary, sub)
        page = {
            "role": role,
            "name": "ARAM助手-%s" % cn,
            "primaryStyleId": primary,
            "subStyleId": sub,
            "selectedPerkIds": selected,
            "primary_cn": primary_cn(primary, selected[0]),
            "secondary_cn": STYLE_CN.get(sub, str(sub)),
            "note": "u.gg-aram winRate=%s pickCount=%s" % (r.get("winRate"), r.get("pickCount")),
        }
        stats["ugg"] += 1
    elif en in HAND:
        page = copy.deepcopy(HAND[en])
        page["role"] = role
        page["name"] = "ARAM助手-%s" % cn
        stats["hand"] += 1
        gaps.append("%s/%s hand-tuned" % (cn, en))
    else:
        page = copy.deepcopy(ROLE_TEMPLATES[role])
        page["role"] = role
        page["name"] = "ARAM助手-%s" % cn
        page["note"] = "heuristic from DDragon tags %s" % (tags or ["?"])
        stats["heuristic"] += 1
        gaps.append("%s/%s heuristic tags=%s" % (cn, en, tags))
    assert len(page["selectedPerkIds"]) == 9
    champions[cn] = page

name_map = {"mage": "法师", "marksman": "射手", "fighter": "战士", "tank": "坦克", "assassin": "刺客", "support": "辅助"}
roles = {}
for role, tmpl in ROLE_TEMPLATES.items():
    pge = copy.deepcopy(tmpl)
    pge["name"] = "ARAM助手-%s" % name_map[role]
    roles[role] = pge

default = copy.deepcopy(ROLE_TEMPLATES["mage"])
default["name"] = "ARAM助手-通用"
default["note"] = "通用法术输出兜底"

hints = {r: [lab] for r, lab in name_map.items()}
for cn, en in cn_to_en.items():
    role = en_to_role[en]
    for x in (en, cn):
        if x not in hints[role]:
            hints[role].append(x)

out = {
    "_meta": {
        "note": "Full ARAM rune coverage 2026-09-09. Primary source champ-r u.gg-aram 14.22.1. Perk IDs reordered for LCU. Missing champs use DDragon tags + heuristics.",
        "source": "champ-r/u.gg-aram",
        "sourceVersion": "14.22.1-v1731961059465",
        "built": "2026-09-09",
        "stats": stats,
        "style_ids": {"精密": 8000, "主宰": 8100, "巫术": 8200, "坚决": 8400, "启迪": 8300},
    },
    "default": default,
    "roles": roles,
    "champions": champions,
    "role_hints": hints,
}

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
    f.write(chr(10))
print("champions", len(champions), "stats", stats, "size", os.path.getsize(OUT))
for g in gaps:
    print("gap", g)
missing = [cn for cn in cn_to_en if cn not in champions]
print("missing", missing)
bad = [cn for cn, pg in champions.items() if len(pg["selectedPerkIds"]) != 9]
print("bad", bad)
