# -*- coding: utf-8 -*-
"""一次性体检：候选词表 vs 已生成图片，列出缺图与孤儿图。"""
import csv
import os

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE)
CSV = os.path.join(PROJECT_ROOT, "data", "vocab_image_list_filtered.csv")
IMG_DIR = os.path.join(PROJECT_ROOT, "practice", "static", "images", "vocab")

with open(CSV, encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))

missing = []
for r in rows:
    name = f"{r['lesson_id']}_{r['word_id']}.png"
    if not os.path.exists(os.path.join(IMG_DIR, name)):
        missing.append((r["lesson_id"], r["word_id"], r.get("kanji") or "",
                        r.get("hiragana") or "", r.get("meaning") or "",
                        r.get("category") or ""))

have = {f for f in os.listdir(IMG_DIR) if f.endswith(".png")}
want = {f"{r['lesson_id']}_{r['word_id']}.png" for r in rows}
orphan = sorted(have - want)

print(f"候选表 {len(rows)} 词 | 已有图 {len(have)} 张")
print(f"\n缺图 {len(missing)} 个：")
for lid, wid, kanji, hira, meaning, cat in missing:
    print(f"  {lid}_{wid} | {kanji}({hira}) {meaning} | {cat}")
print(f"\n孤儿图（有图但不在候选表）{len(orphan)} 张：")
for name in orphan:
    print(f"  {name}")
