#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""作品沉浸 —— 导入器：把 txt / epub 轻小说切成「句子流」入库

两条入口共用下面的落盘逻辑（import_work 函数）：
  - 界面：作品阅读器顶部「＋ 导入作品」上传（app.py 的 POST /api/works/import，
    经系统临时目录绕一圈复用本函数）；
  - 命令行（先停掉练习服务，避免与 /api 的词库写入互踩——README 有警示）：
    python.exe practice\\import_work.py 作品.txt --title "作品名" --author "作者"
    python.exe practice\\import_work.py 作品.epub
    python.exe practice\\import_work.py --list

产出（作品本体不入 git，见 .gitignore 的 demo_n5 白名单）：
    data/works/{id}/work.json         元信息 + 章节索引
    data/works/{id}/content/chNN.json {"title", "paras": [[句子, ...], ...]}

只存原文，不做分词与词库匹配——那是 works.py 在请求时实时算的，词库
（含挖矿新词、掌握度）变化后阅读器立即生效，无需重新导入。
"""
import argparse
import datetime
import hashlib
import html
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import works  # noqa: E402

CHUNK_SENTS = 150  # txt 无章节标记时，按句数分部的大小


# ---------------------------------------------------------------------------
# 文本读取与章节切分
# ---------------------------------------------------------------------------

def read_text(path):
    """txt 编码探测：日文小说常见 cp932/shift_jis，中文常见 gbk，先试 utf-8。"""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp932", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def text_to_paras(text):
    """全文 → 段落列表（每个非空行一段，段首缩进剥掉）。"""
    paras = []
    for line in text.splitlines():
        line = line.strip().lstrip("　").strip()
        if line:
            paras.append(line)
    return paras


def paras_to_chapters(paras, source_title):
    """段落 → [(章节标题, 段落列表)]。

    有「第N章/節/話」标记行按标记分章；否则每 CHUNK_SENTS 句分一部
    （段落边界保留，只是部与部之间切开）。
    """
    marked = any(works._CHAPTER_TITLE_RE.match(p) and len(p) <= 60 for p in paras)
    if not marked:
        chapters, cur, count = [], [], 0
        for p in paras:
            cur.append(p)
            count += len(works.split_sentences(p))
            if count >= CHUNK_SENTS:
                chapters.append((f"{source_title}·{len(chapters) + 1}", cur))
                cur, count = [], 0
        if cur:
            chapters.append((f"{source_title}·{len(chapters) + 1}", cur))
        return chapters

    chapters, cur_title, cur_paras = [], None, []
    for para in paras:
        if works._CHAPTER_TITLE_RE.match(para) and len(para) <= 60:
            if cur_title is not None:
                chapters.append((cur_title, cur_paras))
            cur_title = para.strip().lstrip("■◆▼【*").strip()
            cur_paras = []
        else:
            cur_paras.append(para)
    if cur_title is not None:
        chapters.append((cur_title, cur_paras))
    return chapters


class _XHTMLText(HTMLParser):
    """epub 单文档抽正文：块级标签（p/h1-6/li/blockquote/div）各成一段，br 断段。"""

    BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "div"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras = []
        self.buf = []
        self._in_heading = False

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3"):
            self._in_heading = True
        elif tag in self.BLOCK and self.buf:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self._flush()

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self._flush()

    def handle_data(self, data):
        self.buf.append(data)

    def _flush(self):
        text = html.unescape("".join(self.buf)).strip().lstrip("　").strip()
        self.buf = []
        self._in_heading = False
        if text:
            self.paras.append(text)


def extract_epub(path):
    """epub → 段落列表。按 OPF spine 顺序读每个 xhtml 文档。"""
    with zipfile.ZipFile(path) as zf:
        container = zf.read("META-INF/container.xml").decode("utf-8", errors="replace")
        m = re.search(r'full-path="([^"]+)"', container)
        if not m:
            raise ValueError("container.xml 里找不到 OPF 路径")
        opf_name = m.group(1)
        opf = zf.read(opf_name).decode("utf-8", errors="replace")
        # OPF 所在目录：无子目录（zip 根）时是 ""，不能拿 rsplit 的 [0]——
        # 那会把整个文件名当目录前缀（content.opf/x.html 读不到）
        parts = opf_name.rsplit("/", 1)
        opf_dir = parts[0] if len(parts) > 1 else ""

        manifest = dict(re.findall(r'<item\b[^>]*\bid="([^"]+)"[^>]*\bhref="([^"]+)"', opf))
        for href, item_id in re.findall(r'<item\b[^>]*\bhref="([^"]+)"[^>]*\bid="([^"]+)"', opf):
            manifest.setdefault(item_id, href)
        spine = re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf)

        paras = []
        for item_id in spine:
            href = manifest.get(item_id)
            if not href or not href.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            full = f"{opf_dir}/{href}" if opf_dir else href
            try:
                doc = zf.read(full).decode("utf-8", errors="replace")
            except KeyError:
                continue
            parser = _XHTMLText()
            try:
                parser.feed(doc)
            except Exception:
                continue
            paras.extend(parser.paras)
    return paras


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def slug_id(title):
    """标题 → 目录名安全 id：ASCII 字母数字保留，否则 md5 前 8 位。"""
    slug = re.sub(r"[^0-9A-Za-z_-]+", "", title.replace(" ", "-"))
    if len(slug) < 2:
        slug = "work-" + hashlib.md5(title.encode("utf-8")).hexdigest()[:8]
    return slug[:40]


def import_work(path, title=None, author="", work_id=None, work_type="novel",
                source_name=None):
    """把 txt/epub 切成句子流落盘到 data/works/{id}/。

    source_name：界面导入（上传文件临时落盘）时传原始文件名，比临时文件
    的随机名更能说明内容来源；CLI 调用不传，默认用文件路径名。
    已存在的同名 work_id 会整体覆盖（同一作品重复导入 = 重新处理一遍）。
    """
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"文件不存在：{path}")
    title = title or path.stem
    work_id = work_id or slug_id(title)

    if path.suffix.lower() == ".epub":
        paras = extract_epub(path)
    else:
        paras = text_to_paras(read_text(path))
    if not paras:
        raise SystemExit("没有解析到任何文本（编码或格式问题？）")

    chapters = paras_to_chapters(paras, title)
    out_dir = works.WORKS_DIR / work_id
    content_dir = out_dir / "content"
    content_dir.mkdir(parents=True, exist_ok=True)

    # 落盘章节：句子过少的空章跳过；章节名重复时补序号
    chapter_index = []
    for ch_title, ch_paras in chapters:
        grouped = []
        for p in ch_paras:
            ss = works.split_sentences(p)
            if ss:
                grouped.append(ss)
        if grouped:
            chapter_index.append({
                "title": ch_title or "未命名",
                "paras": grouped,
                "sentences": sum(len(p) for p in grouped),
            })

    meta = {
        "id": work_id,
        "title": title,
        "author": author,
        "type": work_type,
        "source": source_name or path.name,
        "created": datetime.date.today().isoformat(),
        "chapters": [],
    }
    for i, ch in enumerate(chapter_index):
        fname = f"ch{i:02d}.json"
        meta["chapters"].append({"id": i, "title": ch["title"], "file": fname,
                                 "sentences": ch["sentences"]})
        works.write_json_atomic(content_dir / fname,
                                {"title": ch["title"], "paras": ch["paras"]})
    works.write_json_atomic(out_dir / "work.json", meta, indent=1)

    total = sum(c["sentences"] for c in meta["chapters"])
    print(f"已导入《{title}》→ data/works/{work_id}/")
    print(f"  章节 {len(meta['chapters'])} · 句子 {total} · 段落 {len(paras)}")
    return meta


def main():
    ap = argparse.ArgumentParser(description="导入 txt/epub 作品到 data/works/")
    ap.add_argument("path", nargs="?", help="txt 或 epub 文件路径")
    ap.add_argument("--title", help="作品名（默认取文件名）")
    ap.add_argument("--author", default="")
    ap.add_argument("--id", help="作品 id（默认由标题生成）")
    ap.add_argument("--type", default="novel", choices=["novel", "anime", "manga"])
    ap.add_argument("--list", action="store_true", help="列出已导入作品")
    args = ap.parse_args()

    if args.list:
        for m in works.list_works():
            n = sum(c.get("sentences", 0) for c in m.get("chapters", []))
            print(f"{m['id']:30s} 《{m['title']}》 {n} 句")
        return
    if not args.path:
        ap.error("需要作品文件路径（或 --list）")
    import_work(args.path, args.title, args.author, args.id, args.type)


if __name__ == "__main__":
    main()
