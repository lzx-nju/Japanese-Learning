#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""音声作品 —— 字幕 / 台本解析库

给「一条音轨 + 一份字幕或台本」的音声作品（DLsite RJ 那类）做文本侧的准备：
把字幕/台本解析成**带时间轴的句子流**，落盘的格式与轻小说作品完全一致
（见 import_voice.py），所以阅读器那套点词查义 / 挖矿 / 已学词标注可以直接复用，
只是句子多了一对 `t0/t1`（这句子在音频里的起止秒数）。

两条文本来源，质量与工序不同：

  1. **字幕**（`.vtt` / `.srt` / `.ass`）——自带时间轴，解析出来就能用
     （`cues_to_sentences`）。字幕是按「一屏放得下」切的、不是按句子切的，
     所以要先把 cue 首尾接起来、用轻小说同一套 `works.split_sentences` 切句，
     再按字符位置把时间找回来。一条 cue 里挤多个人的台词（`【ミア】…【アヤ】…`）
     时先按说话人标签切开，再按台词长度把这条 cue 的时长分给各段
     （见 `split_speakers`）——不切的话整屏糊成一句、时间也只能整条共用。
  2. **台本**（`.txt` / `.pdf`）——出品方原稿，文本更准，但**没有时间轴**，
     只有句子。要么当纯文本读（`lines_to_sentences`），要么交给
     `voice_align.py` 用 ASR 做强制对齐把时间反推出来。

字幕与台本都要洗一遍：字幕里混着 `<v 说话人>` 这类内联标签与排版代码，
台本里混着 `（囁き）`、`SE：ドア` 这类演出指示（ト書き）——都不是台词，
留着会把句子切碎、也会污染挖矿的词形。

CLI（先看清楚再导入，导入见 import_voice.py）：

    python.exe practice\\voice.py probe  <文件...>   只看解析统计，不落盘
    python.exe practice\\voice.py dump   <文件>      按解析后的句子逐条打印
"""
import html
import re
import sys
import unicodedata
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import works  # noqa: E402  切句规则与轻小说同一套，别另写一份


# ---------------------------------------------------------------------------
# 时间轴 cue
# ---------------------------------------------------------------------------

Cue = namedtuple("Cue", "start end text")


# 时间戳：HH:MM:SS.mmm / HH:MM:SS,mmm / MM:SS.mmm / H:MM:SS.cc（ass 是两位小数）
_TS = r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})"


def parse_timestamp(s):
    """'00:03:21.480' / '03:21.48' / '0:00:01.00' → 秒（float）。解析不了返回 None。"""
    m = re.fullmatch(_TS, (s or "").strip())
    if not m:
        return None
    h, mi, sec, frac = m.group(1), m.group(2), m.group(3), m.group(4)
    # 小数位补齐到毫秒：'.5' 是 500ms，'.48' 是 480ms（ass 用两位）
    ms = int((frac + "000")[:3])
    return int(h or 0) * 3600 + int(mi) * 60 + int(sec) + ms / 1000.0


def format_timestamp(sec):
    """秒 → 'MM:SS'（阅读器进度条用，超过一小时给 'H:MM:SS'）。"""
    sec = max(0, int(sec or 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_ffprobe_duration(text):
    """ffprobe -show_entries format=duration 的输出 → 秒。取不到返回 None。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("duration="):
            try:
                return float(line.split("=", 1)[1])
            except (ValueError, IndexError):
                return None
    return None


def probe_duration(path, runner=None):
    """音频时长（秒）。ffprobe 不在 / 解析失败一律返回 None——时长只是展示用，
    拿不到不该让导入失败（阅读器也能从 audio 元素自己读出总长）。"""
    import subprocess
    run = runner or subprocess.run
    try:
        res = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=nw=1:nk=1", str(path)],
                  capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    if getattr(res, "returncode", 1) != 0:
        return None
    return parse_ffprobe_duration(getattr(res, "stdout", ""))


