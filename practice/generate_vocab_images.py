"""
批量生成单词配图，使用 SenseNova U1.5 Lite API。

用法：
  # 小批量测试（前15个词）
  python practice/generate_vocab_images.py --limit 15

  # 全量生成
  python practice/generate_vocab_images.py

  # 指定课程
  python practice/generate_vocab_images.py --lesson lesson_duolingo

环境变量：
  SENSENOVA_API_KEY  必填，SenseNova API Key

图片输出：
  practice/static/images/vocab/{lesson_id}_{word_id}.png
"""
import argparse
import base64
import csv
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

# 路径配置
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
VOCAB_CSV = os.path.join(PROJECT_ROOT, "data", "vocab_image_list_filtered.csv")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "practice", "static", "images", "vocab")

# API 配置
API_URL = "https://token.sensenova.cn/v1/images/generations"
MODEL = "sensenova-u1.5-lite"
IMAGE_SIZE = "1024x1024"
OUTPUT_FORMAT = "png"
WATERMARK = False
PROMPT_EXTEND = True

# 生成 prompt 模板
def clean_meaning(meaning):
    """清理中文释义：去括号、取第一释义、去结尾'的'。"""
    import re
    m = meaning.strip()
    # 去括号及括号内内容（中英文括号）
    m = re.sub(r'[（(][^）)]*[）)]', '', m)
    # 取分号/逗号/顿号前的第一释义
    m = re.split(r'[；;，,、]', m)[0]
    # 去结尾的"的"
    m = re.sub(r'的$', '', m)
    return m.strip()


