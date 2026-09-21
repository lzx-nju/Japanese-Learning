/* 作品沉浸·阅读器（reader.html）
 *
 * 正文由后端下发的「句子 → 词块」结构渲染：
 *   词块带 w 字段 = 词库里有的词，按掌握度三档着色，点开是词卡（释义/例句/朗读）；
 *   没有 w 的 = 库外生词，点开是「＋ 生词本」表单，保存后重新拉章节即变已知词。
 * 标注是请求时实时算的，所以挖矿/练习涨的掌握度刷新即生效，不需要重新导入作品。
 */
const state = {
  works: [],
  workId: "",
  work: null,
  chapter: 0,
  payload: null,
  sentNodes: [],        // 本章句子节点（按顺序），带 t0/t1，供「播到哪句」定位
};

function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function hasKanji(s) { return /[一-鿿]/.test(s || ""); }
function mClass(m) { return m >= 3 ? "m-known" : (m >= 1 ? "m-learn" : "m-new"); }
function mLabel(m) { return m >= 3 ? "会了" : (m >= 1 ? "在学" : "库内未学"); }

let audioEl = null;
function playAudio(url) {
  if (audioEl) audioEl.pause();
  // 章节原声让路：否则点一下「朗读整句」或词卡发音，会和正在播的音轨叠着放
  if (media.el && !media.el.paused) media.el.pause();
  audioEl = new Audio(url);
  audioEl.play().catch(() => {});
}
function playSentence(text) {
  playAudio("/api/tts/text?text=" + encodeURIComponent(text));
}

// ---------- 音声作品的章节音轨 ----------
// 有音轨的章节用**同一个** audio 实例全程复用：点句播放要频繁 seek，每次
// new Audio 会重新拉流、也没法让 iOS 保持「已解锁」状态。
// stopAt 是「播到这句末尾就停」——单句模式下不许它继续往下念。
// detached 是「用户自己滚开了、暂时不跟随」：只更新高亮不抢滚动，右下角浮出
// 「↑/↓ 当前句」；scrollGuard 是自家 scrollIntoView 的保护窗口，免得被当成
// 用户滚动（见 onUserScroll）。chapterKey 是「作品 + 章号 + 音轨」的指纹：
// 同一章重渲染（切开关、挖矿加词、改句）不打断播放，换章才整体重置。
const media = { el: null, url: "", stopAt: null, node: null, rafId: null, sync: 0,
                detached: false, scrollGuard: false, guardTimer: null,
                chapterKey: "" };

// 字幕↔语音同步偏移（秒）：whisper 时间戳对句边界有 ±0.3s 级抖动，
// 播放条上「同步 −/+」按 50ms 步进调整，存浏览器，对所有作品生效。
// 方向与播放条 tooltip 同一口径：**正值 = 高亮延迟**。两个换算必须互为反函数：
//   contentAt(a) = a - sync   音频时间 → 字幕时间（跟读高亮、找上一句）
//   audioAt(t)   = t + sync   字幕时间 → 音频时间（点句跳播、单句停止）
// 早先两处都用 `t - sync`（同一个函数当正反两用），偏移非零时高亮与
// 点句播放会朝相反方向偏 2·sync——把高亮调准就必然把跳播调歪，反之亦然。
function contentAt(a) {
  return a - media.sync;
}
function audioAt(t) {
  return t + media.sync;
}
function setVoiceSync(deltaMs) {
  const cur = (parseInt(localStorage.getItem("voice-sync-ms") || "0", 10) || 0) + deltaMs;
  const v = Math.max(-3000, Math.min(3000, cur));
  localStorage.setItem("voice-sync-ms", String(v));
  media.sync = v / 1000;
  const el = $("sync-ms");
  if (el) el.textContent = `${v > 0 ? "+" : ""}${v}ms`;
}
function resetVoiceSync() {
  localStorage.setItem("voice-sync-ms", "0");
  media.sync = 0;
  const el = $("sync-ms");
  if (el) el.textContent = "0ms";
}
function initVoiceSync() {
  setVoiceSync(0);
}

