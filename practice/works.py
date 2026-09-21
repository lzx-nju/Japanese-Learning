#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""作品沉浸 —— 内容解析库

用轻小说/动画字幕/漫画学日语的公共解析层：导入器（import_work.py）负责把
txt/epub 切成「句子流」落盘，本模块负责请求时的实时分析——把每个句子的文本
对齐到 vocabulary.json 的词条上，产出阅读器需要的标注信息：

  - 已知词：字符级最长匹配（写法/读音归一到平假名后查表），复合词不受分词器
    切分影响，直接命中词条（含掌握度）；
  - 库外词：间隙用 janome 分词成可点击词块（surface / base_form / reading），
    base_form 兜底能接住大部分活用变形（行く ↔ 行きます）；
  - 难度画像：整部作品的独特词数、已学词覆盖率、高频生词 Top100。

匹配在请求时实时计算（不落盘），所以挖矿新加的词、掌握度的变化，阅读器
下一次刷新立即生效。CLI 见 import_work.py，路由见 app.py 的 /reader 系列。
"""
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path

WORKS_DIR = Path(__file__).resolve().parent.parent / "data" / "works"

# 匹配归一只用「片假名 → 平假名」这一步（与 app.to_hiragana 同口径，但这里
# 不能反向 import app——app.py 顶部 import 本模块，会成环）。
_KATA_TO_HIRA = {c: c - 0x60 for c in range(0x30A1, 0x30F7)}

# 句子终止符；连打（！！/？！/……）视作一个终止符，不断在中间
_TERM = "。！？…"
_OPEN_Q = "「『（("
_CLOSE_Q = "」』）)"
# 章节标题行（txt 导入用）：第X章/节/話/回，序号可汉字可阿拉伯，前面可有装饰符号
_CHAPTER_TITLE_RE = re.compile(
    r"^\s*[■◆▼【*]?\s*"
    r"(?:序章|終章|終わり|あとがき|コラム|prologue|epilogue"
    r"|[第卷巻][0-9０-９一二三四五六七八九十百千]+[章节節話话回幕部])"
    r"[\s：:」』）)]*[^\n]{0,60}$",
    re.IGNORECASE)


_KANJI_RE = re.compile(r"[一-鿿]")


def to_hiragana(s):
    """匹配口径的假名归一：NFKC + 片假名 → 平假名（长音符「ー」保留）。"""
    return unicodedata.normalize("NFKC", s or "").translate(_KATA_TO_HIRA)


def _has_kanji(s):
    return bool(_KANJI_RE.search(s or ""))


def split_sentences(paragraph):
    """一个自然段切成句子列表，保留句尾标点与引号。

    带引号深度扫描，而不是纯正则：对话体（轻小说的主体）里「おはよう。」的
    「。」在引号内，不能断——否则会切出「おはよう。 这种半截引号的句子。
    规则：
      - 终止符（。！？…）在引号内不断；紧跟闭引号（……。」）也不断，引号归前句；
      - 闭引号把深度降回 0 后，若紧跟下一个开引号（「…。」「…。」）则切开；
      - 引号后接正文（「…」と言った）不切。
    """
    paragraph = paragraph.strip()
    if not paragraph:
        return []
    parts = []
    start = 0
    depth = 0
    n = len(paragraph)
    i = 0
    while i < n:
        c = paragraph[i]
        nxt = paragraph[i + 1] if i + 1 < n else ""
        if c in _OPEN_Q:
            depth += 1
        elif c in _CLOSE_Q:
            if depth:
                depth -= 1
                # 一条对话结束、紧接下一条：切开（同一行挤两句对话很常见）
                if depth == 0 and nxt in _OPEN_Q:
                    parts.append(paragraph[start:i + 1].strip())
                    start = i + 1
        elif c in _TERM:
            if nxt in _TERM:
                pass  # 连打符号：只在最后一个后面断
            elif depth == 0 and nxt not in _CLOSE_Q:
                parts.append(paragraph[start:i + 1].strip())
                start = i + 1
        i += 1
    tail = paragraph[start:].strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


# ---------------------------------------------------------------------------
# 词库表单映射（已知词侧）
# ---------------------------------------------------------------------------

# 词条形式与命中方式的打分：是这个词的「写法」还是只是它读音？
#   2 写法：汉字词的汉字形（大学），或假名词本身的形（これ / パン，kanji 为 ---）
#   1 读音：汉字词按读音命中（と → 戸 的「と」只是 戸 的读音）
# 短假名串（≤2 字、不含汉字）只认写法命中——见 known_spans。
FORM_WRITING = 2
FORM_READING = 1


def build_form_map(vocab):
    """vocabulary.json → (form_map, max_form_len)。

    form_map: 归一形式 → [(ref, mastery, kanji, hiragana, score)]，按「写法优先、
    掌握度降序」排好——同形多词（同音异义）时取列表第一个。
    写法与读音都注册：さかな/魚、テレビ/てれび 都能命中。
    """
    form_map = {}
    for lid, ldata in vocab.get("lessons", {}).items():
        # 句型课（多邻国「を食べます」这类整句模板，id 以 _phrases 结尾，与
        # app.py 的 skip_phrase_lessons 同口径）不进阅读器词表：它们是带
        # 助词的整块模板，按字符匹配会把「を食べます」当成一个词标出来，
        # 反而盖住了真正该标的「食べる」。
        if lid.endswith("_phrases"):
            continue
        for w in ldata.get("words", []):
            wid = w.get("id")
            if not wid:
                continue
            try:
                m = int(w.get("mastery") or 0)
            except (TypeError, ValueError):
                m = 0
            kanji = w.get("kanji", "") or ""
            hiragana = w.get("hiragana", "") or ""
            # 外来语等「没有汉字写法」的词：假名就是它的写法，不是读音
            kana_word = kanji in ("", "---")
            forms = []
            if not kana_word:
                forms.append((to_hiragana(kanji), FORM_WRITING))
            if hiragana:
                forms.append((to_hiragana(hiragana),
                              FORM_WRITING if kana_word else FORM_READING))
            for form, score in forms:
                if not form:
                    continue
                entry = (f"{lid}:{wid}", m, kanji, hiragana, score)
                form_map.setdefault(form, []).append(entry)
    # 同形多词：写法命中优先于读音命中，其次掌握度高的优先
    for entries in form_map.values():
        entries.sort(key=lambda e: (-e[4], -e[1]))
    max_len = max((len(f) for f in form_map), default=0)
    return form_map, max_len


def lookup(form_map, surface):
    """按表面形查词库，返回排在最前的词条（没有/不该命中返回 None）。

    命中策略只有一条补充规则：短假名串（≤2 字且不含汉字）只认「写法」命中。
    と→戸、に→二、なか→中、さん→三 都是同音汉字词被读音撞上的误命中，
    「田中さん」里冒出「中」「三」比整词不标更糟。3 字以上（たまご→卵、
    ぐらい）仍按读音命中——词写作假名是常态。
    """
    entries = form_map.get(to_hiragana(surface))
    if not entries:
        return None
    if len(surface) <= 2 and not _has_kanji(surface):
        entries = [e for e in entries if e[4] >= FORM_WRITING]
    return entries[0] if entries else None


def known_spans(text, form_map, max_len):
    """字符级最长匹配，返回 [(start, end, entry), ...]，互不重叠、按序。

    逐字往后试，每个位置从最长可能形式往下试到能命中为止（复合词直接整块
    命中，不受分词器切分影响）。
    """
    spans = []
    i = 0
    n = len(text)
    while i < n:
        hit = None
        for l in range(min(max_len, n - i), 0, -1):
            entry = lookup(form_map, text[i:i + l])
            if entry:
                hit = (i, i + l, entry)
                break
        if hit:
            spans.append(hit)
            i = hit[1]
        else:
            i += 1
    return spans


# ---------------------------------------------------------------------------
# janome 分词（库外词块）
# ---------------------------------------------------------------------------

_janome_tokenizer = None


def _tokenizer():
    global _janome_tokenizer
    if _janome_tokenizer is None:
        from janome.tokenizer import Tokenizer
        _janome_tokenizer = Tokenizer(wakati=False)
    return _janome_tokenizer


def _merge_keep(tokens, keep, keep_max, skip):
    """把 janome 切碎的相邻词块按用户词典合回去。

    keep 里的写法（如「だから」）若被 janome 拆成连续几块（だ+から），这里
    贪心最长匹配地并回一块；skip 是本句的例外写法（用户在复查里勾掉、
    确认「这样切其实是对的」），命中就跳过不合并。
    合并块的 surface/reading 由子块拼接，base 取 surface（能接住整词恰好在词库里的情况）。
    """
    if not keep or len(tokens) < 2:
        return tokens
    out = []
    i = 0
    n = len(tokens)
    while i < n:
        merged = 0
        for l in range(min(keep_max, n - i), 1, -1):  # 至少两块才有「合并」可言
            combined = "".join(t[0] for t in tokens[i:i + l])
            if combined in keep and combined not in skip:
                merged = l
                break
        if merged:
            grp = tokens[i:i + merged]
            surface = "".join(g[0] for g in grp)
            reading = "".join(g[1] for g in grp)
            out.append((surface, reading, surface, "keep"))
            i += merged
        else:
            out.append(tokens[i])
            i += 1
    return out


def _merged_reading(surface):
    """合并块的读音：整块正好是 janome 的一个词，就用它的读音（瀬戸→せと）。

    不能简单把子块读音拼起来——「瀬」单独的读音是 せら，拼上「戸」的 と 会成 せらと。
    """
    try:
        toks = _gap_tokens(surface)
    except Exception:
        return ""
    return toks[0][1] if len(toks) == 1 else ""


def _merge_keep_segs(segs, keep, keep_max, skip=(), form_map=None):
    """在最终词块序列上再按用户词典并一次——能跨过词库命中块。

    _merge_keep 只在 janome 的库外间隙内部合并，可「戸」这类单字本身就在词库里，
    会被 known_spans 切成一个独立命中块，把「瀬戸」这种名字夹在中间永远合不回去：
    用户明明选了「瀬戸」，复查却扫不到任何位置。这里对最终词块贪心最长匹配地再并一次，
    跨过命中块，让手动修正真正生效；合并后若在词库里就照常挂掌握度。
    """
    if not keep or len(segs) < 2:
        return segs
    out = []
    i = 0
    n = len(segs)
    while i < n:
        merged = 0
        for l in range(min(keep_max, n - i), 1, -1):
            combined = "".join(s["t"] for s in segs[i:i + l])
            if combined in keep and combined not in skip:
                merged = l
                break
        if not merged:
            out.append(segs[i])
            i += 1
            continue
        grp = segs[i:i + merged]
        surface = "".join(g["t"] for g in grp)
        seg = {"t": surface}
        # 整块是 janome 的一个词就取它的读音，否则退回子块读音拼接
        reading = _merged_reading(surface) or "".join(g.get("r", "") for g in grp)
        if reading:
            seg["r"] = reading
        entry = lookup(form_map, surface) if form_map is not None else None
        if entry:
            seg["w"] = entry[0]
            seg["m"] = entry[1]
            seg["base"] = surface
        out.append(seg)
        i += merged
    return out


def _gap_tokens(text, keep=None, keep_max=0, skip=()):
    """库外间隙切词块：[(surface, reading平假名, base_form, pos)]。

    janome 缺词（人名等）时会按字符碎裂——上层不做处理，碎块仍是可点击词块。
    给了 keep（用户分词词典）就在 janome 之后做一步合并，修掉「だから→だ+から」
    这类高频切分错误；skip 指定的写法在本句不合并（用户确认为正确切分）。
    """
    out = []
    try:
        tokens = list(_tokenizer().tokenize(text))
    except Exception:
        # janome 未安装 / 个别怪字符崩溃：整段退化为一个词块，阅读器仍可用
        return [(text, "", "", "")] if text else []
    kept = []
    for t in tokens:
        surface = t.surface
        if not surface.strip():
            continue
        reading = to_hiragana(t.reading or "") if t.reading and t.reading != "*" else ""
        base = t.base_form if t.base_form and t.base_form != "*" else surface
        kept.append((surface, reading, base, t.part_of_speech or ""))
    return _merge_keep(kept, keep, keep_max, skip)


def analyze_sentence(text, form_map, max_len, ud=None):
    """单句 → 阅读器标注结构。

    返回 {"segs": [...], "known_refs": [...], "known_chars": int, "base_forms": {ref: [词形]}}
    segs 元素：{t, r, w, m, base}——t 表层文字，r 读音（假名，可空），
    w 词条 ref（已知词），m 掌握度（已知词），base 活用兜底命中的原形。
    ud 是 load_user_tokens() 的产物（用户分词修正）；缺省不修正，行为同旧版。
    """
    keep = ud["keep"] if ud else None
    keep_max = ud["keep_max"] if ud else 0
    skip = ud["exceptions"].get(text, ()) if ud and ud.get("exceptions") else ()
    spans = known_spans(text, form_map, max_len)
    segs = []
    base_hits = {}

    def push(surface, reading, ref=None, mastery=None, base=None):
        seg = {"t": surface}
        if reading:
            seg["r"] = reading
        if ref:
            seg["w"] = ref
            seg["m"] = mastery
            if base:
                seg["base"] = base
                base_hits.setdefault(ref, []).append(base)
        segs.append(seg)

    pos = 0
    for start, end, sp_entry in spans:
        # 命中前的库外间隙 → janome 词块；base_form 再查一次表（活用兜底）
        for surface, reading, base, _pos in _gap_tokens(text[pos:start], keep, keep_max, skip):
            gap_entry = lookup(form_map, base)
            if gap_entry:
                push(surface, reading, gap_entry[0], gap_entry[1], base)
            else:
                push(surface, reading)
        ref, mastery, _kanji, hiragana, _score = sp_entry
        push(text[start:end], hiragana, ref, mastery)
        pos = end
    for surface, reading, base, _pos in _gap_tokens(text[pos:], keep, keep_max, skip):
        gap_entry = lookup(form_map, base)
        if gap_entry:
            push(surface, reading, gap_entry[0], gap_entry[1], base)
        else:
            push(surface, reading)

    # 词库命中块会把 janome 的整词（人名等）切开，间隙内的合并够不着，
    # 再在最终词块上并一次，用户词典才跨得过命中块
    if keep:
        segs = _merge_keep_segs(segs, keep, keep_max, skip, form_map)

    return {
        "segs": segs,
        "known_refs": [s["w"] for s in segs if s.get("w")],
        "known_chars": sum(len(s["t"]) for s in segs if s.get("w")),
        "base_hits": base_hits,
    }


# ---------------------------------------------------------------------------
# 单句分析缓存（难度画像全书扫描 / 章节标注共用）
# ---------------------------------------------------------------------------
# 为什么要缓存：难度画像要扫全书（30 章 × 150 句 × janome 分词 ≈ 6 秒），而词库
# 一变（练一次词 vocabulary.json 就写盘、_profile_cache 立刻失效）下次打开阅读器
# 又得整本重扫——用户体感是「每次打开都要等很久」。
#
# 关键观察：ann 里只有 segs[].m（掌握度）随词库变，命中的词形与分词块都不变。
# 所以缓存「不含 mastery 的分析结果」，读取时按当前词库刷新 m：练完词的重算
# 退化成「查表 + 累加」（毫秒级）；只有挖矿加词（词形集合真的变了）才重扫。
#
# 两级：进程内 _ann_cache（key = 词形指纹 + 用户词典 stamp + 句子文本）；
# 落盘 data/annot_cache/<work_id>.json（整部作品快照，服务重启后第一次打开也快）。
# 指纹对不上就整体忽略、重扫后覆盖写——不拿旧缓存硬凑。

_ann_cache = {}
_ann_cache_max = 80000          # 句级条目上限（约十几部长篇），超了整体丢弃重来
_disk_loaded = set()            # 已尝试读盘的 (work_id, fp, ud_stamp)
_disk_ok = set()                # 读盘成功、且已灌进内存的 (work_id, fp, ud_stamp)


def form_fingerprint(form_map):
    """词形集合指纹（只认「词形 + 命中优先级」，掌握度不参与）。

    lookup 的排序在同分时按掌握度降序——同形多词场景下，掌握度会轻微影响
    命中哪个词条。缓存按词形集合冻结这一选择：同形歧义本就没有唯一答案，
    练词后换个同形词条显示不是可感知的损失，换来的是练词后不必重扫全书。
    """
    parts = []
    for form in sorted(form_map):
        parts.append(form + ":" + ",".join(str(e[4]) for e in form_map[form]))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def mastery_map(vocab):
    """{ref: 掌握度} —— 缓存读取时刷新 seg.m 用（ref 形如 lesson_x:w001）。"""
    out = {}
    for lid, ldata in (vocab.get("lessons") or {}).items():
        for w in ldata.get("words", []):
            wid = w.get("id")
            if not wid:
                continue
            try:
                out[f"{lid}:{wid}"] = int(w.get("mastery") or 0)
            except (TypeError, ValueError):
                out[f"{lid}:{wid}"] = 0
    return out


def annot_cache_path(work_id):
    """落盘缓存路径（跟 WORKS_DIR 走，测试重定向 WORKS_DIR 即自动隔离）。"""
    return WORKS_DIR.parent / "annot_cache" / f"{work_id}.json"


def _load_disk_annot(work_id, fp, ud_stamp):
    """读整部作品快照；版本/指纹不一致或坏档返回 None。"""
    data = _read_json(annot_cache_path(work_id))
    if not data or data.get("v") != 1:
        return None
    if data.get("fp") != fp or data.get("ud") != ud_stamp:
        return None
    sents = data.get("sents")
    return sents if isinstance(sents, dict) else None


def _save_disk_annot(work_id, fp, ud_stamp, sents):
    """原子写整部作品快照；写不了静默放弃（缓存只是加速，缺了也得能用）。"""
    path = annot_cache_path(work_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as fp_:
            json.dump({"v": 1, "fp": fp, "ud": ud_stamp, "sents": sents},
                      fp_, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def ensure_disk_annot(work_id, fp, ud_stamp):
    """把落盘快照灌进进程内缓存（每部作品每指纹只尝试一次）。"""
    marker = (work_id, fp, ud_stamp)
    if marker in _disk_loaded:
        return
    _disk_loaded.add(marker)
    sents = _load_disk_annot(work_id, fp, ud_stamp)
    if not sents:
        return
    for text, ann in sents.items():
        _ann_cache.setdefault((fp, ud_stamp, text), ann)
    _disk_ok.add(marker)


def analyze_sentence_cached(text, form_map, max_len, ud, fp, mastery):
    """analyze_sentence 的缓存版：命中时只按当前词库刷新掌握度。

    mastery 是 mastery_map(vocab) 的产物；segs[].m 每次都刷成当前值——
    高亮颜色与画像统计都读 m，不刷的话练完词回来看还是旧颜色。
    （多线程下刷新是就地改共享对象：最坏某次读到上一版 m，下次刷新即正。）
    """
    global _ann_cache
    ud_stamp = (ud or {}).get("stamp", 0)
    key = (fp, ud_stamp, text)
    ann = _ann_cache.get(key)
    if ann is None:
        ann = analyze_sentence(text, form_map, max_len, ud)
        if len(_ann_cache) >= _ann_cache_max:
            _ann_cache = {}   # 粗暴但够用：超限整体丢弃，下次重扫
        _ann_cache[key] = ann
    for seg in ann["segs"]:
        ref = seg.get("w")
        if ref:
            seg["m"] = mastery.get(ref, 0)
    return ann


# ---------------------------------------------------------------------------
# 作品文件读写
# ---------------------------------------------------------------------------

def work_dir(work_id):
    return WORKS_DIR / work_id


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError):
        return None


def write_text_atomic(path, text):
    """整份内容先写临时文件再整体替换：读者永远看不到半截文件。

    直接 `open(path, "w")` 覆写已存在的 work.json 时，导入进程被中断、
    或阅读器恰好在此刻打开它，都会让 `_read_json` 解析失败返回 None——
    整部作品显示「作品不存在」，而 content/ 里的章节其实全是好的。

    临时文件名带 pid+**线程标识**：同一进程里两个线程写同一个目标不能共用一个
    tmp（导入页一次恢复多个任务就是并发线程），否则两个写者会交替往同一个
    tmp 里灌，把半截内容原子替换进正式路径。
    `flush + fsync` 是让字节真的落到盘上再改名：NTFS 只保证 rename 这条元数据
    操作是 journalled，断电时没 fsync 的数据块可能还是空的，改名后会留下 0 字节
    坏档。目录 fsync 在 Windows 上做不到（打不开目录），rename 的持久化交给文件系统。
    Windows 下目标正被读时 replace 会报 PermissionError，短暂重试即可。
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(text)
        fp.flush()
        os.fsync(fp.fileno())
    for attempt in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05)


