# -*- coding: utf-8 -*-
"""从 vocabulary.json 与 quizzes/ 生成文档里的统计段落。

手写聚合数字一定会漂（词表从 466 涨到 1137 时 progress.md 就没跟上），
所以这部分交给脚本：

    python.exe practice\\report.py            # 打印到终端，看看当前数字
    python.exe practice\\report.py --write    # 回写 progress.md / weaknesses.md
    python.exe practice\\report.py --check    # 只判断生成区是否已过期，退出码非 0 即过期

比较时忽略生成头里的日期：日期天天变，把它算进差异会让「有无变化」永远是变化，
既报不出真正的数字漂移，也当不了回归锁。

md 文件里用注释圈出「生成区」，--write 只替换标记之间的内容，标记外的
手写散文（错因归纳、阶段目标等）一律不动：

    <!-- BEGIN GENERATED:vocab_stats -->
    ...脚本维护...
    <!-- END GENERATED:vocab_stats -->
"""
import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app  # 复用词表读写与 ref 解析，避免两处实现漂移

BASE_DIR = app.BASE_DIR
TODAY = datetime.now().strftime("%Y-%m-%d")

MARKER_RE = "<!-- BEGIN GENERATED:{name} -->"
MARKER_END = "<!-- END GENERATED:{name} -->"


def _dist_text(dist):
    return " / ".join(f"{k}分×{dist[k]}" for k in sorted(dist) if dist[k])


def vocab_stats_block():
    """各课程掌握度分布、例句覆盖率与两个独立题库的进度。

    「到期」拆成两列（复习到期 / 未学）：is_due 把从没练过的词也算到期，混在一个
    数里就是 1238 词永远「1233 到期」——复习做得再好那个数也不动，等于没有反馈。
    首页早就是两个轴（build_home_payload），报告这边跟着拆。
    """
    vocab = app.load_vocab()
    today = datetime.now().date()
    lines = ["| 课程 | 词条 | 掌握度分布 | 复习到期 | 未学 |",
             "|---|---|---|---|---|"]
    total = 0
    total_dist = Counter()
    with_example = 0
    review_due = unlearned = 0
    oldest_due = None
    for ldata in vocab.get("lessons", {}).values():
        words = ldata.get("words", [])
        dist = Counter(int(w.get("mastery") or 0) for w in words)
        total_dist.update(dist)
        total += len(words)
        with_example += sum(1 for w in words if w.get("example_ja"))
        # 逾期天数只有复习词有意义：未学词的 effective_due 是 date.min
        due_here = [w for w in words if app.is_learned(w) and app.is_due(w, today)]
        new_here = sum(1 for w in words if not app.is_learned(w))
        review_due += len(due_here)
        unlearned += new_here
        for w in due_here:
            due = app.effective_due(w)
            if oldest_due is None or due < oldest_due:
                oldest_due = due
        lines.append(
            f"| {ldata.get('title', '')} | {len(words)} | {_dist_text(dist) or '—'} "
            f"| {len(due_here)} | {new_here} |"
        )
    overdue_days = (today - oldest_due).days if oldest_due else 0
    lines.append(
        f"| **合计 {total} 词条** | | {_dist_text(total_dist)} | {review_due} | {unlearned} |"
    )
    lines.append("")
    lines.append(
        f"- 例句覆盖：{with_example}/{total} 词条带例句"
        + ("（全覆盖）" if with_example == total else "")
    )
    if not total:
        lines.append("- 词表为空")
    else:
        mastered = sum(m for k, m in total_dist.items() if k >= 4)
        lines.append(
            f"- 复习债：{review_due} 个练过的词已到期（最久逾期 {overdue_days} 天），"
            f"未学 {unlearned} 个"
        )
        lines.append(
            f"- 已掌握（掌握度 ≥4）{mastered} 个，占 {mastered / total:.0%}"
            "——这个数会随练习往上走，是真正的成绩线"
        )

    parts = []
    for name, path, ref_prefix in (
        ("助词题库", app.PARTICLES_PATH, "particle"),
        ("真题题库", app.EXAM_PATH, "exam"),
    ):
        if not path.exists():
            continue
        bank = json.loads(path.read_text(encoding="utf-8-sig"))
        qs = bank.get("questions", [])
        studied = [q for q in qs if q.get("last_reviewed")]
        dist = Counter(int(q.get("mastery") or 0) for q in qs)
        parts.append(
            f"{name} {len(qs)} 题（练过 {len(studied)} 题，{_dist_text(dist)}）"
        )
    if parts:
        lines.append("- " + "；".join(parts) + "。")
    return "\n".join(lines)