function fmtTime(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function mediaEl() {
  const url = (state.payload && state.payload.chapter && state.payload.chapter.audio) || "";
  if (!url) return null;
  if (media.el && media.url === url) return media.el;
  if (media.el) media.el.pause();
  const el = new Audio(url);
  el.preload = "metadata";
  el.playbackRate = parseFloat($("media-rate").value) || 1;   // 新实例跟上倍速选择
  el.addEventListener("timeupdate", onMediaTick);
  el.addEventListener("ended", () => { media.stopAt = null; stopFollowLoop(); syncMediaUI(); });
  el.addEventListener("play", () => { startFollowLoop(); syncMediaUI(); });
  el.addEventListener("pause", () => { stopFollowLoop(); syncMediaUI(); });
  el.addEventListener("loadedmetadata", () => { syncSeekRange(); syncMediaUI(); });
  el.addEventListener("error", () => {
    toast("音轨加载失败（文件被移动了？重新导入即可）");
  });
  media.el = el;
  media.url = url;
  return el;
}

function resetMedia() {
  if (media.el) {
    media.el.pause();
    media.el.removeAttribute("src");
    media.el.load();
  }
  media.el = null;
  media.url = "";
  media.stopAt = null;
  media.node = null;
  setDetached(false);           // 换章/换作品：跟随状态与箭头一起归零
  clearPlayingMark();
  const bar = $("media-bar");
  if (bar) bar.hidden = true;
}

function syncSeekRange() {
  const el = media.el;
  const seek = $("media-seek");
  if (!el || !seek) return;
  const d = isFinite(el.duration) ? el.duration : 0;
  seek.max = String(d || 0);
  seek.value = String(Math.min(el.currentTime || 0, d || 0));
}

function syncMediaUI() {
  const el = media.el;
  if (!el) return;
  $("media-play").textContent = el.paused ? "▶" : "⏸";
  const d = isFinite(el.duration) ? el.duration : 0;
  $("media-time").textContent = `${fmtTime(el.currentTime)} / ${fmtTime(d)}`;
}

// 播放中把「当前句」标出来，并滚进视野（长独白不看屏幕也知道念到哪了）
function onMediaTick() {
  const el = media.el;
  if (!el) return;
  $("media-seek").value = String(el.currentTime || 0);
  syncMediaUI();
  if (media.stopAt != null && el.currentTime >= media.stopAt - 0.06) {
    media.stopAt = null;
    el.pause();
    return;
  }
}

// timeupdate 只有约 4Hz，跟随高亮靠它会滞后跳格；播放期间改由 rAF
// （每帧 ~16ms）轮询 currentTime 切句，暂停即停。seek 条/时间文案仍走 timeupdate。
function startFollowLoop() {
  if (media.rafId != null) return;
  const loop = () => {
    media.rafId = requestAnimationFrame(loop);
    const el = media.el;
    if (!el || el.paused) return;
    if (media.stopAt != null && el.currentTime >= media.stopAt - 0.06) {
      media.stopAt = null;
      el.pause();
      return;
    }
    if ($("media-follow").checked) markSentenceAt(contentAt(el.currentTime));
  };
  media.rafId = requestAnimationFrame(loop);
}

function stopFollowLoop() {
  if (media.rafId != null) {
    cancelAnimationFrame(media.rafId);
    media.rafId = null;
  }
}

function clearPlayingMark() {
  const cur = document.querySelector(".sent.playing");
  if (cur) cur.classList.remove("playing");
}

// ---- 跟随滚动 / 「回到当前句」 ----
// 默认跟着正在播的句子滚（长独白不看屏幕也知道念到哪）。但人想回顶部看画像、
// 改设置时，每次切句都被拽回来就很烦：所以用户自己一滚（当前句滚出舒适区）就
// 停下跟随、右下角浮出「↓ 当前句」；点它跳回当前句并恢复跟随。自己滚回当前句
// 附近也算回来（箭头自动收起，继续跟）。
function followOn() {
  const cb = $("media-follow");
  return !cb || cb.checked;
}

function playingNode() {
  return document.querySelector(".sent.playing");
}

function updateFollowBtn() {
  const btn = $("follow-btn");
  if (!btn) return;
  const node = (media.detached && followOn()) ? playingNode() : null;
  btn.hidden = !node;
  if (!node) return;
  // 箭头指向当前句：人在下面（当前句在上半屏外）就朝上，反之朝下
  const r = node.getBoundingClientRect();
  const above = r.top + r.height / 2 < window.innerHeight / 2;
  btn.querySelector(".fb-arrow").textContent = above ? "↑" : "↓";
}

function setDetached(on) {
  media.detached = !!on;
  updateFollowBtn();
}

// 「当前句在舒适区里」——自动跟随用的就是这套判定（见 markSentenceAt）：跟着的
// 时候代码把当前句保持在这个区间内，反过来拿它判断用户有没有接管。两把尺子完全
// 互补，就不会出现「没脱离跟随、切句又被拽回去」的中间态。
function inComfortZone(node) {
  const r = node.getBoundingClientRect();
  return r.top >= 60 && r.bottom <= window.innerHeight - 40;
}

// 用户滚动后判一次：当前句还在舒适区就继续跟随，出了舒适区就停下。
// 滚轮 / 触摸是「人手」的确定信号（自家 scrollIntoView 不会发这两种事件）；
// 拖滚动条只有 scroll 事件，那种要走带 scrollGuard 的 onScrollMaybeUser。
function onUserScroll() {
  if (!followOn()) return;
  const node = playingNode();
  if (!node) return;
  setDetached(!inComfortZone(node));
}

// 自家 scrollIntoView 引发的滚动不算用户滚动。平滑滚动的时长随距离变（固定
// 时长会漏判），所以每来一个 scroll 事件就把保护窗口往后推 160ms——滚动停下来
// 窗口自然失效。
function guardScroll() {
  media.scrollGuard = true;
  clearTimeout(media.guardTimer);
  media.guardTimer = setTimeout(() => { media.scrollGuard = false; }, 160);
}

function onScrollMaybeUser() {
  if (media.scrollGuard) { guardScroll(); return; }   // 自己滚的，续窗口
  onUserScroll();
}

function backToPlayingSentence() {
  const node = playingNode();
  setDetached(false);                            // 恢复跟随
  if (!node) return;
  guardScroll();
  node.scrollIntoView({ block: "center", behavior: "smooth" });
}

function markSentenceAt(t) {
  const hit = state.sentNodes.find((n) => n.t0 != null && t >= n.t0 && t < n.t1);
  const node = hit ? hit.el : null;
  const cur = document.querySelector(".sent.playing");
  if (cur === node) return;
  clearPlayingMark();
  if (node) {
    node.classList.add("playing");
    if (media.detached) {          // 人自己滚开了：只标高亮，别抢滚动
      updateFollowBtn();
      return;
    }
    if (!inComfortZone(node)) {
      guardScroll();
      node.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
}

// 点句播放：跳到这句开头；勾了「单句循环停」就播到句尾自动停
function playSentenceAudio(node, t0, t1) {
  const el = mediaEl();
  if (!el) { toast("这一章没有音轨"); return; }
  media.node = node;
  media.stopAt = ($("media-loop").checked && t1 != null && t1 > t0) ? audioAt(t1) : null;
  try {
    const aim = Math.max(0, audioAt(t0));
    if (Math.abs((el.currentTime || 0) - aim) > 0.05) el.currentTime = aim;
  } catch (e) { /* 元数据没到时 seek 会抛，忽略 */ }
  el.play().catch(() => toast("浏览器拦下了自动播放：先点一下播放条再试"));
  clearPlayingMark();
  if (node) node.classList.add("playing");
  setDetached(false);       // 点了某句的 ▶ = 就在跟着它了：恢复跟随
}

// 回到上一句开头（连续听时漏了一句，不想拖进度条）
function backToPrevSentence() {
  const el = mediaEl();
  if (!el) return;
  const t = contentAt(el.currentTime || 0);
  let prev = null;
  for (const n of state.sentNodes) {
    if (n.t0 == null || n.t0 >= t - 1.0) continue;
    prev = n;
  }
  if (prev) { el.currentTime = Math.max(0, audioAt(prev.t0)); if (el.paused) el.play().catch(() => {}); }
  else el.currentTime = 0;
}

// 播放条的显隐与文案：只有「有音轨」的章节才显示；没时间轴的章节
// 仍能整轨播（当听力材料），只是点不了单句
function setupMediaBar() {
  const ch = (state.payload && state.payload.chapter) || {};
  const bar = $("media-bar");
  if (!bar) return;
  if (!ch.audio) { bar.hidden = true; return; }
  bar.hidden = false;
  $("media-hint").textContent = ch.timed
    ? "点句子左边的 ▶ 从那一句开始播（勾着「单句循环停」就播到句尾自动停）；"
      + "空格播放 / 暂停整轨。音轨不进仓库，直接读磁盘上的原文件。"
    : "这一章没有时间轴（台本还没对齐），单句播不了，句子只能用 🔊 合成音朗读；"
      + "整轨仍可播来当听力材料。";
  $("media-play").textContent = "▶";
  $("media-seek").max = "0";
  $("media-seek").value = "0";
  $("media-time").textContent = "00:00 / 00:00";
  $("media-back").disabled = !ch.timed;
}

let toastTimer = null;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2200);
}

// ---------- 载入 ----------

// 加载遮罩：切作品 / 切章要重新分词标注，期间挡住旧画面，免得看着像卡死。
// 用计数器是因为 loadWork → loadChapter 会嵌套；150ms 内就完事的不闪这一下。
let busyCount = 0;
let busyTimer = null;

async function withLoading(msg, fn) {
  busyCount++;
  $("loading-text").textContent = msg || "加载中…";
  if (busyCount === 1) {
    clearTimeout(busyTimer);
    busyTimer = setTimeout(() => { if (busyCount) $("loading").hidden = false; }, 150);
  }
  try {
    return await fn();
  } finally {
    busyCount--;
    if (busyCount <= 0) {
      busyCount = 0;
      clearTimeout(busyTimer);
      $("loading").hidden = true;
    }
  }
}

async function init() {
  wireEvents();
  const q = new URLSearchParams(location.search);
  await withLoading("正在载入作品…",
                    () => refreshWorks(q.get("work"), q.get("ch")));
  // ?import=1：来自首页《作品沉浸》→ 导入作品的入口，自动开模态框
  if (q.get("import") === "1") openImportPanel();
}

// 拉作品列表并载入作品。workId 指定则选中它（导入成功后可立即切过去）；
// 为空时沿用当前正在读的、再退回上次读的 / 列表第一部。
async function refreshWorks(workId, ch) {
  const res = await fetch("/api/works").then((r) => r.json());
  state.works = res.works || [];
  const sel = $("work-select");
  if (!state.works.length) {
    $("empty-hint").hidden = false;
    sel.innerHTML = "";
    $("chapter-select").innerHTML = "";
    $("text").innerHTML = "";
    $("profile-card").hidden = true;
    $("chapter-stats").innerHTML = "";
    state.work = null;
    state.payload = null;
    return;
  }
  $("empty-hint").hidden = true;
  sel.innerHTML = state.works
    .map((w) => `<option value="${esc(w.id)}">《${esc(w.title)}》${
      w.chapters.length} 章</option>`).join("");

  let pickId = workId;
  if (!pickId) {
    const last = localStorage.getItem("reader.work") || "";
    const cur = state.workId && state.works.some((w) => w.id === state.workId)
      ? state.workId : "";
    pickId = cur || (state.works.some((w) => w.id === last) ? last : state.works[0].id);
  }
  sel.value = pickId;
  await loadWork(pickId, ch);
}

async function loadWork(workId, chapterArg) {
  state.workId = workId;
  localStorage.setItem("reader.work", workId);
  const res = await fetch("/api/works/" + encodeURIComponent(workId)).then((r) => r.json());
  if (res.error) { toast(res.error); return; }
  state.work = res.work;
  renderProfile(res.profile);

  const sel = $("chapter-select");
  sel.innerHTML = state.work.chapters
    .map((c, i) => `<option value="${i}">${esc(c.title || ("第" + (i + 1) + "章"))}（${c.sentences} 句）</option>`)
    .join("");
  const saved = chapterArg != null ? chapterArg
    : (localStorage.getItem("reader.ch." + workId) || "0");
  await loadChapter(saved == null ? 0 : parseInt(saved, 10) || 0);
}

// 章请求序号：连点「下一章」或快速切章时，先发的请求可能后到，
// 晚到的旧响应会把正文渲染成上一本/上一章，而章号与地址栏已是新的了。
let chapterSeq = 0;

async function loadChapter(n) {
  if (!state.work) return;   // 一部作品都没有时（空列表）翻页按钮仍在，别抛错
  const chapters = state.work.chapters || [];
  n = Math.max(0, Math.min(chapters.length - 1, n));
  state.chapter = n;
  localStorage.setItem("reader.ch." + state.workId, String(n));
  $("chapter-select").value = String(n);
  history.replaceState(null, "", "/reader?work=" + encodeURIComponent(state.workId)
    + "&ch=" + n);

  const seq = ++chapterSeq;
  const res = await fetch("/api/works/" + encodeURIComponent(state.workId)
    + "/chapter/" + n).then((r) => r.json());
  if (seq !== chapterSeq) return;  // 更新的章已经在途：这个响应丢掉
  if (res.error) { toast(res.error); return; }
  state.payload = res;
  renderChapter();
}

// ---------- 渲染 ----------

function renderProfile(p) {
  if (!p) return;
  $("profile-card").hidden = false;
  const pct = (x) => (x * 100).toFixed(1) + "%";
  $("profile-grid").innerHTML = [
    [p.sentences, "总句数"],
    [pct(p.coverage), "已学词覆盖"],
    [pct(p.known_sentence_rate), "含已学词句子"],
    [p.unique_known, "命中已学词"],
    [p.unique_unknown, "生词种数"],
  ].map(([n, l]) => `<div class="prof-item"><span class="num">${esc(n)}</span><span class="lbl">${l}</span></div>`)
   .join("");
  $("profile-hint").textContent =
    `覆盖率＝掌握度≥1 的词所占字符比例；另有 ${(p.library_seen * 100).toFixed(1)}% 是词库里`
    + `还没学的词（灰色虚线）。点下方高频生词可直接加进生词本。`;
  $("top-unknown").innerHTML = (p.top_unknown || []).slice(0, 40).map(
    (u) => `<span class="uk-chip" data-surface="${esc(u.surface)}">${esc(u.surface)}`
      + `<span class="cnt">${u.count}</span></span>`).join("");
}

// 「作品 + 章号 + 音轨地址」指纹：同章重渲染（切开关、挖矿加词、改句文本）与
// 换章都走 renderChapter，但前者不该动正在播的音轨（见 media.chapterKey）。
function mediaKey() {
  const ch = (state.payload && state.payload.chapter) || {};
  return `${state.workId}:${state.chapter}:${ch.audio || ""}`;
}

function renderChapter() {
  if (!state.payload) return;   // 一部作品都没导入（或还没选上）：开关点了不该抛错
  const key = mediaKey();
  const keep = key === media.chapterKey && !!media.el;   // 同一章：音轨别断
  media.chapterKey = key;
  const box = $("text");
  box.innerHTML = "";
  box.classList.toggle("no-highlight", !$("toggle-known").checked);
  box.classList.toggle("hide-chips", !$("toggle-chips").checked);
  hideCtxMenu();                       // DOM 重建，旧选区已失效
  if (!keep) resetMedia();
  state.sentNodes = [];
  setupMediaBar();

  for (const [pi, para] of (state.payload.paras || []).entries()) {
    const p = document.createElement("p");
    p.className = "rp";
    for (const [si, sent] of para.entries()) {
      p.appendChild(sentNode(sent, pi, si));
    }
    box.appendChild(p);
  }

  const s = state.payload.stats || {};
  $("chapter-stats").innerHTML = [
    `${s.sentences || 0} 句`,
    `含已学词 ${s.known_sentences || 0} 句`,
    `已学词出现 ${s.known_hits || 0} 次`,
  ].map((t) => `<span>${esc(t)}</span>`).join("");

  if (keep) restoreMedia();
}

// 音轨没断时把播放条与「正在播的那句」接回来：同一个 audio 实例还在放（位置、
// 倍速、单句停点都不丢），只是正文换成了一批新句子节点。
function restoreMedia() {
  const el = media.el;
  if (!el) return;
  syncSeekRange();
  syncMediaUI();
  if (el.currentTime > 0 || !el.paused) markSentenceAt(contentAt(el.currentTime));
}

function sentNode(sent, pi, si) {
  const wrap = document.createElement("span");
  wrap.className = "sent" + (sent.has_known ? " has-known" : "");
  wrap.dataset.text = sent.text || "";
  wrap.dataset.pi = String(pi);      // 段落下标，句子编辑定位用
  wrap.dataset.si = String(si);      // 句内下标，句子编辑定位用

  const btn = document.createElement("button");
  btn.className = "sent-btn";
  btn.type = "button";
  if (sent.t0 != null) {
    // 有音轨的作品：这一句在音频里有确切位置，播原声（而不是合成音）
    wrap.dataset.t0 = String(sent.t0);
    wrap.dataset.t1 = String(sent.t1 == null ? sent.t0 : sent.t1);
    btn.textContent = "▶";
    btn.classList.add("play");   // 常驻可见，别让人找不到「从这句开始播」
    btn.title = `从 ${fmtTime(sent.t0)} 播这一句`;
    btn.onclick = () => playSentenceAudio(wrap, sent.t0, sent.t1);
    state.sentNodes.push({
      el: wrap, t0: sent.t0, t1: sent.t1 == null ? sent.t0 : sent.t1,
    });
  } else {
    // 没有时间轴（台本没对齐 / 轻小说）：退回 TTS 朗读整句
    btn.textContent = "🔊";
    btn.title = "朗读这句话";
    btn.onclick = () => playSentence(sent.text);
  }
  wrap.appendChild(btn);

  // 文本修正入口：ASR / 台本转写错字点 ✏️ 直接改（右键菜单里也有，
  // 但右键不好发现——每句都摆一个，悬停时变亮）
  const editBtn = document.createElement("button");
  editBtn.className = "sent-btn edit";
  editBtn.type = "button";
  editBtn.textContent = "✏️";
  editBtn.title = "这句文本有误（如 ASR 转写错字）？点此修正";
  editBtn.onclick = (ev) => { ev.stopPropagation(); openSentEdit(wrap); };
  wrap.appendChild(editBtn);

  for (const seg of sent.segs || []) {
    wrap.appendChild(segNode(seg));
  }

  if (sent.known && sent.known.length) {
    const chips = document.createElement("span");
    chips.className = "sent-chips";
    for (const ref of sent.known) {
      const w = (state.payload.words || {})[ref];
      if (!w) continue;
      const chip = document.createElement("span");
      chip.className = "chip " + mClass(w.mastery);
      chip.textContent = w.kanji && w.kanji !== "---" ? w.kanji : w.hiragana;
      chip.title = `${w.meaning}（${mLabel(w.mastery)}）`;
      chip.onclick = (e) => { e.stopPropagation(); openWordCard(ref, e.pageX, e.pageY); };
      chips.appendChild(chip);
    }
    if (chips.childNodes.length) wrap.appendChild(chips);
  }
  return wrap;
}

function segNode(seg) {
  const span = document.createElement("span");
  span.className = "seg";
  span.dataset.t = seg.t;                 // 原样词面（合并分词时取这个，不受注音 ruby 影响）
  if (seg.w) {
    span.dataset.ref = seg.w;
    span.classList.add(mClass(seg.m));
  } else {
    span.dataset.surface = seg.t;
    span.dataset.reading = seg.r || "";
    span.dataset.base = seg.base || "";
  }
  if ($("toggle-furigana").checked && seg.r && hasKanji(seg.t)) {
    const ruby = document.createElement("ruby");
    ruby.appendChild(document.createTextNode(seg.t));
    const rt = document.createElement("rt");
    rt.textContent = seg.r;
    ruby.appendChild(rt);
    span.appendChild(ruby);
  } else {
    span.textContent = seg.t;
  }
  return span;
}

// ---------- 点词弹卡 ----------

// 词卡定位：x/y 传 pageX/pageY（文档坐标；.word-popup 是 absolute）——
// 卡片锚在点中的词旁，滚动页面时跟着内容走。纵向用视口判断放不放得下：
// 点视口底部的词就翻到词上方，保证弹出来就在眼前，不用先滚动。
function popupAt(x, y) {
  const p = $("word-popup");
  p.hidden = false;                      // 先显示才有 offsetWidth/Height
  const w = p.offsetWidth, h = p.offsetHeight;
  const maxX = document.documentElement.clientWidth - w - 12;
  p.style.left = Math.max(8, Math.min(x - 16, maxX)) + "px";
  const viewTop = window.scrollY;
  const viewBottom = viewTop + window.innerHeight;
  let top = (y + h + 20 > viewBottom) ? y - h - 12 : y + 16;
  top = Math.max(viewTop + 8, top);
  p.style.top = top + "px";
}

function closePopup() { $("word-popup").hidden = true; }

function openWordCard(ref, x, y) {
  const w = (state.payload.words || {})[ref];
  if (!w) return;
  const hasKanjiForm = w.kanji && w.kanji !== "---";
  const form = hasKanjiForm ? w.kanji : w.hiragana;
  $("word-popup").innerHTML = `
    <span class="pop-close" data-act="close">✕</span>
    <div class="pop-head">
      <span class="pop-word">${esc(form)}</span>
      ${hasKanjiForm ? `<span class="pop-read">${esc(w.hiragana)}</span>` : ""}
    </div>
    <div class="pop-meta">${esc(w.lesson_title)} · ${esc(mLabel(w.mastery))}（掌握度 ${w.mastery}/5）</div>
    ${w.image ? `<img class="pop-img" src="${esc(w.image)}" alt="">` : ""}
    <div class="pop-meaning">${esc(w.meaning)}</div>
    ${w.notes ? `<div class="pop-meta">${esc(w.notes)}</div>` : ""}
    ${w.example_ja ? `<div class="pop-ex"><div class="ja">${esc(w.example_ja)}</div>
      ${w.example_zh ? `<div class="zh">${esc(w.example_zh)}</div>` : ""}</div>` : ""}
    <div class="pop-actions">
      <button data-act="word-audio">🔊 读单词</button>
      ${w.example_ja ? `<button data-act="ex-audio">🔊 读例句</button>` : ""}
    </div>`;
  const p = $("word-popup");
  p.dataset.ref = ref;
  popupAt(x, y);
}

function openMineForm(surface, reading, base, sentence, x, y) {
  $("word-popup").innerHTML = `
    <span class="pop-close" data-act="close">✕</span>
    <div class="pop-head">
      <span class="pop-word">${esc(surface)}</span>
      ${reading ? `<span class="pop-read">${esc(reading)}</span>` : ""}
    </div>
    <div class="pop-meta">词库里没有这个词${base && base !== surface ? `（原形 ${esc(base)}）` : ""}</div>
    <div class="pop-dict" id="mine-dict"><span class="pop-dim">正在查词典…</span></div>
    <form class="pop-form" id="mine-form">
      <label>读音（平假名）</label>
      <input class="ja" name="reading" value="${esc(reading)}" placeholder="よみかた">
      <label>中文释义（必填）</label>
      <input name="meaning" placeholder="点上面的义项即可填入">
      <label>例句（默认就是这一句）</label>
      <input class="ja" name="sentence" value="${esc(sentence)}">
      <div class="pop-actions">
        <button type="submit" class="pop-save">＋ 加入生词本</button>
        <button type="button" data-act="close">取消</button>
      </div>
      <div class="pop-err" id="mine-err"></div>
    </form>
    <div class="pop-occ" id="mine-occ"><span class="pop-dim">正在统计出处…</span></div>`;
  const p = $("word-popup");
  p.dataset.surface = surface;
  p.dataset.base = base || "";
  p.dataset.reading = reading || "";
  popupAt(x, y);
  const first = p.querySelector("input[name='reading']");
  if (first && !reading) first.focus();
  loadMineHints(surface, base, reading);
}

// 词性代码 → 中文（JMdict 的 v5r / adj-na 之类对学习者是噪音）
const POS_ZH = {
  n: "名词", pn: "代词", num: "数词", ctr: "助数词", adv: "副词",
  adj: "形容词", "adj-i": "イ形容词", "adj-na": "ナ形容词", "adj-no": "连体词",
  exp: "惯用语", int: "感叹词", conj: "接续词", prt: "助词", aux: "助动词",
  suf: "后缀", pref: "前缀", vi: "自动词", vt: "他动词", uk: "常用假名",
};

function posZh(list) {
  const out = (list || []).map((p) => {
    if (/^v5/.test(p)) return "五段动词";
    if (p === "v1") return "一段动词";
    if (p === "vs" || p === "vs-i") return "サ变动词";
    if (p === "vk") return "カ变动词";
    return POS_ZH[p] || "";
  }).filter(Boolean);
  return [...new Set(out)].join("・");
}

function dictHintsHtml(d) {
  if (!d || d.available === false) return "";
  if (!d.entries || !d.entries.length) {
    return `<span class="pop-dim">词典未收录这个词</span>`;
  }
  return d.entries.map((e, i) => `
    <div class="dict-cand" data-kana="${esc(e.kana)}">
      <div class="dict-cand-head">
        <span class="dict-kana">${esc(e.kana)}</span>
        <span class="dict-pos">${esc(posZh(e.senses[0] && e.senses[0].pos))}</span>
        ${d.entries.length > 1 ? `<span class="pop-dim">候选 ${i + 1}</span>` : ""}
      </div>
      <div class="dict-senses">
        ${e.senses.map((s) => `<button type="button" class="dict-sense"
          data-zh="${esc(s.zh)}" data-kana="${esc(e.kana)}">${esc(s.zh)}</button>`).join("")}
      </div>
    </div>`).join("") + `<div class="pop-dim dict-tip">点义项填入释义；同形异读时可换候选</div>`;
}

function occHintsHtml(o, workId) {
  if (!o || !o.total) {
    return `<span class="pop-dim">已导入的作品里没有其他出处</span>`;
  }
  const rows = o.works.map((w) => {
    const marks = w.chapters.slice(0, 6).map((c) => esc(c.title || String(c.n + 1))).join("、");
    const more = w.chapters.length > 6 ? ` 等 ${w.chapters.length} 章` : "";
    const here = w.work_id === workId ? `<span class="occ-here">本书</span>` : "";
    return `<li><span class="occ-w">${esc(w.title)}</span>${here}`
      + `<span class="occ-n">${w.count} 次</span>`
      + `<span class="occ-chs">${marks}${more}</span></li>`;
  }).join("");
  return `<div class="occ-head">在你的作品里共出现 <b>${o.total}</b> 次</div>`
    + `<ul class="occ-list">${rows}</ul>`;
}

async function loadMineHints(surface, base, reading) {
  const dictBox = $("mine-dict");
  try {
    const q = "surface=" + encodeURIComponent(surface)
      + "&base=" + encodeURIComponent(base || "")
      + "&reading=" + encodeURIComponent(reading || "");
    const d = await fetch("/api/dict?" + q).then((r) => r.json());
    if (dictBox) dictBox.innerHTML = dictHintsHtml(d);
  } catch (e) {
    if (dictBox) dictBox.innerHTML = "";
  }
  const occBox = $("mine-occ");
  try {
    const o = await fetch("/api/words/occurrences?surface=" + encodeURIComponent(surface))
      .then((r) => r.json());
    if (occBox) occBox.innerHTML = occHintsHtml(o, state.workId);
  } catch (e) {
    if (occBox) occBox.innerHTML = "";
  }
}

async function submitMine(form) {
  const fd = new FormData(form);
  const p = $("word-popup");
  const body = {
    surface: p.dataset.surface,
    base: p.dataset.base,
    reading: fd.get("reading"),
    meaning: fd.get("meaning"),
    sentence: fd.get("sentence"),
    chapter: state.chapter,
  };
  const res = await fetch("/api/works/" + encodeURIComponent(state.workId) + "/mine", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => r.json());
  if (res.error) {
    $("mine-err").textContent = res.error;
    if (res.duplicate) toast("词库里已经有了");
    return;
  }
  closePopup();
  toast("已加入" + (res.lesson_title || "生词本"));
  await withLoading("正在标注本章…", () => loadChapter(state.chapter));
}

// ---------- 修正分词（拖选几个字 → 右键 → 合并）----------
// 鼠标在正文里横向拖选几个字后右键，取选区覆盖到的相邻词块原样词面拼成「写法」
// （だ→から 拖过去就是「だから」）；「合并分词」把该写法放回整部作品扫描，
// 列出所有会被合并的句子（默认全勾），用户勾掉极少数本来切对的——
// 勾掉的只在当句保持切开，其余全书合并。
let reviewSurface = "";
let ctxTarget = null;             // 打开右键菜单那一刻的选区信息

// 扫描选区：返回覆盖到的词块（文档顺序）和选中的字面（不含 ruby 注音）。
// 逐个文本节点和 Range 比对，所以开着假名注音时也不会把注音算进选区。
function scanSelection() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return { segs: [], text: "" };
  const range = sel.getRangeAt(0);
  const box = $("text");
  const anc = range.commonAncestorContainer;
  const ancEl = anc.nodeType === 1 ? anc : anc.parentElement;
  if (!ancEl || !box.contains(ancEl)) return { segs: [], text: "" };

  const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      const p = n.parentElement;
      if (!p) return NodeFilter.FILTER_REJECT;
      if (p.tagName === "RT") return NodeFilter.FILTER_REJECT;   // 注音不算进选区
      return p.closest(".seg") ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const segs = [];
  let text = "";
  let n;
  while ((n = walker.nextNode())) {
    const len = n.nodeValue.length;
    if (range.comparePoint(n, len) < 0) continue;   // 整块都在选区起点之前
    if (range.comparePoint(n, 0) > 0) continue;     // 整块都在选区终点之后
    let s = 0, e = len;
    if (n === range.startContainer) s = range.startOffset;
    if (n === range.endContainer) e = range.endOffset;
    if (e <= s) continue;                           // 只是边界擦到，没真选中
    text += n.nodeValue.slice(s, e);
    const seg = n.parentElement.closest(".seg");
    if (seg && segs[segs.length - 1] !== seg) segs.push(seg);
  }
  return { segs, text };
}