def write_json_atomic(path, obj, indent=None):
    """JSON 版的原子写（序列化后交给 write_text_atomic，唯一实现见那里）。"""
    write_text_atomic(path, json.dumps(obj, ensure_ascii=False, indent=indent))


def load_work(work_id):
    """work.json 元信息；不存在返回 None。"""
    return _read_json(work_dir(work_id) / "work.json")


def list_works():
    """已导入作品列表（按导入时间降序）。坏目录跳过，不让一个坏文件拖垮列表。"""
    out = []
    if not WORKS_DIR.exists():
        return out
    for d in sorted(WORKS_DIR.iterdir(), key=lambda p: p.name):
        if not d.is_dir():
            continue
        meta = _read_json(d / "work.json")
        if meta and meta.get("id"):
            out.append(meta)
    out.sort(key=lambda m: m.get("created", ""), reverse=True)
    return out


def load_chapter_raw(work_id, n):
    """content/chNN.json → {"title", "paras": [[句子, ...], ...]}。"""
    return _read_json(work_dir(work_id) / "content" / f"ch{n:02d}.json")


def chapter_sentences(chapter):
    """章节 → 有序句子列表（画像统计用，段落边界抹平）。"""
    return [s for para in chapter.get("paras", []) for s in para]


# ---------------------------------------------------------------------------
# 用户分词修正词典（全局，跨作品生效）
# ---------------------------------------------------------------------------
# 自动分词（janome）对高频功能词偶尔切错，比如 だから → だ + から。用户
# 在阅读器里把相邻碎块合起来后，修正以「写法」形式存进这里：整词一旦入典，
# 所有已导入 / 将来导入的作品都默认按合并后的词块显示（全局生效）。
# 极少数句子那种切分本来就是对的（例：「本だから」＝だ 系词 + から 助词），
# 用户在复查弹窗里勾掉——该句记为 split_exception，本句不合并、他处仍合并。
#
# 落盘结构 data/works/user_tokens.json：
#   {"keep": ["だから", ...],
#    "split_exceptions": [{"surface": "だから", "sentence": "……本だから……"}, ...]}