def all_words(vocab):
    return [(lesson_id, word)
            for lesson_id, ldata in vocab.get("lessons", {}).items()
            for word in ldata.get("words", [])]


def mode_counts_block():
    """各词库题型在当前全词库上的可出题词数。

    README 与 weaknesses.md 里手抄的「N 个词条可出题」以这里的输出为准：
    这类数字随词表增长必漂，两个文件各抄一份就更对不上。
    """
    vocab = app.load_vocab()
    words = all_words(vocab)
    lines = ["| 题型 | 可出题 |", "|---|---|"]
    eligible = {}
    for mode in app.PRACTICE_MODES:
        if mode.get("module") != "drill":
            continue  # 课程模式与独立题库（助词/真题）不走词表筛选
        hits = [w for lid, w in words if app.mode_accepts(w, mode, lesson_id=lid)]
        eligible[mode["id"]] = hits
        lines.append(f"| {mode['name']} | {len(hits)} |")

    kanji_hits = eligible.get("kana_to_kanji", [])
    by_reading = Counter(w.get("hiragana", "") for w in kanji_hits)
    amb = sum(1 for w in kanji_hits if by_reading[w.get("hiragana", "")] > 1)
    lines += [
        "",
        f"- 「看假名写汉字」的 {len(kanji_hits)} 个里，{amb} 个读音在全词库撞车"
        "（同音多形），题干只给读音无法唯一判分——这就是必须带释义的原因。",
        "- 配图题型的计数随本机 `practice/static/images/vocab/` 里的图片数变化"
        "（该目录不入 git，换机后需重新生成才会对上）。",
    ]
    return "\n".join(lines)