// 拖选时首尾容易带上句号、空格这类碎块，合并写法里不该有它们
const EDGE_RE = /^[\s、。，．,.!！?？…‥:;；：]+$/;
function trimEdgeSegs(segs) {
  const out = segs.slice();
  while (out.length && EDGE_RE.test(out[0].dataset.t || "")) out.shift();
  while (out.length && EDGE_RE.test(out[out.length - 1].dataset.t || "")) out.pop();
  return out;
}

// 当前选区能不能拿来合并：{ segs, surface, text, ok, reason }
function selectionTarget() {
  const scan = scanSelection();
  const segs = trimEdgeSegs(scan.segs);
  const surface = segs.map((s) => s.dataset.t || "").join("");
  const text = scan.text;
  let reason = "";
  if (!segs.length) reason = "先用鼠标拖选正文里相邻的几个字";
  else if (segs.length < 2) reason = "只选中了一个词块，请多选一个相邻的";
  else if (segs.some((s) => s.closest(".sent") !== segs[0].closest(".sent"))) {
    reason = "请在同一句里选择";
  }
  return { segs, surface, text, ok: !reason && surface.length >= 2, reason };
}

function openCtxMenu(x, y, sentEl) {
  ctxSent = sentEl;
  const t = selectionTarget();
  ctxTarget = t;
  const menu = $("ctx-menu");
  $("ctx-merge").textContent = t.surface ? `🔧 合并分词「${t.surface}」…` : "🔧 合并分词…";
  $("ctx-merge").disabled = !t.ok;
  $("ctx-read").disabled = !t.text;
  $("ctx-copy").disabled = !t.text;
  $("ctx-edit-sent").disabled = !sentEl;
  const tip = $("ctx-tip");
  tip.textContent = t.ok ? "" : (t.reason || "先选中要合并的几个字");
  tip.hidden = t.ok;
  menu.hidden = false;
  const w = menu.offsetWidth, h = menu.offsetHeight;
  menu.style.left = Math.max(6, Math.min(x, window.innerWidth - w - 8)) + "px";
  menu.style.top = Math.max(6, Math.min(y, window.innerHeight - h - 8)) + "px";
}