_ud_cache = None  # (mtime_ns, {keep, keep_max, exceptions})


def user_tokens_path():
    """用户分词词典路径（跟 WORKS_DIR 走，测试重定向 WORKS_DIR 即自动隔离）。"""
    return WORKS_DIR / "user_tokens.json"


def load_user_tokens_raw():
    """读词典原始结构；不存在 / 坏档返回空骨架（不抛异常）。"""
    data = _read_json(user_tokens_path()) or {}
    keep = [k for k in (data.get("keep") or []) if isinstance(k, str) and k.strip()]
    excs = []
    for e in (data.get("split_exceptions") or []):
        if isinstance(e, dict) and e.get("surface") and e.get("sentence"):
            excs.append({"surface": e["surface"], "sentence": e["sentence"]})
    return {"keep": keep, "split_exceptions": excs}


def load_user_tokens():
    """词典的派生形态（分析时用的集合 / 按句索引），按文件 mtime 缓存。

    返回 {"keep": set, "keep_max": int, "exceptions": {句子: set(不合并写法)}}。
    """
    global _ud_cache
    try:
        mtime = user_tokens_path().stat().st_mtime_ns
    except OSError:
        mtime = 0
    if _ud_cache is not None and _ud_cache[0] == mtime:
        return _ud_cache[1]
    raw = load_user_tokens_raw()
    keep = {k for k in raw["keep"] if len(k) >= 2}  # 单字无「合并」意义
    exceptions = {}
    for e in raw["split_exceptions"]:
        if e["surface"] in keep:
            exceptions.setdefault(e["sentence"], set()).add(e["surface"])
    derived = {"keep": keep,
               "keep_max": max((len(k) for k in keep), default=0),
               "exceptions": exceptions,
               "stamp": mtime}  # 分析缓存的失效依据（见 analyze_sentence_cached）
    _ud_cache = (mtime, derived)
    return derived


