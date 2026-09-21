# -*- coding: utf-8 -*-
"""检查与修复 TTS 音频。

用法：
    python.exe practice\\check_audio.py            # 扫描低于大小阈值的音频（严重截断）
    python.exe practice\\check_audio.py --fix      # 扫描并重新生成
    python.exe practice\\check_audio.py --regen lesson_duolingo:w117 [ref2 ...]
        # 强制重录指定词条：多次生成取最大文件（新文件不比旧文件小才替换）
    python.exe practice\\check_audio.py --sweep
        # 全库清扫：每个词条重新生成一次，仅当新文件明显更大（>300B，约
        # 相当于一个假名）时替换。用于一次性清除历史上断流生成的坏文件。

说明：
- 大小阈值只能发现「整体明显偏短」的严重截断；edge-tts 更常见的失效是
  生成当日网络断流，音频流开头丢失，表现为开头一个假名没发音，
  文件仅比正常小 ~8%，与静音填充波动难以区分，无法靠阈值自动发现。
- 听练习时若发现某个词发音不完整，用 --regen 重录该词条；
  或跑一次 --sweep 全库自愈（edge-tts 输出在服务正常时是确定性的，
  完好文件重新生成大小不变，不会被替换）。
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

REGEN_ATTEMPTS = 4  # 重录时的生成次数（取最大文件）


def scan_problems(vocab):
    """返回疑似严重截断的音频列表 [(ref, text, current_size, min_size)]。"""
    lessons = vocab.get("lessons", {})
    problems = []
    for lid, ldata in lessons.items():
        for w in ldata.get("words", []):
            ref = f"{lid}:{w['id']}"
            text = app.clean_tts_text(app.tts_word_text(w))
            if not text:
                continue
            out_path = app.audio_path_for_ref(ref)
            if not out_path.exists():
                continue
            current = out_path.stat().st_size
            min_size = app.estimate_min_audio_size(text)
            if current < min_size:
                problems.append((ref, text, current, min_size))
    return problems


def regen_keep_largest(ref, text, attempts=REGEN_ATTEMPTS):
    """重录单个词条：生成 attempts 次取最大文件，且不比旧文件小才替换。

    返回 (ok, old_size, new_size)。
    """
    out_path = app.audio_path_for_ref(ref)
    old_size = out_path.stat().st_size if out_path.exists() else 0

    best_path, best_size = None, 0
    for i in range(attempts):
        tmp = out_path.with_suffix(f".regen{i}.mp3")
        if tmp.exists():
            tmp.unlink()
        try:
            app.generate_audio_sync(text, tmp)
        except Exception as e:
            print(f"    生成异常: {e}")
            continue
        size = tmp.stat().st_size if tmp.exists() else 0
        print(f"    第 {i + 1}/{attempts} 次: {size}B")
        if size > best_size:
            if best_path is not None:
                best_path.unlink()
            best_path, best_size = tmp, size
        elif tmp.exists():
            tmp.unlink()

    if best_path is None:
        return False, old_size, 0
    if best_size < old_size:
        best_path.unlink()
        print(f"    保留旧文件（旧 {old_size}B > 新 {best_size}B）")
        return True, old_size, old_size
    best_path.replace(out_path)
    return True, old_size, best_size


def sweep_all(vocab, min_delta=300):
    """全库清扫：逐词重新生成一次，新文件比旧文件大 min_delta 字节以上才替换。

    返回 (checked, replaced) 列表信息。
    """
    import time
    lessons = vocab.get("lessons", {})
    tasks = []
    for lid, ldata in lessons.items():
        for w in ldata.get("words", []):
            text = app.clean_tts_text(app.tts_word_text(w))
            if text:
                tasks.append((f"{lid}:{w['id']}", text))
    print(f"清扫开始: 共 {len(tasks)} 个词条（仅替换明显变大的文件）")
    replaced, kept, failed = [], 0, 0
    t0 = time.time()
    for i, (ref, text) in enumerate(tasks, 1):
        out_path = app.audio_path_for_ref(ref)
        old_size = out_path.stat().st_size if out_path.exists() else 0
        tmp = out_path.with_suffix(".sweep.mp3")
        if tmp.exists():
            tmp.unlink()
        try:
            app.generate_audio_sync(text, tmp)
        except Exception as e:
            failed += 1
            print(f"  [{i}/{len(tasks)}] {ref} 生成异常: {e}")
            continue
        new_size = tmp.stat().st_size if tmp.exists() else 0
        if new_size > old_size + min_delta:
            tmp.replace(out_path)
            replaced.append((ref, text, old_size, new_size))
            print(f"  [{i}/{len(tasks)}] 替换 {ref} ({text}): {old_size}B -> {new_size}B")
        else:
            kept += 1
            if tmp.exists():
                tmp.unlink()
        if i % 50 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            print(f"  ... 进度 {i}/{len(tasks)}，已替换 {len(replaced)} | {rate:.1f}词/s | 剩余 {(len(tasks)-i)/rate if rate else 0:.0f}s")
    print(f"\n清扫完成: 检查 {len(tasks)}，替换 {len(replaced)}，保留 {kept}，失败 {failed}，耗时 {time.time()-t0:.0f}s")
    return replaced


def main():
    vocab = app.load_vocab()
    args = sys.argv[1:]

    if "--sweep" in args:
        replaced = sweep_all(vocab)
        if replaced:
            print("\n被替换的词条（旧文件疑似断流缺开头）:")
            for ref, text, old, new in replaced:
                print(f"  {ref} {text}: {old}B -> {new}B")
        return

    if "--regen" in args:
        refs = [a for a in args if not a.startswith("--")]
        if not refs:
            print("用法: python.exe practice\\check_audio.py --regen <ref> [ref2 ...]")
            return
        for ref in refs:
            resolved = app.resolve_word(vocab, ref)
            if not resolved:
                print(f"[{ref}] 未找到词条")
                continue
            text = app.clean_tts_text(app.tts_word_text(resolved[1]))
            if not text:
                print(f"[{ref}] 无可朗读文本")
                continue
            print(f"[{ref}] {resolved[1].get('kanji', '')}({text}) 重录中...")
            ok, old, new = regen_keep_largest(ref, text)
            print(f"  -> {'完成' if ok else '失败'}: {old}B -> {new}B")
        return

    fix_mode = "--fix" in args
    problems = scan_problems(vocab)

    if not problems:
        total = sum(len(l.get("words", [])) for l in vocab.get("lessons", {}).values())
        print(f"扫描完成，所有 {total} 个词条音频均达到大小阈值。")
        return

    print(f"发现 {len(problems)} 个疑似截断的音频：\n")
    print(f"{'ref':<36} {'当前':>8} {'阈值':>8}  文本")
    print("-" * 84)
    for ref, text, cur, mn in problems:
        print(f"{ref:<36} {cur:>8}B {mn:>8}B  {text}")

    if not fix_mode:
        print(f"\n共 {len(problems)} 个问题。加 --fix 参数以重新生成，"
              f"或用 --regen <ref> 重录单个词条。")
        return

    print(f"\n开始修复 {len(problems)} 个音频...")
    fixed = failed = 0
    for i, (ref, text, _, _) in enumerate(problems, 1):
        print(f"  [{i}/{len(problems)}] {ref} ({text})")
        ok, old, new = regen_keep_largest(ref, text)
        if ok:
            fixed += 1
        else:
            print(f"    生成失败")
            failed += 1
    print(f"\n修复完成：成功 {fixed}，失败 {failed}")


if __name__ == "__main__":
    main()
