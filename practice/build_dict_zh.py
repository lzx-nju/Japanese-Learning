#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量把 JMdict 的英文释义预翻成中文，落盘 data/dict_zh.json。

一次性离线工具：跑完就有本地中文词典，日常查词不再联网、不再依赖 API。
设计要点：
- **API Key 只从环境变量 SENSENOVA_API_KEY 读**（与配图脚本同名），启动即校验；
  不写入任何文件、不打印、不进 git、不写日志。缺失直接退出，不存在"没 key
  也能跑一半"的中间态。
- 关闭思考模式（reasoning_effort=none）+ 低温度 + 批量打包：翻译任务不需要
  推理，开着思考会白白烧掉几倍 token。
- 断点续跑：每批翻完就落盘，中断后重跑只补缺的部分，不重复烧 token。
- 先跑 --sample 看质量与真实消耗，再决定全量翻多少。

用法（key 由你自己在终端里设置，不要写进任何文件）:
    set SENSENOVA_API_KEY=sk-...
    python practice\\build_dict_zh.py --sample 60    # 先跑小样验证
    python practice\\build_dict_zh.py --limit 3000   # 翻前 3000 条
    python practice\\build_dict_zh.py                # 翻完全部 common 词条
    python practice\\build_dict_zh.py --from-zip X:\\path\\jmdict-eng-common.json.zip
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE_DIR / "data" / "jmdict"        # 原始词典（体积大，不入库）
OUT_PATH = BASE_DIR / "data" / "dict_zh.json"  # 翻译结果（入库）

API_URL = "https://token.sensenova.cn/v1/chat/completions"
MODEL = "deepseek-v4-flash"
# JMdict 只发布欧洲语言包（eng/dut/fre/ger/hun/rus/slv/spa/swe），**没有中文**，
# 这正是要本地预翻的原因。各语言包体积：eng-common 1.4MB / eng 11MB / all 24MB。
# 发布包文件名带版本号（jmdict-eng-common-3.6.2+20260907165411.json.zip），
# 所以先查 release API 拿真实文件名，再下载。
API_RELEASE = "https://api.github.com/repos/scriptin/jmdict-simplified/releases/latest"
SCOPE_PREFIX = {"common": "jmdict-eng-common", "eng": "jmdict-eng", "all": "jmdict-all"}

BATCH_DEFAULT = 40      # 每批词条数：打包越大越省 prompt 开销，但太长易截断
RETRY = 3               # 单批失败重试次数
SLEEP_OK = 0.8          # 批间间隔初值（秒）；撞限流后翻倍，顺畅后缓慢回落
SLEEP_MAX = 20          # 批间间隔上限（秒）
SLEEP_429 = 8           # 撞上速率限制后的退避（秒）
SLEEP_QUOTA = 45        # 撞上额度上限后的退避（秒）：等窗口回补，退太短没意义


class ApiError(Exception):
    """接口错误：带上 type 与 message，用来区分「速率」和「额度」。

    两者都是 429，但应对方式完全不同：速率限制退几秒就恢复；额度用尽要等
    5 小时窗口回补，硬重试只会白白把重试次数耗光还一条都翻不出来。
    """

    def __init__(self, status, type_, message):
        super().__init__(f"HTTP {status} {type_}: {message}")
        self.status = status
        self.type = type_ or ""
        self.message = message or ""

    @property
    def is_quota(self):
        return ("quota" in self.type.lower()
                or "quota" in self.message.lower()
                or "额度" in self.message)

