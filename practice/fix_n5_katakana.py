# -*- coding: utf-8 -*-
"""修正 N5 词表里被写成平假名的外来语（58 条）。

源数据 data/n5_raw.csv 里这些词是片假名（アパート / エレベーター），导入时被转
成了平假名（あぱーと / えれべーたー），于是词条写法跟例句对不上（例句是「私は
アパートに住んでいます」），「片假名转平假名」模式也白白少了一批题。

改法沿用既有约定：kanji 仍是 "---"，片假名记在 hiragana（读音）字段——与多邻国
词表里「タクシー / アイスクリーム」的存法一致。读音变了，按 ref 缓存的 TTS 音频
就过时（文件名只含 ref），照 /api/words/edit 的老规矩删掉，下次播放自动重生成。

判定基准取自 n5_raw.csv 里「词形＝读音」的纯假名词，不靠猜（片假名的长音符 ー
在平假名里也一样，肉眼分不出来）。幂等，重复运行不再有命中。

用法：
    python.exe practice\\fix_n5_katakana.py            # 预览（默认）
    python.exe practice\\fix_n5_katakana.py --write     # 回写并清过期音频
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

RAW_CSV = app.BASE_DIR / "data" / "n5_raw.csv"


def source_kana_words():
    """源数据里「词形与读音相同」的纯假名词集合（即外来语），作为片假名基准。注意：本脚本与练习服务是两个进程，app._DATA_LOCK 管不了跨进程——
运行前先停掉练习服务（或避开正在练习的时间），否则并发写 vocabulary.json
可能互相覆盖、丢练习进度。
"""
    with open(RAW_CSV, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    return {r[0] for r in rows if len(r) >= 2 and r[0] and r[0] == r[1]}


def to_katakana(s):
    return s.translate(str.maketrans({chr(c): chr(c + 0x60) for c in range(0x3041, 0x3097)}))


def main():
    ap = argparse.ArgumentParser(description="修正被写成平假名的外来语")
    ap.add_argument("--write", action="store_true", help="回写 vocabulary.json（默认只预览）")
    ap.add_argument("--lesson", default="lesson_n5", help="课程 id（默认 lesson_n5）")
    args = ap.parse_args()

    kana = source_kana_words()
    vocab = app.load_vocab()
    words = vocab["lessons"][args.lesson]["words"]

    hits = []
    for w in words:
        if (w.get("kanji") or "") != "---":
            continue  # 有汉字写法的不是外来语（ゴミ箱 这类混写词也不动）
        hira = w.get("hiragana") or ""
        kata = to_katakana(hira)
        if kata != hira and kata in kana:
            hits.append((w, hira, kata))

    print(f"{args.lesson}：检出 {len(hits)} 条外来语写成平假名")
    for w, hira, kata in hits[:15]:
        print(f"  {w['id']}  {hira}  →  {kata}   （{w.get('meaning', '')}）")
    if len(hits) > 15:
        print(f"  ……另有 {len(hits) - 15} 条")
    if not hits:
        return
    if not args.write:
        print("\n预览模式，未改动任何文件；加 --write 生效。")
        return

    removed = 0
    for w, hira, kata in hits:
        w["hiragana"] = kata
        cached = app.audio_path_for_ref(f"{args.lesson}:{w['id']}")
        if cached.exists():
            cached.unlink()
            removed += 1
    app.save_vocab(vocab)
    print(f"\n已写入 {app.VOCAB_PATH}；删除过期音频缓存 {removed} 个（下次播放自动按新读音重生成）")


if __name__ == "__main__":
    main()