# 同形异义词特殊处理：用明确的英文描述避免误生成
SPECIAL_SUBJECTS = {
    "水": "a clear glass of drinking water",
    "日": "the sun shining in a clear sky",
    "本": "a closed hardcover book",
    "口": "an open human mouth",
    "眼": "a human eye",
    "目": "a human eye",
    "耳": "a human ear",
    "鼻": "a human nose",
    "手": "a human hand",
    "足": "a human foot",
    "山": "a mountain peak with snow",
    "川": "a flowing river with clear water",
    "河": "a flowing river",
    "海": "the ocean with blue waves",
    "花": "a blooming red flower",
    "木": "a green leafy tree",
    "空": "a clear blue sky with clouds",
    "雨": "falling raindrops on window",
    "雪": "falling snowflakes in winter",
    "風": "wind blowing through green trees",
    "月": "the full moon in night sky",
    "星": "twinkling stars in dark night sky",
    "火": "a burning orange flame",
    "土": "a pile of brown soil",
    "玉": "a green jade gemstone",
    "石": "a smooth gray stone",
    "岩": "a large gray rock",
    "砂": "a pile of yellow sand",
    "氷": "a clear ice cube",
    "雲": "a fluffy white cloud",
    "霧": "thick white fog in forest",
    "虹": "a colorful rainbow in sky",
    "雷": "lightning striking in dark sky",
    "光": "a beam of warm sunlight",
    "影": "a shadow cast on ground",
    "春": "spring cherry blossoms",
    "夏": "summer beach with blue sky",
    "秋": "autumn red maple leaves",
    "冬": "winter snow covered landscape",
    "朝": "morning sunrise over horizon",
    "昼": "noon bright sun overhead",
    "晩": "evening sunset with orange sky",
    "夜": "night sky with stars and moon",
    # ---- 第二轮扩充（2026-09-03）：新词里的同形/多义/易误解词 ----
    "頭": "a human head in profile",
    "顔": "a smiling human face",
    "体": "a full human body outline",
    "歯": "a single white tooth",
    "池": "a calm pond surrounded by trees",
    "庭": "a Japanese garden with stone lantern and plants",
    "村": "a small rural village with houses and fields",
    "道": "a country road stretching to the horizon",
    "門": "a traditional Japanese wooden gate",
    "橋": "a wooden arch bridge over a river",
    "戸": "a traditional Japanese wooden sliding door",
    "鳥": "a small sparrow perched on a branch",
    "魚": "a single orange goldfish",
    "薬": "white pills and a medicine bottle",
    "鍵": "a golden door key",
    "傘": "an open blue umbrella",
    "窓": "an open window with white curtains",
    "机": "a wooden study desk with a lamp",
    "箸": "a pair of wooden chopsticks",
    "塩": "a glass salt shaker",
    "紙": "a stack of white paper sheets",
    "絵": "a framed painting on an easel",
    "上着": "a casual jacket",
    "電気": "a glowing yellow light bulb",
    "時計": "a round analog wall clock",
    "財布": "a brown leather wallet",
    "お金": "a pile of gold coins",
    "円": "a silver Japanese yen coin",
    "玄関": "the entranceway of a Japanese home",
    "交番": "a small Japanese police koban box",
    "大使館": "an embassy building with national flags",
    "八百屋": "a Japanese greengrocer shop with vegetables",
    "郵便局": "a Japanese post office building",
    "食堂": "a school cafeteria dining hall",
    "廊下": "a long bright school corridor",
    "部屋": "a tidy Japanese bedroom with futon",
    "台所": "a traditional Japanese kitchen",
    "駅": "a train station platform with a train",
    "会社": "a modern office building",
    "大学": "a university campus building",
    "図書館": "a library building with bookshelves",
    "教室": "a classroom with desks and blackboard",
    "公園": "a green park with trees and a bench",
    "広場": "a town plaza with a fountain",
    "銀行": "a bank building with columns",
    "受付": "a hotel reception desk",
    "コンサート": "a concert stage with spotlights",
    "バンド": "a rock band performing on stage",
    "ぱーてぃー": "a birthday party with balloons and cake",
    "カラオケ": "a wireless karaoke microphone",
    "ゲーム": "a video game controller",
    "魔法": "a magic wand with sparkles",
    "音楽": "colorful musical notes",
    "パズル": "a colorful jigsaw puzzle",
    "アルバム": "an open photo album",
    "ネックレス": "a pearl necklace",
    "コインロッカー": "a row of coin-operated lockers",
    "コンセント": "a white wall power outlet",
    "階段": "a wooden staircase",
    "エレベーター": "an elevator with open doors",
    "シャワー": "a shower head with running water",
    "お風呂": "a bathtub filled with water",
    "すとーぶ": "a small kerosene stove heater",
    "てれび": "a flat-screen television",
    "電話": "a smartphone",
    "冷蔵庫": "a refrigerator with food inside",
    "べっど": "a neatly made bed",
    "てーぶる": "a wooden dining table",
    "荷物": "a suitcase and travel bags",
    "はんかち": "a folded handkerchief",
    "万年筆": "an elegant fountain pen",
    "ぼたん": "a round shirt button",
    "ぽすと": "a red Japanese mailbox",
    "れこーど": "a black vinyl record",
    "てーぷれこーだー": "a vintage cassette tape recorder",
    "てーぷ": "a roll of adhesive tape",
    "かれんだー": "a wall calendar with a photo",
    "かめら": "a digital camera",
    "切符": "a train ticket",
    "新聞": "a folded newspaper",
    "雑誌": "an open fashion magazine",
    "辞書": "a thick open dictionary",
    "のーと": "a spiral notebook",
    "葉書": "a picture postcard",
    "封筒": "a white envelope",
    "ふぃるむ": "a roll of 35mm film",
    "ぺっと": "a cute puppy and kitten",
    "トースト": "two slices of toasted bread",
    "ココア": "a warm cup of cocoa with marshmallows",
    "定食": "a Japanese set meal on a tray",
    "デザート": "a plate of colorful desserts",
    "ソース": "a bottle of brown sauce",
    "コーラ": "a glass of cola with ice",
    "誕生日": "a birthday cake with lit candles",
    "お菓子": "an assortment of Japanese sweets",
    "お弁当": "a Japanese bento boxed lunch",
    "かれー": "a plate of Japanese curry rice",
    "飴": "colorful wrapped hard candies",
    "食べ物": "a table full of delicious dishes",
    "飲み物": "an assortment of drinks in glasses",
    "ぱん": "freshly baked bread loaves",
    "ばたー": "a stick of butter on a dish",
    "こーひー": "a cup of hot coffee",
    "かっぷ": "a ceramic coffee cup",
    "こっぷ": "a clear drinking glass",
    "お皿": "a white ceramic plate",
    "ふぉーく": "a silver fork",
    "すぷーん": "a silver spoon",
    "ないふ": "a kitchen knife",
    "こーと": "a winter coat",
    "ドレス": "an elegant evening dress",
    "すかーと": "a pleated skirt",
    "ずぼん": "a pair of trousers",
    "せーたー": "a knitted sweater",
    "しゃつ": "a white dress shirt",
    "ねくたい": "a silk necktie",
    "バスケットボール": "an orange basketball",
    "サッカー": "a black and white soccer ball",
    "バレーボール": "a blue and yellow volleyball",
    "野球": "a baseball and wooden bat",
    "テニス": "a tennis racket with a ball",
    "タクシー": "a yellow taxi",
    "ばす": "a city bus",
    "電車": "a Japanese commuter train",
    "飛行機": "an airplane",
    "自転車": "a bicycle",
    "車": "a silver sedan car",
    "ほてる": "a modern hotel building exterior with clear entrance",
    # ---- 第三轮扩充（2026-09-05）：全词表人工复审补充的假名/外来语词与图标类 ----
    "うち": "a cozy Japanese suburban house with a red roof",
    "えれべーたー": "an elevator with open doors and warm light inside",
    "たくしー": "a yellow taxi",
    "ぽけっと": "a close-up of blue denim jeans front pocket",
    "にゅーす": "a TV screen showing a news anchor at the news desk",
    "ニュース": "a TV screen showing a news anchor at the news desk",
    "たばこ": "a pack of cigarettes with two loose cigarettes",
    "半分": "an apple cut in half showing the cross section with seeds",
    "丸い": "a solid gray circle shape",
    "男": "a blue male gender symbol, circle with an arrow pointing diagonally up-right",
    "女": "a pink female gender symbol, circle with a cross below",
    "色": "a wooden artist palette with colorful paint blobs and a paint brush",
    "喉": "a clear medical illustration of the human throat and neck area",
    # ---- 第四轮扩充：新课程新词 ----
    "広告": "a large outdoor advertising billboard with a colorful poster",
    "グッズ": "a collection of character merchandise goods with keychains badges and plush toys",
    "イラスト": "a colorful cartoon illustration of a cat drawn on a sheet of paper",
    "パーカー": "a hoodie sweatshirt with a hood and a front pocket",
}


