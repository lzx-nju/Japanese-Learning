# -*- coding: utf-8 -*-
"""从 CSV 导入词表为课程（合并式，可反复运行）。

CSV 格式（UTF-8，首行表头会被跳过）：
    写法,读音,释义,备注
    地図,ちず,地图,
    ゲーム,ゲーム,游戏,

合并规则：
- 按（写法, 读音, 释义）判重：已存在的词条原样保留（id、掌握度、复习记录不动）；
- 只新增 CSV 中出现的词条，不删除、不修改已有词条 → 重复导入安全；
- 新词条由 ensure_word_ids 自动分配稳定 id。

用法：
    python.exe practice\\import_csv.py 词表.csv --lesson lesson_n5 --title "JLPT N5 词表"
"""
import argparse
import csv
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app


def read_rows(csv_path):
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)  # 跳过表头
        for lineno, r in enumerate(reader, 2):
            if not r or not any(c.strip() for c in r):
                continue
            while len(r) < 4:
                r.append("")
            kanji, hiragana, meaning, notes = (c.strip() for c in r[:4])
            if not hiragana:
                print(f"  [跳过] 第 {lineno} 行缺读音: {kanji} / {meaning}")
                continue
            rows.append({
                "kanji": kanji or "---",
                "hiragana": hiragana,
                "romaji": "",
                "meaning": meaning,
                "mastery": 0,
                "last_reviewed": None,
                "next_due": None,
                "notes": notes,
            })
    return rows


def main():
    ap = argparse.ArgumentParser(description="CSV 词表导入（合并式）")
    ap.add_argument("csv_path", help="CSV 文件路径（写法,读音,释义,备注）")
    ap.add_argument("--lesson", required=True, help="课程 id，如 lesson_n5")
    ap.add_argument("--title", default=None, help="课程显示名，默认用 lesson id")
    args = ap.parse_args()

    rows = read_rows(args.csv_path)
    if not rows:
        print("CSV 中没有有效词条，未做任何修改。")
        return

    vocab = app.load_vocab()
    lessons = vocab["lessons"]
    existing = lessons.get(args.lesson, {}).get("words", [])

    def key(w):
        return (w.get("kanji", ""), w.get("hiragana", ""), w.get("meaning", ""))

    merged = {}
    for w in existing:
        merged[key(w)] = w  # 已有词条：原样保留
    added = kept = 0
    for row in rows:
        if key(row) in merged:
            kept += 1
            continue
        merged[key(row)] = row
        added += 1

    lessons[args.lesson] = {
        "title": args.title or args.lesson,
        "words": list(merged.values()),  # 已有词在前，新增按 CSV 顺序在后
    }
    app.ensure_word_ids(vocab)
    app.save_vocab(vocab)
    print(f"导入完成: 新增 {added}，已存在保留 {kept}，课程「{args.title or args.lesson}」共 {len(merged)} 词")


if __name__ == "__main__":
    main()
