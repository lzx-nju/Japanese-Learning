#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""图片日语 —— 日常图片讲解课的课程库（请求时实时标注）。

课件数据：`data/picture_lessons/<id>.json`，由 `import_picture_lesson.py`
从人写的 markdown 稿子导入（md 仍是学习稿本体，JSON 只是网页端的结构化视图）。

本模块负责在请求时把课程内容接上现有的学习体系：
  - **课程单词 ↔ 词库**：按（写法 / 读音）在 `vocabulary.json` 里找同一个词
    （复用 works 的表单映射，片假名/平假名归一），找到就给 ref 与掌握度——
    网页上因此能显示「已在词库 · 会了 / 在学」还是「生词」，并能一键加生词本；
  - **图片原文的已学词标注**：与作品阅读器同一套字符级最长匹配（works），
    照片里的文字切成词块，已学词可点亮、可点开查义、可朗读；
  - **图片文件定位**：路径写在 JSON 里（相对仓库根），只认课程里登记过的那些，
    发图路由据此白名单放行（不接受任意路径参数）。

不做落盘缓存的理由与阅读器一致：挖矿新加的词、练习涨的掌握度，刷新页面立即生效。
"""
import json
import re
from pathlib import Path

import works

BASE_DIR = Path(__file__).resolve().parent.parent
LESSONS_DIR = BASE_DIR / "data" / "picture_lessons"
# 单词配图缓存（与 app.vocab_image_url 同一目录与命名口径：{lesson_id}_{word_id}.png）
VOCAB_IMAGE_DIR = Path(__file__).resolve().parent / "static" / "images" / "vocab"

# 数据文件里图片路径可能写成 data/... （仓库根相对）；发图时 resolve 后
# 必须仍在仓库内，防越界
_ALLOWED_IMAGE_SUFFIX = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def lesson_path(lesson_id):
    return LESSONS_DIR / f"{lesson_id}.json"


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError):
        return None


def _is_lesson_file(p):
    """课程数据文件；参考答案 sidecar（<id>.answers.json）不是课程本体。"""
    return p.suffix == ".json" and not p.name.endswith(".answers.json")


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def list_lessons(vocab=None):
    """已导入的课程元信息列表（按 id 升序）。坏文件跳过，不拖垮列表。

    给了 vocab 就顺带算 practice_count（本课有多少词已在词库里、能直接练）——
    表单映射只建一次，给课程下拉的角标用。
    """
    out = []
    if not LESSONS_DIR.exists():
        return out
    form_map = works.build_form_map(vocab)[0] if vocab is not None else None
    for p in sorted(LESSONS_DIR.iterdir(), key=lambda x: x.name):
        if not p.is_file() or not _is_lesson_file(p):
            continue
        data = _read_json(p)
        if not data or not data.get("id"):
            continue
        photos = data.get("photos", [])
        meta = {
            "id": data["id"],
            "title": data.get("title", data["id"]),
            "intro": data.get("intro", ""),
            "updated": data.get("updated", ""),
            "photo_count": len(photos),
            "word_count": sum(len(ph.get("words", [])) for ph in photos),
        }
        if form_map is not None:
            meta["practice_count"] = len(_practice_refs(data, form_map))
        out.append(meta)
    return out


def load_lesson(lesson_id):
    data = _read_json(lesson_path(lesson_id))
    if not data or not data.get("id"):
        return None
    return data


def photo_by_n(lesson, n):
    n = _as_int(n)
    for ph in lesson.get("photos", []):
        if _as_int(ph.get("n")) == n:
            return ph
    return None


def image_path(lesson, n):
    """第 n 张照片的图片文件（Path）；未登记 / 不存在 / 越界返回 None。"""
    ph = photo_by_n(lesson, n)
    if not ph:
        return None
    rel = (ph.get("image") or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/"):
        return None
    path = (BASE_DIR / rel).resolve()
    try:
        path.relative_to(BASE_DIR.resolve())
    except ValueError:
        return None   # 越界路径（数据文件手改坏了）
    if path.suffix.lower() not in _ALLOWED_IMAGE_SUFFIX or not path.is_file():
        return None
    return path


# ---------------------------------------------------------------------------
# 课程单词 ↔ 词库
# ---------------------------------------------------------------------------

def lookup_entry(form_map, form, kana=""):
    """课程里的一个词（写法 + 假名）→ 词库词条 ref/掌握度，找不到返回 None。

    先按写法查（汉字词的汉字形），再按假名查（纯假名词的「写法」就是假名）；
    两次都走 works.lookup——短假名串只认写法命中，不会把「に」误配成「二」。
    """
    for surface in (form, kana):
        if not surface:
            continue
        entry = works.lookup(form_map, surface)
        if entry:
            return entry
    return None


def _practice_refs(lesson, form_map):
    refs, seen = [], set()
    for ph in lesson.get("photos", []):
        for w in ph.get("words", []):
            entry = lookup_entry(form_map, w.get("form", ""), w.get("kana", ""))
            if entry and entry[0] not in seen:
                seen.add(entry[0])
                refs.append(entry[0])
    return refs


def practice_refs(lesson, vocab=None, form_map=None):
    """本课里「已经在词库、可以直接练」的词条 ref（按出现顺序去重）。"""
    if form_map is None:
        form_map, _ = works.build_form_map(vocab or {})
    return _practice_refs(lesson, form_map)


def link_word(form_map, w):
    """课程单词 → 下发结构：带上词库联动（ref / 掌握度 / 是否在库）。"""
    item = {
        "form": w.get("form", ""),
        "kana": w.get("kana", ""),
        "meaning": w.get("meaning", ""),
    }
    if w.get("note"):
        item["note"] = w["note"]
    entry = lookup_entry(form_map, w.get("form", ""), w.get("kana", ""))
    if entry:
        item["ref"] = entry[0]
        item["mastery"] = entry[1]
        item["in_bank"] = True
    return item


# ---------------------------------------------------------------------------
# 请求时标注（照片原文 + 讲解里的例句）
# ---------------------------------------------------------------------------

# md 行内强调 **x**：切词块前先剥掉，否则「**」会混进词块文本、
# 前端还得按未转义的标记对位切分。强调本身不丢信息——被强调的多半是
# 目标词/短语，它要么在单词表里单列，要么已被已知词高亮。
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")


def _plain(text):
    return _MD_BOLD_RE.sub(r"\1", text or "")


def _kana_ratio(text):
    if not text:
        return 0.0
    return len(_KANA_RE.findall(text)) / len(text)


def lesson_single_forms(lesson):
    """本课单词里的单字写法/假名：单字命中只有这些才点亮（见 annotate_line）。"""
    out = set()
    for ph in lesson.get("photos", []):
        for w in ph.get("words", []):
            for f in (w.get("form", ""), w.get("kana", "")):
                if len(f or "") == 1:
                    out.add(f)
    return out


def _drop_single_hits(segs, allow):
    """丢掉不受本课认可的单字已知词命中，只留文本。

    词库里有大量单字词（三 / 西 / 山 / 町 / 本…），字符级匹配会把
    「三重県津市西区平山町」「募集中」「日本」拆出一堆单字「已学词」——
    在作品长文里这点噪声可以忽略，但在**一屏一张图**的讲解页里，
    整行都在发绿光，反而看不出真正该学的词。本课单词表里有的单字
    （如 姉、肌）照常点亮。
    """
    out = []
    for s in segs:
        if s.get("w") and len(s.get("t", "")) == 1 and s["t"] not in allow:
            s = {k: v for k, v in s.items() if k in ("t", "r")}
        out.append(s)
    return out


def annotate_line(text, form_map, max_len, ud, mixed=False, singles=()):
    """一行文本 → {"text", "segs"}；segs 与阅读器同构（w/m 表示已知词）。

    mixed=True 用于语法/句式的讲解行：这些行常是「中日混排的说明」
    （「**お肌** ＝ 肌肤（比「肌」更文雅）」），整行交给 janome 会把中文汉字
    按日语音读切开、甚至凭读音误命中词库（「中」→なか），画面上就是一串
    没意义的绿字。假名占比太低的行直接不切词块，原样显示。
    singles 是本课单词里的单字写法（见 _drop_single_hits）。
    """
    text = _plain(text)
    if mixed and _kana_ratio(text) < 0.2:
        return {"text": text}
    ann = works.analyze_sentence(text, form_map, max_len, ud)
    return {"text": text, "segs": _drop_single_hits(ann["segs"], singles)}


def _annotate_blocks(blocks, form_map, max_len, ud, singles=()):
    out = []
    for b in blocks or []:
        t = b.get("t")
        if t == "ul":
            out.append({"t": "ul", "items": [
                annotate_line(it, form_map, max_len, ud, mixed=True, singles=singles)
                for it in b.get("items", [])]})
        else:
            item = {"t": t or "p"}
            line = annotate_line(b.get("text", ""), form_map, max_len, ud,
                                 mixed=True, singles=singles)
            item["text"] = line["text"]
            if line.get("segs"):
                item["segs"] = line["segs"]
            out.append(item)
    return out


def _collect_refs(obj, out):
    """递归收集载荷里所有已知词 ref（照片原文与讲解行里的 w 字段）。"""
    if isinstance(obj, dict):
        ref = obj.get("w")
        if isinstance(ref, str) and ref:
            out.add(ref)
        for v in obj.values():
            _collect_refs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_refs(v, out)


def _vocab_index(vocab):
    """ref → (lesson_id, word)：给原文里被点亮的词配「词卡」用。

    句型课（lesson_*_phrases）不进：它们是整块模板，不参与标注（与 works 同口径）。
    """
    idx = {}
    for lid, ldata in vocab.get("lessons", {}).items():
        if lid.endswith("_phrases"):
            continue
        for w in ldata.get("words", []):
            if w.get("id"):
                idx[f"{lid}:{w['id']}"] = (lid, w)
    return idx


def word_card(idx, ref):
    """词库词条 → 前端词卡（点原文里的已学词时弹的那张）。"""
    hit = idx.get(ref)
    if not hit:
        return None
    lid, w = hit
    kanji = w.get("kanji") or ""
    kana = w.get("hiragana") or ""
    pure_kana = kanji in ("", "---")
    card = {
        "ref": ref,
        "form": kana if pure_kana else kanji,
        "kana": "" if pure_kana else kana,
        "meaning": w.get("meaning", ""),
        "mastery": int(w.get("mastery") or 0),
    }
    if w.get("tip"):
        card["tip"] = w["tip"]
    if w.get("example_ja"):
        card["example_ja"] = w["example_ja"]
    if w.get("example_zh"):
        card["example_zh"] = w["example_zh"]
    img = VOCAB_IMAGE_DIR / f"{lid}_{w.get('id', '')}.png"
    if img.exists():
        card["image"] = f"/static/images/vocab/{lid}_{w.get('id', '')}.png"
    return card


def build_payload(lesson, vocab):
    """课程 JSON → 网页载荷：单词挂词库、原文与例句切好词块、统计与练习入口。"""
    form_map, max_len = works.build_form_map(vocab)
    ud = works.load_user_tokens()

    singles = lesson_single_forms(lesson)
    photos = []
    word_count = in_bank = learned = 0
    for ph in lesson.get("photos", []):
        words = [link_word(form_map, w) for w in ph.get("words", [])]
        word_count += len(words)
        in_bank += sum(1 for w in words if w.get("in_bank"))
        learned += sum(1 for w in words if w.get("in_bank") and w.get("mastery", 0) >= 1)
        photos.append({
            "n": ph.get("n"),
            "title": ph.get("title", ""),
            "caption": ph.get("caption", ""),
            "image_url": f"/api/pictures/{lesson['id']}/image/{ph.get('n')}",
            "texts": [annotate_line(t, form_map, max_len, ud, singles=singles)
                      for t in ph.get("texts", [])],
            "words": words,
            "points": [{
                "kind": pt.get("kind", "note"),
                "title": pt.get("title", ""),
                "blocks": _annotate_blocks(pt.get("blocks", []), form_map, max_len, ud,
                                           singles=singles),
            } for pt in ph.get("points", [])],
        })

    # まとめ的单词名单：能对上课文单词就补上假名/释义，对不上只留名字
    by_form = {}
    for ph in lesson.get("photos", []):
        for w in ph.get("words", []):
            by_form.setdefault(w.get("form", ""), w)
    summary_words = []
    for name in lesson.get("summary", {}).get("words", []):
        if name in by_form:
            summary_words.append(link_word(form_map, by_form[name]))
        else:
            summary_words.append(link_word(form_map, {"form": name, "kana": "", "meaning": ""}))

    summary_points = [annotate_line(t, form_map, max_len, ud, mixed=True, singles=singles)
                      for t in lesson.get("summary", {}).get("points", [])]

    # 原文/讲解里被点亮的词 → 词卡（点一下就能看释义、听发音、加重点词）。
    # 课程单词表与小结里已挂上词库的词也一并带上：前端手里的 ref 一律有卡可开。
    refs = set()
    _collect_refs({"photos": photos, "summary": summary_points}, refs)
    refs.update(w["ref"] for ph in photos for w in ph["words"] if w.get("ref"))
    refs.update(w["ref"] for w in summary_words if w.get("ref"))
    idx = _vocab_index(vocab)
    vocab_words = {}
    for ref in refs:
        card = word_card(idx, ref)
        if card:
            vocab_words[ref] = card

    return {
        "id": lesson["id"],
        "title": lesson.get("title", lesson["id"]),
        "intro": lesson.get("intro", ""),
        "updated": lesson.get("updated", ""),
        "source": lesson.get("source", ""),
        "photos": photos,
        "summary": {"points": summary_points, "words": summary_words},
        "exercises": lesson.get("exercises", []),
        "vocab_words": vocab_words,
        "stats": {
            "photo_count": len(photos),
            "word_count": word_count,
            "in_bank": in_bank,
            "learned": learned,
            "practice_refs": _practice_refs(lesson, form_map),
        },
    }


def mining_notes(lesson, n):
    """挖矿词的备注：来源记到「哪一课 · 哪张照片」，方便回看是在哪儿学的。"""
    n = _as_int(n)
    ph = photo_by_n(lesson, n)
    notes = f"来源：{lesson.get('title', lesson.get('id', ''))} 写真{n}"
    if ph and ph.get("title"):
        notes += f"（{ph['title']}）"
    return notes
