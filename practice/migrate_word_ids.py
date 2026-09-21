# -*- coding: utf-8 -*-
"""一次性迁移：把「课程:下标」引用全面迁移为「课程:稳定id」引用。

迁移内容（可重复运行，幂等）：
1. vocabulary.json：每个单词分配稳定 id（课内 w001 起，只补缺失不重排），
   并回填 next_due（按旧规则 last_reviewed + 间隔 推算，保持现有复习计划不变）。
2. quizzes/*.json：results[].ref 由 "lesson:下标" 改写为 "lesson:id"。
3. practice/audio/*.mp3：按 ref 对应关系重命名（lesson_01_0.mp3 -> lesson_01_w001.mp3）。

迁移后词表可自由插入/重排单词，历史记录与音频不受影响。
运行: python.exe practice\\migrate_word_ids.py
"""
import json
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app


def main():
    vocab = app.load_vocab()  # load_vocab 已自动补 id
    lessons = vocab["lessons"]

    # 旧下标 ref -> 新 id ref 的映射（按当前词序）
    ref_map = {}
    for lid, ldata in lessons.items():
        for i, w in enumerate(ldata["words"]):
            ref_map[f"{lid}:{i}"] = f"{lid}:{w['id']}"
    total_words = sum(len(l["words"]) for l in lessons.values())
    print(f"[1/3] 词条 id 分配完成: {total_words} 词")

    # 回填 next_due（与旧版 is_due 的推算方式一致：last_reviewed + INTERVAL_DAYS[mastery]）
    filled = 0
    for ldata in lessons.values():
        for w in ldata["words"]:
            if w.get("next_due"):
                continue
            due = None
            last = w.get("last_reviewed")
            if last:
                try:
                    d = datetime.strptime(last, "%Y-%m-%d").date()
                    interval = app.INTERVAL_DAYS.get(int(w.get("mastery", 0)), 1)
                    due = (d + timedelta(days=interval)).isoformat()
                except (ValueError, TypeError):
                    due = None
            if due:
                filled += 1
            # 插到 last_reviewed 之后，保持字段顺序可读
            reordered = {}
            for k, v in w.items():
                reordered[k] = v
                if k == "last_reviewed":
                    reordered["next_due"] = due
            if "next_due" not in reordered:
                reordered["next_due"] = due
            w.clear()
            w.update(reordered)
    print(f"[2/3] next_due 回填: {filled} 个有复习记录，其余置空（视为到期）")
    app.save_vocab(vocab)

    # 改写 quizzes/*.json 中的 ref
    changed_files = 0
    changed_refs = 0
    for f in sorted(app.QUIZZES_DIR.glob("*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                rec = json.load(fp)
        except (json.JSONDecodeError, OSError):
            continue
        dirty = False
        for r in rec.get("results", []):
            old = r.get("ref", "")
            if old in ref_map:
                r["ref"] = ref_map[old]
                dirty = True
                changed_refs += 1
        if dirty:
            with open(f, "w", encoding="utf-8") as fp:
                json.dump(rec, fp, ensure_ascii=False, indent=2)
            changed_files += 1
    print(f"      quizzes 改写: {changed_files} 个文件 / {changed_refs} 条 ref")

    # 重命名音频缓存
    renamed = 0
    for old_ref, new_ref in ref_map.items():
        old_p = app.audio_path_for_ref(old_ref)
        new_p = app.audio_path_for_ref(new_ref)
        if old_p.exists() and not new_p.exists():
            old_p.replace(new_p)
            renamed += 1
    print(f"[3/3] 音频重命名: {renamed} 个")

    print("迁移完成。")


if __name__ == "__main__":
    main()
