"""后处理：从词表中排除明显不适合配图的词。"""
import csv
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_CSV = os.path.join(ROOT, "data", "vocab_image_list.csv")
OUTPUT_CSV = os.path.join(ROOT, "data", "vocab_image_list_filtered.csv")

# 排除规则
def should_exclude(word):
    kanji = word.get("kanji", "").strip()
    hiragana = word.get("hiragana", "").strip()
    meaning = word.get("meaning", "").strip()
    lesson_id = word.get("lesson_id", "").strip()

    # 1. 排除短语课程（lesson_duolingo_phrases 全是语法短语）
    if lesson_id == "lesson_duolingo_phrases":
        return True, "短语课程"

    # 2. 排除完全等于单个助词的词（不是以助词开头！）
    particles = {"を", "に", "と", "が", "は", "も", "で", "へ", "や", "か", "な", "よ", "ね", "わ", "ぞ", "ぜ", "の"}
    if hiragana in particles or kanji in particles:
        return True, "单个助词"

    # 3. 排除时间词
    time_keywords = ["每天", "每日", "每周", "每月", "每年", "今天", "明天", "昨天",
                      "前天", "后天", "本周", "下周", "上周", "这个月", "下个月", "上个月",
                      "今年", "明年", "去年", "星期", "周日", "周六", "周五", "周四",
                      "周三", "周二", "周一", "礼拜", "月份", "小时", "分钟", "秒",
                      "毎日", "毎朝", "毎晩", "今日", "明日", "昨日", "一時間", "四時"]
    for kw in time_keywords:
        if kw in meaning or kw in kanji or kw in hiragana:
            return True, f"时间词({kw})"

    # 4. 排除明确的形容词/副词（不排除颜色词）
    adj_keywords = ["好吃的", "美味的", "有趣的", "好玩的", "好好地", "很多", "许多",
                    "擅长", "不擅长", "真实", "真的", "假的", "对的", "错的",
                    "大的", "小的", "高的", "低的", "长的", "短的", "宽的", "窄的",
                    "厚的", "薄的", "深的", "浅的", "重的", "轻的", "快的", "慢的",
                    "早的", "晚的", "新的", "旧的", "好的", "坏的", "美的", "丑的",
                    "热闹的", "安静的", "繁华的", "荒凉的", "明亮的", "黑暗的",
                    "晴朗的", "阴沉的", "温暖的", "寒冷的", "炎热的", "凉爽的",
                    "潮湿的", "干燥的", "干净的", "肮脏的", "整洁的", "凌乱的",
                    "便宜的", "昂贵的", "低廉的", "高昂的", "经济的", "奢侈的",
                    "朴素的", "华丽的", "简朴的", "豪华的", "简单的", "复杂的",
                    "容易的", "困难的", "轻松的", "沉重的", "愉快的", "悲伤的",
                    "高兴的", "难过的", "快乐的", "痛苦的", "幸福的", "不幸的",
                    "幸运的", "倒霉的", "顺利的", "挫折的", "成功的", "失败的"]
    for kw in adj_keywords:
        if kw in meaning:
            return True, f"形容词/副词({kw})"

    # 5. 排除寒暄语/短语
    greeting_keywords = ["对不起", "不好意思", "请多关照", "明天见", "再见",
                         "你好", "谢谢", "不客气", "没关系", "没事", "欢迎",
                         "恭喜", "加油", "辛苦了", "我开动了", "我吃饱了",
                         "すみません", "よろしく", "さようなら", "また明日",
                         "こんにちは", "ありがとう", "いただきます", "ごちそうさま"]
    for kw in greeting_keywords:
        if kw in meaning or kw in kanji or kw in hiragana:
            return True, f"寒暄语/短语({kw})"

    # 6. 排除抽象名词（人、职业、家人、钱、语言等）
    abstract_keywords = ["日本人", "会社員", "公司职员", "学生", "先生", "老师",
                         "留学生", "医者", "医生", "工程师", "社长", "总经理",
                         "律师", "弁護士", "家族", "家人", "日本語", "日语",
                         "お金", "钱", "金钱", "暇", "空闲", "具合", "不舒服",
                         "上手", "擅长", "下手", "不擅长", "本当", "真实",
                         "沢山", "很多", "一緒に", "一起", "ちゃんと", "好好地",
                         "ところ", "地方", "もうすぐ", "快了", "次", "下一个"]
    for kw in abstract_keywords:
        if kw in meaning or kw in kanji or kw in hiragana:
            return True, f"抽象名词({kw})"

    return False, ""


def main():
    words = []
    with open(INPUT_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            words.append(row)

    filtered = []
    excluded = []
    for w in words:
        ex, reason = should_exclude(w)
        if ex:
            excluded.append((w, reason))
        else:
            filtered.append(w)

    print(f"原始词数: {len(words)}")
    print(f"过滤后: {len(filtered)}")
    print(f"排除: {len(excluded)}")
    print()
    print("排除的词:")
    for w, reason in excluded:
        print(f"  {w['lesson_id']}_{w['word_id']} | {w['kanji']} | {w['meaning'][:30]} | {reason}")

    # 保存过滤后的词表
    with open(OUTPUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["lesson_id","word_id","kanji","hiragana","romaji","meaning","category"])
        writer.writeheader()
        writer.writerows(filtered)
    print(f"\n过滤后词表已保存到: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
