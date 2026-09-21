#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""音声作品 —— 导入器：一条音轨 + 一份字幕或台本 → 带时间轴的句子流

落盘与轻小说作品同构（`practice/import_work.py`），所以阅读器那套点词查义 /
挖矿 / 已学词标注一行不用改；差别只在于：

  - 每章多一个 `audio` 字段（**音轨的绝对路径，不复制文件**——wav 动辄几百 MB，
    复制进仓库既没必要也搬不动）；
  - 章节内容多一张扁平的 `times` 表（每句在音频里的起止秒数），有字幕才写。

用法（先停掉练习服务，避免与 /api 的词库写入互踩——README 有警示）：

    # 自动配对：给作品目录，按文件名把音轨与字幕/台本配起来，先看计划
    python.exe practice\\import_voice.py "D:\\path\\to\\作品目录" --dry-run
    python.exe practice\\import_voice.py "D:\\path\\to\\作品目录"

    # 同一作品常同时给「含SE / 无SE」两版音轨，只导入一版即可：
    python.exe practice\\import_voice.py "...\\RJ01140031\\无SE\\mp3"

    # 没有字幕的轨道用 ASR 出时间轴（可选依赖 faster-whisper）
    python.exe practice\\import_voice.py <目录> --align

    python.exe practice\\import_voice.py --list

配对规则（按优先级）：

    字幕 —— `<音轨全名>.vtt`（如 `track1.mp3.vtt`）→ `<主名>.vtt` → 同规则的
            .srt / .ass / .ssa；字幕自带时间轴，优先用它。
    台本 —— `<主名>.txt` / `<主名>.pdf`，同目录或 Script / 台本 这类子目录里；
            文本更准但没有时间轴，要 `--align` 才有点句播放。
    都没有 —— 只能整轨交给 ASR（`--align`），或者当纯音频（不导入）。

