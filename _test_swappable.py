"""自动回归：验证「能换上」数据源的三级降级逻辑（不依赖真实客户端）。

1. 客户端 pickable 列表可用 → 直接用它
2. pickable 拿不到 → 用英雄库（拥有+周免）兜底
3. 两个都拿不到 → None（不置灰，宁可不标也不标错）
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)

from scripts.bench_pick import BenchPickService


class StubLCU:
    def __init__(self, pickable, owned):
        self._pickable, self._owned = pickable, owned
        self.id_to_cn = {1: "阿狸"}

    def get_pickable_champion_ids(self):
        return self._pickable

    def get_owned_champion_ids(self):
        return self._owned


sess = {"benchEnabled": True,
        "benchChampions": [{"championId": 1}, {"championId": 9}]}

# 1) pickable 可用
s = BenchPickService(StubLCU({1, 2, 3}, None), None, None)
s._load_swappable(force=True)
assert s._swappable_ids == {1, 2, 3}, s._swappable_ids
items = s._parse_bench(sess)
assert items[0]["swappable"] is True and items[1]["swappable"] is False

# 2) pickable 拿不到 → 拥有列表兜底
s2 = BenchPickService(StubLCU(None, {1}), None, None)
s2._load_swappable(force=True)
assert s2._swappable_ids == {1}, s2._swappable_ids
items2 = s2._parse_bench(sess)
assert items2[0]["swappable"] is True and items2[1]["swappable"] is False

# 3) 都拿不到 → None，不置灰
s3 = BenchPickService(StubLCU(None, None), None, None)
s3._load_swappable(force=True)
assert s3._swappable_ids is None
items3 = s3._parse_bench(sess)
assert items3[0]["swappable"] is None and items3[1]["swappable"] is None

# 4) 首次失败后自动重试（3 秒节流，force 跳过节流）
s4 = BenchPickService(StubLCU(None, None), None, None)
s4._load_swappable(force=True)
assert s4._swappable_ids is None
s4.lcu._owned = {1, 9}      # 客户端过会儿能查到了
s4._swappable_ts = 0.0      # 模拟 3 秒后
s4._load_swappable()
assert s4._swappable_ids == {1, 9}

print("ALL_PASS")
