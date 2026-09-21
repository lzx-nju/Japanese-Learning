# -*- coding: utf-8 -*-
"""app.py 纯函数的单元测试（不依赖网络与文件）。

运行: python.exe practice\\test_app.py
"""
import ast
import base64
import csv
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app
import conjugation
import generate_vocab_images as gen  # 模块级不发请求、不读 API Key
import picture  # 图片日语：课程库与实时标注
from server_asr_bridge import ServerAsrError  # _FakeBridge 抛同族异常

# 重点词清单在测试期间改指向临时文件：各用例自建的词表与开发机上的真实清单
# 互不相干，若不隔离，「重点词」开着时一大片 /api/start 用例会集体报
# 「重点词里没有词能出这个模式的题」——那是环境状态，不是被测行为。
_FOCUS_TMP = None
_OLD_FOCUS_PATH = None
# 进度快照同理：save_vocab 现在每次覆写前拍一份 data/backups/*.bak_<日期>.json。
# 不隔离的话测试会真的写出两份快照，既留垃圾，又占掉「当天只有第一份」的名额，
# 当天第一次真实保存反而拍不到。
_BACKUP_TMP = None
_OLD_BACKUP_DIR = None


def setUpModule():
    global _FOCUS_TMP, _OLD_FOCUS_PATH, _BACKUP_TMP, _OLD_BACKUP_DIR
    _FOCUS_TMP = tempfile.TemporaryDirectory()
    _OLD_FOCUS_PATH = app.FOCUS_PATH
    app.FOCUS_PATH = Path(_FOCUS_TMP.name) / "focus_words.json"
    _BACKUP_TMP = tempfile.TemporaryDirectory()
    _OLD_BACKUP_DIR = app.BACKUP_DIR
    app.BACKUP_DIR = Path(_BACKUP_TMP.name) / "backups"


def tearDownModule():
    global _FOCUS_TMP, _OLD_FOCUS_PATH, _BACKUP_TMP, _OLD_BACKUP_DIR
    app.FOCUS_PATH = _OLD_FOCUS_PATH
    _FOCUS_TMP.cleanup()
    _FOCUS_TMP = None
    app.BACKUP_DIR = _OLD_BACKUP_DIR
    _BACKUP_TMP.cleanup()
    _BACKUP_TMP = None


def make_vocab():
    return {"lessons": {"lesson_01": {"title": "t", "words": [
        {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人"},
        {"id": "w002", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗"},
    ]}}}


class TestKana(unittest.TestCase):
    def test_normalize_kana(self):
        self.assertEqual(app.normalize_kana("ガッコウ"), "がっこう")
        self.assertEqual(app.normalize_kana(" ちゅう ごく\t"), "ちゅうごく")
        self.assertEqual(app.normalize_kana(""), "")

    def test_check_answer(self):
        self.assertTrue(app.check_answer("ガッコウ", "がっこう"))
        # 长音「ー」不宽容：少写一拍就是错（パーカ ≠ パーカー），错因标「长音」
        self.assertFalse(app.check_answer("たくし", "たくしー"))
        self.assertEqual(app.classify_mismatch("たくし", "たくしー"), ["长音"])
        self.assertFalse(app.check_answer("たくしい", "たくしー"))
        # 备选答案
        self.assertTrue(app.check_answer("なな", "しち", ["なな"]))
        self.assertFalse(app.check_answer("", "しち"))

    def test_halfwidth_and_decomposed_input_not_misjudged(self):
        # 输入法半角模式、以及从 PDF/词表粘贴的「か + 组合浊点」
        self.assertTrue(app.check_answer("ｶﾝｼ", "かんし"))
        self.assertTrue(app.check_answer("か" + chr(0x3099), "が"))
        self.assertTrue(app.check_answer("ｾﾞｯﾎﾟｳ", "ぜっぽう"))


class TestMismatchHint(unittest.TestCase):
    """答错时区分「写法差异出在哪一类」，只作提示、不改判分。"""

    def test_single_class(self):
        self.assertEqual(app.classify_mismatch("たくしー", "たくしい"), ["长音"])
        self.assertEqual(app.classify_mismatch("けか", "けっか"), ["促音"])
        self.assertEqual(app.classify_mismatch("ぱん", "ばん"), ["浊点"])

    def test_compound_but_minimal(self):
        # かこう 对比 がっこう：浊点和促音都丢了，两类一起报
        self.assertEqual(app.classify_mismatch("かこう", "がっこう"), ["浊点", "促音"])
        # がこー 的浊点其实写对了，错因只有促音+长音
        self.assertEqual(app.classify_mismatch("がこー", "がっこう"), ["促音", "长音"])

    def test_long_vowel_stays_wrong(self):
        # おばさん / おばあさん 只差长音但词义不同，判错并给出错因
        self.assertFalse(app.check_answer("おばさん", "おばあさん"))
        self.assertEqual(app.classify_mismatch("おばさん", "おばあさん"), ["长音"])

    def test_real_word_error_has_no_hint(self):
        self.assertEqual(app.classify_mismatch("ねこ", "いぬ"), [])
        self.assertEqual(app.classify_mismatch("", "ねこ"), [])
        # 汉字书写模式的答案不是假名，不该冒出假名错因
        self.assertEqual(app.classify_mismatch("ちゅうごくじん", "中国人"), [])


class TestRef(unittest.TestCase):
    def test_parse_ref(self):
        self.assertEqual(app.parse_ref("lesson_01:w002"), ("lesson_01", "w002"))
        self.assertIsNone(app.parse_ref("nocolon"))
        self.assertIsNone(app.parse_ref("lesson_01:"))
        self.assertIsNone(app.parse_ref(None))

    def test_resolve_by_id(self):
        lid, w = app.resolve_word(make_vocab(), "lesson_01:w002")
        self.assertEqual((lid, w["hiragana"]), ("lesson_01", "いぬ"))

    def test_resolve_legacy_index(self):
        lid, w = app.resolve_word(make_vocab(), "lesson_01:0")
        self.assertEqual(w["hiragana"], "ひと")
        self.assertIsNone(app.resolve_word(make_vocab(), "lesson_01:99"))

    def test_resolve_missing(self):
        self.assertIsNone(app.resolve_word(make_vocab(), "lesson_01:w009"))
        self.assertIsNone(app.resolve_word(make_vocab(), "nope:w001"))

    def test_ensure_word_ids(self):
        vocab = make_vocab()
        vocab["lessons"]["lesson_01"]["words"].append({"kanji": "猫", "hiragana": "ねこ"})
        vocab["lessons"]["lesson_01"]["words"].append({"kanji": "鱼", "hiragana": "さかな"})
        app.ensure_word_ids(vocab)
        ids = [w["id"] for w in vocab["lessons"]["lesson_01"]["words"]]
        self.assertEqual(ids, ["w001", "w002", "w003", "w004"])
        # 幂等：已有 id 不变
        app.ensure_word_ids(vocab)
        self.assertEqual([w["id"] for w in vocab["lessons"]["lesson_01"]["words"]],
                         ["w001", "w002", "w003", "w004"])
        # id 位于字段首位
        self.assertEqual(list(vocab["lessons"]["lesson_01"]["words"][2])[0], "id")


class TestDue(unittest.TestCase):
    TODAY = date(2026, 8, 29)

    def test_next_due(self):
        due = {"mastery": 4, "next_due": (self.TODAY - timedelta(days=1)).isoformat()}
        self.assertTrue(app.is_due(due, self.TODAY))
        future = {"mastery": 4, "next_due": (self.TODAY + timedelta(days=3)).isoformat()}
        self.assertFalse(app.is_due(future, self.TODAY))

    def test_legacy_fallback_without_next_due(self):
        # 旧数据（无 next_due）：按 last_reviewed + 间隔推算
        w = {"mastery": 3, "last_reviewed": (self.TODAY - timedelta(days=7)).isoformat()}
        self.assertTrue(app.is_due(w, self.TODAY))
        w = {"mastery": 3, "last_reviewed": (self.TODAY - timedelta(days=6)).isoformat()}
        self.assertFalse(app.is_due(w, self.TODAY))

    def test_never_reviewed(self):
        self.assertTrue(app.is_due({"mastery": 0}, self.TODAY))

    # ---- effective_due：到期日（is_due 与「到期优先」排序共用的口径）----

    def test_effective_due_prefers_next_due(self):
        w = {"mastery": 5, "last_reviewed": "2026-01-01", "next_due": "2026-09-01"}
        self.assertEqual(app.effective_due(w), date(2026, 9, 1))

    def test_effective_due_derives_from_last_reviewed_and_mastery(self):
        """无 next_due 的旧数据：last_reviewed + 该掌握度的间隔。"""
        w = {"mastery": 2, "last_reviewed": "2026-08-01"}
        self.assertEqual(app.effective_due(w),
                         date(2026, 8, 1) + timedelta(days=app.INTERVAL_DAYS[2]))

    def test_effective_due_unparsable_dates_mean_immediately_due(self):
        """next_due 读不出来时退回 last_reviewed；两个都读不出来 = 从未练过。"""
        self.assertEqual(app.effective_due({"next_due": "garbage",
                                            "last_reviewed": "2026-09-01",
                                            "mastery": 3}),
                         date(2026, 9, 8))   # +7 天（m=3 档）
        self.assertEqual(app.effective_due({}), date.min)
        self.assertEqual(app.effective_due({"last_reviewed": "bad"}), date.min)
        self.assertEqual(app.effective_due({"mastery": None}), date.min)

    def test_is_due_agrees_with_effective_due(self):
        """两个口径必须永远一致：is_due 现在就是拿 effective_due 算的，
        哪天有人把其中一个改回独立实现，这条会红。"""
        fixtures = [{}, {"mastery": 0}, {"mastery": 4, "next_due": "2026-08-28"},
                    {"mastery": 4, "next_due": "2026-08-31"},
                    {"mastery": 3, "last_reviewed": "2026-08-22"},
                    {"mastery": None, "last_reviewed": "2026-08-22"},
                    {"next_due": "garbage"}, {"mastery": 99, "last_reviewed": "x"}]
        for w in fixtures:
            self.assertEqual(app.is_due(w, self.TODAY),
                             self.TODAY >= app.effective_due(w), msg=str(w))


class TestDueQueueOrder(unittest.TestCase):
    """「到期优先」必须按真正到期日排，不能按 last_reviewed 排。

    起因：_apply_answer 无论答对答错都把 last_reviewed 刷成今天，于是按它排序的
    到期队列里，同一天练过的词一起沉到底部；而到期与否其实由 next_due 决定
    （掌握度越高间隔越长），两个口径会给出相反的顺序。
    """

    def test_more_overdue_wins_even_if_reviewed_more_recently(self):
        """两个都到期，但「最久没碰」与「逾期最久」给出相反顺序时按后者。

        a 前天练过、next_due 已逾期一天；b 两周前练过、昨天才到期。按
        last_reviewed 排 b 在前（碰它更早），按到期日排 a 在前——该复习的紧迫度
        是「逾期多久」，不是「多久没碰」。
        """
        a = {"word": {"id": "a", "mastery": 0, "last_reviewed": "2026-09-17",
                      "next_due": "2026-09-17"}}
        b = {"word": {"id": "b", "mastery": 0, "last_reviewed": "2026-09-01",
                      "next_due": "2026-09-18"}}
        picked = app.pick_due_first([b, a], 2)
        self.assertEqual([c["word"]["id"] for c in picked], ["a", "b"])

    def test_never_reviewed_stay_at_head(self):
        """「全部」池的老行为：没练过的词排在到期复习词之前（想只复习要选复习池）。"""
        fresh = {"word": {"id": "fresh", "mastery": 0}}
        overdue = {"word": {"id": "overdue", "mastery": 0,
                            "last_reviewed": "2026-07-01",
                            "next_due": "2026-07-08"}}
        picked = app.pick_due_first([overdue, fresh], 2)
        self.assertEqual([c["word"]["id"] for c in picked], ["fresh", "overdue"])

    def test_new_pool_keeps_word_list_order(self):
        """新词池按词表顺序出题：所有未学词到期日同为 date.min，排序必须稳定。"""
        cands = [{"word": {"id": f"w{i:03d}", "mastery": 0}} for i in range(5, 0, -1)]
        picked = app.pick_due_first(cands, 5)
        self.assertEqual([c["word"]["id"] for c in picked],
                         ["w005", "w004", "w003", "w002", "w001"])

    def test_no_due_queue_sorts_by_last_reviewed(self):
        """回归锁：四处「到期优先」排序全部走 effective_due，不许残留按
        last_reviewed 排的写法（那是本次 bug 的形状）。"""
        src = (Path(__file__).resolve().parent / "app.py").read_text(encoding="utf-8")
        stale = [ln for ln in src.splitlines()
                 if "sort" in ln and "last_reviewed" in ln]
        self.assertEqual(stale, [], f"仍在按 last_reviewed 排序：{stale}")


class TestFinishScheduling(unittest.TestCase):
    """api_finish 的调度语义（走临时目录的完整 API，不碰真实数据文件）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = make_vocab()
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp  # record_file 返回相对路径，需同步指向临时目录
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR = self._old
        self._tmp.cleanup()

    def _finish(self, ref, correct, session_id="t"):
        return self.client.post("/api/finish", json={
            "mode": "audio_to_kana", "lesson": "lesson_01",
            "session_id": session_id,
            "results": [{"ref": ref, "correct": correct}],
        }).get_json()

    def _word(self, idx=0):
        return app.load_vocab()["lessons"]["lesson_01"]["words"][idx]

    def test_first_correct_due_tomorrow(self):
        """首答巩固步：掌握度从 0 答对升 1，次日到期（而非直接跳 2 天）。"""
        self._finish("lesson_01:w001", True, "s1")
        w = self._word()
        self.assertEqual(w["mastery"], 1)
        self.assertEqual(w["next_due"], (date.today() + timedelta(days=1)).isoformat())

    def test_correct_uses_new_mastery_interval(self):
        """非首次答对：按新掌握度间隔（mastery 2 → 3 → 7 天）。"""
        vocab = app.load_vocab()
        vocab["lessons"]["lesson_01"]["words"][0]["mastery"] = 2
        app.save_vocab(vocab)
        self._finish("lesson_01:w001", True, "s2")
        w = self._word()
        self.assertEqual(w["mastery"], 3)
        self.assertEqual(w["next_due"], (date.today() + timedelta(days=7)).isoformat())

    def test_wrong_due_tomorrow_even_at_high_mastery(self):
        """答错固定次日到期，而非按掌握度间隔。"""
        vocab = app.load_vocab()
        vocab["lessons"]["lesson_01"]["words"][0]["mastery"] = 4
        app.save_vocab(vocab)
        self._finish("lesson_01:w001", False, "s3")
        w = self._word()
        self.assertEqual(w["mastery"], 3)
        self.assertEqual(w["next_due"], (date.today() + timedelta(days=1)).isoformat())

    def test_duplicate_session_not_reapplied(self):
        """幂等：同一 session_id 重复交卷不再叠加掌握度。"""
        payload_ref = "lesson_01:w001"
        r1 = self._finish(payload_ref, True, "sessB")
        r2 = self._finish(payload_ref, True, "sessB")
        self.assertTrue(r2.get("duplicate"))
        self.assertEqual(r2["stats"]["correct"], r1["stats"]["correct"])
        self.assertEqual(self._word()["mastery"], 1)  # 没有被加两次

    def test_results_deduped_by_ref(self):
        """单次请求里重复的 ref 只计一次（双击/重试防御）。"""
        res = self.client.post("/api/finish", json={
            "mode": "audio_to_kana", "lesson": "lesson_01", "session_id": "s5",
            "results": [{"ref": "lesson_01:w001", "correct": True}] * 3,
        }).get_json()
        self.assertEqual(res["stats"]["total"], 1)
        self.assertEqual(self._word()["mastery"], 1)

    def test_record_filename_ms_and_session(self):
        """记录文件名带毫秒与 session_id：同秒交卷不互相覆盖、可幂等识别。"""
        self._finish("lesson_01:w001", True, "sessC")
        files = list(Path(app.QUIZZES_DIR).glob("*_sessC_*.json"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^\d{8}_\d{6}_\d{3}_sessC_audio_to_kana\.json$")

    def test_record_name_date_matches_timestamp(self):
        """摘要只按文件名头 8 位取日期，所以文件名前缀必须与 timestamp 同一天。

        两者本来是交卷时同一时刻写下来的；哪天有人改了命名或补数据，
        这条会先红，而不是让「今日已练 / 连续天数」悄悄少算一天。
        """
        self._finish("lesson_01:w001", True, "sessD")
        f = next(Path(app.QUIZZES_DIR).glob("*_sessD_*.json"))
        rec = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(f.name[:8], date.today().strftime("%Y%m%d"))
        self.assertEqual(str(rec["timestamp"])[:10], date.today().isoformat())


class TestLearnApi(unittest.TestCase):
    """学习模块：选词（m=0 按序）、完成标记（0→1，今天到期）、学习记录、refs 过滤。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "t", "words": [
            {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人"},
            {"id": "w002", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗", "mastery": 2},
            {"id": "w003", "kanji": "猫", "hiragana": "ねこ", "meaning": "猫"},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR = self._old
        self._tmp.cleanup()

    def test_start_picks_unlearned_in_order(self):
        """按词表顺序取 m=0 的词，已学词跳过；干扰项随每个词下发。"""
        data = self.client.post("/api/learn/start", json={
            "lesson": "lesson_01", "count": 7}).get_json()
        refs = [w["ref"] for w in data["words"]]
        self.assertEqual(refs, ["lesson_01:w001", "lesson_01:w003"])
        # 本批词自身不进池：两个词都只剩 w002 的释义
        self.assertEqual([w["distractors"] for w in data["words"]], [["狗"], ["狗"]])

    def test_finish_marks_seen_and_due_today(self):
        # 必须断言响应：只查副作用抓不到「视图没有 return」——那种情况下词表和
        # 记录都已写盘，看起来一切正常，但前端拿到的是 500 HTML 页
        res = self.client.post("/api/learn/finish", json={
            "lesson": "lesson_01", "refs": ["lesson_01:w001", "lesson_01:w003"]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"saved": True, "learned": 2})
        words = app.load_vocab()["lessons"]["lesson_01"]["words"]
        self.assertEqual(words[0]["mastery"], 1)
        self.assertEqual(words[0]["next_due"], date.today().isoformat())
        self.assertEqual(words[2]["mastery"], 1)
        # 幂等：重复 finish 不再涨掌握度
        res2 = self.client.post("/api/learn/finish", json={
            "lesson": "lesson_01", "refs": ["lesson_01:w001"]})
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.get_json(), {"saved": True, "learned": 1})
        words = app.load_vocab()["lessons"]["lesson_01"]["words"]
        self.assertEqual(words[0]["mastery"], 1)
        # 学习记录已写入：mode=learn，results 为空（不进错题本/正确率）
        recs = list(Path(app.QUIZZES_DIR).glob("*_learn.json"))
        self.assertEqual(len(recs), 2)
        rec = json.loads(recs[0].read_text(encoding="utf-8"))
        self.assertEqual(rec["results"], [])
        self.assertEqual(rec["total"], 2)

    def test_start_no_new_words(self):
        self.client.post("/api/learn/finish", json={
            "lesson": "lesson_01",
            "refs": ["lesson_01:w001", "lesson_01:w003"]})
        res = self.client.post("/api/learn/start", json={"lesson": "lesson_01"})
        self.assertEqual(res.status_code, 400)

    def test_start_refs_filter(self):
        """/api/start 的 refs 过滤：只出指定词（学习结束页「立即练习这批词」）。"""
        st = self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": "audio_to_kana", "count": 10,
            "schedule": "due", "refs": ["lesson_01:w002"]}).get_json()
        self.assertEqual([q["ref"] for q in st["questions"]], ["lesson_01:w002"])

    def test_stats_mode_names_include_learn(self):
        data = self.client.get("/api/stats").get_json()
        self.assertEqual(data["mode_names"]["learn"], app.LEARN_MODE_NAME)

    def test_start_carries_tip(self):
        # 给未学词 w001 写上讲解，learn/start 必须原样下发；没有则空串
        vocab = app.load_vocab()
        vocab["lessons"]["lesson_01"]["words"][0]["tip"] = "近义辨析小知识"
        app.save_vocab(vocab)
        data = self.client.post("/api/learn/start", json={
            "lesson": "lesson_01", "count": 7}).get_json()
        by_ref = {w["ref"]: w["word"] for w in data["words"]}
        self.assertEqual(by_ref["lesson_01:w001"]["tip"], "近义辨析小知识")
        self.assertEqual(by_ref["lesson_01:w003"]["tip"], "")


class TestHomeNav(unittest.TestCase):
    """首页四宫格（/api/home）：方块结构与概览数字。

    前端不硬编码任何模式列表，全靠这个接口下发——新模式若漏写 module 字段
    会「静默消失」（不报错，就是首页找不到入口），所以必须钉住归属完整性。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "第1课", "words": [
            # m=0 从未复习 → 未学且到期
            {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人"},
            # 已复习且未到期
            {"id": "w002", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗",
             "mastery": 3, "next_due": (date.today() + timedelta(days=9)).isoformat()},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "particles.json").write_text(json.dumps({"questions": [
            {"id": "p001", "sentence": "わたし＿学生です。", "answer": "は"}]}),
            encoding="utf-8")
        (tmp / "exam.json").write_text(json.dumps({"meta": {}, "questions": [
            {"id": "e001", "stem": "s", "options": ["1"], "answer": "1"}]}),
            encoding="utf-8")
        (tmp / "progress.md").write_text("# 学习进度\n", encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.PARTICLES_PATH, app.EXAM_PATH,
                     app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.PARTICLES_PATH = tmp / "particles.json"
        app.EXAM_PATH = tmp / "exam.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        self.client = app.app.test_client()

    def tearDown(self):
        (app.VOCAB_PATH, app.PARTICLES_PATH, app.EXAM_PATH,
         app.QUIZZES_DIR, app.BASE_DIR) = self._old
        self._tmp.cleanup()

    def test_every_practice_mode_is_placed_in_a_module(self):
        data = self.client.get("/api/home").get_json()
        self.assertEqual([m["id"] for m in data["modules"]],
                         [m["id"] for m in app.MODULE_DEFS])
        placed = {it["id"] for m in data["modules"] for it in m["items"]}
        for m in app.PRACTICE_MODES:
            self.assertTrue(m.get("module"), f"模式 {m['id']} 缺 module 字段")
            self.assertIn(m["id"], placed, f"模式 {m['id']} 没出现在任何方块里")
        for m in data["modules"]:
            self.assertTrue(m["items"], f"方块 {m['id']} 是空的")
            self.assertTrue(m["badge"], f"方块 {m['id']} 缺统计角标")

    def test_drill_items_grouped_and_bank_items_carry_counts(self):
        data = self.client.get("/api/home").get_json()
        drill = next(m for m in data["modules"] if m["id"] == "drill")
        self.assertTrue({it["group"] for it in drill["items"] if it["group"]})
        bank = {it["id"]: it for m in data["modules"] if m["id"] == "bank"
                for it in m["items"]}
        self.assertEqual(bank["particle"]["badge"], "1 题")
        self.assertEqual(bank["exam"]["badge"], "1 题")
        # 工具入口也要在：学新词/汉字卡/录入 → course，统计/错题本/文档 → review
        course = {it["id"] for m in data["modules"] if m["id"] == "course"
                  for it in m["items"]}
        self.assertLessEqual({"learn", "kanji", "add_word"}, course)
        review = {it["id"] for m in data["modules"] if m["id"] == "review"
                  for it in m["items"]}
        self.assertLessEqual({"stats", "wrongbook", "docs"}, review)

    def test_overview_numbers_match_data(self):
        ov = self.client.get("/api/home").get_json()["overview"]
        self.assertEqual(ov["unlearned"], 1)   # w001 未学
        self.assertEqual(ov["review_due"], 0)  # w002 练过但未到期 → 该复习的 0 个
        self.assertEqual(ov["due_words"], 1)   # w001 到期，w002 未到期
        self.assertEqual(ov["due_bank"], 2)    # 助词 1 题 + 真题 1 题，都没复习过
        self.assertEqual(ov["wrong_words"], 0)
        self.assertEqual(ov["today_done"], 0)
        self.assertEqual(ov["streak_days"], 0)  # quizzes 为空

    def test_docs_endpoint_reads_project_notes(self):
        data = self.client.get("/api/docs").get_json()
        self.assertIn("# 学习进度", data["progress"])
        self.assertIn("weaknesses.md", data["weaknesses"])  # 文件缺失时给出提示


class TestPracticePool(unittest.TestCase):
    """练习池：把「该复习的（练过的）」与「该学的（没练过的）」分开出题。

    起因：is_due 把从没练过的词也算到期，于是一个到期池里混着新词与复习词，
    打开练习抽到的多半是没学过的——那是在考、不是在学。
    """

    STATIC = Path(__file__).resolve().parent / "static"
    TEMPLATES = Path(__file__).resolve().parent / "templates"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        today = date.today()
        vocab = {"lessons": {
            "lesson_01": {"title": "第1课", "words": [
                # 没练过（新词），词表顺序 n1 → n2
                {"id": "n1", "kanji": "人", "hiragana": "ひと", "meaning": "人"},
                {"id": "n2", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗"},
                # 练过且到期（该复习）
                {"id": "r1", "kanji": "猫", "hiragana": "ねこ", "meaning": "猫",
                 "mastery": 1, "last_reviewed":
                     (today - timedelta(days=9)).isoformat()},
                # 练过且未到期
                {"id": "r2", "kanji": "鳥", "hiragana": "とり", "meaning": "鸟",
                 "mastery": 3, "last_reviewed": today.isoformat(),
                 "next_due": (today + timedelta(days=9)).isoformat()},
            ]},
            # 整课都是没练过的词：用来验证「到期复习」被筛空时报错而不是静默退回
            "lesson_02": {"title": "第2课", "words": [
                {"id": "n3", "kanji": "山", "hiragana": "やま", "meaning": "山"},
                {"id": "n4", "kanji": "川", "hiragana": "かわ", "meaning": "河"},
            ]},
        }}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, app.FOCUS_PATH)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.QUIZZES_DIR.mkdir()
        app.BASE_DIR = tmp
        app.FOCUS_PATH = tmp / "focus_words.json"
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, app.FOCUS_PATH = self._old
        self._tmp.cleanup()

    def _refs(self, **cfg):
        cfg.setdefault("mode", "kanji_to_kana")
        cfg.setdefault("count", 20)
        st = self.client.post("/api/start", json=cfg).get_json()
        return st, [q["ref"].split(":")[1] for q in st.get("questions", [])]

    def test_review_pool_only_learned_and_due_first(self):
        """到期复习：只出练过的词，到期的（r1）排在未到期的（r2）前面。"""
        st, ids = self._refs(lesson="lesson_01", pool="review")
        self.assertEqual(ids, ["r1", "r2"])
        self.assertNotIn("n1", ids)
        self.assertNotIn("n2", ids)

    def test_new_pool_only_unlearned_in_word_order(self):
        """新词池只出没练过的词，按词表顺序（不是随机）——编排顺序即学习顺序。"""
        st, ids = self._refs(lesson="lesson_01", pool="new")
        self.assertEqual(ids, ["n1", "n2"])

    def test_all_pool_keeps_legacy_behaviour(self):
        """全部：不筛选（旧行为）——新词也在内，未到期的词排最后。"""
        st, ids = self._refs(lesson="lesson_01", pool="all")
        self.assertEqual(sorted(ids), ["n1", "n2", "r1", "r2"])
        self.assertEqual(ids[-1], "r2")  # 未到期，补在末尾

    def test_invalid_pool_falls_back_to_all(self):
        st, ids = self._refs(lesson="lesson_01", pool="nonsense")
        self.assertEqual(sorted(ids), ["n1", "n2", "r1", "r2"])

    def test_pool_also_applies_to_random_schedule(self):
        """随机出题也要先分池：否则「新词」一洗牌就被洗没了。"""
        st, ids = self._refs(lesson="lesson_01", pool="new", schedule="random")
        self.assertEqual(sorted(ids), ["n1", "n2"])

    def test_empty_pool_errors_with_guidance(self):
        """池子被筛空：明确报错并给出下一步，不静默退回整本词库。"""
        st, _ = self._refs(lesson="lesson_02", pool="review")
        self.assertIn("error", st)
        self.assertIn("新词", st["error"])
        st, _ = self._refs(lesson="lesson_02", pool="new")
        self.assertNotIn("error", st)

    def test_named_refs_bypass_the_pool(self):
        """学习完成页「立即练习这批词」点名要练的词不受池子影响。"""
        st, ids = self._refs(lesson="lesson_01", pool="review", refs=["lesson_01:n1"])
        self.assertEqual(ids, ["n1"])

    def test_is_learned_accepts_legacy_progress_fields(self):
        """只有 next_due / mastery、没有 last_reviewed 的旧数据也算练过。"""
        self.assertTrue(app.is_learned({"mastery": 2}))
        self.assertTrue(app.is_learned({"next_due": "2026-01-01"}))
        self.assertFalse(app.is_learned({"mastery": 0}))

    def test_frontend_wires_the_pool_selector(self):
        """配置页的「练习内容」三选一 → /api/start 的 pool 字段。"""
        html = (self.TEMPLATES / "index.html").read_text(encoding="utf-8")
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="pool-row"', html)
        for value in app.POOL_KINDS:
            self.assertIn(f'name="pool" value="{value}"', html)
        self.assertIn('toggleRow("pool-row"', appjs)
        self.assertIn('input[name="pool"]:checked', appjs)
        self.assertIn("schedule, pool, exam_source", appjs)


class TestViewResponses(unittest.TestCase):
    """所有路由函数都必须返回响应。

    `api_learn_finish` 曾因在 with 块末尾与新代码之间丢了 return：Flask 抛
    「did not return a valid response」，前端拿到 500 的 HTML 错误页，
    `res.json()` 于是报 `Unexpected token '<'`——而词表与练习记录其实都已写盘。
    只断言副作用的测试抓不到这个（数据全是对的），必须静态查返回值。
    """
    APP = Path(__file__).resolve().parent / "app.py"

    @staticmethod
    def _route_functions(tree):
        out = []
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if any(isinstance(d, ast.Call)
                   and getattr(getattr(d, "func", None), "attr", "") == "route"
                   for d in node.decorator_list):
                out.append(node)
        return out

    @staticmethod
    def _returns_value(fn):
        """函数自身（不深入嵌套定义）是否存在带值的 return。"""
        stack = list(fn.body)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # 嵌套定义的 return 不算视图的返回
            if isinstance(n, ast.Return) and n.value is not None:
                return True
            stack.extend(ast.iter_child_nodes(n))
        return False

    def test_every_route_returns_a_response(self):
        tree = ast.parse(self.APP.read_text(encoding="utf-8"))
        routes = self._route_functions(tree)
        self.assertTrue(routes, "没解析到任何路由：装饰器判断可能需要更新")
        missing = [f.name for f in routes if not self._returns_value(f)]
        self.assertEqual(missing, [], f"路由函数缺少 return：{missing}")


class TestDocQuotedCounts(unittest.TestCase):
    """文档生成区**之外**手抄的出题数，必须与代码算出来的一致。

    同一个数字 README 和 weaknesses.md 各抄了一份，词表一涨就漂（曾经 820 与
    847 并存）。progress.md 的表格由 report.py 刷新，这里守的是刷不到的那部分。
    TODO.md 的「已完成」是按日期的变更日志，当时的数字是事实，不参与校对。
    """
    DOCS = ("README.md", "weaknesses.md")

    def eligible(self, mode_id):
        mode = next(m for m in app.PRACTICE_MODES if m["id"] == mode_id)
        vocab = app.load_vocab()
        return sum(
            1 for lesson_id, ldata in vocab.get("lessons", {}).items()
            for word in ldata.get("words", [])
            if app.mode_accepts(word, mode, lesson_id=lesson_id)
        )

class TestVocabFileFormat(unittest.TestCase):
    """vocabulary.json 必须保持 app 的「每词一行」写入口径。

    为什么值得钉：词表在 `61c005a`（音声作品 / 查词兜底 / 图片日语 / 动词活用那一轮）
    被整体 `indent=2` 展开过——17565 行；而 app 每次练习结束都按 `_format_vocab()`
    保存，于是下一次保存就把它改回来，git diff 一次性冒出 1.7 万行（逐条比对过：
    7 课 / 1238 词一个不差，只有 65 处进度字段真的变了）。

    内容没错，错的是**没有任何东西保证「磁盘上的写法」与「app 的写法」一致**——
    不拦住，它就会再来一次，而且每次都藏在一堆噪声里。
    """

    def setUp(self):
        # 按文件位置算路径：不受其他用例给 app.VOCAB_PATH 打的桩影响
        self.path = Path(app.__file__).resolve().parent.parent / "vocabulary.json"

class TestQuizScanScope(unittest.TestCase):
    """首页那两处摘要只解析**今天**的记录，历史只数文件名里的日期。

    起因：首页每次加载要把全部历史解析三遍（实测 172 条 = 22 ms × 3），
    且随文件数线性涨（5000 条时每次约 640 ms）。而「练过哪些天」和「今天练了多少」
    本来就不需要读历史文件——文件名头 8 位就是日期（交卷时与 timestamp 同一时刻写死）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.qdir = Path(self._tmp.name) / "quizzes"
        self.qdir.mkdir()
        self._old = app.QUIZZES_DIR
        app.QUIZZES_DIR = self.qdir

    def tearDown(self):
        app.QUIZZES_DIR = self._old
        self._tmp.cleanup()

    def _rec(self, name, day, total):
        (self.qdir / name).write_text(json.dumps(
            {"timestamp": f"{day} 09:00:00", "mode": "kana_to_cn", "lesson": "all",
             "total": total, "results": []}, ensure_ascii=False), encoding="utf-8")
        return name

    def test_only_todays_files_are_parsed(self):
        today = date.today()
        old = self._rec("20200101_000000_000_old_kana_to_cn.json", "2020-01-01", 99)
        cur = self._rec(f"{today:%Y%m%d}_090000_000_t_kana_to_cn.json",
                        today.isoformat(), 7)
        calls = []
        real_load = app.json.load
        app.json.load = lambda fp: (calls.append(Path(fp.name).name), real_load(fp))[1]
        try:
            summary = app._quiz_days_summary()
            act = app._today_plan_activity([])
        finally:
            app.json.load = real_load
        self.assertIn(cur, calls, "今天的记录没被算进去")
        self.assertNotIn(old, calls, "历史记录被解析了：首页摘要又会随文件数线性变慢")
        self.assertEqual(summary["today_done"], 7, "只该算今天的题数")
        self.assertEqual(summary["streak_days"], 1)
        self.assertEqual(act["word"], 7)

    def test_day_set_comes_from_filenames(self):
        """连续天数只看文件名：连着三天就算练过，记录内容是什么都不影响。"""
        today = date.today()
        for d in (today, today - timedelta(days=1), today - timedelta(days=2)):
            (self.qdir / f"{d:%Y%m%d}_080000_000_x_learn.json").write_text(
                "{}", encoding="utf-8")
        self.assertEqual(app._quiz_days_summary()["streak_days"], 3)


class TestReportVocabStats(unittest.TestCase):
    """progress.md 的词表统计段：「到期」必须拆成 复习到期 / 未学 两列。

    起因：is_due 把从没练过的词也算到期，所以那个单一数字永远是「1238 词里 1233
    到期」——复习做得再好它也不动，当不了进度反馈。首页早就分成两个轴
    （build_home_payload），报告这边跟着拆。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        today = date.today()
        vocab = {"lessons": {"lesson_01": {"title": "第1课", "words": [
            {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人",
             "example_ja": "人は学生です。"},                       # 从未练过 → 未学
            {"id": "w002", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗",
             "mastery": 3, "example_ja": "犬が好きです。",
             "last_reviewed": (today - timedelta(days=10)).isoformat(),
             "next_due": (today - timedelta(days=3)).isoformat()},  # 到期，逾期 3 天
            {"id": "w003", "kanji": "猫", "hiragana": "ねこ", "meaning": "猫",
             "mastery": 4, "example_ja": "猫が寝ます。",
             "next_due": (today + timedelta(days=9)).isoformat()},  # 已掌握，未到期
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        self._old = (app.VOCAB_PATH, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.BASE_DIR = tmp

    def tearDown(self):
        app.VOCAB_PATH, app.BASE_DIR = self._old
        self._tmp.cleanup()

    def _block(self):
        import report as report_mod      # 局部导入：顶层的 report 名字太容易撞
        return report_mod.vocab_stats_block()

    def test_due_column_is_split(self):
        block = self._block()
        self.assertIn("| 课程 | 词条 | 掌握度分布 | 复习到期 | 未学 |", block)
        row = next(ln for ln in block.splitlines() if ln.startswith("| 第1课"))
        cells = [c.strip() for c in row.strip("|").split("|")]
        self.assertEqual(cells[1:], ["3", "0分×1 / 3分×1 / 4分×1", "1", "1"])
        total = next(ln for ln in block.splitlines() if "合计" in ln)
        self.assertTrue(total.endswith("| 1 | 1 |"), total)

    def test_review_debt_and_mastered_lines(self):
        block = self._block()
        self.assertIn("复习债：1 个练过的词已到期（最久逾期 3 天），未学 1 个", block)
        # 「已掌握」会随练习往上走；旧的「掌握度 0-1 占 87%」不会动，已作废
        self.assertIn("已掌握（掌握度 ≥4）1 个，占 33%", block)
        self.assertNotIn("掌握度 0-1", block)

    def test_empty_vocab_does_not_divide_by_zero(self):
        app.VOCAB_PATH.write_text(json.dumps({"lessons": {}}), encoding="utf-8")
        self.assertIn("词表为空", self._block())


class TestFinishInputGuards(unittest.TestCase):
    """/api/finish 的 mode 会进记录文件名，非法值必须挡住。

    实测过没挡之前的后果：`mode="a/../../vocabulary"` 返回 200，同时把
    vocabulary.json 整个覆盖成那条练习记录——词表与全部掌握度当场没了，
    而且没有任何报错。session_id 早就按同样理由清洗过，mode 是漏掉的那个。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "t", "words": [
            {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人"}]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        (tmp / "audio").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, app.AUDIO_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        app.AUDIO_DIR = tmp / "audio"
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, app.AUDIO_DIR = self._old
        self._tmp.cleanup()

    def _finish(self, mode):
        return self.client.post("/api/finish", json={
            "mode": mode, "results": [{"ref": "lesson_01:w001", "correct": True}]})

    def test_traversal_mode_rejected_and_vocab_intact(self):
        before = app.VOCAB_PATH.read_text(encoding="utf-8")
        for mode in ("a/../../vocabulary", "a/..\\..\\vocabulary", "../x", ""):
            res = self._finish(mode)
            self.assertEqual(res.status_code, 400, f"mode={mode!r} 没有被拒绝")
        self.assertEqual(app.VOCAB_PATH.read_text(encoding="utf-8"), before, "词表被改写")
        self.assertEqual(list(app.QUIZZES_DIR.iterdir()), [], "非法请求仍落了记录")

    def test_known_mode_still_saves(self):
        """挡住非法值不能顺手把正常交卷也挡掉。"""
        res = self._finish("kanji_to_kana")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["saved"])
        self.assertEqual(len(list(app.QUIZZES_DIR.glob("*.json"))), 1)

    def test_audio_ref_cannot_leave_audio_dir(self):
        """audio_path_for_ref 只滤 `/` 时，Windows 上反斜杠照样能出去。"""
        for ref in ("..\\evil", "..\\..\\..\\escape", "lesson_01:w001"):
            path = app.audio_path_for_ref(ref)
            self.assertEqual(path.parent, app.AUDIO_DIR, f"ref={ref!r} 跑到了 {path}")


class TestMcqApi(unittest.TestCase):
    """多邻国式选择题：看词选意思/听音选意思/例句填空 + 选择题连对两次才 +1。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {
            "lesson_01": {"title": "t", "words": [
                {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人",
                 "example_ja": "あの人は学生です。"},
                {"id": "w002", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗",
                 "example_ja": "犬が好きです。"},
                {"id": "w003", "kanji": "猫", "hiragana": "ねこ", "meaning": "猫",
                 "example_ja": "猫が寝ています。"},
                {"id": "w004", "kanji": "---", "hiragana": "のりかえ", "meaning": "换乘",
                 "example_ja": "駅で乗り換えます。"},  # 活用形，原形不在句中
            ]},
            "lesson_02_phrases": {"title": "句型", "words": [
                {"id": "w001", "kanji": "---", "hiragana": "をみます", "meaning": "看（某物）"},
            ]},
        }}
        exam = {"meta": {}, "questions": [
            {"id": "e001", "stem": "q1", "options": ["a", "b", "c", "d"], "answer": 1},
            {"id": "e002", "stem": "q2", "options": ["a", "b", "c", "d"], "answer": 2},
        ]}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "exam_n5.json").write_text(
            json.dumps(exam, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.EXAM_PATH, app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.EXAM_PATH = tmp / "exam_n5.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.EXAM_PATH, app.QUIZZES_DIR, app.BASE_DIR = self._old
        self._tmp.cleanup()

    def _finish(self, mode, results, session_id="m1"):
        return self.client.post("/api/finish", json={
            "mode": mode, "lesson": "lesson_01", "session_id": session_id,
            "results": results,
        }).get_json()

    def _word(self, idx=0):
        return app.load_vocab()["lessons"]["lesson_01"]["words"][idx]

    def test_kana_to_cn_options_and_check(self):
        st = self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": "kana_to_cn",
            "count": 10, "schedule": "random"}).get_json()
        # 「人」「猫」的释义与词形同字，正确选项直接印在题干上 → 送分题，不出
        self.assertEqual({q["ref"] for q in st["questions"]},
                         {"lesson_01:w002", "lesson_01:w004"})
        self.assertEqual(st["total"], 2)
        for q in st["questions"]:
            self.assertEqual(len(q["options"]), 4)
            self.assertNotIn(q["prompt"], q["options"])  # 题干不得就是正确选项
            self.assertIn(q["prompt"], ("犬", "のりかえ"))
            ok = self.client.post("/api/check", json={
                "ref": q["ref"], "mode": "kana_to_cn",
                "answer": q["options"][0]}).get_json()
            # 选项之一必为正确答案：正确项文本应判对（逐个试到对为止）
            tried = ok["correct"]
            for opt in q["options"][1:]:
                if tried:
                    break
                r = self.client.post("/api/check", json={
                    "ref": q["ref"], "mode": "kana_to_cn", "answer": opt}).get_json()
                tried = r["correct"]
            self.assertTrue(tried, f"四个选项没有一个判对: {q}")

    def test_cloze_mode_derives_blank_and_skips_inflected(self):
        st = self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": "cloze_cn",
            "count": 10, "schedule": "random"}).get_json()
        refs = {q["ref"] for q in st["questions"]}
        self.assertNotIn("lesson_01:w004", refs)  # 活用形句子不挖空
        for q in st["questions"]:
            self.assertIn("＿", q["prompt"])
            self.assertEqual(len(q["options"]), 4)
            # 用正确词形（选项之一）作答应判对
            w = app.resolve_word(app.load_vocab(), q["ref"])[1]
            ans = app.expected_for(w, app.get_mode("cloze_cn"))
            res = self.client.post("/api/check", json={
                "ref": q["ref"], "mode": "cloze_cn", "answer": ans}).get_json()
            self.assertTrue(res["correct"], q["prompt"])
            self.assertIn(ans, q["options"])

    def test_audio_to_cn_skips_phrase_lessons(self):
        st = self.client.post("/api/start", json={
            "lesson": "all", "mode": "audio_to_cn",
            "count": 10, "schedule": "random"}).get_json()
        self.assertTrue(all(q["lesson_id"] == "lesson_01" for q in st["questions"]))

    def test_mcq_needs_two_corrects_to_promote(self):
        ref = "lesson_01:w001"
        self._finish("kana_to_cn", [{"ref": ref, "correct": True}])
        w = self._word()
        self.assertEqual(w["mastery"], 0)  # 第一次答对不晋升
        self.assertEqual(w["streak_ok"], 1)
        self.assertEqual(w["next_due"], (date.today() + timedelta(days=1)).isoformat())  # 明天继续练
        self._finish("kana_to_cn", [{"ref": ref, "correct": True}], session_id="m2")
        w = self._word()
        self.assertEqual(w["mastery"], 1)  # 连对两次才 +1
        self.assertEqual(w["streak_ok"], 0)

    def test_mcq_wrong_resets_streak(self):
        ref = "lesson_01:w001"
        self._finish("kana_to_cn", [{"ref": ref, "correct": True}])
        self._finish("kana_to_cn", [{"ref": ref, "correct": False}], session_id="m2")
        w = self._word()
        self.assertEqual(w["mastery"], 0)
        self.assertEqual(w["streak_ok"], 0)  # 答错清零

    def test_typing_mode_stays_single_correct(self):
        """打字模式不受收紧影响：答对即 +1（回归验证）。"""
        ref = "lesson_01:w001"
        self._finish("audio_to_kana", [{"ref": ref, "correct": True}])
        self.assertEqual(self._word()["mastery"], 1)

    def test_exam_streak_gate(self):
        self._finish("exam", [{"ref": "exam:e001", "correct": True}], session_id="e1")
        bdata = app.load_exam()
        q1 = bdata["questions"][0]
        self.assertEqual(q1["mastery"], 0)  # 真题同样连对两次才 +1
        self.assertEqual(q1["streak_ok"], 1)
        self._finish("exam", [{"ref": "exam:e001", "correct": True},
                              {"ref": "exam:e002", "correct": True}], session_id="e2")
        bdata = app.load_exam()
        self.assertEqual(bdata["questions"][0]["mastery"], 1)
        self.assertEqual(bdata["questions"][1]["mastery"], 0)  # e002 第一次答对

    def test_exam_still_serves_options(self):
        """回归：真题模式出题与判分不受重构影响。"""
        st = self.client.post("/api/start", json={
            "lesson": "all", "mode": "exam", "count": 2}).get_json()
        self.assertEqual(st["total"], 2)
        q = app.load_exam()["questions"][0]
        res = self.client.post("/api/check", json={
            "ref": f"exam:{q['id']}", "mode": "exam",
            "answer": q["options"][q["answer"]]}).get_json()
        self.assertTrue(res["correct"])


class TestUniqueAnswer(unittest.TestCase):
    """四选一/配对题必须只有唯一正确答案：题干要能定答案，选项之间不能同义。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "t", "words": [
            {"id": "w001", "kanji": "先生", "hiragana": "せんせい", "meaning": "老师",
             "example_ja": "あの方は私の先生です。"},
            {"id": "w002", "kanji": "学生", "hiragana": "がくせい", "meaning": "学生",
             "example_ja": "あの人は学生です。"},
            {"id": "w003", "kanji": "教師", "hiragana": "きょうし", "meaning": "老师，教师",
             "example_ja": "田中さんは教師です。"},  # 与 w001 同义
            {"id": "w004", "kanji": "---", "hiragana": "きょうしつ", "meaning": "教室",
             "example_ja": "教室に入ります。"},
            {"id": "w005", "kanji": "本", "hiragana": "ほん", "meaning": "书",
             "example_ja": "本を読みます。"},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR = self._old
        self._tmp.cleanup()

    def _start(self, mode):
        return self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": mode,
            "count": 10, "schedule": "random"}).get_json()

    def test_gloss_senses_splits_but_keeps_confusables(self):
        # 同义判定：多义项拆开比对
        self.assertTrue(app.gloss_senses("老师")
                        & app.gloss_senses("老师，教师"))
        # 易混但不同义的单字片段不能误判（这周/下周 是好的干扰项）
        self.assertFalse(app.gloss_senses("这周") & app.gloss_senses("下周"))
        self.assertEqual(app.gloss_senses(""), set())

    def test_cloze_prompt_carries_meaning_hint(self):
        """空位不唯一决定词形：「あの方は私の ＿ です。」填学生/教师都通，题干必须给释义。"""
        q = next(x for x in self._start("cloze_cn")["questions"]
                 if x["ref"] == "lesson_01:w001")
        self.assertEqual(q["prompt"], "あの方は私の＿です。（老师）")
        self.assertIn("先生", q["options"])
        self.assertNotIn("教師", q["options"])  # 同义项不能当干扰项
        self.assertEqual(len(q["options"]), 4)

    def test_meaning_options_exclude_synonym_glosses(self):
        q = next(x for x in self._start("kana_to_cn")["questions"]
                 if x["ref"] == "lesson_01:w001")
        self.assertNotIn("老师，教师", q["options"])
        self.assertIn("学生", q["options"])

    def test_pair_rounds_have_no_overlapping_senses_or_forms(self):
        st = self._start("pair_match")
        self.assertTrue(st["questions"])
        for q in st["questions"]:
            pairs = q["pairs"]
            for a, b in itertools.combinations(pairs, 2):
                self.assertFalse(app.gloss_senses(a["meaning"])
                                 & app.gloss_senses(b["meaning"]),
                                 f"同轮出现可互换的释义: {a} {b}")
                self.assertNotEqual(a["form"], b["form"])

    def test_learn_distractors_filtered_per_word(self):
        data = self.client.post("/api/learn/start", json={
            "lesson": "lesson_01", "count": 2}).get_json()
        by_ref = {x["ref"]: x for x in data["words"]}
        self.assertNotIn("老师，教师", by_ref["lesson_01:w001"]["distractors"])
        self.assertIn("老师，教师", by_ref["lesson_01:w002"]["distractors"])


class TestCourseApi(unittest.TestCase):
    """课程模式：混合题型与脚手架、组句排序、错题回炉不计分。"""

    @classmethod
    def setUpClass(cls):
        try:
            import janome  # noqa: F401
            cls.has_janome = True
        except ImportError:
            cls.has_janome = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {
            "lesson_01": {"title": "t", "words": [
                # 新词 m0：预习卡 + 选择题
                {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人",
                 "example_ja": "あの人は学生です。"},
                {"id": "w002", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗",
                 "example_ja": "犬が好きです。"},
                # m2：例句填空（语境再认）
                {"id": "w003", "kanji": "猫", "hiragana": "ねこ", "meaning": "猫",
                 "example_ja": "猫が寝ています。", "mastery": 2},
                # m3 + 例句可分块：组句排序
                {"id": "w004", "kanji": "本", "hiragana": "ほん", "meaning": "书",
                 "example_ja": "図書館で本を読みます。", "mastery": 3},
                # m3 无例句：打字
                {"id": "w005", "kanji": "水", "hiragana": "みず", "meaning": "水",
                 "mastery": 3},
                # 第二个 m3 + 例句：拼句的第二道走听写（与组句交替）
                {"id": "w006", "kanji": "茶", "hiragana": "ちゃ", "meaning": "茶",
                 "example_ja": "毎日お茶を飲みます。", "mastery": 3},
            ]},
            "lesson_02_phrases": {"title": "句型", "words": [
                {"id": "w001", "kanji": "---", "hiragana": "をみます", "meaning": "看（某物）"},
            ]},
        }}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR = self._old
        self._tmp.cleanup()

    def test_course_composition(self):
        """混合题型与脚手架：新词选择题、m2 填空、m3 拼句/打字；句型课排除。"""
        st = self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": "course", "count": 15}).get_json()
        self.assertEqual(st["total"], 6)  # 2 新词 + 4 复习
        by_ref = {q["ref"]: q for q in st["questions"]}
        # 新词：带预习卡 + 选择题题型
        for ref in ("lesson_01:w001", "lesson_01:w002"):
            self.assertIn(by_ref[ref]["intro"]["meaning"], ("人", "狗"))
            self.assertIn(by_ref[ref]["mode"],
                          ("kana_to_cn", "audio_to_cn", "cloze_cn"))
            self.assertTrue(all(o in by_ref[ref]["options"] for o in []) or
                            len(by_ref[ref]["options"]) == 4)
        # m2 → 例句填空（例句可用）；m3+例句 → 拼句；m3 不能拼句 → 打字
        # 打字在「看词形写假名 / 听音写假名」之间交替（两者都要求该词能出这道题）
        self.assertEqual(by_ref["lesson_01:w003"]["mode"], "cloze_cn")
        if self.has_janome:
            self.assertEqual(by_ref["lesson_01:w004"]["mode"], "order_cn")
            self.assertGreaterEqual(len(by_ref["lesson_01:w004"]["blocks"]), 3)
            # 第二道拼句轮到听写：词块照旧，题干换成整句发音
            self.assertEqual(by_ref["lesson_01:w006"]["mode"], "dictation")
            self.assertGreaterEqual(len(by_ref["lesson_01:w006"]["blocks"]), 3)
            self.assertTrue(by_ref["lesson_01:w006"]["audio"].startswith("/api/tts/text?text="))
            self.assertEqual(by_ref["lesson_01:w005"]["mode"], "kanji_to_kana")
        else:
            self.assertEqual(by_ref["lesson_01:w004"]["mode"], "kanji_to_kana")
            self.assertEqual(by_ref["lesson_01:w005"]["mode"], "audio_to_kana")
            self.assertEqual(by_ref["lesson_01:w006"]["mode"], "kanji_to_kana")
        # 课内题序（脚手架）：选择题 → 拼句 → 打字
        kinds = [q["mode"] for q in st["questions"]]
        first_typing = next((i for i, m in enumerate(kinds)
                             if m in ("kanji_to_kana", "audio_to_kana")), len(kinds))
        self.assertTrue(all(m in ("kana_to_cn", "audio_to_cn", "cloze_cn",
                                  "order_cn", "dictation")
                            for m in kinds[:first_typing]))
        # 句型课不进课程
        self.assertTrue(all(q["lesson_id"] == "lesson_01" for q in st["questions"]))

    def test_order_check(self):
        """组句判分：原句序判对，乱序判错。"""
        vocab = app.load_vocab()
        w = vocab["lessons"]["lesson_01"]["words"][3]
        self._start_course()
        res = self.client.post("/api/check", json={
            "ref": "lesson_01:w004", "mode": "order_cn",
            "answer": w["example_ja"]}).get_json()
        self.assertTrue(res["correct"])
        self.assertEqual(res["expected"], w["example_ja"])
        bad = self.client.post("/api/check", json={
            "ref": "lesson_01:w004", "mode": "order_cn",
            "answer": w["example_ja"][::-1]}).get_json()
        self.assertFalse(bad["correct"])

    def test_finish_course_mastery_by_qtype(self):
        """课程计分按题型：选择题连对两次才 +1，打字/组句答对即 +1。"""
        self._start_course()
        self.client.post("/api/finish", json={
            "mode": "course", "lesson": "lesson_01", "session_id": "c1",
            "review_count": 2,
            "results": [
                {"ref": "lesson_01:w001", "correct": True, "qtype": "kana_to_cn"},
                {"ref": "lesson_01:w004", "correct": True, "qtype": "order_cn"},
                {"ref": "lesson_01:w005", "correct": True, "qtype": "kanji_to_kana"},
            ]}).get_json()
        words = app.load_vocab()["lessons"]["lesson_01"]["words"]
        self.assertEqual(words[0]["mastery"], 0)  # 选择题：连对一次不够
        self.assertEqual(words[0]["streak_ok"], 1)
        self.assertEqual(words[3]["mastery"], 4)  # 组句：答对即 +1
        self.assertEqual(words[4]["mastery"], 4)  # 打字（看词形写假名）：答对即 +1
        recs = list(Path(app.QUIZZES_DIR).glob("*_course.json"))
        self.assertEqual(len(recs), 1)
        rec = json.loads(recs[0].read_text(encoding="utf-8"))
        self.assertEqual(rec["review_count"], 2)

    def _start_course(self):
        return self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": "course", "count": 15}).get_json()

    def test_all_lessons_duplicate_word_ids_still_reviewable(self):
        """跨课同 id 不得互相顶掉：词 id 是课内短号（真实词库里 456 个 id 在多课
        重复，w001 出现 7 次）。

        选「全部课程」且到期词不够时从未到期词里补位。按裸 id 比对，lesson_02 的
        w001 会因为 lesson_01 有个同名新词进了预习名额而被整条排除，永远出不到题。
        """
        vocab = {"lessons": {
            "lesson_01": {"title": "1", "words": [
                {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人",
                 "example_ja": "人は学生です。"},                  # m0 → 预习名额
                {"id": "w002", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗",
                 "mastery": 3, "next_due": "2026-01-01"},          # 到期复习词
            ]},
            "lesson_02": {"title": "2", "words": [
                {"id": "w001", "kanji": "猫", "hiragana": "ねこ", "meaning": "猫",
                 "mastery": 3, "next_due": "2099-01-01"},          # 未到期：只能走补位
            ]},
        }}
        app.VOCAB_PATH.write_text(json.dumps(vocab, ensure_ascii=False),
                                  encoding="utf-8")
        st = self.client.post("/api/start", json={
            "lesson": "all", "mode": "course", "count": 10}).get_json()
        refs = {q["ref"] for q in st["questions"]}
        self.assertIn("lesson_01:w001", refs)   # 新词预习卡
        self.assertIn("lesson_01:w002", refs)   # 到期复习
        self.assertIn("lesson_02:w001", refs)   # 补位词：曾被同名新词顶掉


class TestFormToAudio(unittest.TestCase):
    """看词形选发音：题干是词形，四个选项各是一段发音（见字知音）。"""

    @classmethod
    def setUpClass(cls):
        cls.client = app.app.test_client()

    def test_requires_kanji_and_speakable(self):
        """纯假名词的题干就是答案；读音念不出来的（句型 xx 占位符）也不出题。"""
        md = app.get_mode("form_to_audio")
        self.assertTrue(app.mode_accepts({"kanji": "学生", "hiragana": "がくせい"}, md))
        self.assertFalse(app.mode_accepts({"kanji": "---", "hiragana": "あそこ"}, md))
        self.assertFalse(app.mode_accepts({"kanji": "学生", "hiragana": "xxです"}, md))


class TestDictation(unittest.TestCase):
    """例句听写：题干是整句发音，用词块拼出整句（与组句共用 UI 与判分）。"""

    @classmethod
    def setUpClass(cls):
        cls.client = app.app.test_client()
        try:
            import janome  # noqa: F401
            cls.has_janome = True
        except ImportError:
            cls.has_janome = False

class TestPairMatchApi(unittest.TestCase):
    """词义配对：每轮 5 词、同轮释义去重、连对闸与选择题一致。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        words = [{"id": f"w{i:03d}", "kanji": f"字{i}", "hiragana": f"じ{i}",
                  "meaning": f"义{i}"} for i in range(1, 12)]
        words.append({"id": "w012", "kanji": "字6", "hiragana": "じ6b",
                      "meaning": "义6"})  # 与 w6 释义相同：同轮必须去重
        vocab = {"lessons": {
            "lesson_01": {"title": "t", "words": words},
            "lesson_02_phrases": {"title": "句型", "words": [
                {"id": "w001", "kanji": "---", "hiragana": "をみます", "meaning": "看（某物）"},
            ]},
        }}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR = self._old
        self._tmp.cleanup()

    def test_rounds_built_with_distinct_meanings(self):
        st = self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": "pair_match", "count": 10}).get_json()
        self.assertEqual(st["total"], 2)  # 12 词（两词释义重复）→ 2 轮
        seen_refs = set()
        for q in st["questions"]:
            self.assertEqual(len(q["pairs"]), 5)
            meanings = [p["meaning"] for p in q["pairs"]]
            self.assertEqual(len(set(meanings)), 5)  # 同轮释义互不相同
            for p in q["pairs"]:
                self.assertNotIn(p["ref"], seen_refs)  # 一个词只出一轮
                seen_refs.add(p["ref"])
        self.assertTrue(all(q["lesson_id"] == "lesson_01" for q in st["questions"]))

    def test_finish_applies_streak_gate(self):
        self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": "pair_match", "count": 5})
        self.client.post("/api/finish", json={
            "mode": "pair_match", "lesson": "lesson_01", "session_id": "p1",
            "results": [{"ref": "lesson_01:w001", "correct": True}]})
        w = app.load_vocab()["lessons"]["lesson_01"]["words"][0]
        self.assertEqual(w["mastery"], 0)  # 配对是全提示再认：连对两次才 +1
        self.assertEqual(w["streak_ok"], 1)

    def test_too_few_words(self):
        vocab = {"lessons": {"lesson_01": {"title": "t", "words": [
            {"id": "w001", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗"},
        ]}}}
        app.save_vocab(vocab)
        res = self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": "pair_match", "count": 5})
        self.assertEqual(res.status_code, 400)


class TestDistractorVariety(unittest.TestCase):
    """回归：选择题干扰项不能是「课程开头前 3 词」的固定组合。

    _build_mcq_options 曾按池子顺序取前 3 个可用候选——池子按词表顺序构建，
    于是第 1 课开头的 中国人/日本人/学生 成了几乎每题都在的常驻干扰项
    （选项只在 4 个位置上洗牌，内容不变）。修复后每题对池子副本洗牌再抽。
    """

    def test_distractor_combos_vary_across_questions(self):
        words = [{"id": f"w{i:03d}", "kanji": f"词{i}", "hiragana": f"ことば{i}",
                  "meaning": f"意思{i}"} for i in range(12)]
        mode = app.get_mode("kana_to_cn")
        pool = app._mcq_pool(words, mode)
        self.assertEqual(len(pool), 12)
        combos = set()
        for _ in range(40):
            opts = app._build_mcq_options(words[0], mode, pool)
            self.assertEqual(len(opts), 4)
            self.assertIn(words[0]["meaning"], opts)
            combos.add(tuple(sorted(set(opts) - {words[0]["meaning"]})))
        # 11 个候选里抽 3 个共 165 种组合：仍按固定顺序取时 40 次全是同一种
        self.assertGreater(len(combos), 5,
                           "干扰项组合几乎不变：疑似仍按池子顺序取前 3 个")

    def test_distractors_stay_within_pool(self):
        """抽样随机化不改变池子约束：干扰项必须来自池内，且不与答案同义项。"""
        words = [{"id": f"w{i:03d}", "kanji": f"词{i}", "hiragana": f"ことば{i}",
                  "meaning": m} for i, m in enumerate(
                      ["学生", "学生，老师", "中国人", "日本人", "猫", "狗", "花", "鸟",
                       "山", "川", "本", "車"])]
        mode = app.get_mode("kana_to_cn")
        pool = app._mcq_pool(words, mode)
        own = app.gloss_senses("学生")
        for _ in range(20):
            opts = app._build_mcq_options(words[0], mode, pool)
            self.assertEqual(len(opts), 4)
            # 返回前整体洗牌过，正确答案可能在任意位置：用集合差取真正的干扰项
            for t in set(opts) - {"学生"}:
                self.assertFalse(app.gloss_senses(t) & own,
                                 f"干扰项「{t}」与正确答案同义项")
                self.assertTrue(any(t == text for text, _ in pool))


class TestKanjiApi(unittest.TestCase):
    """汉字识字卡：按词条逐字聚合、去重、按包含词数降序。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "t", "words": [
            {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人"},
            {"id": "w002", "kanji": "大人", "hiragana": "おとな", "meaning": "大人"},
            {"id": "w003", "kanji": "人々", "hiragana": "ひとびと", "meaning": "人们"},
            {"id": "w004", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗"},
            {"id": "w005", "kanji": "---", "hiragana": "のりかえ", "meaning": "换乘"},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR = self._old
        self._tmp.cleanup()

    def test_aggregation_and_sort(self):
        data = self.client.get("/api/kanji?lesson=lesson_01").get_json()
        chars = {k["char"]: k for k in data["kanji"]}
        # 人：人/大人/人々 三词（々 不是汉字字符不单列；同一词去重）
        self.assertEqual(len(chars["人"]["words"]), 3)
        self.assertEqual(len(chars["犬"]["words"]), 1)
        self.assertEqual(len(chars["大"]["words"]), 1)
        self.assertEqual(data["kanji"][0]["char"], "人")  # 按词数降序
        # 纯假名词（---）不进聚合
        self.assertTrue(all(k["char"] not in ("の", "か") for k in data["kanji"]))

    def test_returns_lesson_title(self):
        # 标题栏要显示课程名，不能让前端拿内部 id（lesson_01）凑合
        self.assertEqual(
            self.client.get("/api/kanji?lesson=lesson_01").get_json()["title"], "t")
        self.assertEqual(
            self.client.get("/api/kanji?lesson=all").get_json()["title"], "全部课程")


class TestCleanTts(unittest.TestCase):
    def test_xx_phrase(self):
        self.assertEqual(app.clean_tts_text("xxをします"), "します")
        self.assertEqual(app.clean_tts_text("xxができます"), "できます")

    def test_emoji_and_question(self):
        self.assertEqual(app.clean_tts_text("✨犬？"), "犬")

    def test_tts_word_text_override(self):
        # 覆盖表最优先（購入 的假名串 TTS 会拆拗音，用汉字走词典读音）
        self.assertEqual(app.tts_word_text(
            {"kanji": "購入", "hiragana": "こうにゅう"}), "購入")
        # 反向：次 有 つぎ / ジ 两读，TTS 挑了音读念成 じ，重生成多少次都一样，
        # 只能强制回落假名（janome 校验是通过的，所以这里必须靠覆盖表兜）
        self.assertEqual(app.tts_word_text(
            {"kanji": "次", "hiragana": "つぎ"}), "つぎ")
        # 纯假名/片假名词的写法字段是 "---"，无汉字可校验，读音字段原样
        self.assertEqual(app.tts_word_text(
            {"kanji": "---", "hiragana": "アニメ"}), "アニメ")

    def test_tts_word_text_janome_verified(self):
        """汉字形式须经 janome 词典读音校验：一致用汉字（拗音/音调更自然），
        多音字或读音对不上回落读音字段（多音字读错会让听力题没法答）。"""
        try:
            import janome  # noqa: F401
        except ImportError:
            self.skipTest("未安装 janome")
        # 词典读音一致 → 用汉字
        self.assertEqual(app.tts_word_text(
            {"kanji": "学校", "hiragana": "がっこう"}), "学校")
        # 多音字回落：一日 词典读音 イチニチ，与 ついたち 不一致
        self.assertEqual(app.tts_word_text(
            {"kanji": "一日", "hiragana": "ついたち"}), "ついたち")
        # 读音字段与词典对不上（含拼错）一律回落，行为与旧版一致
        self.assertEqual(app.tts_word_text(
            {"kanji": "学校", "hiragana": "がっこお"}), "がっこお")
        self.assertEqual(app.tts_word_text(
            {"kanji": "学校", "hiragana": ""}), "学校")  # 无读音字段：写法兜底


class TestApi(unittest.TestCase):
    """API 层冒烟（只读路径，不落盘）。"""

    @classmethod
    def setUpClass(cls):
        cls.client = app.app.test_client()

    def test_config_has_all_modes(self):
        data = self.client.get("/api/config").get_json()
        mode_ids = {m["id"] for m in data["modes"]}
        self.assertIn("flashcard", mode_ids)
        self.assertIn("audio_to_kana", mode_ids)

    def test_flashcard_start_includes_word(self):
        # lesson_01 全部词条都有例句，可同时验证例句字段
        data = self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": "flashcard",
            "count": 3, "schedule": "random",
        }).get_json()
        self.assertGreater(len(data["questions"]), 0)
        for q in data["questions"]:
            self.assertIn("word", q)  # 自评模式附带单词详情供翻面
            self.assertTrue(q["prompt"] and q["prompt"] != "---")
            self.assertTrue(q["word"]["example_ja"])
            self.assertTrue(q["word"]["example_zh"])

    def test_particle_mode(self):
        st = self.client.post("/api/start", json={
            "lesson": "all", "mode": "particle", "count": 5,
        }).get_json()
        self.assertEqual(st["total"], 5)
        self.assertTrue(all(q["ref"].startswith("particle:") for q in st["questions"]))
        # 用题库第一题验证判分（答对/答错）
        q = app.load_particles()["questions"][0]
        ok = self.client.post("/api/check", json={
            "ref": f"particle:{q['id']}", "mode": "particle", "answer": q["answer"],
        }).get_json()
        self.assertTrue(ok["correct"])
        self.assertTrue(ok["word"]["example_ja"])  # 完整句（填空已补上）
        bad = self.client.post("/api/check", json={
            "ref": f"particle:{q['id']}", "mode": "particle", "answer": "びみょう",
        }).get_json()
        self.assertFalse(bad["correct"])

    def test_exam_mode(self):
        st = self.client.post("/api/start", json={
            "lesson": "all", "mode": "exam", "count": 5,
        }).get_json()
        self.assertEqual(st["total"], 5)
        for q in st["questions"]:
            self.assertTrue(q["ref"].startswith("exam:"))
            self.assertEqual(len(q["options"]), 4)
        # 按官方正解作答应判对；选其它项判错
        q = app.load_exam()["questions"][0]
        expected = q["options"][q["answer"]]
        ok = self.client.post("/api/check", json={
            "ref": f"exam:{q['id']}", "mode": "exam", "answer": expected,
        }).get_json()
        self.assertTrue(ok["correct"])
        wrong_opt = next(o for i, o in enumerate(q["options"]) if i != q["answer"])
        bad = self.client.post("/api/check", json={
            "ref": f"exam:{q['id']}", "mode": "exam", "answer": wrong_opt,
        }).get_json()
        self.assertFalse(bad["correct"])

    def test_exam_filter(self):
        """真题按卷/科目过滤出题。"""
        st = self.client.post("/api/start", json={
            "lesson": "all", "mode": "exam", "count": 300,
            "exam_source": "all", "exam_section": "聴解",
        }).get_json()
        self.assertEqual(st["total"], 56)  # 两卷聴解（含例题）
        self.assertTrue(all(q["audio"] for q in st["questions"]))

        st = self.client.post("/api/start", json={
            "lesson": "all", "mode": "exam", "count": 300,
            "exam_source": "2012", "exam_section": "all",
        }).get_json()
        self.assertEqual(st["total"], 93)  # Vol.1 全卷
        # 题目 id 以 e2012 开头（ref 形如 exam:e2012v001），逐题验证过滤生效
        self.assertTrue(all(q["ref"].startswith("exam:e2012")
                            for q in st["questions"]))

        st = self.client.post("/api/start", json={
            "lesson": "all", "mode": "exam", "count": 300,
            "exam_source": "2018", "exam_section": "文法",
        }).get_json()
        self.assertEqual(st["total"], 26)
        # 对称验证：文法 26 题必须全部来自 2018 卷
        self.assertTrue(all(q["ref"].startswith("exam:e2018")
                            for q in st["questions"]))

    def test_exam_check_returns_script_zh(self):
        """聴解题判分反馈带官方原文与中文翻译。"""
        q = next(x for x in app.load_exam()["questions"] if x.get("script_zh"))
        res = self.client.post("/api/check", json={
            "ref": f"exam:{q['id']}", "mode": "exam",
            "answer": q["options"][q["answer"]],
        }).get_json()
        self.assertEqual(res["word"]["example_ja"], q["script"])
        self.assertEqual(res["word"]["example_zh"], q["script_zh"])

    def test_kanji_mode_cannot_be_faked_with_kana(self):
        """全量不变式：汉字题里用平假名读音作答必须判错。

        写法含片假名的词条（エンジニア、バス停）在折叠字形的比对下会让
        「只打假名没换汉字」蒙混过关，所以汉字题既不选它们、也不折叠比对。
        """
        md = app.get_mode("kana_to_kanji")
        checked = 0
        for ldata in app.load_vocab()["lessons"].values():
            for w in ldata["words"]:
                if not app.mode_accepts(w, md):
                    continue
                checked += 1
                self.assertFalse(
                    app.check_answer(w["hiragana"], w["kanji"], fold_kana=False),
                    f'{w["kanji"]}：只打假名 {w["hiragana"]} 被判对',
                )
        self.assertGreater(checked, 500)

    def test_stats_endpoints(self):
        self.assertEqual(self.client.get("/stats").status_code, 200)
        data = self.client.get("/api/stats").get_json()
        self.assertIn("summary", data)
        self.assertIn("lessons", data)
        self.assertIn("top_wrong", data)
        self.assertIn("top_confusions", data)
        # 模式名取自注册表：统计页不再自己抄一份清单
        names = data["mode_names"]
        for m in app.PRACTICE_MODES:
            self.assertEqual(names[m["id"]], m["name"])


class TestWrongBookGraduation(unittest.TestCase):
    """错题本毕业规则：mastery >= 3 且最后一次错误之后答对过 → 移出。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = app.QUIZZES_DIR
        app.QUIZZES_DIR = Path(self._tmp.name)

    def tearDown(self):
        app.QUIZZES_DIR = self._old_dir
        self._tmp.cleanup()

    @staticmethod
    def _write(name, timestamp, results):
        rec = {"timestamp": timestamp, "mode": "audio_to_kana",
               "lesson": "lesson_01", "results": results}
        (app.QUIZZES_DIR / name).write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

    def _refs(self, vocab):
        return [x["ref"] for x in app.gather_wrong_refs(vocab)]

    def test_graduated_excluded(self):
        vocab = make_vocab()
        vocab["lessons"]["lesson_01"]["words"][0]["mastery"] = 3  # w001
        self._write("a.json", "2026-08-01 10:00:00", [{"ref": "lesson_01:w001", "correct": False}])
        self._write("b.json", "2026-08-02 10:00:00", [{"ref": "lesson_01:w001", "correct": True}])
        self._write("c.json", "2026-08-03 10:00:00", [{"ref": "lesson_01:w002", "correct": False}])
        self.assertEqual(self._refs(vocab), ["lesson_01:w002"])  # w001 已毕业

    def test_low_mastery_stays(self):
        vocab = make_vocab()  # w001 mastery 0：虽然后来答对过，但没到毕业线
        self._write("a.json", "2026-08-01 10:00:00", [{"ref": "lesson_01:w001", "correct": False}])
        self._write("b.json", "2026-08-02 10:00:00", [{"ref": "lesson_01:w001", "correct": True}])
        self.assertEqual(self._refs(vocab), ["lesson_01:w001"])

    def test_wrong_after_correct_stays(self):
        vocab = make_vocab()
        vocab["lessons"]["lesson_01"]["words"][0]["mastery"] = 4
        self._write("a.json", "2026-08-01 10:00:00", [{"ref": "lesson_01:w001", "correct": False}])
        self._write("b.json", "2026-08-02 10:00:00", [{"ref": "lesson_01:w001", "correct": True}])
        self._write("c.json", "2026-08-03 10:00:00", [{"ref": "lesson_01:w001", "correct": False}])
        self.assertEqual(self._refs(vocab), ["lesson_01:w001"])  # 最近一次仍是错的

    def test_no_correct_record_stays(self):
        vocab = make_vocab()
        vocab["lessons"]["lesson_01"]["words"][0]["mastery"] = 5  # mastery 高但错后没答对过
        self._write("a.json", "2026-08-01 10:00:00", [{"ref": "lesson_01:w001", "correct": False}])
        self.assertEqual(self._refs(vocab), ["lesson_01:w001"])

    def test_orphan_ref_excluded(self):
        vocab = make_vocab()
        # 指向不存在词条的旧 ref（下标越界）应被剔除
        self._write("a.json", "2026-08-01 10:00:00", [{"ref": "lesson_01:999", "correct": False}])
        self.assertEqual(self._refs(vocab), [])


class TestMasteryNullRobustness(unittest.TestCase):
    """mastery 为 null（如手改数据）不得崩接口：读取口径统一 `or 0`。

    上轮一致性修复在 _apply_answer/gather_candidates 等处加了 `or 0`，
    但 is_due/api_stats/start_bank 仍有漏网（int(None) 直接 TypeError，
    is_due 一处就打挂 /api/config 与出题）。这里锁住全部读取路径。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "t", "words": [
            {"id": "w001", "kanji": "人", "hiragana": "ひと",
             "meaning": "人", "mastery": None},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        particles = {"questions": [
            {"id": "p001", "sentence": "わたし＿学生です。", "answer": "は",
             "mastery": None, "next_due": tomorrow},  # 未到期 → 走 mastery 排序分支
        ]}
        (tmp / "particles.json").write_text(
            json.dumps(particles, ensure_ascii=False), encoding="utf-8")
        exam = {"meta": {"volumes": [], "sections": []}, "questions": []}
        (tmp / "exam_n5.json").write_text(
            json.dumps(exam, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.PARTICLES_PATH, app.EXAM_PATH,
                     app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.PARTICLES_PATH = tmp / "particles.json"
        app.EXAM_PATH = tmp / "exam_n5.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        self.client = app.app.test_client()

    def tearDown(self):
        (app.VOCAB_PATH, app.PARTICLES_PATH, app.EXAM_PATH,
         app.QUIZZES_DIR, app.BASE_DIR) = self._old
        self._tmp.cleanup()

    def test_is_due_with_null_mastery(self):
        self.assertTrue(app.is_due({"mastery": None, "last_reviewed": "2026-01-01"}))

    def test_config_and_stats_survive_null_mastery(self):
        self.assertEqual(self.client.get("/api/config").status_code, 200)
        self.assertEqual(self.client.get("/api/stats").status_code, 200)

    def test_start_bank_survives_null_mastery(self):
        """start_bank 未到期补位按 mastery 排序：null 不炸（粒子/真题共用）。"""
        st = self.client.post("/api/start", json={
            "mode": "particle", "count": 5}).get_json()
        self.assertEqual(st["total"], 1)

    def test_start_and_finish_survive_null_mastery(self):
        st = self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": "audio_to_kana", "count": 5}).get_json()
        self.assertEqual(st["total"], 1)
        self.client.post("/api/finish", json={
            "mode": "audio_to_kana", "lesson": "lesson_01", "session_id": "n1",
            "results": [{"ref": "lesson_01:w001", "correct": True}]})
        w = app.load_vocab()["lessons"]["lesson_01"]["words"][0]
        self.assertEqual(w["mastery"], 1)


class TestFocusApi(unittest.TestCase):
    """重点词（聚焦练习）：清单维护 + 出题范围 + 题量循环补齐。

    要解决的问题：词库上千词，按到期排练时很多词练一次要等十几天才再见——
    印象还没建立就凉了。圈一小撮反复练，所以「只出清单里的词」必须真的排他，
    且题量大于清单时要循环补齐（同一词出多遍）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {
            "lesson_01": {"title": "t1", "words": [
                {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人",
                 "example_ja": "あの人は学生です。"},
                {"id": "w002", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗",
                 "example_ja": "犬が好きです。"},
                {"id": "w003", "kanji": "猫", "hiragana": "ねこ", "meaning": "猫",
                 "example_ja": "猫が寝ています。"},
            ]},
            "lesson_02": {"title": "t2", "words": [
                {"id": "w001", "kanji": "鳥", "hiragana": "とり", "meaning": "鸟"},
            ]},
        }}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, app.FOCUS_PATH)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        app.FOCUS_PATH = tmp / "data" / "focus_words.json"
        self.client = app.app.test_client()

    def tearDown(self):
        (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, app.FOCUS_PATH) = self._old
        self._tmp.cleanup()

    def _post(self, **payload):
        return self.client.post("/api/focus", json=payload).get_json()

    def _start(self, **payload):
        body = {"lesson": "all", "mode": "audio_to_kana", "count": 10}
        body.update(payload)
        return self.client.post("/api/start", json=body).get_json()

    def test_empty_list_and_pick_candidates(self):
        data = self.client.get("/api/focus").get_json()
        self.assertEqual(data["count"], 0)
        self.assertFalse(data["enabled"])
        picks = self.client.get("/api/focus/pick?lesson=lesson_01").get_json()
        self.assertEqual(picks["total"], 3)
        self.assertTrue(all(not w["selected"] for w in picks["words"]))

    def test_add_persists_and_auto_enables(self):
        data = self._post(action="add", refs=["lesson_01:w001", "lesson_01:w002"])
        self.assertEqual(data["refs"], ["lesson_01:w001", "lesson_01:w002"])
        self.assertTrue(data["enabled"])   # 从空到有自动启用：挑完就能练
        self.assertEqual(app.load_focus()["refs"], data["refs"])  # 落盘可重读

    def test_invalid_refs_rejected(self):
        data = self._post(action="add", refs=["lesson_01:w999", "lesson_01:w001", ""])
        self.assertEqual(data["refs"], ["lesson_01:w001"])

    def test_remove_and_clear(self):
        self._post(action="add", refs=["lesson_01:w001", "lesson_01:w002"])
        data = self._post(action="remove", refs=["lesson_01:w001"])
        self.assertEqual(data["refs"], ["lesson_01:w002"])
        data = self._post(action="clear")
        self.assertEqual(data["refs"], [])
        self.assertFalse(data["enabled"])  # 空清单自动停用，否则「启用但没词可出」

    def test_start_restricted_to_focus_words(self):
        """启用后整本词库都不参与出题——这是这个功能唯一的核心保证。"""
        self._post(action="add", refs=["lesson_01:w001", "lesson_02:w001"])
        st = self._start(schedule="random")
        refs = {q["ref"] for q in st["questions"]}
        self.assertEqual(refs, {"lesson_01:w001", "lesson_02:w001"})

    def test_count_larger_than_list_cycles(self):
        """题量大于清单时循环补齐：同一词反复出现（聚焦练习要的就是重复）。"""
        self._post(action="add", refs=["lesson_01:w001"])
        st = self._start(count=4, schedule="random")
        self.assertEqual(st["total"], 4)
        self.assertEqual({q["ref"] for q in st["questions"]}, {"lesson_01:w001"})

    def test_disabled_or_overridden_falls_back_to_lesson(self):
        self._post(action="add", refs=["lesson_01:w001"])
        self._post(action="enable", enabled=False)
        st = self._start(schedule="random")
        self.assertEqual(st["total"], 4)   # 全词库
        # 前端显式传 focus=false 也能单次退出（配置页选「按课程范围」）
        self._post(action="enable", enabled=True)
        st = self._start(schedule="random", focus=False)
        self.assertEqual(st["total"], 4)

    def test_independent_banks_ignore_focus(self):
        """助词/真题是独立题库，不按词库出题，永远不受清单影响。"""
        self._post(action="add", refs=["lesson_01:w001"])
        self.assertEqual(app.focus_scope(app.get_mode("exam"), {})[0], False)
        self.assertEqual(app.focus_scope(app.get_mode("particle"), {})[0], False)

    def test_mode_without_eligible_word_errors(self):
        """清单里没有能出这个模式题的词：报错而不是静默退回整本词库。"""
        self._post(action="add", refs=["lesson_01:w001"])  # 人：无配图
        st = self._start(mode="image_to_kana", schedule="random")
        self.assertIn("error", st)

    def test_course_and_pair_match_follow_focus(self):
        self._post(action="add", refs=["lesson_01:w001", "lesson_01:w002", "lesson_01:w003"])
        st = self._start(mode="course", count=15)
        refs = {q["ref"] for q in st["questions"]}
        # 清单里的词一个不落（课程模式原本一次只带 5 个新词，重点词要整份练到），
        # 凑不满题量时循环补齐，所以题数按题量来
        self.assertEqual(refs, {"lesson_01:w001", "lesson_01:w002", "lesson_01:w003"})
        self.assertEqual(st["total"], 15)
        # 预习卡不重复：同一张卡看两遍没意义
        self.assertEqual(len([q for q in st["questions"] if q.get("intro")]), 3)
        pair = self._start(mode="pair_match", count=3)
        self.assertTrue(pair["questions"])
        self.assertTrue(all(p["ref"] in
                            {"lesson_01:w001", "lesson_01:w002", "lesson_01:w003"}
                            for q in pair["questions"] for p in q["pairs"]))

    def test_pick_filters_and_search(self):
        vocab = app.load_vocab()
        vocab["lessons"]["lesson_01"]["words"][0]["mastery"] = 4
        app.save_vocab(vocab)
        weak = self.client.get("/api/focus/pick?lesson=lesson_01&filter=weak").get_json()
        self.assertEqual([w["ref"] for w in weak["words"]],
                         ["lesson_01:w002", "lesson_01:w003"])
        hit = self.client.get("/api/focus/pick?lesson=all&q=ねこ").get_json()
        self.assertEqual([w["ref"] for w in hit["words"]], ["lesson_01:w003"])

    def test_max_cap_enforced(self):
        old = app.FOCUS_MAX
        app.FOCUS_MAX = 2
        try:
            data = self._post(action="add", refs=["lesson_01:w001", "lesson_01:w002",
                                                  "lesson_01:w003"])
            self.assertIn("error", data)
            self.assertEqual(app.load_focus()["refs"], [])   # 超限不落盘
        finally:
            app.FOCUS_MAX = old

    def test_repeated_word_keeps_last_attempt(self):
        """同一词一组里出多遍时，掌握度按最后一次作答算（循环补齐的前提）。"""
        self._post(action="add", refs=["lesson_01:w001"])
        self.client.post("/api/finish", json={
            "mode": "audio_to_kana", "lesson": "all", "session_id": "f1",
            "results": [{"ref": "lesson_01:w001", "correct": False},
                        {"ref": "lesson_01:w001", "correct": True}]})
        w = app.load_vocab()["lessons"]["lesson_01"]["words"][0]
        self.assertEqual(w["mastery"], 1)

    def test_config_and_home_report_focus_count(self):
        self._post(action="add", refs=["lesson_01:w001", "lesson_01:w002"])
        cfg = self.client.get("/api/config").get_json()
        self.assertEqual(cfg["focus"], {"count": 2, "enabled": True})
        home = self.client.get("/api/home").get_json()
        course = next(m for m in home["modules"] if m["id"] == "course")
        item = next(i for i in course["items"] if i["id"] == "focus")
        self.assertEqual(item["badge"], "2 词")


class TestFrontendConsistency(unittest.TestCase):
    """前端防呆：跨文件错位（改了调用点忘了同步 id）Python 侧看不见，
    至少保证脚本引用的 DOM id 真的在页面里，以及静态资源带内容版本号。
    """
    STATIC = Path(__file__).resolve().parent / "static"
    TEMPLATES = Path(__file__).resolve().parent / "templates"

    @staticmethod
    def _html_ids(text):
        return set(re.findall(r'id="([\w-]+)"', text))

    @staticmethod
    def _referenced_ids(script):
        used = set(re.findall(r'\$\("([\w-]+)"\)', script))
        used |= set(re.findall(r'getElementById\("([\w-]+)"\)', script))
        return used

    def test_app_js_ids_exist_in_index(self):
        index = (self.TEMPLATES / "index.html").read_text(encoding="utf-8")
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        used = self._referenced_ids(appjs)
        self.assertTrue(used, "没解析到任何 id：选择规则可能需要更新")
        # 脚本里 createElement 后赋的 id、以及 innerHTML 模板里写的 id
        # （如组句排序的 order-line）不在页面模板中，属正常
        dynamic = set(re.findall(r'\.id\s*=\s*"([\w-]+)"', appjs))
        dynamic |= set(re.findall(r'id="([\w-]+)"', appjs))
        missing = used - self._html_ids(index) - dynamic
        self.assertEqual(missing, set(), f"app.js 引用了 index.html 里不存在的元素 id: {missing}")

    def test_screens_registered_and_present(self):
        """首页导航新增了屏幕：SCREENS 清单必须与 index.html 的 section 一一对应。

        漏登记会让 showScreen 切不回来（页面全空白也不报错），
        页面里多出没登记的 section 则会永远显示不出来。
        """
        index = (self.TEMPLATES / "index.html").read_text(encoding="utf-8")
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        m = re.search(r"const SCREENS = \[(.*?)\];", appjs, re.DOTALL)
        self.assertIsNotNone(m, "app.js 里找不到 SCREENS 清单")
        screens = re.findall(r'"([\w-]+)"', m.group(1))
        html_ids = self._html_ids(index)
        for s in ("home-screen", "module-screen", "docs-screen"):
            self.assertIn(s, screens, f"{s} 没登记进 SCREENS")
        for s in screens:
            self.assertIn(s, html_ids, f"SCREENS 里的 {s} 在 index.html 里不存在")
        for s in html_ids:
            if s.endswith("-screen"):
                self.assertIn(s, screens, f"index.html 的 {s} 没登记进 SCREENS")

    def test_home_takes_modules_from_api_not_hardcoded(self):
        """方块与子项只能由后端下发：前端硬编码模式列表会随注册表新增而漂移。"""
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('fetch("/api/home")', appjs)
        self.assertIn('fetch("/api/docs")', appjs)
        index = (self.TEMPLATES / "index.html").read_text(encoding="utf-8")
        # 三个工具入口已迁到「课程与新词」方块，配置页不再平铺这些按钮
        for bid in ("learn-btn", "kanji-btn", "add-word-btn"):
            self.assertNotIn(bid, index)

    def test_focus_scope_wired_both_ends(self):
        """回归：重点词范围必须两端都接上（配置页单选 + /api/start 认账）。

        只在配置页加个单选而后端不读 focus 字段，就会「勾了只练重点词，
        出来的还是整本词库」——这类错位 Python 侧的接口测试抓不到。
        """
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('fetch("/api/focus"', appjs)
        # 默认按配置页的单选；外部入口（图片日语「练本课单词」）可显式 overrides.focus=false
        self.assertIn('focus: overrides.focus !== undefined\n          ? overrides.focus : selectedScope() === "focus"',
                      appjs)
        self.assertIn('if (it.kind === "focus") return openFocus();', appjs)
        index = (self.TEMPLATES / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="scope-row"', index)
        self.assertIn('name="scope"', index)
        self.assertIn('id="focus-screen"', index)

    def test_stats_page_ids_match_inline_script(self):
        stats = (self.TEMPLATES / "stats.html").read_text(encoding="utf-8")
        script = re.search(r"<script>(.*?)</script>", stats, re.DOTALL).group(1)
        missing = self._referenced_ids(script) - self._html_ids(stats)
        self.assertEqual(missing, set(), f"统计页脚本引用了页面里不存在的元素 id: {missing}")

    def test_assets_parse_as_js(self):
        """语法冒烟：脚本至少要不报错地解析通过。

        只挡语法错误；`ref is not defined` 这类未定义标识符要 eslint 或
        浏览器实跑才看得见（本项目没引入 Node 依赖，故靠手动验证）。
        """
        node = shutil.which("node")
        if not node:
            self.skipTest("未安装 node，跳过脚本语法检查")
        for name in ("app.js", "kana.js", "reader.js", "pictures.js"):
            path = self.STATIC / name
            proc = subprocess.run([node, "--check", str(path)],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"{name} 语法错误：{proc.stderr}")

    def test_asset_url_content_hashed(self):
        url = app.asset_url("app.js")
        self.assertRegex(url, r"^/static/app\.js\?v=[0-9a-f]{8}$")
        self.assertEqual(url, app.asset_url("app.js"))  # 未变更时命中缓存
        self.assertEqual(app.asset_url("缺失文件.js"), "/static/缺失文件.js")

    # ---- 回归锁：以下三个 bug 只能靠浏览器实跑发现，用源码断言钉住 ----

    def _js_function(self, name):
        """提取 app.js 顶层函数全文（闭合大括号顶格，参数列表任意）。"""
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        m = re.search(rf"function {name}\([^)]*\) \{{.*?\n\}}", appjs, re.DOTALL)
        self.assertIsNotNone(m, f"app.js 里找不到 function {name}")
        return m.group(0)

    def test_order_ui_reachable_from_standalone_mode(self):
        """回归：独立组句模式必须渲染词块并走统一检查入口。

        曾经 buildOrderUI 只在课程模式被调用，独立模式退化成
        「看中文默写整句」（题干词块被完全忽略）。
        """
        render = self._js_function("renderQuestion")
        # 词块 UI 由后端 ui.order 标记驱动（组句排序与例句听写共用，不按模式 id 分叉）
        self.assertIn("uiOf(q).order", render)
        self.assertIn("buildOrderUI(q)", render)
        check = self._js_function("checkOrderAnswer")
        self.assertIn("courseCheck(", check)        # 课程内：回炉流程
        self.assertIn("submitWithAnswer(", check)   # 独立模式：普通判分
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        # 独立模式 Enter 路径：next-btn 未显示且当前题是词块题时触发检查
        self.assertRegex(appjs, r"else if \(uiOf\(currentQ\(\)\)\.order === true\)")
        # 课程内的组句同理走 ui，不再 hardcode order_cn
        self.assertRegex(appjs, r"if \(uiOf\(course\.q\)\.order === true\) \{")

    def test_enter_to_next_does_not_auto_answer_next_choice(self):
        """回归：四选一（看图选汉字/真题等）作答后按 Enter 只翻页，不能替新题判答案。

        两个 keydown 监听都挂在 document 上：全局 Enter 监听先跑并翻到下一题，
        新题渲染后 examAnswered 复位；若只 preventDefault，后面的「选择题 Enter
        确认」监听会在同一个事件里接着跑，用游标（默认 0）直接替新题选了 A。
        """
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        # 全局 Enter 监听吃掉按键时必须 stopImmediatePropagation
        self.assertRegex(
            appjs,
            r'const consumeEnter = \(e\) => \{\s*e\.preventDefault\(\);\s*'
            r'e\.stopImmediatePropagation\(\);\s*\};')
        # 「下一题」分支必须走 consumeEnter，而不是只 preventDefault
        branch = re.search(
            r'const nextBtn = \$\("next-btn"\);(.*?)\n  \}\);', appjs, re.DOTALL)
        self.assertIsNotNone(branch, "app.js 里找不到 Enter 的 next-btn 分支")
        self.assertIn("consumeEnter(e)", branch.group(1))
        self.assertNotIn("e.preventDefault();", self._strip_comments(branch.group(1)))
        # 选择题监听与课程推进分支保持原样：未作答时才确认游标
        self.assertIn("if (!state.config || state.examAnswered) return;", appjs)
        self.assertIn("submitExam(state.choiceCursor)", appjs)

    def test_choice_cursor_wraps_around(self):
        """回归：选项游标首尾相接——末项再按 ↓ 绕回第一项，首项按 ↑ 绕到末项。

        原先两处都写 `Math.min(n - 1, Math.max(0, ...))` 夹紧，按到末项就没反应，
        得反着按回来。四选一只有 4 个选项，绕一圈比「按到底不动」顺手得多。
        """
        appjs = self._strip_comments(
            (self.STATIC / "app.js").read_text(encoding="utf-8"))
        for var, count in (("state.choiceCursor", "n"),
                           ("learn.quizCursor", "btns.length")):
            self.assertNotIn(f"Math.min({count} - 1, Math.max(0,", appjs,
                             f"{var} 又改回夹紧了——按到末项会没反应")
            self.assertIn(f"{var} = ({var} + step + {count}) % {count};", appjs,
                          f"{var} 的取模写法变了，确认还绕得回来")
        # ↑/↓ 共用一条式子、方向只由 step 决定：两处监听各一份，别只改一处
        self.assertEqual(
            appjs.count('const step = e.key === "ArrowDown" ? 1 : -1;'), 2)

    @staticmethod
    def _strip_comments(js):
        """去掉 // 行注释，避免断言撞上注释里的字面量。"""
        return re.sub(r"^\s*//.*$", "", js, flags=re.MULTILINE)

    def test_play_button_targets_current_question(self):
        """回归：课程模式推进用 course.pos（state.index 恒 0），
        播放按钮/音频查找必须用 currentQ()，否则课程内恒播第 1 题。"""
        appjs = self._strip_comments(
            (self.STATIC / "app.js").read_text(encoding="utf-8"))
        m = re.search(
            r'\$\("play-audio-btn"\)\.addEventListener\("click", \(\) => \{(.*?)\}\);',
            appjs, re.DOTALL)
        self.assertIsNotNone(m, "app.js 里找不到播放按钮的监听器")
        self.assertIn("currentQ()", m.group(1))
        self.assertNotIn("state.questions[state.index]", m.group(1))
        player = self._strip_comments(self._js_function("playQuestionAudio"))
        self.assertIn("currentQ()", player)
        self.assertNotIn("state.questions[state.index]", player)

    def test_prefetch_covers_course_and_audio_prompt(self):
        """回归：预取需覆盖课程队列，并按 ui.audio 判断「进题自动播」的题型
        （听音写假名/听音选意思都在内，不再逐个列模式 id）。"""
        body = self._js_function("prefetchNextAudio")
        self.assertIn("course.queue[course.pos + 1]", body)
        self.assertIn("uiOf(nq).audio", body)

    def test_skip_no_audio_removes_question_without_scoring(self):
        """回归：听力题「不方便听」跳过的语义是整题从本轮剔除——
        不能走 /api/check（否则会进 results、被 finish 计入掌握度/错题本），
        必须 splice 出队列并让本组剩余听力题停止自动播放。"""
        appjs = self._strip_comments(
            (self.STATIC / "app.js").read_text(encoding="utf-8"))
        # 三个听力入口都要渲染「不方便听」行；自动播放须尊重静音
        for fn in ("renderQuestion", "renderCourseQuestion"):
            body = self._js_function(fn)
            self.assertIn("refreshNoAudioRow()", body)
            self.assertIn("!state.noAudio", body)
        skip = self._js_function("skipNoAudio")
        self.assertIn("splice", skip)
        self.assertIn("state.noAudio = true", skip)
        self.assertNotIn('"/api/check"', skip)
        self.assertIn("courseQueueTail()", skip)   # 课程轮次被跳空走统一收尾
        self.assertIn("endSessionOrBackHome()", skip)  # 独立模式跳空后同样收尾
        tail = self._js_function("courseQueueTail")
        self.assertIn("endSessionOrBackHome()", tail)
        home = self._js_function("endSessionOrBackHome")
        # 一题未答时静默退回（导航改成三级后，落点是来时的模块页而非配置页）
        self.assertIn("backToModule()", home)
        self.assertIn("finishSession()", home)     # 有已答结果则正常交卷
        # 本组静音后可一键恢复（场合变化时），恢复即对当前题重播
        row = self._js_function("refreshNoAudioRow")
        self.assertIn('"restore-audio-btn"', row)
        self.assertIn('"no-audio-row"', row)
        restore = self._js_function("restoreAudio")
        self.assertIn("refreshNoAudioRow()", restore)
        self.assertIn("playQuestionAudio(q.ref)", restore)

    def test_skip_all_listening_questions_toggle(self):
        """「跳过听力题」：一次剔掉本轮全部听力题，且开关记在本机（上课反复用）。

        与逐题「不方便听」同语义——不判分、不改掌握度；区别只是省得一道道点。
        """
        appjs = self._strip_comments(
            (self.STATIC / "app.js").read_text(encoding="utf-8"))
        drop = self._js_function("dropListeningQuestions")
        self.assertIn("isListeningQuestion", drop)
        self.assertIn("i < state.index", drop)      # 已答过的题不动
        self.assertIn("course.queue", drop)         # 课程模式走自己的队列
        self.assertNotIn('"/api/check"', drop)      # 剔除 = 不判分
        # 开练时就生效，而不是等第一道听力题出现
        start = self._js_function("startSession")
        self.assertIn("skipAudioPref()", start)
        self.assertIn("dropListeningQuestions()", start)
        self.assertIn("endSessionOrBackHome()", start)   # 剔空了要收尾，不能卡在空屏
        # 开关记在本机（存不了也不能崩：退化成当次手动开关）
        self.assertIn("localStorage.getItem", self._js_function("skipAudioPref"))
        self.assertIn("localStorage.setItem", self._js_function("setSkipAudioPref"))
        # 按钮真的在页面上、也真的接上了
        index = (self.TEMPLATES / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="mute-btn"', index)
        self.assertIn('$("mute-btn").addEventListener("click", toggleSkipAudio)', appjs)

    def test_pair_match_hides_play_button(self):
        """回归：配对题同屏 5 个词，没有「当前词」可言——播放按钮只能固定读
        本轮第一个词，还会让人以为读的是刚选中的那个。点词瓦片已自动发音。"""
        body = self._strip_comments(self._js_function("canPlayQuestionAudio"))
        self.assertIn('m === "pair_match"', body)
        # 必须在 exam/课程分支之前返回，否则会被后面的分支盖掉
        self.assertLess(body.index('m === "pair_match"'), body.index('m === "exam"'))

    def test_count_field_uses_rounds_for_pair_match(self):
        """回归：配对按「轮数」出题（每轮 5 对，后端上限 8），沿用「题目数量」
        (1-100) 会让人填 20 却只出 8 题。切换模式时必须同步刷新这个框。"""
        appjs = self._strip_comments(
            (self.STATIC / "app.js").read_text(encoding="utf-8"))
        self.assertRegex(appjs,
                         r'pair_match: \{ label: "轮数（每轮 5 对）", min: 2, max: 8 \}')
        self.assertRegex(appjs,
                         r'modeDiv\.onchange = \(\) => \{\s*'
                         r'updateExamFilterVisibility\(\);\s*updateCountField\(\);')
        body = self._js_function("updateCountField")
        self.assertIn('$("count-label")', body)
        self.assertIn("genericCount", body)  # 切走配对时恢复原本的题目数量

    def test_kanji_card_resumes_browsing_position(self):
        """回归：全部课程 500+ 字，不记位置等于每次打开都从头翻。"""
        start = self._js_function("startKanji")
        self.assertIn("kanjiSavedPos(data.lesson)", start)
        self.assertIn("kstate.title = data.title", start)
        render = self._js_function("renderKanjiCard")
        self.assertIn("localStorage.setItem(KANJI_POS_PREFIX", render)
        self.assertIn('$("k-lesson").textContent = kstate.title', render)

    def test_romaji_to_kana_behavior(self):
        """kana.js 行为冒烟：n+y、nn、促音、n'、结尾 n 收敛、外来语特殊音等关键规则。"""
        node = shutil.which("node")
        if not node:
            self.skipTest("未安装 node，跳过 kana.js 行为检查")
        cases = [
            ("norikae", "のりかえ"),
            ("genki", "げんき"),      # n+辅音 → ん
            ("konya", "こにゃ"),      # ny 是拗音，与主流 IME 一致（んや 打 kon'ya）
            ("konna", "こんな"),      # nn+元音：第二个 n 与元音成音节
            ("sann", "さん"),         # 结尾 nn
            ("kon'ya", "こんや"),     # n' 显式 ん
            ("gakkou", "がっこう"),   # 双辅音促音
            ("san", "さん"),          # 提交时结尾悬空 n 收敛
            ("tanoshii", "たのしい"),
            ("chuugoku", "ちゅうごく"),
            # 外来语特殊音（与主流 IME 扩展罗马字一致）：ti/di 仍是 ち/ぢ，
            # ティ/ディ 要打 thi/dhi（ミーティング → mithingu，见 w402）
            ("mithingu", "みてぃんぐ"),
            ("mi-thingu", "みーてぃんぐ"),
            ("komedhi", "こめでぃ"),
            ("fantaji", "ふぁんたじ"),
            ("kafe", "かふぇ"),
            ("fikushon", "ふぃくしょん"),
            ("chekku", "ちぇっく"),
        ]
        script = (self.STATIC / "kana.js").read_text(encoding="utf-8") + """
const cases = %s;
for (const [input, expected] of cases) {
  const got = romajiToKana(input, true);
  if (got !== expected) {
    console.error(`${input} -> ${got}（期望 ${expected}）`);
    process.exit(1);
  }
}
console.log("ok");
""" % json.dumps(cases, ensure_ascii=False)
        proc = subprocess.run([node, "-e", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         f"kana.js 行为不符：{proc.stdout} {proc.stderr}")

    def test_order_drop_index_geometry(self):
        """组句拖放的落点几何（纯函数，用 node 实跑）：指针落在某个词块左半边
        就插到它前面，落在行尾之外就补到行尾；折行的块先按 y 归行再判断。"""
        node = shutil.which("node")
        if not node:
            self.skipTest("未安装 node，跳过落点几何检查")
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        m = re.search(r"function orderDropIndex\(.*?\n\}", appjs, re.DOTALL)
        self.assertIsNotNone(m, "app.js 里找不到 orderDropIndex")
        # 每个块 [left, top, width, height]：一行三个 80×40 的块 A|B|C
        row = [[0, 0, 80, 40], [80, 0, 80, 40], [160, 0, 80, 40]]
        # 折行：A、B 在第一行，C 在第二行（top=50）
        wrapped = [[0, 0, 80, 40], [80, 0, 80, 40], [0, 50, 80, 40]]
        cases = [
            ([], 0, 0, 0),           # 空行：放第一个位置
            (row, 10, 20, 0),        # A 左半 → A 前
            (row, 70, 20, 1),        # A 右半 → B 前
            (row, 199, 20, 2),       # C 左半 → C 前
            (row, 230, 20, 3),       # 行尾之外 → 末尾
            ([[0, 0, 80, 40]], 70, 20, 1),   # 单个块：右半 → 其后
            (wrapped, 230, 10, 2),   # 第一行行尾 → C 之前
            (wrapped, 10, 60, 2),    # 第二行左半 → C 之前
            (wrapped, 70, 60, 3),    # 第二行右半 → C 之后
        ]
        script = m.group(0) + """
const cases = %s;
for (const [rects, x, y, want] of cases) {
  const rs = rects.map(([left, top, width, height]) => ({left, top, width, height}));
  const got = orderDropIndex(rs, x, y);
  if (got !== want) {
    console.error(`x=${x} y=${y} -> ${got}（期望 ${want}）`);
    process.exit(1);
  }
}
console.log("ok");
""" % json.dumps(cases)
        proc = subprocess.run([node, "-e", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         f"落点几何不符：{proc.stdout} {proc.stderr}")

    def test_order_chip_reorderable_by_drag(self):
        """回归：组句块原先只能在「待选区 ↔ 句子」之间 toggle，句子里想换语序
        只能把词全点回去重拼；更糟的是拖动句子里的块会被 toggle 踢回待选区。
        现在拖拽按落点插入（句子内拖 = 换语序），点击保留 toggle 作为快捷方式。"""
        build = self._js_function("buildOrderUI")
        self.assertIn("orderInsertAt(zone, chip, x, y)", build)
        self.assertIn("e.clientX", build)
        self.assertIn("e.clientY", build)
        self.assertIn("toggleChip(chip)", build)              # 点击仍是 toggle
        self.assertNotIn("moveChip(course.dragChip)", build)  # 拖拽不再走 toggle
        insert = self._js_function("orderInsertAt")
        self.assertIn("c !== chip", insert)                    # 落点排除被拖块本身
        self.assertIn("nextElementSibling === ref", insert)    # 位置未变不重排（防抖动）



class TestAddWordApi(unittest.TestCase):
    """前端新增单词：写入/校验/判重/自动建课，临时目录落盘不碰真实数据。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "t", "words": [
            {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人"},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR = self._old
        self._tmp.cleanup()

    def _add(self, **kw):
        payload = {"lesson": "lesson_01", "kanji": "犬",
                   "hiragana": "いぬ", "meaning": "狗"}
        payload.update(kw)
        return self.client.post("/api/words/add", json=payload)

    def test_add_to_existing_lesson(self):
        res = self._add()
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["ref"], "lesson_01:w002")
        self.assertEqual(data["word_count"], 2)
        words = app.load_vocab()["lessons"]["lesson_01"]["words"]
        w = words[-1]
        self.assertEqual(w["id"], "w002")  # id 自动顺延
        self.assertEqual(w["mastery"], 0)
        self.assertIsNone(w["last_reviewed"])
        self.assertIsNone(w["next_due"])
        self.assertEqual(w["romaji"], "")
        # 没填例句：不产生 example 键（与 CSV 导入口径一致）
        self.assertNotIn("example_ja", w)
        # 原有词条不受影响
        self.assertEqual(words[0]["hiragana"], "ひと")

    def test_blank_kanji_becomes_dashes(self):
        res = self._add(kanji="   ")
        self.assertEqual(res.status_code, 200)
        w = app.load_vocab()["lessons"]["lesson_01"]["words"][-1]
        self.assertEqual(w["kanji"], "---")

    def test_example_fields_when_provided(self):
        res = self._add(example_ja="犬が好きです。", example_zh="我喜欢狗。")
        self.assertEqual(res.status_code, 200)
        w = app.load_vocab()["lessons"]["lesson_01"]["words"][-1]
        self.assertEqual(w["example_ja"], "犬が好きです。")
        self.assertEqual(w["example_zh"], "我喜欢狗。")

    def test_tip_only_when_provided(self):
        # 填了讲解才写 tip 键，不填保持最小字段集
        self._add(hiragana="ねこ", meaning="猫")
        w0 = app.load_vocab()["lessons"]["lesson_01"]["words"][-1]
        self.assertNotIn("tip", w0)
        self._add(hiragana="いぬ", meaning="狗", tip="书面语，口语用買う")
        w1 = app.load_vocab()["lessons"]["lesson_01"]["words"][-1]
        self.assertEqual(w1["tip"], "书面语，口语用買う")

    def test_missing_required_fields_400(self):
        self.assertEqual(self._add(hiragana="").status_code, 400)
        self.assertEqual(self._add(meaning="").status_code, 400)
        # 新建课程却不给标题
        res = self.client.post("/api/words/add", json={
            "create_new": True, "new_lesson_title": "",
            "hiragana": "ねこ", "meaning": "猫"})
        self.assertEqual(res.status_code, 400)

    def test_duplicate_rejected(self):
        self.assertEqual(self._add().status_code, 200)
        res = self._add()  # 同（写法, 读音, 释义）三元组再交一次
        self.assertEqual(res.status_code, 409)
        self.assertTrue(res.get_json()["duplicate"])
        # 拦截后不增加词条
        words = app.load_vocab()["lessons"]["lesson_01"]["words"]
        self.assertEqual(len(words), 2)

    def test_halfwidth_katakana_normalized(self):
        # 半角片假名 + 组合浊点经 NFKC 归一为全角（与判分链路同一形态）
        res = self._add(kanji="ｹﾞｰﾑ", hiragana="げーむ", meaning="游戏")
        self.assertEqual(res.status_code, 200)
        w = app.load_vocab()["lessons"]["lesson_01"]["words"][-1]
        self.assertEqual(w["kanji"], "ゲーム")

    def test_default_mywords_auto_created(self):
        # 不传 lesson：首次自动创建「我的生词本」
        res = self.client.post("/api/words/add", json={
            "hiragana": "ねこ", "meaning": "猫"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["lesson_id"], app.DEFAULT_USER_LESSON)
        self.assertEqual(data["lesson_title"], app.DEFAULT_USER_LESSON_TITLE)
        vocab = app.load_vocab()
        self.assertIn(app.DEFAULT_USER_LESSON, vocab["lessons"])

    def test_wrong_book_falls_back_to_mywords(self):
        # 错题本是虚拟课程，绝不允许写入，回落到生词本
        res = self._add(lesson=app.WRONG_LESSON_ID, hiragana="ねこ", meaning="猫")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["lesson_id"], app.DEFAULT_USER_LESSON)

    def test_unknown_real_lesson_400(self):
        res = self._add(lesson="lesson_nope")
        self.assertEqual(res.status_code, 400)

    def test_create_new_lesson_numbering_and_merge(self):
        def create(title, hira, mean):
            return self.client.post("/api/words/add", json={
                "create_new": True, "new_lesson_title": title,
                "hiragana": hira, "meaning": mean}).get_json()
        self.assertEqual(create("Unit5", "ねこ", "猫")["lesson_id"], "lesson_user_01")
        self.assertEqual(create("Unit6", "いぬ", "狗")["lesson_id"], "lesson_user_02")
        # 同名课程并入而非新建
        merged = create("Unit5", "さかな", "鱼")
        self.assertEqual(merged["lesson_id"], "lesson_user_01")
        self.assertEqual(merged["word_count"], 2)
        lessons = app.load_vocab()["lessons"]
        self.assertEqual(len(lessons["lesson_user_01"]["words"]), 2)
        self.assertEqual(len(lessons["lesson_user_02"]["words"]), 1)

    def test_persisted_reloadable_and_file_format(self):
        self._add(notes="来自前端新增")
        # 重新从磁盘读出：原子写入的文件必须是合法 JSON
        vocab = app.load_vocab()
        w = vocab["lessons"]["lesson_01"]["words"][-1]
        self.assertEqual(w["notes"], "来自前端新增")
        # 落盘格式保持「单词一行一个」（_format_vocab 的紧凑格式，便于 diff）
        text = app.VOCAB_PATH.read_text(encoding="utf-8")
        self.assertIn('"id": "w002"', text)

    def test_practice_feedback_carries_tip(self):
        """练习反馈两条数据链路都要带讲解：闪卡取 /api/start，主练习取 /api/check。"""
        self._add(hiragana="いぬ", meaning="狗", tip="书面语辨析")
        # 闪卡（self_assessed）：出题响应的 word 带 tip，供翻面反馈卡渲染
        st = self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": "flashcard", "count": 10,
            "schedule": "random", "refs": ["lesson_01:w002"]}).get_json()
        self.assertEqual(st["questions"][0]["word"]["tip"], "书面语辨析")
        # 主练习：判分响应的 word 带 tip，供 showFeedback 渲染
        ck = self.client.post("/api/check", json={
            "ref": "lesson_01:w002", "mode": "audio_to_kana", "answer": "いぬ"}).get_json()
        self.assertTrue(ck["correct"])
        self.assertEqual(ck["word"]["tip"], "书面语辨析")


class TestRenameLessonApi(unittest.TestCase):
    """重命名课程：只改 lessons.<id>.title，单词 / 进度 / 引用一律不动。"""

    STATIC = Path(__file__).resolve().parent / "static"
    TEMPLATES = Path(__file__).resolve().parent / "templates"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {
            "lesson_01": {"title": "第1课", "words": [
                {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人",
                 "mastery": 3, "next_due": "2026-09-09"},
            ]},
            "lesson_02": {"title": "第2课", "words": []},
        }}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR = self._old
        self._tmp.cleanup()

    def _rename(self, **kw):
        payload = {"lesson_id": "lesson_01", "title": "X"}
        payload.update(kw)
        return self.client.post("/api/lesson/rename", json=payload)

    def test_renames_title_only(self):
        res = self._rename()
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["title"], "X")
        self.assertEqual(data["old_title"], "第1课")
        vocab = app.load_vocab()
        self.assertEqual(vocab["lessons"]["lesson_01"]["title"], "X")
        # 词条、id、掌握度、复习调度一个字段都不能动（都不依赖课程标题）
        w = vocab["lessons"]["lesson_01"]["words"][0]
        self.assertEqual(w["id"], "w001")
        self.assertEqual(w["mastery"], 3)
        self.assertEqual(w["next_due"], "2026-09-09")
        # 其他课程不受影响
        self.assertEqual(vocab["lessons"]["lesson_02"]["title"], "第2课")

    def test_config_reflects_new_title(self):
        self._rename()
        lessons = {l["id"]: l
                   for l in self.client.get("/api/config").get_json()["lessons"]}
        self.assertEqual(lessons["lesson_01"]["title"], "X")

    def test_persisted_and_reloadable(self):
        self._rename(title="社交平台X")
        # 原子写出的文件仍是合法 JSON，且保留「单词一行一个」的紧凑格式
        text = app.VOCAB_PATH.read_text(encoding="utf-8")
        self.assertIn('"title": "社交平台X"', text)
        self.assertEqual(app.load_vocab()["lessons"]["lesson_01"]["title"], "社交平台X")

    def test_title_normalized_like_word_fields(self):
        # 与 /api/words/add 的新课程名同一口径：半角片假名经 NFKC 收敛为全角，
        # 否则「同名课程」判定会漏（两个写法不同、显示却一样的课名）
        self._rename(title="  ﾂｲｯﾀｰ  ")
        self.assertEqual(app.load_vocab()["lessons"]["lesson_01"]["title"], "ツイッター")

    def test_blank_title_400(self):
        self.assertEqual(self._rename(title="").status_code, 400)
        self.assertEqual(self._rename(title="   ").status_code, 400)
        self.assertEqual(app.load_vocab()["lessons"]["lesson_01"]["title"], "第1课")

    def test_unknown_lesson_404(self):
        self.assertEqual(self._rename(lesson_id="lesson_nope").status_code, 404)

    def test_virtual_lessons_rejected(self):
        # 「全部课程」是筛选值、错题本是虚拟课，二者都没有落盘的标题可改
        self.assertEqual(self._rename(lesson_id="all").status_code, 400)
        self.assertEqual(self._rename(lesson_id=app.WRONG_LESSON_ID).status_code, 400)

    def test_duplicate_title_409(self):
        # 下拉里两门同名课分不清是谁，与新增单词的同名策略一致：拒绝
        res = self._rename(lesson_id="lesson_01", title="第2课")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(app.load_vocab()["lessons"]["lesson_01"]["title"], "第1课")

    def test_home_nav_carries_entry(self):
        """入口只能由后端下发：前端不写死子项列表，漏登记 = 静默没有入口。"""
        mods = self.client.get("/api/home").get_json()["modules"]
        items = [it for m in mods if m["id"] == "course" for it in m["items"]]
        entry = next((it for it in items if it["id"] == "rename_lesson"), None)
        self.assertIsNotNone(entry, "首页「课程与新词」里没有重命名课程入口")
        self.assertEqual(entry["kind"], "rename_lesson")

    def test_frontend_wiring(self):
        """Python 侧看不见的接线：弹窗 id、接口调用、配置页入口按钮。"""
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        index = (self.TEMPLATES / "index.html").read_text(encoding="utf-8")
        self.assertIn('fetch("/api/lesson/rename"', appjs)
        self.assertIn('it.kind === "rename_lesson"', appjs)
        for dom_id in ("rename-modal", "rn-lesson", "rn-title", "rn-save"):
            self.assertIn(f'id="{dom_id}"', index)
        self.assertIn('id="rename-lesson-btn"', index)


class TestDrillMatrix(unittest.TestCase):
    """题型矩阵：看xx写xx / 看xx选xx 由「题干 × 作答」派生，规则只此一份。"""

    def test_matrix_modes_declare_cue_and_task(self):
        # 矩阵里的题型必须声明 (cue, task)，且两者都在定义表内
        matrix_ids = {"kanji_to_kana", "kana_to_kanji", "audio_to_kana", "form_to_audio",
                      "kana_to_cn", "audio_to_cn", "image_to_kanji", "image_to_word"}
        for m in app.PRACTICE_MODES:
            if m["id"] not in matrix_ids:
                continue
            self.assertIn(m["cue"], app.CUE_DEFS, m["id"])
            self.assertIn(m["task"], app.TASK_DEFS, m["id"])

    def test_ui_matches_derived_flags(self):
        for m in app.PRACTICE_MODES:
            ui = app.mode_ui(m)
            self.assertEqual(ui["choice"], bool(m.get("mcq_mode")), m["id"])
            self.assertEqual(ui["image"], bool(m.get("image_mode")), m["id"])
            self.assertEqual(ui["audio"], bool(m.get("audio_mode")), m["id"])
            self.assertEqual(ui["order"], bool(m.get("order_mode")), m["id"])
            self.assertEqual(ui["spoiler"], bool(m.get("spoiler")), m["id"])

    def test_no_image_to_write_kana(self):
        """配图题干不再配「写假名」：一张图对应哪个读音不唯一（已停用）。"""
        for m in app.PRACTICE_MODES:
            self.assertFalse(
                "image" in m.get("cue", "") and m.get("task") == "write_kana",
                f"{m['id']} 仍是看图写假名")
        self.assertIsNone(app.get_mode("image_to_kana"))

    def test_no_cn_prompt_production_task(self):
        """中文不再作题干：释义在词库里不唯一，产出题无法判分（看中文写假名已停用）。"""
        for m in app.PRACTICE_MODES:
            self.assertNotEqual(m.get("cue"), "cn", f"{m['id']} 仍以中文作题干")
        self.assertIsNone(app.get_mode("cn_to_kana"))

    def test_retired_mode_still_named_in_stats(self):
        """旧记录里的 mode id 仍要有中文名，统计页不显示裸 id。"""
        names = self._stats_mode_names()
        for mode_id, label in app.RETIRED_MODES.items():
            self.assertEqual(names[mode_id], label, mode_id)

    def test_spoiler_only_when_prompt_hides_reading(self):
        """题干已把读音交出来（假名/发音）的题型，播放发音不算剧透。"""
        for m in app.PRACTICE_MODES:
            if "cue" not in m:
                continue
            gives_reading = app.CUE_DEFS[m["cue"]]["gives_reading"]
            if gives_reading:
                self.assertFalse(m.get("spoiler"), m["id"])
            elif m["task"] in ("write_kana", "choose_form", "choose_audio"):
                self.assertTrue(m.get("spoiler"), m["id"])

    def test_image_modes_are_choice_only(self):
        """配图只能出再认题（选），不能出产出题（写）。"""
        for m in app.PRACTICE_MODES:
            if "image" in m.get("cue", ""):
                self.assertTrue(m["task"].startswith("choose"), m["id"])

    def test_default_mode_ids_still_valid(self):
        """回归锁：停用某个模式后，各处兜底的默认 mode id 最容易漏改。"""
        practice = Path(app.BASE_DIR) / "practice"
        apppy = (practice / "app.py").read_text(encoding="utf-8")
        for hit in re.finditer(r'get\("mode", "([a-z_0-9]+)"\)', apppy):
            self.assertIsNotNone(app.get_mode(hit.group(1)), hit.group(1))
        appjs = (practice / "static" / "app.js").read_text(encoding="utf-8")
        for hit in re.finditer(r'mode: "([a-z_0-9]+)"', appjs):
            self.assertIsNotNone(app.get_mode(hit.group(1)), hit.group(1))
        # 前端两处「没选中就兜底」的默认值（课程下拉的 lesson 不算）
        sel = re.search(r"function selectedMode\(\) \{.*?\n\}", appjs, re.DOTALL)
        self.assertIsNotNone(sel, "app.js 里找不到 selectedMode")
        sel_default = re.search(r': "([a-z_0-9]+)"', sel.group(0)).group(1)
        self.assertIsNotNone(app.get_mode(sel_default), sel_default)
        start_default = re.search(
            r'overrides\.mode \|\| \(modeEl \? modeEl\.value : "([a-z_0-9]+)"\)', appjs)
        self.assertIsNotNone(start_default, "app.js 里找不到 startSession 的默认题型")
        self.assertIsNotNone(app.get_mode(start_default.group(1)), start_default.group(1))

    def _stats_mode_names(self):
        with app.app.test_client() as c:
            return c.get("/api/stats").get_json()["mode_names"]


class TestImageModes(unittest.TestCase):
    """配图模式：无图的词不出题；图片 URL 随题目/反馈下发；首页导航自动收录。"""

    @classmethod
    def setUpClass(cls):
        cls.client = app.app.test_client()

    def test_unpictured_word_never_accepted(self):
        md = app.get_mode("image_to_kanji")
        vocab = app.load_vocab()
        lid, w = next((lid, w) for lid, l in vocab["lessons"].items()
                      for w in l["words"] if not app.vocab_image_url(lid, w["id"]))
        self.assertFalse(app.mode_accepts(w, md, lid))
        self.assertFalse(app.mode_accepts(w, app.get_mode("image_to_word"), lid))
        # 漏传 lesson_id 时宁可不出题（防御：不能把无图判断放行成「有图」）
        self.assertFalse(app.mode_accepts(w, md))

    def test_audio_options_need_speakable_reading(self):
        """选项是一段发音：念不出来的词既不能出题，也不能当干扰项。"""
        self.assertFalse(app.speakable_reading({"hiragana": ""}))
        self.assertFalse(app.speakable_reading({"hiragana": "xxを食べます"}))
        self.assertTrue(app.speakable_reading({"hiragana": "みず"}))
        md = app.get_mode("image_to_word")
        self.assertFalse(app.mode_accepts({"id": "w1", "hiragana": "xxです"}, md, "lesson_01"))
        # 干扰项池过滤：句型课的残句读音（「xxを飲みます」→ をのみます）不会混进选项
        vocab = app.load_vocab()
        words = app.audio_option_words(vocab)
        self.assertTrue(words)
        pool = app._mcq_pool(words, md)
        self.assertTrue(pool)
        texts = {t for t, _ in pool}
        self.assertNotIn("xx", " ".join(texts).lower())
        self.assertTrue(texts <= {w["hiragana"].strip() for w in words})

    def test_home_nav_lists_image_modes(self):
        payload = app.build_home_payload()
        drill = next(m for m in payload["modules"] if m["id"] == "drill")
        ids = {it["id"] for it in drill["items"]}
        self.assertIn("image_to_kanji", ids)
        self.assertIn("image_to_word", ids)
        self.assertNotIn("image_to_kana", ids)  # 看图写假名已停用


class TestCourseImageRotation(unittest.TestCase):
    """课程模式接配图：新词轮换里「有图出看图选词、无图自动降级」，预习卡带图。"""

    MIN_PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {
            "lesson_01": {"title": "t", "words": [
                # 全部 m0：都进新词名额（COURSE_NEW_CAP=5）
                {"id": "w001", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗"},
                {"id": "w002", "kanji": "猫", "hiragana": "ねこ", "meaning": "猫"},
                {"id": "w003", "kanji": "鳥", "hiragana": "とり", "meaning": "鸟"},
                {"id": "w004", "kanji": "魚", "hiragana": "さかな", "meaning": "鱼"},
                # 无图词：轮换到配图位时必须降级成其他再认题型
                {"id": "w005", "kanji": "家", "hiragana": "いえ", "meaning": "家"},
            ]},
        }}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        img_dir = tmp / "images" / "vocab"
        img_dir.mkdir(parents=True)
        # 配图判断只看文件存在性，1x1 PNG 足够
        for stem in ("lesson_01_w001", "lesson_01_w002", "lesson_01_w003", "lesson_01_w004"):
            (img_dir / f"{stem}.png").write_bytes(self.MIN_PNG)
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, app.VOCAB_IMAGE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        app.VOCAB_IMAGE_DIR = img_dir
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, app.VOCAB_IMAGE_DIR = self._old
        self._tmp.cleanup()

    def test_rotation_pref_and_intro_image(self):
        self.assertIn("image_to_kanji", app.COURSE_RECOGNITION_ROTATION)
        self.assertIn("image_to_word", app.COURSE_RECOGNITION_ROTATION)
        st = self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": "course", "count": 15}).get_json()
        self.assertEqual(st["total"], 5)
        by_ref = {q["ref"]: q for q in st["questions"]}
        # 轮换序（COURSE_RECOGNITION_ROTATION）：i=0 看词选意思、i=1 看图选汉字、
        # i=2 看图选发音、i=3 听音选意思、i=4 例句填空（无例句降级看词）
        self.assertEqual(by_ref["lesson_01:w001"]["mode"], "kana_to_cn")
        self.assertEqual(by_ref["lesson_01:w002"]["mode"], "image_to_kanji")
        self.assertEqual(by_ref["lesson_01:w003"]["mode"], "image_to_word")
        self.assertEqual(by_ref["lesson_01:w004"]["mode"], "audio_to_cn")
        self.assertEqual(by_ref["lesson_01:w005"]["mode"], "audio_to_cn")
        # 无图无例句 → 降到看词选意思；但「家」的释义就是「家」，那是送分题，
        # 再往下降一级到听音选意思（题干是发音，不泄露答案）
        # 看图选汉字：带图 + 词形选项；看图选发音：带图 + 四段发音（选项音频随题下发）
        q2 = by_ref["lesson_01:w002"]
        self.assertTrue(q2["image"])
        self.assertEqual(len(q2["options"]), 4)
        self.assertIn("猫", q2["options"])
        self.assertEqual(q2["mode_name"], "看图选汉字")  # 题面徽标与独立模式同名
        q3 = by_ref["lesson_01:w003"]
        self.assertTrue(q3["ui"]["audio_options"])
        self.assertIn("とり", q3["options"])
        self.assertEqual(len(q3["option_audio"]), 4)
        self.assertEqual(q3["option_audio"][q3["options"].index("とり")],
                         f"/api/tts?ref={quote(q3['ref'], safe=':')}")
        # 预习卡图与有无配图一一对应
        self.assertEqual(by_ref["lesson_01:w001"]["intro"]["image"],
                         "/static/images/vocab/lesson_01_w001.png")
        self.assertEqual(by_ref["lesson_01:w005"]["intro"]["image"], "")
        self.assertEqual(by_ref["lesson_01:w001"]["intro"]["image"],
                         "/static/images/vocab/lesson_01_w001.png")
        self.assertEqual(by_ref["lesson_01:w005"]["intro"]["image"], "")


class TestImagePrompt(unittest.TestCase):
    """配图 prompt：颜色类不能靠「颜色汉字 in 词形」做子串匹配。

    「面白い」被标成 color 类后含「白」→ 生成 a solid white color square，
    实测那张图主色占比 98.7%、均值 RGB(253,253,253)，是一张纯白色块；
    「金曜日」（含「金」）、「灰皿」（含「灰」）同样中招。
    """

    CSV = Path(__file__).resolve().parent.parent / "data" / "vocab_image_list_filtered.csv"

    def test_true_color_words_recognized(self):
        for subject, meaning in [("赤い", "红色的"), ("白い", "白色的"), ("黄色い", "yellow"),
                                 ("黄色", "yellow"), ("みどり", "绿色"), ("青", "blue"),
                                 ("黒い", "black; dark"), ("白", "white"), ("緑", "green")]:
            self.assertTrue(gen.color_word(subject, meaning), f"{subject} 应是颜色词")

    def test_words_containing_color_kanji_are_not_colors(self):
        """含颜色汉字但另有所指的词，不能被判成颜色词。"""
        for subject, meaning in [("面白い", "interesting, amusing"),
                                 ("面白い", "有趣的、好玩的"),
                                 ("金曜日", "Friday"), ("灰皿", "ashtray"),
                                 ("お金", "money")]:
            self.assertFalse(gen.color_word(subject, meaning),
                             f"{subject}({meaning}) 不是颜色词，却被判成颜色")

    def test_no_solid_color_square_for_non_color_words(self):
        """回归：这三个词曾被生成成纯色方块（图与词义毫无关系）。"""
        for word in (
            {"kanji": "面白い", "hiragana": "おもしろい",
             "meaning": "interesting, amusing", "category": "color"},
            {"kanji": "金曜日", "hiragana": "きんようび",
             "meaning": "Friday", "category": "color"},
            {"kanji": "灰皿", "hiragana": "はいざら",
             "meaning": "ashtray", "category": "object"},
        ):
            prompt = gen.build_prompt(word)
            self.assertNotIn("color square", prompt,
                             f"{word['kanji']} 仍在生成纯色块：{prompt}")

    def test_ashtray_uses_special_prompt(self):
        """灰皿是具体物件：固定英文主体，免得被理解成「灰色的盘子」。"""
        prompt = gen.build_prompt({"kanji": "灰皿", "hiragana": "はいざら",
                                   "meaning": "ashtray", "category": "object"})
        self.assertIn("ashtray", prompt)

    def test_candidate_csv_has_no_mislabeled_color_rows(self):
        """数据层防回归：候选表里标成 color 的词必须都是真颜色词。"""
        if not self.CSV.exists():
            self.skipTest("候选词表不存在")
        with open(self.CSV, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertTrue(rows, "候选词表是空的")
        bad = []
        for r in rows:
            if (r.get("category") or "").strip() != "color":
                continue
            subject = (r.get("kanji") or "").strip()
            if not subject or subject == "---":
                subject = (r.get("hiragana") or "").strip()
            if not gen.color_word(subject, r.get("meaning") or ""):
                bad.append(f"{subject}({r.get('meaning')})")
        self.assertEqual(bad, [], f"候选表里被误标成 color 的词：{bad}")


class TestWordSimilar(unittest.TestCase):
    """新增单词的相近词匹配：写法/读音相等或互相包含即命中，单字包含不算。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "第1课", "words": [
            {"id": "w001", "kanji": "中国", "hiragana": "ちゅうごく", "meaning": "中国"},
            {"id": "w002", "kanji": "中国語", "hiragana": "ちゅうごくご", "meaning": "中文"},
            {"id": "w003", "kanji": "---", "hiragana": "アニメ", "meaning": "动画"},
            {"id": "w004", "kanji": "---", "hiragana": "アニメーション", "meaning": "动画制作"},
            {"id": "w005", "kanji": "一", "hiragana": "いち", "meaning": "一"},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR = self._old
        self._tmp.cleanup()

    def _similar(self, kanji="", hiragana="", **kw):
        payload = {"kanji": kanji, "hiragana": hiragana}
        payload.update(kw)
        return self.client.post("/api/words/similar", json=payload).get_json()["similar"]

    def test_kanji_containment_matches_both_directions(self):
        # 中国 → 命中 中国（相同）与 中国語（相近，用户举的例子）
        refs = [(x["ref"], x["relation"]) for x in self._similar(kanji="中国")]
        self.assertIn(("lesson_01:w001", "写法相同"), refs)
        self.assertIn(("lesson_01:w002", "写法相近"), refs)
        # 反向：输入 中国語 也能拉出 中国
        refs = [x["ref"] for x in self._similar(kanji="中国語")]
        self.assertIn("lesson_01:w001", refs)

    def test_kana_containment_and_exact_word(self):
        # 假名包含：アニメ → アニソン（读音相近）；完全相同排最前
        hits = self._similar(kanji="中国", hiragana="ちゅうごく")
        self.assertEqual(hits[0]["ref"], "lesson_01:w001")
        self.assertEqual(hits[0]["relation"], "完全相同")
        refs = [x["ref"] for x in self._similar(hiragana="アニメ")]
        self.assertIn("lesson_01:w004", refs)

    def test_single_char_containment_not_matched(self):
        # 「一」的互相包含（一月/一人 这类）会拉出满屏噪声，不计分
        refs = [x["ref"] for x in self._similar(kanji="一")]
        self.assertEqual(refs, ["lesson_01:w005"])  # 只有写法完全相同的那条

    def test_empty_input_and_exclude_ref(self):
        self.assertEqual(self._similar(), [])
        self.assertEqual(self._similar(kanji="  ", hiragana=""), [])
        # 修改已有词时排除自身
        hits = self._similar(kanji="中国", exclude_ref="lesson_01:w001")
        refs = [x["ref"] for x in hits]
        self.assertNotIn("lesson_01:w001", refs)
        self.assertIn("lesson_01:w002", refs)


class TestWordEdit(unittest.TestCase):
    """修改已有词：字段更新、id 与学习进度保留、读音/例句变更清音频缓存。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_mywords": {"title": "我的生词本", "words": [
            {"id": "w001", "kanji": "喉", "hiragana": "のど", "meaning": "喉咙",
             "mastery": 3, "last_reviewed": "2026-09-05", "next_due": "2026-09-10",
             "notes": "", "example_ja": "喉が渇いた", "example_zh": "口渴了"},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        audio = tmp / "audio"
        audio.mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, app.AUDIO_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        app.AUDIO_DIR = audio
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, app.AUDIO_DIR = self._old
        self._tmp.cleanup()

    def _edit(self, **kw):
        payload = {"ref": "lesson_mywords:w001", "kanji": "喉",
                   "hiragana": "のど", "meaning": "喉咙"}
        payload.update(kw)
        return self.client.post("/api/words/edit", json=payload)

    def test_edit_updates_fields_and_keeps_progress(self):
        res = self._edit(meaning="喉咙；嗓子", notes="つかれる")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["ok"])
        w = app.load_vocab()["lessons"]["lesson_mywords"]["words"][0]
        # 中文释义不做 NFKC：全角标点原样保留（折成半角会与 CSV 导入的词条风格不一致）
        self.assertEqual(w["meaning"], "喉咙；嗓子")
        self.assertEqual(w["notes"], "つかれる")
        self.assertEqual(w["mastery"], 3)  # 学习进度原样保留
        self.assertEqual(w["next_due"], "2026-09-10")

    def test_edit_clears_example_when_empty(self):
        res = self._edit(example_ja="", example_zh="")
        self.assertEqual(res.status_code, 200)
        w = app.load_vocab()["lessons"]["lesson_mywords"]["words"][0]
        self.assertNotIn("example_ja", w)
        self.assertNotIn("example_zh", w)

    def test_edit_tip_update_clear_and_keep(self):
        # 传了 tip 就更新
        self._edit(tip="书面说法，口语用買う")
        w = app.load_vocab()["lessons"]["lesson_mywords"]["words"][0]
        self.assertEqual(w["tip"], "书面说法，口语用買う")
        # 传空串 = 清除
        self._edit(tip="")
        w = app.load_vocab()["lessons"]["lesson_mywords"]["words"][0]
        self.assertNotIn("tip", w)
        # 不传 tip 键 = 保持原样（调用方可能只改释义）
        self._edit(tip="保留我")
        res = self._edit(meaning="喉咙")
        self.assertEqual(res.status_code, 200)
        w = app.load_vocab()["lessons"]["lesson_mywords"]["words"][0]
        self.assertEqual(w["tip"], "保留我")

    def test_edit_invalid_input(self):
        self.assertEqual(self._edit(hiragana="").status_code, 400)
        self.assertEqual(self._edit(meaning="").status_code, 400)
        self.assertEqual(self._edit(ref="lesson_nope:w001").status_code, 404)
        self.assertEqual(self._edit(ref="bad-ref").status_code, 400)

    def test_edit_stale_audio_invalidated_on_reading_change(self):
        # 读音变更后按 ref 缓存的音频已过时：单词音频删除重生成，例句不动
        word_audio = app.audio_path_for_ref("lesson_mywords:w001")
        ex_audio = app.AUDIO_DIR / (word_audio.stem + "_ex.mp3")
        word_audio.write_bytes(b"old")
        ex_audio.write_bytes(b"old-ex")
        self._edit(hiragana="のどもと")
        self.assertFalse(word_audio.exists())
        self.assertTrue(ex_audio.exists())
        # 例句变更同理：只清例句音频
        self._edit(example_ja="喉が渇きました")
        self.assertFalse(ex_audio.exists())
        # 读音没变时不误删
        word_audio.write_bytes(b"new")
        self._edit(meaning="喉咙")
        self.assertTrue(word_audio.exists())


class TestTtsRegen(unittest.TestCase):
    """重置语音：先生成到临时文件试听，apply 才覆盖/丢弃；旧缓存不动。"""

    STATIC = Path(__file__).resolve().parent / "static"
    TEMPLATES = Path(__file__).resolve().parent / "templates"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "t", "words": [
            # 纯假名词：tts_word_text 恒返回读音，测试不随汉字切换分支漂移
            {"id": "w001", "kanji": "---", "hiragana": "ひと", "meaning": "人"},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        audio = tmp / "audio"
        audio.mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, app.AUDIO_DIR,
                     app.generate_audio_sync)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        app.AUDIO_DIR = audio

        def fake_generate(text, out_path, max_retries=3):
            out_path.write_bytes(("audio:" + text).encode("utf-8"))
            return True

        app.generate_audio_sync = fake_generate  # 不联网的桩
        self.client = app.app.test_client()

    def tearDown(self):
        (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, app.AUDIO_DIR,
         app.generate_audio_sync) = self._old
        self._tmp.cleanup()

    def test_regen_writes_temp_only_and_returns_audio(self):
        main = app.audio_path_for_ref("lesson_01:w001")
        main.write_bytes(b"old")  # 现有缓存
        res = self.client.post("/api/tts/regen", json={"ref": "lesson_01:w001"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("audio", data)
        self.assertEqual(main.read_bytes(), b"old")  # 旧缓存未动
        temp = app.AUDIO_DIR / (main.stem + ".regen.mp3")
        self.assertTrue(temp.exists())

    def test_apply_replace_and_discard(self):
        main = app.audio_path_for_ref("lesson_01:w001")
        main.write_bytes(b"old")
        temp = app.AUDIO_DIR / (main.stem + ".regen.mp3")
        self.client.post("/api/tts/regen", json={"ref": "lesson_01:w001"})
        res = self.client.post("/api/tts/regen/apply",
                               json={"ref": "lesson_01:w001", "keep": "new"})
        self.assertTrue(res.get_json()["replaced"])
        self.assertEqual(main.read_bytes(), "audio:ひと".encode("utf-8"))  # 新音频转正
        self.assertFalse(temp.exists())
        # 保留原音频：临时文件删除、缓存不动
        self.client.post("/api/tts/regen", json={"ref": "lesson_01:w001"})
        res = self.client.post("/api/tts/regen/apply",
                               json={"ref": "lesson_01:w001", "keep": "old"})
        self.assertFalse(res.get_json()["replaced"])
        self.assertEqual(main.read_bytes(), "audio:ひと".encode("utf-8"))
        self.assertFalse(temp.exists())

    def test_apply_without_regen_404(self):
        res = self.client.post("/api/tts/regen/apply",
                               json={"ref": "lesson_01:w001", "keep": "new"})
        self.assertEqual(res.status_code, 404)

    def test_regen_unknown_word_404(self):
        res = self.client.post("/api/tts/regen", json={"ref": "lesson_01:w999"})
        self.assertEqual(res.status_code, 404)

    def test_learn_screen_wired_to_regen(self):
        """学习卡片同样要能重置语音：第一次听到某个词的发音，最可能发现残缺。

        两处共用一套流程（REGEN_CTX 抽走「当前词是谁 + 状态写哪块 DOM」），
        漏接线的表现只是按钮点了没反应，Python 侧抓不到，只能查前端。
        """
        index = (self.TEMPLATES / "index.html").read_text(encoding="utf-8")
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="learn-regen-btn"', index)
        self.assertIn('id="learn-regen-panel"', index)
        self.assertIn('$("learn-regen-btn").addEventListener("click", () => regenCurrentAudio("learn"))',
                      appjs)
        # 不能直接传函数引用：click 事件对象会被当成 which，取 ctx 时直接炸
        self.assertNotIn('addEventListener("click", regenCurrentAudio)', appjs)
        # 取当前词要覆盖学习分支，否则重置的是练习队列里的题（学习阶段根本没有）
        self.assertIn('which === "learn"', appjs)

    def test_kanji_screen_wired_to_regen(self):
        """汉字卡同样要能重置语音：重置的就是「读第一个词」那个发音。"""
        index = (self.TEMPLATES / "index.html").read_text(encoding="utf-8")
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="k-regen-btn"', index)
        self.assertIn('id="k-regen-panel"', index)
        self.assertIn('$("k-regen-btn").addEventListener("click", () => regenCurrentAudio("kanji"))',
                      appjs)
        # 取当前词要覆盖汉字卡分支，且必须与「读第一个词」同口径（k.words[0]）
        self.assertIn('which === "kanji"', appjs)

    def test_regen_ui_resets_on_card_change(self):
        """切卡片/切字时清掉上一张的试听面板：留着会跟着翻页变成残影。"""
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        for fn in ("renderLearnCard", "renderKanjiCard"):
            body = re.search(rf"function {fn}\(\) \{{.*?\n\}}", appjs, re.DOTALL)
            self.assertIsNotNone(body, f"找不到 {fn}")
            self.assertIn("resetRegenUI()", body.group(0), f"{fn} 切换时没清重置面板")
        ui = re.search(r"function resetRegenUI\(\) \{.*?\n\}", appjs, re.DOTALL)
        self.assertIsNotNone(ui, "找不到 resetRegenUI")
        self.assertIn("REGEN_CTX", ui.group(0))  # 三块面板一起清，不能只清练习页


class TestMasteryBadge(unittest.TestCase):
    """掌握度标注：后端下发 mastery，前端按 3 档（新 / 在学 / 会了）着色。

    两个容易踩的坑：
    1. 掌握度在 /api/finish 才落盘，所以接口给的是「作答前」的档位
       （含义是「这个词你原本有多熟」，不预判答后升降——那要复制交卷的
       计分规则，含选择题连对闸，容易和落盘值对不上）。
    2. 真题 / 助词的 word 是虚拟构造，根本没有 mastery 字段，
       前端不能把「缺数据」fallback 成 0，否则那些题会被误标成「新」。
    """

    STATIC = Path(__file__).resolve().parent / "static"
    TEMPLATES = Path(__file__).resolve().parent / "templates"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "t", "words": [
            {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人"},  # m=0 新词
            {"id": "w002", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗",
             "mastery": 3},  # 会读写
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR = self._old
        self._tmp.cleanup()

    def test_mastery_level_clamps_and_defaults(self):
        self.assertEqual(app.mastery_level({"mastery": 3}), 3)
        self.assertEqual(app.mastery_level({}), 0)                 # 缺字段
        self.assertEqual(app.mastery_level({"mastery": None}), 0)  # 手改成 null
        self.assertEqual(app.mastery_level({"mastery": "2"}), 2)   # 手改成字符串
        self.assertEqual(app.mastery_level({"mastery": "abc"}), 0)  # 完全非法
        self.assertEqual(app.mastery_level({"mastery": 99}), app.MASTERY_MAX)
        self.assertEqual(app.mastery_level({"mastery": -1}), app.MASTERY_MIN)

    def test_check_word_carries_mastery_before_scoring(self):
        """反馈卡 / 错题回顾的数据源：作答前的档位。"""
        data = self.client.post("/api/check", json={
            "ref": "lesson_01:w002", "mode": "audio_to_kana", "answer": "いぬ"}).get_json()
        self.assertEqual(data["word"]["mastery"], 3)
        # 掌握度不被 /api/check 改动（落盘在 /api/finish）
        self.assertEqual(app.load_vocab()["lessons"]["lesson_01"]["words"][1]["mastery"], 3)

    def test_kanji_words_carry_mastery(self):
        data = self.client.get("/api/kanji?lesson=lesson_01").get_json()
        by_ref = {w["ref"]: w for k in data["kanji"] for w in k["words"]}
        self.assertEqual(by_ref["lesson_01:w001"]["mastery"], 0)  # 新词 → 标「新」
        self.assertEqual(by_ref["lesson_01:w002"]["mastery"], 3)  # 会了 → 青绿

    def test_learn_and_flashcard_carry_mastery(self):
        learn = self.client.post("/api/learn/start", json={
            "lesson": "lesson_01", "count": 7}).get_json()
        self.assertEqual(learn["words"][0]["word"]["mastery"], 0)  # 学新词恒为 0
        st = self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": "flashcard", "count": 5}).get_json()
        for q in st["questions"]:
            self.assertIn("mastery", q["word"])

    def test_virtual_words_not_marked_as_new(self):
        """真题 / 助词的 word 没有 mastery：缺数据不得 fallback 成 0（会被误标「新」）。"""
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("if (!Number.isInteger(m) || m < 0 || m > 5) return null;", appjs)

    def test_frontend_has_three_tiers_and_styles(self):
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        for cls in ("m-new", "m-learn", "m-known"):
            self.assertIn(cls, appjs)
        self.assertIn("masteryClass(", appjs)
        self.assertIn("masteryTag(", appjs)
        css = (self.STATIC / "style.css").read_text(encoding="utf-8")
        # 三档色条要真的作用到三类容器上，只加类名不生效
        for sel in (".fb-word.m-new", ".wrong-item.m-new", ".k-word.m-new"):
            self.assertIn(sel, css)
        self.assertIn("--m-color", css)


class TestWorks(unittest.TestCase):
    """作品沉浸：阅读器的标注、词库命中策略、挖矿闭环、列表/画像接口。

    词表与作品树都建在临时目录（works.WORKS_DIR 一并重定向），不碰真实数据。
    """

    def setUp(self):
        import works
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "第1课", "words": [
            {"id": "w001", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗", "mastery": 3},
            {"id": "w002", "kanji": "卵", "hiragana": "たまご", "meaning": "蛋", "mastery": 1},
            {"id": "w003", "kanji": "見る", "hiragana": "みる", "meaning": "看", "mastery": 2},
            {"id": "w004", "kanji": "戸", "hiragana": "と", "meaning": "门", "mastery": 5},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR,
                     works.WORKS_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        works.WORKS_DIR = tmp / "works"
        works._profile_cache.clear()
        works._sent_cache.clear()
        self.client = app.app.test_client()
        self._make_work()

    def tearDown(self):
        import works
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, works.WORKS_DIR = self._old
        works._profile_cache.clear()
        works._sent_cache.clear()
        self._tmp.cleanup()

    def _make_work(self, wid="wk1", title="测试作品"):
        import works
        wdir = works.WORKS_DIR / wid
        (wdir / "content").mkdir(parents=True)
        meta = {"id": wid, "title": title, "author": "", "type": "novel",
                "created": date.today().isoformat(),
                "chapters": [{"id": 0, "title": "第1章", "file": "ch00.json",
                              "sentences": 2}]}
        (wdir / "work.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        ch0 = {"title": "第1章", "paras": [
            ["犬とたまごを見る。", "戸を閉める。"]]}
        (wdir / "content/ch00.json").write_text(
            json.dumps(ch0, ensure_ascii=False), encoding="utf-8")

    # ---- 分词 / 命中 ----

    def test_split_sentences_keeps_quotes_together(self):
        import works
        self.assertEqual(works.split_sentences("「おはよう。今日はいい。」"),
                         ["「おはよう。今日はいい。」"])
        self.assertEqual(works.split_sentences("「はい。」「いいえ。」"),
                         ["「はい。」", "「いいえ。」"])
        self.assertEqual(works.split_sentences("田中は「はい」と言った。行った。"),
                         ["田中は「はい」と言った。", "行った。"])

    def test_lookup_rejects_short_kana_hit_on_kanji_reading(self):
        """と 只该命中「戸」的读音，短假名命中被拒——不然 と→戸 到处误标。"""
        import works
        vocab = app.load_vocab()
        form_map, _ = works.build_form_map(vocab)
        self.assertIsNone(works.lookup(form_map, "と"))   # 戸 的读音，拒
        self.assertEqual(works.lookup(form_map, "戸")[0], "lesson_01:w004")
        # 3 字假名串按读音命中（たまご→卵，词写作假名是常态）
        self.assertEqual(works.lookup(form_map, "たまご")[0], "lesson_01:w002")

    def test_chapter_annotation_payload(self):
        data = self.client.get("/api/works/wk1/chapter/0").get_json()
        self.assertEqual(data["chapter"]["id"], 0)
        s = data["paras"][0][0]
        # 词块拼回去必须等于原文
        self.assertEqual("".join(x["t"] for x in s["segs"]), "犬とたまごを見る。")
        by_text = {x["t"]: x for x in s["segs"]}
        self.assertEqual(by_text["犬"]["m"], 3)     # 已学（3+）
        self.assertEqual(by_text["たまご"]["m"], 1)  # 在学，按读音命中
        self.assertEqual(by_text["見る"]["m"], 2)
        # 短假名串 と/を 不该被标成词（词库里只有 戸 的同音条目），
        # 但仍是可点击的库外词块（没有 w 字段）
        self.assertNotIn("w", by_text["と"])
        self.assertNotIn("w", by_text["を"])
        # 词条卡带全字段
        card = data["words"]["lesson_01:w001"]
        self.assertEqual(card["meaning"], "狗")
        self.assertEqual(card["lesson_title"], "第1课")
        self.assertIn("mastery", card)

    def test_work_list_detail_and_404s(self):
        data = self.client.get("/api/works").get_json()
        self.assertEqual([w["id"] for w in data["works"]], ["wk1"])
        r = self.client.get("/api/works/wk1")
        self.assertEqual(r.status_code, 200)
        prof = r.get_json()["profile"]
        for key in ("coverage", "sentences", "unique_known", "top_unknown"):
            self.assertIn(key, prof)
        self.assertEqual(self.client.get("/api/works/nope").status_code, 404)
        self.assertEqual(self.client.get("/api/works/wk1/chapter/9").status_code, 404)

    def test_sentence_edit_updates_content(self):
        """人工修 whisper/台本错字：写回 content JSON，重标后生效。"""
        import works
        # 改第 0 段第 1 句（原本「戸を閉める。」）
        r = self.client.post("/api/works/wk1/chapter/0/sentence", json={
            "para": 0, "index": 1, "text": "戸を締める。"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        ch = works.load_chapter_raw("wk1", 0)
        self.assertEqual(ch["paras"][0][1], "戸を締める。")
        # 重新标注的词块拼回原文一致
        data = self.client.get("/api/works/wk1/chapter/0").get_json()
        self.assertEqual("".join(x["t"] for x in data["paras"][0][1]["segs"]),
                         "戸を締める。")
        # 参数校验：越界 / 空文本 / 缺定位
        self.assertEqual(self.client.post(
            "/api/works/wk1/chapter/0/sentence",
            json={"para": 9, "index": 0, "text": "x"}).status_code, 400)
        self.assertEqual(self.client.post(
            "/api/works/wk1/chapter/0/sentence",
            json={"para": 0, "index": 9, "text": "x"}).status_code, 400)
        self.assertEqual(self.client.post(
            "/api/works/wk1/chapter/0/sentence",
            json={"para": 0, "index": 0, "text": "  "}).status_code, 400)
        self.assertEqual(self.client.post(
            "/api/works/nope/chapter/0/sentence",
            json={"para": 0, "index": 0, "text": "x"}).status_code, 404)

    # ---- 挖矿闭环 ----

    def _mine(self, **kw):
        body = {"surface": "魔法", "reading": "まほう", "meaning": "魔法",
                "sentence": "犬が魔法を見る。", "chapter": 0}
        body.update(kw)
        return self.client.post("/api/works/wk1/mine", json=body)

    def test_mine_adds_to_mywords_and_notes_source(self):
        res = self._mine()
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        ref = data["ref"]
        self.assertTrue(ref.startswith("lesson_mywords:"))
        words = app.load_vocab()["lessons"]["lesson_mywords"]["words"]
        w = words[0]
        self.assertEqual(w["kanji"], "魔法")
        self.assertEqual(w["hiragana"], "まほう")
        self.assertIn("测试作品", w["notes"])       # 来源要记录到备注
        self.assertEqual(w["example_ja"], "犬が魔法を見る。")  # 例句＝原句
        # 挖完之后词表生效：下次标注立刻把这个词当已知词（m=0 灰色档）
        data2 = self.client.get("/api/works/wk1/chapter/0").get_json()
        for p in data2["paras"]:
            for s in p:
                for x in s["segs"]:
                    if x.get("w") == ref:
                        self.assertEqual(x["m"], 0)
                        break

    def test_mine_duplicate_conflicts(self):
        res1 = self._mine()
        self.assertEqual(res1.status_code, 200)
        ref = res1.get_json()["ref"]
        # 同一词再挖：409 且带上已有 ref
        res2 = self._mine()
        self.assertEqual(res2.status_code, 409)
        self.assertEqual(res2.get_json()["ref"], ref)
        # 词库其他课里已经有同形同音词：同样 409，不重复录
        vocab = app.load_vocab()
        vocab["lessons"]["lesson_01"]["words"].append(
            {"id": "w005", "kanji": "魔法", "hiragana": "まほう", "meaning": "魔法"})
        app.save_vocab(vocab)
        res3 = self._mine()
        self.assertEqual(res3.status_code, 409)
        self.assertEqual(res3.get_json()["ref"], "lesson_01:w005")
        # 挖词库已有词（犬）同样拦下
        res4 = self._mine(surface="犬", reading="いぬ", meaning="狗")
        self.assertEqual(res4.status_code, 409)

    def test_mine_validation(self):
        self.assertEqual(self._mine(meaning="").status_code, 400)
        self.assertEqual(self._mine(surface="").status_code, 400)
        self.assertEqual(self.client.post("/api/works/nope/mine", json={
            "surface": "x", "meaning": "y"}).status_code, 404)

    def test_mine_keeps_chinese_punctuation(self):
        """释义是中文：全角标点原样落盘，不折成半角。

        挖矿的释义大多直接来自 data/dict_zh.json，那里多义项用全角逗号
        （「明显，清楚」）、夹注用全角括号（「包含（如价格中含税）」）——
        走 NFKC 会变成半角，与本地词典和 CSV 导入的词条风格都不一致。
        这是 NFKC 最容易踩到的一条路径：点一下义项即保存。
        """
        res = self._mine(surface="明白", reading="めいはく",
                         meaning="明显，清楚（书面）")
        self.assertEqual(res.status_code, 200)
        w = app.load_vocab()["lessons"]["lesson_mywords"]["words"][-1]
        self.assertEqual(w["meaning"], "明显，清楚（书面）")

    # ---- 界面导入（/api/works/import）----

    def _upload(self, name, raw, **fields):
        return self.client.post(
            "/api/works/import",
            data={"file": (io.BytesIO(raw), name), **fields},
            content_type="multipart/form-data")

    def test_upload_txt(self):
        body = ("第1章\n犬が好きです。卵を食べます。\n"
                "「おはよう。」\n第2章\n猫も好きです。\n").encode("utf-8")
        res = self._upload("我的书.txt", body, title="上传测试", author="示例")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["title"], "上传测试")
        self.assertEqual(data["chapters"], 2)
        self.assertEqual(data["sentences"], 4)
        # 列表与阅读接口都能看到这部新作品
        ids = [w["id"] for w in self.client.get("/api/works").get_json()["works"]]
        self.assertIn(data["work_id"], ids)
        ch = self.client.get(f"/api/works/{data['work_id']}/chapter/0").get_json()
        self.assertEqual(ch["chapter"]["title"], "第1章")
        self.assertEqual(ch["paras"][0][0]["text"], "犬が好きです。")
        # work.json 的 source 记的是上传时的文件名（不是临时文件名）
        import works
        meta = works.load_work(data["work_id"])
        self.assertEqual(meta["source"], "我的书.txt")

    def test_upload_reimport_overwrites_same_id(self):
        res1 = self._upload("re.txt", "第1章\n犬です。\n".encode("utf-8"), title="同名作品")
        self.assertEqual(res1.status_code, 200)
        wid = res1.get_json()["work_id"]
        res2 = self._upload("re2.txt", "第1章\n猫です。\n".encode("utf-8"), title="同名作品")
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.get_json()["work_id"], wid)
        # 同名覆盖而不是叠加出第二部
        ids = [w["id"] for w in self.client.get("/api/works").get_json()["works"]]
        self.assertEqual(ids.count(wid), 1)

    def test_upload_epub(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("META-INF/container.xml",
                        '<container><rootfiles><rootfile full-path="content.opf"/>'
                        "</rootfiles></container>")
            zf.writestr("content.opf",
                        "<package><manifest><item id=\"c1\" href=\"ch1.xhtml\" "
                        "media-type=\"application/xhtml+xml\"/></manifest>"
                        "<spine><itemref idref=\"c1\"/></spine></package>")
            zf.writestr("ch1.xhtml",
                        "<html><body><p>これは本です。</p>"
                        "<p>あの本を読みます。</p></body></html>")
        res = self._upload("book.epub", buf.getvalue())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["title"], "book")  # 缺省用文件名
        self.assertEqual(data["sentences"], 2)

    def test_upload_rejects_bad_input(self):
        # 扩展名不支持
        self.assertEqual(self._upload("bad.pdf", b"x").status_code, 400)
        # 空文件
        self.assertEqual(self._upload("empty.txt", b"").status_code, 400)
        # 缺文件
        res = self.client.post("/api/works/import",
                               data={"title": "x"},
                               content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)
        # 解析不到任何文本（全是空白行）
        res = self._upload("空白.txt", " \t \n\n  \n".encode("utf-8"))
        self.assertEqual(res.status_code, 400)
        self.assertIn("解析", res.get_json()["error"])

    # ---- 音声作品压缩包（/api/works/import 上传 .zip）----

    ZIP_VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nこんばんは。\n\n" \
              "00:00:03.000 --> 00:00:06.000\nおやすみなさい。\n"

    def _voice_zip(self, name="RJ99999999.zip"):
        """音轨 + 同名字幕打包的 zip（就像 DLsite 下载包）。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("RJ99999999/track1.mp3", "fake-mp3")
            zf.writestr("RJ99999999/track1.mp3.vtt", self.ZIP_VTT)
        return buf.getvalue()

    def test_upload_voice_zip(self):
        res = self._upload("RJ99999999.zip", self._voice_zip())
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["work_id"], "rj99999999")   # RJ 号自动当作品 id
        self.assertEqual(data["type"], "voice")
        self.assertEqual(data["chapters"], 1)
        self.assertEqual(data["sentences"], 2)
        # 列表里标注音声类型，章节带时间轴
        works_list = self.client.get("/api/works").get_json()["works"]
        w = next(x for x in works_list if x["id"] == "rj99999999")
        self.assertEqual(w["type"], "voice")
        self.assertTrue(w["chapters"][0]["timed"])
        # 阅读接口能出句子 + 句级时间轴（t0/t1 挂在句子 item 上）
        ch = self.client.get("/api/works/rj99999999/chapter/0").get_json()
        s0 = ch["paras"][0][0]
        self.assertEqual(s0["text"], "こんばんは。")
        self.assertEqual((s0["t0"], s0["t1"]), (1.0, 3.0))
        # 音轨解压落盘且在作品登记里（data/audio/<key>/，不入 git）
        import works as works_mod
        meta = works_mod.load_work("rj99999999")
        audio_path = meta["chapters"][0]["audio"]
        self.assertTrue(Path(audio_path).exists())
        self.assertTrue(Path(audio_path).is_relative_to(
            app.works.WORKS_DIR.parent / "audio"))

    # ---- 上传体积：读进内存之前就该拒掉 ----

    def test_upload_zip_rejected_before_being_read(self):
        """超限 zip 直接拒，且不留解压目录。

        原来写的是 raw = f.read() 再判 len(raw) > 上限：音声 zip 是音轨包
        （上限 2GB），超限请求会先把整个文件搬进 RAM 才回一句「太大」，
        MemoryError 杀死进程正好落在写词表/写作品树的窗口里。
        """
        import works
        payload = self._voice_zip()
        old = app.VOICE_UPLOAD_MAX_BYTES
        app.VOICE_UPLOAD_MAX_BYTES = len(payload) - 1     # 真实包不可能这么大
        try:
            res = self._upload("超限包.zip", payload)
        finally:
            app.VOICE_UPLOAD_MAX_BYTES = old
        self.assertEqual(res.status_code, 400, res.get_json())
        self.assertIn("文件太大", res.get_json()["error"])
        self.assertFalse((works.WORKS_DIR.parent / "audio" / "超限包").exists())

    def test_fs_nbytes_ignores_zero_content_length(self):
        """FileStorage.content_length 读的是分片头，multipart 分片通常根本没这条
        头，werkzeug 于是返回 0 而不是 None——按「非 None 就采用」会把每个上传都
        判成空文件（本用例就是那次翻车的锁）。"""
        class Part:
            content_length = 0

        data = b"12345678"
        part = Part()
        part.stream = io.BytesIO(data)
        self.assertEqual(app._fs_nbytes(part), 8)
        self.assertEqual(part.stream.tell(), 0)   # 探大小不许把游标挪走

        class WithHeader:
            content_length = 123
            stream = None

        self.assertEqual(app._fs_nbytes(WithHeader()), 123)

        class Unusable:
            content_length = 0
            stream = None

        self.assertIsNone(app._fs_nbytes(Unusable()))

    def test_request_body_ceiling_returns_json(self):
        """整条请求超限（werkzeug 掐断流）：必须返 JSON，导入页读的是 {error}，
        默认的 413 HTML 页在界面上只会糊成一句「请求失败」。"""
        old = app.app.config["MAX_CONTENT_LENGTH"]
        app.app.config["MAX_CONTENT_LENGTH"] = 64
        try:
            res = self._upload("big.txt", ("犬です。\n" * 200).encode("utf-8"))
        finally:
            app.app.config["MAX_CONTENT_LENGTH"] = old
        self.assertEqual(res.status_code, 413, res.get_json())
        self.assertIn("上传内容过大", res.get_json()["error"])

    def test_max_content_length_covers_legit_caps(self):
        """全局上限必须盖得住目录上传的体量：音轨文件夹动辄几 GB，先被 werkzeug
        挡掉的话业务层那句「太大」根本轮不到说，用户看到的是一句没头没尾的失败。"""
        self.assertGreaterEqual(app.app.config["MAX_CONTENT_LENGTH"],
                                app.DIR_UPLOAD_MAX_BYTES)
        self.assertGreaterEqual(app.app.config["MAX_CONTENT_LENGTH"],
                                app.VOICE_UPLOAD_MAX_BYTES)

    def test_upload_voice_zip_rejects_zip_slip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../evil.txt", "x")            # 越界路径
            zf.writestr("ok/track1.mp3", "fake-mp3")
        res = self._upload("evil.zip", buf.getvalue())
        self.assertEqual(res.status_code, 400)
        self.assertIn("越界", res.get_json()["error"])

    def test_upload_voice_zip_complex_layout(self):
        """压缩包目录复杂（音轨/字幕/台本/杂物分层）也能正确配对。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("RJ55555/README.txt", "説明書\n")           # 无关文件
            zf.writestr("RJ55555/mp3/01.mp3", "fake-mp3")            # 音轨子目录
            zf.writestr("RJ55555/mp3/01.mp3.vtt", self.ZIP_VTT)      # 音轨全名字幕
            zf.writestr("RJ55555/mp3/02.mp3", "fake-mp3-2")
            zf.writestr("RJ55555/字幕/02.srt",
                        "1\n00:00:01,000 --> 00:00:03,000\nおやすみなさい。\n")
            zf.writestr("RJ55555/images/cover.jpg", "img")           # 杂物
            zf.writestr("RJ55555/スクリプト/01.txt", "こんばんは。\n")
        res = self._upload("RJ55555.zip", buf.getvalue())
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        self.assertEqual(data["work_id"], "rj55555")
        self.assertEqual(data["chapters"], 2)     # 01、02 两条音轨都配到字幕
        self.assertEqual(data["sentences"], 3)    # 01: 2 句 + 02: 1 句
        ch = self.client.get("/api/works/rj55555/chapter/0").get_json()
        self.assertEqual(ch["paras"][0][0]["text"], "こんばんは。")
        self.assertEqual(ch["paras"][0][1]["text"], "おやすみなさい。")
        ch1 = self.client.get("/api/works/rj55555/chapter/1").get_json()
        self.assertEqual(ch1["paras"][0][0]["text"], "おやすみなさい。")

    def test_upload_voice_folder(self):
        """已解压文件夹（webkitdirectory）上传：没有单文件 file 字段，每个文件走
        并行的 files / path 两个列表——入口校验不能把这条来源挡在门外。"""
        res = self.client.post(
            "/api/works/import",
            data={
                "source": "dir",
                "title": "RJ77777777",
                "files": [
                    (io.BytesIO(b"fake-mp3"), "RJ77777777/mp3/01.mp3"),
                    (io.BytesIO(self.ZIP_VTT.encode("utf-8")),
                     "RJ77777777/mp3/01.mp3.vtt"),
                ],
                "path": ["RJ77777777/mp3/01.mp3", "RJ77777777/mp3/01.mp3.vtt"],
            },
            content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        self.assertEqual(data["work_id"], "rj77777777")
        self.assertEqual(data["type"], "voice")
        self.assertEqual(data["sentences"], 2)
        # 音轨按相对路径重建进 data/audio/<key>/（作品登记其绝对路径）
        import works as works_mod
        meta = works_mod.load_work("rj77777777")
        audio_path = Path(meta["chapters"][0]["audio"])
        self.assertTrue(audio_path.exists())
        self.assertTrue(audio_path.is_relative_to(
            app.works.WORKS_DIR.parent / "audio"))
        # 缺省作品名取相对路径第一段（所选文件夹名）
        res = self.client.post(
            "/api/works/import",
            data={"source": "dir", "files": [(io.BytesIO(b"x"), "RJ88888888/a.mp3")],
                  "path": ["RJ88888888/a.mp3"]},
            content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)     # 无字幕/台本且未 ASR：明确报错
        self.assertIn("没有任何一条轨道能出句子", res.get_json()["error"])
        # files / path 数量对不上：400 而不是 500
        res = self.client.post("/api/works/import",
                               data={"source": "dir", "title": "x",
                                     "path": ["a/b.mp3"]},
                               content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)

    def test_zip_name_restores_shift_jis(self):
        """老日文压缩包没有 UTF-8 标志位，zipfile 按 cp437 解码文件名成乱码；
        _zip_name 应按字节还原回 Shift-JIS 日文，纯 ASCII / 真 UTF-8 不受影响。"""
        sjis = "トラック01.mp3.vtt".encode("shift_jis")
        info = zipfile.ZipInfo(sjis.decode("cp437"))     # 无 UTF-8 flag 时的乱码
        self.assertFalse(info.flag_bits & 0x800)
        self.assertEqual(app._zip_name(info), "トラック01.mp3.vtt")
        # 纯 ASCII 原样
        self.assertEqual(app._zip_name(zipfile.ZipInfo("01.mp3")), "01.mp3")
        # 有 UTF-8 标志位的名字是正确解码，保持原样
        u = zipfile.ZipInfo("トラック01.mp3")
        u.flag_bits |= 0x800
        self.assertEqual(app._zip_name(u), "トラック01.mp3")


class TestTtsEnsureAudio(unittest.TestCase):
    """ensure_audio 的缓存目录兜底——/api/tts/text 把句子朗读缓存到
    audio/text/ 子目录，第一次写时若父目录不存在必须自动建，
    否则会写文件失败、被异常吞掉，前端只看到「没声音」静默失败。
    """

    def test_custom_subdir_created_on_demand(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            fake = tmp / "deep" / "sub" / "x.mp3"
            self.assertFalse(fake.parent.exists())
            original = app.generate_audio_sync

            def _fake(text, path, **kw):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"ID3" + b"\x00" * 100)
                return True

            app.generate_audio_sync = _fake
            try:
                p = app.ensure_audio("ref:x", "テスト", out_path=fake)
            finally:
                app.generate_audio_sync = original
            self.assertEqual(p, fake)
            self.assertTrue(p.exists() and p.stat().st_size > 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_existing_file_returned_without_regenerate(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "a.mp3"
            f.write_bytes(b"already")
            called = []

            def _should_not_run(text, path, **kw):
                called.append(1)
                return True

            original = app.generate_audio_sync
            app.generate_audio_sync = _should_not_run
            try:
                p = app.ensure_audio("r", "テスト", out_path=f)
            finally:
                app.generate_audio_sync = original
            self.assertEqual(p, f)
            self.assertFalse(called, "已有缓存就不该再调生成")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestMineHints(unittest.TestCase):
    """挖矿弹窗的词典预填（/api/dict）与跨作品出处统计（/api/words/occurrences）。

    词典与作品树都建在临时目录（app.BASE_DIR / works.WORKS_DIR 一并重定向）。
    """

    def setUp(self):
        import works
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        (tmp / "data").mkdir()
        (tmp / "data" / "dict_zh.json").write_text(json.dumps({"meta": {}, "words": {
            "走る|はしる": {"kanji": "走る", "kana": "はしる", "senses": [
                {"en": "to run", "pos": ["v5r"], "zh": "奔跑，跑步"},
                {"en": "to operate", "pos": ["v5r"], "zh": "运营，经营"}]},
            "参考|さんこう": {"kanji": "参考", "kana": "さんこう",
                            "senses": [{"en": "reference", "pos": ["n"], "zh": "参考"}]},
        }}, ensure_ascii=False), encoding="utf-8")
        (tmp / "vocabulary.json").write_text(
            json.dumps({"lessons": {}}, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, works.WORKS_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        works.WORKS_DIR = tmp / "works"
        works._sent_cache.clear()
        app._DICT_CACHE.update(key=None, by_kanji={}, by_kana={})
        self.client = app.app.test_client()
        self._make_work("wk1", "第一部", [["走る。", "走るのは楽しい。"], ["また走る。"]])
        self._make_work("wk2", "第二部", [["走る。"]])

    def tearDown(self):
        import works
        (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, works.WORKS_DIR) = self._old
        works._sent_cache.clear()
        app._DICT_CACHE.update(key=None, by_kanji={}, by_kana={})
        self._tmp.cleanup()

    def _make_work(self, wid, title, chapters):
        import works
        wdir = works.WORKS_DIR / wid
        (wdir / "content").mkdir(parents=True)
        meta = {"id": wid, "title": title, "created": "2026-09-01",
                "chapters": [{"id": i, "title": f"第{i + 1}章", "file": f"ch{i:02d}.json"}
                             for i in range(len(chapters))]}
        (wdir / "work.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        for i, paras in enumerate(chapters):
            (wdir / f"content/ch{i:02d}.json").write_text(
                json.dumps({"title": f"第{i + 1}章", "paras": [paras]}, ensure_ascii=False),
                encoding="utf-8")

    def test_dict_by_base_surface_and_reading(self):
        """原形优先，退回词形，再退回读音；多义项照给（前端让用户点选）。"""
        d = self.client.get("/api/dict", query_string={
            "surface": "走った", "base": "走る"}).get_json()
        self.assertTrue(d["available"])
        self.assertEqual(len(d["entries"]), 1)
        self.assertEqual(d["entries"][0]["kana"], "はしる")
        self.assertEqual([s["zh"] for s in d["entries"][0]["senses"]],
                         ["奔跑，跑步", "运营，经营"])
        self.assertEqual(self.client.get("/api/dict",
                         query_string={"surface": "参考"}).get_json()
                         ["entries"][0]["kana"], "さんこう")
        # 写法对不上时按读音兜底（送り仮名差异、纯假名词）
        d3 = self.client.get("/api/dict", query_string={
            "surface": "未知词", "reading": "はしる"}).get_json()
        self.assertEqual(d3["entries"][0]["kanji"], "走る")
        # 词形本身就是读音时（纯假名词/片假名外来语），surface 也要能命中读音索引
        d4 = self.client.get("/api/dict", query_string={"surface": "さんこう"}).get_json()
        self.assertEqual(d4["entries"][0]["kanji"], "参考")

    def test_dict_miss_and_bad_params(self):
        d = self.client.get("/api/dict", query_string={"surface": "ぬるぬる"}).get_json()
        self.assertEqual(d["entries"], [])
        self.assertEqual(self.client.get("/api/dict").status_code, 400)

    def test_dict_absent_file(self):
        """没装词典文件时 available=false，前端退回原表单而不是报错。"""
        (Path(self._tmp.name) / "data" / "dict_zh.json").unlink()
        app._DICT_CACHE.update(key=None, by_kanji={}, by_kana={})
        d = self.client.get("/api/dict", query_string={"surface": "走る"}).get_json()
        self.assertFalse(d["available"])
        self.assertEqual(d["entries"], [])

    def test_occurrences_across_works(self):
        """跨作品统计：总数、按作品降序、章节分布、样例句。"""
        o = self.client.get("/api/words/occurrences",
                            query_string={"surface": "走る"}).get_json()
        self.assertEqual(o["total"], 4)   # 第一部 2+1，第二部 1
        self.assertEqual([w["title"] for w in o["works"]], ["第一部", "第二部"])
        first = o["works"][0]
        self.assertEqual(first["count"], 3)
        self.assertEqual([(c["title"], c["count"]) for c in first["chapters"]],
                         [("第1章", 2), ("第2章", 1)])
        self.assertTrue(first["samples"][0]["sentence"])
        self.assertEqual(o["works"][1]["count"], 1)

    def test_occurrences_none_and_bad_params(self):
        o = self.client.get("/api/words/occurrences",
                            query_string={"surface": "ぬるぬる"}).get_json()
        self.assertEqual(o, {"total": 0, "works": []})
        self.assertEqual(self.client.get("/api/words/occurrences").status_code, 400)

    def test_frontend_wired(self):
        """两端接线：弹窗里要有词典区、义项按钮（点击填释义）与出处区。"""
        root = Path(app.__file__).resolve().parent
        js = (root / "static" / "reader.js").read_text(encoding="utf-8")
        for needle in ("/api/dict?", "/api/words/occurrences?surface=",
                       "loadMineHints(", "mine-dict", "mine-occ",
                       "dict-sense", "posZh("):
            self.assertIn(needle, js, needle)
        self.assertIn(".dict-sense", (root / "static" / "reader.css").read_text(
            encoding="utf-8"))


class TestSentenceCache(unittest.TestCase):
    """单句分析缓存：练词（mastery 变）不重扫、加词（词形变）才重算、
    落盘快照可跨重启复用（打开阅读器秒开的关键路径）。"""

    def setUp(self):
        import works
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._old_works = works.WORKS_DIR
        works.WORKS_DIR = tmp / "works"
        works.WORKS_DIR.mkdir(parents=True)
        works._ann_cache.clear()
        works._disk_loaded.clear()
        works._disk_ok.clear()

    def tearDown(self):
        import works
        works.WORKS_DIR = self._old_works
        works._ann_cache.clear()
        works._disk_loaded.clear()
        works._disk_ok.clear()
        self._tmp.cleanup()

    @staticmethod
    def _vocab(mastery=0):
        return {"lessons": {"l1": {"words": [
            {"id": "w001", "kanji": "学校", "hiragana": "がっこう",
             "mastery": mastery}]}}}

    def test_fingerprint_ignores_mastery_but_sees_new_words(self):
        import works
        fm1, _ = works.build_form_map(self._vocab(0))
        fm2, _ = works.build_form_map(self._vocab(5))
        self.assertEqual(works.form_fingerprint(fm1), works.form_fingerprint(fm2),
                         "掌握度变化不该让全库重扫")
        vocab = self._vocab()
        vocab["lessons"]["l1"]["words"].append(
            {"id": "w002", "kanji": "先生", "hiragana": "せんせい", "mastery": 0})
        fm3, _ = works.build_form_map(vocab)
        self.assertNotEqual(works.form_fingerprint(fm1), works.form_fingerprint(fm3),
                            "新增词条（词形集合变了）必须让缓存失效")

    def test_hit_skips_recompute_and_refreshes_mastery(self):
        import works
        fm, ml = works.build_form_map(self._vocab())
        fp = works.form_fingerprint(fm)
        calls = []
        orig = works.analyze_sentence

        def spy(*a, **kw):
            calls.append(a[0])
            return orig(*a, **kw)

        works.analyze_sentence = spy
        try:
            m1 = works.mastery_map(self._vocab(0))
            works.analyze_sentence_cached("学校へ行く。", fm, ml, None, fp, m1)
            self.assertEqual(len(calls), 1)
            works.analyze_sentence_cached("学校へ行く。", fm, ml, None, fp, m1)
            self.assertEqual(len(calls), 1, "命中缓存不该再分词")
            # 掌握度刷新：缓存里的 m 换成当前值（高亮颜色靠它）
            m2 = works.mastery_map(self._vocab(5))
            ann = works.analyze_sentence_cached("学校へ行く。", fm, ml, None, fp, m2)
            seg = [s for s in ann["segs"] if s.get("w") == "l1:w001"]
            self.assertTrue(seg, "应命中 学校")
            self.assertEqual(seg[0]["m"], 5)
        finally:
            works.analyze_sentence = orig

    def test_disk_snapshot_survives_restart(self):
        import works
        fm, ml = works.build_form_map(self._vocab())
        fp = works.form_fingerprint(fm)
        works._save_disk_annot("wk", fp, 0,
                               {"学校へ行く。": works.analyze_sentence("学校へ行く。", fm, ml, None)})
        # 模拟重启：清内存，只留磁盘
        works._ann_cache.clear()
        works._disk_loaded.clear()
        works._disk_ok.clear()
        calls = []
        orig = works.analyze_sentence
        works.analyze_sentence = lambda *a, **kw: calls.append(1) or orig(*a, **kw)
        try:
            works.ensure_disk_annot("wk", fp, 0)
            works.analyze_sentence_cached("学校へ行く。", fm, ml, None, fp,
                                          works.mastery_map(self._vocab()))
            self.assertEqual(calls, [], "重启后应直接吃磁盘快照")
        finally:
            works.analyze_sentence = orig
        # 指纹不符 → 快照不采用（宁可重扫，不拿旧缓存硬凑）
        self.assertIsNone(works._load_disk_annot("wk", "other-fp", 0))

    def test_profile_recompute_after_practice_skips_reanalysis(self):
        """练词后 profile 失效重算，但不再重新分词（用户「每次打开都要等」的根治点）。"""
        import works
        wdir = works.WORKS_DIR / "wk"
        (wdir / "content").mkdir(parents=True)
        (wdir / "work.json").write_text(json.dumps({
            "id": "wk", "title": "t", "created": "2026-09-01",
            "chapters": [{"id": 0, "file": "ch00.json", "sentences": 2}]},
            ensure_ascii=False), encoding="utf-8")
        (wdir / "content/ch00.json").write_text(json.dumps({
            "title": "第1章", "paras": [["学校へ行く。", "先生です。"]]},
            ensure_ascii=False), encoding="utf-8")
        work = works.load_work("wk")
        calls = []
        orig = works.analyze_sentence
        works.analyze_sentence = lambda *a, **kw: calls.append(1) or orig(*a, **kw)
        try:
            works.profile(work, self._vocab(0))
            n1 = len(calls)
            self.assertEqual(n1, 2, "两句各分析一次")
            works._profile_cache.clear()            # 模拟练词后词库变动
            works.profile(work, self._vocab(5))
            self.assertEqual(len(calls), n1, "重算画像不该重新分词")
        finally:
            works.analyze_sentence = orig


class TestReaderFrontend(unittest.TestCase):
    """阅读器前端：页面/脚本/样式与入口接线（纯源码断言，同既有前端回归锁风格）。"""

    def test_reader_assets_exist_and_are_linked(self):
        base = Path(app.BASE_DIR) / "practice"
        tpl = (base / "templates" / "reader.html").read_text(encoding="utf-8")
        self.assertIn("asset('reader.css')", tpl)
        self.assertIn("asset('reader.js')", tpl)
        for f in ("static/reader.js", "static/reader.css"):
            self.assertTrue((base / f).exists(), f)

    def test_home_jumps_to_reader(self):
        appjs = (Path(app.BASE_DIR) / "practice" / "static" / "app.js").read_text(
            encoding="utf-8")
        self.assertIn('window.location.href = "/reader"', appjs)

    def test_import_entry_uses_light_page_not_reader(self):
        # 「导入作品」走独立 /import 页（不加载阅读器），入口与模板都要能对上
        base = Path(app.BASE_DIR) / "practice"
        appjs = (base / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('window.location.href = "/import"', appjs)
        self.assertNotIn('window.location.href = "/reader?import=1"', appjs)
        self.assertTrue((base / "templates" / "import.html").exists(),
                        "缺少独立导入页模板 templates/import.html")

    def test_import_page_type_step_and_back_link(self):
        """导入页两级流程：先选类型（轻小说 / 音声），再展示对应表单。

        历史：入口曾把 txt/epub 并进「上传 .zip」的 accept 里，UI 看不出轻小说入口
        （实机反馈）；随后改成平铺的三个单选。现在两步走：类型卡片 → 对应表单，
        accept 随类型联动，返回链接走 history.back（回上一页，无历史才兜底首页）。
        """
        base = Path(app.BASE_DIR) / "practice"
        tpl = (base / "templates" / "import.html").read_text(encoding="utf-8")
        for needle in ('id="step-type"', 'id="step-form"', 'data-type="novel"',
                       'data-type="voice"',
                       'value="zip"', 'value="dir"',
                       '".txt,.epub"', '".zip"', "SOURCE_LABELS", "currentSource(",
                       'class="nav-header"', 'id="back-btn"', "window.history.back()"):
            self.assertIn(needle, tpl, needle)
        # 表单屏的「← 返回」先回类型选择屏（不是直接退到上一页）
        self.assertIn("if (importType) { showTypeStep(); return; }", tpl)
        # JS 里 $("id") 引用的每个 id 都要在 DOM 里（少一个就是运行时抛错）
        ids = set(re.findall(r'\bid="([^"]+)"', tpl))
        used = set(re.findall(r'\$\("([^"]+)"\)', tpl))
        self.assertEqual(sorted(used - ids), [],
                         "导入页 JS 引用了不存在的元素 id")

    def test_pages_share_nav_header_back_button(self):
        """顶部导航全站统一：主标题「日语练习」+ [← 返回] + 「板块 › 子项」。

        返回按钮走 history.back（回上一页）；从独立页面回到首页时，app.js 按
        URL 的 #module=<id> 恢复原模块页——「← 返回」因此是逐级回退，
        与「课程与新词 → 课程模式」的返回体验一致。
        """
        base = Path(app.BASE_DIR) / "practice"
        templates = {n: (base / "templates" / n).read_text(encoding="utf-8")
                     for n in ("index.html", "reader.html", "pictures.html",
                               "import.html", "stats.html")}
        # 正上方主标题全站统一（站点已不只单词，改叫「日语练习」）
        for name, tpl in templates.items():
            self.assertIn("<h1>日语练习</h1>", tpl, name)
        for name in ("reader.html", "pictures.html", "import.html", "stats.html"):
            tpl = templates[name]
            self.assertIn('class="nav-header"', tpl, name)
            self.assertIn('class="secondary back-btn"', tpl, name)
            # 返回右边是「板块 › 子项」（与配置页面包屑同一格式，标明隶属）
            self.assertRegex(tpl, r'class="nav-title">[^<]*›', name)
            self.assertNotIn("← 返回首页", tpl, name)           # 旧的居中返回链接已撤掉
        # SPA 内的直达屏（重点词 / 进度与薄弱点）同样用「板块 › 子项」
        idx = templates["index.html"]
        self.assertIn("🎓 课程与新词 › 🎯 重点词（聚焦练习）", idx)
        self.assertIn("📊 统计与复习 › 📈 进度与薄弱点", idx)
        self.assertNotIn('<a href="/stats">', idx)   # 主标题下方的统计入口已去
        # hidden 必须真的隐藏：.row 的 display:flex 会覆盖 UA 的 [hidden]，
        # 曾让「轻小说 / 音声」两屏长得一模一样（音声选项泄漏到轻小说屏）
        css = (base / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("[hidden] { display: none !important; }", css)
        appjs = (base / "static" / "app.js").read_text(encoding="utf-8")
        for needle in ("syncNavHash", "#module=", "bootFromHash"):
            self.assertIn(needle, appjs, needle)
        for js in ("reader.js", "pictures.js"):
            src = (base / "static" / js).read_text(encoding="utf-8")
            self.assertIn('$("back-btn").addEventListener', src, js)

    def test_word_popup_follows_page_and_sentence_edit_button(self):
        """词卡随页面滚动（absolute + page 坐标）；每句带 ✏️ 修正入口（修 ASR 错字）。

        历史：词卡曾用 position:fixed + clientX/clientY（钉在视口上），滚一下
        卡片就与点中的词「脱钩」停在屏幕原处；句子修正只有右键菜单，不好发现。
        """
        base = Path(app.BASE_DIR) / "practice"
        css = (base / "static" / "reader.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.word-popup \{[^}]*position: absolute")
        self.assertIn(".sent-btn.edit", css)
        js = (base / "static" / "reader.js").read_text(encoding="utf-8")
        self.assertIn("openWordCard(ref, e.pageX, e.pageY)", js)
        self.assertIn("openWordCard(seg.dataset.ref, e.pageX, e.pageY)", js)
        self.assertNotIn("openWordCard(ref, e.clientX, e.clientY)", js)
        self.assertIn("sent-btn edit", js)
        self.assertIn("openSentEdit(wrap)", js)
        tpl = (base / "templates" / "reader.html").read_text(encoding="utf-8")
        self.assertIn("✏️", tpl)   # 工具栏提示里写明入口


class TestTokenFix(unittest.TestCase):
    """分词修正：用户词典的合并逻辑、按句例外、与 /api/tokens 系列接口。

    合并路径走真代码，只把 janome 分词器打桩（works._janome_tokenizer）以保证确定性；
    入典 / 列表 / 删除 / 复查接口用真实 HTTP 跑临时目录，不碰真数据。
    """

    def setUp(self):
        import works
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "第1课", "words": [
            {"id": "w001", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗", "mastery": 3},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR,
                     works.WORKS_DIR, works._janome_tokenizer)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        works.WORKS_DIR = tmp / "works"
        works.WORKS_DIR.mkdir(parents=True)
        works._profile_cache.clear()
        works._ud_cache = None
        self.client = app.app.test_client()
        self._make_work()

    def tearDown(self):
        import works
        (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR,
         works.WORKS_DIR, works._janome_tokenizer) = self._old
        works._profile_cache.clear()
        works._ud_cache = None
        self._tmp.cleanup()

    def _make_work(self):
        import works
        wdir = works.WORKS_DIR / "wk1"
        (wdir / "content").mkdir(parents=True)
        meta = {"id": "wk1", "title": "测试作品", "author": "", "type": "novel",
                "created": date.today().isoformat(),
                "chapters": [{"id": 0, "title": "第1章", "file": "ch00.json",
                              "sentences": 2}]}
        (wdir / "work.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        ch0 = {"title": "第1章", "paras": [["犬とたまごを見る。", "本だからいい。"]]}
        (wdir / "content/ch00.json").write_text(
            json.dumps(ch0, ensure_ascii=False), encoding="utf-8")

    # ---- 纯合并逻辑 ----

    def test_merge_keep_merges_adjacent(self):
        import works
        toks = [("だ", "だ", "だ", ""), ("から", "から", "から", "")]
        out = works._merge_keep(toks, {"だから"}, 3, ())
        self.assertEqual(out, [("だから", "だから", "だから", "keep")])

    def test_merge_keep_respects_exception(self):
        import works
        toks = [("だ", "だ", "だ", ""), ("から", "から", "から", "")]
        out = works._merge_keep(toks, {"だから"}, 3, {"だから"})
        self.assertEqual(out, toks)   # 例外句：保持切开

    def test_merge_keep_longest_span(self):
        import works
        toks = [("あ", "", "", ""), ("り", "", "", ""), ("が", "", "", ""),
                ("と", "", "", ""), ("う", "", "", ""), ("か", "", "", "")]
        out = works._merge_keep(toks, {"ありがとう"}, 5, ())
        self.assertEqual([t[0] for t in out], ["ありがとう", "か"])

    def test_analyze_applies_keep_and_exception(self):
        """整条 analyze 链路：把 janome 桩成切成 だ+から，合不合都走真代码。"""
        import works

        class _Tok:
            def __init__(self, s):
                self.surface = s; self.reading = s
                self.base_form = s; self.part_of_speech = "助词"

        works._janome_tokenizer = type("Z", (), {
            "tokenize": lambda self, text: [_Tok("だ"), _Tok("から")]})()
        ud = {"keep": {"だから"}, "keep_max": 3, "exceptions": {}}
        ann = works.analyze_sentence("だから", {}, 0, ud)
        self.assertEqual([s["t"] for s in ann["segs"]], ["だから"])
        # 本句在例外集里 → 不合并
        ud2 = {"keep": {"だから"}, "keep_max": 3, "exceptions": {"だから": {"だから"}}}
        ann2 = works.analyze_sentence("だから", {}, 0, ud2)
        self.assertEqual([s["t"] for s in ann2["segs"]], ["だ", "から"])

    def test_analyze_keep_crosses_known_span(self):
        """「戸」在词库里会把 janome 的整词「瀬戸」切成 瀬＋戸，修正得跨过命中块。

        间隙内的 _merge_keep 够不着这种情形，用户就会遇到「选了瀬戸却说找不到
        可合并的位置」——命中块夹在中间，两块永远不在同一个间隙里。
        """
        import works
        readings = {"瀬": "せ", "戸": "と", "瀬戸": "せと", "さん": "さん"}

        class _Tok:
            def __init__(self, s):
                self.surface = s
                self.reading = readings.get(s, s)
                self.base_form = s
                self.part_of_speech = "名詞"

        # 传进来什么就当它是一个整词：模拟 janome 把「瀬戸」认成人名
        works._janome_tokenizer = type("Z", (), {
            "tokenize": lambda self, text: [_Tok(text)] if text else []})()
        vocab = {"lessons": {"lesson_01": {"title": "第1课", "words": [
            {"id": "w001", "kanji": "戸", "hiragana": "と", "meaning": "门", "mastery": 0},
        ]}}}
        form_map, max_len = works.build_form_map(vocab)

        # 未修正：命中块把整词切开
        base = works.analyze_sentence("瀬戸さん", form_map, max_len)
        self.assertEqual([s["t"] for s in base["segs"]], ["瀬", "戸", "さん"])
        # 修正后：跨过命中块并回去，读音取 janome 的整词读音（不是 せ＋と）
        ud = {"keep": {"瀬戸"}, "keep_max": 2, "exceptions": {}}
        ann = works.analyze_sentence("瀬戸さん", form_map, max_len, ud)
        self.assertEqual([s["t"] for s in ann["segs"]], ["瀬戸", "さん"])
        self.assertEqual(ann["segs"][0]["r"], "せと")
        self.assertNotIn("w", ann["segs"][0])     # 瀬戸 是库外词，不该再挂「戸」
        # 本句在例外集里 → 保持切开
        ud2 = {"keep": {"瀬戸"}, "keep_max": 2,
               "exceptions": {"瀬戸さん": {"瀬戸"}}}
        ann2 = works.analyze_sentence("瀬戸さん", form_map, max_len, ud2)
        self.assertEqual([s["t"] for s in ann2["segs"]], ["瀬", "戸", "さん"])

    def test_load_user_tokens_derived(self):
        import works
        self.client.post("/api/tokens", json={
            "surface": "だから", "exceptions": ["本だからいい。"]})
        ud = works.load_user_tokens()
        self.assertIn("だから", ud["keep"])
        self.assertEqual(ud["exceptions"].get("本だからいい。"), {"だから"})

    # ---- /api/tokens 入典 / 列表 / 删除 ----

    def test_tokens_add_list_delete(self):
        r = self.client.post("/api/tokens", json={
            "surface": "だから", "exceptions": ["本だからいい。"]})
        self.assertEqual(r.status_code, 200)
        data = self.client.get("/api/tokens").get_json()
        self.assertIn("だから", data["keep"])
        self.assertEqual([e["sentence"] for e in data["split_exceptions"]],
                         ["本だからいい。"])
        # 重复添加不叠加；同 surface 重写会覆盖其例外集
        self.client.post("/api/tokens", json={"surface": "だから", "exceptions": []})
        data = self.client.get("/api/tokens").get_json()
        self.assertEqual(data["keep"].count("だから"), 1)
        self.assertEqual(data["split_exceptions"], [])
        # 删除连带清例外
        self.client.post("/api/tokens", json={
            "surface": "だから", "exceptions": ["本だからいい。"]})
        self.client.delete("/api/tokens", json={"surface": "だから"})
        data = self.client.get("/api/tokens").get_json()
        self.assertNotIn("だから", data["keep"])
        self.assertEqual(data["split_exceptions"], [])

    def test_tokens_validation(self):
        self.assertEqual(self.client.post("/api/tokens", json={"surface": "あ"}).status_code, 400)
        self.assertEqual(self.client.post("/api/tokens", json={"surface": ""}).status_code, 400)
        self.assertEqual(self.client.delete("/api/tokens", json={"surface": ""}).status_code, 400)

    def test_token_review_endpoint(self):
        self.assertEqual(self.client.post("/api/works/nope/token_review",
                     json={"surface": "だから"}).status_code, 404)
        self.assertEqual(self.client.post("/api/works/wk1/token_review",
                     json={"surface": "あ"}).status_code, 400)
        r = self.client.post("/api/works/wk1/token_review", json={"surface": "だから"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["surface"], "だから")
        self.assertIsInstance(body["occurrences"], list)


class TestConjugation(unittest.TestCase):
    """动词活用变形：规则表锚点 + 词库派生 + overrides 段（见 conjugation.py）。"""

    @classmethod
    def setUpClass(cls):
        try:
            import janome  # noqa: F401
            cls.has_janome = True
        except ImportError:
            cls.has_janome = False

    def test_conjugate_per_infl_type(self):
        """规则表每个活用型分支至少一条实测锚点（四张变形全覆盖）。"""
        cases = [
            ("開く", "五段・カ行イ音便", "開いて", "開いた", "開かない", "開きます"),
            ("泳ぐ", "五段・ガ行", "泳いで", "泳いだ", "泳がない", "泳ぎます"),
            ("行く", "五段・カ行促音便", "行って", "行った", "行かない", "行きます"),
            ("待つ", "五段・タ行", "待って", "待った", "待たない", "待ちます"),
            ("取る", "五段・ラ行", "取って", "取った", "取らない", "取ります"),
            ("買う", "五段・ワ行促音便", "買って", "買った", "買わない", "買います"),
            ("死ぬ", "五段・ナ行", "死んで", "死んだ", "死なない", "死にます"),
            ("遊ぶ", "五段・バ行", "遊んで", "遊んだ", "遊ばない", "遊びます"),
            ("飲む", "五段・マ行", "飲んで", "飲んだ", "飲まない", "飲みます"),
            ("話す", "五段・サ行", "話して", "話した", "話さない", "話します"),
            ("下さる", "五段・ラ行特殊", "下さって", "下さった", "下さらない", "下さいます"),
            ("食べる", "一段", "食べて", "食べた", "食べない", "食べます"),
            ("勉強する", "サ変・スル", "勉強して", "勉強した", "勉強しない", "勉強します"),
        ]
        for basic, infl, te, ta, nai, masu in cases:
            self.assertEqual(conjugation.conjugate(basic, infl),
                             {"te": te, "ta": ta, "nai": nai, "masu": masu}, basic)

    def test_conjugate_manual_basics(self):
        """整词特判先于规则表：ある的ない形不是「あらない」、する不是「すって」。"""
        self.assertEqual(conjugation.conjugate("ある", "五段・ラ行"),
                         {"te": "あって", "ta": "あった", "nai": "ない", "masu": "あります"})
        self.assertEqual(conjugation.conjugate("する", "五段・ラ行"),
                         {"te": "して", "ta": "した", "nai": "しない", "masu": "します"})
        # カ変按拍板结论用假名全形作标准答案（汉字形由题型层 alternates 兼容）
        self.assertEqual(conjugation.conjugate("来る", "カ変・来ル"),
                         {"te": "きて", "ta": "きた", "nai": "こない", "masu": "きます"})
        # ある/いる 的汉字形词条：ない形习惯写作假名，不是「有らない/居らない」
        self.assertEqual(conjugation.conjugate("有る", "五段・ラ行"),
                         {"te": "あって", "ta": "あった", "nai": "ない", "masu": "あります"})
        self.assertEqual(conjugation.conjugate("居る", "五段・ラ行"),
                         {"te": "いて", "ta": "いた", "nai": "いない", "masu": "います"})

    def test_conjugate_unknown_rule_returns_none(self):
        """规则表覆盖不到的活用型返回 None（上层报错，不许静默漏词）。"""
        self.assertIsNone(conjugation.conjugate("未知", "五段・谜之行"))
        self.assertIsNone(conjugation.conjugate("", "一段"))

    def _synth_vocab(self):
        return {"lessons": {"lesson_01": {"title": "t", "words": [
            {"id": "w001", "kanji": "開く", "hiragana": "ひらく", "meaning": "开"},
            {"id": "w002", "kanji": "---", "hiragana": "ある", "meaning": "有；在"},
            {"id": "w003", "kanji": "先生", "hiragana": "せんせい", "meaning": "老师"},
            {"id": "w004", "kanji": "働きます", "hiragana": "はたらきます", "meaning": "工作"},
            {"id": "w005", "kanji": "嫌い", "hiragana": "きらい", "meaning": "讨厌"},
        ]}}}

    def test_build_verb_index(self):
        """收录口径：基本形/纯假名收、ます形还原收、名词跳过、janome 误判进清单。"""
        if not self.has_janome:
            self.skipTest("未安装 janome")
        entries, report = conjugation.build_verb_index(self._synth_vocab())
        by_ref = {e["ref"]: e for e in entries}
        self.assertEqual(by_ref["lesson_01:w001"]["source"], "derived")
        self.assertEqual(by_ref["lesson_01:w001"]["forms"]["te"], "開いて")
        self.assertEqual(by_ref["lesson_01:w002"]["source"], "derived")  # 纯假名词也收
        self.assertEqual(by_ref["lesson_01:w002"]["forms"]["nai"], "ない")
        self.assertEqual(by_ref["lesson_01:w004"]["source"], "converted")
        self.assertEqual(by_ref["lesson_01:w004"]["basic"], "働く")  # ます形还原
        self.assertEqual(by_ref["lesson_01:w004"]["forms"]["te"], "働いて")
        # 假名全形：同一套规则换假名词干（ converted 词条读音剥ます反推基本形）
        self.assertEqual(by_ref["lesson_01:w001"]["kana_forms"]["te"], "ひらいて")
        self.assertEqual(by_ref["lesson_01:w001"]["kana_forms"]["nai"], "ひらかない")
        self.assertEqual(by_ref["lesson_01:w004"]["kana_forms"]["te"], "はたらいて")
        # ある 的标准答案本是假名：两套一致属正常（去重是消费方职责）
        self.assertEqual(by_ref["lesson_01:w002"]["kana_forms"]["te"], "あって")
        self.assertNotIn("lesson_01:w003", by_ref)  # 名词不收
        unresolved = {u["written"]: u["reason"] for u in report["unresolved"]}
        self.assertIn("嫌い", unresolved)  # janome 误判：不收但不静默漏
        self.assertEqual(report["errors"], [])

    def test_full_vocab_all_forms_resolved(self):
        """全量不变式：真实词库派生的每条动词三张变形都非空、规则无漏洞。"""
        if not self.has_janome:
            self.skipTest("未安装 janome")
        entries, report = conjugation.build_verb_index(app.load_vocab())
        self.assertGreater(len(entries), 100)
        for e in entries:
            for f in conjugation.FORMS:
                self.assertTrue(e["forms"].get(f), f"{e['ref']} {f} 为空")
                self.assertTrue(e["kana_forms"].get(f),
                                f"{e['ref']} {f} 假名全形为空（MANUAL_KANA_BASIC 缺特判？）")
        self.assertEqual(report["errors"], [])
        # 音便锚点抽查：最容易混的几类
        forms = {e["written"]: e["forms"] for e in entries}
        self.assertEqual(forms.get("行く", {}).get("te"), "行って")   # 促音便
        self.assertEqual(forms.get("開く", {}).get("te"), "開いて")  # イ音便
        self.assertEqual(forms.get("泳ぐ", {}).get("te"), "泳いで")  # ガ行浊化
        self.assertEqual(forms.get("話す", {}).get("te"), "話して")  # サ行
        self.assertEqual(forms.get("食べる", {}).get("masu"), "食べます")
        self.assertEqual(forms.get("下さい", {}).get("masu"), "下さいます")  # ます形特例

    def test_generate_overrides_preserved(self):
        """重派生只重写 verbs/meta：overrides 原样保留并重新套用。"""
        if not self.has_janome:
            self.skipTest("未安装 janome")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp = Path(tmp.name)
        vocab_path = tmp / "vocabulary.json"
        vocab_path.write_text(
            json.dumps(self._synth_vocab(), ensure_ascii=False), encoding="utf-8")
        out, review = tmp / "conjugation.json", tmp / "review.md"
        conjugation.generate(vocab_path, out, review)
        # 人工修正：剔除一条 + 直接指定答案串
        data = json.loads(out.read_text(encoding="utf-8"))
        data["overrides"] = {"lesson_01:w002": {"exclude": True},
                             "lesson_01:w001": {"te": "開きまして"}}
        out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        # 词库变动（新增动词）后再派生：overrides 仍在，exclude/答案串覆盖生效
        v = self._synth_vocab()
        v["lessons"]["lesson_01"]["words"].append(
            {"id": "w006", "kanji": "食べる", "hiragana": "たべる", "meaning": "吃"})
        vocab_path.write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
        data2, _, _ = conjugation.generate(vocab_path, out, review)
        self.assertEqual(data2["overrides"], data["overrides"])  # 原样保留
        by_ref = {e["ref"]: e for e in data2["verbs"]}
        self.assertNotIn("lesson_01:w002", by_ref)  # exclude 生效
        self.assertEqual(by_ref["lesson_01:w001"]["forms"]["te"], "開きまして")
        self.assertEqual(by_ref["lesson_01:w006"]["forms"]["nai"], "食べない")

    def test_alt_forms_and_infl_type_fixes(self):
        """alt_forms 汉字形备选 + janome 活用型误标修正（v2 起标签会上卡片）。"""
        # 只有标准答案本就是假名的条目需要汉字备选：来る 四形齐全，普通动词无
        self.assertEqual(conjugation.alt_forms({"basic": "来る"}),
                         {"te": "来て", "ta": "来た", "nai": "来ない", "masu": "来ます"})
        self.assertEqual(conjugation.alt_forms({"basic": "開く"}), {})
        if not self.has_janome:
            self.skipTest("未安装 janome")
        entries, _ = conjugation.build_verb_index(app.load_vocab())
        by_basic = {e["basic"]: e for e in entries}
        # 居る/する 的 janome 标签是「五段・ラ行」，卡片会一边写 いて 一边讲
        # 「词尾る→って」；改标签后说明与变形一致，假名全形也跟着修好
        self.assertEqual(by_basic["居る"]["infl_type"], "一段")
        self.assertEqual(by_basic["居る"]["kana_forms"]["te"], "いて")  # 原是 いって
        self.assertEqual(by_basic["する"]["infl_type"], "サ変・スル")
        self.assertTrue(conjugation.infl_note(by_basic["居る"]["infl_type"]))
        # 三条来る（含ます形词条还原的）都要带汉字备选
        self.assertEqual(by_basic["来る"]["alt_forms"]["nai"], "来ない")
        self.assertTrue(all(e["alt_forms"].get(f) for e in entries
                            if e["basic"] == "来る" for f in conjugation.FORMS))

    def test_manual_skip_silences_unresolved(self):
        """MANUAL_SKIP 是「拍板完成、彻底静音」：不进清单也不进 unresolved。"""
        if not self.has_janome:
            self.skipTest("未安装 janome")
        entries, report = conjugation.build_verb_index(app.load_vocab())
        written = {u["written"] for u in report["unresolved"]}
        for w in ("ちゅうごく", "とんかつ", "会いましょう", "撮りましょう",
                  "帰りましょう", "使いましょう", "歌いましょう"):
            self.assertNotIn(w, written, f"{w} 已确认不收录，不该再挂清单")
            self.assertNotIn(w, {e["written"] for e in entries})
        # 对照：MANUAL_EXCLUDE_NOTES 只改文案，词条仍挂清单（两套机制的差别）
        self.assertIn("嫌い", written)
        md = conjugation.render_review(entries, report, [])
        self.assertNotIn("とんかつ", md)

    def test_review_lists_unresolved(self):
        """核对清单必须列出未收录的动词性词条（人工拍板依据）。"""
        if not self.has_janome:
            self.skipTest("未安装 janome")
        entries, report = conjugation.build_verb_index(app.load_vocab())
        md = conjugation.render_review(entries, report, [])
        self.assertIn("未收录的动词性词条", md)
        # 清单必须真的带出变形值（曾因嵌套键取值写反渲染成空列）
        self.assertIn("行って", md)
        self.assertIn("開かない", md)
        self.assertIn("ます形", md)  # ます形列（下さいます特例也在）
        self.assertIn("下さいます", md)
        self.assertIn("kana_forms", md)  # 假名全形说明段（三期打字判分 alternates）


class TestConjCard(unittest.TestCase):
    """动词活用卡 /api/conj 与练习反馈的活用提示（api_conj / conj_hint）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {
            "lesson_01": {"title": "t", "words": [
                {"id": "w001", "kanji": "開く", "hiragana": "ひらく", "meaning": "开",
                 "example_ja": "ドアを開きます。", "example_zh": "开门。"},
                {"id": "w002", "kanji": "先生", "hiragana": "せんせい", "meaning": "老师"},
                {"id": "w003", "kanji": "起きます", "hiragana": "おきます", "meaning": "起床"},
            ]},
            "lesson_02": {"title": "t2", "words": [
                {"id": "w101", "kanji": "学生", "hiragana": "がくせい", "meaning": "学生"},
            ]},
        }}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        (tmp / "data").mkdir()
        # 活用数据落盘在 BASE_DIR/data 下（与真实布局同构）：一条 derived（写法即基本
        # 形）+ 一条 converted（多邻国ます形词条），各带 kana_forms 假名全形
        conj = {"meta": {}, "overrides": {}, "verbs": [
            {"ref": "lesson_01:w001", "lesson_id": "lesson_01", "word_id": "w001",
             "written": "開く", "reading": "ひらく", "meaning": "开",
             "basic": "開く", "infl_type": "五段・カ行イ音便", "source": "derived",
             "forms": {"te": "開いて", "ta": "開いた",
                       "nai": "開かない", "masu": "開きます"},
             "kana_forms": {"te": "ひらいて", "ta": "ひらいた",
                            "nai": "ひらかない", "masu": "ひらきます"}},
            {"ref": "lesson_01:w003", "lesson_id": "lesson_01", "word_id": "w003",
             "written": "起きます", "reading": "おきます", "meaning": "起床",
             "basic": "起きる", "infl_type": "一段", "source": "converted",
             "forms": {"te": "起きて", "ta": "起きた",
                       "nai": "起きない", "masu": "起きます"},
             "kana_forms": {"te": "おきて", "ta": "おきた",
                            "nai": "おきない", "masu": "おきます"}},
        ]}
        (tmp / "data" / "conjugation.json").write_text(
            json.dumps(conj, ensure_ascii=False), encoding="utf-8")
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        app._CONJ_CACHE["key"] = None  # 缓存带旧路径的 key：换环境必须先失效
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR = self._old
        app._CONJ_CACHE["key"] = None
        self._tmp.cleanup()

    def test_api_conj_filters_and_payload(self):
        st = self.client.get("/api/conj?lesson=lesson_01").get_json()
        self.assertEqual(len(st["verbs"]), 2)
        v = next(v for v in st["verbs"] if v["ref"] == "lesson_01:w001")
        self.assertEqual(v["ref"], "lesson_01:w001")
        self.assertEqual(v["basic"], "開く")
        self.assertEqual(v["forms"]["te"], "開いて")
        self.assertEqual(v["forms"]["masu"], "開きます")
        self.assertIn("イ音便", v["note"])          # 活用型一句话说明随卡下发
        self.assertEqual(v["example_ja"], "ドアを開きます。")  # 例句从词表现取
        # 课程过滤：没动词的课程返回空列表（前端 alert「没有动词词条」）
        st2 = self.client.get("/api/conj?lesson=lesson_02").get_json()
        self.assertEqual(st2["verbs"], [])

    def test_check_carries_conj_hint_for_verbs_only(self):
        """反馈卡活用提示：动词词条带「て形+ない形」，非动词是 None（前端不渲染）。"""
        ok = self.client.post("/api/check", json={
            "ref": "lesson_01:w001", "mode": "audio_to_kana",
            "answer": "ひらく"}).get_json()
        self.assertEqual(ok["word"]["conj"], {"te": "開いて", "nai": "開かない"})
        noun = self.client.post("/api/check", json={
            "ref": "lesson_01:w002", "mode": "audio_to_kana",
            "answer": "せんせい"}).get_json()
        self.assertIsNone(noun["word"]["conj"])

    def test_nav_and_frontend_wired(self):
        """入口两端都要接上：后端下发 EXTRA 项，前端有页面、分发与请求。"""
        self.assertIn("conj", [it["id"] for it in app.EXTRA_ITEMS["course"]])
        root = Path(app.__file__).resolve().parent
        html = (root / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="conj-screen"', html)
        self.assertIn('id="c-basic"', html)
        appjs = (root / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function startConj", appjs)
        self.assertIn('kind === "conj"', appjs)
        self.assertIn("/api/conj?lesson=", appjs)
        # 读基本形不能用 ref 音频：多邻国词条写法是ます形（起きます），ref 读出来是 okimasu
        self.assertIn("function playConjBasic", appjs)
        self.assertIn("/api/tts/text?text=${encodeURIComponent(c.basic)}", appjs)


class TestConjQuiz(TestConjCard):
    """三期活用题型：独立模式「先选后打」两段式 + 课程轮换 + 掌握度独立统计。"""

    def test_start_two_stage_per_verb(self):
        """每词两题：四选一在前（ui.choice）、打字在后（ui.typing），考同一张变形。"""
        st = self.client.post("/api/start",
                              json={"mode": "conjugation", "lesson": "all",
                                    "count": 10, "schedule": "random"}).get_json()
        # 词库里两个动词都在范围内（course 过滤不含 w002/w101）→ 4 题
        self.assertEqual(len(st["questions"]), 4)
        for i in (0, 2):
            choice, typing = st["questions"][i], st["questions"][i + 1]
            self.assertIs(choice["ui"]["choice"], True)
            self.assertIs(typing["ui"]["typing"], True)
            self.assertEqual(choice["ref"], typing["ref"])  # 同一词
            self.assertEqual(choice["conj_form"], typing["conj_form"])
            self.assertIn(choice["conj_form"], conjugation.FORMS)
            # 选择题四选项互不相同，且包含正确答案（答案可由已知数据推出）
            entry = next(e for e in json.loads(
                (Path(self._tmp.name) / "data" / "conjugation.json")
                .read_text(encoding="utf-8"))["verbs"] if e["ref"] == choice["ref"])
            self.assertEqual(len(set(choice["options"])), 4)
            self.assertIn(entry["forms"][choice["conj_form"]], choice["options"])
            self.assertIn("→", choice["prompt"])  # 题干：基本形（释义）→ 变形名

    def test_check_choice_and_typing_kana_alternate(self):
        """判分：点选标准答案算对；打字全假名（ひらいて）与汉字混形都对。"""
        st = self.client.post("/api/start",
                              json={"mode": "conjugation", "lesson": "all",
                                    "count": 10, "schedule": "random"}).get_json()
        qs = st["questions"]
        # 四选一：从已知词条数据找到正确答案点选（fixtures：w001 開く or w003 起きます）
        choice = next(q for q in qs if q["ui"].get("choice"))
        correct = {"te": "開いて", "ta": "開いた", "nai": "開かない",
                   "masu": "開きます"}[choice["conj_form"]] \
            if choice["ref"].endswith("w001") else \
            {"te": "起きて", "ta": "起きた", "nai": "起きない",
             "masu": "起きます"}[choice["conj_form"]]
        ok = self.client.post("/api/check", json={
            "ref": choice["ref"], "mode": "conjugation",
            "answer": correct, "form": choice["conj_form"]}).get_json()
        self.assertTrue(ok["correct"])
        # 打字：全假名作答（converted 词条 おきて）与标准答案（起きて）都对
        typing = next(q for q in qs if q["ui"].get("typing")
                      and q["ref"].endswith("w003"))
        kana = {"te": "おきて", "ta": "おきた", "nai": "おきない",
                "masu": "おきます"}[typing["conj_form"]]
        ok2 = self.client.post("/api/check", json={
            "ref": typing["ref"], "mode": "conjugation",
            "answer": kana, "form": typing["conj_form"]}).get_json()
        self.assertTrue(ok2["correct"])
        # 答错的错因分类照常下发
        bad = self.client.post("/api/check", json={
            "ref": typing["ref"], "mode": "conjugation",
            "answer": "おきていない", "form": typing["conj_form"]}).get_json()
        self.assertFalse(bad["correct"])
        # form 缺失/非法拒判
        self.assertEqual(self.client.post("/api/check", json={
            "ref": typing["ref"], "mode": "conjugation",
            "answer": "起きて", "form": "te-form"}).status_code, 400)

    def test_check_kuru_kanji_alternate(self):
        """回归：来る 的标准答案是假名（きて），打汉字（来て）也必须算对。

        来る 的 kana_forms 与 forms 完全一致 → 去重后备选为空；判分只做假名折叠，
        不会把汉字归一到假名，所以没有 alt_forms 时「来て/来ない/来ます」全判错。
        """
        tmp = Path(self._tmp.name)
        vocab = json.loads((tmp / "vocabulary.json").read_text(encoding="utf-8"))
        vocab["lessons"]["lesson_01"]["words"].append(
            {"id": "w004", "kanji": "来る", "hiragana": "くる", "meaning": "来"})
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        conj = json.loads((tmp / "data" / "conjugation.json").read_text(encoding="utf-8"))
        kuru = {"ref": "lesson_01:w004", "lesson_id": "lesson_01", "word_id": "w004",
                "written": "来る", "reading": "くる", "meaning": "来",
                "basic": "来る", "infl_type": "カ変・来ル", "source": "derived",
                "forms": {"te": "きて", "ta": "きた", "nai": "こない", "masu": "きます"},
                "kana_forms": {"te": "きて", "ta": "きた", "nai": "こない", "masu": "きます"},
                "alt_forms": {"te": "来て", "ta": "来た", "nai": "来ない", "masu": "来ます"}}
        conj["verbs"].append(kuru)
        (tmp / "data" / "conjugation.json").write_text(
            json.dumps(conj, ensure_ascii=False), encoding="utf-8")
        app._CONJ_CACHE["key"] = None  # 换过文件：按 mtime 失效外再显式清一次

        def check(answer, form):
            return self.client.post("/api/check", json={
                "ref": "lesson_01:w004", "mode": "conjugation",
                "answer": answer, "form": form}).get_json()["correct"]

        for form, answer in (("te", "来て"), ("ta", "来た"), ("nai", "来ない"),
                             ("masu", "来ます")):
            self.assertTrue(check(answer, form), f"{form} {answer}")
        self.assertTrue(check("きて", "te"))       # 假名标准答案照旧
        self.assertFalse(check("来っている", "te"))  # 真错仍然判错

    def test_regular_verb_alternates_unchanged(self):
        """对照：普通五段动词没有汉字备选，假名备选照旧生效（不被 alt 改动影响）。"""
        conj = json.loads((Path(self._tmp.name) / "data" / "conjugation.json")
                          .read_text(encoding="utf-8"))
        entry = next(v for v in conj["verbs"] if v["ref"] == "lesson_01:w001")
        self.assertFalse(entry.get("alt_forms"))  # 标准答案已是汉字混形，无需 alt
        for answer in ("開いて", "ひらいて"):
            ok = self.client.post("/api/check", json={
                "ref": "lesson_01:w001", "mode": "conjugation",
                "answer": answer, "form": "te"}).get_json()
            self.assertTrue(ok["correct"], answer)

    def test_finish_no_mastery_and_no_dedup(self):
        """独立统计口径：交卷不写词条掌握度；同 ref 两行结果都进记录。"""
        st = self.client.post("/api/finish", json={
            "mode": "conjugation", "lesson": "all",
            "results": [{"ref": "lesson_01:w001", "correct": True},
                        {"ref": "lesson_01:w001", "correct": False}],
        }).get_json()
        self.assertEqual(st["stats"]["total"], 2)   # 不去重：两行都保留
        self.assertEqual(st["stats"]["correct"], 1)
        vocab = json.loads(app.VOCAB_PATH.read_text(encoding="utf-8"))
        w = vocab["lessons"]["lesson_01"]["words"][0]
        self.assertIsNone(w.get("mastery"))          # 掌握度未被写
        self.assertIsNone(w.get("last_reviewed"))

    def test_course_rotation_conj_choice_and_mastery_skip(self):
        """课程轮换：新词动词出四选一活用题；交卷时 qtype=conjugation 不写掌握度。"""
        # 把轮换钉在 conjugation 上（否则 kana_to_cn 等永远先命中，够不着活用题）
        old = app.COURSE_RECOGNITION_ROTATION
        app.COURSE_RECOGNITION_ROTATION = ("conjugation",)
        self.addCleanup(setattr, app, "COURSE_RECOGNITION_ROTATION", old)
        st = self.client.post("/api/start", json={
            "mode": "course", "lesson": "lesson_01", "count": 10}).get_json()
        conj_qs = [q for q in st["questions"] if q.get("mode") == "conjugation"]
        self.assertTrue(conj_qs)
        # 轮换只认写法即基本形的词条：起きます（converted）不考活用，開く考
        self.assertTrue(all(q["ref"] == "lesson_01:w001" for q in conj_qs))
        self.assertTrue(all(q["ui"].get("choice") for q in conj_qs))  # 课程内只出四选一
        correct = {"te": "開いて", "ta": "開いた", "nai": "開かない",
                   "masu": "開きます"}[conj_qs[0]["conj_form"]]
        ok = self.client.post("/api/check", json={
            "ref": conj_qs[0]["ref"], "mode": conj_qs[0]["mode"],
            "answer": correct, "form": conj_qs[0]["conj_form"]}).get_json()
        self.assertTrue(ok["correct"])
        fin = self.client.post("/api/finish", json={
            "mode": "course", "lesson": "lesson_01",
            "results": [{"ref": "lesson_01:w001", "correct": False,
                         "qtype": "conjugation"}]}).get_json()
        self.assertTrue(fin["saved"])
        vocab = json.loads(app.VOCAB_PATH.read_text(encoding="utf-8"))
        w = vocab["lessons"]["lesson_01"]["words"][0]
        self.assertIsNone(w.get("mastery"))  # 活用题答错也不拉低词条掌握度

    def test_conj_mode_registered_and_frontend_wired(self):
        """新模式自动进 /api/config；前端判分请求带 form、提示文案就位。"""
        cfg = self.client.get("/api/config").get_json()
        self.assertIn("conjugation", [m["id"] for m in cfg["modes"]])
        appjs = (Path(app.__file__).resolve().parent
                 / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("form: q.conj_form", appjs)   # 三处 /api/check 都带变形名
        self.assertIn("conjugation:", appjs)        # CONFIG_HINTS 提示文案


class TestVoiceParse(unittest.TestCase):
    """音声字幕 / 台本解析：时间戳、三种字幕格式、演出指示清洗、切句与时间映射。"""

    VTT = """WEBVTT
Kind: captions

NOTE 这一段是注释，不该进正文

STYLE
::cue { color: white }

1
00:00:01.000 --> 00:00:04.000 align:start position:10%
<v エムデン>こんばんは、ご主人

2
00:00:04.000 --> 00:00:07.000
さま。

00:00:10.000 --> 00:00:13.000
今日も一日お疲れさまでした。
"""

    def test_parse_timestamp_variants(self):
        import voice
        self.assertAlmostEqual(voice.parse_timestamp("00:03:21.480"), 201.48)
        self.assertAlmostEqual(voice.parse_timestamp("03:21.480"), 201.48)
        self.assertAlmostEqual(voice.parse_timestamp("00:03:21,480"), 201.48)
        # ass 是百分秒（两位小数）：不能当成 4.8 毫秒
        self.assertAlmostEqual(voice.parse_timestamp("0:00:01.05"), 1.05)
        self.assertAlmostEqual(voice.parse_timestamp("1:02:03.5"), 3723.5)
        for bad in ("", "1.5", "--:--", "00:00"):
            self.assertIsNone(voice.parse_timestamp(bad), bad)

    def test_format_timestamp(self):
        import voice
        self.assertEqual(voice.format_timestamp(0), "00:00")
        self.assertEqual(voice.format_timestamp(201.9), "03:21")
        self.assertEqual(voice.format_timestamp(3725), "1:02:05")

    def test_parse_ffprobe_duration(self):
        import voice
        self.assertAlmostEqual(voice.parse_ffprobe_duration("duration=735.02\n"), 735.02)
        self.assertIsNone(voice.parse_ffprobe_duration("n/a"))
        self.assertIsNone(voice.parse_ffprobe_duration(""))

    def test_webvtt_skips_header_note_style(self):
        import voice
        cues = voice.parse_webvtt(self.VTT)
        self.assertEqual(len(cues), 3)
        self.assertAlmostEqual(cues[0].start, 1.0)
        self.assertAlmostEqual(cues[0].end, 4.0)
        self.assertIn("こんばんは", cues[0].text)
        # 头 / NOTE / STYLE 都不该变成 cue
        self.assertFalse(any("字幕" in c.text or "cue {" in c.text for c in cues))

    def test_srt_uses_comma_timestamps(self):
        import voice
        srt = ("1\n00:00:01,000 --> 00:00:02,500\nおはよう。\n\n"
               "2\n00:00:02,500 --> 00:00:04,000\nおやすみ。\n")
        cues = voice.parse_srt(srt)
        self.assertEqual([c.text for c in cues], ["おはよう。", "おやすみ。"])
        self.assertAlmostEqual(cues[1].start, 2.5)

    def test_ass_uses_format_columns_and_text_may_contain_commas(self):
        """ass 的列序由 Format 行决定，Text 里本身含逗号也不能切错。"""
        import voice
        ass = ("[Script Info]\nTitle: x\n\n[Events]\n"
               "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
               "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,{\\an8}あ、そう。\n"
               "Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,二行目\\Nつづき\n")
        cues = voice.parse_ass(ass)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0].start, 1.0)
        self.assertEqual(cues[0].text, "あ、そう。")
        self.assertEqual(len(cues[1].text.split("\n")), 1)   # \N 是转义不是换行
        self.assertIn("二行目 つづき", cues[1].text)

    def test_parse_subtitles_sniffs_when_extension_unknown(self):
        import voice
        self.assertEqual(len(voice.parse_subtitles(self.VTT, "x.unknown")), 3)
        self.assertEqual(
            len(voice.parse_subtitles("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nあ\n")), 1)
        ass = "[Events]\nFormat: Start, End, Text\nDialogue: 0:00:01.00,0:00:02.00,あ\n"
        self.assertEqual(len(voice.parse_subtitles(ass, "")), 1)
        # 认不出来时按 srt 兜底（有没有 --> 决定成败）
        self.assertEqual(
            len(voice.parse_subtitles("1\n00:00:01,000 --> 00:00:02,000\nあ\n", "")), 1)

    def test_strip_markup_removes_tags_and_entities(self):
        import voice
        self.assertEqual(
            voice.strip_markup("<v エムデン>こんばんは</v>"), "こんばんは")
        self.assertEqual(voice.strip_markup("<c.yellow>警告</c>"), "警告")
        self.assertEqual(voice.strip_markup("A&amp;B &lt;x&gt;"), "A&B <x>")
        # 卡拉OK式内联时间戳整块去掉
        self.assertEqual(voice.strip_markup("<00:00:01.000>あ<00:00:02.000>い"), "あい")
        self.assertEqual(voice.strip_markup("{\\pos(10,20)}あ{\\an8}"), "あ")

    def test_strip_directions_is_conservative(self):
        """行内演出指示才剥：括号短 + 命中关键词；台词一个字都不能少。"""
        import voice
        self.assertEqual(voice.strip_directions("（囁き）こんばんは"), "こんばんは")
        self.assertEqual(voice.strip_directions("（心の声）俺は諦めない"), "俺は諦めない")
        self.assertEqual(voice.strip_directions("（SE）ドアの音（ため息）"), "ドアの音")
        # 括号里不是指示关键词 → 原样保留（可能是台词的一部分）
        self.assertEqual(voice.strip_directions("（とても大事）そう思う"),
                         "（とても大事）そう思う")
        # 括号太长就不像指示了，不剥
        long_note = "（ここは長い説明なので指示とはみなさずそのまま残す文章です）"
        self.assertEqual(voice.strip_directions(long_note + "はい"), long_note + "はい")
        # 用户追加的关键词也生效
        self.assertEqual(voice.strip_directions("（うなずく）はい", ("うなずく",)), "はい")

    def test_drop_script_lines_reports_what_it_threw_away(self):
        import voice
        text = ("SE：ドアの音\n─────\n12\nこんにちは\n（囁き）おはよう\n"
                "♪♪\n………\n■トラック1\n今日はいい天気ですね\n")
        kept, dropped = voice.drop_script_lines(text)
        self.assertEqual(kept, ["こんにちは", "おはよう", "■トラック1",
                                "今日はいい天気ですね"])
        # 行内指示是「洗掉」不是「丢行」：行还在，括号没了
        self.assertNotIn("（囁き）おはよう", kept)
        # 丢弃的原文要留下来给人核对，不静默丢内容
        for line in ("SE：ドアの音", "─────", "12", "♪♪", "………"):
            self.assertIn(line, dropped)

    def test_cues_to_sentences_merges_across_cues_and_maps_times(self):
        """一句话横跨两条 cue：合并后时间取并集，不是只取第一条。"""
        import voice
        sents = voice.cues_to_sentences(voice.parse_webvtt(self.VTT))
        self.assertEqual([s["text"] for s in sents],
                         ["こんばんは、ご主人さま。", "今日も一日お疲れさまでした。"])
        self.assertAlmostEqual(sents[0]["t0"], 1.0)
        self.assertAlmostEqual(sents[0]["t1"], 7.0)   # 覆盖到第二条 cue 的结束
        self.assertAlmostEqual(sents[1]["t0"], 10.0)
        # 第二句前有 6 秒停顿（7→10 之外还差 3 秒），gap 要算出来
        self.assertAlmostEqual(sents[1]["gap"], 3.0)

    def test_cues_to_sentences_splits_two_sentences_in_one_cue(self):
        import voice
        cues = [voice.Cue(0.0, 4.0, "おはよう。今日もいい天気ですね。")]
        sents = voice.cues_to_sentences(cues)
        self.assertEqual([s["text"] for s in sents],
                         ["おはよう。", "今日もいい天気ですね。"])
        self.assertAlmostEqual(sents[0]["t0"], 0.0)
        self.assertAlmostEqual(sents[1]["t1"], 4.0)
        # 一条 cue 里两句共享它的时长：按长度切，顺序即时间顺序、首尾相接
        self.assertAlmostEqual(sents[0]["t1"], sents[1]["t0"])
        self.assertAlmostEqual(sents[0]["t1"], 4.0 * 5 / 16)   # 5 字 : 11 字

    def test_lines_to_sentences_has_no_times(self):
        import voice
        sents = voice.lines_to_sentences(["こんばんは。", "（囁き）おやすみなさい。"])
        self.assertEqual([s["text"] for s in sents], ["こんばんは。", "おやすみなさい。"])
        self.assertTrue(all(s["t0"] is None for s in sents))

    def test_speaker_labels_split_cue_and_share_its_time(self):
        """一条 cue 里两个人的台词（`【ミア】…【アヤ】…`）：按说话人标签切开，
        时长按台词长度分给各句——不切就糊成一句、时间也只能整条共用。"""
        import voice
        cues = [voice.Cue(10.0, 16.0, "【ミア】おはよう【アヤ】おはようございます")]
        sents = voice.cues_to_sentences(cues)
        self.assertEqual([s["text"] for s in sents],
                         ["【ミア】おはよう", "【アヤ】おはようございます"])
        self.assertAlmostEqual(sents[0]["t0"], 10.0)
        self.assertAlmostEqual(sents[1]["t1"], 16.0)
        self.assertAlmostEqual(sents[0]["t1"], sents[1]["t0"])   # 首尾相接不重叠
        # 台词长得多的那一句分到的时间也多（按字符长度分）
        self.assertGreater(sents[1]["t1"] - sents[1]["t0"],
                           sents[0]["t1"] - sents[0]["t0"])
        # 标签换一种写法也认（［］），且原样留在句子里
        cues = [voice.Cue(0.0, 2.0, "［ミア］おはよう")]
        self.assertEqual([s["text"] for s in voice.cues_to_sentences(cues)],
                         ["［ミア］おはよう"])

    def test_speaker_label_not_taken_inside_quotes_or_as_direction(self):
        """方括号词只在「引号外、且不是演出记号」时才算说话人：宁可漏切别切碎。"""
        import voice
        sents = voice.cues_to_sentences([voice.Cue(0.0, 4.0, "「これは【重要】だ」")])
        self.assertEqual([s["text"] for s in sents], ["「これは【重要】だ」"])
        sents = voice.cues_to_sentences([voice.Cue(0.0, 2.0, "[SE]ドアの音")])
        self.assertEqual([s["text"] for s in sents], ["[SE]ドアの音"])

    def test_speaker_labels_split_script_lines_without_times(self):
        """台本一行里挤两个人的台词照切；行与行之间仍不合并。"""
        import voice
        sents = voice.lines_to_sentences([
            "【ミア】おはよう【アヤ】おはようございます",
            "（囁き）またね",
        ])
        self.assertEqual([s["text"] for s in sents],
                         ["【ミア】おはよう", "【アヤ】おはようございます", "またね"])
        self.assertTrue(all(s["t0"] is None and s["t1"] is None for s in sents))
        # 同一个说话人跨 cue 重复标签：各自成句（cue 边界就是新的一句）
        cues = [voice.Cue(0.0, 1.0, "【ミア】今日は"),
                voice.Cue(1.0, 2.0, "【ミア】いい天気だね")]
        self.assertEqual([s["text"] for s in voice.cues_to_sentences(cues)],
                         ["【ミア】今日は", "【ミア】いい天気だね"])
        self.assertAlmostEqual(voice.cues_to_sentences(cues)[1]["gap"], 0.0)

    def test_group_paragraphs_breaks_on_gap_and_on_length(self):
        import voice
        sents = [{"text": "あ。", "t0": 0.0, "t1": 1.0, "gap": 0.0},
                 {"text": "い。", "t0": 9.0, "t1": 10.0, "gap": 8.0}]
        self.assertEqual(len(voice.group_paragraphs(sents)), 2)   # 长停顿断段
        many = [{"text": "か。", "t0": i, "t1": i + 1, "gap": 0.0} for i in range(9)]
        paras = voice.group_paragraphs(many)
        self.assertEqual(sum(len(p) for p in paras), 9)
        self.assertTrue(all(len(p) <= voice.PARA_MAX_SENTS for p in paras))

    def test_build_chapter_shape_and_flat_times(self):
        import voice
        sents = [{"text": "あ。", "t0": 0.0, "t1": 1.0, "gap": 0.0},
                 {"text": "い。", "t0": 1.0, "t1": 2.0, "gap": 0.0}]
        ch = voice.build_chapter("track1", sents)
        self.assertEqual(ch["paras"], [["あ。", "い。"]])
        self.assertEqual(ch["times"], [[0.0, 1.0], [1.0, 2.0]])
        # 扁平 times 与 paras 展开同长才算数
        self.assertEqual(voice.flat_times(ch["times"], ch["paras"]), ch["times"])
        self.assertIsNone(voice.flat_times([[0, 1]], [["あ。", "い。"]]))
        self.assertIsNone(voice.flat_times(None, ch["paras"]))
        # 没有时间轴的台本不写 times 键
        plain = voice.build_chapter("台本", [{"text": "あ。", "t0": None,
                                              "t1": None, "gap": 0.0}])
        self.assertNotIn("times", plain)


class TestVoiceImport(unittest.TestCase):
    """音声作品导入：音轨与字幕/台本自动配对、去重、落盘结构。"""

    def setUp(self):
        import works
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._old = (works.WORKS_DIR,)
        works.WORKS_DIR = self.tmp / "works"
        works.WORKS_DIR.mkdir(parents=True)

    def tearDown(self):
        import works
        (works.WORKS_DIR,) = self._old
        self._tmp.cleanup()

    def _write(self, rel, text):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nこんばんは。\n\n" \
          "00:00:03.000 --> 00:00:06.000\nおやすみなさい。\n"

    def _make_work_tree(self):
        """造一个像 DLsite 作品那样的目录：mp3 + 同名 .vtt，另有 wav 冗余。"""
        self._write("RJ99999999/track1.mp3", "fake-mp3")
        self._write("RJ99999999/track1.mp3.vtt", self.VTT)
        self._write("RJ99999999/track2.mp3", "fake-mp3-2")
        self._write("RJ99999999/track2.vtt", self.VTT)
        self._write("RJ99999999/wav/track1.wav", "fake-wav")
        self._write("RJ99999999/wav/track2.wav", "fake-wav")

    def test_discover_pairs_subtitles_and_dedupes_wav(self):
        import import_voice
        self._make_work_tree()
        tracks = import_voice.discover(self.tmp / "RJ99999999")
        # 同主名的 wav 被丢掉（mp3 优先），且只认到两条轨
        self.assertEqual([t.label for t in tracks], ["track1", "track2"])
        self.assertTrue(all(t.audio.suffix == ".mp3" for t in tracks))
        self.assertEqual(tracks[0].subtitle.name, "track1.mp3.vtt")
        self.assertEqual(tracks[1].subtitle.name, "track2.vtt")

    def test_discover_pairs_script_in_script_dir(self):
        """台本放在 Script 子目录里，靠主名配对（音轨与台本主名要一致）。"""
        import import_voice
        self._write("工作/track1　第一轨.mp3", "a")
        self._write("工作/Script/track1　第一轨.txt",
                    "こんばんは。\n（囁き）おやすみ。\n")
        # 主名对不上的台本不该被硬配上去
        self._write("工作/Script/别的文件.txt", "無関係。\n")
        tracks = import_voice.discover(self.tmp / "工作")
        self.assertEqual(len(tracks), 1)
        self.assertIsNone(tracks[0].subtitle)
        self.assertIsNotNone(tracks[0].script)
        self.assertIn("Script", str(tracks[0].script))

    def test_discover_natural_order(self):
        import import_voice
        for n in ("track2", "track10", "track1"):
            self._write(f"自然/{n}.mp3", "x")
        tracks = import_voice.discover(self.tmp / "自然")
        self.assertEqual([t.label for t in tracks], ["track1", "track2", "track10"])

    def test_import_writes_voice_work_with_times(self):
        import import_voice, works
        self._make_work_tree()
        meta = import_voice.import_voice(root=self.tmp / "RJ99999999", probe=False)
        self.assertEqual(meta["id"], "rj99999999")   # RJ 号当作品 id
        self.assertEqual(meta["type"], "voice")
        self.assertEqual(len(meta["chapters"]), 2)
        ch = meta["chapters"][0]
        self.assertTrue(ch["timed"])
        self.assertTrue(Path(ch["audio"]).is_absolute())   # 只登记路径，不复制文件
        self.assertTrue(Path(ch["audio"]).exists())
        raw = json.loads((works.WORKS_DIR / meta["id"] / "content/ch00.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(raw["paras"], [["こんばんは。", "おやすみなさい。"]])
        self.assertEqual(raw["times"], [[1.0, 3.0], [3.0, 6.0]])
        # 作品能出现在阅读器的作品列表里
        self.assertIn(meta["id"], [m["id"] for m in works.list_works()])

    def test_import_script_only_has_no_times(self):
        import import_voice, works
        self._write("台本作品/track1.mp3", "x")
        self._write("台本作品/track1.txt",
                    "こんばんは。\nSE：ドアの音\nおやすみなさい。\n")
        meta = import_voice.import_voice(root=self.tmp / "台本作品", probe=False)
        self.assertFalse(meta["chapters"][0]["timed"])
        wdir = works.WORKS_DIR / meta["id"]
        raw = json.loads((wdir / "content/ch00.json").read_text(encoding="utf-8"))
        self.assertNotIn("times", raw)
        self.assertEqual(raw["paras"], [["こんばんは。", "おやすみなさい。"]])
        # 清洗丢掉的行要留档，供人核对规则够不够
        self.assertIn("SE：ドアの音", (wdir / "dropped.txt").read_text(encoding="utf-8"))

    def test_import_dry_run_writes_nothing(self):
        import import_voice, works
        self._make_work_tree()
        self.assertIsNone(import_voice.import_voice(
            root=self.tmp / "RJ99999999", probe=False, dry_run=True))
        self.assertFalse((works.WORKS_DIR / "rj99999999").exists())

    def test_reimport_clears_stale_chapters(self):
        import import_voice, works
        self._make_work_tree()
        meta = import_voice.import_voice(root=self.tmp / "RJ99999999", probe=False)
        cdir = works.WORKS_DIR / meta["id"] / "content"
        (cdir / "ch09.json").write_text("{}", encoding="utf-8")   # 假装上次导多了
        import_voice.import_voice(root=self.tmp / "RJ99999999", probe=False)
        self.assertFalse((cdir / "ch09.json").exists())

    def test_import_without_any_text_explains_align(self):
        import import_voice
        self._write("只有音频/track1.mp3", "x")
        with self.assertRaises(SystemExit) as cm:
            import_voice.import_voice(root=self.tmp / "只有音频", probe=False)
        self.assertIn("--align", str(cm.exception))

    def test_include_dirs_picks_one_version(self):
        """同一作品给了「含SE / 无SE」两版音轨：--dir 指一版，别全导进去。"""
        import import_voice
        self._write("双版本/含SE/track1.mp3", "x")
        self._write("双版本/含SE/track1.vtt", self.VTT)
        self._write("双版本/无SE/track1.mp3", "x")
        self._write("双版本/无SE/track1.vtt", self.VTT)
        all_tracks = import_voice.discover(self.tmp / "双版本")
        self.assertEqual(len(all_tracks), 2)   # 同名两版会被都收进来
        picked = import_voice.discover(self.tmp / "双版本", include_dirs=["无SE"])
        self.assertEqual(len(picked), 1)
        self.assertIn("无SE", str(picked[0].audio))


class TestVoiceAlign(unittest.TestCase):
    """ASR 强制对齐：文本以台本为准，时间来自 whisper；插入/缺失不许错位。"""

    def _sents(self, texts):
        return [{"text": t, "t0": None, "t1": None, "gap": 0.0} for t in texts]

    def test_norm_for_align(self):
        import voice_align
        self.assertEqual(voice_align.norm_for_align("カタカナ、。！"), "かたかな")
        self.assertEqual(voice_align.norm_for_align("ＡＢＣ １２３"), "ABC123")
        self.assertEqual(voice_align.norm_for_align(""), "")

    def test_align_perfect_match(self):
        import voice_align
        sents = self._sents(["こんばんは。", "おやすみなさい。"])
        segs = [{"text": "こんばんは", "t0": 1.0, "t1": 3.0},
                {"text": "おやすみなさい", "t0": 3.0, "t1": 6.0}]
        out = voice_align.align(sents, segs)
        self.assertEqual([(s["t0"], s["t1"]) for s in out], [(1.0, 3.0), (3.0, 6.0)])

    def test_align_survives_asr_insertion(self):
        """ASR 多出一整段（片头/自由谈话）时，后面的句子不能整体错位。"""
        import voice_align
        sents = self._sents(["こんばんは。", "おやすみなさい。"])
        segs = [{"text": "ご視聴ありがとうございました", "t0": 0.0, "t1": 2.0},
                {"text": "こんばんは", "t0": 10.0, "t1": 12.0},
                {"text": "おやすみなさい", "t0": 20.0, "t1": 23.0}]
        out = voice_align.align(sents, segs)
        self.assertEqual((out[0]["t0"], out[0]["t1"]), (10.0, 12.0))
        self.assertEqual((out[1]["t0"], out[1]["t1"]), (20.0, 23.0))

    def test_align_survives_asr_drop(self):
        """ASR 漏掉一整句：漏掉的那句不给时间，后面的照旧对上。"""
        import voice_align
        sents = self._sents(["こんばんは。", "（聞き取れない早口）", "おやすみなさい。"])
        segs = [{"text": "こんばんは", "t0": 1.0, "t1": 3.0},
                {"text": "おやすみなさい", "t0": 8.0, "t1": 11.0}]
        out = voice_align.align(sents, segs)
        self.assertIsNotNone(out[0]["t0"])
        self.assertEqual((out[2]["t0"], out[2]["t1"]), (8.0, 11.0))

    def test_align_leaves_low_match_rate_alone(self):
        """对不上的句子保持 None——错位的时间轴比没有更糟。"""
        import voice_align
        sents = self._sents(["まったく別のことを言っている長い長い台詞です。"])
        segs = [{"text": "ありがとう", "t0": 1.0, "t1": 3.0}]
        out = voice_align.align(sents, segs)
        self.assertIsNone(out[0]["t0"])

    def test_align_empty_inputs(self):
        import voice_align
        self.assertEqual(voice_align.align([], [{"text": "あ", "t0": 0, "t1": 1}]), [])
        sents = self._sents(["あ。"])
        self.assertIsNone(voice_align.align(sents, [])[0]["t0"])

    def test_transcribe_accepts_injected_transcriber(self):
        """转写可注入替身：不装 faster-whisper 也能测整条链路。"""
        import voice_align
        def fake(path):
            return [{"text": "こんばんは", "t0": 0.0, "t1": 2.0},
                    {"text": "   ", "t0": 2.0, "t1": 3.0},        # 空白要滤掉
                    {"text": "おやすみ", "t0": 3.0, "t1": 3.0}]   # 时长为 0 也滤掉
        out = voice_align.transcribe("x.mp3", transcriber=fake)
        self.assertEqual([s["text"] for s in out], ["こんばんは"])

    def test_align_report(self):
        import voice_align
        self.assertEqual(voice_align.align_report([]), "0 句")
        sents = [{"text": "あ", "t0": 1.0}, {"text": "い", "t0": None}]
        self.assertIn("1/2", voice_align.align_report(sents))


class TestVoiceReader(unittest.TestCase):
    """音声作品接进阅读器：章节音轨下发（支持 Range）+ 句子时间轴进载荷。"""

    def setUp(self):
        import works
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "第1课", "words": [
            {"id": "w001", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗", "mastery": 3},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, works.WORKS_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        works.WORKS_DIR = tmp / "works"
        works._profile_cache.clear()
        works._sent_cache.clear()
        # 真造一个小「音轨」：send_file 要读到文件并支持 Range
        self.audio = tmp / "track1.mp3"
        self.audio.write_bytes(b"ID3fake-audio-bytes" * 4)

        wdir = works.WORKS_DIR / "rjx"
        (wdir / "content").mkdir(parents=True)
        (wdir / "work.json").write_text(json.dumps({
            "id": "rjx", "title": "测试音声", "author": "", "type": "voice",
            "created": date.today().isoformat(),
            "chapters": [
                {"id": 0, "title": "track1", "file": "ch00.json", "sentences": 2,
                 "audio": str(self.audio), "timed": True},
                # 第二轨故意没有 audio：验证 404 与「句子退回 TTS」两条路
                {"id": 1, "title": "track2", "file": "ch01.json", "sentences": 1,
                 "timed": False},
            ]}, ensure_ascii=False), encoding="utf-8")
        (wdir / "content/ch00.json").write_text(json.dumps({
            "title": "track1",
            "paras": [["犬がいる。", "こんばんは。"]],
            "times": [[1.0, 3.0], [3.0, 6.5]],
        }, ensure_ascii=False), encoding="utf-8")
        (wdir / "content/ch01.json").write_text(json.dumps({
            "title": "track2", "paras": [["台本だけ。"]],
        }, ensure_ascii=False), encoding="utf-8")
        self.client = app.app.test_client()

    def tearDown(self):
        import works
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, works.WORKS_DIR = self._old
        works._profile_cache.clear()
        works._sent_cache.clear()
        self._tmp.cleanup()

    def test_work_list_marks_voice_and_timed_chapters(self):
        data = self.client.get("/api/works").get_json()
        w = data["works"][0]
        self.assertTrue(w["voice"])
        self.assertEqual([c["timed"] for c in w["chapters"]], [True, False])

    def test_chapter_sentences_carry_times(self):
        data = self.client.get("/api/works/rjx/chapter/0").get_json()
        ch = data["chapter"]
        self.assertTrue(ch["timed"])
        self.assertEqual(ch["audio"], "/api/works/rjx/audio/0")
        sents = [s for p in data["paras"] for s in p]
        self.assertEqual([(s["t0"], s["t1"]) for s in sents], [(1.0, 3.0), (3.0, 6.5)])
        # 标注链路不受影响：词块照旧拼得回原文
        self.assertEqual("".join(x["t"] for x in sents[0]["segs"]), "犬がいる。")

    def test_chapter_without_audio_or_times(self):
        data = self.client.get("/api/works/rjx/chapter/1").get_json()
        self.assertFalse(data["chapter"]["timed"])
        self.assertEqual(data["chapter"]["audio"], "")
        self.assertNotIn("t0", data["paras"][0][0])   # 没时间轴就不下发 t0

    def test_audio_endpoint_serves_file_and_supports_range(self):
        full = self.client.get("/api/works/rjx/audio/0")
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.data, self.audio.read_bytes())
        # Range 是「点句跳到某秒」的前提：没有它就只能整轨重下
        part = self.client.get("/api/works/rjx/audio/0",
                               headers={"Range": "bytes=0-3"})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part.data, self.audio.read_bytes()[:4])

    def test_audio_endpoint_404s(self):
        self.assertEqual(self.client.get("/api/works/nope/audio/0").status_code, 404)
        self.assertEqual(self.client.get("/api/works/rjx/audio/9").status_code, 404)
        # 登记了 audio 但文件被移走：明确 404，不是 500
        self.assertEqual(self.client.get("/api/works/rjx/audio/1").status_code, 404)
        self.audio.unlink()
        self.assertEqual(self.client.get("/api/works/rjx/audio/0").status_code, 404)

    def test_frontend_wired(self):
        base = Path(app.__file__).resolve().parent
        js = (base / "static" / "reader.js").read_text(encoding="utf-8")
        html = (base / "templates" / "reader.html").read_text(encoding="utf-8")
        css = (base / "static" / "reader.css").read_text(encoding="utf-8")
        for el in ("media-bar", "media-play", "media-seek", "media-follow",
                   "media-loop", "media-rate"):
            self.assertIn(f'id="{el}"', html, el)
        self.assertIn("playSentenceAudio", js)
        self.assertIn("state.sentNodes.push", js)     # 句子节点登记，供「播到哪句」定位
        self.assertIn("media.stopAt", js)             # 单句播到句尾停
        self.assertIn('classList.add("play")', js)    # ▶ 常驻可见
        self.assertIn(".sent.playing", css)
        # 「回到当前句」：人手滚开就停跟随、浮出箭头（朝上/朝下跟着当前句在屏上
        # 还是屏下翻转）；点它跳回去并恢复跟随；自家 scrollIntoView 不算用户滚动
        self.assertIn('id="follow-btn"', html)
        for needle in ("backToPlayingSentence", "setDetached(", "onUserScroll",
                       "media.scrollGuard", "onScrollMaybeUser",
                       '"↑"', "inComfortZone"):
            self.assertIn(needle, js, needle)
        self.assertIn(".follow-btn", css)
        # 同章重渲染（切开关 / 挖矿加词 / 改句文本）不打断正在播的音轨
        for needle in ("mediaKey", "media.chapterKey", "restoreMedia"):
            self.assertIn(needle, js, needle)


class TestDirUploadPaths(unittest.TestCase):
    """目录上传：相对路径归一/清洗 + 流式落盘 + 大小上限（越界一律不落盘）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.stage = Path(self._tmp.name) / "stage"

    def tearDown(self):
        self._tmp.cleanup()

    def test_safe_rel_path_normalizes(self):
        self.assertEqual(app._safe_rel_path("a/b/c.txt"), "a/b/c.txt")
        self.assertEqual(app._safe_rel_path(r"a\b\c.mp3"), "a/b/c.mp3")
        self.assertEqual(app._safe_rel_path("/etc/passwd"), "etc/passwd")

    def test_safe_rel_path_drops_dots_and_controls(self):
        # `..` / `.` 片段直接剔除——目录上传不许越出作品目录
        self.assertEqual(app._safe_rel_path("../evil.txt"), "evil.txt")
        self.assertEqual(app._safe_rel_path("a/../../b.txt"), "a/b.txt")
        self.assertEqual(app._safe_rel_path("a\x00b\x1fc.mp3"), "a_b_c.mp3")
        self.assertEqual(app._safe_rel_path(""), "")

    def test_materialize_rebuilds_structure(self):
        def stream(text):
            return io.BytesIO(text.encode("utf-8"))
        total = app._materialize_dir_upload([
            ("作品/track1.mp3.vtt", stream("WEBVTT\n")),
            ("作品/Script/track1.txt", stream("こんばんは。\n")),
        ], self.stage)
        self.assertEqual(total, len("WEBVTT\n".encode("utf-8"))
                          + len("こんばんは。\n".encode("utf-8")))
        self.assertEqual((self.stage / "作品/track1.mp3.vtt").read_text(
            encoding="utf-8"), "WEBVTT\n")
        self.assertEqual((self.stage / "作品/Script/track1.txt").read_text(
            encoding="utf-8"), "こんばんは。\n")

    def test_materialize_never_escapes_stage(self):
        # 再坏的相对路径，落盘后都必须待在 stage 里（.. 段被剔掉）
        app._materialize_dir_upload([
            ("../../outside.txt", io.BytesIO(b"x")),
            (r"..\..\..\escape.mp3", io.BytesIO(b"y")),
            ("/abs/root.txt", io.BytesIO(b"z")),
        ], self.stage)
        # 临时目录顶层只有 stage 一个条目，文件都收在它下面
        self.assertEqual([p.name for p in Path(self._tmp.name).iterdir()],
                         ["stage"])
        for p in self.stage.rglob("*"):
            if p.is_file():
                p.relative_to(self.stage.resolve())   # 能算相对路径＝没越界

    def test_materialize_oversize_rejected(self):
        with self.assertRaises(ValueError):
            app._materialize_dir_upload([("big.mp3", io.BytesIO(b"12345"))],
                                        self.stage, max_bytes=4)


class TestServerAsrBridge(unittest.TestCase):
    """校验白名单 + nvidia-smi 解析 + 服务器段加载（全部离线）。"""

    class _R:  # subprocess.CompletedProcess 替身
        def __init__(self, rc=0, stdout="", stderr=""):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = stderr

    def test_validate_engine(self):
        from server_asr_bridge import validate_engine, ServerAsrError
        self.assertEqual(validate_engine("QWEN3"), "qwen3")
        self.assertEqual(validate_engine(" largev3 "), "largev3")
        with self.assertRaises(ServerAsrError):
            validate_engine("gpt-4")

    def test_validate_gpu(self):
        from server_asr_bridge import validate_gpu, ServerAsrError
        self.assertEqual(validate_gpu(""), "0")          # 空 → 默认 0
        self.assertEqual(validate_gpu("0,3"), "0,3")     # 多卡逗号拼接
        with self.assertRaises(ServerAsrError):
            validate_gpu("0;a")
        with self.assertRaises(ServerAsrError):
            validate_gpu("1.5")

    def test_gpu_status_parsing(self):
        from server_asr_bridge import ServerAsr
        out = ("0, NVIDIA GeForce RTX 4090, 65536, 6144, 12\n"
               "1, NVIDIA GeForce RTX 4090, 65536, 32768, 87\n")
        r = self._R(stdout=out)
        b = ServerAsr(runner=lambda args, timeout=120: r)
        gpus = b.gpu_status()
        self.assertEqual([g["index"] for g in gpus], [0, 1])
        self.assertEqual(gpus[0]["mem_total"], 64.0)     # MiB → GiB
        self.assertEqual(gpus[0]["mem_used"], 6.0)
        self.assertEqual(gpus[1]["util"], 87)

    def test_gpu_status_command_failure(self):
        from server_asr_bridge import ServerAsr, ServerAsrError
        b = ServerAsr(runner=lambda args, timeout=120: self._R(rc=1, stderr="boom"))
        with self.assertRaises(ServerAsrError):
            b.gpu_status()

    def test_load_engine_segs_skips_meta_and_norm_stems(self):
        from server_asr_bridge import ServerAsr
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        eng = tmp / "qwen3"
        eng.mkdir(parents=True)
        # 服务器端以音轨主名命名：transcribe.py 写 <audio.stem>.json
        (eng / "meta.json").write_text("{}", encoding="utf-8")      # 跳过
        (eng / "track1.json").write_text(
            json.dumps([{"t0": 1.0, "t1": 3.0, "text": "こんばんは"}]),
            encoding="utf-8")
        (eng / "track2.json").write_text(
            json.dumps([{"t0": 3.0, "t1": 6.0, "text": "おやすみ"}]),
            encoding="utf-8")
        segs = ServerAsr().load_engine_segs(str(tmp), "qwen3")
        self.assertEqual(sorted(segs), ["track1", "track2"])         # 主名归一
        self.assertEqual(segs["track1"][0]["text"], "こんばんは")


class TestServerAsrImportSegProvider(unittest.TestCase):
    """seg_provider 钩子：服务器段注入 import_voice（有台本→对齐；无文本→ASR 句子）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = Path(self._tmp.name) / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def _seg(self, text, t0, t1):
        return {"text": text, "t0": t0, "t1": t1}

    def test_script_track_aligned_by_server_segs(self):
        import import_voice
        (self.root / "台本作品/track1.mp3").parent.mkdir(parents=True)
        (self.root / "台本作品/track1.mp3").write_bytes(b"x")
        (self.root / "台本作品/track1.txt").write_text(
            "こんばんは。\nおやすみなさい。\n", encoding="utf-8")
        segs = [self._seg("こんばんは", 1.0, 3.0),
                self._seg("おやすみなさい", 3.0, 6.0)]
        meta = import_voice.import_voice(
            root=self.root / "台本作品", align=True, model="server:qwen3",
            seg_provider=lambda trk: segs, probe=False, out_root=self.out)
        ch = json.loads((self.out / meta["id"] / "content/ch00.json")
                        .read_text(encoding="utf-8"))
        # 文本以台本为准，时间来自服务器段
        self.assertEqual(ch["paras"], [["こんばんは。", "おやすみなさい。"]])
        self.assertEqual(ch["times"], [[1.0, 3.0], [3.0, 6.0]])

    def test_textless_track_uses_server_sentences(self):
        import import_voice
        (self.root / "只有音频").mkdir(parents=True)
        (self.root / "只有音频/track1.mp3").write_bytes(b"x")
        segs = [self._seg("こんばんは", 1.0, 3.0),
                self._seg("おやすみ", 3.0, 5.0)]
        meta = import_voice.import_voice(
            root=self.root / "只有音频", align=True, model="server:qwen3",
            seg_provider=lambda trk: segs, probe=False, out_root=self.out)
        ch = json.loads((self.out / meta["id"] / "content/ch00.json")
                        .read_text(encoding="utf-8"))
        # 无文本轨：句子直接来自服务器 ASR，带时间轴
        self.assertEqual(ch["paras"], [["こんばんは", "おやすみ"]])
        self.assertEqual(ch["times"], [[1.0, 3.0], [3.0, 5.0]])

    def test_failed_alignment_falls_back_without_times(self):
        import import_voice
        (self.root / "对不上/track1.mp3").parent.mkdir(parents=True)
        (self.root / "对不上/track1.mp3").write_bytes(b"x")
        (self.root / "对不上/track1.txt").write_text(
            "まったく別のことを言っている長い長い台詞です。\n", encoding="utf-8")
        segs = [self._seg("ありがとう", 1.0, 3.0)]   # 匹配率太低，不给时间
        meta = import_voice.import_voice(
            root=self.root / "对不上", align=True, model="server:qwen3",
            seg_provider=lambda trk: segs, probe=False, out_root=self.out)
        ch = json.loads((self.out / meta["id"] / "content/ch00.json")
                        .read_text(encoding="utf-8"))
        self.assertNotIn("times", ch)


class _FakeBridge:
    """服务器桥接替身：记录调用序列，可按剧本抛错。"""

    def __init__(self, ssh_fail=None, wait_fail=None):
        self.ssh_fail = ssh_fail
        self.wait_fail = wait_fail
        self.calls = []

    def check_ssh(self):
        if self.ssh_fail:
            raise ServerAsrError(self.ssh_fail)

    def upload(self, stage, name):
        self.calls.append(("upload", name))

    def start_transcribe(self, name, engine, gpu):
        self.calls.append(("start", name, engine, gpu))

    def wait_transcribe(self, name, max_sec=7200, interval=10):
        self.calls.append(("wait", name))
        if self.wait_fail:
            raise ServerAsrError(self.wait_fail)

    def pull(self, name, local_out):
        self.calls.append(("pull", name))

    def cleanup(self, name, keep_results=False):
        # 记下 keep_results：失败路径必须保留远端转写产物（回归锁）
        self.calls.append(("cleanup", name, keep_results))


class TestServerAsrTask(unittest.TestCase):
    """任务状态机 + 落盘 + 恢复（bridge 全程 mock，不联网不落真实 data/）。"""

    def setUp(self):
        import server_asr_task
        self.mod = server_asr_task
        self._tmp = tempfile.TemporaryDirectory()
        self._old_tasks_dir = self.mod.TASKS_DIR
        self.mod.TASKS_DIR = Path(self._tmp.name) / "import_tasks"
        self.mod._TASKS.clear()

    def tearDown(self):
        self.mod.TASKS_DIR = self._old_tasks_dir
        self.mod._TASKS.clear()
        self._tmp.cleanup()

    def _make_stage(self, name="RJ1234"):
        stage = Path(self._tmp.name) / "audio" / name
        stage.mkdir(parents=True, exist_ok=True)
        (stage / "track1.mp3").write_bytes(b"x")
        return stage

    def test_full_flow_success(self):
        from unittest.mock import patch
        bridge = _FakeBridge()
        stage = self._make_stage()
        t = self.mod.create_task(stage=stage, title="测试作品", key="rj1234",
                                 engine="qwen3", gpu="0,3")
        with patch.object(self.mod, "_discover_any", return_value=True), \
             patch.object(self.mod, "_segs_map", lambda lo, eng: {"track1": []}), \
             patch.object(self.mod, "_do_import",
                          return_value={"work_id": "rj1234", "chapters": 2,
                                        "sentences": 7}):
            self.mod.run_task(t.id, bridge)
        t = self.mod.get_task(t.id)
        self.assertTrue(t.done)
        self.assertEqual(t.phase, "done")
        self.assertEqual(t.work_id, "rj1234")
        self.assertEqual((t.chapters, t.sentences), (2, 7))
        # 调用序列完整且收尾清理了服务器残留
        self.assertEqual(bridge.calls[-1], ("cleanup", "rj1234", False))
        self.assertIn(("start", "rj1234", "qwen3", "0,3"), bridge.calls)
        # 状态确实落盘（关掉 Flask 后重启能扫到）
        self.assertTrue(self.mod._path(t.id).exists())

    def test_check_ssh_failure_marks_error(self):
        from unittest.mock import patch
        bridge = _FakeBridge(ssh_fail="无法连接服务器：请确认已连上VPN")
        t = self.mod.create_task(stage=self._make_stage(), key="rj1234")
        with patch.object(self.mod, "_discover_any", return_value=True):
            self.mod.run_task(t.id, bridge)
        t = self.mod.get_task(t.id)
        self.assertTrue(t.done)
        self.assertEqual(t.phase, "error")
        self.assertIn("VPN", t.error)
        self.assertIn(("cleanup", "rj1234", True), bridge.calls)
        # 失败时不清远端产物：转写可能已跑完（甚至还在跑），删了就无从续跑

    def test_transcribe_timeout_marks_error(self):
        from unittest.mock import patch
        bridge = _FakeBridge(wait_fail="服务器转写超过 120 分钟仍未结束，可稍后重新提交")
        t = self.mod.create_task(stage=self._make_stage(), key="rj1234")
        with patch.object(self.mod, "_discover_any", return_value=True):
            self.mod.run_task(t.id, bridge)
        t = self.mod.get_task(t.id)
        self.assertEqual(t.phase, "error")
        self.assertIn("120 分钟", t.error)
        # 超时最要命的就是"远端其实跑完了"：产物必须留着
        self.assertIn(("cleanup", "rj1234", True), bridge.calls)

    def test_no_audio_fails_before_upload(self):
        from unittest.mock import patch
        bridge = _FakeBridge()
        t = self.mod.create_task(stage=self._make_stage(), key="rj1234")
        with patch.object(self.mod, "_discover_any", return_value=False):
            self.mod.run_task(t.id, bridge)
        t = self.mod.get_task(t.id)
        self.assertEqual(t.phase, "error")
        self.assertIn("没有可转写", t.error)
        self.assertEqual([c[0] for c in bridge.calls], ["cleanup"])  # 没碰网络

    def test_restore_resumes_transcribing_task(self):
        t = self.mod.create_task(stage=self._make_stage(), key="rj1234")
        self.mod.update_task(t.id, phase="transcribing", done=False)
        restored = self.mod.restore_tasks()
        self.assertEqual([r.id for r in restored], [t.id])
        self.assertIn(t.id, self.mod._TASKS)

    def test_restore_interrupted_local_phase_marks_error(self):
        # Flask 在收集/上传阶段被关掉：无法安全续跑 → error + 清远程残留
        import server_asr_bridge
        fake = type("FakeAsr", (), {"cleanup": lambda self, name: log.append(name)})
        log = []
        old = server_asr_bridge.ServerAsr
        server_asr_bridge.ServerAsr = fake
        try:
            t = self.mod.create_task(stage=self._make_stage(), key="rj1234")
            self.mod.update_task(t.id, phase="collecting", done=False)
            restored = self.mod.restore_tasks()
        finally:
            server_asr_bridge.ServerAsr = old
        self.assertEqual(restored, [])
        t = self.mod.get_task(t.id)
        self.assertTrue(t.done)
        self.assertEqual(t.phase, "error")
        self.assertIn("重新提交", t.error)
        self.assertEqual(log, ["rj1234"])


# ---------------------------------------------------------------------------
# 图片日语（日常图片讲解课）：导入器 / 接口 / 前端接线
# ---------------------------------------------------------------------------

# 一份等价于 lessons/图片日语_第1课.md 写法的小样稿：格式约定全在里面
_PIC_MD = """\
# 「日常图片で学ぶ日本語」第9課

> 两张图，边看边学。

---

## 写真1：テスト写真

![测试图](../data/extracted_images/image1.jpg)

**通知文字（竖排）：**

> これはテストです。
> 二行目。

### 核心单词

| 单词 | 假名 | 释义 |
|---|---|---|
| テスト | てすと | test，测试 |
| 気持ち悪い | きもちわるい | 恶心，不舒服 |

### 语法：～てみる（试着……）

「てみる」接在动词て形后，表示试着做某事。

- 食べる → **食べてみる** ＝ 试着吃
- 見る → **見てみる** ＝ 试着看

> 同类：～ておく（预先做）。

### 实用句式：～中

- 営業中 ＝ 正在营业

---

## 写真2：二枚目

![二](../data/extracted_images/image2.jpeg)

**标题：**

> ああ。

### 核心单词

| 单词 | 假名 | 释义 |
|---|---|---|
| あ | あ | 啊 |

---

## まとめ

### 本课核心语法

1. **～てみる** — 试着……
2. **～中** — 正在……

### 本课核心单词（3个）

テスト、気持ち悪い、あ

---

## 練習

**1. 翻译以下句子：**

- 试着吃。（食べる → てみる）
- 正在营业中。（営業）

**2. 说说看：**

描述一下这张图。
"""


class TestPictureImport(unittest.TestCase):
    """图片日语课件导入器：人写的 md 稿 → 网页端用的结构化课程数据。"""

    def test_parse_structure(self):
        import import_picture_lesson as imp
        lesson, warnings = imp.parse_lesson(_PIC_MD, "lesson_09")
        self.assertEqual(warnings, [])
        self.assertEqual(lesson["title"], "「日常图片で学ぶ日本語」第9課")
        self.assertEqual(lesson["intro"], "两张图，边看边学。")
        self.assertEqual(len(lesson["photos"]), 2)

        p1, p2 = lesson["photos"]
        self.assertEqual((p1["n"], p1["title"]), (1, "テスト写真"))
        self.assertEqual(p1["caption"], "通知文字（竖排）")
        self.assertEqual(p1["texts"], ["これはテストです。", "二行目。"])
        self.assertEqual(p1["image"], "data/extracted_images/image1.jpg")
        # 表头行「| 单词 | 假名 | 释义 |」不能当词条
        self.assertEqual([w["form"] for w in p1["words"]], ["テスト", "気持ち悪い"])
        self.assertEqual(p1["words"][0]["kana"], "てすと")
        self.assertEqual(p2["caption"], "标题")

        # 讲解块：段落 / 要点 / 提示各归各位
        kinds = [pt["kind"] for pt in p1["points"]]
        self.assertEqual(kinds, ["grammar", "pattern"])
        blocks = p1["points"][0]["blocks"]
        self.assertEqual([b["t"] for b in blocks], ["p", "ul", "tip"])
        self.assertEqual(len(blocks[1]["items"]), 2)
        self.assertIn("**食べてみる**", blocks[1]["items"][0])
        self.assertEqual(blocks[2]["text"], "同类：～ておく（预先做）。")

        self.assertEqual(lesson["summary"]["points"],
                         ["**～てみる** — 试着……", "**～中** — 正在……"])
        self.assertEqual(lesson["summary"]["words"], ["テスト", "気持ち悪い", "あ"])

        # 练习：题组 + 题干 + 括号里的提示
        self.assertEqual([ex["title"] for ex in lesson["exercises"]],
                         ["翻译以下句子", "说说看"])
        ex1 = lesson["exercises"][0]
        self.assertEqual(ex1["items"][0], {"q": "试着吃。", "hint": "食べる → てみる"})
        self.assertEqual(lesson["exercises"][1]["blocks"][0]["text"], "描述一下这张图。")

    def test_merge_answers_sidecar(self):
        """参考答案单列（md 是学习稿不写答案）：逐题按序号合并，开放题整组一段。"""
        import import_picture_lesson as imp
        lesson, _ = imp.parse_lesson(_PIC_MD, "lesson_09")
        warns = imp.merge_answers(lesson, {
            "1": ["食べてみる。", "営業中です。"],
            "2": ["这是一张通知的照片。"],
        })
        self.assertEqual(warns, [])
        self.assertEqual(lesson["exercises"][0]["items"][0]["answer"], "食べてみる。")
        self.assertEqual(lesson["exercises"][1]["answer"], "这是一张通知的照片。")
        # 题数与答案数对不上：整组不合并、只警告（不静默错配）
        fresh, _ = imp.parse_lesson(_PIC_MD, "lesson_09")
        warnings = imp.merge_answers(fresh, {"1": ["只有一个答案"]})
        self.assertEqual(len(warnings), 1)
        self.assertIn("对不上", warnings[0])
        self.assertNotIn("answer", fresh["exercises"][0]["items"][0])

    def test_build_requires_images_and_writes_source(self):
        import import_picture_lesson as imp
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        (base / "lessons").mkdir()
        old = imp.BASE_DIR
        imp.BASE_DIR = base
        self.addCleanup(lambda: setattr(imp, "BASE_DIR", old))

        md = base / "lessons" / "第9课.md"
        md.write_text(_PIC_MD, encoding="utf-8")
        # 图片还没有：明确报错，不写出半成品数据
        with self.assertRaises(SystemExit) as ctx:
            imp.build(md, "lesson_09")
        self.assertIn("图片文件不存在", str(ctx.exception))

        img_dir = base / "data" / "extracted_images"
        img_dir.mkdir(parents=True)
        (img_dir / "image1.jpg").write_bytes(b"x")
        (img_dir / "image2.jpeg").write_bytes(b"x")
        lesson, warnings = imp.build(md, "lesson_09")
        self.assertEqual(warnings, [])
        self.assertEqual(lesson["id"], "lesson_09")
        self.assertEqual(lesson["source"], "lessons/第9课.md")
        # 相对 md 的 ../ 路径已归一到仓库根相对
        self.assertEqual([p["image"] for p in lesson["photos"]],
                         ["data/extracted_images/image1.jpg",
                          "data/extracted_images/image2.jpeg"])

class TestPictureApi(unittest.TestCase):
    """图片日语接口：课程列表 / 课件载荷（词库联动 + 已知词标注）/ 发图 / 挖矿。"""

    def setUp(self):
        import works
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_x": {"title": "词库课", "words": [
            {"id": "w001", "kanji": "女の子", "hiragana": "おんなのこ",
             "meaning": "女孩，女生", "mastery": 3, "example_ja": "この女の子は姉です。"},
            {"id": "w002", "kanji": "中", "hiragana": "なか", "meaning": "里面，中间", "mastery": 1},
            {"id": "w003", "kanji": "姉", "hiragana": "あね", "meaning": "姐姐", "mastery": 1},
            {"id": "w004", "kanji": "---", "hiragana": "グッズ", "meaning": "goods，周边商品", "mastery": 3},
        ]}}}
        (tmp / "vocabulary.json").write_text(json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        (tmp / "img").mkdir()
        (tmp / "img" / "p1.jpg").write_bytes(b"\xff\xd8fakejpeg")
        lessons = tmp / "lessons"
        lessons.mkdir()
        self.lesson = {
            "schema": 1, "id": "lesson_01", "title": "图片日语 第1课",
            "intro": "一图一课", "updated": "2026-09-11",
            "photos": [
                {"n": 1, "title": "招募海报", "image": "img/p1.jpg", "caption": "海报文字",
                 "texts": ["この女の子に罵られたい同志募集中！", "名大同好会"],
                 "words": [
                     {"form": "女の子", "kana": "おんなのこ", "meaning": "女孩"},
                     {"form": "姉", "kana": "あね", "meaning": "姐姐"},
                     {"form": "グッズ", "kana": "ぐっず", "meaning": "goods，周边"},
                     {"form": "同好会", "kana": "どうこうかい", "meaning": "同好会"},
                 ],
                 "points": [{"kind": "grammar", "title": "～に～られたい",
                             "blocks": [
                                 {"t": "p", "text": "被动＋愿望。"},
                                 {"t": "ul", "items": ["この女の子に罵られたい ＝ 想被这个女孩骂"]},
                                 {"t": "tip", "text": "同类：踏まれたい。"}]}]},
                {"n": 2, "title": "缺图的照片", "image": "img/nope.jpg", "caption": "",
                 "texts": [], "words": [], "points": []},
            ],
            "summary": {"points": ["**～に～られたい** — 想被……"],
                        "words": ["女の子", "同好会"]},
            "exercises": [{"n": 1, "title": "翻译", "blocks": [],
                           "items": [{"q": "我想被老师表扬。", "hint": "ほめる",
                                      "answer": "先生にほめられたい。"}]}],
        }
        (lessons / "lesson_01.json").write_text(
            json.dumps(self.lesson, ensure_ascii=False), encoding="utf-8")
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR,
                     picture.LESSONS_DIR, picture.BASE_DIR, works.WORKS_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        picture.LESSONS_DIR = lessons
        picture.BASE_DIR = tmp
        works.WORKS_DIR = tmp / "works"
        self.client = app.app.test_client()

    def tearDown(self):
        import works
        (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR,
         picture.LESSONS_DIR, picture.BASE_DIR, works.WORKS_DIR) = self._old
        works._profile_cache.clear()
        works._sent_cache.clear()
        self._tmp.cleanup()

    def test_list_lessons_with_practice_count(self):
        r = self.client.get("/api/pictures")
        self.assertEqual(r.status_code, 200)
        lessons = r.get_json()["lessons"]
        self.assertEqual(len(lessons), 1)
        self.assertEqual((lessons[0]["id"], lessons[0]["photo_count"], lessons[0]["word_count"]),
                         ("lesson_01", 2, 4))
        # 已在词库、能直接练的词：女の子 / 姉 / グッズ
        self.assertEqual(lessons[0]["practice_count"], 3)

    def test_detail_links_vocab_and_annotates_texts(self):
        pay = self.client.get("/api/pictures/lesson_01").get_json()
        words = {w["form"]: w for w in pay["photos"][0]["words"]}
        self.assertEqual(words["女の子"]["ref"], "lesson_x:w001")
        self.assertEqual(words["女の子"]["mastery"], 3)
        self.assertTrue(words["女の子"]["in_bank"])
        self.assertNotIn("ref", words["同好会"])   # 词库里没有 → 生词
        # 片假名词条（kanji 为 ---）也要能对上
        self.assertEqual(words["グッズ"]["ref"], "lesson_x:w004")
        self.assertEqual(pay["stats"]["in_bank"], 3)
        self.assertEqual(pay["stats"]["learned"], 3)
        self.assertEqual(pay["stats"]["practice_refs"],
                         ["lesson_x:w001", "lesson_x:w003", "lesson_x:w004"])

        # 已知词标注：女の子 点亮；「中」（募集中 里的单字词条）不点亮——
        # 单字命中在一屏一张图的讲解页里就是一片噪声
        segs = pay["photos"][0]["texts"][0]["segs"]
        marked = {s["t"]: s.get("w") for s in segs if s.get("w")}
        self.assertEqual(marked, {"女の子": "lesson_x:w001"})
        # 点亮的词配好词卡（释义 / 例句 / 掌握度）
        card = pay["vocab_words"]["lesson_x:w001"]
        self.assertEqual(card["form"], "女の子")
        self.assertEqual(card["meaning"], "女孩，女生")
        self.assertEqual(card["mastery"], 3)
        self.assertEqual(card["example_ja"], "この女の子は姉です。")
        # 片假名词卡：写法取读音字段（词表里 kanji 是 ---）
        self.assertEqual(pay["vocab_words"]["lesson_x:w004"]["form"], "グッズ")
        # 小结里的单词同样挂词库
        self.assertEqual(pay["summary"]["words"][0]["ref"], "lesson_x:w001")

    def test_image_route_whitelist(self):
        r = self.client.get("/api/pictures/lesson_01/image/1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"\xff\xd8fakejpeg")
        # 课件里登记了但文件不在 / 没登记的序号 / 不存在的课
        self.assertEqual(self.client.get("/api/pictures/lesson_01/image/2").status_code, 404)
        self.assertEqual(self.client.get("/api/pictures/lesson_01/image/9").status_code, 404)
        self.assertEqual(self.client.get("/api/pictures/nope").status_code, 404)

    def test_image_path_rejects_outside_repo(self):
        """数据文件被手改成越界路径也发不出去（只认登记 + 落点必须在仓库内）。"""
        tmp = Path(self._tmp.name)
        (tmp.parent / "secret.jpg").write_bytes(b"x")
        self.addCleanup(lambda: (tmp.parent / "secret.jpg").unlink(missing_ok=True))
        fake = {"id": "x", "photos": [
            {"n": 1, "image": "../secret.jpg"},
            {"n": 2, "image": "C:/Windows/win.ini"},
            {"n": 3, "image": "img/p1.jpg"},
        ]}
        self.assertIsNone(picture.image_path(fake, 1))
        self.assertIsNone(picture.image_path(fake, 2))
        self.assertIsNotNone(picture.image_path(fake, 3))

    def test_mine_adds_to_mywords_with_source_notes(self):
        r = self.client.post("/api/pictures/lesson_01/mine", json={
            "form": "同好会", "kana": "どうこうかい", "meaning": "同好会，兴趣社团",
            "sentence": "名大同好会", "photo": 1})
        self.assertEqual(r.status_code, 200)
        ref = r.get_json()["ref"]
        self.assertTrue(ref.startswith(app.DEFAULT_USER_LESSON + ":"))
        vocab = app.load_vocab()
        w = next(x for x in vocab["lessons"][app.DEFAULT_USER_LESSON]["words"]
                 if x["kanji"] == "同好会")
        self.assertEqual(w["hiragana"], "どうこうかい")
        self.assertEqual(w["meaning"], "同好会，兴趣社团")
        self.assertEqual(w["mastery"], 0)
        self.assertEqual(w["notes"], "来源：图片日语 第1课 写真1（招募海报）")
        self.assertEqual(w.get("example_ja"), "名大同好会")

    def test_mine_katakana_keeps_katakana_in_reading_field(self):
        """片假名词照词表存法：写法记 ---、片假名原样进读音字段（不是平假名转写）。"""
        r = self.client.post("/api/pictures/lesson_01/mine", json={
            "form": "ポーズ", "kana": "ぽーず", "meaning": "pose，姿势", "photo": 2})
        self.assertEqual(r.status_code, 200)
        vocab = app.load_vocab()
        w = next(x for x in vocab["lessons"][app.DEFAULT_USER_LESSON]["words"]
                 if x["hiragana"] == "ポーズ")
        self.assertEqual(w["kanji"], "---")
        self.assertIn("写真2", w["notes"])

    def test_mine_duplicate_and_validation(self):
        # 全库判重：词库里已有（女の子）→ 409 带已有 ref，不再重复录
        r = self.client.post("/api/pictures/lesson_01/mine", json={
            "form": "女の子", "kana": "おんなのこ", "meaning": "女孩"})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["duplicate"])
        self.assertEqual(r.get_json()["ref"], "lesson_x:w001")
        # 缺释义 / 缺词形 / 课程不存在
        self.assertEqual(self.client.post("/api/pictures/lesson_01/mine",
                                          json={"form": "x"}).status_code, 400)
        self.assertEqual(self.client.post("/api/pictures/lesson_01/mine",
                                          json={"meaning": "y"}).status_code, 400)
        self.assertEqual(self.client.post("/api/pictures/nope/mine",
                                          json={"form": "x", "meaning": "y"}).status_code, 404)

    def test_mine_extends_practice_refs(self):
        """挖矿闭环：新词落地后课程载荷的练习入口多一个 ref。"""
        before = self.client.get("/api/pictures/lesson_01").get_json()["stats"]["practice_refs"]
        self.client.post("/api/pictures/lesson_01/mine", json={
            "form": "同好会", "kana": "どうこうかい", "meaning": "同好会"})
        after = self.client.get("/api/pictures/lesson_01").get_json()["stats"]["practice_refs"]
        self.assertEqual(len(after), len(before) + 1)

    def test_page_and_home_entry(self):
        html = self.client.get("/pictures").get_data(as_text=True)
        self.assertIn("pictures.js", html)
        self.assertIn("图片日语", html)
        home = self.client.get("/api/home").get_json()
        items = [i for m in home["modules"] if m["id"] == "immersive"
                 for i in m["items"]]
        pics = next(i for i in items if i["id"] == "pictures")
        self.assertEqual(pics["kind"], "pictures")
        self.assertEqual(pics["badge"], "1 课")     # 角标单位由 badge_text 覆盖
        self.assertEqual(home["overview"]["picture_lessons"], 1)


class TestPictureFrontend(unittest.TestCase):
    """图片日语前端接线：首页入口 → /pictures、页面 id ↔ 脚本引用、练习回跳。"""

    STATIC = Path(__file__).resolve().parent / "static"
    TEMPLATES = Path(__file__).resolve().parent / "templates"

    def test_home_entry_opens_pictures_page(self):
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('if (it.kind === "pictures") { window.location.href = "/pictures"; return; }',
                      appjs)

    def test_page_ids_match_script(self):
        html = (self.TEMPLATES / "pictures.html").read_text(encoding="utf-8")
        js = (self.STATIC / "pictures.js").read_text(encoding="utf-8")
        used = set(re.findall(r'\$\("([\w-]+)"\)', js))
        self.assertTrue(used, "没解析到任何 id：选择规则可能需要更新")
        # 脚本里 innerHTML 模板写的 id（如词卡里的 pop-msg）不在页面模板中，属正常
        dynamic = set(re.findall(r'id="([\w-]+)"', js))
        missing = used - set(re.findall(r'id="([\w-]+)"', html)) - dynamic
        self.assertEqual(missing, set(), f"pictures.js 引用了页面里不存在的元素 id: {missing}")

    def test_practice_handoff_wired_both_ends(self):
        """「练本课单词」把 refs 带回练习页自动开练：两端都要接上。"""
        js = (self.STATIC / "pictures.js").read_text(encoding="utf-8")
        self.assertIn("practice_refs", js)
        self.assertIn("practice_mode", js)
        appjs = (self.STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('bootParams.get("practice_refs")', appjs)
        self.assertIn("refs: bootRefs", appjs)
        # 点名词条时绕过重点词清单（否则清单里没有这些词会整批筛空）
        self.assertIn("focus: false", appjs)

    def test_client_uses_server_annotations_and_tts(self):
        js = (self.STATIC / "pictures.js").read_text(encoding="utf-8")
        for token in ("/api/pictures/", "/api/tts/text", "/api/dict", "/api/focus",
                      'class="seg ', "reloadLesson", "openWordCard", "openMine"):
            self.assertIn(token, js)
        css = (self.STATIC / "pictures.css").read_text(encoding="utf-8")
        for token in (".seg-known", ".seg-learn", ".seg-new", ".sw-chip", ".pw-row"):
            self.assertIn(token, css)


class TestAnswerIntegrity(unittest.TestCase):
    """2026-09 通览修复：判分口径、选项唯一性、发音缓存与字幕解析。

    每条都对应一个实测复现过的问题（复现方式写在用例名/注释里）。
    """

    HERE = Path(__file__).resolve().parent

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "t", "words": [
            {"id": "w001", "kanji": "友人", "hiragana": "ゆうじん", "meaning": "朋友",
             "alternates": ["ともだち"]},
            {"id": "w002", "kanji": "猫", "hiragana": "ねこ", "meaning": "猫",
             "example_ja": "猫が寝ています。"},
            {"id": "w003", "kanji": "犬", "hiragana": "いぬ", "meaning": "狗",
             "example_ja": "犬が好きです。"},
            {"id": "w004", "kanji": "---", "hiragana": "Tシャツ", "meaning": "T恤",
             "example_ja": "Tシャツを着ます。"},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, app.AUDIO_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        app.AUDIO_DIR = tmp / "audio"
        app.AUDIO_DIR.mkdir()
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR, app.AUDIO_DIR = self._old
        self._tmp.cleanup()

    # ---- A1 写汉字题不接受读音别名（词库 7 例，如 友人→ともだち） ----
    def test_kanji_writing_rejects_kana_alias(self):
        for alias in ("ともだち", "ゆうじん"):
            r = self.client.post("/api/check", json={
                "ref": "lesson_01:w001", "mode": "kana_to_kanji",
                "answer": alias}).get_json()
            self.assertFalse(r["correct"], f"只打假名「{alias}」不该过汉字题")
        r = self.client.post("/api/check", json={
            "ref": "lesson_01:w001", "mode": "kana_to_kanji",
            "answer": "友人"}).get_json()
        self.assertTrue(r["correct"])

    # ---- A2 文字题干不得就是正确选项（扫真实词库：锁的是数据形状） ----
    def test_no_text_cue_prints_the_answer(self):
        # 读的是仓库里的真实词表（绕过本类 setUp 覆盖的 VOCAB_PATH / BASE_DIR）。
        # 公开起始版冷启动时它还不存在：按仓库布局补一次自举（私人仓库里从不触发）。
        root = self.HERE.parent
        app.ensure_vocab_file(root / "vocabulary.json",
                              root / "data" / "seed" / "vocabulary_seed.json")
        real = json.loads((root / "vocabulary.json").read_text(encoding="utf-8"))
        checked = 0
        for md in app.PRACTICE_MODES:
            if not md.get("mcq_mode") or md.get("image_mode") or md.get("audio_mode"):
                continue    # 图片/发音题干不是可比对的文字
            for lid, ldata in real["lessons"].items():
                for w in ldata.get("words", []):
                    if not app.mode_accepts(w, md, lesson_id=lid):
                        continue
                    self.assertNotEqual(
                        app.prompt_for(w, md), app.expected_for(w, md),
                        f"{md['id']} 把答案印在题干上: {w.get('kanji')}")
                    checked += 1
        self.assertGreater(checked, 1000, "扫描覆盖太少，检查 mode_accepts 是否走偏")

    # ---- A3 活用四选一不出现两个相同选项（同动词常在两课重复收录） ----
    def test_latin_in_reading_is_case_insensitive(self):
        self.assertTrue(app.check_answer("tシャツ", "Tシャツ"))
        self.assertTrue(app.check_answer("Ｔシャツ", "Tシャツ"))   # 全角大写经 NFKC
        self.assertFalse(app.check_answer("ちゃいつ", "Tシャツ"))   # 不是无脑放宽
        self.assertEqual(app.normalize_kana("ガッコウ"), "がっこう")
        r = self.client.post("/api/check", json={
            "ref": "lesson_01:w004", "mode": "audio_to_kana",
            "answer": "tシャツ"}).get_json()
        self.assertTrue(r["correct"], "听音写假名打 Tシャツ 必须能判对")

    # ---- B1 只改汉字写法也要失效发音（朗读文本优先用汉字） ----
    def test_kanji_only_edit_invalidates_audio(self):
        p = app.audio_path_for_ref("lesson_01:w001")
        p.write_bytes(b"old")
        self.client.post("/api/words/edit", json={
            "ref": "lesson_01:w001", "kanji": "友達", "hiragana": "ゆうじん",
            "meaning": "朋友"})
        self.assertFalse(p.exists(), "改写法后旧音频念的还是旧字形")
        p.write_bytes(b"new")
        self.client.post("/api/words/edit", json={
            "ref": "lesson_01:w001", "kanji": "友達", "hiragana": "ゆうじん",
            "meaning": "伙伴"})
        self.assertTrue(p.exists(), "只改释义不该重生成发音")

    # ---- B2 疑似截断的音频绝不落正式缓存 ----
    def test_truncated_audio_is_not_cached(self):
        from unittest import mock

        def fake_tts(payload):
            fake = mock.Mock()

            class Comm:
                def __init__(self, *a, **kw):
                    pass

                async def save(self, path):
                    Path(path).write_bytes(payload)
            fake.Communicate = Comm
            return mock.patch.dict(sys.modules, {"edge_tts": fake})

        out = app.AUDIO_DIR / "cut.mp3"
        with fake_tts(b"x" * 20):        # 远小于按字符数估算的阈值
            self.assertFalse(app.generate_audio_sync("テストテキスト", out))
        self.assertFalse(out.exists(), "残缺音频一旦进缓存就永久生效")
        self.assertEqual(list(app.AUDIO_DIR.glob("*.tmp*")), [], "临时文件要清掉")
        with fake_tts(b"x" * 5000):
            self.assertTrue(app.generate_audio_sync("テスト", out))
        self.assertTrue(out.exists())

    # ---- C1 轮询真的在睡；单次 ssh 抖动不判失败 ----
    def test_wait_transcribe_sleeps_and_survives_blip(self):
        from unittest import mock
        from server_asr_bridge import ServerAsr

        class R:
            def __init__(self, stdout=""):
                self.returncode, self.stdout = 0, stdout

        replies = [Exception("ssh 抖动"), R("ALIVE"), R("ALIVE"), R("DEAD"), R("完成")]

        def runner(args, timeout=120):
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        slept = []
        with mock.patch("server_asr_bridge.time.sleep", slept.append):
            log = ServerAsr(runner=runner).wait_transcribe("k", max_sec=600, interval=10)
        self.assertEqual(log, "完成")
        self.assertEqual(slept, [10, 10, 10], "每轮必须真等 interval 秒，否则 max_sec 形同虚设")

    # ---- D1 纯说话人标签的 cue：不成句，但要闭合上一句 ----
    def test_label_only_cue_is_a_boundary(self):
        import voice
        out = voice.cues_to_sentences([
            voice.Cue(6, 9, "♪♪"), voice.Cue(9, 10, "【ミア】"),
            voice.Cue(10, 12, "こんにちは")])
        self.assertEqual([s["text"] for s in out], ["♪♪", "こんにちは"])
        self.assertEqual(out[0]["t1"], 9.0, "上一句被拉长到下次换人 = 点读播错区间")

    # ---- D2 SRT 块之间缺空行不并句、不把时间戳读进正文 ----
    def test_srt_without_blank_lines_keeps_two_cues(self):
        import voice
        raw = ("1\n00:00:00,000 --> 00:00:02,000\n今日は\n"
               "2\n00:00:02,000 --> 00:00:04,000\nいい天気\n")
        cues = voice._cues_from_blocks(voice._split_blocks(raw))
        self.assertEqual([c[2] for c in cues], ["今日は", "いい天気"])

    # ---- C3 / E：只有运行时才看得见，用源码断言钉住 ----
    def test_restore_only_in_serving_process(self):
        src = (self.HERE / "app.py").read_text(encoding="utf-8")
        self.assertIn("WERKZEUG_RUN_MAIN", src,
                      "debug 重载器会把模块顶层跑两遍，恢复 ASR 任务必须只在一个进程里做")

    def test_reader_honours_work_param(self):
        js = (self.HERE / "static" / "reader.js").read_text(encoding="utf-8")
        self.assertIn('refreshWorks(q.get("work"), q.get("ch"))', js,
                      "导入页的「去阅读」带 ?work=，init 不读就会打开旧作品")


class TestServerAsrBridge(unittest.TestCase):
    """ssh 编排的两处实测：远端删除的目标、nvidia-smi 输出的解析。"""

    HERE = Path(__file__).resolve().parent

    @staticmethod
    def _bridge(stdout="", returncode=0):
        """返回 (ServerAsr, 已发出的命令列表)。"""
        from server_asr_bridge import SRV_JOBS, ServerAsr
        seen = []

        def runner(args, timeout=120):
            seen.append(args)
            return subprocess.CompletedProcess(args, returncode, stdout, "")

        return ServerAsr(runner=runner), seen, SRV_JOBS

    def test_cleanup_refuses_blank_or_nested_name(self):
        """空任务名会把目标退化成 `rm -rf <jobs 根目录>`——共享机上连别人的任务一起删。"""
        bridge, seen, _ = self._bridge()
        for bad in ("", "a/b", "a\\b"):
            with self.assertRaises(ServerAsrError, msg=f"key={bad!r} 竟然放过去了"):
                bridge.cleanup(bad)
        self.assertEqual(seen, [], "被拒绝的任务名不应该发出任何远端命令")

    def test_cleanup_only_touches_its_own_job(self):
        bridge, seen, srv_jobs = self._bridge()
        bridge.cleanup("rj12345")
        self.assertEqual(len(seen), 1)
        targets = seen[0][-1].split()[2:]        # "rm -rf <目标...>"
        self.assertTrue(targets, "没有发出删除命令")
        for t in targets:
            self.assertTrue(t.startswith(f"{srv_jobs}/rj12345"),
                            f"删除目标跳出了自己的 job 目录：{t}")

    def test_gpu_status_skips_short_rows(self):
        """字段数按 5 个取（parts[4] 是利用率），只挡到 4 会 IndexError 炸成 500。"""
        stdout = ("0, NVIDIA GeForce RTX 4090, 32760, 100\n"     # 少一列：跳过
                  "1, NVIDIA GeForce RTX 4090, 32760, 200, 45\n")  # 正常
        bridge, _, _ = self._bridge(stdout=stdout)
        gpus = bridge.gpu_status()
        self.assertEqual([g["index"] for g in gpus], [1])
        self.assertEqual(gpus[0]["util"], 45)


class TestAtomicAndPersistGuards(unittest.TestCase):
    """作品 JSON 的原子写，以及 ASR 任务落盘失败必须出声。"""

    def test_write_json_atomic_leaves_no_temp_and_overwrites(self):
        import works
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "work.json"
            works.write_json_atomic(path, {"title": "第一版"}, indent=1)
            works.write_json_atomic(path, {"title": "第二版"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                             {"title": "第二版"})
            leftovers = [p.name for p in Path(d).iterdir() if p != path]
            self.assertEqual(leftovers, [], f"临时文件没被换掉：{leftovers}")

    def test_work_json_readable_by_load_work(self):
        """写进去的必须能被 works 那边读出来（半截文件会变成「作品不存在」）。"""
        import works
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "work.json"
            works.write_json_atomic(path, {"id": "w1", "chapters": []})
            self.assertEqual(works._read_json(path)["id"], "w1")

    def test_persist_failure_is_loud(self):
        import server_asr_task as st
        t = st.AsrTask(id="abc123", stage="s")
        old_path, old_stdout = st._path, sys.stdout
        buf = io.StringIO()
        st._path = lambda tid: Path(tempfile.gettempdir()) / "no-such-dir-jl" / f"{tid}.json"
        sys.stdout = buf
        try:
            st._persist(t)         # 不能抛：抛了会把整个导入流程带崩
        finally:
            sys.stdout = old_stdout
            st._path = old_path
        self.assertIn("落盘失败", buf.getvalue(),
                      "任务状态没落盘却不吭声，重启后这个任务就凭空消失了")


class TestProgressFilePersistence(unittest.TestCase):
    """进度文件（vocabulary / particles / exam）的落盘路径。

    这三个文件存着全部掌握度与复习调度，不可再生。原子写只防「半截文件」，
    防不住：同进程两个线程共用同一个临时名（互相把对方的内容搅进去）、断电时
    rename 已完成而数据块还在页缓存里（留下 0 字节坏档）、逻辑 bug 写出
    「完整而错误」的整表（要靠快照回滚）。
    """

    HERE = Path(__file__).resolve().parent

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.dir = tmp
        self.vocab = tmp / "vocabulary.json"
        self.vocab.write_text(json.dumps(make_vocab(), ensure_ascii=False),
                              encoding="utf-8")
        self._old = (app.VOCAB_PATH, app.BASE_DIR, app.BACKUP_DIR)
        app.VOCAB_PATH = self.vocab
        app.BASE_DIR = tmp
        app.BACKUP_DIR = tmp / "data" / "backups"

    def tearDown(self):
        (app.VOCAB_PATH, app.BASE_DIR, app.BACKUP_DIR) = self._old
        self._tmp.cleanup()

    def test_single_atomic_write_implementation(self):
        """app._atomic_write 必须只是 works.write_text_atomic 的转发。

        以前两处各一份、口径不同（works 那版的临时名只带 pid），漂出来的结果
        是并发导入能把 work.json 写成半截。别再退回两份实现。
        """
        src = (self.HERE / "app.py").read_text(encoding="utf-8")
        self.assertIn("works.write_text_atomic(path, text)", src,
                      "_atomic_write 又变回自己写临时文件了")
        self.assertNotIn("threading.get_ident()}.tmp", src,
                         "临时名造法散回 app.py 了，两份实现会重新漂移")

    def test_atomic_write_fsyncs_before_replace(self):
        """没 fsync 就改名：NTFS 只 journalled 了 rename 这条元数据操作，
        断电后可能留下 0 字节的正式文件。"""
        src = (self.HERE / "works.py").read_text(encoding="utf-8")
        body = src[src.index("def write_text_atomic"):src.index("def write_json_atomic")]
        self.assertIn("fp.flush()", body)
        self.assertIn("os.fsync(fp.fileno())", body)
        self.assertLess(body.index("os.fsync"), body.index("tmp.replace(path)"),
                        "fsync 要在改名之前，否则保不住数据块")

    def test_concurrent_writers_do_not_interleave(self):
        """8 个线程写同一个目标：结果必须是某一份完整内容，且不留临时文件。

        共用同一个 tmp 名时，两个写者会交替往同一个文件灌，再把半截内容改名进
        正式路径——正式文件看着是「原子替换」的，其实读不回来。
        """
        import threading
        payloads = [json.dumps({"n": i, "pad": "x" * 40000}, ensure_ascii=False)
                    for i in range(8)]
        target = self.dir / "concurrent.json"
        threads = [threading.Thread(target=works_write_text,
                                     args=(target, text)) for text in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        final = target.read_text(encoding="utf-8")
        self.assertIn(final, payloads, "写进去的是两份内容交错后的半截文件")
        self.assertEqual([p.name for p in self.dir.iterdir()
                          if p.name.startswith(target.name) and p != target], [],
                         "留下没换掉的临时文件")

    def test_daily_snapshot_before_overwrite(self):
        """快照拍的是「今天动手之前」的状态——写坏之后它才有回滚价值。"""
        before = json.loads(self.vocab.read_text(encoding="utf-8"))
        app.save_vocab({"lessons": {"lesson_01": {"title": "t", "words": [
            {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人"}]}}})
        snaps = sorted(app.BACKUP_DIR.glob("vocabulary.bak_*.json"))
        self.assertEqual(len(snaps), 1, "第一次覆写前没留当日快照")
        self.assertEqual(json.loads(snaps[0].read_text(encoding="utf-8")), before,
                         "快照拍的必须是覆写前的内容，否则等于没备份")
        # 同日再存不重复拍（一天最多一份）
        app.save_vocab({"lessons": {}})
        self.assertEqual(len(sorted(app.BACKUP_DIR.glob("vocabulary.bak_*.json"))), 1)

    def test_snapshot_keeps_recent_days_only(self):
        (app.BACKUP_DIR.parent / "backups").mkdir(parents=True, exist_ok=True)
        for i in range(app.BACKUP_KEEP + 3):
            (app.BACKUP_DIR / f"vocabulary.bak_2020-01-{i + 1:02d}.json").write_text(
                "{}", encoding="utf-8")
        app._backup_daily(self.vocab)      # 再叠上今天这份
        names = [p.name for p in sorted(app.BACKUP_DIR.glob("vocabulary.bak_*.json"))]
        self.assertEqual(len(names), app.BACKUP_KEEP)
        self.assertIn(f"vocabulary.bak_{date.today():%Y-%m-%d}.json", names)
        self.assertIn("vocabulary.bak_2020-01-10.json", names)     # 最近的过去快照留着
        self.assertNotIn("vocabulary.bak_2020-01-04.json", names)  # 排到第 8 名就出局

    def test_snapshot_failure_does_not_block_save(self):
        """快照目录本身坏掉（被一个同名文件占住）也要照常保存。

        备份是保险，不是主流程；宁可少一份快照，也不能让交卷失败。
        """
        app.BACKUP_DIR.parent.mkdir(parents=True, exist_ok=True)
        app.BACKUP_DIR.write_text("我不是目录", encoding="utf-8")
        app.save_vocab({"lessons": {"lesson_01": {"title": "t", "words": []}}})
        self.assertEqual(json.loads(self.vocab.read_text(encoding="utf-8")),
                         {"lessons": {"lesson_01": {"title": "t", "words": []}}})

    def test_second_instance_is_refused(self):
        """Windows 上重复 bind 同端口不报错，程序自己得挡住（见函数注释）。"""
        import socket as _sock
        free = _sock.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        app._refuse_second_instance(port)          # 空端口：放行
        busy = _sock.socket()
        busy.bind(("127.0.0.1", port))
        busy.listen(1)
        try:
            with self.assertRaises(SystemExit) as ctx:
                app._refuse_second_instance(port)
            self.assertIn("另一个实例正在监听", str(ctx.exception))
        finally:
            busy.close()
        app._refuse_second_instance(port)          # 对方关掉后立刻能再起

    def test_guard_skips_reloader_child(self):
        """守卫只能查重载器的父进程。

        debug=True 下是「父进程 bind 端口 → 子进程靠 SO_REUSEADDR 重绑接管」，
        子进程再查一次会把自家父进程当成别人：第一次正常启动就自我否决，
        两个进程全退出、端口上空无一人（这是我加这条守卫时真踩到的）。
        """
        src = (self.HERE / "app.py").read_text(encoding="utf-8")
        block = src[src.index('if __name__ == "__main__":'):]
        call = block.index("_refuse_second_instance()")
        guard = block.index('os.environ.get("WERKZEUG_RUN_MAIN") != "true"')
        self.assertLess(guard, call,
                        "守卫没挡在重载器子进程外面，正常启动会自己拒自己")


def works_write_text(path, text):
    import works
    works.write_text_atomic(path, text)


class TestFrontendGuards(unittest.TestCase):
    """前端几处交互守卫（按源码断言，与项目里既有的 JS 静态检查同一风格）。"""

    HERE = Path(__file__).resolve().parent

    def _read(self, rel):
        return (self.HERE / rel).read_text(encoding="utf-8")

    def test_skip_no_audio_clamps_index(self):
        """剔掉最后一张时 index 会等于新长度，不夹住就 renderQuestion 取到 undefined。"""
        js = self._read("static/app.js")
        body = js[js.index("function skipNoAudio()"):js.index("function endSessionOrBackHome()")]
        self.assertIn("state.questions.splice(state.index, 1);", body,
                      "skipNoAudio 的结构变了，这条断言需要跟着改")
        self.assertIn("if (state.index >= state.questions.length)", body,
                      "剔除末题后没夹 index：本屏卡死且已答结果不再交卷")

    def test_modes_container_binds_handler_once(self):
        js = self._read("static/app.js")
        self.assertNotIn(
            'modeDiv.addEventListener("change"', js,
            "#modes 是常驻节点，loadConfig 每跑一次就多叠一个监听器")
        self.assertIn("modeDiv.onchange =", js, "改回赋值写法：后一次绑定覆盖前一次")

    def test_stale_fetches_are_dropped(self):
        """并发请求只认最后一次的结果，否则慢回来的旧响应会覆盖新状态。"""
        appjs = self._read("static/app.js")
        self.assertIn("if (seq !== focusPickSeq) return;", appjs,
                      "重点词挑词列表会被后到的旧筛选结果覆盖")
        reader = self._read("static/reader.js")
        self.assertIn("if (seq !== chapterSeq) return;", reader,
                      "连点下一章时正文与章号会错位")

    def test_reader_survives_no_works(self):
        """清空作品后（或新机器只带 demo_n5 又被删掉）按钮还在，点下去不能抛错。"""
        reader = self._read("static/reader.js")
        nav = reader[reader.index("async function loadChapter("):
                     reader.index("// ---------- 渲染 ----------")]
        self.assertIn("if (!state.work) return;", nav, "loadChapter 没防 state.work 为空")
        self.assertIn("if (!state.payload) return;",
                      reader[reader.index("function renderChapter()"):
                             reader.index("function renderChapter()") + 400],
                      "三个显示开关在没有正文时会抛 TypeError")

    def test_play_audio_yields_to_chapter_track(self):
        reader = self._read("static/reader.js")
        self.assertIn("if (media.el && !media.el.paused) media.el.pause();",
                      reader[reader.index("function playAudio("):
                             reader.index("function playSentence(")],
                      "朗读整句/词卡发音与章节原声会叠着放")

    def test_import_page_phase_map_covers_error(self):
        html = self._read("templates/import.html")
        self.assertIn('error: "失败"', html,
                      "PHASE_CN 缺 error：失败的任务在进度条上是一行空白")

    def test_next_btn_focus_does_not_scroll(self):
        """「下一题」在练习屏最底部：focus() 默认把页面滚下去，答完一跳、切题又跳回来。

        preventScroll 只关掉滚动，焦点照给（Enter 推进走全局监听，不依赖焦点）。
        """
        calls = re.findall(r'\$\("next-btn"\)\.focus\(([^)]*)\)',
                           self._read("static/app.js"))
        self.assertTrue(calls, "找不到 next-btn 的 focus 调用，这条锁失效了")
        for arg in calls:      # 扫全部调用：将来新增一处也会被抓住
            self.assertIn("preventScroll", arg,
                          "next-btn 在最底部，不加 preventScroll 就是「答完跳到底」")

    def test_feedback_sits_after_answer_area_before_flashcard(self):
        """反馈面板的位置两头都卡死：在作答区之后、闪卡控件之前。

        排到作答区前面 → 选择题一作答，刚点的四个选项被反馈整块顶下去；
        排到闪卡控件后面 → 闪卡背面（渲染进 #feedback）会跑到「认识/不认识」下面。
        """
        html = self._read("templates/index.html")
        i_opts = html.index('id="exam-controls"')
        i_fb = html.index('id="feedback"')
        i_flash = html.index('id="flashcard-controls"')
        self.assertLess(i_opts, i_fb, "反馈又排到作答区前面了")
        self.assertLess(i_fb, i_flash, "闪卡背面必须排在翻面/自评按钮之前")

    def test_next_btn_sticks_to_viewport_bottom(self):
        """「下一题」粘在视口底部：反馈很长时也不用滚下去找它。"""
        css = self._read("static/style.css")
        m = re.search(r"(?m)^#next-btn\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m, "style.css 里找不到 #next-btn 的独立规则")
        self.assertIn("position: sticky", m.group(1))
        self.assertIn("bottom: 0", m.group(1))


# ====================================================================
# 2026-09-21 审查修复的回归锁（每条对应一处真问题，别删）
# ====================================================================


class TestReaderSyncDirections(unittest.TestCase):
    """阅读器「同步 −/+」的偏移必须拆成正反两个换算。

    字幕时间 ↔ 音频时间是一对反函数。先前两处都用同一个 `syncT(t)=t-sync`
    （高亮拿它减、点句跳播也拿它减），偏移非零时两个功能朝相反方向偏 2·sync：
    把跟读高亮调准，「点句播放 / 单句循环停」就必然歪掉，反之亦然。
    默认偏移 0 时看不出，所以一直没被发现。
    """

    HERE = Path(__file__).resolve().parent

    def setUp(self):
        self.js = (self.HERE / "static/reader.js").read_text(encoding="utf-8")

    def test_two_inverse_helpers(self):
        self.assertIn("function contentAt(a)", self.js, "缺「音频时间 → 字幕时间」的换算")
        self.assertIn("function audioAt(t)", self.js, "缺「字幕时间 → 音频时间」的换算")
        self.assertNotIn("syncT(", self.js,
                         "同一个函数当正反两用：偏移会被算成 2·sync")

    def test_highlight_converts_audio_time_first(self):
        # currentTime 是音频时间，直接拿去比 n.t0（字幕时间）会整体偏 sync
        self.assertIn("markSentenceAt(contentAt(el.currentTime))", self.js)

    def test_seek_and_stop_use_audio_time(self):
        self.assertIn("const aim = Math.max(0, audioAt(t0));", self.js,
                      "点句跳播要 seek 到字幕时刻对应的音频位置")
        self.assertIn("? audioAt(t1) : null", self.js, "单句循环停的停点是音频时间")
        self.assertIn("el.currentTime = Math.max(0, audioAt(prev.t0));", self.js,
                      "「回上一句」的跳转同样要换算成音频时间")


class TestCourseIntroTip(unittest.TestCase):
    """课程预习卡要带 tip（前端「💡 用法」读 w.tip）。

    闪卡与「学新词」卡片都带（TestLearnApi.test_start_carries_tip），课程预习卡
    漏了这个字段时前端不报错、只是那一段永远不渲染——静默少功能。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        vocab = {"lessons": {"lesson_01": {"title": "t", "words": [
            {"id": "w001", "kanji": "人", "hiragana": "ひと", "meaning": "人",
             "tip": "用法讲解示例"},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        app.FOCUS_PATH.unlink(missing_ok=True)
        self.client = app.app.test_client()

    def tearDown(self):
        app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR = self._old
        self._tmp.cleanup()

    def test_course_intro_carries_tip(self):
        st = self.client.post("/api/start", json={
            "lesson": "lesson_01", "mode": "course", "count": 5}).get_json()
        intros = [q["intro"] for q in st["questions"] if q.get("intro")]
        self.assertTrue(intros, "课程模式没给出预习卡")
        self.assertEqual(intros[0]["tip"], "用法讲解示例",
                         "预习卡没带 tip：前端「💡 用法」整片不显示")


class TestInputGuards(unittest.TestCase):
    """畸形请求不该变成 500 的 HTML 错误页。

    前端每个请求都走 res.json()：视图抛异常时拿到 HTML，报出来的错会是
    「Unexpected token '<'」——与真实原因毫不相干（README 记过这类事故）。
    数值参数一律经 _int_field/_num_field 转换并夹紧。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        today = date.today()
        vocab = {"lessons": {"lesson_01": {"title": "t", "words": [
            {"id": "w001", "kanji": "先生", "hiragana": "せんせい", "meaning": "老师"},
            {"id": "w002", "kanji": "学生", "hiragana": "がくせい", "meaning": "学生"},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        # 助词题库换成临时文件：一条未到期 + 一条到期，用例不依赖真实题库内容
        (tmp / "particles.json").write_text(json.dumps({"questions": [
            {"id": "p001", "sentence": "わたし＿学生です。", "answer": "は",
             "mastery": 3, "next_due": (today + timedelta(days=30)).isoformat()},
            {"id": "p002", "sentence": "本＿読みます。", "answer": "を"},
        ]}, ensure_ascii=False), encoding="utf-8")
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR,
                     app.PARTICLES_PATH)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        app.PARTICLES_PATH = tmp / "particles.json"
        app.FOCUS_PATH.unlink(missing_ok=True)
        self.client = app.app.test_client()

    def tearDown(self):
        (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR,
         app.PARTICLES_PATH) = self._old
        self._tmp.cleanup()

    def test_int_field_clamps_bad_and_huge(self):
        self.assertEqual(app._int_field("abc", 20, lo=1), 20)
        self.assertEqual(app._int_field(None, 20, lo=1), 20)
        self.assertEqual(app._int_field(float("inf"), 20, lo=1), 20)
        self.assertEqual(app._int_field(0, 20, lo=1), 20)      # 沿用 `x or 默认` 的老口径
        self.assertEqual(app._int_field(10 ** 9, 20, lo=1, hi=500), 500)
        self.assertEqual(app._num_field("fast", 0.0), 0.0)

    def test_drill_count_falls_back_instead_of_500(self):
        for bad in ("abc", None, [], {"x": 1}, float("inf")):
            res = self.client.post("/api/start", json={
                "lesson": "lesson_01", "mode": "kanji_to_kana", "count": bad})
            self.assertEqual(res.status_code, 200, f"count={bad!r} 没被兜住")
            self.assertTrue(res.is_json)

    def test_bank_count_falls_back_instead_of_500(self):
        # 助词/真题走的是 start_bank 这条分支，单独兜
        for bad in ("abc", float("inf")):
            res = self.client.post("/api/start", json={
                "lesson": "all", "mode": "particle", "count": bad})
            self.assertEqual(res.status_code, 200, f"count={bad!r} 没被兜住")
            self.assertTrue(res.is_json)

    def test_finish_tolerates_bad_stats_and_results(self):
        res = self.client.post("/api/finish", json={
            "mode": "kanji_to_kana", "lesson": "lesson_01", "session_id": "guard1",
            "max_streak": "abc", "avg_reaction_ms": "fast", "review_count": "many",
            "results": [1, "two", None, {"ref": "lesson_01:w001", "correct": True}],
        })
        self.assertEqual(res.status_code, 200)
        stats = res.get_json()["stats"]
        self.assertEqual(stats["total"], 1, "非 dict 的 results 条目没被剔掉")
        self.assertEqual(stats["max_streak"], 0)
        self.assertEqual(stats["avg_reaction_ms"], 0.0)

    def test_api_404_is_json_not_html(self):
        res = self.client.get("/api/nope")
        self.assertEqual(res.status_code, 404)
        self.assertTrue(res.is_json, "/api/* 的 404 也该是 JSON")
        self.assertIn("error", res.get_json())


class TestRawInputWiring(unittest.TestCase):
    """每题一份 ui 里的 raw_input 必须被前端读到。

    只认本组下发的 data.raw_input 时，课程模式一旦出「看假名写汉字」，
    输入框的罗马字实时转换会把打进去的汉字当场改写、必然判错。
    """

    HERE = Path(__file__).resolve().parent

    def test_backend_stops_shipping_dead_cue(self):
        ui = app.mode_ui(app.get_mode("kanji_to_kana"))
        self.assertNotIn("cue", ui, "cue 是出题口径、前端不读，不该混在下发里")
        self.assertIn("raw_input", ui, "raw_input 要留着：前端按每题判定")

    def test_frontend_judges_per_question(self):
        js = (self.HERE / "static/app.js").read_text(encoding="utf-8")
        self.assertIn("function rawInputFor(q)", js)
        self.assertIn("ui.raw_input === undefined ? state.rawInput : !!ui.raw_input", js)
        self.assertIn("setAnswerPlaceholder(q);", js)
        self.assertIn("rawInputFor(currentQ()) ? input.value", js, "提交时的转换没按每题判")
        self.assertIn("if (rawInputFor(currentQ()) || e.isComposing) return;", js,
                      "输入框实时转换没按每题判")


class TestPortabilityAndHygiene(unittest.TestCase):
    """便携性与几处「同一个数字抄了多份」的收敛（P5/P6/P7）。"""

    HERE = Path(__file__).resolve().parent

    def _read(self, rel):
        return (self.HERE / rel).read_text(encoding="utf-8")

    def test_start_bat_falls_back_to_other_interpreters(self):
        """bat 里只能有「优先路径」，不能只有它：换机器没那个路径就起不来。"""
        bat = (self.HERE.parent / "start_practice.bat").read_bytes().decode("utf-8")
        self.assertIn("if exist", bat, "连本机优先解释器都没探测")
        self.assertIn("py -3", bat, "缺 py 启动器兜底")
        self.assertIn("python --version", bat, "缺 PATH 里 python 的兜底")

    def test_requirements_have_upper_bounds(self):
        """大版本升级会改行为（TTS 参数、分词结果、PDF 接口都改过）。"""
        text = (self.HERE.parent / "requirements.txt").read_text(encoding="utf-8")
        deps = [ln.strip() for ln in text.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
        self.assertTrue(deps, "requirements.txt 里没有依赖行")
        for ln in deps:
            self.assertRegex(ln, r"^[A-Za-z0-9_.\-]+\s*>=[^,]+,\s*<[^,\s]+",
                             f"{ln} 少了主版本上限")

    def test_learn_batch_single_source_of_truth(self):
        """「7 个新词」只能由 LEARN_BATCH_DEFAULT 派生：文案与按钮各抄一份会漂。"""
        desc = next(it["desc"] for it in app.EXTRA_ITEMS["course"]
                    if it["id"] == "learn")
        self.assertIn(str(app.LEARN_BATCH_DEFAULT), desc)
        self.assertNotIn("7 个未学词", self._read("app.py"),
                         "描述文案里又写死了新词数")
        js = self._read("static/app.js")
        self.assertIn("const LEARN_BATCH = 7;", js)
        self.assertNotIn("count: 7", js, "startLearn 又写死了每批词数")

    def test_shared_literals_collapsed(self):
        """几处同一常量抄多份的地方收敛掉（防漂）。"""
        js = self._read("static/app.js")
        self.assertIn("FOCUS_PICK_LIMIT", js)
        self.assertNotIn("slice(0, 300)", js, "候选上限又写成字面量了")
        self.assertIn('const MY_WORDS_LESSON = "lesson_mywords";', js)
        self.assertEqual(js.count('"lesson_mywords"'), 1,
                         "生词本课程 id 只该在常量定义处出现一次")


class TestPlanApi(unittest.TestCase):
    """今日处方（/api/plan）：步骤由当前数据算出来，且每步的参数真能开练。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        today = date.today()
        vocab = {"lessons": {"lesson_01": {"title": "第1课", "words": [
            # 未学（学新词步骤）
            {"id": "w001", "kanji": "先生", "hiragana": "せんせい", "meaning": "老师"},
            {"id": "w002", "kanji": "学生", "hiragana": "がくせい", "meaning": "学生"},
            # 练过且已逾期 5 天（复习步骤）
            {"id": "w003", "kanji": "学校", "hiragana": "がっこう", "meaning": "学校",
             "mastery": 2, "last_reviewed": (today - timedelta(days=10)).isoformat(),
             "next_due": (today - timedelta(days=5)).isoformat()},
            # 待会用它造一条错题记录（错题本步骤）
            {"id": "w004", "kanji": "図書館", "hiragana": "としょかん", "meaning": "图书馆"},
        ]}}}
        (tmp / "vocabulary.json").write_text(
            json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
        (tmp / "quizzes").mkdir()
        (tmp / "particles.json").write_text(json.dumps({"questions": [
            {"id": "p001", "sentence": "わたし＿学生です。", "answer": "は",
             "mastery": 3, "next_due": (today + timedelta(days=30)).isoformat()},
            {"id": "p002", "sentence": "本＿読みます。", "answer": "を"},
        ]}, ensure_ascii=False), encoding="utf-8")
        self._old = (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR,
                     app.PARTICLES_PATH)
        app.VOCAB_PATH = tmp / "vocabulary.json"
        app.QUIZZES_DIR = tmp / "quizzes"
        app.BASE_DIR = tmp
        app.PARTICLES_PATH = tmp / "particles.json"
        app.FOCUS_PATH.unlink(missing_ok=True)
        self.client = app.app.test_client()

    def tearDown(self):
        (app.VOCAB_PATH, app.QUIZZES_DIR, app.BASE_DIR,
         app.PARTICLES_PATH) = self._old
        app.FOCUS_PATH.unlink(missing_ok=True)
        self._tmp.cleanup()

    def _make_wrong(self):
        """造一条错题记录：错题本步骤据此出现（走真实交卷链路，不手写记录文件）。"""
        self.client.post("/api/finish", json={
            "mode": "kanji_to_kana", "lesson": "lesson_01", "session_id": "planwrong",
            "results": [{"ref": "lesson_01:w004", "correct": False}],
        })

    def test_steps_from_current_data_and_each_step_starts(self):
        self._make_wrong()
        plan = self.client.get("/api/plan").get_json()
        steps = {s["id"]: s for s in plan["steps"]}
        self.assertEqual([s["id"] for s in plan["steps"]],
                         ["review", "learn", "wrong", "particle"],
                         "处方顺序：复习 → 学新词 → 错题本 → 助词")
        # 剂量按当前数据算：复习 1 个到期、助词 1 题到期
        self.assertEqual(steps["review"]["count"], 1)
        self.assertEqual(steps["review"]["unit"], "个词")
        self.assertEqual(steps["learn"]["count"], app.LEARN_BATCH_DEFAULT)
        self.assertEqual(steps["particle"]["count"], 1)
        self.assertIn("最久逾期 5 天", steps["review"]["desc"])

        # 关键契约：每一步自带全部参数，且真能把练习开起来
        # （只给个 id 的话前端还得回配置页配一遍；参数不对就是点了报错）
        for st in plan["steps"]:
            for key in ("id", "title", "desc", "count", "unit", "start"):
                self.assertIn(key, st, f"步骤 {st['id']} 缺 {key}")
            self.assertIn(st["start"]["kind"], ("drill", "learn"))
            body = {k: v for k, v in st["start"].items() if k != "kind"}
            url = "/api/learn/start" if st["start"].get("kind") == "learn" else "/api/start"
            res = self.client.post(url, json=body)
            self.assertEqual(res.status_code, 200,
                             f"{st['id']} 的 start 参数开不了练："
                             f"{res.get_data(as_text=True)[:200]}")
            data = res.get_json()
            self.assertTrue(data.get("questions") or data.get("words"),
                            f"{st['id']} 开出来是空的")

    def test_focus_list_replaces_review_step(self):
        """重点词清单开着时课程范围已被它顶掉，不能再给一条按课程的复习步骤。"""
        app.save_focus(["lesson_01:w001", "lesson_01:w002"], True)
        ids = [s["id"] for s in self.client.get("/api/plan").get_json()["steps"]]
        self.assertEqual(ids[0], "focus")
        self.assertNotIn("review", ids)
        step = self.client.get("/api/plan").get_json()["steps"][0]
        self.assertTrue(step["start"]["focus"], "重点词步骤必须显式带 focus")

    def test_done_flag_comes_from_today_records(self):
        """打卡状态从当天练习记录现算，且每个桶归属正确（同一份记录只落进一个桶）。"""
        old_bank, old_wrong = app.PLAN_BANK_COUNT, app.PLAN_WRONG_COUNT
        app.PLAN_BANK_COUNT = app.PLAN_WRONG_COUNT = 1   # 一题就到剂量，便于断言
        try:
            first = {s["id"]: s for s in self.client.get("/api/plan").get_json()["steps"]}
            self.assertEqual(first["particle"]["done_today"], 0)
            self.assertFalse(first["particle"]["done"])
            self.assertFalse(first["review"]["done"])

            # 错题本练习（真实记录形状：lesson=lesson_wrong）→ 只算进 wrong 那一步
            self.client.post("/api/finish", json={
                "mode": "kana_to_cn", "lesson": app.WRONG_LESSON_ID, "session_id": "planw0",
                "results": [{"ref": "lesson_01:w004", "correct": False}]})
            steps = {s["id"]: s for s in self.client.get("/api/plan").get_json()["steps"]}
            self.assertEqual(steps["wrong"]["done_today"], 1)
            self.assertTrue(steps["wrong"]["done"], "做到剂量了却没打上卡")
            self.assertEqual(steps["review"]["done_today"], 0, "错题本的记录串进普通复习了")

            # 助词与普通词库练习各一笔 → 各归各的桶
            # （答 p001：它不到期，答完 p002 仍是到期题，助词那一步不会整步消失）
            self.client.post("/api/finish", json={
                "mode": "particle", "lesson": "particle", "session_id": "planp1",
                "results": [{"ref": "particle:p001", "correct": True}]})
            self.client.post("/api/finish", json={
                "mode": "kana_to_cn", "lesson": "all", "session_id": "planw1",
                "results": [{"ref": "lesson_01:w001", "correct": True}]})
            steps = {s["id"]: s for s in self.client.get("/api/plan").get_json()["steps"]}
            self.assertTrue(steps["particle"]["done"])
            self.assertEqual(steps["wrong"]["done_today"], 1, "助词那笔算进错题本了")
            self.assertEqual(steps["review"]["done_today"], 1)
        finally:
            app.PLAN_BANK_COUNT, app.PLAN_WRONG_COUNT = old_bank, old_wrong

    def test_huge_count_is_clamped(self):
        """重点词模式按题量循环补齐：不夹住的话一次请求就是几亿道题。"""
        app.save_focus(["lesson_01:w001", "lesson_01:w002"], True)
        res = self.client.post("/api/start", json={
            "lesson": "all", "mode": "kana_to_cn", "count": 10 ** 6}).get_json()
        self.assertEqual(res["total"], app.DRILL_COUNT_MAX)

    def test_empty_plan_when_nothing_to_do(self):
        today = date.today()
        tmp = Path(self._tmp.name)
        (tmp / "vocabulary.json").write_text(
            json.dumps({"lessons": {"lesson_01": {"title": "t", "words": [
                {"id": "w001", "kanji": "先生", "hiragana": "せんせい",
                 "meaning": "老师", "mastery": 5,
                 "next_due": (today + timedelta(days=30)).isoformat()},
            ]}}}, ensure_ascii=False), encoding="utf-8")
        (tmp / "particles.json").write_text(json.dumps(
            {"questions": [{"id": "p001", "sentence": "わたし＿学生です。",
                            "answer": "は", "mastery": 3,
                            "next_due": (today + timedelta(days=30)).isoformat()}]},
            ensure_ascii=False), encoding="utf-8")
        plan = self.client.get("/api/plan").get_json()
        self.assertEqual(plan["steps"], [], "没事可做时该给空处方（前端整块隐藏）")


class TestPlanFrontendWiring(unittest.TestCase):
    """今日处方的两端接线：前端硬编码模式列表或漏接线都会让功能静默不存在。"""

    HERE = Path(__file__).resolve().parent

    def test_home_has_plan_container(self):
        html = (self.HERE / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="plan"', html, "首页没有处方容器")

    def test_frontend_renders_done_state(self):
        """做完一项必须有打卡反馈（浅底 + ✓ + 「今天已做 N」），不能毫无变化。

        起因（真事）：处方原先不显示完成态，而「过一遍重点词」那一步的文案只跟清单
        长度有关——做完一遍页面上一个字都不变，看着像 bug。
        """
        js = (self.HERE / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('class="plan-step${s.done ? " done" : ""}', js)
        self.assertIn("s.done_today", js)
        self.assertIn("今天已做", js)
        css = (self.HERE / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".plan-step.done", css, "打卡样式没了，做完那项又会看不出区别")

    def test_frontend_fetches_plan_and_launches_step(self):
        js = (self.HERE / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('fetch("/api/plan")', js)
        self.assertIn('$("plan").addEventListener("click"', js)
        self.assertIn("function startPlanStep(stepId)", js)
        self.assertIn("planSteps.find((s) => s.id === stepId)", js)
        # 步骤参数必须原样带进 startSession/startLearn：池与薄弱过滤都要能覆盖，
        # 否则配置页停在「新词」时，「复习到期词」那一步会出成新词
        self.assertIn("overrides.pool", js)
        self.assertIn("overrides.focus_weak !== undefined", js)
        self.assertIn("overrides.lesson !== undefined ? overrides.lesson", js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