# 整条 prompt 覆盖：用于按类别模板会触发内容审核或明显跑偏的词
SPECIAL_PROMPTS = {
    # 「お腹」按 food 模板会被理解成裸腹，触发 sensitive image
    "お腹": "a cute cartoon person gently holding their belly with both hands, "
            "simple flat illustration, white background, fully clothed, no text, no watermark",
    # 「ホテル」分类成 food 会让模型理解成「ホテルパン」（烘焙术语），生成面包
    "ホテル": "a modern hotel building exterior with clear entrance and signage, "
              "white background, simple flat illustration style, no text, no watermark",
    # ---- 第二轮扩充：动词需要具体动作场景，短语主体直接写清 ----
    "開く": "a simple flat illustration of a door standing open, white background, no text, no watermark",
    "開ける": "a simple flat illustration of a hand opening a door, white background, no text, no watermark",
    "上げる": "a simple flat illustration of a person raising their hand high, white background, no text, no watermark",
    "遊ぶ": "a simple flat illustration of children playing with a ball in a park, white background, no text, no watermark",
    "歩く": "a simple flat illustration of a person walking, side view, white background, no text, no watermark",
    "売る": "a simple flat illustration of a shopkeeper handing goods to a customer over a counter, white background, no text, no watermark",
    "歌う": "a simple flat illustration of a person singing into a microphone, white background, no text, no watermark",
    "押す": "a simple flat illustration of a finger pressing a doorbell button, white background, no text, no watermark",
    "泳ぐ": "a simple flat illustration of a person swimming in water, freestyle stroke, white background, no text, no watermark",
    "降りる": "a simple flat illustration of a person stepping off a bus, white background, no text, no watermark",
    "買い物": "a simple flat illustration of a person carrying colorful shopping bags, white background, no text, no watermark",
    "買う": "a simple flat illustration of a person paying at a cash register, white background, no text, no watermark",
    "帰る": "a simple flat illustration of a person walking toward their home at dusk, white background, no text, no watermark",
    "書く": "a simple flat illustration of a hand writing with a pen on paper, white background, no text, no watermark",
    "掛ける": "a simple flat illustration of a picture being hung on a wall, white background, no text, no watermark",
    "かぶる": "a simple flat illustration of a person putting a cap on their head, white background, no text, no watermark",
    "切る": "a simple flat illustration of a knife cutting vegetables on a cutting board, white background, no text, no watermark",
    "着る": "a simple flat illustration of a person putting on a T-shirt, white background, no text, no watermark",
    "消す": "a simple flat illustration of a hand erasing pencil lines with an eraser, white background, no text, no watermark",
    "閉まる": "a simple flat illustration of a door closing shut, white background, no text, no watermark",
    "閉める": "a simple flat illustration of hands sliding a door shut, white background, no text, no watermark",
    "座る": "a simple flat illustration of a person sitting on a chair, side view, white background, no text, no watermark",
    "立つ": "a simple flat illustration of a person standing up straight, side view, white background, no text, no watermark",
    "食べる": "a simple flat illustration of a person eating rice with chopsticks, white background, no text, no watermark",
    "疲れる": "a simple flat illustration of a tired office worker rubbing their shoulders, white background, no text, no watermark",
    "作る": "a simple flat illustration of hands making a paper craft, white background, no text, no watermark",
    "つける": "a simple flat illustration of a hand switching on a wall light switch, white background, no text, no watermark",
    "出かける": "a simple flat illustration of a person leaving home with a shoulder bag, white background, no text, no watermark",
    "飛ぶ": "a simple flat illustration of a bird flying in the sky, white background, no text, no watermark",
    "撮る": "a simple flat illustration of a person taking a photo with a camera, white background, no text, no watermark",
    "登る": "a simple flat illustration of a hiker climbing up a mountain, white background, no text, no watermark",
    "飲む": "a simple flat illustration of a person drinking a glass of water, white background, no text, no watermark",
    "乗る": "a simple flat illustration of a person boarding a train, white background, no text, no watermark",
    "働く": "a simple flat illustration of a person working at a desk with a laptop, white background, no text, no watermark",
    "待つ": "a simple flat illustration of a person checking their watch while waiting, white background, no text, no watermark",
    "持つ": "a simple flat illustration of a person carrying a bag by its handle, white background, no text, no watermark",
    "読む": "a simple flat illustration of a person sitting and reading a book, white background, no text, no watermark",
    "休む": "a simple flat illustration of a person resting on a sofa, white background, no text, no watermark",
    "曲る": "a simple flat illustration of a winding road curving through hills, top view, no text, no watermark",
    "はく": "a simple flat illustration of a person putting on trousers, white background, no text, no watermark",
    "脱ぐ": "a simple flat illustration of a person taking off a jacket, white background, no text, no watermark",
    "磨く": "a simple flat illustration of a person brushing their teeth with a toothbrush, white background, no text, no watermark",
    "引く": "a simple flat illustration of a hand pulling open a drawer, white background, no text, no watermark",
    "弾く": "a simple flat illustration of hands playing a grand piano, white background, no text, no watermark",
    "貼る": "a simple flat illustration of hands sticking a poster onto a wall, white background, no text, no watermark",
    "咲く": "a close-up of a pink cherry blossom flower in full bloom, pure white background, no text, no watermark",
    "降る": "a simple flat illustration of raindrops falling from gray clouds, white background, no text, no watermark",
    "ヨガ": "a simple flat illustration of a person doing a yoga lotus pose, white background, no text, no watermark",
    "柔道": "a simple flat illustration of two judo practitioners in white gi performing a throw, white background, no text, no watermark",
    "観光": "a simple flat illustration of a tourist taking photos in front of a famous landmark, white background, no text, no watermark",
    "動物": "a cute elephant and a giraffe standing together on pure white background, soft illustration, no text, no watermark",
    # 「灰皿」原先被分到 color 类（含「灰」）→ 生成了一张纯灰色方块。
    # 它是具体物件，改回 object 并固定英文主体，免得模型理解成「灰色的盘子」
    "灰皿": "a single clean ceramic ashtray, centered on pure white background, realistic product photo, soft even lighting, sharp focus, no text, no watermark",
    # ---- 第三轮扩充（2026-09-05）：空间方位一套「红球+木箱」视觉语言，概念词用图标 ----
    "上": "a red ball on top of a wooden box, simple flat illustration showing the concept of above, white background, no text, no watermark",
    "下": "a red ball on the ground under a wooden box, simple flat illustration showing the concept of below, white background, no text, no watermark",
    "中": "a red ball inside an open wooden box, simple flat illustration showing the concept of inside, white background, no text, no watermark",
    "外": "a red ball on the ground next to a wooden box, simple flat illustration showing the concept of outside, white background, no text, no watermark",
    "前": "a red ball on the ground in front of a wooden box, simple flat illustration showing the concept of in front of, white background, no text, no watermark",
    "隣": "a red ball and a wooden box side by side on the ground, simple flat illustration showing the concept of next to, white background, no text, no watermark",
    "危ない": "a yellow triangle warning sign with a black exclamation mark symbol, flat design, white background, no text, no watermark",
    "セール": "a red price discount tag with a percent symbol, flat design, white background, no text, no watermark",
    "監獄": "a simple flat illustration of a prison building with barred windows and a watchtower, white background, no text, no watermark",
    "出迎え": "a simple flat illustration of a person holding a blank welcome sign at an airport arrival gate, white background, no text, no watermark",
    "デート": "a simple flat illustration of a young couple holding hands at a cafe table with two cups of coffee, white background, no text, no watermark",
    "ミーティング": "a simple flat illustration of business people sitting around a conference table having a meeting, white background, no text, no watermark",
    "プレゼン": "a simple flat illustration of a person giving a presentation pointing at a rising chart on a screen, white background, no text, no watermark",
    "風邪": "a simple flat illustration of a sick person with a red nose sneezing into a tissue, white background, no text, no watermark",
    "止まる": "a simple flat illustration of a red car stopped at a traffic light, white background, no text, no watermark",
    "並ぶ": "a simple flat illustration of people standing in a queue line, side view, white background, no text, no watermark",
    "警察": "a police car with flashing lights on the roof, white background, simple flat illustration, no text, no watermark",
    "渇いた": "a simple flat illustration of a thirsty person with sweat drops holding an upside down empty glass, white background, no text, no watermark",
}