function hideCtxMenu() { $("ctx-menu").hidden = true; }

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制");
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    toast("已复制");
  }
}

// ---------- 编辑句子文本（修 whisper / 台本转写错字）----------
let ctxSent = null;    // 右键命中的句子节点（带 pi/si 定位）

function openSentEdit(sentEl) {
  ctxSent = sentEl;
  $("sent-edit-err").textContent = "";
  $("sent-edit-text").value = (sentEl && sentEl.dataset.text) || "";
  $("sent-edit").hidden = false;
  $("sent-edit-text").focus();
}

async function saveSentEdit() {
  const sentEl = ctxSent;
  if (!sentEl) return;
  const text = $("sent-edit-text").value.trim();
  const err = $("sent-edit-err");
  if (!text) { err.textContent = "句子不能为空"; return; }
  if (text.length > 500) { err.textContent = "句子过长（最多 500 字）"; return; }
  const pi = parseInt(sentEl.dataset.pi, 10) || 0;
  const si = parseInt(sentEl.dataset.si, 10) || 0;
  const btn = $("sent-edit-save");
  btn.disabled = true;
  try {
    const res = await fetch(
      "/api/works/" + encodeURIComponent(state.workId) + "/chapter/" + state.chapter + "/sentence",
      {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ para: pi, index: si, text }),
      }).then((r) => r.json());
    if (res.error) { err.textContent = res.error; return; }
    $("sent-edit").hidden = true;
    toast("已保存，正在重新标注…");
    await withLoading("正在标注本章…", () => loadChapter(state.chapter));
  } finally {
    btn.disabled = false;
  }
}