def find_occurrences(work, vocab, surface):
    """扫整部作品，找出 surface（含本次拟新增的修正）会被合回去的句子。

    用与真实分析同一套合并逻辑（临时把 surface 并进 keep），保证复查列表
    与实际生效位置一致。返回 [{"sentence", "count"}]（按句聚合）。
    """
    form_map, max_len = build_form_map(vocab)
    base = load_user_tokens()
    keep = set(base["keep"]) | {surface}
    ud = {"keep": keep,
          "keep_max": max(base["keep_max"], len(surface)),
          "exceptions": {}}  # 复查时不预设例外，把所有候选位置都摆给用户
    occ = {}
    for ch in range(len(work.get("chapters", []))):
        raw = load_chapter_raw(work.get("id", ""), ch)
        if not raw:
            continue
        for s in chapter_sentences(raw):
            if surface not in s:
                continue
            ann = analyze_sentence(s, form_map, max_len, ud)
            c = sum(1 for seg in ann["segs"] if seg.get("t") == surface)
            if c:
                occ[s] = occ.get(s, 0) + c
    return [{"sentence": s, "count": c} for s, c in occ.items()]


# ---------------------------------------------------------------------------
# 出处溯源：这个词在你的作品里出现过几次、都在哪
# ---------------------------------------------------------------------------