# 颜色词本体：形容词去「い」、名词去「色」后落在这些字里，才算真·颜色词
COLOR_STEMS = {
    "赤", "紅", "青", "緑", "黄", "白", "黒", "紫", "桃", "橙",
    "茶", "灰", "金", "銀", "藍", "紺", "朱",
}
# 释义是颜色才算（整义项比对，避免 "goldfish" 这类被 gold 子串误命中）
COLOR_MEANINGS = {
    "red", "blue", "green", "yellow", "white", "black", "purple", "pink",
    "orange", "brown", "gray", "grey", "gold", "silver", "color", "colour",
    "红", "蓝", "绿", "黄", "白", "黑", "紫", "粉", "橙", "茶", "灰", "金", "银",
}


def color_word(subject, meaning):
    """该词条是不是真·颜色词——只有它为真才生成纯色块。

    不能只看「颜色汉字 in 词形」：那样「面白い」含「白」→ 纯白色方块、
    「金曜日」含「金」→ 纯金色方块、「灰皿」含「灰」→ 纯灰色方块，
    三张图跟词义毫无关系。判定不通过时退回按类别的通用 prompt。
    """
    stem = subject[:-1] if subject.endswith("い") else subject
    for _ in range(2):  # 最多剥两层：「黄色い」→「黄色」→「黄」
        if stem in COLOR_STEMS:
            return True
        if stem.endswith("色"):
            stem = stem[:-1]
            continue
        break
    for part in re.split(r"[,，、;；/]", meaning or ""):
        if part.strip().rstrip("的色") in COLOR_MEANINGS:
            return True
    return False