async function startMerge(surface) {
  if (!surface) return;
  // 全书扫描较慢（逐句重新分词），给个遮罩
  const res = await withLoading("正在全书扫描…", () =>
    fetch("/api/works/" + encodeURIComponent(state.workId) + "/token_review", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ surface }),
    }).then((r) => r.json()));
  if (res.error) { toast(res.error); return; }
  const occ = res.occurrences || [];
  if (!occ.length) { toast("没找到可合并的位置，无需修正"); return; }
  reviewSurface = surface;
  $("review-surface").textContent = surface;
  $("review-hint").textContent =
    `本书共 ${occ.length} 句会出现「${surface}」。默认全部合并——若某处其实该切开（如「本だから」＝だ＋から），取消勾选即可，它只在当句保持分开。`;
  $("review-list").innerHTML = occ.map((o, i) => `
    <label class="tr-row">
      <input type="checkbox" data-i="${i}" checked>
      <span>${highlightSurface(o.sentence, surface)}<span class="cnt">×${o.count}</span></span>
    </label>`).join("");
  $("token-review")._occ = occ;
  $("token-review").hidden = false;
}

function highlightSurface(sent, surface) {
  const es = esc(surface);
  return esc(sent).split(es).join("<em>" + es + "</em>");
}

async function confirmReview() {
  const occ = $("token-review")._occ || [];
  const exceptions = [];
  $("review-list").querySelectorAll("input[type=checkbox]").forEach((b) => {
    if (!b.checked) exceptions.push(occ[+b.dataset.i].sentence);
  });
  const res = await fetch("/api/tokens", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ surface: reviewSurface, exceptions }),
  }).then((r) => r.json());
  if (res.error) { toast(res.error); return; }
  $("token-review").hidden = true;
  toast(`已修正「${reviewSurface}」，全书生效`);
  hideCtxMenu();
  const sel = window.getSelection();
  if (sel) sel.removeAllRanges();
  await withLoading("正在标注本章…", () => loadChapter(state.chapter));
}