# ---------------------------------------------------------------------------
# 文本清洗
# ---------------------------------------------------------------------------

# 字幕内联标签与排版代码：<v Speaker> <c.yellow> <i> <br> <00:00:01.000>，
# ass 的 {\pos(...)} / {\an8}。标签名可能带属性（<v.loud 名前>），一律整块去掉。
_TAG_RE = re.compile(r"<[^>\n]{0,120}>")
_ASS_CODE_RE = re.compile(r"\{[^{}\n]{0,120}\}")
# ass 的换行/硬空格转义
_ASS_BREAK_RE = re.compile(r"\\[Nnh]")

DIRECTION_LINE_KEYWORDS = (
    "SE", "ＳＥ", "効果音", "効果", "BGM", "ＢＧＭ", "音楽", "ジングル",
    "フェード", "ノイズ", "足音", "ドア", "扉", "水音", "衣擦れ", "衣ずれ",
    "ナレーション", "モノローグ", "ト書き", "モノラル", "ステレオ",
)
# 整行就是演出指示（不是台词）的行首形状。**只收明显不是台词的行**：
# 分隔线、`SE：…` 这类指示、纯编号行。`■トラック1` 这种记号开头的标题行
# 反而不丢——它可能是章节边界，丢掉比留一行噪声更亏（留下的噪声一眼能看见，
# 丢掉的边界丢了就找不回来了）。行内指示由 strip_directions 处理，
# 洗完只剩标点/记号的行（`♪♪`、`………`）另有 isalnum 兜底。
_DROP_LINE_RE = re.compile(
    r"^\s*(?:"
    r"[-—―─=＝~～+*＊#>／/]{2,}[^\n]*"                      # 分隔线 / 装饰线
    r"|(?:" + "|".join(DIRECTION_LINE_KEYWORDS) + r")\s*[:：]\s*[^\n]*"
    r"|\d{1,3}(?:\s*[.．、)）])?\s*$"                        # 纯编号行（cue 序号之类）
    r")\s*$"
)

# 行内演出指示的括号内容关键词：只有「短 + 命中关键词」才当指示剥掉。
# 单独用「整行被括号包住」判定太狠——`（心の声）台词` 是常见写法，括号里是
# 说话方式、括号外才是台词；而整行 `（……）` 在有些台本里恰恰是独白内容。
DIRECTION_INLINE_KEYWORDS = (
    "囁", "ささやき", "ささやく", "小声", "囁き声",
    "間", "ポーズ", "沈黙", "無音", "静か",
    "ため息", "溜息", "吐息", "息遣い", "鼻息", "呼吸",
    "笑", "くすくす", "ふふ", "泣", "嗚咽", "涙",
    "キス", "ちゅ", "リップ音", "リップ",
    "SE", "ＳＥ", "効果音", "効果", "BGM", "ＢＧＭ", "音楽", "ジングル",
    "フェード", "ノイズ", "足音", "ドア", "扉", "水音", "衣擦れ", "衣ずれ",
    "心の声", "独白", "モノローグ", "ナレーション", "ト書き",
    "場所", "時間", "状況", "注", "※", "モノラル",
)
_INLINE_DIR_MAX = 16   # 括号内容超过这么多字就不像演出指示了（别误删台词）
_INLINE_DIR_RE = re.compile(r"[(（]([^()（）\n]{1,%d})[)）]" % _INLINE_DIR_MAX)


def _is_inline_direction(content, extra=()):
    kw = tuple(extra) + DIRECTION_INLINE_KEYWORDS
    return any(k and k in content for k in kw)


def strip_markup(text):
    """去掉字幕的内联标签 / 排版代码，并还原 HTML 实体（&amp; → &）。"""
    if not text:
        return ""
    s = _ASS_CODE_RE.sub("", text)
    s = _TAG_RE.sub("", s)
    s = _ASS_BREAK_RE.sub(" ", s)
    return html.unescape(s)


