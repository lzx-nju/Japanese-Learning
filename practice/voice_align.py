#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""音声作品 —— ASR 转写与台本强制对齐

台本是出品方原稿（文本准），但没有时间轴；whisper 的转写自带时间轴但文本
会错（汉字写法、专有名词、静音段的幻觉）。这个模块把两者合起来：
**文本用台本、时间用 ASR**。

做法不是「逐句找最像的那段」（ASR 会多出/漏掉整句，逐句对齐一错错到底），
而是把两边的文本**各自归一后拼成一长串**，用 `difflib.SequenceMatcher` 求
字符级匹配块，再把匹配到的字符映射回「第几句台本」与「第几段 ASR」，取该句
覆盖到的 ASR 段的时间并集。整句插入/缺失（前情提要、自由谈话、ASR 漏掉的
一行）只会挤掉那一处的映射，不会把后面全部错位。

依赖：转写要 faster-whisper（可选装）。模型权重首次会从 HuggingFace 下载，
国内网络可先设 `HF_ENDPOINT=https://hf-mirror.com`。对齐本身只用标准库，
不装 whisper 也能跑（测试就是拿替身转写结果喂进来的）。
"""
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import works  # noqa: E402
import voice  # noqa: E402

# 对齐时低于这个匹配率就不给时间——宁可让这一句退回 TTS 朗读，也不要
# 给一个错位的时间轴（点一句播的是另一句，比没有更糟）
MIN_MATCH_RATE = 0.3

_model_cache = {}


def norm_for_align(s):
    """对齐比较用的归一：NFKC + 片假名→平假名 + 只留字母数字与假名汉字。

    标点、空白、记号全去掉——ASR 的句读与台本几乎不可能一致（whisper 常
    整段不加标点），留着只会把匹配率拉下来。
    """
    return "".join(c for c in works.to_hiragana(s or "") if c.isalnum())


def transcribe(audio_path, model="small", transcriber=None, language="ja"):
    """音频 → [{"text", "t0", "t1"}]（按时间序）。

    transcriber 可注入：签名为 (audio_path) -> [{"text","t0","t1"}]。测试用它
    喂假结果，也就不必装 faster-whisper、更不必真的跑模型。
    """
    if transcriber is not None:
        segs = transcriber(audio_path)
    else:
        segs = _faster_whisper(audio_path, model, language)
    out = []
    for s in segs or []:
        text = voice.clean_text(s.get("text") or "")
        if not text:
            continue
        t0, t1 = s.get("t0"), s.get("t1")
        if t0 is None or t1 is None or t1 <= t0:
            continue
        out.append({"text": text, "t0": float(t0), "t1": float(t1)})
    return out


def _faster_whisper(audio_path, model="small", language="ja"):
    """faster-whisper 转写（模型按 size 缓存，重复导入不必反复加载）。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "ASR 需要 faster-whisper：pip install faster-whisper"
            "（不装也行——有字幕的作品不需要它）") from e
    key = model
    if key not in _model_cache:
        _model_cache[key] = WhisperModel(model, device="cpu", compute_type="int8")
    m = _model_cache[key]
    # vad_filter 去掉静音段：音声里大量耳语/环境音，不过滤容易出幻觉文本
    segments, _info = m.transcribe(str(audio_path), language=language,
                                   vad_filter=True, beam_size=5)
    return [{"text": s.text, "t0": s.start, "t1": s.end} for s in segments]


def align(sentences, segments):
    """台本句子 + ASR 段 → 句子（补上 t0/t1）。文本以台本为准，时间来自 ASR。

    长度对不上不做任何假设，逐句算自己的匹配率：配不上的保持 None，调用方
    照旧退回 TTS 朗读。
    """
    if not sentences or not segments:
        return [dict(s) for s in sentences]

    sent_of_char, script_text = _concat_with_owner(
        [s.get("text") or "" for s in sentences])
    seg_of_char, asr_text = _concat_with_owner(
        [s.get("text") or "" for s in segments])
    if not script_text or not asr_text:
        return [dict(s) for s in sentences]

    # 每句匹配到的字符数，与覆盖到的 ASR 段区间 [min, max]
    matched = [0] * len(sentences)
    span = [None] * len(sentences)
    matcher = difflib.SequenceMatcher(None, script_text, asr_text, autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            si = sent_of_char[a + k]
            gi = seg_of_char[b + k]
            matched[si] += 1
            cur = span[si]
            if cur is None:
                span[si] = [gi, gi]
            else:
                if gi < cur[0]:
                    cur[0] = gi
                if gi > cur[1]:
                    cur[1] = gi

    out = []
    for i, s in enumerate(sentences):
        item = dict(s)
        item["t0"] = item.get("t0")
        item["t1"] = item.get("t1")
        norm_len = len(norm_for_align(s.get("text") or ""))
        ok = (norm_len > 0 and span[i] is not None
              and matched[i] / norm_len >= MIN_MATCH_RATE)
        if ok and item["t0"] is None:
            item["t0"] = segments[span[i][0]]["t0"]
            item["t1"] = segments[span[i][1]]["t1"]
        out.append(item)
    return out


def _concat_with_owner(texts):
    """[文本] → (每个归一字符属于第几个元素, 归一后拼成的长串)。"""
    owner, parts = [], []
    for i, t in enumerate(texts):
        norm = norm_for_align(t)
        if not norm:
            continue
        parts.append(norm)
        owner.extend([i] * len(norm))
    return owner, "".join(parts)


def align_report(sentences):
    """对齐结果的一句话统计（导入报告用）。"""
    total = len(sentences)
    hit = sum(1 for s in sentences if s.get("t0") is not None)
    if not total:
        return "0 句"
    return f"{hit}/{total} 句配上时间轴（{hit / total:.0%}）"