# 质量优先于 token：每个义项带上词性一起给模型（同形异读、俚语、委婉义都靠
# 词性和读音消歧），并附一条示例把输出格式钉死。
# 义项必须是数组对象：试过压成「a;b」的文本行，模型对多义项的对应会乱掉。
SYSTEM_PROMPT = (
    "你是资深日中词典编纂者，为中国的日语学习者把 JMdict 的英文释义译成简体中文。\n"
    "输入：词条数组。k=日文写法，r=读音，s=义项数组（p=词性，e=英文释义）。\n"
    "输出：{\"r\":[{\"i\":序号,\"z\":[\"中文义项1\",\"中文义项2\"]}]}\n"
    "硬性要求：\n"
    "1. z 与 s 严格一一对应，个数必须相同，不得合并或拆分义项；\n"
    "2. 只译释义本身：不写词性、不加解释、不加例句、不加引号、不重复日文；\n"
    "3. 中国大陆惯用说法，简洁准确，每义项不超过 12 字；\n"
    "4. to do / -ing 形式译为中文动词原形（to run → 奔跑）；\n"
    "5. 结合读音与词性判断义项：同形异读、俚语义、委婉义都要如实译出；\n"
    "6. 外来语与专名用通行译法（CD player → CD播放器）。\n"
    "示例：\n"
    "输入 [{\"i\":1,\"k\":\"走る\",\"r\":\"はしる\","
    "\"s\":[{\"p\":[\"v5r\"],\"e\":\"to run\"},{\"p\":[\"v5r\"],\"e\":\"to operate\"}]}]\n"
    "输出 {\"r\":[{\"i\":1,\"z\":[\"奔跑，跑步\",\"运营，经营\"]}]}"
)


def die(msg):
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(1)


def load_keys():
    """从环境变量读 key（支持多枚轮询），缺失即退出。

    只认环境变量：SENSENOVA_API_KEYS（多枚，逗号/分号/空格分隔）或
    SENSENOVA_API_KEY（单枚）。绝不回落到任何文件或默认值，也绝不打印明文。
    """
    raw = os.environ.get("SENSENOVA_API_KEYS", "").strip()
    keys = [k for k in re.split(r"[,;\s]+", raw) if k]
    if not keys:
        one = os.environ.get("SENSENOVA_API_KEY", "").strip()
        keys = [one] if one else []
    if not keys:
        die("未设置 SENSENOVA_API_KEYS（多枚用逗号分隔）或 SENSENOVA_API_KEY。"
            "请在终端里 set 后再跑（不要写进任何文件）。")
    seen, uniq = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


class KeyPool:
    """多 key 轮询：被限流的那枚单独冷却，其余继续干活。

    限流若是按 key 计，多枚轮询就等于把总吞吐乘以 key 数——比傻等退避有效得多。
    日志里只用编号（key#3）指代，不碰明文。
    """

    def __init__(self, keys):
        self.keys = keys
        self.i = 0
        self.cool_until = {}
        self.hits = {}

    def label(self, key):
        try:
            return f"key#{self.keys.index(key) + 1}"
        except ValueError:
            return "key#?"

    def pick(self):
        """返回 (key, 需等待秒数)：优先没在冷却的；全在冷却就等最早恢复的那枚。"""
        now = time.time()
        alive = [k for k in self.keys if self.cool_until.get(k, 0) <= now]
        if alive:
            k = alive[self.i % len(alive)]
            self.i += 1
            return k, 0.0
        soonest = min(self.keys, key=lambda x: self.cool_until.get(x, 0))
        wait = self.cool_until.get(soonest, 0) - now
        if wait > 600:
            # 十分钟以上基本是无效 key 而非限流，别干等
            raise RuntimeError("所有 key 都不可用（无效或长期限流）")
        return soonest, max(0.0, wait)

    def cooldown(self, key, seconds):
        self.cool_until[key] = time.time() + seconds

    def note(self, key):
        self.hits[key] = self.hits.get(key, 0) + 1


# 人工兜底：这批词的英文释义是日语语法术语，中文译文天然要引用假名
# （「イ形容词」「表示所属的「の」」），会被 _zh_ok 判为「夹带假名」而**永远**
# 过不了自检——模型每次翻出来都对，却每次都被退回。逐条人工译好写死在这里，
# 生成时直接采用（义项数对不上就跳过，宁可缺一条也不写错序号）。
MANUAL_ZH = {
    "形容詞|けいようし": ["形容词，イ形容词"],
    "甲乙|こうおつ": ["甲与乙，第一和第二", "优劣，高下，辨别"],
    "骨抜き|ほねぬき": ["去骨，剔骨", "（计划、法案等）被架空，被削弱", "使失去骨气"],
    "込める|こめる": ["装填（枪炮等），上膛", "倾注（感情、努力）",
                    "包含（如价格中含税）", "笼罩，弥漫"],
    "籠める|こめる": ["装填（枪炮等），上膛", "倾注（感情、努力）",
                    "包含（如价格中含税）", "笼罩，弥漫"],
    "此処|ここ": ["这里，此处", "这一点，此时",
                 "过去这…（如这三年）", "接下来这…（如这几天）"],
    "此所|ここ": ["这里，此处", "这一点，此时",
                 "过去这…（如这三年）", "接下来这…（如这几天）"],
    "乃|の": ["表示所属（相当于「の」）", "使动词、形容词名词化",
             "在从句中代替「が」", "表示确信的断定", "表示情感强调", "表示疑问"],
    "之|の": ["表示所属（相当于「の」）", "使动词、形容词名词化",
             "在从句中代替「が」", "表示确信的断定", "表示情感强调", "表示疑问"],
    "さ|さ": ["（接形容词）表程度、性质", "表示断言", "（催促）来呀，走吧", "（迟疑）呃，嗯",
             "表示质疑或反驳", "表示传闻", "（招呼）喂", "方言中代替各种助词",
             "相当于标准语中的「に」「へ」"],
}


