#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p .cache/ugg-aram
cd .cache/ugg-aram
npm pack "@champ-r/u.gg-aram"
tar -xzf champ-r-u.gg-aram-*.tgz
test -d package
cd "$ROOT"
python3 scripts/_ci_download_rune_static.py
test -f .cache/champion_en.json
test -d .cache/ugg-aram/package
echo Rune build inputs ready.