// ---------- 修正表管理 ----------

async function openTokenPanel() {
  await renderTokens();
  $("token-panel").hidden = false;
}

async function renderTokens() {
  const data = await fetch("/api/tokens").then((r) => r.json());
  const keep = data.keep || [];
  const excs = data.split_exceptions || [];
  const box = $("token-list");
  if (!keep.length) { box.innerHTML = `<div class="tp-empty">还没有任何分词修正。</div>`; return; }
  box.innerHTML = keep.map((k) => {
    const n = excs.filter((e) => e.surface === k).length;
    return `<div class="tp-row">
      <span class="tp-word">${esc(k)}</span>
      <span class="tp-meta">${n ? `例外 ${n} 句` : "全书合并"}</span>
      <button class="tp-del secondary" type="button" data-del="${esc(k)}">撤销</button>
    </div>`;
  }).join("");
}

async function deleteToken(surface) {
  const res = await fetch("/api/tokens", {
    method: "DELETE", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ surface }),
  }).then((r) => r.json());
  if (res.error) { toast(res.error); return; }
  toast(`已撤销「${surface}」`);
  await renderTokens();
  await withLoading("正在标注本章…", () => loadChapter(state.chapter));
}

// ---------- 导入作品 ----------

function openImportPanel() {
  $("import-err").textContent = "";
  // 重新选过文件就用它的文件名补作品名（用户改过则不动）
  const fn = $("import-file").files && $("import-file").files[0];
  if (fn && !$("import-title").value.trim()) {
    $("import-title").value = fn.name.replace(/\.[^.]+$/, "");
  }
  $("import-panel").hidden = false;
}

