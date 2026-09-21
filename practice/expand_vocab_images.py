"""第二轮配图扩词：人工审阅全词表后补充的关键词表漏掉的词。

第一轮管线（analyze_vocab_images.py + filter_vocab_images.py）只靠关键词匹配，
1137 词只筛出 314 词。本脚本收录人工逐词审阅后追加的可视图词，
把词条（从 vocabulary.json 取全字段）追加到 data/vocab_image_list_filtered.csv。

用法：
  python practice/expand_vocab_images.py            # 追加（已存在的自动跳过，幂等）
  python practice/expand_vocab_images.py --dry-run  # 只打印，不写文件
"""
import argparse
import csv
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOCAB = os.path.join(ROOT, "vocabulary.json")
TARGET = os.path.join(ROOT, "data", "vocab_image_list_filtered.csv")

# {lesson_id: {word_id: category}}
EXTRA = {
    "lesson_duolingo": {
        "w021": "object",       # ゲーム
        "w022": "object",       # 魔法
        "w069": "object",       # バスケットボール
        "w070": "object",       # サッカー
        "w071": "object",       # 音楽
        "w073": "object",       # 雑誌
        "w078": "object",       # 野球
        "w085": "food",         # トースト
        "w101": "object",       # 財布
        "w111": "object",       # 円
        "w115": "place",        # 店
        "w139": "object",       # バレーボール
        "w145": "verb_visual",  # ヨガ
        "w154": "object",       # シャワー
        "w172": "object",       # テニス
        "w182": "verb_visual",  # 柔道
        "w200": "place",        # コンサート
        "w204": "food",         # ココア
        "w208": "food",         # 定食
        "w242": "vehicle",      # タクシー
        "w244": "place",        # エレベーター
        "w246": "place",        # 階段
        "w247": "object",       # コンセント
        "w254": "object",       # コインロッカー
        "w265": "object",       # お風呂
        "w312": "object",       # 薬
        "w317": "food",         # デザート
        "w327": "food",         # 誕生日
        "w330": "object",       # ネックレス
        "w334": "clothing",     # ドレス
        "w353": "verb_visual",  # 観光
        "w355": "place",        # 広場
        "w374": "food",         # ソース
        "w379": "food",         # コーラ
        "w391": "place",        # バンド
        "w392": "object",       # アルバム
        "w394": "object",       # パズル
        "w400": "place",        # 受付
        "w409": "place",        # 食堂
        "w417": "object",       # カラオケ
    },
    "lesson_n5": {
        # 季节/天气/自然
        "w008": "nature",       # 秋
        "w012": "nature",       # 朝
        "w052": "nature",       # 池
        "w216": "nature",       # 曇り
        "w265": "nature",       # 咲く
        "w452": "nature",       # 夏
        "w507": "nature",       # 春
        "w509": "nature",       # 晴れ
        "w547": "nature",       # 冬
        "w548": "verb_visual",  # 降る
        "w630": "nature",       # 夕方
        # 身体
        "w015": "body",         # 足
        "w020": "body",         # 頭
        "w148": "body",         # 顔
        "w173": "body",         # 体
        "w480": "body",         # 歯
        # 颜色
        "w223": "color",        # 黒
        "w224": "color",        # 黒い
        "w599": "color",        # 緑
        # 动物
        "w263": "animal",       # 魚
        "w419": "animal",       # 動物
        "w439": "animal",       # 鳥
        "w554": "animal",       # ぺっと
        # 交通工具
        "w222": "vehicle",      # 車
        "w283": "vehicle",      # 自転車
        "w411": "vehicle",      # 電車
        "w494": "vehicle",      # ばす
        "w522": "vehicle",      # 飛行機
        # 服饰
        "w086": "clothing",     # 上着
        "w238": "clothing",     # こーと
        "w293": "clothing",     # しゃつ
        "w308": "clothing",     # すかーと
        "w317": "clothing",     # ずぼん
        "w323": "clothing",     # せーたー
        "w472": "clothing",     # ねくたい
        # 食物
        "w038": "food",         # 飴
        "w101": "food",         # お菓子
        "w129": "food",         # お弁当
        "w176": "food",         # かれー
        "w239": "food",         # こーひー
        "w274": "food",         # 塩
        "w364": "food",         # 食べ物
        "w369": "food",         # 誕生日
        "w477": "food",         # 飲み物
        "w495": "food",         # ばたー
        "w513": "food",         # ぱん
        # 文具/纸张类
        "w094": "stationery",   # 鉛筆
        "w169": "stationery",   # 紙
        "w396": "stationery",   # てーぷ
        "w475": "stationery",   # のーと
        "w485": "stationery",   # 葉書
        "w537": "stationery",   # 封筒
        "w557": "stationery",   # ぺん
        "w560": "stationery",   # ぼーるぺん
        "w589": "stationery",   # 万年筆
        # 建筑物/场所
        "w031": "building",     # あぱーと
        "w092": "place",        # 駅
        "w142": "building",     # 会社
        "w197": "place",        # 教室
        "w206": "building",     # 銀行
        "w230": "place",        # 玄関
        "w233": "place",        # 公園
        "w236": "building",     # 交番
        "w301": "place",        # 食堂
        "w346": "building",     # 大学
        "w347": "building",     # 大使館
        "w351": "place",        # 台所
        "w359": "building",     # 建物
        "w405": "place",        # でぱーと
        "w415": "place",        # といれ
        "w428": "building",     # 図書館
        "w469": "place",        # 庭
        "w481": "place",        # ぱーてぃー
        "w488": "building",     # 橋
        "w538": "place",        # ぷーる
        "w555": "place",        # 部屋
        "w567": "building",     # ほてる
        "w594": "place",        # 店
        "w596": "place",        # 道
        "w609": "place",        # 村
        "w618": "building",     # 門
        "w621": "place",        # 八百屋
        "w632": "building",     # 郵便局
        "w660": "place",        # れすとらん
        "w661": "place",        # 廊下
        # 物品
        "w087": "object",       # 絵
        "w102": "object",       # お金
        "w107": "object",       # お皿
        "w111": "object",       # 円
        "w128": "object",       # お風呂
        "w150": "object",       # 鍵
        "w155": "object",       # 傘
        "w163": "object",       # かっぷ
        "w166": "object",       # かばん
        "w170": "object",       # かめら
        "w177": "object",       # かれんだー
        "w187": "object",       # ぎたー
        "w191": "object",       # 切符
        "w209": "object",       # 薬
        "w248": "object",       # こっぷ
        "w268": "object",       # 雑誌
        "w278": "object",       # 辞書
        "w294": "object",       # しゃわー
        "w297": "object",       # 宿題
        "w305": "object",       # 新聞
        "w314": "object",       # すとーぶ
        "w315": "object",       # すぷーん
        "w388": "object",       # 机
        "w397": "object",       # てーぷれこーだー
        "w398": "object",       # てーぶる
        "w408": "object",       # てれび
        "w410": "object",       # 電気
        "w412": "object",       # 電話
        "w413": "object",       # 戸
        "w414": "object",       # どあ
        "w424": "object",       # 時計
        "w446": "object",       # ないふ
        "w467": "object",       # 荷物
        "w489": "object",       # 箸
        "w514": "object",       # はんかち
        "w536": "object",       # ふぃるむ
        "w539": "object",       # ふぉーく
        "w553": "object",       # べっど
        "w564": "object",       # ぽすと
        "w566": "object",       # ぼたん
        "w585": "object",       # まっち
        "w586": "object",       # 窓
        "w650": "object",       # らじお
        "w658": "object",       # 冷蔵庫
        "w659": "object",       # れこーど
        # 动词（可視化）
        "w009": "verb_visual",  # 開く
        "w010": "verb_visual",  # 開ける
        "w011": "verb_visual",  # 上げる
        "w018": "verb_visual",  # 遊ぶ
        "w033": "verb_visual",  # 浴びる
        "w042": "verb_visual",  # 歩く
        "w080": "verb_visual",  # 歌う
        "w084": "verb_visual",  # 売る
        "w110": "verb_visual",  # 教える
        "w111": "verb_visual",  # 押す
        "w134": "verb_visual",  # 泳ぐ
        "w135": "verb_visual",  # 降りる
        "w144": "verb_visual",  # 買い物
        "w145": "verb_visual",  # 買う
        "w147": "verb_visual",  # 帰る
        "w151": "verb_visual",  # 書く
        "w153": "verb_visual",  # 掛ける
        "w168": "verb_visual",  # かぶる
        "w201": "verb_visual",  # 切る
        "w202": "verb_visual",  # 着る
        "w227": "verb_visual",  # 消す
        "w288": "verb_visual",  # 閉まる
        "w289": "verb_visual",  # 閉める
        "w320": "verb_visual",  # 座る
        "w357": "verb_visual",  # 立つ
        "w365": "verb_visual",  # 食べる
        "w385": "verb_visual",  # 疲れる
        "w389": "verb_visual",  # 作る
        "w390": "verb_visual",  # つける
        "w399": "verb_visual",  # 出かける
        "w435": "verb_visual",  # 飛ぶ
        "w442": "verb_visual",  # 撮る
        "w470": "verb_visual",  # 脱ぐ
        "w474": "verb_visual",  # 寝る
        "w476": "verb_visual",  # 登る
        "w478": "verb_visual",  # 飲む
        "w479": "verb_visual",  # 乗る
        "w486": "verb_visual",  # はく
        "w497": "verb_visual",  # 働く
        "w508": "verb_visual",  # 貼る
        "w519": "verb_visual",  # 引く
        "w520": "verb_visual",  # 弾く
        "w578": "verb_visual",  # 曲る
        "w583": "verb_visual",  # 待つ
        "w590": "verb_visual",  # 磨く
        "w616": "verb_visual",  # 持つ
        "w626": "verb_visual",  # 休む
        "w644": "verb_visual",  # 読む
    },
    # ---- 第三轮扩充（2026-09-05）：全词表人工复审（629 个未配图词逐词过）----
    # 原则：图必须能无歧义对应单词。不配：人/职业/家人（沿用既有口径）、数词、
    # 时间词、寒暄、国家、图里必然出现文字的词（片仮名/漢字/作文等）、
    # 以及图会与已配图名词完全无法区分的动词（晴れる/曇る vs 晴れ/曇り）。
    "lesson_01": {
        "w009": "building",     # 大学（lesson_n5 已配，本课 ref 补齐）
        "w012": "verb_visual",  # 出迎え（举接机牌）
    },
    "lesson_duolingo": {
        "w012": "color",        # 白い（第一轮漏掉的颜色词）
        "w075": "object",       # ニュース（电视新闻）
        "w124": "place",        # 監獄
        "w296": "object",       # 危ない（警告三角）
        "w300": "vehicle",      # 警察（警车）
        "w326": "verb_visual",  # デート（咖啡馆约会）
        "w384": "object",       # セール（折扣标签）
        "w402": "verb_visual",  # ミーティング（会议桌）
        "w415": "verb_visual",  # プレゼン（演讲）
    },
    "lesson_n5": {
        # 空间方位：红球+木箱示意图（一套视觉语言，位置一一对应）
        "w076": "object",       # 上（球在箱上）
        "w280": "object",       # 下（球在箱下）
        "w447": "object",       # 中（球在箱里）
        "w339": "object",       # 外（球在箱外）
        "w577": "object",       # 前（球在箱前）
        "w433": "object",       # 隣（球与箱并排）
        # 概念图标
        "w034": "object",       # 危ない（警告三角）
        "w074": "object",       # 色（调色盘）
        "w117": "object",       # 男（男性符号）
        "w138": "object",       # 女（女性符号）
        "w517": "object",       # 半分（切半的苹果）
        "w562": "object",       # ぽけっと（牛仔裤口袋）
        "w587": "object",       # 丸い（圆形色块）
        # 场所/物品（部分是已有 SPECIAL_SUBJECTS 但漏进清单的词）
        "w081": "place",        # うち（房子外观）
        "w093": "place",        # えれべーたー
        "w143": "place",        # 階段（duolingo 已配，本 ref 补齐）
        "w157": "nature",       # 風
        "w262": "object",       # 財布（duolingo 已配，本 ref 补齐）
        "w355": "vehicle",      # たくしー（duolingo 已配，本 ref 补齐）
        "w362": "object",       # たばこ
        "w468": "object",       # にゅーす
        "w619": "building",     # 門（原 w618 条目是 id 笔误，实际配到了「物」上）
        "w645": "nature",       # 夜
        # 动作
        "w158": "verb_visual",  # 風邪（打喷嚏）
        "w436": "verb_visual",  # 止まる（红灯停车）
        "w459": "verb_visual",  # 並ぶ（排队）
    },
    "lesson_mywords": {
        "w001": "body",         # 喉（喉咙部位图）
        "w002": "verb_visual",  # 渇いた（口渴：汗+空杯，避开与「飲む」的图冲突）
    },
    # ---- 第四轮扩充：新课程的可视词 ----
    # 其余新词（機能/利用/登場/発売/開始/決定/プレミアム/サブスクライブ 等）
    # 都是抽象 UI 概念，按第三轮口径不配。
    "lesson_user_01": {
        "w001": "object",       # 広告（广告牌）
        "w015": "object",       # グッズ（周边商品）
        "w016": "object",       # イラスト（插画）
        "w018": "clothing",     # パーカー（连帽衫）
    },
}


