
import json, os, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache"
CACHE.mkdir(parents=True, exist_ok=True)
DD = "https://" + "ddragon.leagueoflegends.com"
CD = "https://" + "raw.communitydragon.org"
ver = json.load(urllib.request.urlopen(DD + "/api/versions.json", timeout=30))[0]
print("Data Dragon", ver)
champ_url = DD + "/cdn/%s/data/en_US/champion.json" % ver
with urllib.request.urlopen(champ_url, timeout=60) as r:
    (CACHE / "champion_en.json").write_bytes(r.read())
perk_url = CD + "/latest/plugins/rcp-be-lol-game-data/global/default/v1/perkstyles.json"
try:
    with urllib.request.urlopen(perk_url, timeout=60) as r:
        (CACHE / "perkstyles.json").write_bytes(r.read())
except Exception as e:
    print("perkstyles optional fail:", e)
print("static inputs ok")