def strip_directions(text, extra=()):
    """剥掉行内演出指示：`（囁き）こんばんは` → `こんばんは`。

    只删「括号内容短、且命中关键词」的，宁可漏删也不误删台词。
    """
    if not text:
        return ""
    return _INLINE_DIR_RE.sub(
        lambda m: "" if _is_inline_direction(m.group(1), extra) else m.group(0),
        text)


def clean_text(text, extra_directions=()):
    """字幕/台本文本的标准清洗：去内联标签 → 去演出指示 → 压空白。"""
    s = strip_directions(strip_markup(text), extra_directions)
    s = s.replace("\u3000", " ").strip()
    return re.sub(r"\s{2,}", " ", s)


def drop_script_lines(text, extra_directions=()):
    """台本整文 → (保留的行, 丢弃的行)。

    丢弃的是整行演出指示（`SE：ドアの音`、`─────`、纯编号行）。返回丢弃的
    原文交给调用方落盘成报告——**不静默丢内容**，用户翻一遍就知道规则够不够。
    """
    kept, dropped = [], []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("\u3000").strip()
        if not line:
            continue
        if _DROP_LINE_RE.match(line):
            dropped.append(line)
            continue
        cleaned = clean_text(line, extra_directions)
        # 洗完只剩标点/记号的（`♪♪`、`………`、`（囁き）` 剥空）也不是台词
        if not cleaned or not any(c.isalnum() for c in cleaned):
            dropped.append(line)
            continue
        kept.append(cleaned)
    return kept, dropped


# ---------------------------------------------------------------------------
# 字幕解析：WebVTT / SRT / ASS
# ---------------------------------------------------------------------------

_SRT_TS_RE = re.compile(r"(\S+)\s*-->\s*(\S+)")
_VTT_HEADER_RE = re.compile(r"^\s*WEBVTT", re.IGNORECASE)


def _cues_from_blocks(blocks):
    """统一处理「时间戳行 + 其后若干文本行」的块结构（VTT 与 SRT 都是这个形状）。

    块内可能出现**第二个**时间戳（字幕文件缺空行是常见坏味道），按时间戳行再切
    一次；否则两条 cue 会并成一条、还把时间戳读进正文。纯数字行是 SRT 的序号，
    不吃进正文。

    cue 文本在这里就过一遍 `strip_markup`：内联标签是**格式层面**的东西
    （`<v 说话人>`、卡拉OK时间戳、`{\\an8}`），解析器该负责清掉；演出指示
    是**内容层面**的，留给 clean_text（它需要用户追加的关键词）。
    """
    cues = []
    for lines in blocks:
        ts_at = [i for i, line in enumerate(lines) if "-->" in line]
        for pos, idx in enumerate(ts_at):
            m = _SRT_TS_RE.search(lines[idx])
            if not m:
                continue
            start = parse_timestamp(m.group(1))
            end = parse_timestamp(m.group(2))
            if start is None or end is None:
                continue
            stop = ts_at[pos + 1] if pos + 1 < len(ts_at) else len(lines)
            body = [ln for ln in lines[idx + 1:stop] if not ln.strip().isdigit()]
            text = strip_markup("".join(body)).strip()
            if text:
                cues.append(Cue(start, end, text))
    return cues


def _split_blocks(text):
    """按空行切块。VTT/SRT 的 cue 之间必有空行；行尾 \\r 一并吃掉。"""
    blocks, cur = [], []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip():
            cur.append(line.rstrip())
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def parse_webvtt(text):
    """WebVTT → [Cue]。跳过 WEBVTT 头、NOTE 注释块与 STYLE 样式块。"""
    blocks = _split_blocks(text)
    keep = []
    for b in blocks:
        head = b[0].strip()
        if _VTT_HEADER_RE.match(head) or head.upper().startswith("NOTE") \
                or head.upper().startswith("STYLE") or head.upper().startswith("REGION"):
            continue
        keep.append(b)
    return _cues_from_blocks(keep)