_sent_cache = {}


def all_sentences(work):
    """整部作品的 [(章节序号, 句子), ...]，按作品 mtime 缓存。

    出处统计要对每个生词扫一遍全部作品，现读磁盘太慢；句表缓存在内存里，
    作品或词库一变动 key 就失效（与难度画像同一套失效口径）。
    """
    wid = work.get("id", "")
    key = _cache_key(wid)
    hit = _sent_cache.get(wid)
    if hit and hit[0] == key:
        return hit[1]
    out = []
    for n in range(len(work.get("chapters", []))):
        raw = load_chapter_raw(wid, n)
        if not raw:
            continue
        out.extend((n, s) for s in chapter_sentences(raw))
    _sent_cache[wid] = (key, out)
    return out


def count_occurrences(surface, sample_limit=3):
    """surface 在所有作品里的出现统计（挖矿决策依据：出现 1 次 vs 20 次）。

    按**字面**匹配而非词块分析：统计「这个词出现几次」够用，而且不受分词
    与词库变动影响、跨作品扫描也快。返回：
      {"total": 总次数, "works": [{"work_id","title","count","chapters","samples"}]}
    works 按出现次数降序；samples 每部作品最多给 sample_limit 句（前端预览用）。
    """
    if not surface:
        return {"total": 0, "works": []}
    total, out = 0, []
    for work in list_works():   # list_works 返回的就是 work.json 元信息（含 chapters）
        wid = work.get("id", "")
        if not wid:
            continue
        chapters = work.get("chapters", [])
        per_ch, samples, cnt = {}, [], 0
        for n, s in all_sentences(work):
            c = s.count(surface)
            if not c:
                continue
            cnt += c
            per_ch[n] = per_ch.get(n, 0) + c
            if len(samples) < sample_limit:
                samples.append({"chapter": n, "sentence": s})
        if not cnt:
            continue
        out.append({
            "work_id": wid,
            "title": work.get("title", wid),
            "count": cnt,
            "chapters": [
                {"n": n, "title": (chapters[n].get("title", "") if n < len(chapters) else ""),
                 "count": c}
                for n, c in sorted(per_ch.items())
            ],
            "samples": samples,
        })
        total += cnt
    out.sort(key=lambda x: (-x["count"], x["title"]))
    return {"total": total, "works": out}