function closeImportPanel() { $("import-panel").hidden = true; }

function importTitleFromFile() {
  const fn = $("import-file").files && $("import-file").files[0];
  if (fn && !$("import-title").value.trim()) {
    $("import-title").value = fn.name.replace(/\.[^.]+$/, "");
  }
}

async function submitImport(form) {
  const btn = $("import-submit");
  if (btn.disabled) return;
  const fd = new FormData(form);
  const file = fd.get("file");
  if (!(file && file.size)) {
    $("import-err").textContent = "请先选择要导入的文件";
    return;
  }
  // zip 是音声作品：解压 + 可能 ASR，明显比 txt/epub 久
  const isVoice = /\.zip$/i.test(file.name);
  btn.disabled = true;
  btn.textContent = isVoice ? "导入中（音声作品较慢）…" : "导入中…";
  $("import-err").textContent = "";
  try {
    const res = await withLoading(
      isVoice ? "正在解压并解析音声作品（没字幕又要 ASR 的话得等模型）…"
              : "正在导入并解析…",
      () => fetch("/api/works/import", { method: "POST", body: fd })
        .then((r) => r.json()));
    if (res.error) {
      $("import-err").textContent = res.error;
      return;
    }
    $("import-file").value = "";
    $("import-title").value = "";
    $("import-author").value = "";
    $("import-align").checked = false;
    closeImportPanel();
    toast(`已导入《${res.title}》：${res.chapters} 章 · ${res.sentences} 句`);
    await withLoading("正在标注首章…", () => refreshWorks(res.work_id, 0));
  } finally {
    btn.disabled = false;
    btn.textContent = "开始导入";
  }
}

// ---------- 事件 ----------