def parse_srt(text):
    """SRT → [Cue]。格式与 VTT 只差时间戳用逗号，块结构一致。"""
    return _cues_from_blocks(_split_blocks(text))


_ASS_TS_RE = re.compile(r"^\s*(\d+:\d{1,2}:\d{1,2}[.,]\d{1,3})\s*,\s*"
                        r"(\d+:\d{1,2}:\d{1,2}[.,]\d{1,3})\s*,")


def parse_ass(text):
    """ASS/SSA → [Cue]。只取 [Events] 段的 Dialogue 行。

    列序由该段自己的 Format: 行决定（Dialogue 的 Text 在最后一列），
    所以先解析 Format 找到 Start / End / Text 的下标，不写死列号。
    """
    cues = []
    in_events = False
    cols = None
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_events = line.lower() == "[events]"
            cols = None
            continue
        if not in_events:
            continue
        if line.lower().startswith("format:"):
            cols = [c.strip().lower() for c in line.split(":", 1)[1].split(",")]
            continue
        if not line.lower().startswith("dialogue:"):
            continue
        body = line.split(":", 1)[1]
        if cols:
            # Text 列里本身含逗号，所以只按前 len(cols)-1 个逗号切
            parts = body.split(",", len(cols) - 1)
            if len(parts) < len(cols):
                continue
            row = dict(zip(cols, parts))
            start = parse_timestamp(row.get("start", ""))
            end = parse_timestamp(row.get("end", ""))
            text = row.get("text", "")
        else:
            m = _ASS_TS_RE.match(body)
            if not m:
                continue
            start = parse_timestamp(m.group(1))
            end = parse_timestamp(m.group(2))
            text = body[m.end():]
        if start is None or end is None:
            continue
        text = strip_markup(text).strip()
        if text:
            cues.append(Cue(start, end, text))
    return cues


# 扩展名 → 解析器；嗅探失败时按内容特征再猜一次
_PARSERS = {".vtt": parse_webvtt, ".webvtt": parse_webvtt,
            ".srt": parse_srt, ".ass": parse_ass, ".ssa": parse_ass}
SUBTITLE_EXTS = tuple(_PARSERS)


def parse_subtitles(text, name=""):
    """字幕文本 → [Cue]。按扩展名选解析器；给的扩展名不认识就按内容嗅探。

    三种格式的时间戳写法互不冲突（ass 有 `Dialogue:` 前缀，srt/vtt 有 `-->`），
    所以嗅探很可靠；解析不出 cue 时返回空列表，由调用方决定报错还是降级。
    """
    ext = Path(name).suffix.lower()
    parser = _PARSERS.get(ext)
    if parser:
        return parser(text)
    if _VTT_HEADER_RE.match((text or "").lstrip()):
        return parse_webvtt(text)
    if "[events]" in (text or "").lower() and "dialogue:" in (text or "").lower():
        return parse_ass(text)
    return parse_srt(text)


# ---------------------------------------------------------------------------
# 台本解析：txt / pdf
# ---------------------------------------------------------------------------

def read_script_text(path):
    """台本 txt 编码探测：日文台本常见 utf-8 / cp932，都读不出来再替换式兜底。"""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp932", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_pdf_text(path, max_pages=400):
    """台本 pdf → 文本。pymupdf 没装就报错交给调用方提示（别静默出空文本）。

    逐页 join 后按行保留：台本 pdf 多是「一行一句台词」，段落边界有用。
    """
    try:
        import pymupdf
    except ImportError as e:  # pragma: no cover - 依赖缺失的分支
        raise RuntimeError("读取 pdf 台本需要 pymupdf：pip install pymupdf") from e
    doc = pymupdf.open(str(path))
    try:
        pages = []
        for i in range(min(doc.page_count, max_pages)):
            pages.append(doc[i].get_text())
        return "\n".join(pages)
    finally:
        doc.close()


