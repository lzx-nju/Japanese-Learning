#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""图片日语 —— 课件 md → 结构化 JSON 导入器。

「日常图片讲解课」的人写稿是 markdown（`lessons/图片日语_第N课.md`），
看图、读单词、讲语法都在这份稿子里；这个脚本把它解析成网页端直接可用的
`data/picture_lessons/<id>.json`：

    写真N：标题 / 图片 / 图片文字 / 核心单词表 / 语法·句式（段落 + 要点 + 提示）
    まとめ：本课核心语法（有序列表）+ 核心单词（顿号分隔的名单）
    練習：每题的题干、括号里的提示、（可选）参考答案

md 的格式约定就是第 1 课那份稿子的写法（见 docstring 末尾的骨架）。
**练习参考答案不在 md 里**（md 是学习稿，答案单列）：写在
`data/picture_lessons/<id>.answers.json` 里，按题号给答案数组，本脚本合并；
题数与答案数对不上时只报警告、不丢内容。格式：

    {"1": ["先生にほめられたい。", ...], "3": ["～に～られたい：先生にほめられたい。"]}

用法（批量改数据文件前先停练习服务；_DATA_LOCK 管不了跨进程）：

    python.exe practice\\import_picture_lesson.py lessons\\图片日语_第1课.md --id lesson_01
    python.exe practice\\import_picture_lesson.py lessons\\图片日语_第1课.md --id lesson_01 --write

默认只预览（打印解析统计与警告，不落盘），`--write` 才写 JSON。幂等：
同一份 md 反复导入结果相同（图片路径、题号顺序都按 md 顺序取）。
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LESSONS_DIR = BASE_DIR / "data" / "picture_lessons"

# ## 写真1：名大ギャルゲー同好会のポスター
_PHOTO_RE = re.compile(r"^写真\s*(\d+)\s*[：:]\s*(.*)$")
# ### 语法：～に～られたい（想被……做……） / ### 实用句式：～募集中（正在招募……）
_POINT_RE = re.compile(r"^(语法|句式|实用句式|句型|表达|说法)\s*[：:]\s*(.*)$")
# ![alt](../data/extracted_images/image1.jpg)
_IMAGE_RE = re.compile(r"^!\[[^\]]*\]\(([^)]+)\)\s*$")
# **1. 翻译以下句子：**
_EX_GROUP_RE = re.compile(r"^\*\*\s*(\d+)\s*[.．、]\s*(.+?)\s*\*\*\s*$")
# 1. ～に～られたい — 想被……做……
_OL_RE = re.compile(r"^\d+\s*[.．、]\s*(.+)$")
# 练习题末尾的括号提示：（ほめる → ほめられたい） / （ボランティア）
_HINT_RE = re.compile(r"^(.*?)[（(]([^（）()]{1,40})[）)]\s*$")

_SECTION_SUMMARY = ("まとめ", "总结", "まとめと復習")
_SECTION_EXERCISE = ("練習", "练习", "练习题")


def _inline(text):
    """md 行内标记归一到前端能认的最小子集：只留段落内的强调由前端渲染。

    这里不做 HTML——落盘的是文本，**渲染时才转义 + 只认 **粗体****，
    免得 JSON 里存进半截 HTML 标签。
    """
    return text.strip()


