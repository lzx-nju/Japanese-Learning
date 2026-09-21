#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""日语练习程序 - 本地 Flask Web 应用

基于项目根目录的 vocabulary.json 进行单词练习，
练习结束后自动更新掌握度（mastery），并把本次记录写入 quizzes/ 目录。

运行方式:
    python.exe practice\\app.py
然后浏览器打开: http://127.0.0.1:5000
"""
import hashlib
import json
import os
import random
import re
import shutil
import socket
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta
from itertools import combinations
from pathlib import Path
from urllib.parse import quote

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException, InternalServerError

# 作品沉浸：解析库（导入器 import_work.py / 阅读器 /reader 共用）
import works
# 作品导入（与 CLI 同一条管线，Web 上传复用）
import import_work
# 音声作品（音轨 + 字幕/台本）：字幕解析与时间轴（导入器 import_voice.py 共用）
import voice
# 图片日语：日常图片讲解课的课程库与实时标注（数据由 import_picture_lesson.py 导入）
import picture
# 音声作品导入器（界面 zip 上传 → 解压 → 与 CLI 同一条管线）
import import_voice
# 动词活用变形（て/た/ない/ます形）：活用卡与练习反馈的活用提示（janome 惰性加载）
import conjugation
# 服务器 ASR 桥接（导入作品 → GPU 服务器转写）与后台任务（落盘可恢复）
from server_asr_bridge import ServerAsr, ServerAsrError, validate_engine, validate_gpu
import server_asr_task

# ---------- 路径配置 ----------
BASE_DIR = Path(__file__).resolve().parent.parent  # 项目根目录
VOCAB_PATH = BASE_DIR / "vocabulary.json"
# 起始版用的零状态种子词表（私人仓库里没有这个文件，自举自然不触发）
VOCAB_SEED_PATH = BASE_DIR / "data" / "seed" / "vocabulary_seed.json"
PARTICLES_PATH = BASE_DIR / "particles.json"
EXAM_PATH = BASE_DIR / "data" / "jlpt_n5" / "exam_n5.json"
QUIZZES_DIR = BASE_DIR / "quizzes"
AUDIO_DIR = Path(__file__).resolve().parent / "audio"  # 音频缓存目录
# 单词配图缓存（generate_vocab_images.py 预生成，命名与音频对齐：{lesson_id}_{word_id}.png）
VOCAB_IMAGE_DIR = Path(__file__).resolve().parent / "static" / "images" / "vocab"
# 重点词（聚焦练习）清单：见下方「重点词」小节与 /api/focus
FOCUS_PATH = BASE_DIR / "data" / "focus_words.json"
FOCUS_MAX = 200  # 清单上限：再大就失去「聚焦」的意义（也防误粘一大片）

# 数据锁：保护 vocabulary/particles/exam 的「读→改→写」临界区。
# Flask dev server 默认多线程，双击交卷/双开标签页会让两个请求并发
# 走完 load→modify→save，后写者覆盖先写者的掌握度更新（静默丢进度）。
_DATA_LOCK = threading.Lock()

app = Flask(__name__, template_folder="templates", static_folder="static")


# ====================================================================
# 练习模式注册表：题干（cue）× 作答（task）
# --------------------------------------------------------------------
# 「看xx写xx」「看xx选xx」其实是同一张矩阵的格子，拆成十几个各写一份的
# 模式定义就会到处按模式 id 分叉（出题、判分、前端交互各写一遍）。
# 这里把词库题型统一声明成「题干怎么给 + 答案怎么交」两个维度：
#
#   题干 cue    中文 cn / 词形 form / 假名 kana / 配图 image / 发音 audio
#   作答 task   写假名 write_kana / 写汉字 write_kanji / 选意思 choose_cn
#               选汉字 choose_form / 选发音 choose_audio
#
# 其余字段（题干字段、答案字段、是否选择题、是否配图/听音题干、是否要含汉字、
# 作答前能否播发音）全部由 make_drill_mode 从 (cue, task) 派生，
# 前端交互再由 mode_ui 打包下发——新增一个组合只要加一行，不必改第二处代码。
#
# 组合合法性（同一个矩阵里不成立的格子不该出题）：
#   ✓ 已启用            × 不成立（已停用/从未启用）
#
#             写假名  写汉字  选意思  选汉字  选发音
#   中文 cn     ×
#   词形 form   ✓         ✓         ✓
#   假名 kana        ✓
#   配图 image                 ✓    ✓
#   发音 audio  ✓         ✓
#
#   中文 → 写假名 ×（已停用）：中文释义在词库里不唯一（同义/近义词、多义项），
#     凭它默写读音无法唯一判分——「朋友」写 ゆうじん 还是 ともだち 都是对的。
#     「义 → 音」这条链路由「看汉字写假名」（形→音）与「听音写假名」覆盖，
#     中文此后只作答案口径（选意思），不再作题干。旧记录按 RETIRED_MODES 显示。
#   配图 → 写假名 ×：一张图对应哪个词/哪个读音不唯一（「一杯清水」是 みず 还是
#     水？），凭图拼出假名也超出再认范围，已停用（旧记录按 RETIRED_MODES 显示）。
#   词形 → 选发音：四个选项各是一段发音，点哪个听哪个——题干不能先播发音，
#     播了等于把答案念一遍（spoiler 由此派生）。纯假名词的题干就是答案本身，
#     故与看汉字写假名同一口径：要求写法含汉字（见 KANJI_PROMPT_PAIRS）。
#   配图 → 选发音：四个选项就是四段发音，同上（旧版「图+音 → 选读音」正是把
#     答案念了一遍，已按此重做）。
#   发音 → 写汉字 ×：同音词（がくせい → 学生/成清）无法唯一判分。
#
# ====================================================================

# 题干呈现方式：field 是取词表里的哪个字段；gives_reading 表示题干是否已把读音
# 交出来了（决定「播放发音」是否算剧透，见 make_drill_mode 的 spoiler）
CUE_DEFS = {
    "cn": {"label": "中文", "field": "meaning", "gives_reading": False},
    "form": {"label": "词形", "field": "kanji", "gives_reading": False},
    "kana": {"label": "假名", "field": "hiragana", "gives_reading": True},
    "image": {"label": "配图", "field": "", "gives_reading": False},
    "audio": {"label": "发音", "field": "hiragana", "gives_reading": True},
}

# 作答方式：answer 是答案字段；choice 为真走四选一 UI，否则打字输入
TASK_DEFS = {
    "write_kana": {"label": "写假名", "answer": "hiragana", "choice": False},
    "write_kanji": {"label": "写汉字", "answer": "kanji", "choice": False,
                    # 答案不是假名：罗马字转换会妨碍打汉字，且必须按原字形比对
                    "raw_input": True, "exact_answer": True},
    "choose_cn": {"label": "选意思", "answer": "meaning", "choice": True},
    "choose_form": {"label": "选汉字", "answer": "kanji", "choice": True},
    # 选发音：四个选项各是一段发音（选项文本仍是读音，只用于判分、不给眼睛看，
    # 前端按 audio_options 把选项渲染成喇叭瓦片）。
    "choose_audio": {"label": "选发音", "answer": "hiragana", "choice": True,
                     "audio_options": True},
}

# 答案或题干必须是「含汉字的写法」的组合：
# 答案是汉字（写/选汉字）自然要汉字；题干是词形、而答案能由字形直接推出时
# （看汉字写假名 / 看词形选发音），纯假名词的题干就是答案本身，也不算出题。
KANJI_ANSWER_TASKS = ("write_kanji", "choose_form")
KANJI_PROMPT_PAIRS = {("form", "write_kana"), ("form", "choose_audio")}


def make_drill_mode(mode_id, module, group, cue, task, name, desc, **extra):
    """按「题干 cue × 作答 task」派生一个词库题型的完整定义。

    派生规则只此一份：出题（prompt_for/expected_for）、判分、前端交互（mode_ui）
    全部读派生结果，不在别处按模式 id 分叉。
    """
    cue_def, task_def = CUE_DEFS[cue], TASK_DEFS[task]
    mode = {
        "id": mode_id,
        "module": module,
        "group": group,
        "name": name,
        "desc": desc,
        "cue": cue,
        "task": task,
        "prompt_field": cue_def["field"],
        "answer_field": task_def["answer"],
        "needs_kanji": task in KANJI_ANSWER_TASKS or (cue, task) in KANJI_PROMPT_PAIRS,
        "mcq_mode": task_def["choice"],
        "image_mode": "image" in cue,          # 题干是配图：无图的词不出题
        "audio_mode": "audio" in cue,          # 题干含发音：进题自动播、可「不方便听」跳过
        "audio_options": task_def.get("audio_options", False),  # 选项是音频（见下发 option_audio）
        # 句型课含 xx 占位符，读出来没有意义：凡是带发音的题干都跳过
        "skip_phrase_lessons": "audio" in cue,
        "raw_input": task_def.get("raw_input", False),
        "exact_answer": task_def.get("exact_answer", False),
        # 只给读音写/选汉字会同音歧义（がくせい→学生/成清），题干必须补中文释义
        "prompt_with_meaning": cue == "kana" and task in ("write_kanji", "choose_form"),
        # 题干没给读音、而读音又能直接推出答案 → 作答前不能播发音（一键作弊）
        # 选发音同此：四个选项里就有一个是正确答案的发音，先播等于报答案
        "spoiler": (not cue_def["gives_reading"]
                    and task in ("write_kana", "choose_audio", "choose_form")),
    }
    mode.update(extra)
    return mode


def mode_ui(mode_def):
    """前端交互描述：题干怎么呈现、答案怎么交。

    与 make_drill_mode 同源，前端据此渲染题干（文字/图片/音频）与作答区
    （打字/四选一/词块/瓦片），不必再维护一份模式 id 白名单。
    """
    # 不下发 cue：它是后端的出题口径（哪些格子成立），前端把题干渲染成文字/图/音
    # 已由 cue 派生出的 image/audio 字段决定，多下发一份只会让人以为前端会读它。
    return {
        "task": mode_def.get("task", ""),
        "choice": bool(mode_def.get("mcq_mode")),
        "typing": not (mode_def.get("mcq_mode") or mode_def.get("order_mode")
                       or mode_def.get("pair_mode") or mode_def.get("self_assessed")
                       or mode_def.get("course_mode")),
        "image": bool(mode_def.get("image_mode")),
        "audio": bool(mode_def.get("audio_mode")),   # 进题自动播（听音题干）
        # 词块拼句（组句排序 / 例句听写）：隐藏打字框，走词块 UI
        "order": bool(mode_def.get("order_mode")),
        # 选项是音频：渲染成喇叭瓦片（点一下播一段，点击即作答）
        "audio_options": bool(mode_def.get("audio_options")),
        "spoiler": bool(mode_def.get("spoiler")),    # 作答前禁用「播放发音」
        "raw_input": bool(mode_def.get("raw_input")),
    }


# 已停用的模式：不再出现在首页，但历史练习记录里还有这些 id，
# 统计页按这里的名字显示，免得翻出一串看不懂的英文 id
RETIRED_MODES = {
    # 看图写假名：一张图对应哪个读音不唯一，凭图默写假名超出再认范围（见上方矩阵）
    "image_to_kana": "看图写假名（已停用）",
    # 看中文写假名：中文释义不唯一（同义/近义词），凭义默写读音无法判分（见上方矩阵）
    "cn_to_kana": "看中文写假名（已停用）",
}


PRACTICE_MODES = [
    make_drill_mode("kanji_to_kana", "drill", "读写输出", "form", "write_kana",
                    name="看汉字写假名", desc="显示汉字，输入假名读音"),
    make_drill_mode("form_to_audio", "drill", "读写输出", "form", "choose_audio",
                    name="看词形选发音",
                    desc="给词形，四个发音里选出它的读音（见字知音，写假名的入门版）",
                    # 选项本身就是发音：句型课的 xx 占位符念不出来，整课跳过
                    skip_phrase_lessons=True),
    make_drill_mode("kana_to_kanji", "drill", "读写输出", "kana", "write_kanji",
                    name="看假名写汉字",
                    desc="给读音和中文释义写出汉字，练汉字输出（需切日文输入法）"),
    make_drill_mode("audio_to_kana", "drill", "听力与再认", "audio", "write_kana",
                    name="听音写假名", desc="播放发音，输入假名"),
    make_drill_mode("kana_to_cn", "drill", "听力与再认", "form", "choose_cn",
                    name="看词选意思",
                    desc="给词形四选一释义（多邻国式再认，适合刚学的词）"),
    make_drill_mode("audio_to_cn", "drill", "听力与再认", "audio", "choose_cn",
                    name="听音选意思", desc="播放发音选释义，打通「耳朵→意思」直连"),
    make_drill_mode("image_to_kanji", "drill", "听力与再认", "image", "choose_form",
                    name="看图选汉字",
                    desc="看配图四选一选汉字词形（意象直连日语，不经过中文）"),
    make_drill_mode("image_to_word", "drill", "听力与再认", "image", "choose_audio",
                    name="看图选发音",
                    desc="看配图，四个发音里选出对应的词（多邻国式图片听力题）",
                    # 选项本身就是发音：句型课的 xx 占位符念不出来，整课跳过
                    skip_phrase_lessons=True),
    {
        "id": "conjugation",
        "module": "drill",
        "group": "读写输出",
        "name": "动词活用",
        "desc": "给基本形与变形名（ます/て/た/ない），先四选一再打字；假名作答也算对",
        "prompt_field": "",
        "answer_field": "",
        "needs_kanji": False,
        "conj_mode": True,   # 题干/答案由 conjugation.json 派生（每词两题：先选后打）
        "no_mastery": True,  # 拍板口径：活用对错不写回词条掌握度（独立统计）
    },
    # ---- 以下不是「题干 × 作答」矩阵的格子：交互方式本身就不同 ----
    # （翻面自评 / 片假名专项 / 例句挖空 / 词块组句 / 配对瓦片 / 混合课 / 独立题库），
    # 仍按各自标志位声明；spoiler 与矩阵题型同义：播放发音会给出答案。
    {
        "id": "flashcard",
        "module": "drill",
        "group": "听力与再认",
        "name": "闪卡认读",
        "desc": "看词听音回忆意思，翻面自评认识/不认识（适合过生词）",
        "prompt_field": "kanji",  # 无汉字的词回退显示假名（见 api_start）
        "answer_field": "hiragana",
        "needs_kanji": False,
        "skip_phrase_lessons": True,  # 句型含 xx 占位符，不适合闪卡
        "self_assessed": True,  # 不打字：翻面后自评，结果直接计入掌握度
    },
    {
        "id": "katakana_to_hiragana",
        "module": "drill",
        "group": "读写输出",
        "name": "片假名转平假名",
        "desc": "看片假名写平假名读音（外来语专项，治片假名互转与写成中文）",
        "prompt_field": "",
        "answer_field": "",
        "needs_kanji": False,
        "katakana_drill": True,  # 题干/答案由 katakana_pair 从词表派生
        "spoiler": True,         # 答案就是读音
    },
    {
        "id": "cloze_cn",
        "module": "drill",
        "group": "句子与游戏",
        "name": "例句填空",
        "desc": "例句挖掉目标词四选一填回（题干附中文释义），在语境里记词",
        "prompt_field": "",
        "answer_field": "",
        "needs_kanji": False,
        "mcq_mode": True,
        "cloze_mode": True,  # 题干/答案由例句挖空派生（见 cloze_pair）
        "spoiler": True,     # 填回的词读音就是答案
    },
    {
        "id": "order_cn",
        "module": "drill",
        "group": "句子与游戏",
        "name": "组句排序",
        "desc": "把打乱的词块点选/拖拽拼成完整例句（练日语语序，需 janome）",
        "prompt_field": "meaning",  # 题干给中文释义提示，任务在语序不在选词
        "answer_field": "",
        "needs_kanji": False,
        "order_mode": True,  # 题干词块由 sentence_blocks 从例句派生
    },
    {
        "id": "dictation",
        "module": "drill",
        "group": "句子与游戏",
        "name": "例句听写",
        "desc": "听整句发音，用词块拼出这句日语（练「音→句」直连，不经过中文）",
        "prompt_field": "",
        "answer_field": "",
        "needs_kanji": False,
        # 与组句排序共用词块 UI 与判分（整句比对），区别只在提示从中文换成音频
        "order_mode": True,
        "audio_mode": True,       # 进题自动播整句、可「不方便听」跳过
        "sentence_audio": True,   # 音频按例句文本现生成（/api/tts/text，内容哈希缓存）
        "skip_phrase_lessons": True,  # 句型课的 xx 占位符念出来没有意义
    },
    {
        "id": "pair_match",
        "module": "drill",
        "group": "句子与游戏",
        "name": "词义配对",
        "desc": "词与释义两两配对的小游戏（每轮 5 词，碎片时间练再认）",
        "prompt_field": "",
        "answer_field": "",
        "needs_kanji": False,
        "pair_mode": True,
        "streak_gate": True,  # 全提示的再认游戏，与选择题同用「连对两次才 +1」
    },
    {
        "id": "course",
        "module": "course",
        "group": "",
        "name": "课程模式",
        "desc": "多邻国式混合课：新词预习 + 旧词复习，选择题起手、组句/打字收尾，错题课末回炉",
        "prompt_field": "",
        "answer_field": "",
        "needs_kanji": False,
        "course_mode": True,  # 混合题型流程，见 start_course
    },
    {
        "id": "particle",
        "module": "bank",
        "group": "",
        "name": "助词填空",
        "desc": "句子挖空填助词，攻 N5 语法核心（独立题库，不按课程）",
        "prompt_field": "sentence",
        "answer_field": "answer",
        "needs_kanji": False,
        "particle_mode": True,  # 题目来自 particles.json，走独立分支
    },
    {
        "id": "exam",
        "module": "bank",
        "group": "",
        "name": "真题实战",
        "desc": "JLPT N5 官方真题选择题（《公式問題集》实录，独立题库）",
        "prompt_field": "stem",
        "answer_field": "options",
        "needs_kanji": False,
        "exam_mode": True,  # 题目来自 exam_n5.json，选择题交互
    },
]

# ====================================================================
# 首页方块导航
# --------------------------------------------------------------------
# 首页只呈现 5 个方块，点进去才是具体子项，避免十几个模式 + 若干工具
# 全平铺在一页上（层级混乱、找不到入口）。
#   MODULE_DEFS  五个方块的定义（顺序即展示顺序）
#   EXTRA_ITEMS  非练习模式的入口（学新词/汉字卡/录入/统计/错题本/文档）
# 练习模式自己通过 PRACTICE_MODES 里的 module / group 声明归属，
# 所以新增模式只要带上这两个字段，就会自动出现在对应方块的分组里。
# ====================================================================
MODULE_DEFS = [
    # 方块只有「图标 + 标题 + 角标」，没有 desc：原来那行说明只是把进去以后能看到
    # 的东西又复述一遍（"15 个单项训练，按读写 / 听力 / 句子分组"），在首页纯属噪声；
    # 角标里那个数（今日到期 N 词）才是点进去之前真正需要的信息。
    {
        "id": "drill", "icon": "📝", "title": "单词练习",
        "badge_key": "due_words", "badge_text": "今日到期 {} 词",
    },
    {
        "id": "course", "icon": "🎓", "title": "课程与新词",
        "badge_key": "unlearned", "badge_text": "未学 {} 词",
    },
    {
        "id": "bank", "icon": "🧩", "title": "语法与真题",
        "badge_key": "due_bank", "badge_text": "到期 {} 题",
    },
    {
        "id": "review", "icon": "📊", "title": "统计与复习",
        "badge_key": "wrong_words", "badge_text": "错题本 {} 词",
    },
    {
        "id": "immersive", "icon": "📚", "title": "作品沉浸",
        "badge_key": "works", "badge_text": "已导入 {} 部",
    },
]

# kind 决定前端点击后的走向：
#   mode      走配置页（课程/题量/排题），再 /api/start
#   learn     走配置页只选课程，再 /api/learn/start
#   kanji     走配置页只选课程，再打开汉字卡
#   add_word  直接开录入弹窗
#   rename_lesson 直接开重命名弹窗（只改课程显示名）
#   stats     跳统计页
#   wrongbook 走配置页（课程锁死为错题本，练法自选）
#   docs      打开进度/薄弱点文档
#
# 学习模块：每批新词数量（仿多邻国的新词组大小）。定义在导航表之前是有意的：
# 下面 learn 条目的文案按它生成，前端「开始学习（N 个新词）」也对同一个数，
# 换数字只改这一处。
LEARN_BATCH_DEFAULT = 7

EXTRA_ITEMS = {
    "course": [
        {
            "id": "focus", "kind": "focus", "name": "🎯 重点词（聚焦练习）",
            "desc": "圈定 10-20 个词，之后各练习模式只出这些词，反复练到熟",
            "badge_key": "focus_words",  # 角标：清单里已有多少词
        },
        {
            "id": "learn", "kind": "learn", "name": "📖 学一批新词",
            "desc": (f"取本课最靠前的 {LEARN_BATCH_DEFAULT} 个未学词："
                     "卡片教学 → 即时小测 → 标记见过（先学后练）"),
        },
        {
            "id": "kanji", "kind": "kanji", "name": "漢 汉字识字卡",
            "desc": "按汉字聚合词条，看每个字出现在哪些词里（纯浏览，不排调度不计分）",
        },
        {
            "id": "conj", "kind": "conj", "name": "🔤 动词活用卡",
            "desc": "逐词浏览动词的 ます形/て形/た形/ない形与音便说明（纯浏览，不排调度不计分）",
        },
        {
            "id": "add_word", "kind": "add_word", "name": "✍️ 新增单词",
            "desc": "不碰代码，直接把刚学到的词录入词表（可连续添加）",
        },
        {
            "id": "rename_lesson", "kind": "rename_lesson", "name": "✏️ 重命名课程",
            "desc": "改单词表的显示名（如「第1课」→「标日初级①」），只改名字，词与学习进度都不动",
        },
    ],
    "review": [
        {
            "id": "stats", "kind": "stats", "name": "📊 学习统计",
            "desc": "掌握度分布、练习趋势、各模式正确率、高频错词与混淆对",
        },
        {
            "id": "wrongbook", "kind": "wrongbook", "name": "📖 错题本练习",
            "desc": "只练历史错过/跳过的词，掌握后自动毕业移出",
        },
        {
            "id": "docs", "kind": "docs", "name": "📈 进度与薄弱点",
            "desc": "直接查看 progress.md 与 weaknesses.md（无需翻文件）",
        },
    ],
    "immersive": [
        {
            "id": "pictures", "kind": "pictures", "name": "🖼️ 图片日语（日常图片讲解）",
            "desc": "从真实日常照片里挑单词、语法与句式（一课可含多张照片）；"
                    "课程单词实时对齐词库，生词一键进生词本",
            "badge_key": "picture_lessons",  # 角标：已导入多少课
            "badge_text": "{} 课",
        },
        {
            "id": "import", "kind": "import", "name": "➕ 导入作品",
            "desc": "轻小说 txt / epub 切成句子流；音声作品 zip（音轨 + 字幕/台本）自动配对导入，"
                    "没字幕还能用 ASR 补时间轴；作品名相同则覆盖更新（音轨不入 git，只登记路径）",
        },
        {
            "id": "reader", "kind": "reader", "name": "📖 作品阅读器",
            "desc": "读已导入的作品：已学词按掌握度标注，含已学词的句子带角标；"
                    "点生词可加入生词本",
        },
    ],
}

# 掌握度更新规则（可按需调整）
MASTERY_MIN = 0
MASTERY_MAX = 5
WEAK_THRESHOLD = 2  # focus_weak 时掌握度 <= 此值视为薄弱

# 词库练习的题量上限：UI 的「题目数量」框最大 100（app.js 的 COUNT_FIELD），
# 这里放宽到 500 兜住脚本调用，同时挡住畸形请求——重点词模式会按题量循环补齐
# （_cycle_fill），题量填成 1e9 就是几亿道题，请求永不返回。
# 独立题库（助词/真题）不设上限：题库本身有限，切片天然收敛，且真题要能一次出满。
DRILL_COUNT_MAX = 500

# 间隔重复：各掌握度对应的复习间隔（天），用于「答对后」安排下次复习；
# 答错则固定明天到期（next_due 由 api_finish 写入）
INTERVAL_DAYS = {0: 1, 1: 2, 2: 4, 3: 7, 4: 14, 5: 30}

# 学习记录在统计页的模式显示名（learn 不是练习模式，不在 PRACTICE_MODES 里）
LEARN_MODE_NAME = "新词学习"


def effective_due(word):
    """这个词真正该到期的日期，是 is_due 与「到期优先」排序共用的唯一口径。

    优先读显式的 next_due（api_finish 写入：答对按新掌握度间隔、答错固定明天）；
    无 next_due 或日期读不出来的旧数据按 last_reviewed + 掌握度间隔推算；
    从未复习过的词返回 date.min（视为立即到期）。

    排序不能用 last_reviewed：答对答错都会把它刷成今天（见 _apply_answer），
    按它排就退化成「最久没碰的优先」，跟到期与否脱钩，同一天练过的词还会
    一起沉到队列尾部。
    """
    next_due = word.get("next_due")
    if next_due:
        try:
            return datetime.strptime(next_due, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            pass
    last = word.get("last_reviewed")
    if not last:
        return date.min
    try:
        last_date = datetime.strptime(last, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return date.min
    mastery = int(word.get("mastery") or 0)
    return last_date + timedelta(days=INTERVAL_DAYS.get(mastery, 1))


def is_due(word, today=None):
    """判断单词是否到期（需要复习）。从未复习过的词视为到期。"""
    if today is None:
        today = datetime.now().date()
    return today >= effective_due(word)


def is_learned(word):
    """这个词是否「练过」（有过一次作答记录）。

    与 is_due 是两条互相独立的轴，别混：
    - is_due 回答「现在该不该复习」，且把从没练过的词也算到期（新词本就该学）；
    - is_learned 回答「碰没碰过」，看有没有作答痕迹。
    出题时两轴叉乘，正好把「练过的（该复习）」与「没练过的（该学）」分成
    两个互不相交的池子——此前只有一个到期池，未练过的词混在里面，
    抽出来的多是没学过的词，那是在考、不是在学（见 POOL_KINDS）。
    """
    # last_reviewed 是标准痕迹（api_finish / api_learn_finish 都会写）；
    # 手工改过或早期版本的数据可能只有 next_due 或 mastery，一并认作练过，
    # 否则这些词会同时被算进「未学」与「待复习」两个池子
    if word.get("last_reviewed") or word.get("next_due"):
        return True
    return int(word.get("mastery") or 0) > 0


def mastery_level(w):
    """词条掌握度（0-5），供前端给卡片着色标注。

    缺失 / None / 非法值一律按 0（未学）——词表里手改坏的数据不该让接口崩，
    各处读取统一走这里，口径与 is_due、api_stats 的 `or 0` 一致。
    注意这是「作答前」的档位：掌握度在 /api/finish 才落盘。
    """
    try:
        m = int(w.get("mastery") or 0)
    except (TypeError, ValueError):
        return 0
    return max(MASTERY_MIN, min(MASTERY_MAX, m))


# ---------- 假名规范化 ----------
# 片假名 -> 平假名 的映射表（U+30A1..U+30F6 -> U+3041..U+3096）
KATA_TO_HIRA = {c: c - 0x60 for c in range(0x30A1, 0x30F7)}
KATAKANA_RE = re.compile(r"[ァ-ヴヶ]")


def has_katakana(s):
    return bool(KATAKANA_RE.search(s or ""))


CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def has_kanji(s):
    """写法里是否含真正的汉字（排除 "---"、纯假名与纯片假名写法）。"""
    return bool(CJK_RE.search(s or ""))


def normalize_kana(s):
    """规范化假名输入：NFKC 归一、片假名转平假名、拉丁字母转小写、去除所有空白。

    NFKC 这步不能省：输入法切在半角模式打出的 ｶﾝｼ、以及从 PDF/词表
    粘贴来的「か + 组合用浊点」都不在片假名映射范围内，会整题误判。
    小写化是因为前端 kana.js 对整串输入 toLowerCase（罗马字转换需要），
    否则读音含拉丁字母的词（Tシャツ）被转成 tシャツ 后永远判错。
    """
    if not s:
        return ""
    return _strip_space(
        unicodedata.normalize("NFKC", s).translate(KATA_TO_HIRA)).lower()


def normalize_literal(s):
    """NFKC 归一 + 去空白，但**不**折叠字形。

    汉字书写题要用：把片假名折成平假名会让「只打假名没换汉字」的作答被判对。
    """
    return _strip_space(unicodedata.normalize("NFKC", s or ""))


def _strip_space(s):
    return re.sub(r"\s+", "", s).strip()


def to_hiragana(s):
    """片假名写法 → 平假名写法。长音符号「ー」原样保留（与词表读音字段口径一致）。"""
    return unicodedata.normalize("NFKC", s or "").translate(KATA_TO_HIRA)


# 每行假名对应的元音，用于识别「あ+あ / お+う」这类长音写法
_VOWEL_ROWS = (
    "あいうえお", "かきくけこ", "さしすせそ", "たちつてと", "なにぬねの",
    "はひふへほ", "まみむめも", "らりるれろ", "がぎぐげご", "ざじずぜぞ",
    "だぢづでど", "ばびぶべぼ", "ぱぴぷぺぽ",
)
_KANA_VOWEL = {}
for _row in _VOWEL_ROWS:
    for _i, _ch in enumerate(_row):
        _KANA_VOWEL[_ch] = "あいうえお"[_i]
for _ch, _v in (("や", "あ"), ("ゆ", "う"), ("よ", "お"), ("わ", "あ")):
    _KANA_VOWEL[_ch] = _v
# 各元音后面哪个假名是「长音标记」而非独立一拍（え段可写 え/い，お段可写 う/お）
_LONG_FOLLOW = {"あ": "あ", "い": "い", "う": "う", "え": "いえ", "お": "うお"}


def _devoice(s):
    """去掉浊点/半浊点：が→か、ぱ→は。"""
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if not unicodedata.combining(c))


def _drop_sokuon(s):
    """去掉促音「っ」。"""
    return s.replace("っ", "")


def _collapse_long(s):
    """抹平长音的各种写法：タクシー/たくしー、こう/こー 都归到同一形式。"""
    s = s.replace("ー", "")
    out = []
    pending_vowel = None
    for ch in s:
        if pending_vowel and ch in _LONG_FOLLOW[pending_vowel]:
            pending_vowel = None
            continue  # 这一拍只是前一个假名的长音
        out.append(ch)
        pending_vowel = _KANA_VOWEL.get(ch)
    return "".join(out)


# 可叠加的规范化：每类把一种假名写法差异抹平
_MISMATCH_OPS = (
    ("浊点", _devoice),
    ("促音", _drop_sokuon),
    ("长音", _collapse_long),
)


def classify_mismatch(user_input, expected):
    """答错时判断差异是否只出在假名写法上，返回能解释差异的最小类别名集合。

    枚举类别组合、取最小的一组：只对两边同时做规范化就把该类记为错因是错的
    ——「がこー」与「がっこう」浊点其实写对了，差异只在促音加长音。
    返回空列表表示差异不止写法（真写错了词形）或无法判定。
    仅作反馈提示：这类差异仍判错（长音/促音会改变词义，如 おばさん 与
    おばあさん），不能因为"只差一点"就算对。
    """
    u = normalize_kana(user_input)
    e = normalize_kana(expected)
    if not u or u == e:
        return []
    for size in range(1, len(_MISMATCH_OPS) + 1):
        for combo in combinations(_MISMATCH_OPS, size):
            fu, fe = u, e
            for _, op in combo:
                fu, fe = op(fu), op(fe)
            if fu == fe:
                return [label for label, _ in combo]
    return []


def check_answer(user_input, expected, alternates=None, fold_kana=True):
    """比对用户输入与标准答案。支持备选答案。

    fold_kana=False 用于答案不是假名的模式（写汉字）：不折叠片假名。
    长音「ー」不做任何宽容——少写/多写一拍就是错（パーカ ≠ パーカー，
    おばさん ≠ おばあさん 同理），这类差异由 classify_mismatch 给出错因提示。
    """
    norm = normalize_kana if fold_kana else normalize_literal
    u = norm(user_input)
    if not u:
        return False
    # 主答案 + 备选答案列表
    candidates = [expected]
    if alternates:
        candidates.extend(alternates)
    return any(u == norm(e) for e in candidates)


# ---------- 词汇读写 ----------
def ensure_word_ids(vocab):
    """确保每个单词都有稳定 id（w + 三位序号，课内唯一，只补缺失、不重排）。

    练习记录与音频缓存都按这个 id 引用单词，因此在词表中间插入或重排
    单词不会使历史记录与音频错位。手动新增单词时不必手写 id，
    load_vocab 会自动补上。
    """
    for ldata in vocab.get("lessons", {}).values():
        words = ldata.get("words", [])
        used = set()
        for w in words:
            m = re.fullmatch(r"w(\d+)", str(w.get("id") or ""))
            if m:
                used.add(int(m.group(1)))
        nxt = 1
        for w in words:
            if w.get("id"):
                continue
            while nxt in used:
                nxt += 1
            used.add(nxt)
            reordered = {"id": f"w{nxt:03d}"}
            reordered.update(w)
            w.clear()
            w.update(reordered)
    return vocab


def _atomic_write(path, text):
    """进度文件落盘的唯一入口（实现见 works.write_text_atomic）。

    这里以前自己是一份、works.py 里还有第二份，两份会漂——works 那版的临时文件名
    只带 pid，同进程里并发的导入线程（启动时 restore_tasks 一次起 N 个）共用同一个
    tmp，会把半截 JSON 原子替换进正式路径。合并成一份，顺带补上 fsync。
    """
    works.write_text_atomic(path, text)


# 三个题库文件存着掌握度与复习调度，是不可再生的学习进度：每次覆写前先留一份
# 当日快照。git 是唯一的离盘副本，而它只在手动 commit 时更新。
BACKUP_DIR = BASE_DIR / "data" / "backups"
BACKUP_KEEP = 7


def _backup_daily(path):
    """覆写前留一份「今天动手之前」的状态，每天一份，只保留最近 BACKUP_KEEP 份。

    原子写只防「半截文件」，防不住误删与逻辑 bug 把整表写坏——那种情况下文件是
    「完整而错误的」，回滚要靠快照。快照在**同一块盘**上，真丢盘仍得靠 git。
    失败只出声不阻断保存：宁可少一份快照，也不能让交卷失败。
    """
    try:
        dst = BACKUP_DIR / f"{path.stem}.bak_{datetime.now():%Y-%m-%d}.json"
        if dst.exists() or not path.exists():
            return
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
        for stale in sorted(BACKUP_DIR.glob(f"{path.stem}.bak_*.json"),
                            reverse=True)[BACKUP_KEEP:]:
            stale.unlink(missing_ok=True)
    except OSError as e:
        print(f"[backup] {path.name} 每日快照失败：{e}")


def _zip_name(info):
    """zip 条目名 → 落盘路径用的文件名。

    Python zipfile 对没有 UTF-8 标志位的压缩包按 cp437 解码文件名——而日文
    压缩包（DLsite 老作品常见）实际是 Shift-JIS 字节，解出来是一串乱码，
    音轨/字幕/台本的主名就对不上，配对全挂。把 cp437 乱码按字节还原回
    SJIS 再解码；纯 ASCII / 已正确 UTF-8 解码的名字还原失败就保持原样。
    """
    if info.flag_bits & 0x800:      # UTF-8 标志位：文件名已是正确解码
        return info.filename
    try:
        return info.filename.encode("cp437").decode("shift_jis")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _dump_json(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _safe_rel_path(rel):
    """webkitRelativePath / 相对路径 → 安全的落盘相对路径。

    归一为 /、去前导、剔除 .. 等越界片段；控制符替换成下划线。
    """
    rel = (rel or "").replace("\\", "/").lstrip("/")
    parts = []
    for seg in rel.split("/"):
        seg = re.sub(r"[\x00-\x1f]", "_", seg)
        if seg and seg not in (".", ".."):
            parts.append(seg)
    return "/".join(parts)


# 目录上传（webkitdirectory）的总量上限：和 zip 同档（音频本体几百 MB 起步）
DIR_UPLOAD_MAX_BYTES = 4 * 1024 * 1024 * 1024

# 整条请求的硬上限，兜底用：不设的话 10GB 的 POST 会先原样写满 werkzeug 的磁盘
# 临时文件，才轮到业务层判「太大」。多留 64MB 给 multipart 分隔符与表单字段。
app.config["MAX_CONTENT_LENGTH"] = DIR_UPLOAD_MAX_BYTES + 64 * 1024 * 1024


# ---------- 请求字段解析 ----------
# 客户端字段一律经这里转换：非法值回落默认值，而不是把请求抛成 500。
# 视图里裸写 int(cfg["count"]) 有两个坑：字符串/对象抛 TypeError/ValueError，
# 而 JSON 里的 1e999 会解析成 inf、int(inf) 抛的是 OverflowError。
def _int_field(value, default, lo=None, hi=None):
    """整数参数：缺失/非法/0 一律回落 default（沿用视图里 `x or default` 的老口径），
    再按 lo/hi 夹紧。"""
    try:
        n = int(default if value is None or value == "" or value == 0 else value)
    except (TypeError, ValueError, OverflowError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _num_field(value, default):
    """浮点参数：口径同 _int_field（不夹紧）。"""
    try:
        return float(default if value is None or value == "" or value == 0 else value)
    except (TypeError, ValueError, OverflowError):
        return default


@app.errorhandler(Exception)
def _unhandled_exception(err):
    """未捕获异常兜底：/api/* 一律返 JSON，页面路由维持「500 + 控制台堆栈」。

    前端每个请求都走 res.json()，视图一旦抛异常拿到 HTML 500，报出来的错会是
    「Unexpected token '<'」——与真实原因毫不相干（README 记过一次：前端提示
    「保存学习记录失败」，而数据其实已经写盘）。这里把它变成明确的 JSON 错误。
    完整堆栈仍由 app.logger.exception 打到控制台，排查不受影响。
    """
    if isinstance(err, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": err.description}), err.code or 500
        return err.get_response()
    app.logger.exception("未处理的异常：%s %s", request.method, request.path)
    if request.path.startswith("/api/"):
        return jsonify({"error": f"服务器内部错误（{type(err).__name__}），详见服务端控制台"}), 500
    return InternalServerError().get_response()


@app.errorhandler(413)
def _request_too_large(err):
    """超限请求返 JSON：导入页读的是 {error}，返回一屏 HTML 只会显示「请求失败」。"""
    limit = app.config["MAX_CONTENT_LENGTH"] // 1024 // 1024
    return jsonify({"error": f"上传内容过大（整次请求上限 {limit}MB）"}), 413


def _fs_nbytes(fs):
    """FileStorage 的字节数：先问 content_length，没有就 seek 探尾（不搬数据）。

    content_length 只在 >0 时可信：werkzeug 对这个属性写的是
    `int(self.headers.get("Content-Length", 0))`，而 multipart 的分片通常根本
    没有 Content-Length 头 → 读出 0。按「非 None 就采用」会把每个上传都当成空文件。
    返回 None 表示两头都问不到（流不可 seek），调用方按未知大小处理。
    """
    n = getattr(fs, "content_length", None)
    if n:
        return int(n)
    stream = getattr(fs, "stream", None)
    if stream is None:
        return None
    try:
        cur = stream.tell()
        end = stream.seek(0, os.SEEK_END)
        stream.seek(cur)
        return end
    except (OSError, AttributeError, io.UnsupportedOperation):
        return None


def _materialize_dir_upload(file_specs, stage, max_bytes=DIR_UPLOAD_MAX_BYTES):
    """目录上传的 (相对路径, 文件流) 列表 → 流式落盘到 stage。

    双重校验相对路径（_safe_rel_path + resolve 后 relative_to(stage)），
    累计超 max_bytes 抛错。返回写入总字节数。
    """
    import shutil

    stage = Path(stage)
    stage.mkdir(parents=True, exist_ok=True)
    total = 0
    for rel, stream in file_specs:
        safe = _safe_rel_path(rel or "")
        if not safe:
            raise ValueError("空路径")
        dst = (stage / safe).resolve()
        try:
            dst.relative_to(stage.resolve())
        except ValueError:
            raise ValueError(f"越界路径：{rel!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src = stream.stream if hasattr(stream, "stream") else stream  # FileStorage
        with open(dst, "wb") as fp:
            shutil.copyfileobj(src, fp, 1 << 20)
            total += fp.tell()
        if total > max_bytes:
            raise ValueError(f"文件夹太大（上限 {max_bytes // 1048576}MB）")
    return total


def load_particles():
    with open(PARTICLES_PATH, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_particles(data):
    _backup_daily(PARTICLES_PATH)
    _atomic_write(PARTICLES_PATH, _dump_json(data))


def load_exam():
    with open(EXAM_PATH, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_exam(data):
    _backup_daily(EXAM_PATH)
    _atomic_write(EXAM_PATH, _dump_json(data))


def ensure_vocab_file(dst=None, seed=None):
    """起始版（不带 vocabulary.json）第一次用到词表时，从 data/seed/ 自举一份零状态词表。

    抽成独立函数、并在 load_vocab 里调用，是为了让**所有**读词表的入口都自愈
    （测试里也有直接读该文件的用例）；私人仓库里这个词表本来就在，不会触发。
    两个参数只在测试里显式传（那时 VOCAB_PATH / BASE_DIR 已被指到临时目录）。
    返回是否真的自举了。
    """
    dst = Path(dst) if dst else VOCAB_PATH
    seed = Path(seed) if seed else VOCAB_SEED_PATH
    if dst.exists() or not seed.exists():
        return False
    seed_text = seed.read_text(encoding="utf-8")
    stage = dst.with_suffix(".json.tmp")
    stage.write_text(seed_text, encoding="utf-8")
    stage.replace(dst)                     # 原子替换，避免半个文件被读到
    print(f"[词表] 首次运行：已由 {seed.name} 生成 {dst.name}"
          f"（{len(json.loads(seed_text).get('lessons', {}))} 课，掌握度全 0；"
          "练习记录写在 quizzes/）")
    return True


def load_vocab():
    ensure_vocab_file()
    # 用 utf-8-sig 读取，兼容带 BOM 的文件
    with open(VOCAB_PATH, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    ensure_word_ids(data)
    return data


def _format_word(w):
    """将单个单词对象压缩为一行，保留键的原始顺序。"""
    parts = [f"{json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}"
             for k, v in w.items()]
    return "{" + ", ".join(parts) + "}"


def _format_vocab(data):
    """格式化整个词汇表：结构按缩进展开，单词对象每行一个（便于手动编辑与 diff）。"""
    lines = ["{"]
    top_keys = list(data.keys())
    for i, k in enumerate(top_keys):
        comma = "," if i < len(top_keys) - 1 else ""
        if k == "lessons":
            lines.append('  "lessons": {')
            lessons = data[k]
            lkeys = list(lessons.keys())
            for li, lid in enumerate(lkeys):
                lcomma = "," if li < len(lkeys) - 1 else ""
                ldata = lessons[lid]
                lines.append(f'    "{lid}": {{')
                inner_keys = list(ldata.keys())
                for ii, ik in enumerate(inner_keys):
                    icomma = "," if ii < len(inner_keys) - 1 else ""
                    if ik == "words":
                        words = ldata[ik]
                        lines.append('      "words": [')
                        for wi, w in enumerate(words):
                            wcomma = "," if wi < len(words) - 1 else ""
                            lines.append("        " + _format_word(w) + wcomma)
                        lines.append("      ]" + icomma)
                    else:
                        v = json.dumps(ldata[ik], ensure_ascii=False)
                        lines.append(f'      "{ik}": {v}' + icomma)
                lines.append("    }" + lcomma)
            lines.append("  }" + comma)
        else:
            # meta 等顶层对象：多行展开，整体缩进 2 空格
            block = json.dumps(data[k], ensure_ascii=False, indent=2)
            block = block.replace("\n", "\n  ")
            lines.append(f'  "{k}": {block}' + comma)
    lines.append("}")
    return "\n".join(lines) + "\n"


def save_vocab(data):
    """以「单词每行一个」的紧凑格式原子写入 vocabulary.json（覆写前先留当日快照）。"""
    _backup_daily(VOCAB_PATH)
    _atomic_write(VOCAB_PATH, _format_vocab(data))


def get_mode(mode_id):
    return next((m for m in PRACTICE_MODES if m["id"] == mode_id), None)


def katakana_pair(word):
    """返回 (片假名题干, 平假名答案)；词条不适合片假名专项则返回 None。

    外来语的片假名写法在词表里有两个落点：有"写法"的记在 kanji
    （アメリカ），纯假名词的 kanji 是 "---"、写法实际记在 hiragana
    （ゲーム），两处都要认。但写法混了汉字的（ゴミ箱、バス停）要排除：
    那种题考的是汉字怎么读，不是片假名互转。
    """
    kanji = word.get("kanji") or ""
    hira = word.get("hiragana") or ""
    form = hira if has_katakana(hira) else (kanji if has_katakana(kanji) else "")
    if not form or has_kanji(form):
        return None
    answer = hira if not has_katakana(hira) else to_hiragana(hira)
    return (form, answer) if form != answer else None


def vocab_image_url(lesson_id, word_id):
    """词条配图的静态 URL；没有配图文件返回 None。

    配图模式的出题资格、题目与反馈里的图片字段都以这一判断为准，
    补图或删图即时生效，无需另维护清单。
    """
    if not lesson_id or not word_id:
        return None
    if not (VOCAB_IMAGE_DIR / f"{lesson_id}_{word_id}.png").exists():
        return None
    return f"/static/images/vocab/{lesson_id}_{word_id}.png"


def mode_accepts(word, mode_def, lesson_id=None, conj_dict_form=False):
    """该词条能否出成这个模式的题（出题与错题本共用同一套筛选）。

    配图模式要靠 lesson_id 定位图片文件，调用方必须带上。
    各条件是与关系（配图 + 需要汉字的题型要同时满足，缺图或纯假名词都不出题）。
    conj_dict_form：活用题在课程新词轮换里只认写法即基本形的词条——
    预习卡展示的是词条写法，刚看过「起きます」就考「起きる的て形」超纲了。
    """
    if mode_def.get("image_mode") and vocab_image_url(lesson_id, word.get("id") or "") is None:
        return False
    if mode_def.get("audio_options") and not speakable_reading(word):
        # 选项是一段发音：念不出来的词（空读音、句型课 xx 占位符）不能出题
        return False
    if mode_def.get("katakana_drill"):
        return katakana_pair(word) is not None
    if mode_def.get("cloze_mode"):
        return cloze_pair(word) is not None
    if mode_def.get("order_mode"):
        return sentence_blocks(word.get("example_ja", "")) is not None
    if mode_def.get("conj_mode"):
        if not lesson_id:
            return False
        entry = conj_index().get(f"{lesson_id}:{word.get('id', '')}")
        if not entry:
            return False
        return (not conj_dict_form) or entry.get("written") == entry.get("basic")
    if mode_def["needs_kanji"]:
        # 纯假名、"---" 与纯片假名写法都不算汉字题（片假名交给专项模式）
        return has_kanji(word.get("kanji", ""))
    if (mode_def.get("mcq_mode") and not mode_def.get("image_mode")
            and not mode_def.get("audio_mode")):
        # 题干文字与正确选项一模一样（猫 的释义就是「猫」、纯假名词的词形就是它的
        # 读音）→ 认不认识都能对，是送分题；这类词条交给别的格子出。
        cue_text = prompt_for(word, mode_def)
        return not cue_text or cue_text != expected_for(word, mode_def)
    return True


# 组句词块的 janome 分词器（惰性初始化；未安装时组句题自动不可用）
_JANOME_TOK = None


def _get_janome_tok():
    """janome 分词器（惰性初始化，未安装/初始化失败返回 None）。"""
    global _JANOME_TOK
    try:
        from janome.tokenizer import Tokenizer
    except ImportError:
        return None
    if _JANOME_TOK is None:
        try:
            _JANOME_TOK = Tokenizer()
        except Exception:
            return None
    return _JANOME_TOK


def _janome_reading(text):
    """janome 词典读音（片假名串）；分词器不可用或异常返回 None。

    用于 TTS 文本校验：汉字形式经词典读音与读音字段一致才可放心交给 TTS。
    """
    tok = _get_janome_tok()
    if tok is None:
        return None
    try:
        parts = []
        for t in tok.tokenize(text):
            # 未知词的 reading 为 "*"：回落表层的假名（含汉字时校验必然失败→回落，安全）
            parts.append(t.reading if t.reading and t.reading != "*" else t.surface)
        return "".join(parts)
    except Exception:
        return None


def sentence_blocks(text, max_blocks=6):
    """例句 → 组句词块列表（janome 分词 + 相邻短块合并）；不适合组句返回 None。

    词块数控制在 3..max_blocks：token 太多时反复合并「最短的相邻对」，
    保持块长均匀。句子超过 14 个 token 或分词不可用（janome 未安装）都
    返回 None，调用方自动降级（课程回退打字题）。
    """
    if not text or len(text) > 60:
        return None
    tok = _get_janome_tok()
    if tok is None:
        return None
    try:
        tokens = [t.surface for t in tok.tokenize(text)]
    except Exception:
        return None
    tokens = [t for t in tokens if t.strip()]
    # 句读标点不单独成块（。、！？），并入前一个块——单独的「。」块没有语序意义
    merged = []
    for t in tokens:
        if merged and re.fullmatch(r"[。、！？!?,.\u3001\u3002]+", t):
            merged[-1] += t
        else:
            merged.append(t)
    tokens = merged
    if not (3 <= len(tokens) <= 14):
        return None
    while len(tokens) > max_blocks:
        idx = min(range(len(tokens) - 1),
                  key=lambda i: len(tokens[i]) + len(tokens[i + 1]))
        tokens[idx:idx + 2] = [tokens[idx] + tokens[idx + 1]]
    if len(tokens) < 3:
        return None
    return tokens


def _shuffled_blocks(blocks):
    """打乱词块顺序，保证不与原序相同（组句题不能开局即正确）。"""
    out = list(blocks)
    for _ in range(5):
        random.shuffle(out)
        if out != blocks:
            return out
    if len(blocks) > 1:
        out = blocks[1:] + blocks[:1]  # 兜底：旋转一位
    return out


def cloze_pair(word):
    """例句挖空：返回 (挖空句, 正确答案词形)；词条不适合则返回 None。

    只认词条原形在例句中的**完整出现**（先汉字写法、再假名读音）：
    活用变形的句子（読む → 読みます）不挖——把词尾留在空外会教错形态，
    而挖「よ」这种残缺片段更是无意义。实测全词库命中率近九成。
    """
    ex = word.get("example_ja") or ""
    if not ex:
        return None
    kanji = word.get("kanji") or ""
    hira = word.get("hiragana") or ""
    if kanji and kanji != "---" and kanji in ex:
        return ex.replace(kanji, "＿", 1), kanji
    if hira and hira in ex:
        return ex.replace(hira, "＿", 1), hira
    return None


def word_form(word):
    """词条的词形：有汉字取汉字写法，纯假名词（kanji 为 "---"）回退读音。

    答案是「词形」的题型（写汉字 / 选汉字 / 例句填空）统一用这个口径取选项与答案，
    否则纯假名词会拿 "---" 当选项。
    """
    kanji = (word.get("kanji") or "").strip()
    if kanji and kanji != "---":
        return kanji
    return word.get("hiragana") or ""


def prompt_for(word, mode_def):
    """题干文本。配图模式的题干是图片，文字留空（URL 由 api_start/start_course 记入）。"""
    if mode_def.get("image_mode"):
        return ""
    if mode_def.get("katakana_drill"):
        pair = katakana_pair(word)
        return pair[0] if pair else ""
    if mode_def.get("cloze_mode"):
        pair = cloze_pair(word)
        if not pair:
            return ""
        # 空位不唯一决定词形：「あの方は私の ＿ です。」填学生/先生/日本人都通，
        # 必须给出释义，题目才有唯一答案（与组句题靠中文提示同理）。
        meaning = word.get("meaning") or ""
        return f"{pair[0]}（{meaning}）" if meaning else pair[0]
    prompt = word.get(mode_def["prompt_field"], "")
    # 无汉字的词（kanji 为 "---"）回退显示假名
    if prompt in ("", "---", None):
        prompt = word.get("hiragana", "")
    if mode_def.get("prompt_with_meaning") and prompt:
        prompt = f"{prompt}（{word.get('meaning', '')}）"
    return prompt


def expected_for(word, mode_def):
    """期望答案原值（比对由 check_answer 负责）。"""
    if mode_def.get("katakana_drill"):
        pair = katakana_pair(word)
        return pair[1] if pair else ""
    if mode_def.get("cloze_mode"):
        pair = cloze_pair(word)
        return pair[1] if pair else ""
    if mode_def.get("answer_field") == "kanji":
        return word_form(word)
    return word.get(mode_def["answer_field"], "")


def gloss_senses(meaning):
    """中文释义拆成义项集合，用于判断两个候选是否同义。

    片段只收 2 字以上的：单字（「周」「书」）会把「这周/下周」这类
    该保留的易混词误判成同义。整条释义原文也收进集合，
    保证「完全相同的释义」一定判为重叠（单字释义如「人」拆不出片段）。
    """
    m = (meaning or "").strip()
    if not m:
        return set()
    return {s for s in re.split(r"[，、,;；。/（）()\s]+", m) if len(s) >= 2} | {m}


def _mcq_pool(words, mode_def):
    """选择题干扰项池：[(选项文本, 该词释义义项集)]，按文本去重、剔除空值。

    选项口径跟着答案走（见 answer_field 派生规则）：答案是词形就取词形
    （纯假名词回退读音），答案是读音取读音，答案是释义取释义。
    """
    out, seen = [], set()
    for w in words:
        if mode_def.get("cloze_mode") or mode_def.get("answer_field") == "kanji":
            text = word_form(w)
        else:
            text = (w.get(mode_def["answer_field"]) or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append((text, gloss_senses(w.get("meaning", ""))))
    return out


def _build_mcq_options(word, mode_def, pool):
    """正确答案 + 3 个干扰项，顺序打乱；池子不足时能凑几个是几个。

    释义义项与正确答案重叠的候选一律剔除——那意味着它对上题干也是对的，
    四选一就成了两个正确项（词库里的真同义词不少：晩ご飯 / 夜ご飯 都释「晚饭」，
    例句填空里同一词的汉字与假名两种写法也会重复出现）。
    """
    answer = expected_for(word, mode_def)
    own = gloss_senses(word.get("meaning", ""))
    opts = [answer]
    # 池子按词表顺序构建（本课词序，不足时按全词库顺序补底），若按顺序取前 3 个
    # 可用候选，「课程最开头的几个词」就成了几乎每题都在的常驻干扰项
    # （第 1 课开头的 中国人/日本人/学生 正是这么混脸熟的）。每题对池子副本洗牌
    # 再抽：池子构成不变，只是抽样随机化。
    candidates = list(pool)
    random.shuffle(candidates)
    for text, senses in candidates:
        if len(opts) >= 4:
            break
        if text == answer or text in opts or (own & senses):
            continue
        opts.append(text)
    random.shuffle(opts)
    return opts


def _mcq_pool_scoped(words, mode_def, wider, min_size=8):
    """干扰项池：优先取题目范围内的词，不足 min_size 个时从更大的词表 wider 里补。

    同义剔除可能把小课程（25 词）的候选筛到不足 3 个，补池保证四选一凑得满。
    """
    pool = _mcq_pool(words, mode_def)
    if len(pool) >= min_size:
        return pool
    have = {t for t, _ in pool}
    return pool + [(t, s) for t, s in _mcq_pool(wider, mode_def) if t not in have]


# ---------- 选项即发音 ----------
# 题干给图、四个选项各是一段发音时，选项文本（读音）只是判分用的标识，
# 屏幕上显示的是喇叭；音频按 ref 取（朗读文本未必等于读音字段，见 tts_word_text），
# 所以要先建「读音 → ref」的查找表。
def speakable_reading(word):
    """该词的读音能否交给 TTS 念出来（选项音频与听音题干都靠它）。"""
    text = (word.get("hiragana") or "").strip()
    return bool(text) and "xx" not in text.lower()


def tts_url_for_ref(ref):
    """词发音的 TTS 地址；ref 为空给空串（前端按空串跳过播放，不出无声题）。"""
    return f"/api/tts?ref={quote(str(ref), safe=':')}" if ref else ""


def tts_url_for_text(text):
    """整句朗读的 TTS 地址（例句听写用）：按内容哈希缓存，同一句只生成一次。"""
    return f"/api/tts/text?text={quote(str(text or ''), safe='')}" if text else ""


def audio_option_words(vocab):
    """能当发音选项的词：读音念得出来，且不在句型课里。

    句型课的读音是残句（「xxを飲みます」→ をのみます），念出来没有意义，
    连干扰项都不该出；这与题干侧 skip_phrase_lessons 是同一件事的两头。
    """
    out = []
    for lid, ldata in vocab.get("lessons", {}).items():
        if lid.endswith("_phrases"):
            continue
        out.extend(w for w in ldata.get("words", []) if speakable_reading(w))
    return out


def audio_ref_map(vocab):
    """读音 → ref 映射，供选项音频查表（同音词任取其一：念的是同一串假名）。"""
    out = {}
    for lid, ldata in vocab.get("lessons", {}).items():
        for w in ldata.get("words", []):
            if not speakable_reading(w):
                continue
            out.setdefault((w.get("hiragana") or "").strip(), f"{lid}:{w.get('id')}")
    return out


def audio_option_urls(options, answer_text, answer_ref, ref_map):
    """与 options 一一对应的音频地址列表（选项是发音的题型用）。

    正确项用本题的 ref（保证与「播放发音」听到的是同一份音频），
    干扰项按读音查表；查不到给空串，前端按无声选项处理。
    """
    return [tts_url_for_ref(answer_ref if t == answer_text else ref_map.get(t, ""))
            for t in options]


def gather_candidates(vocab, lesson_id, mode_def, focus_weak):
    """按课程范围与模式规则收集候选单词。"""
    candidates = []
    lessons = vocab.get("lessons", {})
    target = [lesson_id] if lesson_id != "all" else list(lessons.keys())
    for lid in target:
        if lid not in lessons:
            continue
        # 听音模式跳过句型课程（含 xx 占位符，处理后残余助词不适合听音）
        if mode_def.get("skip_phrase_lessons") and lid.endswith("_phrases"):
            continue
        for w in lessons[lid].get("words", []):
            if not mode_accepts(w, mode_def, lid):
                continue
            if focus_weak and int(w.get("mastery") or 0) > WEAK_THRESHOLD:
                continue
            candidates.append({"lesson_id": lid, "word": w})
    return candidates


def parse_ref(ref):
    """解析 'lesson_01:w007' -> (lesson_id, word_id)，失败返回 None。"""
    try:
        lesson_id, wid = ref.split(":", 1)
        if lesson_id and wid:
            return lesson_id, wid
        return None
    except (ValueError, AttributeError):
        return None


def resolve_word(vocab, ref):
    """按 ref（lesson:id）定位单词，返回 (lesson_id, word) 或 None。

    id 为纯数字时兼容迁移前的旧下标引用（历史记录 / 未刷新的旧会话）。
    """
    parsed = parse_ref(ref)
    if not parsed:
        return None
    lesson_id, wid = parsed
    words = vocab.get("lessons", {}).get(lesson_id, {}).get("words", [])
    if not words:
        return None
    if wid.isdigit():
        idx = int(wid)
        if 0 <= idx < len(words):
            return lesson_id, words[idx]
        return None
    for w in words:
        if w.get("id") == wid:
            return lesson_id, w
    return None


# ---------- 重点词（聚焦练习） ----------
# 词库上千词时按「到期优先」练，很多词练一次要等十几天才再见——印象还没建立
# 就凉了。重点词是用户自己圈的一小撮（建议 10-20 个）：启用后所有词库模式只出
# 这些词，题量大于清单时循环补齐（同一词反复出现），与课程范围互斥。
# 存独立文件、不动 vocabulary.json：清单只是「练什么」的选择，不是学习进度，
# 删掉不影响掌握度与复习计划。
def load_focus():
    """读取重点词清单。文件缺失/损坏/格式不对一律按空清单处理——
    清单是辅助数据，不该让任何接口（尤其首页与配置页）跟着崩。"""
    empty = {"refs": [], "enabled": False, "updated": ""}
    if not FOCUS_PATH.exists():
        return empty
    try:
        with open(FOCUS_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return empty
    if not isinstance(data, dict):
        return empty
    refs, seen = [], set()
    for r in (data.get("refs") or []):
        ref = str(r).strip()
        if ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return {"refs": refs,
            "enabled": bool(data.get("enabled", bool(refs))),
            "updated": str(data.get("updated") or "")}


def save_focus(refs, enabled):
    """原子写清单（与三个题库同一套写入保护）。"""
    data = {"refs": list(refs), "enabled": bool(enabled),
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    FOCUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(FOCUS_PATH, _dump_json(data))
    return data


def focus_word_entries(vocab=None, refs=None):
    """把 ref 清单还原成词条信息，供清单页展示。

    返回 (entries, missing)：missing 是清单里已解析不到的 ref 数（词被删或改过 id），
    交给调用方提示——静默跳过会让人以为那个词还在练。
    """
    vocab = vocab if vocab is not None else load_vocab()
    if refs is None:
        refs = load_focus()["refs"]
    entries, missing = [], 0
    for ref in refs:
        resolved = resolve_word(vocab, ref)
        if not resolved:
            missing += 1
            continue
        lid, w = resolved
        entries.append({
            "ref": ref,
            "lesson_id": lid,
            "lesson_title": vocab.get("lessons", {}).get(lid, {}).get("title", lid),
            "kanji": w.get("kanji", ""),
            "hiragana": w.get("hiragana", ""),
            "meaning": w.get("meaning", ""),
            "mastery": mastery_level(w),
        })
    return entries, missing


def focus_scope(mode_def=None, cfg=None):
    """本组练习是否限定在重点词清单内，返回 (是否限定, 有序 ref 列表)。

    独立题库（助词/真题）不按词库出题，永远不受清单影响；
    前端显式传 focus=false 可单次退出（配置页选「按课程范围」）。
    """
    if mode_def and (mode_def.get("particle_mode") or mode_def.get("exam_mode")):
        return False, []
    focus = load_focus()
    refs = focus.get("refs") or []
    enabled = bool(focus.get("enabled", True))
    if cfg is not None and cfg.get("focus") is not None:
        enabled = bool(cfg.get("focus"))
    if not (enabled and refs):
        return False, []
    return True, list(refs)


def build_focus_candidates(vocab, refs, mode_def, focus_weak):
    """按重点词清单出题：清单顺序即优先级（先挑的词先练）。

    清单跨模式共用，某模式出不了题的词（无配图的词碰上图题、无例句的碰上组句）
    在这里跳过——清页面不按模式过滤，否则换个模式就得重挑一遍。
    """
    out = []
    for ref in refs:
        resolved = resolve_word(vocab, ref)
        if not resolved:
            continue
        lid, w = resolved
        if not mode_accepts(w, mode_def, lid):
            continue
        if focus_weak and int(w.get("mastery") or 0) > WEAK_THRESHOLD:
            continue
        out.append({"lesson_id": lid, "word": w})
    return out


def _cycle_fill(selected, count):
    """题量大于候选时循环补齐（同一词出多遍）：聚焦练习要的就是重复。

    只用于重点词——普通模式候选不够就少出几题（老行为，不打扰）。
    每轮重新洗牌，避免第二遍和第一遍完全同序。
    """
    if not selected or len(selected) >= count:
        return list(selected)
    out = list(selected)
    while len(out) < count:
        nxt = list(selected)
        random.shuffle(nxt)
        out.extend(nxt[:count - len(out)])
    return out


def pick_due_first(candidates, count):
    """间隔重复：到期词优先（逾期最久靠前），不够按掌握度升序补未到期的。"""
    today = datetime.now().date()
    due_list = [c for c in candidates if is_due(c["word"], today)]
    not_due = [c for c in candidates if not is_due(c["word"], today)]
    due_list.sort(key=lambda c: effective_due(c["word"]))
    not_due.sort(key=lambda c: mastery_level(c["word"]))
    return (due_list + not_due)[:count]


# ---------- 练习池：把「该复习的」与「该学的」分开 ----------
# 起因：is_due 把从没练过的词也算到期，于是一个到期池里混着 741 个没学过的
# 与 336 个练过到期的词——打开练习抽到的多半是没学过的，全组都在猜，
# 这不是复习是考试（实测 1157 词里 64% 从未练过）。
# 现在把出题池显式分成三档，配置页选，默认只出练过的词。
POOL_REVIEW = "review"   # 只出练过的词：到期优先（逾期最久靠前），不够用未到期的补
POOL_NEW = "new"         # 只出没练过的词：按词表顺序（编排顺序即学习顺序）
POOL_ALL = "all"         # 不筛选：旧行为（到期优先，含没练过的词）
POOL_KINDS = (POOL_REVIEW, POOL_NEW, POOL_ALL)


def filter_pool(candidates, pool):
    """按练习池筛掉不属于本池的词；pool 非法或 all 时原样返回。

    只做筛选不改顺序：排题仍交给 pick_due_first（按到期/顺序）或洗牌（随机），
    这样「随机出题」选中「新词」时不会把新词筛没了又被洗没。
    """
    if pool == POOL_REVIEW:
        return [c for c in candidates if is_learned(c["word"])]
    if pool == POOL_NEW:
        return [c for c in candidates if not is_learned(c["word"])]
    return list(candidates)


def pool_error(pool):
    """池子被筛空时的提示：说清是哪一档空了、下一步该做什么。"""
    if pool == POOL_REVIEW:
        return "这个范围内还没有练过的词，先学一批新词或改成「新词 / 全部」"
    if pool == POOL_NEW:
        return "这个范围内的词都练过了，换成「到期复习」或「全部」"
    return "没有符合条件的单词，请调整课程范围或薄弱词过滤"


# ---------- 错题本 ----------
# 错题本是虚拟课程：从 quizzes/ 历史聚合所有 correct=false 的 ref，
# 按最近错误时间降序排列（最近错过的优先练）。不污染主词表结构。
WRONG_LESSON_ID = "lesson_wrong"

# 毕业条件：掌握度达到该值且最后一次错误之后答对过（含闪卡自评），
# 视为已经克服，不再进入错题本
WRONG_BOOK_GRADUATE_MASTERY = 3


def gather_wrong_refs(vocab=None):
    """扫描 quizzes/ 目录，返回错题列表。

    传入 vocab 时应用毕业规则：mastery >= WRONG_BOOK_GRADUATE_MASTERY
    且最后一次错误之后答对过的词条自动毕业，不再出现。

    返回: [{"ref": "lesson_x:w001", "last_wrong": "YYYY-MM-DD HH:MM:SS", "wrong_count": n}, ...]
    按最近错误时间降序排序。
    """
    if not QUIZZES_DIR.exists():
        return []
    # ref -> {last_wrong, wrong_count}；另记录每个 ref 最后一次答对的时间
    wrong_map = {}
    correct_map = {}
    for f in sorted(QUIZZES_DIR.glob("*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                rec = json.load(fp)
        except (json.JSONDecodeError, OSError):
            continue
        ts = rec.get("timestamp", "")
        for r in rec.get("results", []):
            ref = r.get("ref", "")
            if not ref:
                continue
            if r.get("correct", False):
                if ref not in correct_map or ts > correct_map[ref]:
                    correct_map[ref] = ts
                continue
            if ref in wrong_map:
                wrong_map[ref]["wrong_count"] += 1
                # 保留最近的时间
                if ts > wrong_map[ref]["last_wrong"]:
                    wrong_map[ref]["last_wrong"] = ts
            else:
                wrong_map[ref] = {"last_wrong": ts, "wrong_count": 1}
    # 毕业过滤；指向已不存在词条的孤儿 ref 一并剔除
    items = list(wrong_map.items())
    if vocab is not None:
        def graduated(ref, last_wrong):
            resolved = resolve_word(vocab, ref)
            if not resolved:
                return True  # 词条已不存在，无法再练
            m = int(resolved[1].get("mastery", 0) or 0)
            last_correct = correct_map.get(ref, "")
            return m >= WRONG_BOOK_GRADUATE_MASTERY and bool(last_correct) and last_correct > last_wrong
        items = [(ref, v) for ref, v in items if not graduated(ref, v["last_wrong"])]
    # 按最近错误时间降序排序
    return [
        {"ref": ref, "last_wrong": v["last_wrong"], "wrong_count": v["wrong_count"]}
        for ref, v in sorted(items, key=lambda x: x[1]["last_wrong"], reverse=True)
    ]


def build_wrong_candidates(vocab, mode_def):
    """根据错题 ref 列表构建候选（按最近错误时间排序）。"""
    wrong_refs = gather_wrong_refs(vocab)
    candidates = []
    for item in wrong_refs:
        resolved = resolve_word(vocab, item["ref"])
        if not resolved:
            continue
        lid, w = resolved
        if not mode_accepts(w, mode_def, lid):
            continue
        candidates.append({
            "lesson_id": lid,
            "word": w,
            "last_wrong": item["last_wrong"],
            "wrong_count": item["wrong_count"],
        })
    return candidates


# ---------- TTS 音频生成 ----------
# 使用 edge-tts（微软 Neural TTS）生成日语音频，按 ref 缓存到 practice/audio/
# 命名：ref 的冒号换下划线，如 lesson_duolingo_w057.mp3
TTS_VOICE = "ja-JP-NanamiNeural"  # 女声，自然亲切
TTS_RATE = "-10%"  # 稍慢，便于初学者分辨

# 个别词的朗读文本覆盖。两个相反的方向都有：
# 1. 汉字 ← 假名串：读音字段没错，但 edge-tts 念假名串别扭（購入 こうにゅう 的
#    にゅ 被拆成 に・ゆ），改用汉字让 TTS 走词典读音；
# 2. 假名 ← 汉字：词典校验通过（janome 也读 つぎ），但 TTS 按音读念成了 じ
#    （次 有 つぎ/ジ 两读，单独出现时它挑了音读）。这种情况重生成多少次都没用，
#    只能强制它念假名。
# 键与配图模块 SPECIAL_SUBJECTS 同口径（写法，纯假名/片假名词用读音）；
# 遇到新的别扭词往这里加一条，再点练习界面的「重置语音」即可生效。
TTS_TEXT_OVERRIDES = {
    "購入": "購入",  # 假名串被拆读 → 改念汉字
    "次": "つぎ",    # 汉字被念成音读 じ → 改念假名
}


_JANOME_READING_CACHE = {}  # (写法, 读音) -> 词典读音校验结果（同一写法可能配不同读音）


def tts_word_text(word):
    """单词发音的朗读文本：人工覆盖 > 汉字形式（读音经词典校验）> 读音字段。

    汉字做 TTS 文本能走词典读音——拗音不拆、音调随词（購入 こうにゅう 的
    にゅ 曾被拆成 に・ゆ）。但汉字有多音字（一日 いちにち/ついたち），所以
    先用 janome 词典读音比对：与读音字段一致才用汉字，不一致回落读音字段，
    行为与旧版完全一致；janome 未安装时全部回落，功能无损。
    """
    subject = (word.get("kanji") or "").strip()
    hira = (word.get("hiragana") or "").strip()
    if not subject or subject == "---":
        return hira
    if subject in TTS_TEXT_OVERRIDES:
        return TTS_TEXT_OVERRIDES[subject]
    if not hira:
        return subject
    cache_key = (subject, hira)
    verified = _JANOME_READING_CACHE.get(cache_key)
    if verified is None:
        reading = _janome_reading(subject)
        verified = bool(reading) and normalize_kana(reading) == normalize_kana(hira)
        _JANOME_READING_CACHE[cache_key] = verified
    return subject if verified else hira
EMOJI_RE_TTS = re.compile(
    "[\U0001F000-\U0001F9FF\U00002600-\U000027BF\U0001F0A0-\U0001F0FF"
    "\uFE0F\u200D\u20E3]+",
    flags=re.UNICODE,
)


def clean_tts_text(text):
    """清理用于 TTS 的文本：剥离 emoji，去掉 xx 占位符及其周边不自然内容。"""
    if not text:
        return ""
    # 剥离 emoji
    text = EMOJI_RE_TTS.sub("", text)
    # 句型词条：去掉 xx 占位符（如 "xxを食べます" -> "を食べます"）
    # 若去掉 xx 后剩余内容以单个助词开头（を/に/が/で/と/は/へ），去掉该助词
    # 但只针对"助词+其他内容"的情况，单独一个假名（如"に"）不处理
    if "xx" in text.lower():
        text = re.sub(r"xx\s*", "", text)
        # 去掉行首单个助词（仅当后面还有其他假名时）
        text = re.sub(r"^([をにがでとはへ])(?=\S)", "", text)
    # 去掉问号（TTS 会把问号读成疑问语气，但单词练习不需要）
    text = text.replace("？", "").replace("?", "")
    return text.strip()


def audio_path_for_ref(ref):
    """根据 ref 计算音频文件路径。

    反斜杠也要滤：Windows 上 `"..\\x"` 会被当成目录分隔符，只滤 `/` 挡不住，
    重置语音时能把 mp3 写到 practice/audio/ 外面。
    """
    safe_name = ref.replace(":", "_").replace("/", "_").replace("\\", "_")
    return AUDIO_DIR / f"{safe_name}.mp3"


# 估算音频最小文件大小的工具：按假名字符数估算时长，再换算成字节数
# edge-tts 默认输出 48kbps mp3（≈5800 B/s），Nanami 女声 + rate=-10%
# 经验值：每个假名字符约 0.12 秒发音，故每字符 ≈ 700 字节
TTS_BYTES_PER_CHAR = 700
TTS_MIN_BYTES = 1800  # 即使单字符也要这么大（mp3 头部 + 最短发音）
TTS_MAX_RETRIES = 3


def estimate_min_audio_size(text):
    """按假名字符数估算音频文件的最小字节数。"""
    char_count = max(1, len(text))
    return max(TTS_MIN_BYTES, char_count * TTS_BYTES_PER_CHAR)


def generate_audio_sync(text, out_path, max_retries=TTS_MAX_RETRIES):
    """同步包装 edge-tts 的异步生成，并自动重试截断的音频。

    edge-tts 偶发性截断音频开头/结尾，表现为文件大小明显偏小。
    通过按字符数估算的最小字节数判断是否完整：
      - 达到阈值：保留并返回
      - 未达阈值：删除重试，最多 max_retries 次
      - 重试结束仍未达阈值：**不落正式路径**，返回 False 让下次请求重生成
    返回 True 表示生成成功且完整，False 表示失败或疑似截断。

    截断的音频不能当缓存留着：ensure_audio 之后只看「存在且非空」，
    一段少了开头的发音会变成一道永远答错、还倒扣掌握度的听力题。
    """
    import asyncio
    import edge_tts

    min_size = estimate_min_audio_size(text)
    best_path = None
    best_size = 0

    async def _gen(path):
        communicate = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE)
        await communicate.save(str(path))

    for attempt in range(max_retries):
        # tmp 名带 pid+线程标识：前端并发拉同一未缓存词条时，
        # 两个生成线程不能共用同一个 tmp 文件（会互踩写出坏 mp3）
        tmp_path = out_path.with_name(
            f"{out_path.stem}.tmp{os.getpid()}_{threading.get_ident()}_{attempt}.mp3")
        try:
            asyncio.run(_gen(tmp_path))
        except Exception as e:
            print(f"[TTS] 生成异常 {text!r} 第 {attempt+1} 次: {e}")
            if tmp_path.exists():
                tmp_path.unlink()
            continue

        if not tmp_path.exists():
            continue

        size = tmp_path.stat().st_size
        if size > best_size:
            # 丢弃之前的最佳，更新最佳
            if best_path is not None and best_path != tmp_path:
                best_path.unlink()
            best_path = tmp_path
            best_size = size
        else:
            tmp_path.unlink()

        # 达到阈值即停止
        if best_size >= min_size:
            break

    if best_path is None:
        return False

    if best_size < min_size:
        # 疑似截断：绝不落到正式路径。截断是偶发的，下次请求重生成大概率就好；
        # 留下来就会被 ensure_audio 当成已缓存，永久喂一段残缺发音。
        best_path.unlink(missing_ok=True)
        print(f"[TTS] 警告：{text!r} 重试 {max_retries} 次仍未达阈值"
              f"（最大 {best_size}B < 阈值 {min_size}B），已丢弃不缓存")
        return False

    best_path.replace(out_path)
    return True


def ensure_audio(ref, text, out_path=None):
    """确保音频文件存在，不存在则生成。返回路径（Path）或 None。

    out_path 不传时按 ref 推导（词读音缓存）；朗读例句、句子朗读等场景
    传自定义路径（可能落在 AUDIO_DIR 的子目录里——按 out_path.parent 兜底建，
    不然非顶层路径会写文件失败却没异常路径外露）。
    """
    if out_path is None:
        out_path = audio_path_for_ref(ref)
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    cleaned = clean_tts_text(text)
    if not cleaned:
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if generate_audio_sync(cleaned, out_path):
            return out_path
        return None
    except Exception as e:
        print(f"[TTS] 生成失败 {ref} ({cleaned!r}): {e}")
        return None


# ====================================================================
# 静态资源版本号
# --------------------------------------------------------------------
# 模板里手写 ?v=13 这类版本号，改完脚本忘了递增就会一直拿缓存里的旧文件
# ——按内容算短哈希，文件一变 URL 就变，浏览器自然重新拉取。
# ====================================================================
STATIC_DIR = Path(__file__).resolve().parent / "static"
_asset_cache = {}


def asset_url(name):
    """返回 static 下某文件的带版本 URL，如 /static/app.js?v=1a2b3c4d。"""
    path = STATIC_DIR / name
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return f"/static/{name}"
    cached = _asset_cache.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    digest = hashlib.sha1(path.read_bytes()).hexdigest()[:8]
    url = f"/static/{name}?v={digest}"
    _asset_cache[name] = (mtime, url)
    return url


@app.context_processor
def inject_asset_url():
    return {"asset": asset_url}


# ====================================================================
# 路由
# ====================================================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config")
def api_config():
    vocab = load_vocab()
    today = datetime.now().date()
    lessons = []
    for lid, ldata in vocab.get("lessons", {}).items():
        words = ldata.get("words", [])
        due_count = sum(1 for w in words if is_due(w, today))
        lessons.append({
            "id": lid,
            "title": ldata.get("title", lid),
            "word_count": len(words),
            "due_count": due_count,
        })
    # 错题本（虚拟课程）
    wrong_refs = gather_wrong_refs(vocab)
    lessons.append({
        "id": WRONG_LESSON_ID,
        "title": "错题本",
        "word_count": len(wrong_refs),
        "due_count": len(wrong_refs),  # 错题本总是视为到期
        "is_wrong_book": True,
    })
    # 真题卷与科目选项（供真题实战模式过滤出题范围）
    exam = load_exam()
    meta = exam.get("meta", {})
    exam_sources = [{"id": "all", "name": "全部真题卷"}]
    exam_sources += [
        {"id": v["id"], "name": v["name"]} for v in meta.get("volumes", [])
    ]
    exam_sections = [{"id": "all", "name": "全部科目"}]
    exam_sections += [{"id": sec, "name": sec} for sec in meta.get("sections", [])]
    focus = load_focus()
    return jsonify({
        "lessons": lessons,
        "modes": [{"id": m["id"], "name": m["name"], "desc": m["desc"],
                   "ui": mode_ui(m)} for m in PRACTICE_MODES],
        "exam_sources": exam_sources,
        "exam_sections": exam_sections,
        # 重点词：配置页据此显示「只练重点词」这一档范围（词数 0 时置灰）
        "focus": {"count": len(focus["refs"]), "enabled": focus["enabled"]},
    })


@app.route("/api/focus", methods=["GET"])
def api_focus_get():
    """重点词清单：ref 列表 + 词条详情（含掌握度），供清单页展示。"""
    vocab = load_vocab()
    focus = load_focus()
    entries, missing = focus_word_entries(vocab, focus["refs"])
    return jsonify({
        "refs": focus["refs"],
        "enabled": focus["enabled"],
        "updated": focus["updated"],
        "words": entries,
        "count": len(entries),
        "missing": missing,  # 清单里已解析不到的词数（词被删或改过 id）
    })


@app.route("/api/focus", methods=["POST"])
def api_focus_update():
    """维护重点词清单：add / remove / set / clear / enable。

    写操作全程持 _DATA_LOCK（与词表同一把锁，两个标签页同时挑词不会互相覆盖）。
    无效 ref 直接忽略：清单里混进解析不到的词只会在出题时静默少题，不如入库前挡掉。
    """
    data = request.get_json() or {}
    action = str(data.get("action") or "add").strip()
    with _DATA_LOCK:
        focus = load_focus()
        refs = list(focus["refs"])
        enabled = bool(focus["enabled"])
        if action in ("add", "remove", "set"):
            incoming = data.get("refs")
            if not isinstance(incoming, list):
                return jsonify({"error": "refs 必须是数组"}), 400
            vocab = load_vocab()
            valid = []
            for r in incoming:
                ref = str(r).strip()
                if ref and ref not in valid and resolve_word(vocab, ref):
                    valid.append(ref)
            if action == "add":
                for ref in valid:
                    if ref not in refs:
                        refs.append(ref)
            elif action == "remove":
                drop = set(valid)
                refs = [r for r in refs if r not in drop]
            else:  # set：整体替换，顺序按传入
                refs = valid
        elif action == "clear":
            refs = []
        elif action == "enable":
            enabled = bool(data.get("enabled", True))
        else:
            return jsonify({"error": f"未知的操作：{action}"}), 400
        if len(refs) > FOCUS_MAX:
            return jsonify({"error": f"重点词最多 {FOCUS_MAX} 个（现在 {len(refs)} 个）"}), 400
        # 从空到有自动启用（挑完就能练）；清空自动停用（否则会「启用但没词可出」）
        if refs and not focus["refs"]:
            enabled = True
        if not refs:
            enabled = False
        saved = save_focus(refs, enabled)
    entries, missing = focus_word_entries(load_vocab(), saved["refs"])
    return jsonify({
        "refs": saved["refs"], "enabled": saved["enabled"], "updated": saved["updated"],
        "words": entries, "count": len(entries), "missing": missing,
    })


@app.route("/api/focus/pick")
def api_focus_pick():
    """挑词面板的候选词：课程 + 筛选 + 关键词，按词表顺序返回。

    不按模式过滤（清单跨模式共用，某模式出不了题由出题时再判）；
    selected 标出已在清单里的词，前端据此渲染勾选态。
    """
    lesson_id = request.args.get("lesson", "all")
    q = (request.args.get("q") or "").strip().lower()
    flt = request.args.get("filter", "all")
    vocab = load_vocab()
    lessons = vocab.get("lessons", {})
    target = [lesson_id] if lesson_id != "all" else list(lessons.keys())
    today = datetime.now().date()
    selected = set(load_focus()["refs"])
    words = []
    for lid in target:
        ldata = lessons.get(lid)
        if not ldata:
            continue
        for w in ldata.get("words", []):
            m = mastery_level(w)
            if flt == "weak" and m > WEAK_THRESHOLD:
                continue
            if flt == "unlearned" and m != 0:
                continue
            if flt == "due" and not is_due(w, today):
                continue
            kanji = str(w.get("kanji") or "").strip()
            hira = str(w.get("hiragana") or "").strip()
            meaning = str(w.get("meaning") or "").strip()
            if q and q not in f"{kanji} {hira} {meaning} {w.get('romaji') or ''}".lower():
                continue
            ref = f"{lid}:{w.get('id', '')}"
            words.append({"ref": ref, "lesson_id": lid,
                          "lesson_title": ldata.get("title", lid),
                          "kanji": kanji, "hiragana": hira, "meaning": meaning,
                          "mastery": m, "selected": ref in selected})
    return jsonify({"words": words, "total": len(words)})


def _quiz_days_summary():
    """从 quizzes/ 聚合「今日已练题数」与「连续练习天数」。

    **只解析今天的记录**：「练过哪些天」直接取文件名头 8 位（`YYYYMMDD_…`），
    一个目录列举就够，历史文件连打开都不打开。
    起因：首页每次加载都要这个摘要，原先它把全部历史解析一遍——172 条要 22 ms，
    而且随文件数线性涨（5000 条时每次约 640 ms，首页会明显变慢）。
    日期取文件名是可核对的：文件名与 timestamp 是交卷时同一时刻写死的，
    实测 172 条记录的「文件名日期集合」与「timestamp 日期集合」完全一致；
    文件名格式本身也有回归锁（TestFinishScheduling.test_record_filename_ms_and_session）。
    """
    days = set()
    today = datetime.now().date()
    today_str = today.isoformat()
    today_prefix = today.strftime("%Y%m%d")
    today_done = 0
    for f in QUIZZES_DIR.glob("*.json"):
        head = f.name[:8]
        if len(head) != 8 or not head.isdigit():
            continue                      # 认不出日期的文件不进「练过哪些天」
        days.add(f"{head[:4]}-{head[4:6]}-{head[6:]}")
        if not f.name.startswith(today_prefix):
            continue                      # 今天之外的记录不用读进来
        try:
            with open(f, "r", encoding="utf-8") as fp:
                rec = json.load(fp)
        except (json.JSONDecodeError, OSError):
            continue
        today_done += _int_field(rec.get("total"), 0, lo=0)
    # 连续天数：今天已练就从今天往回数，今天还没练则从昨天起算（保住昨天的连击）
    streak = 0
    cur = today
    if today_str not in days:
        cur -= timedelta(days=1)
    while cur.isoformat() in days:
        streak += 1
        cur -= timedelta(days=1)
    return {"today_done": today_done, "streak_days": streak}


def build_home_payload():
    """首页方块导航：方块定义 + 每个方块的子项 + 今日概览数字。

    子项有两类来源：练习模式（按 PRACTICE_MODES 里的 module/group 归属）
    与工具入口（EXTRA_ITEMS）。两边都在服务端拼好，前端不硬编码任何模式列表。
    """
    vocab = load_vocab()
    today = datetime.now().date()
    due_words = unlearned = review_due = 0
    for ldata in vocab.get("lessons", {}).values():
        for w in ldata.get("words", []):
            # 「没练过」与「练过但到期」是两个池子：前者该学、后者该复习。
            # 混着看的话首页「今日到期」是个永远消不掉的大数（含全部新词），
            # 于是天天显示「还差一千多词」，反而看不出实际该做多少
            if is_learned(w):
                if is_due(w, today):
                    review_due += 1
            else:
                unlearned += 1
            if is_due(w, today):
                due_words += 1
    # 独立题库（助词/真题）：题量与到期数
    bank_counts = {}
    due_bank = 0
    for mode_id, data in (("particle", load_particles()), ("exam", load_exam())):
        qs = data.get("questions", [])
        bank_counts[mode_id] = len(qs)
        due_bank += sum(1 for q in qs if is_due(q, today))
    overview = {
        "due_words": due_words,
        # 到期里的「练过、该复习」那一半（另一半是新词）。due_words 保留为
        # 两者之和，是给「今天总共有多少待办」用的口径
        "review_due": review_due,
        "unlearned": unlearned,
        "due_bank": due_bank,
        "wrong_words": len(gather_wrong_refs(vocab)),
        # 作品沉浸：已导入作品数（works 目录缺失/损坏都按 0，不拖垮首页）
        "works": len(works.list_works()),
        # 图片日语：已导入的图片讲解课数
        "picture_lessons": len(picture.list_lessons()),
        # 重点词：清单里已圈定的词数（0 时前端不给角标）
        "focus_words": len(load_focus()["refs"]),
    }
    overview.update(_quiz_days_summary())

    modules = []
    for mod in MODULE_DEFS:
        mid = mod["id"]
        items = [{
            "id": m["id"],
            "kind": "mode",
            "name": m["name"],
            "desc": m["desc"],
            "group": m.get("group", ""),
            # 独立题库带题量角标（词库模式的可出题数随课程变，不下发）
            "badge": f"{bank_counts[m['id']]} 题" if m["id"] in bank_counts else "",
        } for m in PRACTICE_MODES if m.get("module") == mid]
        items += [{
            "id": it["id"], "kind": it["kind"], "name": it["name"],
            "desc": it["desc"], "group": "",
            # 工具入口的角标：只有配了 badge_key 且数字非 0 才显示（如重点词词数）；
            # 单位默认「词」，其他口径（如图片日语「N 课」）由 badge_text 覆盖
            "badge": ((it.get("badge_text") or "{} 词").format(overview[it["badge_key"]])
                      if it.get("badge_key") and overview.get(it["badge_key"]) else ""),
        } for it in EXTRA_ITEMS.get(mid, [])]
        modules.append({
            "id": mid,
            "icon": mod["icon"],
            "title": mod["title"],
            "items": items,
            "badge": mod["badge_text"].format(overview.get(mod["badge_key"], 0)),
        })
    return {"modules": modules, "overview": overview}


@app.route("/api/home")
def api_home():
    """首页导航：四个方块的子项列表与统计角标。"""
    return jsonify(build_home_payload())


# ---------- 今日处方 ----------
# 首页给的是一串数字（待复习 / 未学 / 题库到期），但真要开练还得去配置页选
# 课程、题型、题量、池子四样。处方按当前数据算出一份固定配方：每步都带齐参数，
# 点一下直接开练；**不落盘、不记录完成状态**——今天做了多少看首页「今日已练」
# 就够，多一份状态就多一处会漂的地方。
PLAN_REVIEW_COUNT = 20   # 到期复习一剂（约 5-10 分钟）
PLAN_WRONG_COUNT = 10    # 错题本一剂
PLAN_BANK_COUNT = 10     # 助词一剂

# 每一步用哪种题型：三步都挂在 kana_to_cn 上就是「看词选意思 ×3」，单调。
# 按每一步的目的各给一种，**候选只放不依赖发音的题型**——上课时可能开着
# 「不听听力」，audio_* 那几类会被整批剔掉，处方就空了。
# 顺序即优先级：取第一个「这一步的池子真出得了题」的。词库题型都受数据限制
# （汉字题要词有汉字、例句题要词有例句），硬点一个就会写出「过 20 个」而
# 点进去只有几道、甚至一道都没有。
PLAN_STEP_MODES = {
    # 再认：量大求快，先求「认得」。退化候选是给中文借词的——「汉字 = 释义」的
    # 词（学校/学生/先生…）在看词选意思里是送分题，被 mode_accepts 判掉，
    # 这批词真正要记的恰是读音，退到看汉字写假名正好。
    "review": ("kana_to_cn", "kanji_to_kana"),
    "wrong": ("kanji_to_kana", "kana_to_cn"),   # 输出：错过的词得自己能写出来
    "focus": ("cloze_cn", "kana_to_cn"),        # 语境：例句里填回，练「怎么用」
}


def _plan_pick_mode(step, words, want):
    """给处方某一步挑题型与题量：返回 (mode_id, mode_name, count)，挑不到返回 None。

    words 是该步**练习池里的全部候选词**：复习按「练过的词」算而不是只数已到期的
    ——/api/start 的 pick_due_first 到期优先、不够会用未到期的补，只数到期的会低估。
    """
    modes = {m["id"]: m for m in PRACTICE_MODES}
    for mid in PLAN_STEP_MODES.get(step, ()):
        md = modes.get(mid)
        if not md:
            continue
        n = sum(1 for lid, w in words if mode_accepts(w, md, lesson_id=lid))
        if n:
            return mid, md["name"], min(want, n)
    return None


def _today_plan_activity(focus_refs):
    """今日处方用：当天各步「做了多少」，只读练习记录、不落盘任何状态。

    归属口径与处方步骤一一对应（同一份记录只会落进一个桶），单位统一成
    「题」或「词」——所以打卡判定各处都一样：今日量 ≥ 该步剂量。

      learn    mode=learn 记录的 total 之和（learn 记录的 total 就是本批标记
               「见过」的词数，所以单位是「个词」）
      particle mode=particle 的题数
      wrong    lesson=lesson_wrong 的题数（错题本练习）
      word     其余词库练习的题数（含课程模式与词义配对）
      focus    word 里 ref 落在重点词清单内的题数——清单开着时词库练习只出清单词，
               于是「过一遍重点词」的打卡量就是它（清单没开时无人读它）

    和 _quiz_days_summary 同一口径：**只列今天的文件**（按文件名头 8 位筛），
    历史记录不打开——首页每次加载都会调它，全量解析会随文件数线性变慢。
    """
    today_str = datetime.now().date().isoformat()
    today_prefix = datetime.now().strftime("%Y%m%d")
    act = {"learn": 0, "particle": 0, "wrong": 0, "word": 0, "focus": 0}
    fset = {str(r) for r in (focus_refs or [])}
    for f in QUIZZES_DIR.glob(f"{today_prefix}_*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                rec = json.load(fp)
        except (json.JSONDecodeError, OSError):
            continue
        if str(rec.get("timestamp", ""))[:10] != today_str:
            continue
        mode = str(rec.get("mode") or "")
        total = _int_field(rec.get("total"), 0, lo=0)
        if mode == "learn":
            act["learn"] += total
        elif mode == "particle":
            act["particle"] += total
        elif mode == "exam":
            continue                      # 真题不进处方，也不参与打卡
        elif str(rec.get("lesson") or "") == WRONG_LESSON_ID:
            act["wrong"] += total
        else:
            act["word"] += total
            if fset:
                act["focus"] += sum(
                    1 for r in (rec.get("results") or [])
                    if isinstance(r, dict) and r.get("ref") in fset)
    return act


def build_plan():
    """今日处方：按当前数据排出「接下来最该做什么」的步骤表。

    三条口径（都有理由，别顺手改）：
    - 重点词清单启用时第一步换成「过一遍清单」：`focus_scope` 会把课程范围整个
      顶掉，再给一条按课程的复习步骤只会跟它打架（点了出来的还是清单里的词）。
    - 真题不进处方：它是整卷模拟，拆成每日十题就失去了套卷练习的意义。
    - 助词题只看有没有到期、不按「练过多少」筛选：100 题里只碰过 11 题，
      正是它该被排进处方的理由。
    """
    vocab = load_vocab()
    today = datetime.now().date()
    review_due = unlearned = 0
    max_overdue = 0
    # 「练过的词」整份留着：复习步骤按这个池子算能出多少题——/api/start 是
    # 到期优先、不够用未到期的补，只数已到期的会低估。
    learned_words = []
    for lid, ldata in vocab.get("lessons", {}).items():
        for w in ldata.get("words", []):
            if is_learned(w):
                learned_words.append((lid, w))
                if is_due(w, today):
                    review_due += 1
                    max_overdue = max(max_overdue, (today - effective_due(w)).days)
            else:
                unlearned += 1

    steps = []
    focus = load_focus()
    focus_refs = focus.get("refs") or []
    # enabled 的缺省与 focus_scope 一致（缺字段按「开着」算）：不一致的话
    # 处方说的和 /api/start 实际出的题会对不上
    if focus_refs and focus.get("enabled", True):
        words = [w for w in (resolve_word(vocab, ref) for ref in focus_refs) if w]
        pick = _plan_pick_mode("focus", words, min(len(focus_refs), PLAN_REVIEW_COUNT))
        if pick:
            mid, mname, n = pick
            steps.append({
                "id": "focus", "title": "过一遍重点词", "unit": "个词", "count": n,
                "desc": f"清单里 {len(focus_refs)} 个词，今天过 {n} 个 · {mname}",
                "start": {"kind": "drill", "lesson": "all", "mode": mid,
                          "count": n, "schedule": "random", "pool": POOL_ALL,
                          "focus_weak": False, "focus": True},
            })
    elif review_due:
        pick = _plan_pick_mode("review", learned_words,
                               min(review_due, PLAN_REVIEW_COUNT))
        if pick:
            mid, mname, n = pick
            overdue = f"（最久逾期 {max_overdue} 天）" if max_overdue > 0 else ""
            steps.append({
                "id": "review", "title": "复习到期词", "unit": "个词", "count": n,
                "desc": f"练过的词里 {review_due} 个已到期{overdue}，先过 {n} 个 · {mname}",
                "start": {"kind": "drill", "lesson": "all", "mode": mid,
                          "count": n, "schedule": "due", "pool": POOL_REVIEW,
                          "focus_weak": False, "focus": False},
            })

    if unlearned:
        steps.append({
            "id": "learn", "title": "学一批新词", "unit": "个词",
            "count": LEARN_BATCH_DEFAULT,
            "desc": f"没碰过的词还有 {unlearned} 个，今天先学 {LEARN_BATCH_DEFAULT} 个",
            "start": {"kind": "learn", "lesson": "all", "count": LEARN_BATCH_DEFAULT},
        })

    wrong_items = gather_wrong_refs(vocab)   # [{"ref":…, "last_wrong":…, "wrong_count":…}]
    if wrong_items:
        words = [w for w in (resolve_word(vocab, it["ref"]) for it in wrong_items) if w]
        pick = _plan_pick_mode("wrong", words, min(len(wrong_items), PLAN_WRONG_COUNT))
        if pick:
            mid, mname, n = pick
            steps.append({
                "id": "wrong", "title": "错题本", "unit": "个词", "count": n,
                "desc": f"{len(wrong_items)} 个词错过还没毕业，练 {n} 个 · {mname}",
                "start": {"kind": "drill", "lesson": WRONG_LESSON_ID, "mode": mid,
                          "count": n, "schedule": "due", "pool": POOL_ALL,
                          "focus_weak": False, "focus": False},
            })

    particles = load_particles().get("questions", [])
    p_due = sum(1 for q in particles if is_due(q, today))
    if p_due:
        n = min(p_due, PLAN_BANK_COUNT)
        p_seen = sum(1 for q in particles if int(q.get("mastery") or 0) > 0)
        steps.append({
            "id": "particle", "title": "助词填空", "unit": "题", "count": n,
            "desc": f"题库 {len(particles)} 题只练过 {p_seen} 题，{p_due} 题到期",
            "start": {"kind": "drill", "lesson": "all", "mode": "particle",
                      "count": n, "schedule": "due"},
        })

    # 打卡状态：当天做到该步剂量就算「今天做过这一项」（前端换浅底 + ✓，仍可再点）。
    # 纯从练习记录现算，不落盘任何状态——不会与词表/掌握度抢口径，删掉记录即回到未做。
    act = _today_plan_activity(focus_refs)
    kind = {"focus": "focus", "review": "word", "learn": "learn",
            "wrong": "wrong", "particle": "particle"}
    for st in steps:
        st["done_today"] = act[kind[st["id"]]]
        st["done"] = st["done_today"] >= st["count"]
    return {"steps": steps}


@app.route("/api/plan")
def api_plan():
    """今日处方（只读）：步骤表 + 每步开练所需的完整参数。"""
    return jsonify(build_plan())


# 首页「进度与薄弱点」直接看的两个文档（只读，不改文件）
DOC_FILES = [("progress", "progress.md"), ("weaknesses", "weaknesses.md")]
DOC_MAX_CHARS = 20000


@app.route("/api/docs")
def api_docs():
    """返回 progress.md / weaknesses.md 原文，省得为了看进度去翻文件。"""
    out = {}
    for key, name in DOC_FILES:
        path = BASE_DIR / name
        if not path.exists():
            out[key] = f"（项目根目录下没有 {name}）"
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > DOC_MAX_CHARS:
            text = (text[:DOC_MAX_CHARS]
                    + f"\n\n…（内容过长已截断，全文见项目根目录 {name}）")
        out[key] = text
    return jsonify(out)


@app.route("/stats")
def stats_page():
    return render_template("stats.html")


@app.route("/api/stats")
def api_stats():
    """聚合词库与全部练习记录，供统计页展示。"""
    vocab = load_vocab()
    today = datetime.now().date()

    # ---- 词库：各课程掌握度分布与到期数 ----
    lessons = []
    dist_total = {}
    for lid, ldata in vocab.get("lessons", {}).items():
        words = ldata.get("words", [])
        dist = {}
        for w in words:
            m = int(w.get("mastery") or 0)
            dist[m] = dist.get(m, 0) + 1
            dist_total[m] = dist_total.get(m, 0) + 1
        lessons.append({
            "id": lid,
            "title": ldata.get("title", lid),
            "word_count": len(words),
            "due_count": sum(1 for w in words if is_due(w, today)),
            "mastery_dist": {str(k): v for k, v in sorted(dist.items())},
        })

    # ---- 练习记录聚合 ----
    days = {}         # 日期 -> {sessions, total, correct}
    modes = {}        # 模式 -> {sessions, total, correct}
    wrong_words = {}  # ref -> {count, last_wrong}
    confusions = {}   # "expected|your" -> {expected, your, count}
    total_questions = total_correct = 0
    for f in sorted(QUIZZES_DIR.glob("*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                rec = json.load(fp)
        except (json.JSONDecodeError, OSError):
            continue
        ts = rec.get("timestamp", "")
        d = ts[:10]
        t, c = int(rec.get("total", 0) or 0), int(rec.get("correct", 0) or 0)
        total_questions += t
        total_correct += c
        day = days.setdefault(d, {"sessions": 0, "total": 0, "correct": 0})
        day["sessions"] += 1
        day["total"] += t
        day["correct"] += c
        mo = modes.setdefault(rec.get("mode", "?"), {"sessions": 0, "total": 0, "correct": 0})
        mo["sessions"] += 1
        mo["total"] += t
        mo["correct"] += c
        for r in rec.get("results", []):
            if r.get("correct", False):
                continue
            ref = r.get("ref", "")
            if ref:
                ww = wrong_words.setdefault(ref, {"count": 0, "last_wrong": ts})
                ww["count"] += 1
                if ts > ww["last_wrong"]:
                    ww["last_wrong"] = ts
            # 混淆对：作答非空且与正确答案不同（完整作答详情上线后的记录才有）
            ya = normalize_kana(str(r.get("your_answer") or ""))
            exp = normalize_kana(str(r.get("expected") or ""))
            if ya and exp and ya != exp:
                cf = confusions.setdefault(f"{exp}|{ya}", {"expected": exp, "your": ya, "count": 0})
                cf["count"] += 1

    # 错词附上单词信息，按错误次数排序取 TOP
    wrong_list = []
    for ref, v in wrong_words.items():
        resolved = resolve_word(vocab, ref)
        if not resolved:
            continue
        w = resolved[1]
        wrong_list.append({
            "ref": ref,
            "kanji": w.get("kanji", ""),
            "hiragana": w.get("hiragana", ""),
            "meaning": w.get("meaning", ""),
            "mastery": int(w.get("mastery") or 0),
            "count": v["count"],
            "last_wrong": v["last_wrong"],
        })
    # 错误次数降序；同次数按最近错误时间降序（稳定排序分两步实现二级键）
    wrong_list.sort(key=lambda x: x["last_wrong"], reverse=True)
    wrong_list.sort(key=lambda x: x["count"], reverse=True)
    top_wrong = wrong_list[:20]

    top_confusions = sorted(confusions.values(), key=lambda x: -x["count"])[:15]

    return jsonify({
        "summary": {
            "total_words": sum(l["word_count"] for l in lessons),
            "unlearned": dist_total.get(0, 0),
            "total_sessions": sum(d["sessions"] for d in days.values()),
            "total_questions": total_questions,
            "total_correct": total_correct,
            "practice_days": len(days),
        },
        "lessons": lessons,
        "mode_names": {**{m["id"]: m["name"] for m in PRACTICE_MODES},
                       **RETIRED_MODES, "learn": LEARN_MODE_NAME},
        "days": [
            {"date": d, **v, "accuracy": round(v["correct"] / v["total"], 4) if v["total"] else 0}
            for d, v in sorted(days.items(), reverse=True)
        ],
        "modes": [
            {"mode": m, **v, "accuracy": round(v["correct"] / v["total"], 4) if v["total"] else 0}
            for m, v in sorted(modes.items(), key=lambda x: -x[1]["total"])
        ],
        "top_wrong": top_wrong,
        "top_confusions": top_confusions,
    })


def start_bank(mode_def, cfg, load_fn, ref_prefix, lesson_title, prompt_key,
               source=None, section=None):
    """独立题库模式通用选题：助词填空与真题实战共用（复用掌握度调度）。
    source/section：真题题库的范围过滤（卷年份 / 科目），None 或 "all" 表示不过滤。"""
    # 题库本身有限（助词 100 / 真题 188），切片天然收敛，故不设 hi：真题要能一次出满
    count = _int_field(cfg.get("count"), 20, lo=1)
    focus_weak = bool(cfg.get("focus_weak", False))
    schedule = cfg.get("schedule", "due")

    data = load_fn()
    cands = list(data.get("questions", []))
    if source and source != "all":
        cands = [q for q in cands if source in q.get("source", "")]
    if section and section != "all":
        cands = [q for q in cands if q.get("section") == section]
    if focus_weak:
        cands = [q for q in cands if int(q.get("mastery") or 0) <= WEAK_THRESHOLD]
    if not cands:
        return jsonify({"error": "没有符合条件的题目"}), 400

    if schedule == "due":
        today = datetime.now().date()
        due_list = [q for q in cands if is_due(q, today)]
        due_list.sort(key=lambda q: effective_due(q))
        not_due = [q for q in cands if not is_due(q, today)]
        not_due.sort(key=lambda q: int(q.get("mastery") or 0))
        selected = (due_list + not_due)[:count]
    else:
        random.shuffle(cands)
        selected = cands[:count]

    is_exam = mode_def.get("exam_mode", False)
    ui = mode_ui(mode_def)
    if is_exam:
        ui["choice"] = True   # 官方四选一（选项由题库自带）
        ui["typing"] = False
    questions = []
    for q in selected:
        item = {
            "ref": f"{ref_prefix}:{q['id']}",
            "lesson_id": ref_prefix,
            "lesson_title": lesson_title,
            "prompt": q[prompt_key],
        }
        if is_exam:
            item["options"] = q["options"]
            item["qtype"] = q.get("qtype", "")
            if q.get("image"):
                item["image"] = q["image"]
            if q.get("audio"):
                item["audio"] = q["audio"]
        questions.append(item)
    return jsonify({
        "mode": mode_def["id"],
        "mode_name": mode_def["name"],
        "questions": questions,
        "total": len(questions),
        "schedule": schedule,
        "ui": ui,
    })


COURSE_NEW_CAP = 5     # 每课新词上限（多邻国新词组大小）
COURSE_DEFAULT_COUNT = 15
# 课程模式新词的再认题型轮换：轮到配图题但词无图/无汉字时自动跳过降级下一个。
# 两个配图题型都在轮换里：看图选汉字（意象→词形）与看图选发音（图→四段发音选一）
COURSE_RECOGNITION_ROTATION = ("kana_to_cn", "image_to_kanji", "image_to_word",
                               "audio_to_cn", "cloze_cn", "conjugation")


def start_course(lesson_id, cfg):
    """课程模式选题：新词（预习卡+选择题）+ 旧词复习（按掌握度分配题型）。

    脚手架按掌握度递减：m≤1 新词出选择题（点选词库），m2 出例句填空，
    m≥3 出拼句题（例句可分块时，组句排序与例句听写交替）或打字题
    （看词形写假名 / 听音写假名交替）——越熟的词要求越高。
    课内题序 = 选择题 → 拼句 → 打字（先认后产）。
    错题回炉（课末重做）由前端组织，重做结果不计掌握度。
    """
    try:
        count = max(5, min(30, int(cfg.get("count", COURSE_DEFAULT_COUNT)
                                  or COURSE_DEFAULT_COUNT)))
    except (TypeError, ValueError):
        count = COURSE_DEFAULT_COUNT

    vocab = load_vocab()
    lessons = vocab.get("lessons", {})
    target = [lesson_id] if lesson_id != "all" else list(lessons.keys())
    # 重点词：课程模式同样可以只围着清单转（新词预习 + 混合题型都从清单里出）
    focus_on, focus_refs = focus_scope(get_mode("course"), cfg)
    refset = set(focus_refs) if focus_on else None
    pool = []
    for lid in target:
        if lid not in lessons or lid.endswith("_phrases"):
            continue  # 句型课的 xx 占位符不适合听音/组句等混合题型
        for w in lessons[lid].get("words", []):
            if refset is not None and f"{lid}:{w.get('id')}" not in refset:
                continue
            pool.append((lid, w))
    if not pool:
        return jsonify({"error": "重点词清单里没有可练习的单词" if focus_on
                        else "该课程没有可练习的单词"}), 400

    today = datetime.now().date()
    m_new = [(lid, w) for lid, w in pool if int(w.get("mastery") or 0) <= 1]
    m_rest = [(lid, w) for lid, w in pool if int(w.get("mastery") or 0) >= 2]
    # 新词：m0 优先、m1 补位（m1 也给预习卡——「见过但没记住」值得重讲）
    new_items = m_new[:COURSE_NEW_CAP]
    # 比对一律用 (课程, 词 id) 而不是裸 id：词 id 是课内短号，456 个 id 在多课重复
    # （w001 出现 7 次）。选「全部课程」时按裸 id 比对，A 课一个 m≥2 的复习词会
    # 因为 B 课恰好有个同名 id 的新词进了预习名额而被整条排除，永远出不到题。
    new_keys = {(lid, w.get("id")) for lid, w in new_items}
    # 复习池：普通课程只复习 m≥2 的旧词；重点词是用户自己圈的小清单，没进
    # 预习名额的那些词（清单里全是生词时很常见）也要出到题，故整份都算复习池
    review_src = [(lid, w) for lid, w in pool
                  if (lid, w.get("id")) not in new_keys] if focus_on else m_rest
    # 旧词复习：到期优先（逾期最久靠前），不够从未到期里随机补
    n_review = max(0, count - len(new_items))
    due = [(lid, w) for lid, w in review_src if is_due(w, today)]
    due.sort(key=lambda x: effective_due(x[1]))
    review_items = due[:n_review]
    if len(review_items) < n_review:
        picked = {(lid, w.get("id")) for lid, w in review_items} | new_keys
        extra = [(lid, w) for lid, w in review_src
                 if (lid, w.get("id")) not in picked]
        random.shuffle(extra)
        review_items += extra[:n_review - len(review_items)]
    if not new_items and not review_items:
        return jsonify({"error": "该课程没有可练习的单词"}), 400

    # 干扰项池（词义 / 读音 / 词形各一份），整课复用：本课词优先，不足时全词库补
    all_words = [w for _, w in pool]
    other_words = [w for ldata in lessons.values() for w in ldata.get("words", [])]
    meaning_pool = _mcq_pool_scoped(all_words, get_mode("kana_to_cn"), other_words)
    # 读音池的选项要念出来：句型课的残句与空读音排除在干扰项之外
    reading_pool = _mcq_pool_scoped(all_words, get_mode("image_to_word"),
                                    audio_option_words(vocab))
    form_pool = _mcq_pool_scoped(all_words, get_mode("cloze_cn"), other_words)
    audio_refs = audio_ref_map(vocab)

    def build(lid, w, mode_id):
        """构造一道课程题：题干/选项按内层模式生成。"""
        md = get_mode(mode_id)
        if md.get("conj_mode"):
            # 活用题：课程新词轮换只出四选一（写法即基本形的词条才轮得到，
            # 见 mode_accepts 的 conj_dict_form）；变形随机抽一张
            return conj_question(lid, lessons[lid].get("title", lid),
                                 conj_index()[f"{lid}:{w['id']}"],
                                 random.choice(conjugation.FORMS), "choice")
        item = {
            "ref": f"{lid}:{w['id']}",
            "lesson_id": lid,
            "lesson_title": lessons[lid].get("title", lid),
            "mode": mode_id,      # 内层题型，前端与 /api/check 都按它分发
            "mode_name": md["name"],  # 题面徽标：与独立模式同名，不另维护映射表
            "prompt": prompt_for(w, md),
            "ui": mode_ui(md),    # 内层题型的交互描述（打字/点选/图片/音频）
        }
        if md.get("image_mode"):
            # 配图题：题干即图片；prompt 一并记 URL，与独立配图模式口径一致
            item["image"] = vocab_image_url(lid, w["id"]) or ""
            item["prompt"] = item["image"]
        if md.get("mcq_mode"):
            # 干扰项池按答案口径取：词形 / 读音 / 释义——
            # 池与答案口径不一致会出「选读音但干扰项是中文释义」的废题
            if md.get("cloze_mode") or md.get("answer_field") == "kanji":
                pool_ = form_pool
            elif md.get("answer_field") == "hiragana":
                pool_ = reading_pool
            else:
                pool_ = meaning_pool
            item["options"] = _build_mcq_options(w, md, pool_)
            if md.get("audio_options"):
                # 选项即发音：与 options 一一对应的音频地址
                item["option_audio"] = audio_option_urls(
                    item["options"], expected_for(w, md), item["ref"], audio_refs)
        if md.get("order_mode"):
            item["blocks"] = _shuffled_blocks(sentence_blocks(w.get("example_ja", "")))
        if md.get("sentence_audio"):
            # 例句听写：整句发音地址（/api/tts/text，按内容哈希缓存）
            item["audio"] = tts_url_for_text(w.get("example_ja", ""))
        return item

    choice_qs, order_qs, typing_qs = [], [], []
    rotation = COURSE_RECOGNITION_ROTATION
    for i, (lid, w) in enumerate(new_items):
        # 新词：预习卡 + 选择题（再认题型轮换，避免一课全是同款；无图词跳过看图选词）
        prefs = rotation[i % len(rotation):] + rotation[:i % len(rotation)]
        q_mode = next((c for c in prefs
                       if mode_accepts(w, get_mode(c), lid,
                                       conj_dict_form=(c == "conjugation"))),
                      "kana_to_cn")
        item = build(lid, w, q_mode)
        item["intro"] = {
            "kanji": w.get("kanji", ""),
            "hiragana": w.get("hiragana", ""),
            "romaji": w.get("romaji", ""),
            "meaning": w.get("meaning", ""),
            "notes": w.get("notes", ""),
            # 用法讲解：与闪卡 /「学新词」卡片同一份字段（前端三处都读 w.tip）
            "tip": w.get("tip", ""),
            "example_ja": w.get("example_ja", ""),
            "example_zh": w.get("example_zh", ""),
            # 预习卡带配图（有图的词才有）：首次记忆就挂上意象
            "image": vocab_image_url(lid, w["id"]) or "",
            # 预习卡只给 m<=1 的新词，掌握度供前端标注
            "mastery": mastery_level(w),
        }
        choice_qs.append(item)
    for lid, w in review_items:
        m = int(w.get("mastery") or 0)
        if m == 2:
            # 认得但不稳：例句填空优先（语境再认），退回看词选意思
            q_mode = "cloze_cn" if mode_accepts(w, get_mode("cloze_cn")) else "kana_to_cn"
            choice_qs.append(build(lid, w, q_mode))
        elif m >= 3:
            # 熟词：产出型——能拼句先拼句（语序是日语核心难点），否则打字
            if mode_accepts(w, get_mode("order_cn")):
                # 拼句在「看中文组句」与「听整句听写」之间交替：后者不给中文提示，
                # 要求更高（与脚手架同向），也顺便把耳朵练进句子里
                pref = "dictation" if len(order_qs) % 2 else "order_cn"
                q_mode = pref if mode_accepts(w, get_mode(pref), lid) else "order_cn"
                order_qs.append(build(lid, w, q_mode))
            else:
                # 打字：看词形写假名与听音写假名交替——前者要汉字，
                # 纯假名词（写法为 ---）退回听音写假名
                pref = "kanji_to_kana" if len(typing_qs) % 2 == 0 else "audio_to_kana"
                q_mode = pref if mode_accepts(w, get_mode(pref), lid) else "audio_to_kana"
                typing_qs.append(build(lid, w, q_mode))
        else:  # m1 且未进新词名额（新词满员时）：按新词处理出选择题
            q_mode = "kana_to_cn" if mode_accepts(w, get_mode("kana_to_cn")) else "cloze_cn"
            choice_qs.append(build(lid, w, q_mode))

    questions = choice_qs + order_qs + typing_qs
    if not questions:
        return jsonify({"error": "重点词清单里没有可练习的单词" if focus_on
                        else "该课程没有可练习的单词"}), 400
    if focus_on and len(questions) < count:
        # 重点词清单往往只有十几个词，一课凑不满题量：循环补齐（每轮重新洗牌）。
        # 重复的那份去掉预习卡——同一张卡看两遍没意义，重复的是练习题本身
        extra = []
        while len(questions) + len(extra) < count:
            nxt = list(questions)
            random.shuffle(nxt)
            for q in nxt:
                if len(questions) + len(extra) >= count:
                    break
                dup = dict(q)
                dup.pop("intro", None)
                extra.append(dup)
        questions = questions + extra
    return jsonify({
        "mode": "course",
        "mode_name": get_mode("course")["name"],
        "questions": questions,
        "total": len(questions),
        "schedule": "due",
        "raw_input": False,
        "ui": mode_ui(get_mode("course")),  # 混合课：具体交互看每题的 ui
    })


PAIR_ROUND_SIZE = 5  # 每轮配对的词数（多邻国同款）


def start_pair_match(vocab, lesson_id, cfg):
    """词义配对选题：每轮 5 个词（词↔释义各 5 块瓦片），count=轮数（2..8）。

    同一轮内释义互不相同（否则无法唯一配对）；到期词优先、逾期最久靠前。
    配对在客户端判定（瓦片信息本就全在客户端），结果按词计入掌握度，
    与选择题同用「连对两次才 +1」。
    """
    try:
        count = max(2, min(8, int(cfg.get("count", 5) or 5)))
    except (TypeError, ValueError):
        count = 5

    lessons = vocab.get("lessons", {})
    target = [lesson_id] if lesson_id != "all" else list(lessons.keys())
    today = datetime.now().date()
    # 重点词：配对轮次也只从清单里取词
    focus_on, focus_refs = focus_scope(get_mode("pair_match"), cfg)
    refset = set(focus_refs) if focus_on else None
    pool = []
    for lid in target:
        if lid not in lessons or lid.endswith("_phrases"):
            continue
        for w in lessons[lid].get("words", []):
            if refset is not None and f"{lid}:{w.get('id')}" not in refset:
                continue
            meaning = (w.get("meaning") or "").strip()
            form = w.get("kanji") or ""
            if not form or form == "---":
                form = w.get("hiragana") or ""
            if not meaning or not form:
                continue
            pool.append((lid, w, form, meaning))
    if len(pool) < 3:
        return jsonify({"error": "重点词里可配对的词太少（至少 3 个）" if focus_on
                        else "可配对的词条太少"}), 400

    # 练习池：词库模式同一口径（默认只出练过的词）。重点词是用户点名的清单，
    # 不按「练过 / 没练过」再筛一道，否则清单里的生词永远配不上对
    if not focus_on:
        kind = cfg.get("pool", POOL_ALL)
        if kind == POOL_REVIEW:
            pool = [x for x in pool if is_learned(x[1])]
        elif kind == POOL_NEW:
            pool = [x for x in pool if not is_learned(x[1])]
        if len(pool) < 3:
            return jsonify({"error": "可配对的词条太少" if kind == POOL_ALL
                            else pool_error(kind)}), 400

    # 到期优先（逾期最久靠前），未到期的随机补在后面
    due = [x for x in pool if is_due(x[1], today)]
    due.sort(key=lambda x: effective_due(x[1]))
    rest = [x for x in pool if not is_due(x[1], today)]
    random.shuffle(rest)
    ordered = due + rest

    questions = []
    ptr = 0
    for _ in range(count):
        round_items = []
        used_forms = set()
        used_senses = set()
        while ptr < len(ordered) and len(round_items) < PAIR_ROUND_SIZE:
            lid, w, form, meaning = ordered[ptr]
            ptr += 1
            senses = gloss_senses(meaning)
            # 同轮里释义有共同义项、或写法重复，配对就不唯一了
            if form in used_forms or senses & used_senses:
                continue
            used_forms.add(form)
            used_senses |= senses
            round_items.append((lid, w, form, meaning))
        if len(round_items) < 3:
            break  # 剩余词不足，不再开新轮
        pairs = [{
            "ref": f"{lid}:{w['id']}",
            "form": form,
            "hiragana": w.get("hiragana", ""),
            "meaning": meaning,
        } for lid, w, form, meaning in round_items]
        questions.append({
            "ref": pairs[0]["ref"],
            "lesson_id": round_items[0][0],
            "lesson_title": lessons[round_items[0][0]].get("title", round_items[0][0]),
            "mode": "pair_match",
            "prompt": "把词和对应的释义配对",
            "pairs": pairs,
        })
    if not questions:
        return jsonify({"error": "可配对的词条太少"}), 400
    return jsonify({
        "mode": "pair_match",
        "mode_name": get_mode("pair_match")["name"],
        "questions": questions,
        "total": len(questions),
        "schedule": "due",
        "raw_input": False,
        "ui": mode_ui(get_mode("pair_match")),
    })


@app.route("/api/kanji")
def api_kanji():
    """汉字识字卡：把词条按 kanji 字段逐字聚合，按「包含词数」降序。

    每个字返回含它的所有词条（写法/读音/释义），供前端出识字卡。
    """
    lesson_id = request.args.get("lesson", "all")
    vocab = load_vocab()
    lessons = vocab.get("lessons", {})
    target = [lesson_id] if lesson_id != "all" else list(lessons.keys())
    agg = {}
    for lid in target:
        if lid not in lessons:
            continue
        for w in lessons[lid].get("words", []):
            kanji = w.get("kanji") or ""
            if not kanji or kanji == "---":
                continue
            ref = f"{lid}:{w.get('id', '')}"
            form = kanji
            hira = w.get("hiragana", "")
            meaning = w.get("meaning", "")
            for ch in kanji:
                if not CJK_RE.match(ch):
                    continue
                entry = agg.setdefault(ch, {"char": ch, "words": [], "_refs": set()})
                if ref in entry["_refs"]:
                    continue  # 同一词里该字出现两次只记一条
                entry["_refs"].add(ref)
                entry["words"].append({"ref": ref, "form": form,
                                       "hiragana": hira, "meaning": meaning,
                                       # 掌握度：看一个字能组成哪些词时，
                                       # 顺带一眼看出这些词哪些还生、哪些已经会了
                                       "mastery": mastery_level(w)})
    items = sorted(agg.values(), key=lambda x: (-len(x["words"]), x["char"]))
    for it in items:
        del it["_refs"]
    # title 供前端标题栏直接显示（否则只能显示 lesson_01 这类内部 id）
    title = "全部课程" if lesson_id == "all" else \
        lessons.get(lesson_id, {}).get("title", lesson_id)
    return jsonify({"lesson": lesson_id, "title": title, "kanji": items})


# 动词活用数据（conjugation.py 生成落盘）：ref→词条 缓存，按文件 mtime 失效
# （手改 conjugation.json / 重跑生成器都换 mtime，缓存自动失效）
_CONJ_CACHE = {"key": None, "index": {}}


def conj_index():
    """conjugation.json 的 ref→词条索引；无数据文件时返回空索引（活用功能整体缺席）。"""
    path = BASE_DIR / "data" / "conjugation.json"
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        _CONJ_CACHE["key"], _CONJ_CACHE["index"] = None, {}
        return _CONJ_CACHE["index"]
    if _CONJ_CACHE["key"] != key:
        data = conjugation.load_data(path)
        _CONJ_CACHE["index"] = {v["ref"]: v for v in (data or {}).get("verbs", [])}
        _CONJ_CACHE["key"] = key
    return _CONJ_CACHE["index"]


def conj_hint(ref):
    """练习反馈的活用提示：动词词条给「て形 + ない形」（拍板口径），非动词返回 None。"""
    forms = conj_index().get(ref, {}).get("forms")
    if not forms:
        return None
    return {"te": forms.get("te", ""), "nai": forms.get("nai", "")}


def conj_question(lid, title, entry, form_key, stage):
    """构造一道活用题（stage: choice=四选一热身 | typing=打字巩固）。

    题干给基本形+变形名+中文释义；独立模式的「先选后打」与课程新词轮换的
    四选一共用这里。正确答案不下发（判分在 /api/check 服务端现查）。
    """
    forms = entry.get("forms", {})
    correct = forms.get(form_key, "")
    label = conjugation.FORM_NAMES.get(form_key, form_key)
    item = {
        "ref": entry["ref"],
        "lesson_id": lid,
        "lesson_title": title,
        "mode": "conjugation",   # 课程内层题型标识；独立模式忽略
        "mode_name": get_mode("conjugation")["name"],
        "conj_form": form_key,   # 考哪张变形，判分时随答案回传
        "prompt": f'{entry.get("basic", "")}（{entry.get("meaning", "")}）→ {label}',
        # 每题自带交互描述：前端 uiOf(q) 优先取题目自带的 ui
        "ui": {"cue": "", "task": "", "choice": True} if stage == "choice"
              else {"cue": "", "task": "", "typing": True},
    }
    if stage == "choice":
        # 干扰项：同词其他三张变形（逼着区分 て/た 等）+ 其他动词同一张变形。
        # 必须显式排除正确答案：同一动词常在两课重复收录（如 起きる），
        # 别的词条的同一张变形可能与正确答案完全同字，那样会出现两个正确选项。
        pool, seen = [], {correct}
        for v2 in list(forms.values()) + [
                e2.get("forms", {}).get(form_key, "")
                for e2 in conj_index().values() if e2 is not entry]:
            if v2 and v2 not in seen:
                seen.add(v2)
                pool.append(v2)
            if len(pool) >= 12:
                break
        random.shuffle(pool)
        options = pool[:3] + [correct]
        random.shuffle(options)
        item["options"] = options
    return item


@app.route("/api/conj")
def api_conj():
    """动词活用卡：按课程列出动词词条与四张变形（纯浏览，不排调度不计分）。

    变形数据来自 conjugation.py 生成落盘的 data/conjugation.json；
    例句按 ref 从词表现取——词表是唯一事实源，不落两份。
    """
    lesson_id = request.args.get("lesson", "all")
    vocab = load_vocab()
    lessons = vocab.get("lessons", {})
    target = [lesson_id] if lesson_id != "all" else list(lessons.keys())
    allowed = {lid for lid in target if lid in lessons}
    items = []
    for e in conj_index().values():
        if e.get("lesson_id") not in allowed:
            continue
        resolved = resolve_word(vocab, e["ref"])
        w = resolved[1] if resolved else {}
        items.append({
            "ref": e["ref"],
            # written 是词表里的词条写法（多邻国词条是 ます形，如 起きます），
            # basic 是活用用的基本形（起きる）：卡片大字展示 basic，written 标注在旁
            "written": e.get("written", ""),
            "basic": e.get("basic", ""),
            "reading": e.get("reading", ""),
            "meaning": e.get("meaning", ""),
            "infl_type": e.get("infl_type", ""),
            "note": conjugation.infl_note(e.get("infl_type", "")),
            "forms": e.get("forms", {}),
            "example_ja": w.get("example_ja", ""),
            "example_zh": w.get("example_zh", ""),
        })
    title = "全部课程" if lesson_id == "all" else \
        lessons.get(lesson_id, {}).get("title", lesson_id)
    return jsonify({"lesson": lesson_id, "title": title, "verbs": items})


@app.route("/api/start", methods=["POST"])
def api_start():
    cfg = request.get_json() or {}
    lesson_id = cfg.get("lesson", "all")
    mode_id = cfg.get("mode", "kanji_to_kana")
    count = _int_field(cfg.get("count"), 20, lo=1, hi=DRILL_COUNT_MAX)
    focus_weak = bool(cfg.get("focus_weak", False))
    schedule = cfg.get("schedule", "due")  # "due" 间隔重复 | "random" 随机
    # 练习池：只复习练过的 / 只练新词 / 不筛选。默认 all 保持旧行为，
    # 配置页会显式下发（老会话与脚本不带这个字段时不该突然少一半词）。
    pool = cfg.get("pool", POOL_ALL)
    if pool not in POOL_KINDS:
        pool = POOL_ALL

    mode_def = get_mode(mode_id)
    if not mode_def:
        return jsonify({"error": "未知的练习模式"}), 400

    # 助词填空 / 真题实战：独立题库，忽略课程选择
    if mode_def.get("particle_mode"):
        return start_bank(mode_def, cfg, load_particles, "particle", "助词填空", "sentence")
    if mode_def.get("exam_mode"):
        return start_bank(mode_def, cfg, load_exam, "exam", "N5 真题", "stem",
                          source=cfg.get("exam_source") or None,
                          section=cfg.get("exam_section") or None)
    # 课程模式：混合题型流程（新词预习 + 旧词复习 + 错题回炉由前端组织）
    if mode_def.get("course_mode"):
        if lesson_id == WRONG_LESSON_ID:
            return jsonify({"error": "课程模式请选具体课程，不支持错题本"}), 400
        return start_course(lesson_id, cfg)
    # 词义配对：客户端判定配对，按词计入掌握度（连对闸与选择题一致）
    if mode_def.get("pair_mode"):
        if lesson_id == WRONG_LESSON_ID:
            return jsonify({"error": "词义配对请选具体课程或全部课程"}), 400
        return start_pair_match(load_vocab(), lesson_id, cfg)

    vocab = load_vocab()
    # 重点词：与课程范围互斥——启用后就只认清单，课程下拉不参与出题
    focus_on, focus_refs = focus_scope(mode_def, cfg)
    if focus_on:
        candidates = build_focus_candidates(vocab, focus_refs, mode_def, focus_weak)
        if not candidates:
            return jsonify({"error": "重点词里没有词能出这个模式的题，换个模式或调整清单"}), 400
        # 学习模块「立即练习这批词」与重点词同时给时取交集
        ref_filter = cfg.get("refs")
        if isinstance(ref_filter, list) and ref_filter:
            refset = {str(r) for r in ref_filter}
            candidates = [c for c in candidates
                          if f"{c['lesson_id']}:{c['word']['id']}" in refset]
            if not candidates:
                return jsonify({"error": "这批词不在重点词清单里"}), 400
        if schedule == "due":
            selected = pick_due_first(candidates, count)
        else:
            random.shuffle(candidates)
            selected = candidates[:count]
        # 题量大于清单时循环补齐：同一词反复出现（聚焦练习要的就是重复）
        selected = _cycle_fill(selected, count)
    elif lesson_id == WRONG_LESSON_ID:
        # 错题本：从 quizzes 历史聚合错题，按最近错误时间排序
        candidates = build_wrong_candidates(vocab, mode_def)
        if focus_weak:
            candidates = [c for c in candidates if int(c["word"].get("mastery") or 0) <= WEAK_THRESHOLD]
        if not candidates:
            return jsonify({"error": "错题本为空，先做一组练习产生错题吧"}), 400
        # 错题本已按最近错误时间降序排序，直接取前 N 个
        selected = candidates[:count]
    else:
        candidates = gather_candidates(vocab, lesson_id, mode_def, focus_weak)
        if not candidates:
            return jsonify({"error": "没有符合条件的单词，请调整课程范围或薄弱词过滤"}), 400

        # 可选：只练指定 ref 列表（学习模块「立即练习这批词」用）
        ref_filter = cfg.get("refs")
        if isinstance(ref_filter, list) and ref_filter:
            # 点名的词不受练习池影响：调用方已经把词挑好了，
            # 再按「练过 / 没练过」筛一道只会把刚学的词筛掉
            refset = {str(r) for r in ref_filter}
            candidates = [c for c in candidates
                          if f"{c['lesson_id']}:{c['word']['id']}" in refset]
            if not candidates:
                return jsonify({"error": "指定的词不在课程范围内"}), 400
        else:
            # 练习池：只出练过的（默认）/ 只出没练过的 / 不筛选
            candidates = filter_pool(candidates, pool)
            if not candidates:
                return jsonify({"error": pool_error(pool)}), 400

        if schedule == "due":
            # 间隔重复：优先到期词，按真正到期日升序（逾期最久的优先）
            selected = pick_due_first(candidates, count)
        else:
            random.shuffle(candidates)
            selected = candidates[:count]

    # 选择题模式：干扰项池（同课程范围优先，不足 8 个时用全词库补）
    mcq_pool = []
    audio_refs = {}
    if mode_def.get("mcq_mode"):
        all_words = [w for ldata in vocab.get("lessons", {}).values()
                     for w in ldata.get("words", [])]
        if mode_def.get("audio_options"):
            # 选项是一段发音：念不出来的词（句型课残句、空读音）不能当干扰项
            all_words = audio_option_words(vocab)
            audio_refs = audio_ref_map(vocab)
        mcq_pool = _mcq_pool_scoped([c["word"] for c in candidates],
                                    mode_def, all_words)

    questions = []
    for q in selected:
        w = q["word"]
        item = {
            "ref": f"{q['lesson_id']}:{w['id']}",
            "lesson_id": q["lesson_id"],
            "lesson_title": vocab["lessons"][q["lesson_id"]].get("title", q["lesson_id"]),
            "prompt": prompt_for(w, mode_def),
        }
        if mode_def.get("conj_mode"):
            # 活用题每词两题：四选一热身（再认）→ 打字巩固（产出），考同一张变形；
            # 两行结果同 ref，交卷时不按 ref 去重（本模式不写掌握度，无重复计分问题）
            entry = conj_index()[item["ref"]]
            form_key = random.choice(conjugation.FORMS)
            questions.append(conj_question(q["lesson_id"], item["lesson_title"],
                                           entry, form_key, "choice"))
            questions.append(conj_question(q["lesson_id"], item["lesson_title"],
                                           entry, form_key, "typing"))
            continue
        if mode_def.get("image_mode"):
            # 配图模式：题干即图片；prompt 一并记 URL，练习记录与错题回顾可追溯
            item["image"] = vocab_image_url(q["lesson_id"], w["id"]) or ""
            item["prompt"] = item["image"]
        if mode_def.get("mcq_mode"):
            # 四选一：正确答案混入干扰项打乱；正确答案不下发（判分在 /api/check）
            item["options"] = _build_mcq_options(w, mode_def, mcq_pool)
            if mode_def.get("audio_options"):
                # 选项即发音：与 options 一一对应的音频地址（选项是读音，只用于判分）
                item["option_audio"] = audio_option_urls(
                    item["options"], expected_for(w, mode_def), item["ref"], audio_refs)
        if mode_def.get("order_mode"):
            # 组句题：下发打乱词块，原句不下发（判分在 /api/check）
            item["blocks"] = _shuffled_blocks(sentence_blocks(w.get("example_ja", "")))
        if mode_def.get("sentence_audio"):
            # 例句听写：题干是整句发音，URL 随题下发（前端按 q.audio 播放与预取）
            item["audio"] = tts_url_for_text(w.get("example_ja", ""))
        # 自评模式附带单词详情，供前端翻面直接展示（无需逐题再请求）
        if mode_def.get("self_assessed"):
            item["word"] = {
                "kanji": w.get("kanji", ""),
                "hiragana": w.get("hiragana", ""),
                "romaji": w.get("romaji", ""),
                "meaning": w.get("meaning", ""),
                "notes": w.get("notes", ""),
                "example_ja": w.get("example_ja", ""),
                "example_zh": w.get("example_zh", ""),
                "tip": w.get("tip", ""),
                "image": vocab_image_url(q["lesson_id"], w["id"]) or "",
                # 掌握度（作答前）：闪卡背面据此给卡片着色标注
                "mastery": mastery_level(w),
            }
        questions.append(item)
    return jsonify({
        "mode": mode_id,
        "mode_name": mode_def["name"],
        "questions": questions,
        "total": len(questions),
        "schedule": schedule,
        "raw_input": bool(mode_def.get("raw_input")),
        # 交互描述：题干怎么呈现（文字/图片/发音）、答案怎么交（打字/四选一）
        "ui": mode_ui(mode_def),
    })


@app.route("/api/check", methods=["POST"])
def api_check():
    data = request.get_json() or {}
    ref = data.get("ref", "")
    mode_id = data.get("mode", "kanji_to_kana")
    answer = data.get("answer", "")

    mode_def = get_mode(mode_id)
    if not mode_def:
        return jsonify({"error": "未知的练习模式"}), 400

    # 助词填空 / 真题实战：按各自题库判分
    if mode_def.get("particle_mode") or mode_def.get("exam_mode"):
        is_exam = mode_def.get("exam_mode", False)
        prefix = "exam" if is_exam else "particle"
        parsed = parse_ref(ref)
        if not parsed or parsed[0] != prefix:
            return jsonify({"error": "无效的题目引用"}), 400
        bank = load_exam() if is_exam else load_particles()
        q = next((x for x in bank.get("questions", []) if x.get("id") == parsed[1]), None)
        if not q:
            return jsonify({"error": "题目不存在"}), 404
        if is_exam:
            expected = q["options"][int(q["answer"])]
            # 按选项文本比对（官方卷个别题存在同形选项，文本一致即算对）
            correct = str(answer).strip() == expected
            note = q.get("source", "")
            if q.get("note"):
                note = (note + "｜" + q["note"]) if note else q["note"]
            return jsonify({
                "correct": correct,
                "expected": expected,
                "word": {
                    "kanji": "",
                    "hiragana": "",
                    "romaji": "",
                    "meaning": f'{q.get("section", "")}・{q.get("qtype", "")}',
                    "notes": note,
                    # 聴解题 feedback 里展示官方スクリプト原文与中文翻译
                    "example_ja": q.get("script") or expected,
                    "example_zh": q.get("script_zh", ""),
                },
            })
        correct = check_answer(answer, q["answer"], q.get("alternates"))
        return jsonify({
            "correct": correct,
            "expected": q["answer"],
            "mismatch": [] if correct else classify_mismatch(answer, q["answer"]),
            "word": {
                "kanji": "",
                "hiragana": q["answer"],
                "romaji": "",
                "meaning": q.get("zh", ""),
                "notes": q.get("explain", ""),
                "example_ja": q["sentence"].replace("＿", q["answer"]),
                "example_zh": q.get("zh", ""),
            },
        })

    vocab = load_vocab()
    resolved = resolve_word(vocab, ref)
    if not resolved:
        return jsonify({"error": "单词不存在"}), 404
    lid, w = resolved

    if mode_def.get("order_mode"):
        # 组句：answer 是词块拼接的完整句，归一空白后与原例句精确比对
        expected = w.get("example_ja", "")
        correct = normalize_literal(str(answer)) == normalize_literal(expected)
        mismatch = []
    elif mode_def.get("conj_mode"):
        # 活用题：题目只下发变形名（conj_form），期望答案按 ref+form 从
        # conjugation.json 现查——服务端重算，单一事实源，题目不怕被翻
        entry = conj_index().get(ref)
        if not entry:
            return jsonify({"error": "该词条没有活用数据"}), 404
        form_key = data.get("form", "")
        if form_key not in conjugation.FORMS:
            return jsonify({"error": "未知的变形"}), 400
        expected = entry.get("forms", {}).get(form_key, "")
        # 两种备选写法都算对：
        #   kana_forms 全假名（ひらいて）——用户懒得切汉字输入法；
        #   alt_forms  汉字形（来て）——标准是假名全形时（カ変）的另一套写法。
        # 判分只做假名折叠、不会把汉字归一到假名，故两套都得显式给；
        # 与标准答案相同的那套会被去重（来る 的 kana_forms 就与 forms 完全一致）。
        #（选择题点选的选项文本就是标准答案本身，同一套比对也兼容）
        kana = entry.get("kana_forms", {}).get(form_key, "")
        alt = entry.get("alt_forms", {}).get(form_key, "")
        alternates = [a for a in (kana, alt) if a and a != expected]
        correct = check_answer(str(answer), expected, alternates, fold_kana=True)
        mismatch = [] if correct else classify_mismatch(str(answer), expected)
    else:
        expected = expected_for(w, mode_def)
        if mode_def.get("mcq_mode"):
            # 选择题：answer 是点选的选项文本，归一后精确比对。
            # 不走假名折叠与错因分类——释义/词形不是假名输入，写法提示无意义。
            correct = normalize_literal(str(answer)) == normalize_literal(str(expected))
            mismatch = []
        else:
            alternates = w.get("alternates", [])
            if mode_def.get("exact_answer"):
                # 写汉字题的备选只能是另一种汉字写法：词库里的 alternates 存的是
                # 读音别名（友人→ともだち 等 7 例），放过去就等于允许只打假名蒙过汉字题
                alternates = [a for a in alternates if has_kanji(a)]
            correct = check_answer(answer, expected, alternates,
                                   fold_kana=not mode_def.get("exact_answer"))
            mismatch = [] if correct else classify_mismatch(answer, expected)

    return jsonify({
        "correct": correct,
        "expected": expected,
        "mismatch": mismatch,
        "word": {
            "kanji": w.get("kanji", ""),
            "hiragana": w.get("hiragana", ""),
            "romaji": w.get("romaji", ""),
            "meaning": w.get("meaning", ""),
            "notes": w.get("notes", ""),
            "example_ja": w.get("example_ja", ""),
            "example_zh": w.get("example_zh", ""),
            # 用法讲解：答题后反馈卡复现，强化记忆
            "tip": w.get("tip", ""),
            # 反馈面板带配图（有图的词才有）：答错时图+词+义同时呈现，加深记忆
            "image": vocab_image_url(lid, w.get("id") or "") or "",
            # 掌握度（作答前）：掌握度在 /api/finish 才落盘，这里是本次作答前的档位，
            # 反馈卡据此着色——「这个词你原本有多熟」，不预判答后的升降
            "mastery": mastery_level(w),
            # 动词活用提示：反馈卡补一行「て形/ない形」（像 tip 一样强化，
            # 不写回词表；非动词是 None，前端不渲染）
            "conj": conj_hint(ref),
        },
    })


@app.route("/api/learn/start", methods=["POST"])
def api_learn_start():
    """学习模块：取一批未学新词（m=0，按词表顺序），供卡片教学。

    学习与练习的分工：学习建立第一印象（本接口只读词表），练习检验记忆。
    同时返回干扰项释义池（同课程其他词），供前端生成四选一再认小测。
    """
    cfg = request.get_json() or {}
    lesson_id = cfg.get("lesson", "all")
    try:
        count = max(1, min(20, int(cfg.get("count", LEARN_BATCH_DEFAULT)
                                  or LEARN_BATCH_DEFAULT)))
    except (TypeError, ValueError):
        count = LEARN_BATCH_DEFAULT

    vocab = load_vocab()
    lessons = vocab.get("lessons", {})
    target = [lesson_id] if lesson_id != "all" else list(lessons.keys())

    picked = []
    for lid in target:
        if lid not in lessons:
            continue
        for w in lessons[lid].get("words", []):
            if int(w.get("mastery") or 0) != 0:
                continue
            picked.append({"lesson_id": lid, "word": w})
            if len(picked) >= count:
                break
        if len(picked) >= count:
            break
    if not picked:
        return jsonify({"error": "该课程已没有未学的新词，直接去练习吧"}), 400

    words = []
    for c in picked:
        w = c["word"]
        words.append({
            "ref": f"{c['lesson_id']}:{w['id']}",
            "lesson_id": c["lesson_id"],
            "lesson_title": lessons[c["lesson_id"]].get("title", c["lesson_id"]),
            "word": {
                "kanji": w.get("kanji", ""),
                "hiragana": w.get("hiragana", ""),
                "romaji": w.get("romaji", ""),
                "meaning": w.get("meaning", ""),
                "notes": w.get("notes", ""),
                "example_ja": w.get("example_ja", ""),
                "example_zh": w.get("example_zh", ""),
                # 用法讲解/近义辨析小知识（有则展示，帮建立记忆钩子）
                "tip": w.get("tip", ""),
                # 学习卡片带配图（有图的词才有），建立第一印象
                "image": vocab_image_url(c["lesson_id"], w.get("id") or "") or "",
                # 学新词取的都是 m=0，前端标注「新」；口径与其他接口保持一致
                "mastery": mastery_level(w),
            },
        })

    # 干扰项池：目标课程里其他词的释义。按每个词各自过滤义项重叠者——
    # 「漂亮的、干净的」与「漂亮的，很棒的」是两个字符串，却都算正确项。
    picked_keys = {(c["lesson_id"], c["word"].get("id")) for c in picked}
    others = [w for lid in target if lid in lessons
              for w in lessons[lid].get("words", [])
              if (lid, w.get("id")) not in picked_keys]
    pool = _mcq_pool(others, get_mode("kana_to_cn"))
    random.shuffle(pool)
    for item, c in zip(words, picked):
        own = gloss_senses(c["word"].get("meaning", ""))
        item["distractors"] = [t for t, s in pool if not (own & s)][:12]

    return jsonify({
        "lesson": lesson_id,
        "words": words,
    })


@app.route("/api/learn/finish", methods=["POST"])
def api_learn_finish():
    """学习完成：本批词标记「见过」（m 0→1），今天到期（学完即可练）。

    操作天然幂等（已是 1 的不再动），无需 session_id。
    写一条 mode=learn 的轻量记录供统计页展示学习活动；
    results 留空——学习不算对错，不能混进错题本与正确率。
    """
    data = request.get_json() or {}
    lesson_id = data.get("lesson", "all")
    refs = data.get("refs", []) or []

    with _DATA_LOCK:
        vocab = load_vocab()
        today = datetime.now().strftime("%Y-%m-%d")
        learned = []
        for ref in refs:
            resolved = resolve_word(vocab, ref)
            if not resolved:
                continue
            w = resolved[1]
            if int(w.get("mastery") or 0) == 0:
                w["mastery"] = 1
            w["last_reviewed"] = today
            w["next_due"] = datetime.now().date().isoformat()
            learned.append(ref)
        if learned:
            save_vocab(vocab)

        QUIZZES_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        record = {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "learn",
            "lesson": lesson_id,
            "total": len(learned),
            "correct": len(learned),
            "accuracy": 1.0 if learned else 0.0,
            "max_streak": 0,
            "avg_reaction_ms": 0,
            "results": [],
        }
        fname = now.strftime("%Y%m%d_%H%M%S") + f"_{now.microsecond // 1000:03d}_learn.json"
        _atomic_write(QUIZZES_DIR / fname, _dump_json(record))

    # 必须显式返回：视图没有返回值会让 Flask 抛「did not return a valid response」，
    # 前端拿到 500 HTML 页后 res.json() 报 Unexpected token '<'——而此时词表与记录
    # 其实都已写盘，用户只看到「保存学习记录失败」，以为白学了一遍。
    return jsonify({"saved": True, "learned": len(learned)})


# ---------- 前端新增单词 ----------
# 默认生词本：前端不选课程时落到这里，首次添加自动创建
DEFAULT_USER_LESSON = "lesson_mywords"
DEFAULT_USER_LESSON_TITLE = "我的生词本"
# 用户主动「新建课程」时的内部 id 前缀（标题用用户填的中文名）
NEW_LESSON_PREFIX = "lesson_user_"
# 手输字段长度上限，防误粘贴整段文本污染词表
ADD_WORD_MAXLEN = 200


def _clean_word_field(value):
    """清洗用户手输字段：转字符串、NFKC 归一（半角片假名等收敛到全角）、去首尾空白。

    不删内部空白（例句/释义可能含正常间隔）；判分链路另有 normalize_kana
    折叠片假名与空白，这里只保证落盘文本是规范形态。
    """
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def _clean_zh_field(value):
    """清洗**中文**字段（释义/备注/例句中文/用法讲解）：只去首尾空白，不做 NFKC。

    NFKC 会把中文全角标点折成半角：「那边，那里（礼貌说法）」存进去变成
    「那边,那里(礼貌说法)」，与 CSV 导入的词条书写风格不一致（词库里既有的是
    全角）。假名/日文字段仍需 NFKC 做半角片假名归一，所以两类字段分开洗。
    """
    if value is None:
        return ""
    return str(value).strip()


# ---------- 新增单词时的相近词匹配 ----------
# 词库里可能已有这个词（或它的近形，如 中国 / 中国語）。匹配按写法与读音的
# 「相等 / 互相包含」打分，不含释义（中文同义表达太散，子串匹配噪声大）。
def _word_similarity(word, kanji, hiragana):
    """候选词与输入的相近度：返回 (score, relation)，score<50 视为不相关。

    相等始终计入（哪怕单字，如「一」）；互相包含要求较短一侧 ≥2 字，
    否则「一」会把 一月/一日/一人 全拉进来。写法信号权重高于读音
    （汉字词靠写法认，假名词写法字段多为 "---"，靠读音兜底）。
    """
    w_kanji = (word.get("kanji") or "").strip()
    w_hira = (word.get("hiragana") or "").strip()
    best, relation = 0, ""
    if kanji and w_kanji and w_kanji != "---":
        if w_kanji == kanji:
            best, relation = 80, "写法相同"
        elif (kanji in w_kanji or w_kanji in kanji) and min(len(kanji), len(w_kanji)) >= 2:
            best, relation = 60, "写法相近"
    if hiragana and w_hira:
        if w_hira == hiragana:
            if best < 70:
                best, relation = 70, "读音相同"
        elif (hiragana in w_hira or w_hira in hiragana) \
                and min(len(hiragana), len(w_hira)) >= 2 and best < 50:
            best, relation = 50, "读音相近"
    if kanji and w_kanji == kanji and hiragana and w_hira == hiragana:
        best, relation = 90, "完全相同"
    return best, relation


@app.route("/api/words/similar", methods=["POST"])
def api_words_similar():
    """新增单词时查找词库里的相近词，供用户选择「改已有」还是「照常新增」。"""
    data = request.get_json() or {}
    kanji = _clean_word_field(data.get("kanji"))
    hiragana = _clean_word_field(data.get("hiragana"))
    exclude_ref = _clean_word_field(data.get("exclude_ref"))
    if not kanji and not hiragana:
        return jsonify({"similar": []})

    vocab = load_vocab()
    hits = []
    for lid, ldata in vocab.get("lessons", {}).items():
        lesson_title = ldata.get("title", lid)
        for w in ldata.get("words", []):
            ref = f"{lid}:{w.get('id', '')}"
            if ref == exclude_ref:
                continue
            score, relation = _word_similarity(w, kanji, hiragana)
            if score < 50:
                continue
            k = w.get("kanji", "")
            hits.append({
                "ref": ref,
                "lesson_id": lid,
                "lesson_title": lesson_title,
                "kanji": k,
                "hiragana": w.get("hiragana", ""),
                "meaning": w.get("meaning", ""),
                "notes": w.get("notes", ""),
                "example_ja": w.get("example_ja", ""),
                "example_zh": w.get("example_zh", ""),
                "tip": w.get("tip", ""),
                "display": k if k and k != "---" else w.get("hiragana", ""),
                "score": score,
                "relation": relation,
            })
    hits.sort(key=lambda x: -x["score"])
    return jsonify({"similar": hits[:8]})


@app.route("/api/words/edit", methods=["POST"])
def api_words_edit():
    """修改已有词条：更新写法/读音/释义/备注/例句，id 与学习进度原样保留。

    读音或例句变更后，按 ref 缓存的 TTS 音频就过时了（文件名只含 ref），
    对应缓存删掉，下次播放自动按新文本重生成。
    """
    data = request.get_json() or {}
    ref = _clean_word_field(data.get("ref"))
    kanji = _clean_word_field(data.get("kanji")) or "---"
    hiragana = _clean_word_field(data.get("hiragana"))
    meaning = _clean_zh_field(data.get("meaning"))
    romaji = _clean_word_field(data.get("romaji"))
    notes = _clean_zh_field(data.get("notes"))
    example_ja = _clean_word_field(data.get("example_ja"))
    example_zh = _clean_zh_field(data.get("example_zh"))
    # 中文长句：只去首尾空白，不做 NFKC——否则中文全角标点，。；（）会被折成半角
    tip = _clean_zh_field(data.get("tip"))
    # 例句键缺省 = 保持原样（调用方可能只改释义）；传了键（含空串）= 更新或清除
    update_example = "example_ja" in data or "example_zh" in data
    # 讲解键缺省 = 保持原样；传了键（含空串）= 更新或清除
    update_tip = "tip" in data

    if not parse_ref(ref):
        return jsonify({"error": "无效的单词引用"}), 400
    if not hiragana:
        return jsonify({"error": "请填写读音（平假名，也可用罗马字直接输入）"}), 400
    if not meaning:
        return jsonify({"error": "请填写中文释义"}), 400
    field_labels = (
        ("写法", kanji), ("读音", hiragana), ("释义", meaning), ("备注", notes),
        ("例句日语", example_ja), ("例句中文", example_zh), ("用法讲解", tip),
    )
    for label, val in field_labels:
        if len(val) > ADD_WORD_MAXLEN:
            return jsonify({"error": f"{label}过长（上限 {ADD_WORD_MAXLEN} 字）"}), 400

    with _DATA_LOCK:
        vocab = load_vocab()
        resolved = resolve_word(vocab, ref)
        if not resolved:
            return jsonify({"error": "单词不存在"}), 404
        lesson_id, w = resolved
        old_hiragana = w.get("hiragana", "")
        old_kanji = w.get("kanji", "")
        old_example = w.get("example_ja", "")
        w["kanji"] = kanji
        w["hiragana"] = hiragana
        w["romaji"] = romaji
        w["meaning"] = meaning
        w["notes"] = notes
        if update_example:
            # 例句可选：填了才写键，与新增词条的最小字段集口径一致
            if example_ja:
                w["example_ja"] = example_ja
                w["example_zh"] = example_zh
            else:
                w.pop("example_ja", None)
                w.pop("example_zh", None)
        if update_tip:
            if tip:
                w["tip"] = tip
            else:
                w.pop("tip", None)
        save_vocab(vocab)
        if hiragana != old_hiragana or kanji != old_kanji:
            # 朗读文本优先用汉字写法（tts_word_text），所以只改写法也得失效缓存，
            # 否则改完错别字听到的还是旧念法
            audio_path_for_ref(ref).unlink(missing_ok=True)
        if w.get("example_ja", "") != old_example:
            (AUDIO_DIR / (audio_path_for_ref(ref).stem + "_ex.mp3")).unlink(missing_ok=True)
        lesson_title = vocab["lessons"][lesson_id].get("title", lesson_id)

    return jsonify({
        "ok": True,
        "ref": ref,
        "lesson_id": lesson_id,
        "lesson_title": lesson_title,
        "word": w,
    })


@app.route("/api/words/add", methods=["POST"])
def api_add_word():
    """前端「新增单词」：把一个词条写入指定课程，新课程按需自动创建。

    词条模板与 practice/import_csv.py 保持一致（mastery=0、字段口径相同），
    判重也沿用同一套（写法, 读音, 释义）三元组：
    - 读音 hiragana、释义 meaning 必填；写法 kanji 留空记 "---"；
    - 课程三选一：已有课程 id / 默认生词本（不存在自动建）/ create_new+新课程名；
    - 稳定 id 由 ensure_word_ids 分配，新词 mastery=0，自然进入间隔重复队列；
    - 全程持 _DATA_LOCK，与练习交卷的 load→modify→save 互斥，杜绝并发覆盖进度。
    """
    data = request.get_json() or {}
    kanji = _clean_word_field(data.get("kanji")) or "---"
    hiragana = _clean_word_field(data.get("hiragana"))
    meaning = _clean_zh_field(data.get("meaning"))
    romaji = _clean_word_field(data.get("romaji"))
    notes = _clean_zh_field(data.get("notes"))
    example_ja = _clean_word_field(data.get("example_ja"))
    example_zh = _clean_zh_field(data.get("example_zh"))
    # 中文长句：只去首尾空白，不做 NFKC——否则中文全角标点，。；（）会被折成半角
    tip = _clean_zh_field(data.get("tip"))

    if not hiragana:
        return jsonify({"error": "请填写读音（平假名，也可用罗马字直接输入）"}), 400
    if not meaning:
        return jsonify({"error": "请填写中文释义"}), 400
    field_labels = (
        ("写法", kanji), ("读音", hiragana), ("释义", meaning), ("备注", notes),
        ("例句日语", example_ja), ("例句中文", example_zh), ("用法讲解", tip),
    )
    for label, val in field_labels:
        if len(val) > ADD_WORD_MAXLEN:
            return jsonify({"error": f"{label}过长（上限 {ADD_WORD_MAXLEN} 字）"}), 400

    lesson_id = _clean_word_field(data.get("lesson"))
    new_lesson_title = _clean_word_field(data.get("new_lesson_title"))
    create_new = bool(data.get("create_new")) or lesson_id == "__new__"

    with _DATA_LOCK:
        vocab = load_vocab()
        lessons = vocab.setdefault("lessons", {})

        if create_new:
            if not new_lesson_title:
                return jsonify({"error": "请填写新课程名称"}), 400
            if len(new_lesson_title) > ADD_WORD_MAXLEN:
                return jsonify({"error": "课程名称过长"}), 400
            # 同名课程直接并入，避免出现两个标题相同、分不清的课
            same_title = next((lid for lid, ld in lessons.items()
                               if ld.get("title", "") == new_lesson_title), None)
            if same_title:
                lesson_id = same_title
            else:
                used_nums = [
                    int(m.group(1))
                    for lid in lessons
                    for m in [re.fullmatch(NEW_LESSON_PREFIX + r"(\d+)", lid)] if m
                ]
                lesson_id = f"{NEW_LESSON_PREFIX}{(max(used_nums) + 1) if used_nums else 1:02d}"
                lessons[lesson_id] = {"title": new_lesson_title, "words": []}
        else:
            # 未指定课程、或误指向错题本这类虚拟课程：回落到默认生词本
            if not lesson_id or lesson_id == WRONG_LESSON_ID:
                lesson_id = DEFAULT_USER_LESSON
            if lesson_id not in lessons:
                if lesson_id == DEFAULT_USER_LESSON:
                    lessons[lesson_id] = {"title": DEFAULT_USER_LESSON_TITLE, "words": []}
                else:
                    return jsonify({"error": f"课程不存在：{lesson_id}"}), 400

        ldata = lessons[lesson_id]
        words = ldata.setdefault("words", [])
        lesson_title = ldata.get("title", lesson_id)

        # 同课内三元组判重（与 import_csv.py 同口径），重复不落盘
        dup_key = (kanji, hiragana, meaning)
        for w in words:
            existed = (w.get("kanji", ""), w.get("hiragana", ""), w.get("meaning", ""))
            if existed == dup_key:
                return jsonify({
                    "duplicate": True,
                    "error": f"该词已在课程「{lesson_title}」中，未重复添加",
                    "lesson_id": lesson_id,
                    "lesson_title": lesson_title,
                    "ref": f"{lesson_id}:{w.get('id', '')}",
                    "word_count": len(words),
                }), 409

        word = {
            "kanji": kanji,
            "hiragana": hiragana,
            "romaji": romaji,
            "meaning": meaning,
            "mastery": 0,
            "last_reviewed": None,
            "next_due": None,
            "notes": notes,
        }
        # 例句可选：填了才写键，保持与导入词条一致的最小字段集
        if example_ja:
            word["example_ja"] = example_ja
            word["example_zh"] = example_zh
        if tip:
            word["tip"] = tip
        words.append(word)
        ensure_word_ids(vocab)
        save_vocab(vocab)
        new_ref = f"{lesson_id}:{word['id']}"
        word_count = len(words)

    return jsonify({
        "ok": True,
        "ref": new_ref,
        "lesson_id": lesson_id,
        "lesson_title": lesson_title,
        "word_count": word_count,
        "word": word,
    })


@app.route("/api/lesson/rename", methods=["POST"])
def api_rename_lesson():
    """前端「重命名课程」：只改 lessons.<id>.title 一个字段。

    课程名只存在于 vocabulary.json 的 lessons.<id>.title，其他一切（单词 ref、
    TTS 音频缓存、单词配图、练习记录）都按 lesson_id 关联，所以改名不需要
    同步任何数据——练习记录里留着当时的课程名，那是历史快照，改了反而对不上。
    与新增单词的同名策略一致：标题相同的两门课在下拉里分不清是谁，故拒绝。
    """
    data = request.get_json() or {}
    lesson_id = _clean_word_field(data.get("lesson_id"))
    # 课程名与新增课程的标题同一口径：NFKC 归一 + 去首尾空白（与判重比对一致）
    title = _clean_word_field(data.get("title"))

    if not lesson_id or lesson_id in ("all", WRONG_LESSON_ID):
        return jsonify({"error": "请选择一个课程"}), 400
    if not title:
        return jsonify({"error": "请填写新的课程名称"}), 400
    if len(title) > ADD_WORD_MAXLEN:
        return jsonify({"error": f"课程名称过长（上限 {ADD_WORD_MAXLEN} 字）"}), 400

    with _DATA_LOCK:
        vocab = load_vocab()
        lessons = vocab.setdefault("lessons", {})
        if lesson_id not in lessons:
            return jsonify({"error": f"课程不存在：{lesson_id}"}), 404
        clash = next((lid for lid, ld in lessons.items()
                      if lid != lesson_id and ld.get("title", "") == title), None)
        if clash:
            return jsonify({"error": f"已有同名课程「{title}」，换一个名字吧"}), 409
        old_title = lessons[lesson_id].get("title", lesson_id)
        lessons[lesson_id]["title"] = title
        save_vocab(vocab)

    return jsonify({
        "ok": True,
        "lesson_id": lesson_id,
        "title": title,
        "old_title": old_title,
    })


@app.route("/api/tts")
def api_tts():
    """返回指定单词的音频。首次请求时生成，之后读缓存文件。"""
    from flask import send_file

    ref = request.args.get("ref", "")
    if not ref:
        return jsonify({"error": "缺少 ref 参数"}), 400

    parsed = parse_ref(ref)
    if not parsed:
        return jsonify({"error": "无效的 ref"}), 400
    lesson_id, idx = parsed

    vocab = load_vocab()
    resolved = resolve_word(vocab, ref)
    if not resolved:
        return jsonify({"error": "单词不存在"}), 404
    w = resolved[1]

    # field=word（默认）：朗读读音（个别词走 TTS 文本覆盖表）；field=example：朗读例句（无例句返回 404）
    field = request.args.get("field", "word")
    if field == "example":
        text = clean_tts_text(w.get("example_ja", ""))
        audio_path = AUDIO_DIR / (audio_path_for_ref(ref).stem + "_ex.mp3")
    else:
        text = tts_word_text(w)
        audio_path = audio_path_for_ref(ref)
    if not text:
        return jsonify({"error": "该词条暂无例句" if field == "example" else "无有效文本"}), 404

    audio_path = ensure_audio(ref, text, out_path=audio_path)
    if not audio_path:
        return jsonify({"error": "音频生成失败"}), 500

    # max_age=0：文件被「重置语音」覆盖后，浏览器必须重新验证而不是拿启发式缓存
    return send_file(str(audio_path), mimetype="audio/mpeg", max_age=0)


# ---------- 重置语音：练习中直接重生成当前词的发音 ----------
# 流程：regen 生成到 .regen 临时文件（现有缓存不动）→ 前端试听 base64 →
# apply 决定覆盖（临时文件转正）或保留（删临时文件）。
@app.route("/api/tts/regen", methods=["POST"])
def api_tts_regen():
    """重新生成指定单词的发音，写入临时文件并返回 base64 供试听。

    不直接覆盖现有缓存：用户试听新版后自行决定是否替换（apply）。
    generate_audio_sync 内部会多次生成取最完整的一版（治 edge-tts 断流丢开头）。
    """
    import base64

    data = request.get_json() or {}
    ref = data.get("ref", "")
    vocab = load_vocab()
    resolved = resolve_word(vocab, ref)
    if not resolved:
        return jsonify({"error": "该词没有可重新生成的发音"}), 404
    w = resolved[1]
    cleaned = clean_tts_text(tts_word_text(w))
    if not cleaned:
        return jsonify({"error": "该词没有可朗读的文本"}), 400
    main_path = audio_path_for_ref(ref)
    temp_path = AUDIO_DIR / (main_path.stem + ".regen.mp3")
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    if not generate_audio_sync(cleaned, temp_path):
        return jsonify({"error": "语音生成失败，请稍后再试"}), 500
    b64 = base64.b64encode(temp_path.read_bytes()).decode("ascii")
    return jsonify({
        "ok": True,
        "audio": b64,
        "size": temp_path.stat().st_size,
    })


@app.route("/api/tts/regen/apply", methods=["POST"])
def api_tts_regen_apply():
    """确认重置结果：keep=new 把临时文件转正覆盖缓存，keep=old 丢弃新版。"""
    data = request.get_json() or {}
    ref = data.get("ref", "")
    keep = data.get("keep", "old")
    main_path = audio_path_for_ref(ref)
    temp_path = AUDIO_DIR / (main_path.stem + ".regen.mp3")
    if not temp_path.exists():
        return jsonify({"error": "没有待确认的新语音，请先点「重置语音」"}), 404
    if keep == "new":
        temp_path.replace(main_path)
        return jsonify({"ok": True, "replaced": True})
    temp_path.unlink(missing_ok=True)
    return jsonify({"ok": True, "replaced": False})


def find_last_record(mode_id, lesson_id, exclude_path=None):
    """查找最近一次同 mode + lesson 的练习记录（不含 exclude_path）。

    返回 {timestamp, total, correct, accuracy, max_streak, avg_reaction_ms} 或 None。
    """
    if not QUIZZES_DIR.exists():
        return None
    candidates = []
    for f in QUIZZES_DIR.glob("*.json"):
        if exclude_path and f.resolve() == exclude_path.resolve():
            continue
        try:
            with open(f, "r", encoding="utf-8") as fp:
                rec = json.load(fp)
        except (json.JSONDecodeError, OSError):
            continue
        if rec.get("mode") != mode_id or rec.get("lesson") != lesson_id:
            continue
        candidates.append((f, rec))
    if not candidates:
        return None
    # 按文件名（含时间戳）降序，取最新
    candidates.sort(key=lambda x: x[0].name, reverse=True)
    _, rec = candidates[0]
    return {
        "timestamp": rec.get("timestamp", ""),
        "total": rec.get("total", 0),
        "correct": rec.get("correct", 0),
        "accuracy": rec.get("accuracy", 0.0),
        "max_streak": rec.get("max_streak", 0),
        "avg_reaction_ms": rec.get("avg_reaction_ms", 0),
    }


@app.route("/api/finish", methods=["POST"])
def api_finish():
    data = request.get_json() or {}
    mode_id = data.get("mode", "kanji_to_kana")
    mode_def = get_mode(mode_id)
    # mode_id 要进记录文件名：不校验就等于把文件名交给调用方（与 session_id
    # 同一套理由）。"a/../../vocabulary" 这种值会让原子写把词表整个覆盖成一条
    # 练习记录，还照样返回 200——练习数据全丢且没有任何报错迹象。
    if mode_def is None:
        return jsonify({"error": f"未知的练习模式：{mode_id}"}), 400
    # 按 ref 去重是为防同一题把掌握度加两次；活用模式不写掌握度（no_mastery），
    # 同一词「四选一+打字」两行同 ref 结果都要保留进统计与错题本
    no_mastery = bool(mode_def.get("no_mastery"))
    lesson_id = data.get("lesson", "all")
    # 只认 dict 条目：results 里混进裸字符串/数字会让下面每处 r.get 抛 AttributeError
    results = [r for r in (data.get("results") or []) if isinstance(r, dict)]
    max_streak = _int_field(data.get("max_streak"), 0, lo=0)
    avg_reaction_ms = _num_field(data.get("avg_reaction_ms"), 0.0)
    # session_id 只允许字母数字（要进文件名与 glob 模式，不接受其他字符）
    session_id = re.sub(r"[^0-9a-zA-Z]", "", str(data.get("session_id") or ""))

    # results 按 ref 去重（保留最后一次）：双击/重试可能带出重复条目，
    # 同一题不能把掌握度加两次。重点词模式会故意让同一个词在一组里出多遍
    # （题量大于清单时循环补齐），这时最后一次作答才代表「现在会不会」，
    # 所以留最后一条（内容完全相同的重复条目不受影响）。
    # 活用模式不写掌握度，没有重复计分问题：不去重，保住每题的统计。
    if not no_mastery:
        seen_refs = set()
        uniq_results = []
        for r in reversed(results):
            ref = r.get("ref", "") if isinstance(r, dict) else ""
            if ref:
                if ref in seen_refs:
                    continue
                seen_refs.add(ref)
            uniq_results.append(r)
        uniq_results.reverse()
        results = uniq_results

    with _DATA_LOCK:
        return _finish_locked(data, mode_id, lesson_id, results,
                              max_streak, avg_reaction_ms, session_id)


def _apply_answer(obj, correct, streak_gate, today_str, today_date):
    """按一次作答就地更新词条/题目的 mastery、last_reviewed、next_due。

    streak_gate=True（真题与 MCQ 选择题类）时收紧计分：**连对两次才 +1**——
    四选一纯蒙的连对两次概率只有 1/16，蒙题涨不动掌握度；
    未凑满两次前明天到期继续练，答错清零连对计数。
    掌握度从 0 首次晋升先用 1 天巩固步，其余按新掌握度间隔；答错固定明天。
    """
    m_old = int(obj.get("mastery") or 0)
    if correct:
        if streak_gate:
            streak = int(obj.get("streak_ok") or 0) + 1
            if streak < 2:
                obj["streak_ok"] = streak
                obj["mastery"] = m_old
                obj["last_reviewed"] = today_str
                obj["next_due"] = (today_date + timedelta(days=1)).isoformat()
                return
            obj["streak_ok"] = 0
        m = min(MASTERY_MAX, m_old + 1)
        # 首次晋升（掌握度从 0 升起）先用 1 天巩固步；
        # 直接取新掌握度的间隔会让新词第一次复习就跳到 2 天后
        interval = 1 if m_old == 0 else INTERVAL_DAYS.get(m, 1)
    else:
        if streak_gate:
            obj["streak_ok"] = 0
        m = max(MASTERY_MIN, m_old - 1)
        interval = 1
    obj["mastery"] = m
    obj["last_reviewed"] = today_str
    obj["next_due"] = (today_date + timedelta(days=interval)).isoformat()


def _finish_locked(data, mode_id, lesson_id, results,
                   max_streak, avg_reaction_ms, session_id):
    """api_finish 主体，须持有 _DATA_LOCK 调用。"""
    today = datetime.now().strftime("%Y-%m-%d")
    today_date = datetime.now().date()
    mode_def = get_mode(mode_id)

    # 幂等保护（必须在应用掌握度之前）：同一 session 已交过卷（双击/网络重试）
    # 直接返回已有结果，不再应用一遍掌握度更新
    if session_id:
        dup = next(QUIZZES_DIR.glob(f"*_{session_id}_*.json"), None)
        if dup is not None:
            try:
                with open(dup, "r", encoding="utf-8") as fp:
                    prev = json.load(fp)
            except (json.JSONDecodeError, OSError):
                prev = None
            if prev:
                last = find_last_record(mode_id, lesson_id, exclude_path=dup)
                return jsonify({
                    "saved": True,
                    "duplicate": True,
                    "record_file": str(dup.relative_to(BASE_DIR)).replace("\\", "/"),
                    "stats": {
                        "total": prev.get("total", 0),
                        "correct": prev.get("correct", 0),
                        "accuracy": prev.get("accuracy", 0.0),
                        "max_streak": prev.get("max_streak", 0),
                        "avg_reaction_ms": prev.get("avg_reaction_ms", 0),
                    },
                    "last": last,
                })

    correct_count = 0

    if mode_id in ("particle", "exam"):
        # 独立题库：更新对应 JSON 里的掌握度与调度。
        # 计分收紧只对真题（选择题）生效，助词是打字题维持答对即 +1
        if mode_id == "particle":
            bdata = load_particles()
            saver = save_particles
            prefix = "particle"
        else:
            bdata = load_exam()
            saver = save_exam
            prefix = "exam"
        for r in results:
            parsed = parse_ref(r.get("ref", ""))
            if not parsed or parsed[0] != prefix:
                continue
            q = next((x for x in bdata.get("questions", []) if x.get("id") == parsed[1]), None)
            if not q:
                continue
            correct = bool(r.get("correct", False))
            _apply_answer(q, correct, streak_gate=(prefix == "exam"), today_str=today,
                          today_date=today_date)
            if correct:
                correct_count += 1
        saver(bdata)
    else:
        vocab = load_vocab()
        # MCQ 模式（看词/听音选意思、例句填空）与词义配对同样收紧；打字/闪卡模式不变。
        # 课程模式的掌握度规则按每题的内层题型（results 带 qtype）决定：
        # 选择题连对两次才 +1，打字/组句答对即 +1。
        # 活用题掌握度独立统计（拍板口径）：独立模式整场不写，
        # 课程内层的活用题（qtype=conjugation）同样跳过。
        no_mastery = bool(mode_def and mode_def.get("no_mastery"))
        streak_gate = bool(mode_def and (mode_def.get("mcq_mode")
                                         or mode_def.get("streak_gate")))
        for r in results:
            resolved = resolve_word(vocab, r.get("ref", ""))
            if not resolved:
                continue
            w = resolved[1]
            correct = bool(r.get("correct", False))
            qtype = str(r.get("qtype") or "")
            qdef = get_mode(qtype) if qtype else None
            skip = no_mastery or bool(qdef and qdef.get("no_mastery"))
            if not skip:
                gate = (bool(qdef and (qdef.get("mcq_mode") or qdef.get("streak_gate")))
                        if qdef else streak_gate)
                _apply_answer(w, correct, streak_gate=gate, today_str=today,
                              today_date=today_date)
            if correct:
                correct_count += 1
        save_vocab(vocab)

    # 保存测验记录到 quizzes/
    QUIZZES_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    total = len(results)
    accuracy = round(correct_count / total, 4) if total else 0.0
    record = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode_id,
        "lesson": lesson_id,
        "total": total,
        "correct": correct_count,
        "accuracy": accuracy,
        "max_streak": max_streak,
        "avg_reaction_ms": round(avg_reaction_ms, 1),
        "results": results,
        # 课程模式：课末错题回炉的重做次数（不计掌握度，仅留档）
        "review_count": _int_field(data.get("review_count"), 0, lo=0),
    }
    # 文件名带毫秒与 session_id：同秒两份记录不再互相覆盖；
    # session_id 入文件名让重复交卷可被识别（幂等，掌握度不二次累加）
    fname = (now.strftime("%Y%m%d_%H%M%S") + f"_{now.microsecond // 1000:03d}"
             + (f"_{session_id}" if session_id else "") + f"_{mode_id}.json")
    rec_path = QUIZZES_DIR / fname

    # 练习记录同样不可再生（错题本/统计页/report 都依赖它），走原子写避免半截 JSON
    _atomic_write(rec_path, _dump_json(record))

    # 查找上次同条件记录，用于对比
    last = find_last_record(mode_id, lesson_id, exclude_path=rec_path)

    return jsonify({
        "saved": True,
        "record_file": str(rec_path.relative_to(BASE_DIR)).replace("\\", "/"),
        "stats": {
            "total": total,
            "correct": correct_count,
            "accuracy": accuracy,
            "max_streak": max_streak,
            "avg_reaction_ms": round(avg_reaction_ms, 1),
        },
        "last": last,
    })


# ====================================================================
# 作品沉浸：阅读器
# --------------------------------------------------------------------
# 小说 / 字幕 / 漫画都归约为「句子流」存在 data/works/{id}/content/chNN.json，
# 里面**只有原文**——与词库的对齐（哪些词学过、掌握度多少）由 works.py 在
# 请求时实时算，所以挖矿加的新词、练习里涨的掌握度，下一次打开阅读器
# 立即生效，不需要重新导入。
# ====================================================================
# 句子朗读缓存目录（按内容哈希，随读随建，不入 git）
SENT_AUDIO_DIR = AUDIO_DIR / "text"
# 单句朗读的长度上限：整段文本粘进来会生成一个又长又用不上的音频
TTS_TEXT_MAXLEN = 200
# 界面导入作品的文件大小上限（整本轻小说 txt / epub 一般 <10MB）
WORK_UPLOAD_MAX_BYTES = 25 * 1024 * 1024
# 音声作品压缩包的上限：zip 里装着音频本体（mp3 几十~几百 MB / wav 更大），
# 不能拿轻小说的 25MB 限额卡——这里给了 2GB 的口子
VOICE_UPLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024


# ---------- 本地词典（build_dict_zh.py 预翻落盘，供阅读器挖矿预填） ----------
# 加载时只留需要的字段（写法/读音/义项/词性），丢掉英文释义与冗余 id——29MB 的
# 原始 JSON 直接常驻太占内存，精简后只有几 MB。
_DICT_CACHE = {"key": None, "by_kanji": {}, "by_kana": {}}


def dict_index():
    """data/dict_zh.json 的查询索引，返回 (按写法, 按读音)；无文件时都为空。"""
    path = BASE_DIR / "data" / "dict_zh.json"
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        _DICT_CACHE.update(key=None, by_kanji={}, by_kana={})
        return _DICT_CACHE["by_kanji"], _DICT_CACHE["by_kana"]
    if _DICT_CACHE["key"] != key:
        raw = json.loads(path.read_text(encoding="utf-8")).get("words", {})
        by_kanji, by_kana = {}, {}
        for e in raw.values():
            kanji = (e.get("kanji") or "").strip()
            kana = (e.get("kana") or "").strip()
            senses = [{"zh": s.get("zh", ""), "pos": s.get("pos") or []}
                      for s in (e.get("senses") or []) if s.get("zh")]
            if not (kanji or kana) or not senses:
                continue
            entry = {"kanji": kanji, "kana": kana, "senses": senses}
            if kanji:
                by_kanji.setdefault(kanji, []).append(entry)
            if kana:
                by_kana.setdefault(kana, []).append(entry)
        _DICT_CACHE.update(key=key, by_kanji=by_kanji, by_kana=by_kana)
    return _DICT_CACHE["by_kanji"], _DICT_CACHE["by_kana"]


def dict_lookup(surface, base="", reading=""):
    """按「原形 → 词形 → 读音」依次查，返回候选词条（同形异读会有多个）。

    词典里存的是基本形，而阅读器点到的多半是活用形（走った），所以先用 janome
    还原出的原形查；查不到再退回词形本身与读音（送り仮名差异、纯假名词都靠它兜）。
    """
    by_kanji, by_kana = dict_index()
    for key in (base, surface):
        if key and by_kanji.get(key):
            return by_kanji[key][:4]
    # 词形不是汉字时（纯假名词、片假名外来语），写法本身就是读音：
    # surface 也得拿去查读音索引，否则「みちくさ」这种词永远查不到
    for key in (reading, surface, base):
        if key and by_kana.get(key):
            return by_kana[key][:4]
    return []


def _word_card(vocab, ref):
    """词条 → 阅读器弹卡字段（缺字段留空，不抛异常）。

    配图与掌握度都在这里带上：卡片一次取全，前端不必再为弹卡打第二个接口。
    """
    resolved = resolve_word(vocab, ref)
    if not resolved:
        return None
    lid, w = resolved
    return {
        "ref": ref,
        "kanji": w.get("kanji", ""),
        "hiragana": w.get("hiragana", ""),
        "meaning": w.get("meaning", ""),
        "notes": w.get("notes", ""),
        "example_ja": w.get("example_ja", ""),
        "example_zh": w.get("example_zh", ""),
        "mastery": mastery_level(w),
        "lesson_title": vocab.get("lessons", {}).get(lid, {}).get("title", lid),
        "image": vocab_image_url(lid, w.get("id", "")),
    }


def annotate_chapter(vocab, work, n):
    """章节原文 → 阅读器渲染结构（段落 → 句子 → 词块）。

    返回 (payload, None) 或 (None, 错误信息)。
    """
    chapters = work.get("chapters", [])
    if not (isinstance(n, int) and 0 <= n < len(chapters)):
        return None, "章节不存在"
    raw = works.load_chapter_raw(work.get("id", ""), n)
    if not raw:
        return None, "章节内容缺失（导入时损坏？）"

    # 音声作品：句子带 t0/t1（这句在音轨里的起止秒数）。长度对不上就当没有
    # 时间轴——错位比没有更糟（点一句播的是另一句），退回 TTS 朗读即可
    times = voice.flat_times(raw.get("times"), raw.get("paras"))

    form_map, max_len = works.build_form_map(vocab)
    ud = works.load_user_tokens()
    fp = works.form_fingerprint(form_map)
    # 吃落盘快照 / 进程内句子缓存：同一句只分词一次，服务重启后也不用重扫
    works.ensure_disk_annot(work.get("id", ""), fp, ud.get("stamp", 0))
    mastery = works.mastery_map(vocab)
    paras = []
    words = {}
    sid = 0
    sents = known_sents = hits = 0

    for para in raw.get("paras", []):
        rendered = []
        for text in para:
            ann = works.analyze_sentence_cached(text, form_map, max_len, ud, fp, mastery)
            refs = []
            has_known = False
            for seg in ann["segs"]:
                ref = seg.get("w")
                if not ref:
                    continue
                hits += 1
                if ref not in refs:
                    refs.append(ref)
                if int(seg.get("m") or 0) >= 1:
                    has_known = True
                if ref not in words:
                    card = _word_card(vocab, ref)
                    if card:
                        words[ref] = card
            sents += 1
            known_sents += 1 if has_known else 0
            item = {
                "id": sid,
                "text": text,
                "segs": ann["segs"],
                "known": refs,
                "has_known": has_known,
            }
            if times:
                item["t0"], item["t1"] = times[sid]
            rendered.append(item)
            sid += 1
        if rendered:
            paras.append(rendered)

    jp_chars = sum(1 for c in "".join(s for p in paras for s in [x["text"] for x in p])
                   if 0x3000 <= ord(c) <= 0x30FF or 0x4E00 <= ord(c) <= 0x9FFF)
    # 音声作品：把音轨的播放地址一并下发（前端拿它做「点句播这一段」）
    has_audio = bool(chapters[n].get("audio"))
    return {
        "chapter": {"id": n, "title": chapters[n].get("title", ""),
                    "sentences": sents,
                    "timed": bool(times),
                    "audio": (f"/api/works/{quote(str(work.get('id', '')), safe='')}"
                              f"/audio/{n}") if has_audio else ""},
        "paras": paras,
        "words": words,
        "stats": {
            "sentences": sents,
            "known_sentences": known_sents,
            "known_hits": hits,
            "chars": jp_chars,
        },
    }, None


@app.route("/api/works/<work_id>/audio/<int:n>")
def api_work_audio(work_id, n):
    """音声作品的章节音轨。

    送的是工作区外的原始文件（import_voice 只登记路径、不复制音轨——wav 动辄
    几百 MB），所以**只认 work.json 里登记过的那一条路径**，绝不接受任意路径
    参数。`conditional=True` 让 Flask 支持 Range 请求：阅读器要跳到某一句的
    时间点播放，没有 Range 就只能从头下载整轨。
    """
    work = works.load_work(work_id)
    if not work:
        return jsonify({"error": "作品不存在"}), 404
    chapters = work.get("chapters", [])
    if not (isinstance(n, int) and 0 <= n < len(chapters)):
        return jsonify({"error": "章节不存在"}), 404
    src = chapters[n].get("audio") or ""
    path = Path(src)
    if not src or not path.is_file():
        return jsonify({"error": "音轨文件不在了（被移动或删除？重新导入即可）"}), 404
    return send_file(path, conditional=True)


@app.route("/reader")
def reader_page():
    return render_template("reader.html")


@app.route("/import")
def import_page():
    """独立导入页：只做导入，不加载阅读器（避免先渲染整部作品浪费时间）。"""
    return render_template("import.html")


@app.route("/api/works")
def api_works():
    """已导入作品列表（只给元信息，难度画像按作品单独取——它要跑分词）。"""
    out = []
    for m in works.list_works():
        chapters = m.get("chapters", [])
        out.append({
            "id": m.get("id", ""),
            "title": m.get("title", ""),
            "author": m.get("author", ""),
            "type": m.get("type", "novel"),
            "created": m.get("created", ""),
            # 音声作品：有音轨 + 有时间轴，阅读器据此显示播放条
            "voice": m.get("type") == "voice",
            "chapters": [{"id": c.get("id", 0), "title": c.get("title", ""),
                          "sentences": c.get("sentences", 0),
                          "timed": bool(c.get("audio") and c.get("timed"))}
                         for c in chapters],
        })
    return jsonify({"works": out})


@app.route("/api/works/import", methods=["POST"])
def api_works_import():
    """界面上传文件 → 临时文件 → 与 CLI 同一条管线解析入库。

    三张文件都走这里：轻小说 .txt / .epub 走 import_work；音声作品 .zip
    （音轨 + 字幕/台本）解压到 data/audio/<key>/ 后走 import_voice——音轨不
    复制进仓库（动辄几百 MB），/api/works/<id>/audio/<n> 直接读那个目录里
    登记的绝对路径，所以解压目录保留不删（data/audio 已 gitignore）。

    multipart：file（文件本体）+ title / author（可选，title 缺省用文件名）
    + align（可选，"1"= 没字幕/台本的轨道用 ASR 补时间轴，须本地有 faster-whisper）。
    已存在的同名作品整体覆盖（重新处理一遍）；作品文件本体不入 git。
    """
    import io
    import shutil
    import tempfile
    import zipfile

    # 三种来源：轻小说单文件 / 音声 zip 都带 file；音声文件夹（已解压）没有单
    # 文件——前端把每个文件放进并行的 files / path 两个列表（webkitdirectory），
    # 所以 file 的存在性校验只对非 dir 来源做（否则目录上传永远走不到下面）。
    source = request.form.get("source")
    f = request.files.get("file")
    if source == "dir":
        files = request.files.getlist("files")
        paths = request.form.getlist("path")
        if not files or len(files) != len(paths):
            return jsonify({"error": "目录上传的文件与路径对应不上，请重选"}), 400
        ext, raw, zip_src = "", b"", None
    else:
        if not f or not f.filename:
            return jsonify({"error": "请选择要导入的文件"}), 400
        ext = Path(f.filename).suffix.lower()
        if ext not in (".txt", ".epub", ".zip"):
            return jsonify({"error": "只支持轻小说 .txt / .epub，或音声作品压缩包 .zip"}), 400
        # 先量大小、再决定怎么读：音声 zip 是音轨包（几百 MB 起步），把「判上限」
        # 放在读之后等于先把整个文件搬进 RAM 才说太大。
        limit = (VOICE_UPLOAD_MAX_BYTES if ext == ".zip"
                 else WORK_UPLOAD_MAX_BYTES)
        size = _fs_nbytes(f)
        if size is None:
            # 少见：分块传输没带长度头，只能边读边数（多读 1 字节即可判超限）
            raw = f.read(limit + 1)
            zip_src = io.BytesIO(raw)
            size = len(raw)
        elif ext == ".zip":
            # 请求进到这一步时整个分片已经在 FileStorage.stream 里（SpooledTemporaryFile，
            # 大了会自动滚到磁盘），而 zipfile 要的就是这种可 seek 的对象 → 直接喂它，
            # 不再整份复制进内存
            f.stream.seek(0)
            raw, zip_src = b"", f.stream
        else:
            raw = f.read()
            zip_src = io.BytesIO(raw)
        if size > limit:
            return jsonify({"error":
                            f"文件太大（{size // 1024 // 1024}MB，"
                            f"上限 {limit // 1024 // 1024}MB）"}), 400
        if not size:
            return jsonify({"error": "文件是空的"}), 400

    title = _clean_word_field(request.form.get("title"))
    if not title:
        # 缺省作品名：单文件用文件名；目录上传用相对路径第一段（就是所选文件夹名）
        if f and f.filename:
            title = Path(f.filename).stem
        else:
            first = _safe_rel_path(paths[0]) if paths else ""
            title = first.split("/")[0] if first else f"upload-{int(time.time())}"
    author = _clean_word_field(request.form.get("author"))
    if len(title) > 100:
        return jsonify({"error": "作品名过长（上限 100 字）"}), 400
    if len(author) > 100:
        return jsonify({"error": "作者名过长（上限 100 字）"}), 400

    # 音声作品压缩包：解压到 data/audio/<key> 直接走 import_voice
    if ext == ".zip":
        key = re.sub(r"\W+", "-", title, flags=re.UNICODE).strip("-") \
            or f"upload-{int(time.time())}"
        stage = works.WORKS_DIR.parent / "audio" / key
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_src) as zf:
                for info in zf.infolist():
                    tgt = (stage / _zip_name(info)).resolve()
                    # zip-slip 防护：解出的每个文件都必须待在解压根目录里
                    try:
                        tgt.relative_to(stage.resolve())
                    except ValueError:
                        raise SystemExit(f"压缩包里有越界路径：{info.filename!r}")
                    if info.is_dir():
                        tgt.mkdir(parents=True, exist_ok=True)
                        continue
                    tgt.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(tgt, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
        except SystemExit as e:
            err = str(e) or "解析失败，压缩包内容有问题？"
            print(f"[works] 音声导入中止 {f.filename!r}: {err}")
            return jsonify({"error": err}), 400
        except Exception as e:
            print(f"[works] 音声导入异常 {f.filename!r}: {e}")
            return jsonify({"error": f"解析失败：{e}"}), 400
    elif source == "dir":
        # 已解压目录：webkitdirectory 多文件上传，按相对路径重建到 data/audio/<key>
        # （files / paths 已在上头的来源分叉里取好并校验过）
        key = re.sub(r"\W+", "-", title, flags=re.UNICODE).strip("-") \
            or f"upload-{int(time.time())}"
        stage = works.WORKS_DIR.parent / "audio" / key
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True, exist_ok=True)
        try:
            _materialize_dir_upload(
                [(p or item.filename or "file", item)
                 for p, item in zip(paths, files)],
                stage)
        except ValueError as e:
            print(f"[works] 目录上传中止 {title!r}: {e}")
            return jsonify({"error": str(e) or "目录内容有问题？"}), 400
        except Exception as e:
            print(f"[works] 目录上传异常 {title!r}: {e}")
            return jsonify({"error": f"目录上传失败：{e}"}), 400
    else:
        # 轻小说 txt / epub：上限已在读之前按档判过，这里只管落临时文件再解析
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix="work_upload_", suffix=ext)
            with os.fdopen(fd, "wb") as fp:
                fp.write(raw)
            try:
                meta = import_work.import_work(Path(tmp), title=title, author=author,
                                               source_name=Path(f.filename).name)
            except SystemExit as e:
                return jsonify({"error": str(e) or "解析失败，文件内容有问题？"}), 400
            except Exception as e:
                print(f"[works] 导入异常 {f.filename!r}: {e}")
                return jsonify({"error": f"解析失败：{e}"}), 400
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        chapters = meta.get("chapters", [])
        return jsonify({
            "ok": True,
            "work_id": meta.get("id", ""),
            "title": meta.get("title", title),
            "author": meta.get("author", ""),
            "chapters": len(chapters),
            "sentences": sum(c.get("sentences", 0) for c in chapters),
        })

    # ---- 到这里 stage 就绪（data/audio/<key>）：zip / 目录，走语音导入 ----
    if request.form.get("asr") == "server":
        # 服务器引擎：后台任务（上传→转写→拉回→导入），前端轮询 task_id
        try:
            engine = validate_engine(request.form.get("engine"))
            gpu = validate_gpu(request.form.get("gpu"))
        except ServerAsrError as e:
            return jsonify({"error": str(e)}), 400
        task = server_asr_task.create_task(
            stage=stage, title=title, author=author,
            engine=engine, gpu=gpu, key=key)
        threading.Thread(target=server_asr_task.run_task,
                         args=(task.id,), daemon=True).start()
        return jsonify({"ok": True, "task_id": task.id, "engine": engine})

    try:
        meta = import_voice.import_voice(
            root=stage, title=title, author=author,
            align=request.form.get("align") == "1",
            model=request.form.get("model") or "medium")
    except SystemExit as e:
        err = str(e) or "解析失败，压缩包内容有问题？"
        # 日志用 title 而不是 f.filename：目录上传没有单文件，f 是 None
        print(f"[works] 音声导入中止 {title!r}: {err}")
        return jsonify({"error": err}), 400
    except Exception as e:
        print(f"[works] 音声导入异常 {title!r}: {e}")
        return jsonify({"error": f"解析失败：{e}"}), 400

    chapters = meta.get("chapters", [])
    return jsonify({
        "ok": True,
        "work_id": meta.get("id", ""),
        "title": meta.get("title", title),
        "author": meta.get("author", ""),
        "chapters": len(chapters),
        "sentences": sum(c.get("sentences", 0) for c in chapters),
        "type": "voice",
    })


def _task_view(t):
    """服务器导入任务 → 前端轮询用的视图（不暴露 stage 等内部路径）。"""
    return {
        "id": t.id, "phase": t.phase, "msg": t.msg, "error": t.error,
        "title": t.title, "work_id": t.work_id,
        "chapters": t.chapters, "sentences": t.sentences,
        "engine": t.engine, "gpu": t.gpu,
        "done": t.done, "updated": t.updated,
    }


@app.route("/api/works/import/tasks")
def api_works_import_tasks():
    """最近活跃的服务器导入任务（首页悬浮球拉取）。"""
    return jsonify({"tasks": [_task_view(t)
                              for t in server_asr_task.active_tasks()]})


@app.route("/api/works/import/task/<tid>")
def api_works_import_task(tid):
    """单个服务器导入任务进度（前端轮询）。"""
    t = server_asr_task.get_task(tid)
    if t is None:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(_task_view(t))


@app.route("/api/asr/gpu")
def api_asr_gpu():
    """服务器各卡显存占用（nvidia-smi），供导入面板可视化选卡。"""
    try:
        gpus = ServerAsr().gpu_status()
    except ServerAsrError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"gpus": gpus})


@app.route("/api/works/<work_id>")
def api_work_detail(work_id):
    """作品详情 + 难度画像（整部作品跑一遍分词，按 mtime 缓存）。"""
    work = works.load_work(work_id)
    if not work:
        return jsonify({"error": "作品不存在"}), 404
    vocab = load_vocab()
    prof = works.profile(work, vocab)
    return jsonify({
        "work": {
            "id": work.get("id", ""),
            "title": work.get("title", ""),
            "author": work.get("author", ""),
            "type": work.get("type", "novel"),
            "created": work.get("created", ""),
            "chapters": work.get("chapters", []),
        },
        "profile": prof,
    })


@app.route("/api/works/<work_id>/chapter/<int:n>")
def api_work_chapter(work_id, n):
    """章节正文 + 词块标注 + 本章涉及的词条卡。"""
    work = works.load_work(work_id)
    if not work:
        return jsonify({"error": "作品不存在"}), 404
    payload, err = annotate_chapter(load_vocab(), work, n)
    if err:
        return jsonify({"error": err}), 404
    payload["work"] = {"id": work.get("id", ""), "title": work.get("title", "")}
    return jsonify(payload)


@app.route("/api/works/<work_id>/chapter/<int:n>/sentence", methods=["POST"])
def api_work_sentence_edit(work_id, n):
    """人工修改某一章的某一句文本（修 whisper / 台本转写错字）。

    前端传 para（段落下标）与 index（句内下标）；保存后阅读器整章重新分词
    标注。音声作品的句子时间轴（t0/t1）不随文本改动，保持不变。
    """
    work = works.load_work(work_id)
    if not work:
        return jsonify({"error": "作品不存在"}), 404
    body = request.get_json() or {}
    para = body.get("para")
    index = body.get("index")
    text = _clean_word_field(body.get("text"))
    if not isinstance(para, int) or not isinstance(index, int) or para < 0 or index < 0:
        return jsonify({"error": "缺少句子位置"}), 400
    if not text or not (1 <= len(text) <= 500):
        return jsonify({"error": "句子长度需在 1-500 字"}), 400
    path = works.work_dir(work_id) / "content" / f"ch{n:02d}.json"
    with _DATA_LOCK:
        ch = works.load_chapter_raw(work_id, n)
        if not ch:
            return jsonify({"error": "章节不存在"}), 404
        paras = ch.get("paras", [])
        if not (0 <= para < len(paras)):
            return jsonify({"error": "段落不存在"}), 400
        p = paras[para]
        if not (0 <= index < len(p)):
            return jsonify({"error": "句子不存在"}), 400
        p[index] = text
        _atomic_write(path, _dump_json(ch))
    return jsonify({"ok": True, "text": text})


# ---- 分词修正（用户词典：把 janome 切错的相邻块合回一个词）----

TOKEN_SURFACE_MIN = 2
TOKEN_SURFACE_MAX = 20


def _save_user_tokens(data):
    """用户分词词典原子写入（调用方持 _DATA_LOCK）。"""
    path = works.user_tokens_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, _dump_json(data))


@app.route("/api/tokens")
def api_tokens_list():
    """当前所有分词修正（keep 写法 + 按句例外），供阅读器管理面板展示。"""
    return jsonify(works.load_user_tokens_raw())


@app.route("/api/works/<work_id>/token_review", methods=["POST"])
def api_token_review(work_id):
    """把拟合并的写法放进整部作品扫一遍，列出会被合回去的所有句子（默认全勾）。

    让用户在保存前看到影响面，把极少数「其实切对了」的句子勾掉（存为例外）。
    """
    work = works.load_work(work_id)
    if not work:
        return jsonify({"error": "作品不存在"}), 404
    body = request.get_json() or {}
    surface = _clean_word_field(body.get("surface"))
    if not surface or not (TOKEN_SURFACE_MIN <= len(surface) <= TOKEN_SURFACE_MAX):
        return jsonify({"error": f"词形长度需在 {TOKEN_SURFACE_MIN}-{TOKEN_SURFACE_MAX} 字"}), 400
    occ = works.find_occurrences(work, load_vocab(), surface)
    return jsonify({"surface": surface, "occurrences": occ})


@app.route("/api/tokens", methods=["POST"])
def api_token_add():
    """保存一条修正：surface 入全局 keep 词典；exceptions 里的句子记为不合并。

    全局生效——所有已导入 / 将来导入的作品都按合并后词块显示；勾掉的句子只在
    该句保持切开（极少数「だ 系词 + から 助词」这类其实正确的切分）。
    同 surface 重写会覆盖其例外集（幂等，便于反复调整）。
    """
    body = request.get_json() or {}
    surface = _clean_word_field(body.get("surface"))
    if not surface or not (TOKEN_SURFACE_MIN <= len(surface) <= TOKEN_SURFACE_MAX):
        return jsonify({"error": f"词形长度需在 {TOKEN_SURFACE_MIN}-{TOKEN_SURFACE_MAX} 字"}), 400
    exc_sentences = []
    seen_exc = set()
    for s in (body.get("exceptions") or []):
        s = _clean_word_field(s)
        if s and s not in seen_exc:
            seen_exc.add(s)
            exc_sentences.append(s)
    with _DATA_LOCK:
        data = works.load_user_tokens_raw()
        if surface not in data["keep"]:
            data["keep"].append(surface)
        data["split_exceptions"] = [e for e in data["split_exceptions"]
                                    if e.get("surface") != surface]
        for s in exc_sentences:
            data["split_exceptions"].append({"surface": surface, "sentence": s})
        _save_user_tokens(data)
    return jsonify({"ok": True, "surface": surface,
                    "exceptions": len(exc_sentences)})


@app.route("/api/tokens", methods=["DELETE"])
def api_token_delete():
    """撤销一条修正：从 keep 移除该写法，连带清掉它的所有按句例外。"""
    body = request.get_json() or {}
    surface = _clean_word_field(body.get("surface"))
    if not surface:
        return jsonify({"error": "缺少词形"}), 400
    with _DATA_LOCK:
        data = works.load_user_tokens_raw()
        data["keep"] = [k for k in data["keep"] if k != surface]
        data["split_exceptions"] = [e for e in data["split_exceptions"]
                                    if e.get("surface") != surface]
        _save_user_tokens(data)
    return jsonify({"ok": True})


def _find_same_word(vocab, kanji, hiragana):
    """全词库找同形同音词条，返回 (lesson_id, word) 或 None。

    挖矿判重用：词库别的课里已经有了就别再录一遍（同词多份会分散掌握度，
    练习时还可能互相当干扰项）。读音归一走 normalize_kana，半角/片假名一视同仁。
    """
    target_h = normalize_kana(hiragana)
    target_k = "" if kanji == "---" else normalize_kana(kanji)
    if not target_h:
        return None
    for lid, ldata in vocab.get("lessons", {}).items():
        for w in ldata.get("words", []):
            if normalize_kana(w.get("hiragana", "")) != target_h:
                continue
            if target_k:
                if normalize_kana(w.get("kanji", "")) == target_k:
                    return lid, w
            elif not has_kanji(w.get("kanji", "")):
                return lid, w
    return None


@app.route("/api/dict")
def api_dict():
    """查本地词典：阅读器挖矿弹窗据此预填读音/词性/义项（离线，用预翻好的数据）。"""
    surface = (request.args.get("surface") or "").strip()
    base = (request.args.get("base") or "").strip()
    reading = (request.args.get("reading") or "").strip()
    if not (surface or base):
        return jsonify({"error": "缺少 surface"}), 400
    by_kanji, _ = dict_index()
    return jsonify({
        "surface": surface,
        "base": base,
        "entries": dict_lookup(surface, base, reading),
        "available": bool(by_kanji),   # 没装词典文件时前端只显示原表单
    })


@app.route("/api/words/occurrences")
def api_word_occurrences():
    """跨作品出处统计：这词在你的作品里出现过几次、都在哪（挖矿决策依据）。

    出现 1 次的拟声词可以不挖，出现 20 次的必须挖——这个数字比释义更能决定取舍。
    """
    surface = (request.args.get("surface") or "").strip()
    if not surface or len(surface) > TOKEN_SURFACE_MAX:
        return jsonify({"error": "词形长度不合法"}), 400
    return jsonify(works.count_occurrences(surface))


def _mine_word_to_mywords(kanji, hiragana, meaning, notes, sentence=""):
    """挖矿落库（阅读器与图片日语共用）：写进默认生词本，返回 (响应, 状态码)。

    调用方负责字段清洗与校验；这里只管全库判重、分配 id 与原子落盘。
    判重按（写法, 读音）查**全词库**：别的课里已有同形同音词就不重复录，
    返回已有 ref（前端据此立刻按已知词渲染，而不是多出一份分散掌握度）。
    """
    with _DATA_LOCK:
        vocab = load_vocab()
        existed = _find_same_word(vocab, kanji, hiragana)
        if existed:
            lid, w = existed
            return jsonify({
                "duplicate": True,
                "error": f"词库里已有该词（{vocab.get('lessons', {}).get(lid, {}).get('title', lid)}），未重复添加",
                "ref": f"{lid}:{w.get('id', '')}",
                "mastery": int(w.get("mastery") or 0),
            }), 409

        lessons = vocab.setdefault("lessons", {})
        lesson_id = DEFAULT_USER_LESSON
        if lesson_id not in lessons:
            lessons[lesson_id] = {"title": DEFAULT_USER_LESSON_TITLE, "words": []}
        words = lessons[lesson_id].setdefault("words", [])

        word = {
            "kanji": kanji,
            "hiragana": hiragana,
            "romaji": "",
            "meaning": meaning,
            "mastery": 0,
            "last_reviewed": None,
            "next_due": None,
            "notes": notes,
        }
        if sentence:
            word["example_ja"] = sentence
        words.append(word)
        ensure_word_ids(vocab)
        save_vocab(vocab)
        new_ref = f"{lesson_id}:{word['id']}"

    return jsonify({"ok": True, "ref": new_ref, "word": word, "mastery": 0,
                    "lesson_title": DEFAULT_USER_LESSON_TITLE}), 200


@app.route("/api/works/<work_id>/mine", methods=["POST"])
def api_mine_word(work_id):
    """阅读器「＋ 生词本」：把作品里的生词写进默认生词本。

    与 /api/words/add 同口径（字段清洗、长度上限、ensure_word_ids、原子写），
    差别在两处：
      - 判重按（写法, 读音）全库查（不是同课三元组）——同一个词在别的课里
        已经有了，直接把已有 ref 回给前端，阅读器立刻按已知词渲染；
      - notes 记来源「《作品》章节」，例句默认就是挖到的那句话。
    """
    work = works.load_work(work_id)
    if not work:
        return jsonify({"error": "作品不存在"}), 404

    data = request.get_json() or {}
    surface = _clean_word_field(data.get("surface"))
    reading = _clean_word_field(data.get("reading"))
    base = _clean_word_field(data.get("base")) or surface
    # 释义是中文：不做 NFKC——挖矿的义项大多直接来自 dict_zh.json，那里的多义项
    # 用全角逗号分隔（「明显，清楚」）、夹注用全角括号（「包含（如价格中含税）」），
    # 走 NFKC 会被折成「明显,清楚」「包含(如价格中含税)」，与本地词典和 CSV 导入
    # 的词条风格都不一致——这是全文里最容易踩到 NFKC 的一条路径（点一下即保存）
    meaning = _clean_zh_field(data.get("meaning"))
    sentence = _clean_word_field(data.get("sentence"))
    if not surface:
        return jsonify({"error": "缺少词形"}), 400
    if not meaning:
        return jsonify({"error": "请填写中文释义"}), 400

    # 汉字词：写法记汉字、读音记假名；纯假名词：写法记 "---"（与词表口径一致）
    if has_kanji(surface):
        kanji, hiragana = surface, reading
    else:
        kanji, hiragana = "---", (reading or surface)
    if not hiragana:
        return jsonify({"error": "请填写读音（平假名）"}), 400

    try:
        chapter_title = work.get("chapters", [])[int(data.get("chapter") or 0)].get("title", "")
    except (ValueError, IndexError, TypeError):
        chapter_title = ""
    notes = f"来源：《{work.get('title', '')}》{chapter_title}".strip()
    if base and base != surface and has_kanji(base):
        notes += f"（原形 {base}）"

    for label, val in (("词形", kanji), ("读音", hiragana), ("释义", meaning),
                       ("例句", sentence)):
        if len(val) > ADD_WORD_MAXLEN:
            return jsonify({"error": f"{label}过长（上限 {ADD_WORD_MAXLEN} 字）"}), 400

    return _mine_word_to_mywords(kanji, hiragana, meaning, notes, sentence)


# ====================================================================
# 图片日语：日常图片讲解课（/pictures 与 /api/pictures 系列）
# 课件本体在 data/picture_lessons/<id>.json（由 import_picture_lesson.py 导入），
# 图片仍放在原处（data/extracted_images/…），发图路由只认课件里登记过的路径。
# 课程内容与词库的联动（已学词高亮、掌握度、加生词本）都在 picture.py 里实时算。
# ====================================================================

@app.route("/pictures")
def pictures_page():
    """图片日语：看照片学单词/语法/句式（课程列表与课件在同一页切换）。"""
    return render_template("pictures.html")


@app.route("/api/pictures")
def api_pictures():
    """课程列表：图片日语页的课程下拉 + 概述（含「能在词库里练的词」数）。"""
    vocab = load_vocab()
    return jsonify({"lessons": picture.list_lessons(vocab)})


@app.route("/api/pictures/<lesson_id>")
def api_picture_lesson(lesson_id):
    """一课的全部内容：照片（原文标好已学词）+ 单词（挂词库 ref/掌握度）+ 语法/練習。"""
    lesson = picture.load_lesson(lesson_id)
    if not lesson:
        return jsonify({"error": "课程不存在"}), 404
    return jsonify(picture.build_payload(lesson, load_vocab()))


@app.route("/api/pictures/<lesson_id>/image/<int:n>")
def api_picture_image(lesson_id, n):
    """第 n 张照片的原图（白名单：只认课件里登记过的那几张）。"""
    lesson = picture.load_lesson(lesson_id)
    if not lesson:
        return jsonify({"error": "课程不存在"}), 404
    path = picture.image_path(lesson, n)
    if not path:
        return jsonify({"error": "图片不存在"}), 404
    return send_file(str(path), max_age=0)


@app.route("/api/pictures/<lesson_id>/mine", methods=["POST"])
def api_picture_mine(lesson_id):
    """图片日语「＋ 生词本」：把课件里的词写进生词本，备注记来源课次与照片。

    与阅读器挖矿同一套落库逻辑（全库判重、原子写），差别是词形直接来自课件
    （写法 + 课件的假名注音），不需要词典预填：
      - 汉字词：写法记汉字、读音记假名；
      - 片假名词：写法记 `---`、**片假名原样记在读音字段**（与词表里
        タクシー / ゲーム 的存法一致——读音字段不是它的平假名转写）。
    """
    lesson = picture.load_lesson(lesson_id)
    if not lesson:
        return jsonify({"error": "课程不存在"}), 404

    data = request.get_json() or {}
    form = _clean_word_field(data.get("form"))
    kana = _clean_word_field(data.get("kana"))
    meaning = _clean_zh_field(data.get("meaning"))
    sentence = _clean_word_field(data.get("sentence"))
    if not form:
        return jsonify({"error": "缺少词形"}), 400
    if not meaning:
        return jsonify({"error": "请填写中文释义"}), 400

    if has_kanji(form):
        kanji, hiragana = form, kana
    elif has_katakana(form) and kana and normalize_kana(form) == normalize_kana(kana):
        kanji, hiragana = "---", form
    else:
        kanji, hiragana = "---", (kana or form)
    if not hiragana:
        return jsonify({"error": "缺少读音"}), 400

    notes = picture.mining_notes(lesson, data.get("photo"))
    for label, val in (("词形", kanji), ("读音", hiragana), ("释义", meaning),
                       ("例句", sentence)):
        if len(val) > ADD_WORD_MAXLEN:
            return jsonify({"error": f"{label}过长（上限 {ADD_WORD_MAXLEN} 字）"}), 400

    return _mine_word_to_mywords(kanji, hiragana, meaning, notes, sentence)


@app.route("/api/tts/text")
def api_tts_text():
    """朗读任意文本（阅读器的句子）：按内容哈希缓存，只生成一次。"""
    from flask import send_file

    text = clean_tts_text(request.args.get("text", ""))
    if not text:
        return jsonify({"error": "缺少 text 参数"}), 400
    if len(text) > TTS_TEXT_MAXLEN:
        return jsonify({"error": f"文本过长（上限 {TTS_TEXT_MAXLEN} 字）"}), 400
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    path = SENT_AUDIO_DIR / f"{digest}.mp3"
    audio_path = ensure_audio(f"text:{digest}", text, out_path=path)
    if not audio_path:
        return jsonify({"error": "音频生成失败"}), 500
    return send_file(str(audio_path), mimetype="audio/mpeg", max_age=0)


SERVER_PORT = 5000


def _refuse_second_instance(port=SERVER_PORT, host="127.0.0.1"):
    """已经有实例在监听就直接退出，不要两个进程静默并存。

    Windows 上 werkzeug 带着 `allow_reuse_address=True`，第二次 bind 同一个端口
    **不报错**（实测：两个进程都打印「已启动」，流量却只进先起的那个）。于是双击
    两次 start_practice.bat 就得到一个看不见的第二实例——它不接请求，却照样跑
    restore_tasks 起 worker 线程写盘，而 `_DATA_LOCK` 只是进程内的锁，两个进程
    各自 load→modify→save vocabulary.json 会互相覆盖（后写的赢）。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    try:
        if exclusive is not None:
            # Windows：只要端口被占着就失败（SO_REUSEADDR=0 在这里会把
            # 上一轮的 TIME_WAIT 也误判成冲突）
            probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        else:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        probe.bind((host, port))
    except OSError:
        probe.close()
        raise SystemExit(
            f"另一个实例正在监听 http://{host}:{port}——先关掉已经开着的那个窗口。\n"
            "（Windows 上重复 bind 同一端口不报错，所以程序不会替你报错，这里主动挡住。）")
    finally:
        probe.close()


if __name__ == "__main__":
    # 只在重载器的父进程里查：debug 模式下是「父进程 bind → 子进程靠
    # SO_REUSEADDR 重绑接管」，子进程这一查会把自家父进程当成别人，第一次启动
    # 就自我否决（实测：两个进程都退出，端口上空无一人）。
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        _refuse_second_instance()
    # debug 重载器会把本模块顶层跑两遍（父进程 watch + 子进程 serve），
    # 只在真正提供服务的进程里恢复任务——否则同一个导入被两个进程各起一份
    # 上传与 GPU 转写，还会互相把对方的服务器产物 cleanup 掉。
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        # 恢复上次关闭时没跑完的服务器导入任务：转写在远端 nohup 照跑，
        # 本地只需要接着轮询 → 拉回 → 导入（都是幂等的）
        try:
            for t in server_asr_task.restore_tasks():
                threading.Thread(target=server_asr_task.run_task,
                                 args=(t.id,), daemon=True).start()
                print(f"[asr] 恢复任务 {t.id}：{t.title}（phase={t.phase}）")
        except Exception as e:
            print("[asr] 恢复任务失败：", e)
    print(f"日语练习程序已启动: http://127.0.0.1:{SERVER_PORT}  (按 Ctrl+C 退出)")
    app.run(debug=True, port=SERVER_PORT)