def build_prompt(word):
    """根据词条生成英文 prompt。"""
    meaning = word.get("meaning", "").strip()
    kanji = word.get("kanji", "").strip()
    hiragana = word.get("hiragana", "").strip()
    category = word.get("category", "").strip()

    # 优先用日语词（之前验证大部分都正确）
    subject = kanji if kanji and kanji != "---" else hiragana

    # 整条 prompt 覆盖优先
    if subject in SPECIAL_PROMPTS:
        return SPECIAL_PROMPTS[subject]

    # 同形异义词用特殊处理
    if subject in SPECIAL_SUBJECTS:
        subject = SPECIAL_SUBJECTS[subject]

    # 不同类别用不同的 prompt 风格
    # 标成 color 但并非颜色词的（面白い/金曜日/灰皿 这类含颜色汉字却另有所指），
    # 不能生成纯色块——退回后面的按类别通用分支
    if category == "color" and color_word(subject, meaning):
        # 颜色词：纯色块
        color_map = {
            "赤": "red", "红": "red", "青": "blue", "蓝": "blue",
            "緑": "green", "绿": "green", "黄": "yellow", "白": "white",
            "黒": "black", "黑": "black", "紫": "purple", "粉": "pink",
            "橙": "orange", "茶": "brown", "灰": "gray", "金": "gold",
            "银": "silver",
        }
        color_en = "color"
        for jp, en in color_map.items():
            if jp in subject or jp in meaning:
                color_en = en
                break
        return f"a solid {color_en} color square, centered on white background, flat design, no text, no watermark"

    elif category == "verb_visual":
        # 动词：简单场景插画
        return f"a simple illustration of a person doing '{subject}', clear action, white background, flat cartoon style, no text, no watermark"

    elif category == "animal":
        # 动物：写实风格
        return f"a single {subject} animal, centered on pure white background, realistic photo, soft even lighting, sharp focus, no text, no watermark"

    elif category == "food":
        # 食物：写实风格
        return f"a single fresh {subject}, food, centered on pure white background, realistic product photo, soft even lighting, sharp focus, no text, no watermark"

    elif category == "place":
        # 地点：插画风格
        return f"a simple illustration of {subject}, place or location, white background, flat cartoon style, clear and recognizable, no text, no watermark"

    elif category == "body":
        # 身体部位：写实/插画
        return f"a single {subject}, human body part, centered on white background, clear medical illustration style, no text, no watermark"

    elif category == "nature":
        # 自然：写实/插画
        return f"a single {subject}, nature element, centered on white background, realistic illustration, no text, no watermark"

    elif category == "clothing":
        # 服装：写实
        return f"a single {subject}, clothing item, centered on pure white background, realistic product photo, no text, no watermark"

    elif category == "vehicle":
        # 交通工具：写实
        return f"a single {subject}, vehicle, centered on pure white background, realistic product photo, no text, no watermark"

    elif category == "building":
        # 建筑：插画
        return f"a simple illustration of {subject}, building, white background, flat cartoon style, no text, no watermark"

    elif category == "stationery":
        # 文具：写实
        return f"a single {subject}, stationery item, centered on pure white background, realistic product photo, no text, no watermark"

    else:
        # 默认：物品
        return f"a single {subject}, object, centered on pure white background, realistic product photo, soft even lighting, sharp focus, no text, no watermark"


