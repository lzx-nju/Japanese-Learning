#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""动词活用变形（ます形/て形/た形/ない形）派生模块

从 vocabulary.json 派生动词索引，按 janome 标注的活用型（infl_type）分派规则表，
生成四张变形后落盘 data/conjugation.json（含人工 overrides 段）与核对清单
data/conjugation_review.md。供后续「动词活用卡（讲解）」与 conjugation 题型消费。

设计要点（为什么这么做）：
- 规则按 janome 实际输出的 infl_type 字符串分派，不自己另写例外表——
  「五段・カ行イ音便」与「五段・カ行促音便」正是「開く→開いて」与
  「行く→行って」的分野，janome 已经替我们标好了；
- janome 的 base_form 与词条写法对不上的（嫌い→嫌う、曇り→曇る）不自动收录，
  连同它切不动的写法一起进核对清单的 unresolved 段，人工决定去留；
- data/conjugation.json 的 overrides 段是人工修正的唯一入口：重派生只重写
  verbs/meta，overrides 原样保留并在生成时重新套用——手工修正永远不会被
  再生成冲掉（user_tokens.json 的 mtime 缓存教训的镜像问题）；
- 任何动词的四张变形里有一张算不出来就算错误（errors），生成器以非零码退出，
  不允许静默漏词。

运行方式:
    python practice\\conjugation.py        # 重新派生并写 conjugation.json + 核对清单
