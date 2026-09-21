# -*- coding: utf-8 -*-
"""预生成所有词条的 TTS 音频。

遍历 vocabulary.json 所有课程的单词，对每个词生成 mp3 音频。
已存在的音频会跳过（断点续传）。句型词条（含 xx）会清洗后再生成。

运行方式：
    python.exe generate_audio.py
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app


def main():
    vocab = app.load_vocab()
    lessons = vocab.get("lessons", {})

    total = 0
    skipped = 0
    generated = 0
    failed = 0

    # 先统计总数
    all_tasks = []
    for lid, ldata in lessons.items():
        for w in ldata.get("words", []):
            ref = f"{lid}:{w['id']}"
            text = app.tts_word_text(w)  # 个别词走 TTS 文本覆盖表（如 購入）
            if not text:
                continue
            all_tasks.append((ref, text))
    total = len(all_tasks)
    print(f"共 {total} 个词条待处理")

    start_time = time.time()
    for i, (ref, text) in enumerate(all_tasks, 1):
        out_path = app.audio_path_for_ref(ref)
        if out_path.exists() and out_path.stat().st_size > 0:
            skipped += 1
            if i % 50 == 0:
                print(f"  [{i}/{total}] 跳过（已存在）")
            continue

        cleaned = app.clean_tts_text(text)
        if not cleaned:
            failed += 1
            continue

        try:
            app.generate_audio_sync(cleaned, out_path)
            generated += 1
            if i % 20 == 0:
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed > 0 else 0
                eta = (total - i) / rate if rate > 0 else 0
                print(f"  [{i}/{total}] 已生成 {generated} 个，跳过 {skipped} 个，"
                      f"失败 {failed} 个 | 速度 {rate:.1f}/s | 剩余 {eta:.0f}s")
        except Exception as e:
            failed += 1
            print(f"  [{i}/{total}] 失败: {ref} ({cleaned!r}) - {e}")

    elapsed = time.time() - start_time
    print(f"\n完成！生成 {generated}，跳过 {skipped}，失败 {failed}，耗时 {elapsed:.0f}s")
    print(f"音频目录: {app.AUDIO_DIR}")


if __name__ == "__main__":
    main()