音轨去重：同一主名的 mp3 与 wav 只取一个，优先压缩格式（wav 是 mp3 的十倍
体积，而 whisper 反正会重采样到 16kHz）。
"""
import argparse
import datetime
import re
import sys
import unicodedata
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import works      # noqa: E402
import voice      # noqa: E402

# 优先用压缩音轨：wav/flac 只是同一内容的未压缩版
AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".opus", ".ogg", ".flac", ".wav")
COMPRESSED = (".mp3", ".m4a", ".aac", ".opus", ".ogg")
SCRIPT_EXTS = (".txt", ".pdf")
# 台本常见子目录名（小写比较）
SCRIPT_DIR_NAMES = ("script", "scripts", "scenario", "台本", "シナリオ", "せりふ", "セリフ")

Track = namedtuple("Track", "audio subtitle script label")


def natural_key(name):
    """自然序排序键：track2 排在 track10 前面（纯字典序会把 10 排到 2 前）。"""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", name or "")]


def norm_stem(name):
    """主名归一：去扩展名 + NFC + 去空白。用来把音轨与字幕/台本配对。"""
    return unicodedata.normalize("NFC", Path(name).stem).strip()


def _index_by_stem(paths):
    """[{stem: path}]，主名归一后建索引；同主名多个取先出现的。"""
    idx = {}
    for p in paths:
        idx.setdefault(norm_stem(p.name), p)
    return idx


def discover(root, include_dirs=None):
    """扫描作品目录 → [Track]（自然序）。

    返回的 Track 里 subtitle / script 可能为 None（没有文本），由调用方决定
    是报错、跳过还是交给 ASR。
    """
    root = Path(root)
    if root.is_file():
        audio_files = [root]
        search_root = root.parent
    else:
        audio_files = []
        search_root = root
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                continue
            rel = p.relative_to(root).as_posix().lower()
            if include_dirs and not any(d.lower() in rel for d in include_dirs):
                continue
            audio_files.append(p)

    # 同主名去重：**只把「同一轨的压缩版与未压缩版」合成一个**（wav 是 mp3 的
    # 十倍体积，whisper 反正会重采样到 16kHz，留压缩的）。
    # 同一格式的多个同主名文件是**真的不同音轨**——同一作品常同时给「含SE /
    # 无SE」两版，路径不同、主名相同。这种情况全部留着交给用户用 --dir 挑一版：
    # 静默替他选一版（还可能是带音效的那版）是最坏的选择。
    groups = {}
    for p in audio_files:
        groups.setdefault(norm_stem(p.name), []).append(p)
    picked = []          # [(主名, 音轨路径)]
    for stem, paths in groups.items():
        compressed = [p for p in paths if p.suffix.lower() in COMPRESSED]
        if compressed and len(compressed) < len(paths):
            picked.extend((stem, p) for p in compressed)   # 丢掉未压缩的那些
        else:
            picked.extend((stem, p) for p in paths)        # 同格式同主名：各自成轨

    # 字幕 / 台本索引（整棵树扫一遍，主名归一后配对）
    subs, scripts = {}, {}
    for p in search_root.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        name = p.name
        if ext in voice.SUBTITLE_EXTS:
            # `track1.mp3.vtt`：把音轨扩展名也当作主名的一部分去掉
            stem = norm_stem(name)
            if stem.lower().endswith(tuple(AUDIO_EXTS)):
                stem = norm_stem(Path(stem).stem)
            subs.setdefault(stem, []).append(p)
        elif ext in SCRIPT_EXTS:
            scripts.setdefault(norm_stem(name), []).append(p)
    script_dirs = {d.name.lower() for d in search_root.rglob("*")
                   if d.is_dir() and d.name.lower() in SCRIPT_DIR_NAMES}

    tracks = []
    for stem, audio in sorted(picked, key=lambda t: (natural_key(t[0]), str(t[1]))):
        sub = _pick_subtitle(subs.get(stem), script_dirs)
        script = _pick_script(scripts.get(stem), script_dirs)
        tracks.append(Track(audio=audio, subtitle=sub, script=script,
                            label=stem))
    return tracks


def _pick_subtitle(cands, script_dirs):
    """字幕候选里挑一个：优先「不在台本目录里」的（台本目录里偶尔混放字幕）。"""
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    order = {e: i for i, e in enumerate(voice.SUBTITLE_EXTS)}
    return sorted(cands, key=lambda p: (
        any(d in p.parent.name.lower() for d in script_dirs),
        order.get(p.suffix.lower(), 99), str(p)))[0]


def _pick_script(cands, script_dirs):
    """台本候选里挑一个：优先台本目录里的，其次 .txt（pdf 要 pymupdf）。"""
    if not cands:
        return None
    return sorted(cands, key=lambda p: (
        not any(d in p.parent.name.lower() for d in script_dirs),
        p.suffix.lower() != ".txt", str(p)))[0]


def _load_track_text(track, extra_directions=()):
    """一条音轨 → (句子列表, 丢弃行列表, 来源说明)。

    字幕优先（自带时间轴）；退而求其次用台本（无时间轴，等 --align 补）。
    """
    if track.subtitle:
        text = track.subtitle.read_text(encoding="utf-8", errors="replace")
        cues = voice.parse_subtitles(text, track.subtitle.name)
        if cues:
            return (voice.cues_to_sentences(cues, extra_directions), [],
                    f"字幕 {track.subtitle.name}")
    if track.script:
        if track.script.suffix.lower() == ".pdf":
            raw = voice.read_pdf_text(track.script)
        else:
            raw = voice.read_script_text(track.script)
        kept, dropped = voice.parse_script(raw, extra_directions)
        return (voice.lines_to_sentences(kept, extra_directions), dropped,
                f"台本 {track.script.name}")
    return ([], [], "无文本")


def slug_id(title):
    """作品 id：ASCII 字母数字保留，否则哈希——与 import_work.slug_id 同口径。"""
    import import_work
    return import_work.slug_id(title)


def import_voice(root=None, tracks=None, title=None, author="", work_id=None,
                 align=False, model="small", extra_directions=(),
                 include_dirs=None, dry_run=False, out_root=None, probe=True,
                 seg_provider=None):
    """导入音声作品。（root 目录 / tracks 显式给出，二选一）

    align=True 时对「有台本但没字幕」的轨道跑 ASR 强制对齐补时间轴；
    完全没有文本的轨道也能靠 ASR 直接出句子 + 时间轴。
    seg_provider 可注入：callable(track) -> [{text,t0,t1}] | None——服务器端
    ASR 结果（[{t0,t1,text}] 同构）由调用方喂进来，代替本地 voice_align
    转写；None 时走本地 faster-whisper（model 参数）。文本来源约定不变：
    有台本时文本以台本为准、时间用 ASR 段对齐（voice_align.align）。
    probe=False 跳过 ffprobe 探时长（报告里少一列，测试与无 ffmpeg 的机器用）。
    """
    root_path = Path(root) if root else None
    if tracks is None:
        tracks = discover(root_path, include_dirs)
    tracks = [t for t in tracks if t and t.audio]
    if not tracks:
        raise SystemExit("没找到任何音轨（支持的格式："
                         + "、".join(AUDIO_EXTS) + "）")

    title = title or (root_path.name if root_path else tracks[0].audio.stem)
    if not work_id:
        # RJ 号是 DLsite 的稳定编号，目录名就是它时直接拿来当作品 id
        m = re.fullmatch(r"(RJ\d+)", title.strip(), re.IGNORECASE)
        work_id = m.group(1).lower() if m else slug_id(title)

    chapters, dropped_report, stat = [], [], []
    for i, track in enumerate(tracks):
        sents, dropped, origin = _load_track_text(track, extra_directions)
        if not sents and align:
            sents, origin = _asr_sentences(track, model, seg_provider)
        if not sents:
            stat.append((track.label, origin, 0, "跳过", ""))
            continue
        if dropped:
            dropped_report.append(f"### {track.label}（{track.subtitle or track.script}）")
            dropped_report.extend(dropped)

        timed = any(s.get("t0") is not None for s in sents)
        if align and not timed:
            sents, origin = _align_sentences(track, sents, model, origin,
                                             seg_provider)
            timed = any(s.get("t0") is not None for s in sents)

        ch_title = track.label
        chapters.append({
            "title": ch_title,
            "sentences": sents,
            "audio": str(track.audio.resolve()),
            "timed": timed,
            "origin": origin,
        })
        dur = voice.probe_duration(track.audio) if probe else None
        stat.append((ch_title, origin, len(sents),
                     "有字幕" if timed else "无时间轴",
                     voice.format_timestamp(dur) if dur else ""))

    if not chapters:
        lines = "\n".join(f"    {a}  [{b}] {c} 句 {d}" for a, b, c, d, _e in stat)
        raise SystemExit("没有任何一条轨道能出句子：\n" + lines
                         + "\n（这些轨道没有字幕/台本：命令行加 --align、"
                           "界面导入勾选「用本地 ASR 补时间轴」走 ASR）")

    print(f"《{title}》→ 作品 id = {work_id}")
    for label, origin, n, note, dur in stat:
        print(f"  {label}  [{origin}]  {n} 句  {note}"
              + (f"  时长 {dur}" if dur else ""))
    if dry_run:
        print("（--dry-run：只看计划，未落盘）")
        return None

    return _write_work(work_id, title, author, chapters, dropped_report,
                       root_path, out_root)


def _asr_sentences(track, model, seg_provider=None):
    """整轨 ASR：whisper 输出自带时间轴，直接当句子流。

    seg_provider 注入时（服务器 ASR）用它返回的段代替本地转写。
    """
    import voice_align
    if seg_provider is not None:
        segs = seg_provider(track) or []
        if not segs:
            return [], "ASR 无结果"
    else:
        print(f"  [ASR] {track.audio.name}（模型 {model}）…")
        segs = voice_align.transcribe(track.audio, model=model)
    sents = [{"text": s["text"], "t0": s["t0"], "t1": s["t1"], "gap": 0.0}
             for s in segs]
    return sents, f"ASR({model})"


def _align_sentences(track, sents, model, origin, seg_provider=None):
    """台本 + ASR 强制对齐：拿到每句的起止时间（文本仍以台本为准）。

    seg_provider 注入时（服务器 ASR）段来自服务器，否则本地 voice_align。
    """
    import voice_align
    if seg_provider is not None:
        segs = seg_provider(track) or []
    else:
        print(f"  [对齐] {track.audio.name} ← {origin}（模型 {model}）…")
        segs = voice_align.transcribe(track.audio, model=model)
    if not segs:
        print("  [对齐] 没有 ASR 段可对齐，整章退回 TTS 朗读")
        return sents, origin
    aligned = voice_align.align(sents, segs)
    hit = sum(1 for s in aligned if s.get("t0") is not None)
    if not hit:
        print("  [对齐] 没有一句配上时间轴，整章退回 TTS 朗读")
        return sents, origin
    return aligned, f"{origin} + ASR 对齐"


def _write_work(work_id, title, author, chapters, dropped_report, root_path,
                out_root=None):
    """落盘：work.json + content/chNN.json（+ 清洗报告）。"""
    base = Path(out_root) if out_root else works.WORKS_DIR
    out_dir = base / work_id
    content_dir = out_dir / "content"
    content_dir.mkdir(parents=True, exist_ok=True)
    # 重新导入可能章数变少，先清掉旧章节文件（否则残留的 chNN 会被列表读到）
    for old in content_dir.glob("ch*.json"):
        old.unlink()

    meta = {
        "id": work_id,
        "title": title,
        "author": author,
        "type": "voice",
        "source": (str(root_path) if root_path else ""),
        "created": datetime.date.today().isoformat(),
        "chapters": [],
    }
    total = 0
    for i, ch in enumerate(chapters):
        data = voice.build_chapter(ch["title"], ch["sentences"])
        fname = f"ch{i:02d}.json"
        meta["chapters"].append({
            "id": i,
            "title": ch["title"],
            "file": fname,
            "sentences": sum(len(p) for p in data["paras"]),
            "audio": ch["audio"],
            "timed": bool(ch["timed"] and "times" in data),
        })
        works.write_json_atomic(content_dir / fname, data)
        total += meta["chapters"][-1]["sentences"]

    works.write_json_atomic(out_dir / "work.json", meta, indent=1)

    if dropped_report:
        # 清洗丢掉的行原文留档：规则漏了/砍多了，翻一遍就知道，不静默丢内容
        (out_dir / "dropped.txt").write_text("\n".join(dropped_report),
                                             encoding="utf-8")

    print(f"已导入《{title}》→ data/works/{work_id}/")
    print(f"  章节 {len(meta['chapters'])} · 句子 {total}"
          + (f" · 清洗丢弃 {len(dropped_report) - len(chapters)} 行"
             "（见 dropped.txt）" if dropped_report else ""))
    return meta


def main():
    ap = argparse.ArgumentParser(description="导入音声作品（音轨 + 字幕/台本）")
    ap.add_argument("path", nargs="?", help="作品目录（或单个音频文件）")
    ap.add_argument("--title")
    ap.add_argument("--author", default="")
    ap.add_argument("--id", dest="work_id")
    ap.add_argument("--track", action="append", default=[],
                    help="手动指定一轨：音频[=字幕][=台本]（可多次）")
    ap.add_argument("--dir", dest="include_dirs", action="append", default=[],
                    help="只扫相对路径含该片段的目录（可多次），"
                         "用来在「含SE / 无SE」多版音轨里挑一版")
    ap.add_argument("--align", action="store_true",
                    help="没有字幕时用 ASR 出/补时间轴（需 faster-whisper）")
    ap.add_argument("--model", default="small", help="ASR 模型名（默认 small）")
    ap.add_argument("--direction-word", action="append", default=[],
                    help="追加演出指示关键词（可多次）")
    ap.add_argument("--dry-run", action="store_true", help="只打印配对计划")
    ap.add_argument("--no-probe", action="store_true",
                    help="不调 ffprobe 探时长（没装 ffmpeg 时用）")
    ap.add_argument("--list", action="store_true", help="列出已导入作品")
    args = ap.parse_args()

    if args.list:
        for m in works.list_works():
            n = sum(c.get("sentences", 0) for c in m.get("chapters", []))
            tag = "音声" if m.get("type") == "voice" else "文本"
            print(f"{m['id']:30s} [{tag}] 《{m['title']}》 {n} 句")
        return

    tracks = None
    if args.track:
        tracks = []
        for spec in args.track:
            parts = [p.strip() for p in spec.split("=")]
            audio = Path(parts[0])
            if not audio.exists():
                ap.error(f"音轨不存在：{audio}")
            sub = next((Path(p) for p in parts[1:]
                        if Path(p).suffix.lower() in voice.SUBTITLE_EXTS), None)
            scr = next((Path(p) for p in parts[1:]
                        if Path(p).suffix.lower() in SCRIPT_EXTS), None)
            tracks.append(Track(audio=audio, subtitle=sub, script=scr,
                                label=norm_stem(audio.name)))
    elif not args.path:
        ap.error("需要作品目录（或 --track / --list）")

    import_voice(root=args.path, tracks=tracks, title=args.title,
                 author=args.author, work_id=args.work_id, align=args.align,
                 model=args.model, extra_directions=tuple(args.direction_word),
                 include_dirs=args.include_dirs or None, dry_run=args.dry_run,
                 probe=not args.no_probe)


if __name__ == "__main__":
    main()