def _norm_image_path(raw, md_dir):
    """md 里的图片路径 → 仓库根相对路径（posix）。

    相对路径按 **md 文件所在目录** 解析（md 在 lessons/ 下，图片多半写成
    ../data/extracted_images/image1.jpg）；解析后必须落在仓库内，否则原样返回
    （发图路由会再拒一次——越界路径发不出去）。
    """
    raw = raw.strip().strip("<>")
    p = Path(raw)
    if not p.is_absolute():
        p = Path(md_dir) / p
    try:
        return p.resolve().relative_to(BASE_DIR.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


class _LessonBuilder:
    """顺序解析 md：遇到容器标题就切当前容器，内容行按当前模式归位。"""

    def __init__(self, lesson_id, md_dir=None):
        self.md_dir = md_dir
        self.data = {
            "schema": 1,
            "id": lesson_id,
            "title": "",
            "intro": "",
            "photos": [],
            "summary": {"points": [], "words": []},
            "exercises": [],
        }
        self.warnings = []
        self.mode = "head"       # head / photo / point / summary / exercise
        self.photo = None        # 当前 photo dict
        self.point = None        # 当前语法/句式 dict
        self.sub = ""            # words / point / sum_points / sum_words / ex
        self.exercise = None     # 当前练习题组
        self._para = []          # 普通段落缓冲（连续的纯文本行）
        self._words_header = False  # 单词表表头行还没跳过

    # ---- 段落缓冲：遇到非纯文本行先冲出去 ----
    def flush_para(self):
        if not self._para:
            return
        text = " ".join(self._para).strip()
        self._para = []
        if not text:
            return
        if self.mode == "photo" and self.sub == "point" and self.point is not None:
            self.point["blocks"].append({"t": "p", "text": _inline(text)})
        elif self.mode == "exercise" and self.exercise is not None:
            self.exercise["blocks"].append({"t": "p", "text": _inline(text)})
        elif self.mode == "head" and not self.data["intro"]:
            # 标题下第一段普通文本当导语（第 1 课是引用，见 add_quote）
            self.data["intro"] = _inline(text)

    # ---- 各类内容 ----
    def add_quote(self, text):
        text = _inline(text)
        if self.mode == "head":
            self.data["intro"] = text
        elif self.mode == "photo" and self.sub == "":
            self.photo["texts"].append(text)          # 图片原文
        elif self.mode == "photo" and self.sub == "point":
            self.point["blocks"].append({"t": "tip", "text": text})   # 语法提示
        elif self.mode == "exercise" and self.exercise is not None:
            self.exercise["blocks"].append({"t": "tip", "text": text})
        else:
            self.warnings.append(f"未归位的引用行（mode={self.mode}/sub={self.sub}）：{text}")

    def add_bullet(self, text):
        text = _inline(text)
        if self.mode == "photo" and self.sub == "point":
            blocks = self.point["blocks"]
            if blocks and blocks[-1].get("t") == "ul":
                blocks[-1]["items"].append(text)
            else:
                blocks.append({"t": "ul", "items": [text]})
        elif self.mode == "exercise" and self.exercise is not None:
            q, hint = _split_hint(text)
            item = {"q": q}
            if hint:
                item["hint"] = hint
            self.exercise["items"].append(item)
        else:
            self.warnings.append(f"未归位的列表行（mode={self.mode}/sub={self.sub}）：{text}")

    def add_bold(self, text):
        """独立成行的 **xxx**：图片说明（以冒号结尾）或其他强调行。"""
        text = _inline(text)
        if self.mode == "photo" and self.sub == "":
            if text.endswith(("：", ":")):
                self.photo["caption"] = text.rstrip("：:").strip()
            else:
                self.photo["texts"].append(text)
        elif self.mode == "photo" and self.sub == "point":
            self.point["blocks"].append({"t": "p", "text": text})
        else:
            self.warnings.append(f"未归位的强调行（mode={self.mode}/sub={self.sub}）：{text}")

    def add_table_row(self, cells):
        if self.mode != "photo" or self.sub != "words":
            self.warnings.append(f"表格行不在单词表里：{cells}")
            return
        if self._words_header:      # 表头行「| 单词 | 假名 | 释义 |」
            self._words_header = False
            return
        form, kana, meaning = (cells + ["", "", ""])[:3]
        extra = " / ".join(c for c in cells[3:] if c)
        word = {"form": form, "kana": kana, "meaning": meaning}
        if extra:
            word["note"] = extra
        self.photo["words"].append(word)

    def add_ordered(self, text):
        text = _inline(text)
        if self.mode == "summary":
            self.data["summary"]["points"].append(text)
        else:
            self.warnings.append(f"未归位的编号行（mode={self.mode}）：{text}")

    def add_plain(self, text):
        self._para.append(text)

    def add_summary_words(self, text):
        text = text.strip()
        if not text:
            return
        words = [w.strip() for w in re.split(r"[、,，]", text) if w.strip()]
        self.data["summary"]["words"].extend(words)

    # ---- 结构切换 ----
    def start_photo(self, m):
        self.flush_para()
        self.photo = {
            "n": int(m.group(1)),
            "title": m.group(2).strip(),
            "image": "",
            "caption": "",
            "texts": [],
            "words": [],
            "points": [],
        }
        self.data["photos"].append(self.photo)
        self.point = None
        self.mode, self.sub = "photo", ""

    def start_point(self, m):
        self.flush_para()
        kind = "grammar" if m.group(1) == "语法" else "pattern"
        self.point = {"kind": kind, "title": m.group(2).strip(), "blocks": []}
        self.photo["points"].append(self.point)
        self.sub = "point"

    def start_words(self):
        self.flush_para()
        self.sub = "words"
        self._words_header = True

    def start_summary(self):
        self.flush_para()
        self.mode, self.sub = "summary", "sum_points"
        self.point = None

    def start_exercises(self):
        self.flush_para()
        self.mode, self.sub, self.exercise = "exercise", "ex", None

    def start_exercise_group(self, m):
        self.flush_para()
        self.exercise = {"n": int(m.group(1)), "title": m.group(2).strip().rstrip("：:"),
                         "items": [], "blocks": []}
        self.data["exercises"].append(self.exercise)

    def finish(self):
        self.flush_para()


def _split_hint(text):
    """练习题：「句子（提示）」→ (句子, 提示)。括号太长就当成句子的一部分。"""
    m = _HINT_RE.match(text)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2).strip()
    return text, ""