def parse_script(text, extra_directions=()):
    """台本文本 → (保留的行, 丢弃的行)。txt / pdf 抽出的文本走同一条清洗。"""
    return drop_script_lines(text, extra_directions)


# ---------------------------------------------------------------------------
# 说话人标签：`【ミア】セリフ` —— 一条 cue / 一行里塞多个人的台词
# ---------------------------------------------------------------------------
# 音声作品的字幕与台本常把好几个人的台词挤在一条 cue / 一行里：
#     【Mia】你好【Aya】我是Aya
# 不切开的话整行糊成一句（`works.split_sentences` 只认句末标点），时间也只能
# 整条 cue 共用。这里把方括号短名字当句子边界，并按台词长度把这条 cue 的时长
# 分给各段（谁说得长谁多分）——切分点无从得知，按长度分是能做到的最好估计。
#
# 两条保守规则（宁可漏切，别把台词切碎）：
#   - 引号内不算：`「これは【重要】だ」` 是台词里的方括号词，不是说话人；
#   - 演出记号不算：`[SE]`、`[BGM]`、`［笑］`（见 _SPEAKER_STOPWORDS）。
_SPEAKER_MAX = 12          # 标签内容上限：再长就不像名字了（`【とても大事】`）
_SPEAKER_RE = re.compile(
    r"[【［〔\[]([^】］〕\]\n\s。、！？…，,．!?]{1,%d})[】］〕\]]" % _SPEAKER_MAX)
_SPEAKER_STOPWORDS = {"SE", "SFX", "BGM", "音楽", "効果音", "効果", "無音",
                      "沈黙", "笑", "泣", "ト書き"}


def _is_speaker_label(content):
    """方括号内容是不是说话人（演出记号如 SE / BGM 不是）。"""
    return unicodedata.normalize("NFKC", content).strip().upper() \
        not in _SPEAKER_STOPWORDS


def split_speakers(text):
    """文本 → [(标签, 台词)]，标签含方括号原样（`【ミア】`），无标签的段为 None。

    `【Mia】你好【Aya】我是Aya` → [("【Mia】", "你好"), ("【Aya】", "我是Aya")]。
    没有台词的标签（后一个标签紧跟第一个）不单独成段；整段只有标签时返回
    [(标签, "")]——上层据此闭合上一句，而不是把标签当正文吃掉。
    """
    chunks, label, start, depth, i = [], None, 0, 0, 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in works._OPEN_Q:          # 引号深度与 works.split_sentences 同一套字符
            depth += 1
        elif c in works._CLOSE_Q:
            if depth:
                depth -= 1
        elif c in "【［〔[" and depth == 0:
            m = _SPEAKER_RE.match(text, i)
            if m and _is_speaker_label(m.group(1)):
                chunks.append((label, text[start:i].strip()))
                label, start = m.group(0), m.end()
                i = m.end()
                continue
        i += 1
    chunks.append((label, text[start:].strip()))
    out = [(lb, body) for lb, body in chunks if body]
    if out:
        return out
    # 整段只有标签（换人但没说台词）：保留标签、body 留空，交给上层当换人边界用。
    # 直接回落成 [(None, 原文)] 会把「【ミア】」当正文，粘到上一句尾巴上。
    labels = [lb for lb, _b in chunks if lb]
    return [(labels[0], "")] if labels else [(None, text)]


def _allocate(t0, t1, weights):
    """[t0,t1] 按 weights 比例切段；没有时间轴（t0/t1 为 None）全给 None。"""
    if t0 is None or t1 is None:
        return [(None, None)] * len(weights)
    total = sum(weights)
    if total <= 0:                      # 全是空台词：均分，别除零
        weights, total = [1] * len(weights), len(weights)
    out, cur = [], float(t0)
    dur = max(0.0, float(t1) - float(t0))
    for i, w in enumerate(weights):
        end = float(t1) if i == len(weights) - 1 else cur + dur * w / total
        out.append((cur, end))
        cur = end
    return out


