# -*- coding: utf-8 -*-
"""把 JLPT N5 词表的英文释义换成中文（原地改 meaning，id 与学习进度原样保留）。

为什么不用 import_csv.py：它按（写法, 读音, 释义）三元组判重，释义一变就被当成
新词另存一条，旧词条连同掌握度、复习记录一起留着，词表直接翻倍。这里只改
meaning 一个字段，其余字段（id / mastery / last_reviewed / next_due / notes /
例句）一律不动，重复运行幂等。

译表 data/n5_zh.csv，列：ref,写法,读音,中文释义,原英文释义。
译文要改就改这张表再跑一次，不用碰代码。

用法：
    python.exe practice\\apply_n5_zh.py             # 预览（默认，不改文件）
    python.exe practice\\apply_n5_zh.py --write      # 回写 vocabulary.json
    python.exe practice\\apply_n5_zh.py --check      # 统计各课程还剩多少条英文释义
"""
import argparse
import csv
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

CSV_PATH = app.BASE_DIR / "data" / "n5_zh.csv"
LATIN = re.compile(r"[A-Za-z]")
CJK = re.compile(r"[一-鿿]")


def load_table(path):
    """读译表，跳过表头与空行，返回 [ref, 写法, 读音, 中文释义, 原英文释义] 列表。"""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)  # 表头
        return [r for r in reader if r and any(c.strip() for c in r)]


def check_remaining(vocab):
    """各课程释义里的英文情况，分两类报：

    - 纯英文：整条释义没有汉字 → 该翻成中文（这才是残留）；
    - 双语标注：外来语「英文，中文」 → 刻意为之，片假名多半就是英文读音变来的。
    注意：本脚本与练习服务是两个进程，app._DATA_LOCK 管不了跨进程——
运行前先停掉练习服务（或避开正在练习的时间），否则并发写 vocabulary.json
可能互相覆盖、丢练习进度。
"""
    print("各课程释义里的英文统计：")
    pure_total = bi_total = 0
    for lesson_id, lesson in vocab["lessons"].items():
        words = lesson.get("words", [])
        pure = bi = 0
        for w in words:
            m = w.get("meaning") or ""
            if not LATIN.search(m):
                continue
            if CJK.search(m):
                bi += 1
            else:
                pure += 1
        pure_total += pure
        bi_total += bi
        if pure or bi:
            print(f"  {lesson_id:<24} 纯英文 {pure:>3}  双语标注 {bi:>3}"
                  f"  / 共 {len(words)} 词  （{lesson.get('title', '')}）")
    print(f"合计：纯英文 {pure_total} 条（待处理），双语标注 {bi_total} 条（预期）")


def main():
    ap = argparse.ArgumentParser(description="N5 词表释义中文化（原地改 meaning）")
    ap.add_argument("--write", action="store_true", help="回写 vocabulary.json（默认只预览）")
    ap.add_argument("--check", action="store_true", help="只统计剩余的英文释义")
    ap.add_argument("--csv", default=str(CSV_PATH), help=f"译表路径（默认 {CSV_PATH}）")
    args = ap.parse_args()

    vocab = app.load_vocab()
    if args.check:
        check_remaining(vocab)
        return

    rows = load_table(args.csv)
    if not rows:
        print(f"译表是空的：{args.csv}")
        return

    changed = unchanged = bilingual = 0
    problems = []
    samples = []
    for r in rows:
        ref, kanji, kana, zh = ((r + [""] * 5)[:4])
        ref, kanji, kana, zh = ref.strip(), kanji.strip(), kana.strip(), zh.strip()
        if not ref or not zh:
            problems.append(f"  [缺字段] {r}")
            continue
        resolved = app.resolve_word(vocab, ref)
        if not resolved:
            problems.append(f"  [找不到] {ref} 不在词表里")
            continue
        w = resolved[1]
        # 译表里带的写法/读音只是给人看的核对信息：对不上说明词表排过序或改过字，
        # 按 ref 改照样改得到，但要报出来让人确认改的是不是同一个词
        if (w.get("kanji") or "") != kanji or (w.get("hiragana") or "") != kana:
            problems.append(f"  [写法/读音不符] {ref} 词表是"
                            f" {w.get('kanji')}/{w.get('hiragana')}，译表写的是 {kanji}/{kana}")
        # 外来语是「英文，中文」双语标注（片假名多半就是英文读音变来的，保留英文
        # 更好记），所以只有整条译文没有汉字时（漏译/误填英文）才算异常。
        if LATIN.search(zh) and not CJK.search(zh):
            problems.append(f"  [译文是纯英文] {ref} 「{zh}」")
        elif LATIN.search(zh):
            bilingual += 1
        if w.get("meaning") == zh:
            unchanged += 1
            continue
        samples.append((ref, w.get("kanji", ""), w.get("meaning", ""), zh))
        w["meaning"] = zh
        changed += 1

    print(f"译表 {len(rows)} 条：将更新 {changed}，已相同 {unchanged}，"
          f"双语标注 {bilingual}，异常 {len(problems)}")
    for line in problems[:20]:
        print(line)
    if len(problems) > 20:
        print(f"  ……另有 {len(problems) - 20} 条")
    print("抽样（前 10 条）：")
    for ref, form, before, after in samples[:10]:
        print(f"  {ref} {form}：{before}  →  {after}")

    if not args.write:
        print("\n预览模式，未改动任何文件；确认无误后加 --write。")
        return
    if not changed:
        print("\n没有需要更新的条目，未写入。")
        return
    app.save_vocab(vocab)
    print(f"\n已写入 {app.VOCAB_PATH}（更新 {changed} 条）")


if __name__ == "__main__":
    main()