def parse_lesson(text, lesson_id, md_dir=None):
    """md 文本 → 课程 dict。返回 (lesson, warnings)。

    md_dir 是 md 文件所在目录（图片相对路径据此解析）；缺省按仓库 lessons/。
    """
    b = _LessonBuilder(lesson_id, md_dir or (BASE_DIR / "lessons"))
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for raw in lines:
        s = raw.strip()
        if not s:
            b.flush_para()
            continue
        if s.startswith("# ") and not b.data["title"]:
            b.flush_para()
            b.data["title"] = s[2:].strip()
            continue
        if s.startswith("## "):
            h = s[3:].strip()
            m = _PHOTO_RE.match(h)
            if m:
                b.start_photo(m)
            elif h in _SECTION_SUMMARY:
                b.start_summary()
            elif h in _SECTION_EXERCISE:
                b.start_exercises()
            else:
                b.warnings.append(f"未知的一级小节（已跳过，内容按所在容器归位）：{h}")
            continue
        if s.startswith("### "):
            h = s[4:].strip()
            if h.startswith("核心单词") or h.startswith("重点单词"):
                b.start_words()
            elif h.startswith(("本课核心语法", "本课语法")):
                b.flush_para()
                b.mode, b.sub = "summary", "sum_points"
            elif h.startswith("本课核心单词"):
                b.flush_para()
                b.mode, b.sub = "summary", "sum_words"
            elif _POINT_RE.match(h):
                b.start_point(_POINT_RE.match(h))
            elif b.mode == "photo":
                # 没写「语法：」前缀的小节也当讲解块，不丢内容
                b.flush_para()
                b.point = {"kind": "note", "title": h, "blocks": []}
                b.photo["points"].append(b.point)
                b.sub = "point"
            else:
                b.warnings.append(f"未知的二级小节（已跳过）：{h}")
            continue
        if s.startswith("---"):
            b.flush_para()
            continue
        # 练习题组标题 **1. 翻译以下句子：**
        m = _EX_GROUP_RE.match(s)
        if m and b.mode == "exercise":
            b.start_exercise_group(m)
            continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue  # 表头下的分隔行
            b.flush_para()
            b.add_table_row(cells)
            continue
        if s.startswith("> "):
            b.flush_para()
            b.add_quote(s[2:].strip())
            continue
        if s.startswith(("- ", "* ", "・")):
            b.flush_para()
            b.add_bullet(s[2:].strip() if s[0] in "-*" else s[1:].strip())
            continue
        if s.startswith("**") and s.endswith("**") and len(s) > 4:
            b.flush_para()
            b.add_bold(s[2:-2].strip())
            continue
        m = _OL_RE.match(s)
        if m and b.mode == "summary":
            b.flush_para()
            b.add_ordered(m.group(1))
            continue
        m = _IMAGE_RE.match(s)
        if m:
            b.flush_para()
            if b.mode != "photo":
                b.warnings.append(f"图片不在写真的小节里：{s}")
            else:
                b.photo["image"] = _norm_image_path(m.group(1), b.md_dir)
            continue
        # 普通文本
        if b.mode == "summary" and b.sub == "sum_words":
            b.flush_para()
            b.add_summary_words(s)
            continue
        b.add_plain(s)
    b.finish()
    return b.data, b.warnings


def load_answers(lesson_id):
    """参考答案 sidecar：{题号(str): [答案, ...]}；不存在返回 {}。"""
    path = LESSONS_DIR / f"{lesson_id}.answers.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): v for k, v in (raw or {}).items()
            if k != "_comment" and isinstance(v, list)}