def quiz_totals():
    """扫描练习记录，返回 (组数, 题数, 最近记录日期)。"""
    sessions = questions = 0
    latest = ""
    if not app.QUIZZES_DIR.exists():
        return 0, 0, "—"
    for f in sorted(app.QUIZZES_DIR.glob("*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        n = int(rec.get("total", 0) or len(rec.get("results", [])))
        sessions += 1
        questions += n
        ts = rec.get("timestamp", "")
        if ts > latest:
            latest = ts
    return sessions, questions, (latest[:10] or "—")


def wrong_words_block(min_wrong=2, limit=20):
    """累计答错次数达阈值的词表类词条（真题/助词的错题按题库掌握度另算）。"""
    counts = Counter()
    last_wrong = {}
    for f in sorted(app.QUIZZES_DIR.glob("*.json")) if app.QUIZZES_DIR.exists() else []:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        ts = rec.get("timestamp", "")
        for r in rec.get("results", []):
            ref = r.get("ref", "")
            if r.get("correct", False) or not ref.startswith("lesson_"):
                continue
            counts[ref] += 1
            if ts > last_wrong.get(ref, ""):
                last_wrong[ref] = ts
    vocab = app.load_vocab()
    rows = []
    for ref, n in counts.items():
        if n < min_wrong:
            continue
        resolved = app.resolve_word(vocab, ref)
        if not resolved:
            continue  # 词条已删除
        w = resolved[1]
        kanji = w.get("kanji", "")
        hira = w.get("hiragana", "")
        # 纯片假名词的读音字段本身就是片假名，转成平假名展示才有信息量
        reading = app.to_hiragana(hira) if app.has_katakana(hira) else hira
        rows.append((
            kanji if kanji not in ("", "---") else hira,
            reading,
            w.get("meaning", ""),
            n,
            last_wrong[ref],
        ))
    rows.sort(key=lambda r: -r[3])
    sessions, questions, latest = quiz_totals()
    lines = [
        f"来源：`quizzes/` 共 {sessions} 组 {questions} 题（截至 {latest}）。"
        f"以下单词累计答错 ≥ {min_wrong} 次：",
        "",
        "| 单词 | 读音 | 释义 | 错误次数 |",
        "|---|---|---|---|",
    ]
    if rows:
        lines += [f"| {k} | {h} | {m} | {n} |" for k, h, m, n, _ in rows[:limit]]
    else:
        lines.append("| （暂无达阈值的错词） | | | |")
    return "\n".join(lines)


BLOCKS = {
    "vocab_stats": vocab_stats_block,
    "mode_counts": mode_counts_block,
    "wrong_words": wrong_words_block,
}

TARGETS = {
    BASE_DIR / "progress.md": ["vocab_stats", "mode_counts"],
    BASE_DIR / "weaknesses.md": ["wrong_words"],
}


def render(name):
    header = f"_（本节由 `python practice/report.py --write` 生成于 {TODAY}，请勿手改）_"
    return header + "\n\n" + BLOCKS[name]()


def _replace_block(text, name, body):
    begin = MARKER_RE.format(name=name)
    end = MARKER_END.format(name=name)
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(text):
        raise ValueError(f"标记缺失：{name}")
    return pattern.sub(lambda _: f"{begin}\n{body}\n{end}", text, count=1)


def _extract_block(text, name):
    begin = MARKER_RE.format(name=name)
    end = MARKER_END.format(name=name)
    m = re.compile(re.escape(begin) + r"(.*?)" + re.escape(end), re.DOTALL).search(text)
    return m.group(1) if m else None


# 生成头里的日期天天变，不是内容漂移；比较前统一抹成占位符
GEN_DATE_RE = re.compile(r"生成于 \d{4}-\d{2}-\d{2}")


def _comparable(chunk):
    return GEN_DATE_RE.sub("生成于 <日期>", chunk or "").strip()


def stale_blocks(path, names):
    """返回内容与文件里已有生成区不一致的块名（忽略生成日期）。"""
    text = path.read_text(encoding="utf-8")
    return [
        name for name in names
        if _comparable(_extract_block(text, name)) != _comparable(
            "\n" + render(name) + "\n")
    ]


def write_blocks():
    stale_total = 0
    for path, names in TARGETS.items():
        stale = stale_blocks(path, names)
        if not stale:
            print(f"无变化 {path.relative_to(BASE_DIR)}")
            continue
        text = path.read_text(encoding="utf-8")
        for name in stale:
            text = _replace_block(text, name, render(name))
        path.write_text(text, encoding="utf-8")
        stale_total += len(stale)
        print(f"已更新 {path.relative_to(BASE_DIR)}：{', '.join(stale)}")
    return stale_total


def check_blocks():
    """--check：只报哪些生成区已过期，不写文件。返回过期块数。"""
    stale_total = 0
    for path, names in TARGETS.items():
        rel = path.relative_to(BASE_DIR)
        if not path.exists():
            print(f"缺失 {rel}")
            stale_total += len(names)
            continue
        for name in stale_blocks(path, names):
            print(f"过期 {rel} ← GENERATED:{name}（跑 report.py --write 刷新）")
            stale_total += 1
    if not stale_total:
        print("生成区都是最新的")
    return stale_total


def main():
    parser = argparse.ArgumentParser(description="生成文档用的学习统计")
    parser.add_argument("--write", action="store_true",
                        help="回写 progress.md / weaknesses.md 的生成区（只替换标记之间的内容）")
    parser.add_argument("--check", action="store_true",
                        help="只检查生成区是否过期并以此决定退出码，不改动文件")
    args = parser.parse_args()
    if args.check:
        sys.exit(1 if check_blocks() else 0)
    if args.write:
        write_blocks()
        return
    for name in BLOCKS:
        print(f"===== {name} =====")
        print(render(name))
        print()


if __name__ == "__main__":
    main()