def main():
    parser = argparse.ArgumentParser(description="第二轮配图扩词")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写入")
    args = parser.parse_args()

    with open(VOCAB, "r", encoding="utf-8") as f:
        vocab = json.load(f)

    # 已在词表中的不再追加（幂等）
    existing = set()
    if os.path.exists(TARGET):
        with open(TARGET, "r", encoding="utf-8-sig") as f:
            existing = {(r["lesson_id"], r["word_id"]) for r in csv.DictReader(f)}

    new_rows = []
    skipped = []
    for lid, cat_map in EXTRA.items():
        words = {w["id"]: w for w in vocab["lessons"][lid]["words"]}
        for wid, cat in cat_map.items():
            if (lid, wid) in existing:
                skipped.append((lid, wid))
                continue
            w = words[wid]
            new_rows.append({
                "lesson_id": lid, "word_id": wid,
                "kanji": w.get("kanji", ""), "hiragana": w.get("hiragana", ""),
                "romaji": w.get("romaji", ""), "meaning": w.get("meaning", ""),
                "category": cat,
            })

    print(f"新增: {len(new_rows)}, 已存在跳过: {len(skipped)}")
    by_cat = {}
    for r in new_rows:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    for c, n in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"  {c}: {n}")

    if args.dry_run:
        for r in new_rows:
            print(f"  {r['lesson_id']}_{r['word_id']} | {r['kanji'] or r['hiragana']} | {r['meaning'][:24]} | {r['category']}")
        return

    with open(TARGET, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["lesson_id", "word_id", "kanji", "hiragana",
                           "romaji", "meaning", "category"])
        writer.writerows(new_rows)
    print(f"已追加到: {TARGET}")


if __name__ == "__main__":
    main()