def merge_answers(lesson, answers):
    """把 sidecar 答案按题号合并进 exercises；数量对不上只记警告。

    有逐题的（- 列表）按题号下发给每个 item；只有一段说明的开放题
    （如「回看 5 张图片，列出认识的词」）当作整组参考答案 `answer`。
    """
    warnings = []
    for ex in lesson.get("exercises", []):
        ans = answers.get(str(ex.get("n")))
        if not ans:
            continue
        items = ex.get("items") or []
        if not items:
            if len(ans) == 1:
                ex["answer"] = ans[0]
            else:
                warnings.append(
                    f"练习 {ex.get('n')} 没有逐题列表，答案却是 {len(ans)} 段"
                    f"（开放题只支持 1 段参考答案），本题组不合并答案")
            continue
        if len(ans) != len(items):
            warnings.append(
                f"练习 {ex.get('n')} 的答案数（{len(ans)}）与题目数（{len(items)}）"
                f"对不上，本题组不合并答案")
            continue
        for item, a in zip(items, ans):
            if a:
                item["answer"] = a
    return warnings


def validate(lesson):
    """落盘前的自检：错误直接拒绝（宁可报错也不写出半成品数据）。"""
    errors = []
    if not lesson.get("title"):
        errors.append("没解析到课程标题（md 首行 # 标题）")
    if not lesson.get("photos"):
        errors.append("没解析到任何「## 写真N：」小节")
    for p in lesson.get("photos", []):
        if not p.get("image"):
            errors.append(f"写真{p.get('n')} 没有图片（md 里应有 ![alt](路径)）")
        elif not (BASE_DIR / p["image"]).exists():
            errors.append(f"写真{p.get('n')} 的图片文件不存在：{p['image']}")
        if not p.get("words"):
            errors.append(f"写真{p.get('n')} 没有核心单词表")
    return errors


def build(md_path, lesson_id, mark_imported=True):
    md_path = Path(md_path)
    if not md_path.exists():
        raise SystemExit(f"md 文件不存在：{md_path}")
    text = md_path.read_text(encoding="utf-8")
    lesson, warnings = parse_lesson(text, lesson_id, md_path.parent.resolve())
    warnings += merge_answers(lesson, load_answers(lesson_id))
    try:
        lesson["source"] = md_path.resolve().relative_to(BASE_DIR.resolve()).as_posix()
    except ValueError:
        lesson["source"] = md_path.name
    if mark_imported:
        lesson["updated"] = datetime.now().strftime("%Y-%m-%d")
    errors = validate(lesson)
    if errors:
        raise SystemExit("导入失败：\n  - " + "\n  - ".join(errors))
    return lesson, warnings


def main(argv=None):
    ap = argparse.ArgumentParser(description="图片日语课件 md → JSON 导入器")
    ap.add_argument("md", help="课件 markdown（如 lessons/图片日语_第1课.md）")
    ap.add_argument("--id", default=None,
                    help="课程 id（默认取 md 文件名「第N课」→ lesson_NN，取不到则 lesson_01）")
    ap.add_argument("--write", action="store_true", help="写入 data/picture_lessons/<id>.json")
    args = ap.parse_args(argv)

    lesson_id = args.id
    if not lesson_id:
        m = re.search(r"第\s*([0-9０-９]+)\s*课", Path(args.md).stem)
        n = m.group(1) if m else "1"
        n = "".join(chr(ord(c) - 0xFEE0) if "０" <= c <= "９" else c for c in n)
        lesson_id = f"lesson_{int(n):02d}"

    lesson, warnings = build(args.md, lesson_id)
    photos = lesson["photos"]
    print(f"课程 {lesson_id}：{lesson['title']}")
    print(f"  写真 {len(photos)} 张，照片文字 {sum(len(p['texts']) for p in photos)} 条，"
          f"核心单词 {sum(len(p['words']) for p in photos)} 个，"
          f"语法/句式 {sum(len(p['points']) for p in photos)} 条")
    print(f"  まとめ：语法 {len(lesson['summary']['points'])} 条、"
          f"核心单词 {len(lesson['summary']['words'])} 个；"
          f"練習 {len(lesson['exercises'])} 组")
    for ex in lesson["exercises"]:
        got = sum(1 for it in ex["items"] if it.get("answer"))
        ans = f"逐题答案 {got} 条" if ex["items"] else ("整组参考答案 1 段" if ex.get("answer") else "无答案")
        print(f"    练习 {ex['n']}. {ex['title']}：{len(ex['items'])} 题，{ans}")
    for w in warnings:
        print(f"  [警告] {w}")
    if not args.write:
        print("（预览模式，未落盘；确认无误后加 --write）")
        return 0
    LESSONS_DIR.mkdir(parents=True, exist_ok=True)
    out = LESSONS_DIR / f"{lesson_id}.json"
    out.write_text(json.dumps(lesson, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {out.relative_to(BASE_DIR)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