# ---------------------------------------------------------------------------
# cue / 行 → 句子（并映射时间轴）
# ---------------------------------------------------------------------------

def _time_range(spans, a, b):
    """字符区间 [a,b) 落在哪些单元上 → (t0, t1)。跨单元的句子取并集。"""
    t0 = t1 = None
    for s, e, cs, ce in spans:
        if e <= a or s >= b or cs is None:
            continue
        if t0 is None:
            t0 = cs
        t1 = ce
    return t0, t1


def _group_sentences(group):
    """一组连续单元 → 句子流（组内允许一句话跨单元合并）。

    group 元素：{"text", "t0", "t1", "gap"}（见 segments_to_sentences）。
    """
    combined = "".join(u["text"] for u in group)
    spans, gaps, pos = [], [], 0
    for u in group:
        spans.append((pos, pos + len(u["text"]), u["t0"], u["t1"]))
        gaps.append((pos, u["gap"]))
        pos += len(u["text"])
    out, ranges, cursor = [], [], 0
    for sent in works.split_sentences(combined):
        a = combined.find(sent, cursor)
        if a < 0:                       # 理论上不会发生，兜底按游标走
            a = cursor
        b = a + len(sent)
        # 句子起点之前的最后一个 gap（该句开头那次停顿）
        gap = 0.0
        for gpos, gval in gaps:
            if gpos <= a:
                gap = gval
            else:
                break
        t0, t1 = _time_range(spans, a, b)
        ranges.append((a, b))
        out.append({"text": sent, "t0": t0, "t1": t1, "gap": gap})
        cursor = b
    # 整句落在同一个单元内部的（一条 cue 里挤了两三句、同一条说话人台词里两句）：
    # 把该单元的时间按长度比例再分给它们——否则点哪一句都播整条 cue。
    for us, ue, ut0, ut1 in spans:
        if ut0 is None:
            continue
        inner = [i for i, (a, b) in enumerate(ranges) if us <= a and b <= ue]
        if len(inner) < 2:
            continue
        for i, (s0, s1) in zip(inner, _allocate(
                ut0, ut1, [len(out[i]["text"]) for i in inner])):
            out[i]["t0"], out[i]["t1"] = s0, s1
    return out


def segments_to_sentences(segments, extra_directions=()):
    """[(文本, t0, t1, 强制断句)] → [{"text", "t0", "t1", "gap"}]。

    切句边界有两类：
      - 说话人标签（`【ミア】`）——同一条 cue 内被它切开的各段台词，按长度比例
        分这条 cue 的时长；台词的顺序就是时间顺序。
      - 强制断句（台本的一行）：行与行不合并（老行为）。
    没有边界的相邻段仍按老规矩首尾相接：一句话横跨两条 cue 时合并、时间取并集。
    """
    units = []
    prev_end = None
    for text, t0, t1, hard in segments:
        text = clean_text(text, extra_directions)
        if not text:
            continue
        gap = max(0.0, t0 - prev_end) if t0 is not None and prev_end is not None \
            else 0.0
        parts = split_speakers(text)
        times = _allocate(t0, t1, [len(body) for _lb, body in parts])
        if all(not body for _lb, body in parts):
            # 整条 cue 只有说话人标签：没台词可显示，但换人必须生效——
            # 否则下一句会粘到上一句尾巴上，上一句还被拉长到下一次真正换人。
            units.append({"text": "", "t0": t0, "t1": t1,
                          "hard": bool(parts[0][0]), "gap": gap})
        first = True
        for (label, body), (u0, u1) in zip(parts, times):
            if not body:
                continue
            units.append({
                "text": (label or "") + body,
                "t0": u0,
                "t1": u1,
                "hard": bool(label) or (hard and first),
                "gap": gap if first else 0.0,
            })
            first = False
        if t1 is not None:
            prev_end = t1 if prev_end is None else max(prev_end, t1)

    out, group = [], []
    for u in units:
        if u["hard"] and group:         # 说话人变了 / 新的一行：前面的收尾
            out.extend(_group_sentences(group))
            group = []
        if not u["text"]:               # 纯换人标记：闭合用，本身不成句
            continue
        group.append(u)
    out.extend(_group_sentences(group))
    return out