# ---------- 词典下载与解析 ----------

def _get(url, timeout=60):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "curl/8"}), timeout=timeout)


def pick_asset(scope):
    """查 release API 拿到真实文件名（发布包都带版本号）。"""
    prefix = SCOPE_PREFIX[scope]
    data = json.loads(_get(API_RELEASE).read().decode("utf-8"))
    hits = [a for a in data.get("assets", [])
            if a.get("name", "").startswith(prefix + "-")
            and a.get("name", "").endswith(".json.zip")
            and (("-common-" in a["name"]) == (scope == "common"))]
    if not hits:
        die(f"release {data.get('tag_name')} 里没找到 {prefix} 的 zip 包")
    asset = hits[0]
    print(f"最新 release {data.get('tag_name')}：{asset['name']}"
          f"（{asset['size'] // 1024}KB）")
    return asset["name"], asset["browser_download_url"]


def download_zip(dest_dir, scope):
    """下载 JMdict 发布包（本地已有则直接复用），返回 zip 路径。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name, url = pick_asset(scope)
    local = dest_dir / name
    if local.exists() and local.stat().st_size > 1024:
        print(f"已存在词典包：{local.name}")
        return local
    print(f"下载 {url} ...")
    try:
        with _get(url, timeout=120) as r, open(local, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done // 1024}KB / {total // 1024}KB", end="")
        print()
    except (urllib.error.URLError, OSError) as exc:
        if local.exists():
            local.unlink()
        die(f"下载失败：{exc}\n可手动下载后放到 data/jmdict/ 下，"
            f"或用 --from-zip 指定路径。")
    if local.stat().st_size <= 1024:
        local.unlink()
        die("下载到的文件过小，可能不是词典包。")
    print(f"下载完成：{local.name}（{local.stat().st_size // 1024}KB）")
    return local


def load_jmdict(zip_path):
    """解压并读取 JMdict JSON，返回 words 列表。"""
    extracted = CACHE_DIR / (zip_path.stem.replace(".json", "") + ".json")
    if not extracted.exists():
        print(f"解压 {zip_path.name} ...")
        with zipfile.ZipFile(zip_path) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".json"))
            extracted.write_bytes(zf.read(name))
    print(f"读取 {extracted.name} ...")
    return json.loads(extracted.read_text(encoding="utf-8")).get("words", [])


def _gloss_text(g):
    """英文释义文本：3.x 用 text，早期版本用 value，两个都认。"""
    return (g.get("text") or g.get("value") or "").strip()


def extract(words, common_only=True):
    """抽成待翻条目：{id, kanji, kana, pos, senses:[英文释义]}。

    一个词条可能又有多个写法又有多个读音（ＣＤプレーヤー / ＣＤプレイヤー），
    靠 kana 的 appliesToKanji 配对，别把写法和读音乱配——查词时是按
    (写法, 读音) 命中的，配错了会给出错读音。
    """
    out, seen = [], set()
    for w in words:
        kanas = [k for k in w.get("kana", []) if k.get("text")]
        kanjis = [k for k in w.get("kanji", []) if k.get("text")]
        if not kanas:
            continue
        if common_only and not any(k.get("common") for k in kanas) \
                and not any(k.get("common") for k in kanjis):
            continue
        if kanjis:
            pairs = []
            for kj in kanjis[:2]:
                kn = next((k for k in kanas
                           if kj["text"] in (k.get("appliesToKanji") or [])), None)
                pairs.append((kj["text"], (kn or kanas[0])["text"]))
        else:
            pairs = [(k["text"], k["text"]) for k in kanas[:2]]

        senses = []
        for s in w.get("sense", []):
            en = [_gloss_text(g) for g in s.get("gloss", [])
                  if g.get("lang", "eng") == "eng" and _gloss_text(g)]
            if en:
                senses.append({"en": "；".join(en[:3]),
                               "pos": (s.get("partOfSpeech") or [])[:3]})
        if not senses:
            continue
        for form, kana in pairs:
            key = f"{form}|{kana}"
            if key in seen:
                continue
            seen.add(key)
            out.append({"id": key, "kanji": form, "kana": kana, "senses": senses})
    return out


# ---------- 批量翻译 ----------

_KANA_RE = re.compile(r"[\u3041-\u30ff]")


def _zh_ok(value):
    """中文释义自检：非空、且不该夹带假名。

    夹带假名基本等于没译干净（模型把日文原样抄回来），这类结果必须退回重翻，
    否则词典里会混进读不懂的条目——比没有更糟。
    """
    return (isinstance(value, str) and bool(value.strip())
            and not _KANA_RE.search(value))


def call_api(key, batch, timeout=180):
    """翻一批，返回 (results, usage)；失败抛异常。"""
    items = [{"i": i, "k": e["kanji"], "r": e["kana"],
              "s": [{"p": s.get("pos") or [], "e": s["en"]} for s in e["senses"]]}
             for i, e in enumerate(batch, 1)]
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "reasoning_effort": "none",   # 关闭思考：翻译任务不需要推理
        "temperature": 0.1,           # 极低温：术语与风格稳定一致（质量优先）
        "stream": False,
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "ignore")
        except Exception:
            pass
        try:
            err = json.loads(raw).get("error", {})
        except (ValueError, AttributeError):
            err = {}
        raise ApiError(exc.code, err.get("type", ""),
                       err.get("message", "") or raw[:200])
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    # 模型偶发返回空内容或非 JSON（截断/拒答）：必须包成 ApiError，
    # 否则 JSONDecodeError 会穿透上层只捕 ApiError 的重试逻辑，把整个任务打断。
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError) as exc:
        raise ApiError(0, "bad_response",
                       f"响应不是合法 JSON：{str(exc)[:80]} | 前 80 字：{content[:80]!r}")
    raw = (parsed.get("r") if isinstance(parsed, dict) else None) or \
          (parsed.get("results") if isinstance(parsed, dict) else None)
    if not isinstance(raw, list):
        raise ApiError(0, "bad_response", "响应里没有结果数组")
    # 模型回的是序号（省 token），这里换回本批的真实 id
    results = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        idx = item.get("i", item.get("id"))
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= len(batch):
            results.append({"id": batch[idx - 1]["id"], "zh": item.get("z")})
    return results, data.get("usage", {})


def group_representatives(entries, done):
    """同一读音 + 同一组英文释义 → 只翻第一个写法，其余共用译文。

    词典里一个词常有 2-4 个写法变体（ＣＤプレーヤー／ＣＤプレイヤー、彼処／彼所），
    释义完全一样，逐个翻等于同一句话付好几遍钱——实测能省掉约四分之一的请求。
    """
    groups = {}
    for e in entries:
        sig = (e["kana"], "‖".join(s["en"] for s in e["senses"]))
        groups.setdefault(sig, []).append(e)
    reps, members = [], {}
    for group in groups.values():
        if all(g["id"] in done for g in group):
            continue
        reps.append(group[0])
        members[group[0]["id"]] = group
    return reps, members


def translate(pool, entries, done, batch_size, stats, workers=1):
    """翻译并落盘；已翻过的（done）跳过。

    **workers 默认 1（串行）**。实测证据：10 路并发跑起来后，错误从
    rate_limit_error（速率）大面积变成 quota_exceeded_error（额度），失败批次
    骤增——说明多枚 key 共享同一账号的配额，并发只是更快撞墙，并不能把吞吐
    乘以 key 数。稳妥的提速方式是「贴着账号速率上限走」：多 key 轮询分散请求、
    撞限流的那枚单独冷却，由别的 key 顶上。
    """
    todo, members = group_representatives(entries, done)
    saved = sum(1 for e in entries if e["id"] not in done) - len(todo)
    if saved > 0:
        print(f"写法变体去重：{len(todo)} 条代表覆盖 {len(todo) + saved} 条"
              f"（省掉 {saved} 次重复翻译）")
    if not todo:
        print("没有待翻条目（已全部翻过，或 --limit 已到）。")
        return done

    batches = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]
    lock = threading.Lock()
    st = {"batches": 0, "failed": 0}

    def run_one(batch):
        """翻一批；成功回 (results, usage)，彻底失败回 (None, {})。"""
        for attempt in range(RETRY + 1):
            with lock:
                k, wait = pool.pick()
            if wait:
                time.sleep(min(wait, SLEEP_MAX))  # 全池都在冷却：等最早恢复的那枚
            try:
                res, usage = call_api(k, batch)
                with lock:
                    pool.note(k)
                return res, usage
            except ApiError as exc:
                with lock:
                    if exc.status in (401, 403):
                        pool.cooldown(k, 1 << 30)   # 这枚 key 本身不行：本轮剔除
                        kind = "key 无效"
                    elif exc.status == 429:
                        pool.cooldown(k, (SLEEP_QUOTA if exc.is_quota else SLEEP_429) * 2)
                        kind = "额度上限" if exc.is_quota else "速率限制"
                    else:
                        kind = "接口错误"
                if attempt == RETRY:
                    with lock:
                        st["failed"] += 1
                    print(f"  有一批放弃（{kind}），重跑本命令会补", file=sys.stderr)
                    return None, {}
                print(f"  {pool.label(k)} 撞上{kind}，换 key 重试…")
                time.sleep(1.0)   # 换 key 前让一让，别把重试打成忙循环
        return None, {}

    def merge(batch, results, usage):
        """把一批结果并进 done 并落盘。"""
        with lock:
            by_id = {str(r.get("id")): r.get("zh") for r in results}
            missed = 0
            for e in batch:
                zh = by_id.get(e["id"])
                if (isinstance(zh, list) and len(zh) == len(e["senses"])
                        and all(_zh_ok(z) for z in zh)):
                    # 代表翻好后，同组的写法变体共用同一份译文
                    for member in members.get(e["id"], [e]):
                        m = dict(member, senses=[dict(s) for s in member["senses"]])
                        for s, z in zip(m["senses"], zh):
                            s["zh"] = str(z).strip()
                        done[m["id"]] = m
                else:
                    missed += 1
            for kpi, val in (usage or {}).items():
                if isinstance(val, (int, float)):
                    stats[kpi] = stats.get(kpi, 0) + val
            st["batches"] += 1
            print(f"  [{len(done)}/{len(entries)}] 批次 {st['batches']}/{len(batches)} 完成"
                  + (f"（{missed} 条不合格，下次重跑会补）" if missed else ""))
            save(done, stats)

    workers = max(1, min(workers, len(pool.keys), len(batches)))
    print(f"{'并发 ' + str(workers) + ' 路' if workers > 1 else '串行'}"
          f"（多 key 轮询 + 逐枚冷却），共 {len(batches)} 批")
    failed = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(run_one, b): b for b in batches}
        for fu in as_completed(futures):
            batch = futures[fu]
            try:
                results, usage = fu.result()
            except Exception as exc:
                results, usage = None, {}
                print(f"  批次异常：{exc}", file=sys.stderr)
            if results is None:
                failed.append(batch)
                continue
            merge(batch, results, usage)

    # 失败的批次就地再串行重试一轮：趁限流窗口还没连环撞满，能补多少补多少
    if failed:
        print(f"重试 {len(failed)} 个失败批次（放慢节奏）…")
        still = []
        for b in failed:
            time.sleep(SLEEP_429)
            try:
                res, usage = run_one(b)
            except Exception as exc:   # 兜底：任何异常都不该让整个任务倒下
                print(f"  重试仍失败：{exc}", file=sys.stderr)
                still.append(b)
                continue
            if res is None:
                still.append(b)
                continue
            merge(b, res, usage)
        if still:
            print(f"注意：仍有 {len(still)} 批失败（限流）。"
                  f"重跑本命令会补上，不会重复烧 token。", file=sys.stderr)
    return done


def save(done, stats):
    """原子写落盘。"""
    data = {
        "meta": {
            "model": MODEL,
            "source": "JMdict (scriptin/jmdict-simplified)",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "entries": len(done),
            "usage": stats,
        },
        "words": done,
    }
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(OUT_PATH)


def main(argv=None):
    ap = argparse.ArgumentParser(description="批量预翻 JMdict 英文释义为中文")
    ap.add_argument("--sample", type=int, default=0,
                    help="只翻前 N 条（小样验证，建议先跑 60）")
    ap.add_argument("--limit", type=int, default=0, help="最多翻多少条（0=不限）")
    ap.add_argument("--batch", type=int, default=BATCH_DEFAULT, help="每批词条数")
    ap.add_argument("--workers", type=int, default=1,
                    help="并行路数（默认 1）。实测多 key 共享同一账号配额，"
                         "并发只会更快撞限流，非必要别调大")
    ap.add_argument("--scope", choices=("common", "eng", "all"), default="common",
                    help="词典范围：common=常用词包(1.4MB) / eng=全量英文(11MB) / all=全语言(24MB)")
    ap.add_argument("--all-words", action="store_true",
                    help="配合 --scope eng：连非常用词也一起翻（条数暴涨，token 消耗大）")
    ap.add_argument("--from-zip", default="", help="使用本地已有的词典 zip")
    args = ap.parse_args(argv)

    pool = KeyPool(load_keys())  # 先校验 key，避免白下一遍词典
    print(f"已载入 {len(pool.keys)} 枚 key（轮询 + 逐枚冷却；日志只用编号，不打印明文）")
    if args.sample:
        args.limit = args.sample

    zip_path = Path(args.from_zip) if args.from_zip else download_zip(CACHE_DIR, args.scope)
    if not zip_path.exists():
        die(f"词典包不存在：{zip_path}")
    words = load_jmdict(zip_path)
    # common 包里本来全是常用词，无需再过滤；eng/all 包默认只挑常用词
    entries = extract(words, common_only=(args.scope != "common" and not args.all_words))
    if args.limit:
        entries = entries[:args.limit]
    print(f"待翻词条：{len(entries)} 条"
          f"（义项共 {sum(len(e['senses']) for e in entries)} 个）")

    done, stats = {}, {}
    if OUT_PATH.exists():
        try:
            old = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            done = old.get("words", {})
            stats = old.get("meta", {}).get("usage", {})
            print(f"载入已有进度：{len(done)} 条（断点续跑，已翻的不再烧 token）")
        except (ValueError, OSError) as exc:
            print(f"已有文件读不了（{exc}），从头开始")

    # 人工兜底先落：语法术语类词条中文必含假名，走 API 永远过不了自检
    by_id = {e["id"]: e for e in entries}
    filled = 0
    for key, zh in MANUAL_ZH.items():
        e = by_id.get(key)
        if not e or key in done or len(e["senses"]) != len(zh):
            continue   # 词条不存在 / 已翻过 / 义项数对不上（词典更新了）：跳过
        senses = [dict(s, zh=t) for s, t in zip(e["senses"], zh)]
        done[key] = {"id": key, "kanji": e["kanji"], "kana": e["kana"], "senses": senses}
        filled += 1
    if filled:
        save(done, stats)
        print(f"人工兜底补齐 {filled} 条（语法术语类，中文需引用假名）")

    try:
        done = translate(pool, entries, done, args.batch, stats, workers=args.workers)
    except KeyboardInterrupt:
        print("\n已手动中断（已翻的都已落盘，重跑本命令接着翻）。", file=sys.stderr)
    except Exception as exc:
        # 最后一道防线：任何意外都不该以「静默消失」收场，要说清从哪继续
        print(f"\n意外中断：{exc}\n已翻的条目都已落盘，重跑本命令会接着翻。",
              file=sys.stderr)
    save(done, stats)
    if len(pool.keys) > 1:
        used = " ".join(f"{pool.label(k)}×{pool.hits.get(k, 0)}"
                        for k in pool.keys if pool.hits.get(k))
        print(f"各 key 成功批次：{used or '（无）'}")
    total = stats.get("total_tokens", 0)
    print(f"\n完成：{len(done)} 条 → {OUT_PATH}")
    print(f"累计 token：{total}（prompt {stats.get('prompt_tokens', 0)} / "
          f"completion {stats.get('completion_tokens', 0)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