class QuotaExceeded(Exception):
    """额度/限流用尽：不可重试，直接中止整轮生成。"""


QUOTA_HINTS = ("quota", "rate limit", "rate_limit", "ratelimit",
               "exceed", "insufficient", "超出", "限额", "额度", "配额")


def _is_quota_error(code, body):
    """判断 HTTP 错误是否为额度/限流类（重试无意义）。"""
    if code in (402, 403, 429):
        return True
    low = (body or "").lower()
    return any(h in low for h in QUOTA_HINTS)


def call_api(prompt, api_key, retries=3):
    """调用 SenseNova API 生成图片，返回 base64 数据。"""
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "n": 1,
        "size": IMAGE_SIZE,
        "output_format": OUTPUT_FORMAT,
        "response_format": "b64_json",
        "watermark": WATERMARK,
        "prompt_extend": PROMPT_EXTEND,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if "data" in result and len(result["data"]) > 0:
                    b64 = result["data"][0].get("b64_json", "")
                    if b64:
                        return b64
                raise ValueError(f"API 返回格式异常: {str(result)[:200]}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            if _is_quota_error(e.code, body):
                raise QuotaExceeded(f"API HTTP {e.code}: {body}")
            if attempt < retries - 1:
                print(f"  HTTP {e.code}，重试 {attempt+1}/{retries}...")
                time.sleep(2 * (attempt + 1))
            else:
                raise RuntimeError(f"API HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                print(f"  网络错误，重试 {attempt+1}/{retries}...")
                time.sleep(2 * (attempt + 1))
            else:
                raise RuntimeError(f"网络错误: {e}")
        except Exception as e:
            if attempt < retries - 1:
                print(f"  错误: {e}，重试 {attempt+1}/{retries}...")
                time.sleep(2 * (attempt + 1))
            else:
                raise


def save_image(b64_data, output_path):
    """保存 base64 图片到文件。"""
    img_bytes = base64.b64decode(b64_data)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(img_bytes)


def load_word_list(lesson_filter=None, word_filter=None):
    """加载词表 CSV。

    word_filter: 形如 "lesson_n5_w122"，也允许只给 "w122" 或汉字/假名写法。
    """
    words = []
    with open(VOCAB_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if lesson_filter and row["lesson_id"] != lesson_filter:
                continue
            if word_filter:
                key = f'{row["lesson_id"]}_{row["word_id"]}'
                hit = (word_filter == key or word_filter == row["word_id"]
                       or word_filter in (row.get("kanji", ""), row.get("hiragana", "")))
                if not hit:
                    continue
            words.append(row)
    return words


def main():
    parser = argparse.ArgumentParser(description="批量生成单词配图")
    parser.add_argument("--limit", type=int, default=0, help="只生成前 N 个词（0=全部）")
    parser.add_argument("--lesson", type=str, default="", help="只生成指定课程")
    parser.add_argument("--word", type=str, default="", help="只生成指定词（lesson_id_word_id / word_id / 词形），常配合 --force 重生成")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的图片")
    parser.add_argument("--delay", type=float, default=1.0, help="每次请求间隔秒数")
    args = parser.parse_args()

    # 检查 API Key
    api_key = os.environ.get("SENSENOVA_API_KEY", "").strip()
    if not api_key:
        print("错误：未设置环境变量 SENSENOVA_API_KEY")
        print("请先运行：$env:SENSENOVA_API_KEY=\"your_key\"  (PowerShell)")
        print("或：export SENSENOVA_API_KEY=\"your_key\"  (Linux/Mac)")
        sys.exit(1)

    # 加载词表
    words = load_word_list(args.lesson if args.lesson else None,
                           args.word if args.word else None)
    if args.limit > 0:
        words = words[:args.limit]

    print(f"待生成词数: {len(words)}")
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"模型: {MODEL}")
    print(f"尺寸: {IMAGE_SIZE}")
    print()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    success = 0
    skipped = 0
    failed = 0
    failed_words = []
    quota_stop = False

    for i, word in enumerate(words, 1):
        lesson_id = word["lesson_id"]
        word_id = word["word_id"]
        kanji = word.get("kanji", "")
        meaning = word.get("meaning", "")
        filename = f"{lesson_id}_{word_id}.png"
        output_path = os.path.join(OUTPUT_DIR, filename)

        # 跳过已存在
        if os.path.exists(output_path) and not args.force:
            print(f"[{i}/{len(words)}] 跳过 {filename} (已存在)")
            skipped += 1
            continue

        prompt = build_prompt(word)
        print(f"[{i}/{len(words)}] 生成 {filename} | {kanji} ({meaning})")
        print(f"  prompt: {prompt[:80]}...")

        try:
            b64 = call_api(prompt, api_key)
            save_image(b64, output_path)
            file_size = os.path.getsize(output_path)
            print(f"  成功 ({file_size//1024} KB)")
            success += 1
        except QuotaExceeded as e:
            print(f"  额度/限流用尽，中止本轮: {e}")
            print("  （已生成的图片均已落盘，额度恢复后直接重跑即可续传）")
            quota_stop = True
            break
        except Exception as e:
            print(f"  失败: {e}")
            failed += 1
            failed_words.append({"lesson_id": lesson_id, "word_id": word_id,
                                 "kanji": kanji, "meaning": meaning, "error": str(e)})

        # 请求间隔
        if i < len(words):
            time.sleep(args.delay)

    # 汇总
    print()
    print("=" * 50)
    if quota_stop:
        print(f"因额度/限流中止。成功: {success}, 跳过: {skipped}, 失败: {failed}")
    else:
        print(f"完成！成功: {success}, 跳过: {skipped}, 失败: {failed}")
    if failed_words:
        print(f"\n失败的词（{len(failed_words)}个）:")
        for fw in failed_words:
            print(f"  {fw['lesson_id']}_{fw['word_id']} | {fw['kanji']} | {fw['error'][:60]}")
        # 保存失败列表
        failed_path = os.path.join(PROJECT_ROOT, "data", "vocab_image_failed.json")
        with open(failed_path, "w", encoding="utf-8") as f:
            json.dump(failed_words, f, ensure_ascii=False, indent=2)
        print(f"\n失败列表已保存到: {failed_path}")


if __name__ == "__main__":
    main()