def cues_to_sentences(cues, extra_directions=()):
    """[Cue] → [{"text", "t0", "t1", "gap"}]。

    字幕是按「一屏放得下」切的：一句话常横跨两三 cue，一条 cue 里也常塞两句、
    甚至两个人的台词（`【Mia】…【Aya】…`，见 split_speakers）。做法是先把 cue
    内部按说话人切开并分好时间，再首尾相接用 `works.split_sentences` 切句，
    最后按句子在整段里的字符位置把时间找回来。

    gap 是这句之前的时间空档（秒），供分段用——换场景/长停顿处另起一段。
    """
    return segments_to_sentences(
        [(c.text, c.start, c.end, False) for c in cues], extra_directions)


def lines_to_sentences(lines, extra_directions=()):
    """台本行 → 句子。行是「一句台词一行」，一行里也可能挤两句 / 两个人的台词。

    每一行都是天然边界（不与下一行合并）；没有时间轴：t0/t1 全部为 None，
    gap 用行边界的固定值（0）——下游按「有没有 t0」判断该播音频还是退回 TTS。
    行内的说话人标签（`【ミア】…【アヤ】…`）照切不误。
    """
    return segments_to_sentences([(line, None, None, True) for line in lines],
                                 extra_directions)


def assign_times(sentences, timed):
    """把 `timed`（同序的 [(t0,t1)]）贴到无时间轴的句子上（ASR 对齐的出口）。

    长度对不上就不贴——宁可整章退回 TTS 朗读，也不要错位的时间轴
    （错位比没有更糟：点一句播的是另一句）。
    """
    if len(sentences) != len(timed):
        return sentences
    out = []
    for s, (t0, t1) in zip(sentences, timed):
        item = dict(s)
        item["t0"], item["t1"] = t0, t1
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 分组与章节
# ---------------------------------------------------------------------------

PARA_GAP = 2.0        # 相邻 cue 的空档超过这个秒数 → 另起一段（自然停顿）
PARA_MAX_SENTS = 4    # 一段最多几句：长独白没有停顿，也得切开才读得下去
PARA_MAX_CHARS = 160


def group_paragraphs(sentences):
    """句子 → 段落（[[…], […]]）。段落只影响观感，不参与判分。

    断段依据：句子前的停顿 gap 超过 PARA_GAP、或该段已经够长（句数/字数上限）。
    没有时间轴的台本 gap 恒为 0，于是纯按长度切——一样能看。
    """
    paras, cur, chars = [], [], 0
    for s in sentences:
        if cur and (s.get("gap") or 0) > PARA_GAP:
            paras.append(cur)
            cur, chars = [], 0
        cur.append(s)
        chars += len(s["text"])
        if len(cur) >= PARA_MAX_SENTS or chars >= PARA_MAX_CHARS:
            paras.append(cur)
            cur, chars = [], 0
    if cur:
        paras.append(cur)
    return paras