# ---------------------------------------------------------------------------
# 难度画像（请求时实时算 + mtime 内存缓存）
# ---------------------------------------------------------------------------

_profile_cache = {}


def _cache_key(work_id):
    """work.json / content 目录任一文件变动即失效，词库变动同理。"""
    d = work_dir(work_id)
    stamps = [d / "work.json"]
    cdir = d / "content"
    if cdir.exists():
        stamps += sorted(cdir.glob("*.json"))
    parts = []
    for p in stamps:
        try:
            parts.append(f"{p.name}:{p.stat().st_mtime_ns}")
        except OSError:
            continue
    vocab = WORKS_DIR.parent.parent / "vocabulary.json"
    try:
        parts.append(f"vocab:{vocab.stat().st_mtime_ns}")
    except OSError:
        pass
    try:
        parts.append(f"tokens:{user_tokens_path().stat().st_mtime_ns}")
    except OSError:
        parts.append("tokens:0")
    return "|".join(parts)


def profile(work, vocab, top=100):
    """整部作品的难度画像。

    coverage 按「已学词字符数 / 日文字符总数」计（mastery>=1 算已学），
    0 掌握的在库词单列（library_chars）：算你「认识」之前先算你「见过」。

    逐句分析走 analyze_sentence_cached（见上方的单句分析缓存）：练词后
    vocabulary.json 一变本函数就要重算，但句子缓存命中时只刷新掌握度，
    毫秒级出结果；重算后把整部作品的分析快照落到 data/annot_cache/。
    """
    work_id = work.get("id", "")
    key = f"{work_id}:{_cache_key(work_id)}"
    if key in _profile_cache:
        return _profile_cache[key]

    form_map, max_len = build_form_map(vocab)
    ud = load_user_tokens()
    fp = form_fingerprint(form_map)
    ud_stamp = ud.get("stamp", 0)
    ensure_disk_annot(work_id, fp, ud_stamp)
    mastery = mastery_map(vocab)
    before = len(_ann_cache)
    collected = {}
    total_chars = known_chars = lib_chars = 0
    total_sents = known_sents = 0
    uniq_known = set()
    unknown_freq = {}

    for ch in range(len(work.get("chapters", []))):
        raw = load_chapter_raw(work_id, ch)
        if not raw:
            continue
        for sent in chapter_sentences(raw):
            ann = analyze_sentence_cached(sent, form_map, max_len, ud, fp, mastery)
            collected[sent] = ann
            jp_chars = sum(1 for c in sent if _is_jp(c))
            total_chars += jp_chars
            total_sents += 1
            has_known = False
            for seg in ann["segs"]:
                if "w" not in seg:
                    continue
                if seg["m"] >= 1:
                    has_known = True
                    known_chars += len(seg["t"])
                    uniq_known.add(seg["w"])
                else:
                    lib_chars += len(seg["t"])
            if has_known:
                known_sents += 1
            # 库外词频：以「整块 surface」计，太碎的（<=1 字且非内容词）不排进来
            for seg in ann["segs"]:
                if "w" in seg:
                    continue
                surface = seg["t"]
                if len(surface) <= 1:
                    continue
                unknown_freq[surface] = unknown_freq.get(surface, 0) + 1

    top_unknown = sorted(unknown_freq.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    result = {
        "sentences": total_sents,
        "chars": total_chars,
        "coverage": round(known_chars / total_chars, 4) if total_chars else 0,
        "library_seen": round(lib_chars / total_chars, 4) if total_chars else 0,
        "known_sentence_rate": round(known_sents / total_sents, 4) if total_sents else 0,
        "unique_known": len(uniq_known),
        "unique_unknown": len(unknown_freq),
        "top_unknown": [{"surface": s, "count": c} for s, c in top_unknown],
    }
    _profile_cache[key] = result
    # 有新算的句子、或磁盘还没有这份快照时才落盘（全命中且快照在，省一次写）
    marker = (work_id, fp, ud_stamp)
    if len(_ann_cache) > before or marker not in _disk_ok:
        _save_disk_annot(work_id, fp, ud_stamp, collected)
    return result


def _is_jp(c):
    """日文字符（假名 + 汉字 + 日文标点），覆盖率分母只数这些。"""
    o = ord(c)
    return (0x3040 <= o <= 0x30FF or 0x4E00 <= o <= 0x9FFF
            or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFF9F)