"""
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_PATH = BASE_DIR / "data" / "conjugation.json"
REVIEW_PATH = BASE_DIR / "data" / "conjugation_review.md"
GENERATOR_VERSION = 3  # v3: 新增 alt_forms 汉字形备选（カ変 来る 打「来て」此前判错）

FORMS = ("te", "ta", "nai", "masu")
FORM_NAMES = {"te": "て形", "ta": "た形", "nai": "ない形", "masu": "ます形"}


# ---------- 规则表 ----------

# janome 会把 する 标成「五段・ラ行」（照规则表会算出「すって」）、
# ある 的ない形也不是「あらない」。规则表覆盖不到的基本形在这里整词特判，
# 先于规则表命中。ある/いる 在词库里以「在る/有る/居る」汉字形收录，
# 基本形各不相同，须逐个特判（ない形习惯上写作假名「ない/いない」）。
MANUAL_FORMS = {
    "する": {"te": "して", "ta": "した", "nai": "しない", "masu": "します"},
    "ある": {"te": "あって", "ta": "あった", "nai": "ない", "masu": "あります"},
    "有る": {"te": "あって", "ta": "あった", "nai": "ない", "masu": "あります"},
    "在る": {"te": "あって", "ta": "あった", "nai": "ない", "masu": "あります"},
    "居る": {"te": "いて", "ta": "いた", "nai": "いない", "masu": "います"},
}

# ます形特例：ラ行特殊 下さる 的ます形是「下さいます」（不是「下さります」）。
# 假名基本形（くださる）走规则表时同样会算错，两把钥匙都特判。
MANUAL_MASU = {"下さる": "下さいます", "くださる": "くださいます"}

# 汉字形备选：标准答案按拍板用假名全形的条目（カ変 来る），用户按汉字作答
# （来て/来た/来ない/来ます）同样正确。只做 kana_forms 不够——来る 的
# kana_forms 与 forms 完全相同，去重后备选为空，而判分只做假名折叠、不会把
# 汉字归一到假名，于是「来て」被打成错。这里显式给出另一套正确写法。
MANUAL_ALT_FORMS = {
    "来る": {"te": "来て", "ta": "来た", "nai": "来ない", "masu": "来ます"},
}

# janome 活用型误标修正：这几个词的变形一直由 MANUAL_FORMS 兜底（答案本来就是
# 对的），但 v2 起 infl_type 会显示在活用卡上——居る 标「五段・ラ行」会让卡片
# 一边写 いて、一边讲「词尾る→って」，教学上自相矛盾。
# 改标签顺带修掉 kana_forms：居る 原本按ラ行算出「いって」（正确是 いて）。
MANUAL_INFL_TYPES = {
    "居る": "一段",
    "する": "サ変・スル",
}

# カ変（きて/きた/こない）按拍板结论用假名全形作标准答案，汉字形（来て等）
# 由题型层通过 alternates 兼容——TTS 念假名全形也最稳。

# janome 还原不出基本形、或整词切出多个词块的词条：按写法人工指定
# 基本形与活用型，生成时强制收录（不走自动收录口径）。
MANUAL_ENTRIES = {
    "下さい": {"basic": "下さる", "infl_type": "五段・ラ行特殊"},
    "コピーする": {"basic": "コピーする", "infl_type": "サ変・スル"},
    # 词条写法本身是た形（渇いた），janome 整词切出「渇い + た」收不了；
    # 按基本形 渇く（かわく，五段カ行イ音便）强制收录：渇いて/渇いた/渇かない。
    "渇いた": {"basic": "渇く", "infl_type": "五段・カ行イ音便"},
}

# janome 误判成动词的词条（实为形容词/名词形写法），不收录；只改核对清单里的
# 原因文案，词条仍会挂在「未收录」段供复核——要彻底静音请用下面的 MANUAL_SKIP。
MANUAL_EXCLUDE_NOTES = {
    "嫌い": "janome 误判为动词（实为な形容词），不收录",
    "きろ": "janome 还原出的基本形「きる」与词条写法对不上，不收录",
    "曇り": "名词形写法（基本形 曇る），要收录的话人工改写法为 曇る",
}

# 人工拍板「不收录」且不必再出现的写法：收录循环里直接 continue，清单里彻底
# 静音。与 MANUAL_EXCLUDE_NOTES 的区别（别混用）：后者只是把原因写进清单，词条
# 每次重跑都还挂着；这里是结论已定、不再占用核对注意力。
MANUAL_SKIP = {
    "ちゅうごく": "janome 首词块误判为动词，实为名词（中国）",
    "とんかつ": "janome 首词块误判为动词，实为名词（炸猪排）",
    # ましょう（劝诱形）词条：教的是劝诱表达而非动词变形，且基本形都已在表内
    "会いましょう": "ましょう 词条不收活用题，基本形 会う 已在表内",
    "撮りましょう": "ましょう 词条不收活用题，基本形 撮る 已在表内",
    "帰りましょう": "ましょう 词条不收活用题，基本形 帰る 已在表内",
    "使いましょう": "ましょう 词条不收活用题，基本形 使う 已在表内",
    "歌いましょう": "ましょう 词条不收活用题，基本形 歌う 已在表内",
}

# 五段动词 ない形：词尾 う 段假名 → ア 段 + ない（待つ→待たない、買う→買わない）
_U_TO_A = {
    "く": "か", "ぐ": "が", "す": "さ", "つ": "た", "ぬ": "な",
    "ぶ": "ば", "む": "ま", "る": "ら", "う": "わ",
}

# 五段动词 ます形：词尾 う 段假名 → い 段 + ます（書く→書きます、飲む→飲みます）
_U_TO_I = {
    "く": "き", "ぐ": "ぎ", "す": "し", "つ": "ち", "ぬ": "に",
    "ぶ": "び", "む": "み", "る": "り", "う": "い",
}

# て形/た形 词尾，按 janome 的 infl_type 精确字符串分派。
# 注意键名是 janome 的实际输出（カ変・来ル 而非「カ行変格」、
# カ行イ音便 与 カ行促音便 是两个不同的键），改键名前先实测。
_TE_TAILS = {
    "五段・カ行イ音便": ("いて", "いた"),   # 開く→開いて
    "五段・ガ行": ("いで", "いだ"),         # 泳ぐ→泳いで（浊化）
    "五段・カ行促音便": ("って", "った"),   # 行く→行って（唯一的カ行促音便）
    "五段・タ行": ("って", "った"),         # 待つ→待って
    "五段・ラ行": ("って", "った"),         # 取る→取って
    "五段・ワ行促音便": ("って", "った"),   # 買う→買って
    "五段・ラ行特殊": ("って", "った"),     # 下さる→下さって
    "五段・ナ行": ("んで", "んだ"),         # 死ぬ→死んで
    "五段・バ行": ("んで", "んだ"),         # 遊ぶ→遊んで
    "五段・マ行": ("んで", "んだ"),         # 飲む→飲んで
    "五段・サ行": ("して", "した"),         # 話す→話して
}


def conjugate(basic, infl_type):
    """按基本形与活用型派生 ます形/て形/た形/ない形；规则覆盖不到返回 None。

    返回的是「汉字词干 + 假名词尾」的答案串（開く→開いて）；カ変・来ル
    按拍板结论返回假名全形（きて/きた/こない）。
    """
    basic = (basic or "").strip()
    if basic in MANUAL_FORMS:
        return dict(MANUAL_FORMS[basic])
    if infl_type == "カ変・来ル":
        return {"te": "きて", "ta": "きた", "nai": "こない", "masu": "きます"}
    # 「する」本身也只有两字：>=2 让它落到同一分支，stem 为空时正好得 して/した…
    if infl_type == "サ変・スル" and len(basic) >= 2:
        stem = basic[:-2]  # 剥掉「する」：勉強する→勉強して
        return {"te": stem + "して", "ta": stem + "した", "nai": stem + "しない",
                "masu": stem + "します"}
    if infl_type == "一段" and basic.endswith("る"):
        stem = basic[:-1]  # 去る：食べる→食べて
        return {"te": stem + "て", "ta": stem + "た", "nai": stem + "ない",
                "masu": stem + "ます"}
    tails = _TE_TAILS.get(infl_type)
    if tails is None or not basic:
        return None
    stem, last = basic[:-1], basic[-1]
    a = _U_TO_A.get(last)
    if a is None:
        return None
    return {"te": stem + tails[0], "ta": stem + tails[1], "nai": stem + a + "ない",
            "masu": MANUAL_MASU.get(basic, stem + _U_TO_I.get(last, "") + "ます")}


# 活用型 → 一句话说明（活用卡与讲解用）：点出该类词尾怎么变，附一个例词
INFL_NOTES = {
    "五段・カ行イ音便": "イ音便：词尾く→いて（開く→開いて）",
    "五段・ガ行": "ガ行：词尾ぐ→いで（泳ぐ→泳いで）",
    "五段・カ行促音便": "促音便：词尾く→って（行く→行って，カ行唯一的促音便特例）",
    "五段・タ行": "タ行：词尾つ→って（待つ→待って）",
    "五段・ラ行": "ラ行：词尾る→って（取る→取って）",
    "五段・ワ行促音便": "促音便：词尾う→って（買う→買って）",
    "五段・ラ行特殊": "ラ行特殊：下さる→下さって（ます形是 下さいます）",
    "五段・ナ行": "ナ行：词尾ぬ→んで（死ぬ→死んで）",
    "五段・バ行": "バ行：词尾ぶ→んで（遊ぶ→遊んで）",
    "五段・マ行": "マ行：词尾む→んで（飲む→飲んで）",
    "五段・サ行": "サ行：词尾す→して（話す→話して）",
    "一段": "一段动词：去る加 て/た/ない（食べる→食べて）",
    "カ変・来ル": "カ変（不规则）：来る→きて/きた/こない/きます",
    "サ変・スル": "サ変（不规则）：する→して/した/しない/します",
}


def infl_note(infl_type):
    """活用型的一句话说明（未知类型返回空串，前端不渲染说明行）。"""
    return INFL_NOTES.get(infl_type, "")


# ---------- 假名全形（打字判分 alternates） ----------

# ます形读音反推基本形时的 い段→う段（_U_TO_I 的逆映射）：かきます→かく
_I_TO_U = {v: k for k, v in _U_TO_I.items()}

# 假名基本形的整词特判（键为 basic）：读音反推规则覆盖不到的例外。
# する 读音就是基本形但 janome 把它标成五段・ラ行（会算出「すって」）；
# 下さる 的ます形读音 ください 机械反推不出 くださる；
# 渇く 的词条读音是た形（かわいた），不是基本形。
MANUAL_KANA_BASIC = {
    "する": "する",
    "下さる": "くださる",
    "渇く": "かわく",
}


def kana_basic(entry):
    """词条基本形的假名写法（打字判分 alternates 用），推不出返回空串。

    读音不是ます形（開く/ひらく）时读音本身就是基本形假名，直接用；
    多邻国的ます形词条读音是ます形（起きます），剥掉ます按活用型反推：
    一段加る、サ変加する、五段把词尾い段还原成う段（かきます→かく）；
    反推覆盖不到的例外进 MANUAL_KANA_BASIC。
    """
    basic = entry.get("basic", "")
    if basic in MANUAL_KANA_BASIC:
        return MANUAL_KANA_BASIC[basic]
    reading = (entry.get("reading") or "").strip()
    if reading and not reading.endswith("ます"):
        return reading
    stem = reading[:-2]
    it = entry.get("infl_type", "")
    if it == "カ変・来ル":
        return "くる"
    if it == "サ変・スル":
        return stem + "する"
    if it.startswith("一段"):
        return stem + "る"
    if stem and stem[-1] in _I_TO_U:
        return stem[:-1] + _I_TO_U[stem[-1]]
    return ""


def kana_forms(entry):
    """四张变形的假名全形（開く→ひらいて），完整四键；推不出返回空 dict。

    假名基本形走同一套 conjugate 规则——只是把汉字词干换成假名，
    活用规则完全一致。与标准答案相同的键不去重（去重是消费方的职责）：
    标准答案本来就是假名的词（来る/かかる）两套完全一致，属正常现象。
    """
    kb = kana_basic(entry)
    if not kb:
        return {}
    forms = conjugate(kb, entry.get("infl_type", ""))
    if not forms or not all(forms.get(f) for f in FORMS):
        return {}
    return {f: forms[f] for f in FORMS}


def alt_forms(entry):
    """汉字形备选答案（来る→来て），推不出返回空 dict。

    与 kana_forms 对称：kana_forms 让「打全假名」算对，alt_forms 让「打汉字」
    算对。只有标准答案本身就是假名的条目（カ変 来る）才需要——其余动词的
    标准答案已是汉字混形，假名那头由 kana_forms 兜住。
    """
    manual = MANUAL_ALT_FORMS.get(entry.get("basic", ""))
    if not manual:
        return {}
    return {f: manual[f] for f in FORMS if manual.get(f)}


# ---------- 词库派生 ----------

_JANOME_TOK = None


def _get_tok():
    """janome 分词器（惰性初始化，未安装时给出安装提示）。"""
    global _JANOME_TOK
    if _JANOME_TOK is None:
        try:
            from janome.tokenizer import Tokenizer
        except ImportError:
            raise RuntimeError(
                "需要 janome 分词库：pip install janome（规则派生依赖它的活用型标注）")
        _JANOME_TOK = Tokenizer()
    return _JANOME_TOK


def _tokenize(text):
    return list(_get_tok().tokenize(text))


def _written_form(word):
    """词条写法：有汉字取汉字，纯假名词（kanji 为 "---"）回退读音（与 app.word_form 同口径）。"""
    kanji = (word.get("kanji") or "").strip()
    if kanji and kanji != "---":
        return kanji
    return (word.get("hiragana") or "").strip()


def build_verb_index(vocab):
    """逐词扫词库，派生动词索引。返回 (entries, report)。

    收录口径（优先级从上到下）：
    0. MANUAL_SKIP 里的写法直接跳过（人工拍板不收录，不进核对清单）；
    1. MANUAL_ENTRIES 按写法强制收录（source=manual）；
    2. 整词切单个词块、词块是动词且 base_form 归一 == 写法（source=derived）；
    3. 写法 = 动词連用形 + ます（多邻国的 ます形词条）→ 用 base_form 还原
       基本形收录（source=converted，清单里需人工确认还原结果）；
    4. 首词块是动词但以上都不满足 → 不收录，进 report["unresolved"]（人工决定）。
    report["errors"] 是四张变形算不出来的词条（规则表漏洞，生成器报错退出）。
    """
    entries, unresolved = [], []
    for lid, lesson in vocab.get("lessons", {}).items():
        for w in lesson.get("words", []):
            written = _written_form(w)
            if not written or written in MANUAL_SKIP:
                continue
            base = {
                "ref": f"{lid}:{w.get('id')}",
                "lesson_id": lid,
                "word_id": w.get("id") or "",
                "written": written,
                "reading": (w.get("hiragana") or "").strip(),
                "meaning": (w.get("meaning") or "").strip(),
            }
            manual = MANUAL_ENTRIES.get(written)
            if manual:
                entries.append(dict(base, basic=manual["basic"],
                                    infl_type=manual["infl_type"], source="manual"))
                continue
            toks = _tokenize(written)
            if not toks:
                continue
            pos0 = toks[0].part_of_speech.split(",")[0]
            if pos0 != "動詞":
                continue
            if (len(toks) == 1 and (toks[0].base_form or "").strip() == written):
                entries.append(dict(base, basic=written,
                                    infl_type=toks[0].infl_type, source="derived"))
                continue
            if (len(toks) == 2 and toks[1].surface == "ます"
                    and toks[0].infl_form == "連用形"):
                entries.append(dict(base, basic=(toks[0].base_form or "").strip(),
                                    infl_type=toks[0].infl_type, source="converted"))
                continue
            # 动词性词条但自动口径收不了：列出来让人工拍板，不静默漏词
            if len(toks) == 1:
                reason = f"base_form 还原为「{toks[0].base_form}」，与写法不一致"
            else:
                reason = "整词切出 " + " + ".join(t.surface for t in toks) + "，不是基本形"
            if written in MANUAL_EXCLUDE_NOTES:
                reason = MANUAL_EXCLUDE_NOTES[written]
            unresolved.append(dict(base, reason=reason))

    # 四张变形必须都算得出来，算不出 = 规则表漏洞，单独列出并报错
    ok, errors = [], []
    for e in entries:
        # janome 活用型误标修正：变形本身由 MANUAL_FORMS 兜底，但标签会显示在
        # 活用卡上（v2 起用户可见），且影响 kana_forms 的假名词干走哪条规则
        fixed = MANUAL_INFL_TYPES.get(e["basic"]) or MANUAL_INFL_TYPES.get(e["written"])
        if fixed:
            e["infl_type"] = fixed
        forms = conjugate(e["basic"], e["infl_type"])
        if forms is None or not all(forms.get(f) for f in FORMS):
            errors.append(dict(e, reason="规则表算不出四张变形"
                                    f"（basic={e['basic']} infl_type={e['infl_type']}）"))
        else:
            e["forms"] = forms
            e["kana_forms"] = kana_forms(e)
            e["alt_forms"] = alt_forms(e)
            ok.append(e)
    report = {"unresolved": unresolved, "errors": errors}
    return ok, report


# ---------- overrides 与落盘 ----------

def apply_overrides(entries, overrides):
    """把 overrides 段套到派生结果上，返回 (套用后的词条列表, 告警列表)。

    override 按 ref 生效：exclude 剔除词条；basic/infl_type 改派生入参
    （改完按规则重算）；te/ta/nai 直接指定最终答案串，优先级最高。
    """
    overridden, warnings = [], []
    for e in entries:
        ov = overrides.get(e["ref"])
        if not ov:
            overridden.append(e)
            continue
        if ov.get("exclude"):
            continue
        e = dict(e)
        for key in ("basic", "infl_type"):
            if ov.get(key):
                e[key] = str(ov[key])
        forms = conjugate(e["basic"], e["infl_type"])
        if forms is None:
            warnings.append(f"{e['ref']}：override 改 basic/infl_type 后仍算不出变形，"
                            "请直接指定 te/ta/nai")
            forms = e.get("forms") or {}
        e["forms"] = dict(forms)
        for f in FORMS:
            if ov.get(f):
                e["forms"][f] = str(ov[f])
        # basic/infl_type 可能被 override 改过：假名全形与汉字备选按新入参重算
        #（te/ta/nai 直接指定的答案不重算假名，override 是修正规则错误的最后手段）
        e["kana_forms"] = kana_forms(e)
        e["alt_forms"] = alt_forms(e)
        overridden.append(e)
    return overridden, warnings


def load_data(path=None):
    """读取落盘的活用数据（供后续讲解卡/题型消费），无文件时返回 None。"""
    path = Path(path) if path else DATA_PATH
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def generate(vocab_path=None, out_path=None, review_path=None):
    """全量派生 + 套用已有 overrides + 落盘。返回 (data, report, warnings)。

    overrides 段从旧文件原样保留：重派生只重写 verbs/meta，手工修正不丢。
    """
    vocab_path = Path(vocab_path) if vocab_path else BASE_DIR / "vocabulary.json"
    out_path = Path(out_path) if out_path else DATA_PATH
    review_path = Path(review_path) if review_path else REVIEW_PATH

    vocab = json.loads(vocab_path.read_text(encoding="utf-8"))
    entries, report = build_verb_index(vocab)

    overrides, warnings = {}, []
    if out_path.exists():
        try:
            overrides = json.loads(out_path.read_text(encoding="utf-8")).get("overrides", {})
        except (ValueError, OSError) as exc:
            warnings.append(f"旧 overrides 段读取失败，按空处理：{exc}")
    entries, warnings = apply_overrides(entries, overrides)

    data = {
        "meta": {
            "generator_version": GENERATOR_VERSION,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "vocab_mtime": datetime.fromtimestamp(vocab_path.stat().st_mtime).isoformat(timespec="seconds"),
            "counts": {
                "verbs": len(entries),
                "derived": sum(1 for e in entries if e["source"] == "derived"),
                "converted": sum(1 for e in entries if e["source"] == "converted"),
                "manual": sum(1 for e in entries if e["source"] == "manual"),
            },
        },
        "verbs": entries,
        "overrides": overrides,  # 原样保留的人工修正段
    }
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    review_path.write_text(render_review(entries, report, warnings), encoding="utf-8")
    return data, report, warnings


# ---------- 核对清单 ----------

def _cell(entry, key):
    """取单元格值；支持「forms.te」这类一层嵌套键（变形都在 forms 子字典里）。"""
    if "." in key:
        outer, inner = key.split(".", 1)
        sub = entry.get(outer)
        return sub.get(inner) if isinstance(sub, dict) else None
    return entry.get(key)


def _table(entries, columns):
    # columns 为 (取值键, 表头标签) 对：表头用标签，行取键对应的值
    head = "| " + " | ".join(c[1] for c in columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    rows = ["| " + " | ".join(str(_cell(e, c[0]) or "") for c in columns) + " |"
            for e in entries]
    return "\n".join([head, sep] + rows) if rows else "（无）"


def render_review(entries, report, warnings):
    """把派生结果渲染成人工核对清单（markdown）。规则对不对，肉眼翻一遍最快。"""
    cols = [("ref", "ref"), ("written", "写法"), ("basic", "基本形"),
            ("infl_type", "活用型"), ("forms.masu", "ます形"), ("forms.te", "て形"),
            ("forms.ta", "た形"), ("forms.nai", "ない形"),
            ("meaning", "释义"), ("source", "来源")]
    parts = [
        "# 动词活用变形核对清单",
        "",
        "由 conjugation.py 自动生成，请逐条核对变形是否正确；发现错误改",
        "data/conjugation.json 的 overrides 段（按 ref 指定 te/ta/nai，或 exclude 剔除），",
        "改完重跑生成器，overrides 会原样保留。",
        "",
        f"共 {len(entries)} 条（derived {sum(1 for e in entries if e['source'] == 'derived')}"
        f" / converted {sum(1 for e in entries if e['source'] == 'converted')}"
        f" / manual {sum(1 for e in entries if e['source'] == 'manual')}）",
        "",
        "词条另含 kana_forms 假名全形（開く→ひらいて）：打字题全假名作答的备选答案。"
        "标准答案本就是假名的词（来る/かかる）两套一致，属正常；这类词另靠"
        "alt_forms 给出汉字形备选（来る→来て/来た/来ない/来ます），打汉字也算对。",
        "",
        "## 全部收录词条",
        "",
        _table(entries, cols),
        "",
        "## 未收录的动词性词条（需人工拍板）",
        "",
        _table(report["unresolved"],
               [("written", "写法"), ("ref", "ref"), ("reason", "原因")]),
        "",
    ]
    if report["errors"]:
        parts += ["## 规则算不出的词条（错误，必须处理）", "",
                  _table(report["errors"],
                         [("written", "写法"), ("basic", "基本形"),
                          ("infl_type", "活用型"), ("reason", "原因")]), ""]
    if warnings:
        parts += ["## 生成告警", ""] + [f"- {w}" for w in warnings] + [""]
    return "\n".join(parts)


def main(argv=None):
    """CLI：重新派生并落盘；有规则算不出的词条时以非零码退出。"""
    data, report, warnings = generate()
    counts = data["meta"]["counts"]
    print(f"已生成 {DATA_PATH.name}：{counts['verbs']} 条动词"
          f"（derived {counts['derived']} / converted {counts['converted']}"
          f" / manual {counts['manual']}）")
    print(f"核对清单：{REVIEW_PATH}")
    n_kana = sum(1 for e in data["verbs"] if e.get("kana_forms"))
    if n_kana < counts["verbs"]:
        print(f"注意：{counts['verbs'] - n_kana} 条假名全形推不出（打字题只认标准答案），"
              "例外可在 MANUAL_KANA_BASIC 补特判。")
    else:
        print(f"假名全形：{n_kana}/{counts['verbs']} 条全部推出。")
    for w in warnings:
        print(f"告警：{w}")
    if report["unresolved"]:
        print(f"注意：{len(report['unresolved'])} 条动词性词条未自动收录，"
              "见清单「未收录的动词性词条」段，请人工拍板。")
    if report["errors"]:
        for e in report["errors"]:
            print(f"错误：{e['written']}（{e['basic']} / {e['infl_type']}）：{e['reason']}",
                  file=sys.stderr)
        print(f"共 {len(report['errors'])} 条变形算不出来，规则表有漏洞！", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