def build_chapter(title, sentences):
    """句子 → 落盘的章节结构。

    `paras` 与轻小说作品完全同形（[[句子字符串, …], …]），所以 works.py /
    annotate_chapter 的既有链路一行不用改；时间轴另走一张**扁平**的 `times`
    表，下标与 `works.chapter_sentences()` 的展开顺序一一对应。
    """
    paras = group_paragraphs(sentences)
    flat = [s for p in paras for s in p]
    timed = [s for s in flat if s.get("t0") is not None]
    data = {
        "title": title,
        "paras": [[s["text"] for s in p] for p in paras],
    }
    if timed:
        # 强制对齐可能让个别句子没配上时间（t0=None）；读侧只要 t0 非 null
        # 才可点句播放（reader.js），所以这里统一落成 [t0,t1] 或 [null,null]
        # 保持 times 表与句子一一对应，长度校验不致错位。
        data["times"] = [[round(s["t0"], 3), round(s["t1"], 3)]
                         if s.get("t0") is not None and s.get("t1") is not None
                         else [None, None] for s in flat]
    return data


def flat_times(times, paras):
    """落盘的 times → 与 `works.chapter_sentences()` 对齐的扁平表（读侧用）。

    存的是「一句一对」，可扁平表必须与段落展开后的顺序一致——两者本来就
    同序，这里只做长度校验：对不上就当没有时间轴，免得错位播放。
    """
    total = sum(len(p) for p in (paras or []))
    if not times or len(times) != total:
        return None
    return times


# ---------------------------------------------------------------------------
# CLI：先看清楚解析结果，再决定要不要导入
# ---------------------------------------------------------------------------

def _load_any(path, extra=()):
    """按扩展名解析出 (cues, lines)。字幕给 cues，台本给 lines，另一个为空。"""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in SUBTITLE_EXTS:
        return parse_subtitles(p.read_text(encoding="utf-8", errors="replace"), p.name), []
    if ext == ".pdf":
        kept, dropped = parse_script(read_pdf_text(p), extra)
        return [], (kept, dropped)
    text = read_script_text(p)
    kept, dropped = parse_script(text, extra)
    return [], (kept, dropped)


def probe(paths, extra=()):
    """打印每个文件的解析统计（cue 数 / 句数 / 时长 / 丢弃行数），不落盘。"""
    for path in paths:
        p = Path(path)
        print(f"=== {p.name}")
        if not p.exists():
            print("    (文件不存在)")
            continue
        try:
            cues, script = _load_any(p, extra)
        except Exception as e:
            print(f"    (解析失败：{e})")
            continue
        if cues:
            sents = cues_to_sentences(cues, extra)
            span = cues[-1].end - cues[0].start
            print(f"    cue {len(cues)} 条 · 句 {len(sents)} 个 · "
                  f"覆盖 {format_timestamp(span)}")
            for s in sents[:5]:
                print(f"    [{format_timestamp(s['t0'])}] {s['text']}")
            if len(sents) > 5:
                print(f"    … 另 {len(sents) - 5} 句")
        else:
            kept, dropped = script
            sents = lines_to_sentences(kept, extra)
            print(f"    台本：保留 {len(kept)} 行 / 丢弃 {len(dropped)} 行 · 句 {len(sents)} 个")
            for s in sents[:5]:
                print(f"    {s['text']}")
            if len(sents) > 5:
                print(f"    … 另 {len(sents) - 5} 句")
            for d in dropped[:5]:
                print(f"    × 丢弃：{d}")
            if len(dropped) > 5:
                print(f"    … 另丢弃 {len(dropped) - 5} 行")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="音声字幕 / 台本解析预览")
    ap.add_argument("command", choices=["probe", "dump"])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--direction-word", action="append", default=[],
                    help="追加演出指示关键词（可多次）")
    args = ap.parse_args()
    extra = tuple(args.direction_word)
    if args.command == "probe":
        probe(args.paths, extra)
        return
    for path in args.paths:
        p = Path(path)
        cues, script = _load_any(p, extra)
        sents = cues_to_sentences(cues, extra) if cues else \
            lines_to_sentences(script[0], extra)
        print(f"=== {p.name}（{len(sents)} 句）")
        for s in sents:
            ts = format_timestamp(s["t0"]) if s.get("t0") is not None else "--:--"
            print(f"[{ts}] {s['text']}")


if __name__ == "__main__":
    main()