function wireEvents() {
  // 「← 返回」与练习页（.back-btn）同款：回上一页；直接打开本页时兜底回首页
  $("back-btn").addEventListener("click", () => {
    if (window.history.length > 1) window.history.back();
    else window.location.href = "/";
  });
  $("work-select").addEventListener("change",
    () => withLoading("正在载入作品…", () => loadWork($("work-select").value)));
  $("chapter-select").addEventListener("change",
    () => withLoading("正在标注本章…",
      () => loadChapter(parseInt($("chapter-select").value, 10) || 0)));
  $("prev-ch").addEventListener("click",
    () => withLoading("正在标注本章…", () => loadChapter(state.chapter - 1)));
  $("next-ch").addEventListener("click",
    () => withLoading("正在标注本章…", () => loadChapter(state.chapter + 1)));
  $("toggle-furigana").addEventListener("change", () => renderChapter());
  $("toggle-known").addEventListener("change", () => renderChapter());
  $("toggle-chips").addEventListener("change", () => renderChapter());

  // 修正分词：右键菜单 / 复查弹窗 / 修正表 / 句子编辑
  $("text").addEventListener("contextmenu", (e) => {
    e.preventDefault();          // 正文里用自定义菜单，别的地方照旧
    openCtxMenu(e.clientX, e.clientY, e.target.closest(".sent"));
  });
  $("ctx-menu").addEventListener("click", (e) => {
    const t = ctxTarget || selectionTarget();
    const item = e.target.closest(".cm-item");
    hideCtxMenu();
    if (!item) return;
    if (item.disabled) return;
    const id = item.id;
    if (id === "ctx-merge") { if (t.ok) startMerge(t.surface); }
    else if (id === "ctx-read") { if (t.text) playSentence(t.text); }
    else if (id === "ctx-copy") { if (t.text) copyText(t.text); }
    else if (id === "ctx-edit-sent") {
      if (ctxSent) openSentEdit(ctxSent);
    }
  });
  $("sent-edit-save").addEventListener("click", saveSentEdit);
  $("sent-edit").addEventListener("click", (e) => {
    if (e.target === $("sent-edit") || e.target.dataset.act === "cancel-sent-edit") {
      $("sent-edit").hidden = true;
    }
  });
  $("sent-edit-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) saveSentEdit();   // Ctrl+Enter 保存
    if (e.key === "Escape") $("sent-edit").hidden = true;
  });
  $("review-confirm").addEventListener("click", confirmReview);
  $("token-review").addEventListener("click", (e) => {
    if (e.target === $("token-review") || e.target.dataset.act === "cancel-review") {
      $("token-review").hidden = true;
    }
  });
  $("open-tokens").addEventListener("click", openTokenPanel);
  $("token-panel").addEventListener("click", (e) => {
    if (e.target === $("token-panel") || e.target.dataset.act === "close-tokens") {
      $("token-panel").hidden = true;
      return;
    }
    const del = e.target.closest("[data-del]");
    if (del) deleteToken(del.dataset.del);
  });

  $("text").addEventListener("click", (e) => {
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) return;      // 拖选/右键后别误弹词卡
    const seg = e.target.closest(".seg");
    if (!seg) return;
    e.stopPropagation();
    if (seg.dataset.ref) {
      openWordCard(seg.dataset.ref, e.pageX, e.pageY);
    } else {
      const host = seg.closest(".sent");
      openMineForm(seg.dataset.surface || seg.textContent,
        seg.dataset.reading || "", seg.dataset.base || "",
        host ? host.dataset.text : "", e.pageX, e.pageY);
    }
  });

  $("top-unknown").addEventListener("click", (e) => {
    const chip = e.target.closest(".uk-chip");
    if (!chip) return;
    e.stopPropagation();  // 别让 document 的关弹卡监听把刚开的表单立刻关掉
    openMineForm(chip.dataset.surface, "", "", "", e.pageX, e.pageY);
  });

  $("word-popup").addEventListener("click", (e) => {
    const act = e.target.dataset.act;
    if (act === "close") return closePopup();
    // 词典义项：点一下就填进释义（读音还空着就顺手补上），挖矿不必手打
    const sense = e.target.closest(".dict-sense");
    if (sense) {
      const form = $("mine-form");
      const m = form && form.querySelector("input[name='meaning']");
      const r = form && form.querySelector("input[name='reading']");
      if (m) m.value = sense.dataset.zh || "";
      if (r && !r.value.trim()) r.value = sense.dataset.kana || "";
      if (m) m.focus();
      return;
    }
    if (act === "word-audio") {
      return playAudio("/api/tts?ref=" + encodeURIComponent($("word-popup").dataset.ref));
    }
    if (act === "ex-audio") {
      return playAudio("/api/tts?ref=" + encodeURIComponent($("word-popup").dataset.ref)
        + "&field=example");
    }
  });
  $("word-popup").addEventListener("submit", (e) => {
    e.preventDefault();
    if (e.target.id === "mine-form") submitMine(e.target);
  });

  $("import-file").addEventListener("change", importTitleFromFile);
  $("import-panel").addEventListener("click", (e) => {
    if (e.target === $("import-panel") || e.target.dataset.act === "close") {
      closeImportPanel();
    }
  });
  $("import-panel").addEventListener("submit", (e) => {
    e.preventDefault();
    if (e.target.id === "import-form") submitImport(e.target);
  });

  // 音声作品：章节音轨播放条（整轨播放 / 拖动跳转 / 变速）
  $("media-play").addEventListener("click", () => {
    const el = mediaEl();
    if (!el) return;
    if (el.paused) {
      media.stopAt = null;                // 整轨播放：别被上一句的终点卡住
      el.play().catch(() => {});
    } else {
      el.pause();
    }
  });
  $("media-back").addEventListener("click", backToPrevSentence);
  $("media-seek").addEventListener("input", () => {
    const el = mediaEl();
    if (!el) return;
    media.stopAt = null;                  // 手动拖了进度条 = 放弃「播到句尾停」
    try { el.currentTime = parseFloat($("media-seek").value) || 0; } catch (e) { /* 忽略 */ }
    syncMediaUI();
  });
  $("media-rate").addEventListener("change", () => {
    const el = mediaEl();
    if (el) el.playbackRate = parseFloat($("media-rate").value) || 1;
  });
  $("sync-minus").addEventListener("click", () => setVoiceSync(-50));
  $("sync-plus").addEventListener("click", () => setVoiceSync(50));
  $("sync-reset").addEventListener("click", resetVoiceSync);
  initVoiceSync();

  // 「回到当前句」：滚轮 / 触摸是明确的人手信号，拖滚动条只有 scroll 事件
  window.addEventListener("scroll", onScrollMaybeUser, { passive: true });
  window.addEventListener("wheel", onUserScroll, { passive: true });
  window.addEventListener("touchmove", onUserScroll, { passive: true });
  $("follow-btn").addEventListener("click", backToPlayingSentence);
  // 勾掉「跟随高亮」：收起箭头（再勾上从当前位置继续跟）
  $("media-follow").addEventListener("change", () => setDetached(false));

  document.addEventListener("mousedown", (e) => {
    if (!e.target.closest("#ctx-menu")) hideCtxMenu();
  });
  window.addEventListener("scroll", hideCtxMenu, true);
  window.addEventListener("resize", hideCtxMenu);
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#word-popup") && !e.target.closest(".seg")) closePopup();
  });
  document.addEventListener("keydown", (e) => {
    // 空格 = 播放 / 暂停整轨（有音轨的章节）。输入框里打字不算——
    // 空格是挖矿表单里最常见的字符，别让「打法」把音频切了
    if (e.key === " " && !$("media-bar").hidden) {
      const tag = (document.activeElement && document.activeElement.tagName) || "";
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
      e.preventDefault();
      $("media-play").click();
      return;
    }
    if (e.key !== "Escape") return;
    hideCtxMenu();
    closePopup();
    if (!$("import-panel").hidden) closeImportPanel();
    if (!$("token-review").hidden) $("token-review").hidden = true;
    if (!$("token-panel").hidden) $("token-panel").hidden = true;
  });
}

init();
