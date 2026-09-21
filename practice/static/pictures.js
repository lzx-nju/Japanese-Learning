/* 图片日语（/pictures）：看真实日常照片学单词、语法与句式。
 *
 * 数据全在服务端算好（picture.py）：照片原文切好词块、课程单词挂上词库
 * （ref / 掌握度 / 是否在库）、照例在请求时实时算——挖一个词、练涨一次
 * 掌握度，刷新页面立刻反映到颜色上。
 *
 * 本文件只管交互：切课/切照片、朗读（词走 /api/tts，整句走 /api/tts/text）、
 * 点词看词卡、挖矿进生词本、练习入口。 */

const $ = (id) => document.getElementById(id);

const P = {
  lessons: [],       // 课程列表（下拉）
  lesson: null,      // 当前课载荷
  photo: 1,          // 当前照片序号
  audio: null,       // 复用同一个 Audio（iOS 上换实例会丢「已解锁」状态）
  mine: null,        // 挖矿弹窗的当前上下文
};

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, 2600);
}

function loading(on, text) {
  const mask = $("loading");
  if (text) $("loading-text").textContent = text;
  mask.hidden = !on;
}

async function api(url, opts) {
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  return { ok: res.ok, status: res.status, data: data || {} };
}

// ---------- 音频 ----------
function speak(url) {
  if (!P.audio) P.audio = new Audio();
  P.audio.pause();
  P.audio.src = url;
  P.audio.play().catch(() => toast("播放失败：首次生成语音需要联网"));
}

function speakWord(form, ref) {
  speak(ref ? `/api/tts?ref=${encodeURIComponent(ref)}`
           : `/api/tts/text?text=${encodeURIComponent(form)}`);
}

function speakText(text) {
  if (!text) return;
  // 与后端 TTS_TEXT_MAXLEN（200）同口径：超长整段不合成，明确提示而不是静默失败
  if (text.length > 200) return toast("这一行太长了（超过 200 字），拆成短句再听");
  speak(`/api/tts/text?text=${encodeURIComponent(text)}`);
}

// ---------- 词块与掌握度 ----------
function masteryClass(m) {
  if (!Number.isInteger(m) || m < 0 || m > 5) return null;
  if (m === 0) return "m-new";
  if (m <= 2) return "m-learn";
  return "m-known";
}

const MASTERY_TEXT = { "m-new": "库内未学", "m-learn": "在学（1-2）", "m-known": "会了（3+）" };

// 词性代码 → 中文（与 reader.js 的 posZh 同一口径，改一处记得改两处）
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

/* 词块：已学词按掌握度着色（w=词条 ref，m=掌握度）；库外词灰虚线可挖矿。 */
function segHtml(seg) {
  const text = escapeHtml(seg.t);
  if (seg.w) {
    const cls = masteryClass(seg.m) || "m-new";
    return `<span class="seg seg-${cls.slice(2)}" data-ref="${escapeHtml(seg.w)}"`
      + ` title="已在词库 · ${MASTERY_TEXT[cls]}"`
      + ` data-surface="${escapeHtml(seg.t)}">${text}</span>`;
  }
  return `<span class="seg" data-surface="${escapeHtml(seg.t)}"`
    + ` title="点一下：查词典 / 加入生词本">${text}</span>`;
}

function lineHtml(line) {
  // 一行原文：左边小喇叭，右边词块（没有 segs 说明是纯讲解文字，原样显示）
  const text = escapeHtml(line.text);
  const body = line.segs
    ? line.segs.map(segHtml).join("")
    : text;
  return `<div class="ptxt-line${line.segs ? "" : " plain"}">`
    + `<button type="button" class="mini-play" data-act="speak-text"`
    + ` data-text="${escapeHtml(line.text)}" title="朗读这一行">🔊</button>`
    + `<span class="ptxt-body${line.segs ? "" : " ptxt-plain"}">${body}</span></div>`;
}

function bindLineAudio(root) {
  root.querySelectorAll('[data-act="speak-text"]').forEach((btn) => {
    btn.addEventListener("click", () => speakText(btn.dataset.text || ""));
  });
}

/* 词块点击：已学词 → 词卡；库外词 → 挖矿弹窗（词典预填） */
function bindSegs(root) {
  root.querySelectorAll(".seg").forEach((el) => {
    el.addEventListener("click", () => {
      if (el.dataset.ref) return openWordCard(el.dataset.ref);
      // 例句取整行原文（.ptxt-body/.pt-text 里只有词块，不含左边的 🔊）
      const wrap = el.closest(".ptxt-body, .pt-text");
      const sentence = (wrap ? wrap.textContent : el.textContent) || "";
      return openMine(el.dataset.surface || el.textContent, "", "", sentence);
    });
  });
}

// ---------- 顶栏 / 页签 ----------
function renderBar() {
  const L = P.lesson;
  $("lesson-intro").textContent = L.intro || "";
  const refs = (L.stats.practice_refs || []).length;
  const btn = $("practice-btn");
  btn.textContent = refs ? `🖊 练本课单词（${refs} 词）` : "🖊 练本课单词";
  btn.disabled = !refs;
  btn.title = refs
    ? `把这 ${refs} 个已在词库的词带回练习页（看词选意思，先认再写）`
    : "本课单词还没进词库：先在核心单词表里点「＋ 生词本」";

  const tabs = $("photo-tabs");
  tabs.innerHTML = L.photos.map((ph) => `
    <button type="button" class="photo-tab${ph.n === P.photo ? " active" : ""}"
            data-photo="${ph.n}" title="${escapeHtml(ph.title)}">
      ${ph.n}. ${escapeHtml(ph.title)}
    </button>`).join("");
  tabs.querySelectorAll(".photo-tab").forEach((b) => {
    b.addEventListener("click", () => gotoPhoto(parseInt(b.dataset.photo, 10)));
  });
}

// ---------- 照片区 ----------
function renderPhoto() {
  const L = P.lesson;
  const ph = L.photos.find((x) => x.n === P.photo) || L.photos[0];
  if (!ph) return;
  P.photo = ph.n;
  const bust = encodeURIComponent(L.updated || "");
  $("photo-title").textContent = `写真${ph.n}：${ph.title}`;
  $("photo-link").href = `${ph.image_url}?v=${bust}`;
  $("photo-img").src = `${ph.image_url}?v=${bust}`;
  $("photo-img").alt = ph.title;
  $("photo-caption").textContent = ph.caption ? `图片文字（${ph.caption}）` : "图片文字";

  const box = $("photo-text");
  box.innerHTML = ph.texts.map((t) => lineHtml(t)).join("") || `<p class="hint">这张图没有登记文字。</p>`;
  bindLineAudio(box);
  bindSegs(box);

  renderWords(ph);
  renderPoints(ph);
  applyKnownToggle();
}

function renderWords(ph) {
  const box = $("word-list");
  box.innerHTML = ph.words.map((w) => {
    const cls = w.in_bank ? (masteryClass(w.mastery) || "m-new") : null;
    const side = w.in_bank
      ? `<span class="pw-mastery ${cls}">${MASTERY_TEXT[cls]}${w.mastery ? ` · ${w.mastery}` : ""}</span>`
      : `<button type="button" class="pw-mine" data-act="mine-word"
                 data-form="${escapeHtml(w.form)}" data-kana="${escapeHtml(w.kana)}"
                 data-meaning="${escapeHtml(w.meaning)}">＋ 生词本</button>`;
    return `<div class="pw-row" data-form="${escapeHtml(w.form)}">
      <button type="button" class="mini-play" data-act="speak-word"
              data-form="${escapeHtml(w.form)}" data-ref="${escapeHtml(w.ref || "")}"
              title="听发音">🔊</button>
      <div class="pw-main">
        <div class="pw-form">${escapeHtml(w.form)}
          ${w.kana && w.kana !== w.form ? `<span class="pw-kana">${escapeHtml(w.kana)}</span>` : ""}</div>
        <div class="pw-meaning">${escapeHtml(w.meaning)}</div>
        ${w.note ? `<div class="pw-note">${escapeHtml(w.note)}</div>` : ""}
      </div>
      <div class="pw-side">${side}</div>
    </div>`;
  }).join("") || `<p class="hint">这张图没有核心单词。</p>`;

  box.querySelectorAll('[data-act="speak-word"]').forEach((b) => {
    b.addEventListener("click", () => speakWord(b.dataset.form, b.dataset.ref));
  });
  box.querySelectorAll('[data-act="mine-word"]').forEach((b) => {
    b.addEventListener("click", () => {
      P.mine = null;
      $("mine-popup").hidden = true;
      saveMine({
        form: b.dataset.form, kana: b.dataset.kana, meaning: b.dataset.meaning,
        photo: P.photo,
      });
    });
  });
}

function renderPoints(ph) {
  const box = $("points");
  box.innerHTML = ph.points.map((pt) => `
    <div class="card pt-card">
      <h3><span class="pt-kind">${pt.kind === "grammar" ? "语法" : (pt.kind === "pattern" ? "句式" : "讲解")}</span>
        <span class="pt-body">${escapeHtml(pt.title)}</span></h3>
      <div class="pt-blocks">${pt.blocks.map(blockHtml).join("")}</div>
    </div>`).join("");
  bindLineAudio(box);
  bindSegs(box);
}

function blockHtml(b) {
  if (b.t === "ul") {
    return `<ul class="pt-ul">${b.items.map((it) => `
      <li>${playBtnHtml(it.text)}<span class="pt-text">${it.segs ? it.segs.map(segHtml).join("") : escapeHtml(it.text)}</span></li>`)
      .join("")}</ul>`;
  }
  const cls = b.t === "tip" ? "pt-tip" : "pt-p";
  return `<div class="${cls}">${playBtnHtml(b.text)}`
    + `<span class="pt-text">${b.segs ? b.segs.map(segHtml).join("") : escapeHtml(b.text)}</span></div>`;
}

function playBtnHtml(text) {
  return `<button type="button" class="mini-play" data-act="speak-text"`
    + ` data-text="${escapeHtml(text)}" title="朗读这一行">🔊</button>`;
}

// ---------- 小结与练习 ----------
function renderSummary() {
  const s = P.lesson.summary || {};
  const pts = s.points || [];
  $("summary-points").innerHTML = pts.length
    ? pts.map((p) => `<div class="sum-line">${playBtnHtml(p.text)}`
        + `<span class="pt-body">${p.segs ? p.segs.map(segHtml).join("") : escapeHtml(p.text)}</span></div>`).join("")
    : `<p class="hint">没有小结。</p>`;
  const words = s.words || [];
  $("summary-words").innerHTML = words.map((w) => {
    const cls = w.in_bank ? (masteryClass(w.mastery) || "m-new") : "m-new";
    const title = w.in_bank
      ? `${w.meaning || ""}（已在词库 · ${MASTERY_TEXT[cls]}）`
      : `${w.form}：还没进词库，点一下看释义/加入生词本`;
    return `<span class="sw-chip ${cls}" data-form="${escapeHtml(w.form)}"
                  data-ref="${escapeHtml(w.ref || "")}" data-meaning="${escapeHtml(w.meaning || "")}"
                  data-kana="${escapeHtml(w.kana || "")}" title="${escapeHtml(title)}">
      ${escapeHtml(w.form)}${w.kana ? `<span class="sw-kana">${escapeHtml(w.kana)}</span>` : ""}</span>`;
  }).join("") || `<p class="hint">没有核心单词名单。</p>`;

  $("summary-words").querySelectorAll(".sw-chip").forEach((el) => {
    el.addEventListener("click", () => {
      if (el.dataset.ref) return openWordCard(el.dataset.ref);
      P.mine = null;
      $("mine-popup").hidden = true;
      if (el.dataset.meaning) {
        saveMine({ form: el.dataset.form, kana: el.dataset.kana,
                   meaning: el.dataset.meaning, photo: P.photo });
      } else {
        openMine(el.dataset.form, "", "", "");
      }
    });
  });
  const box = $("summary-card");
  bindLineAudio(box);
  bindSegs(box);
}

function renderExercises() {
  const exs = P.lesson.exercises || [];
  const box = $("exercises");
  if (!exs.length) { box.innerHTML = `<p class="hint">这一课没有练习。</p>`; return; }
  box.innerHTML = exs.map((ex) => {
    const items = (ex.items || []).map((it, i) => `
      <div class="ex-item">
        <div class="ex-q">
          <span>${i + 1}.</span>
          <div>
            <div>${escapeHtml(it.q)}</div>
            ${it.hint ? `<div class="ex-hint">提示：${escapeHtml(it.hint)}</div>` : ""}
            ${it.answer ? `<button type="button" class="ex-toggle" data-act="toggle-answer">显示参考答案</button>
              <div class="ex-answer" hidden><button type="button" class="mini-play"
                data-act="speak-text" data-text="${escapeHtml(it.answer)}">🔊</button>
                <span class="pt-body">${escapeHtml(it.answer)}</span></div>` : ""}
          </div>
        </div>
      </div>`).join("");
    const blocks = (ex.blocks || []).map((b) => b.t === "tip"
      ? `<div class="pt-tip">${escapeHtml(b.text)}</div>`
      : `<p>${escapeHtml(b.text)}</p>`).join("");
    const groupAns = ex.answer
      ? `<button type="button" class="ex-toggle" data-act="toggle-answer">显示参考答案</button>
         <div class="ex-answer" hidden><button type="button" class="mini-play"
           data-act="speak-text" data-text="${escapeHtml(ex.answer)}">🔊</button>
           <span class="pt-body">${escapeHtml(ex.answer)}</span></div>`
      : "";
    return `<div class="ex-group"><h3>${ex.n}. ${escapeHtml(ex.title)}</h3>${blocks}${items}${groupAns}</div>`;
  }).join("");

  box.querySelectorAll('[data-act="toggle-answer"]').forEach((b) => {
    b.addEventListener("click", () => {
      const ans = b.nextElementSibling;
      ans.hidden = !ans.hidden;
      b.textContent = ans.hidden ? "显示参考答案" : "收起参考答案";
    });
  });
  bindLineAudio(box);
}

// ---------- 词卡 / 挖矿 ----------
function closePopup() { $("word-popup").hidden = true; }

function openWordCard(ref) {
  const w = (P.lesson.vocab_words || {})[ref];
  if (!w) return;
  const cls = masteryClass(w.mastery) || "m-new";
  const ex = w.example_ja ? `
    <div class="pop-ex">
      <button type="button" class="mini-play" data-act="speak-text"
              data-text="${escapeHtml(w.example_ja)}" title="朗读例句">🔊</button>
      <span class="pt-body">${escapeHtml(w.example_ja)}</span>
      <div class="pop-dim">${escapeHtml(w.example_zh || "")}</div>
    </div>` : "";
  const tip = w.tip ? `<div class="pop-dim">💡 ${escapeHtml(w.tip)}</div>` : "";
  const img = w.image ? `<img class="pop-img" src="${escapeHtml(w.image)}" alt="配图">` : "";
  const pop = $("word-popup");
  pop.innerHTML = `
    <span class="pop-close" data-act="close-word">✕</span>
    <div class="pop-head">
      <span class="pop-word">${escapeHtml(w.form)}</span>
      ${w.kana ? `<span class="pop-read">${escapeHtml(w.kana)}</span>` : ""}
      <span class="pw-mastery ${cls}">${MASTERY_TEXT[cls]}${w.mastery ? ` · ${w.mastery}` : ""}</span>
    </div>
    <div class="pop-meaning">${escapeHtml(w.meaning)}</div>
    ${tip}${ex}${img}
    <div class="pop-actions">
      <button type="button" class="secondary" data-act="speak-word"
              data-form="${escapeHtml(w.form)}" data-ref="${escapeHtml(ref)}">🔊 读单词</button>
      <button type="button" class="secondary" data-act="focus-word">🎯 加入重点词</button>
      <button type="button" data-act="close-word">关闭</button>
    </div>
    <div class="pop-err" id="pop-msg"></div>`;
  pop.hidden = false;
  pop.querySelectorAll('[data-act="close-word"]').forEach((b) => b.addEventListener("click", closePopup));
  bindLineAudio(pop);
  pop.querySelector('[data-act="speak-word"]').addEventListener("click", (e) => {
    speakWord(e.currentTarget.dataset.form, e.currentTarget.dataset.ref);
  });
  pop.querySelector('[data-act="focus-word"]').addEventListener("click", async () => {
    const r = await api("/api/focus", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "add", refs: [ref] }),
    });
    const msg = $("pop-msg");
    msg.textContent = r.data.error || `已加入重点词清单（现在 ${r.data.count} 个，可回首页练）`;
    msg.style.color = r.data.error ? "var(--wrong)" : "var(--correct)";
  });
}

/* 挖矿弹窗：form/kana/meaning 已知就预填（核心单词表直接保存时不走这里，
   这里是「原文里点到库外词」的路径——用本地词典预填读音与义项）。 */
async function openMine(form, kana, meaning, sentence) {
  closePopup();
  P.mine = { form: form, sentence: sentence || "", photo: P.photo };
  $("mine-form-label").textContent = form;
  $("mine-reading").value = kana || "";
  $("mine-meaning").value = meaning || "";
  $("mine-err").textContent = "";
  $("mine-dict").hidden = true;
  $("mine-dict-list").innerHTML = "";
  $("mine-popup").hidden = false;
  $("mine-reading").focus();

  if (kana && meaning) return;   // 已经齐了，不再查词典
  const r = await api(`/api/dict?surface=${encodeURIComponent(form)}`);
  if (!r.ok || !r.data.available || !(r.data.entries || []).length) return;
  // 词典可能给多个候选（同形异读）；逐个列出，点义项填释义、点「用这个读音」填读音
  $("mine-dict").hidden = false;
  $("mine-dict-list").innerHTML = r.data.entries.slice(0, 5).map((e) => {
    const senses = (e.senses || []).map((s) =>
      `<span class="dict-sense" data-act="use-sense"
             data-sense="${escapeHtml(s.zh || "")}">${escapeHtml(s.zh || "")}</span>`).join("");
    const pos = posZh(e.senses && e.senses[0] && e.senses[0].pos);
    return `<div class="dict-cand">
      <div class="dict-cand-head">
        <span class="dict-kana">${escapeHtml(e.kana || e.kanji || "")}</span>
        ${pos ? `<span class="dict-pos">${escapeHtml(pos)}</span>` : ""}
        <button type="button" class="ex-toggle" data-act="use-reading"
                data-reading="${escapeHtml(e.kana || "")}">用这个读音</button>
      </div>
      <div class="dict-senses">${senses || `<span class="pop-dim">（词典没有义项）</span>`}</div>
    </div>`;
  }).join("");
  const list = $("mine-dict-list");
  list.querySelectorAll('[data-act="use-reading"]').forEach((b) => {
    b.addEventListener("click", () => {
      $("mine-reading").value = b.dataset.reading || "";
      $("mine-reading").focus();
    });
  });
  list.querySelectorAll('[data-act="use-sense"]').forEach((b) => {
    b.addEventListener("click", () => {
      $("mine-meaning").value = b.dataset.sense || "";
      $("mine-meaning").focus();
    });
  });
}

async function saveMine(info) {
  const r = await api(`/api/pictures/${encodeURIComponent(P.lesson.id)}/mine`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      form: info.form, kana: info.kana, meaning: info.meaning,
      sentence: info.sentence || "", photo: info.photo || P.photo,
    }),
  });
  if (r.ok) {
    toast(`已加入生词本：${info.form}`);
  } else if (r.status === 409) {
    toast(`${info.form} 已在词库里（${r.data.ref}），没有重复添加`);
  } else {
    $("mine-err").textContent = r.data.error || "保存失败";
    toast(r.data.error || "保存失败");
    return false;
  }
  $("mine-popup").hidden = true;
  await reloadLesson();   // 重新标注：新词立刻按「库内未学」显示、练习按钮更新
  return true;
}

async function reloadLesson() {
  const keep = P.photo;
  await loadLesson(P.lesson.id, keep);
}

// ---------- 载入 ----------
async function loadLessons() {
  const r = await api("/api/pictures");
  P.lessons = r.data.lessons || [];
  const sel = $("lesson-select");
  sel.innerHTML = P.lessons.map((l) => `
    <option value="${escapeHtml(l.id)}">${escapeHtml(l.title)}
      （${l.photo_count} 图 / ${l.word_count} 词，已入库 ${l.practice_count || 0}）</option>`).join("");
  return P.lessons;
}

async function loadLesson(id, photo) {
  loading(true, "加载课程…");
  try {
    const r = await api(`/api/pictures/${encodeURIComponent(id)}`);
    if (!r.ok) {
      alert(r.data.error || "课程加载失败");
      return;
    }
    P.lesson = r.data;
    P.photo = photo || P.photo || 1;
    if (!P.lesson.photos.some((p) => p.n === P.photo)) {
      P.photo = (P.lesson.photos[0] || {}).n || 1;
    }
    try { localStorage.setItem("picLesson", id); } catch (e) { /* 隐私模式忽略 */ }
    $("lesson-select").value = id;
    renderBar();
    renderPhoto();
    renderSummary();
    renderExercises();
  } finally {
    loading(false);
  }
}

function gotoPhoto(n) {
  if (!P.lesson) return;
  const total = P.lesson.photos.length;
  P.photo = Math.min(total, Math.max(1, n));
  try { localStorage.setItem(`picPhoto:${P.lesson.id}`, String(P.photo)); } catch (e) { /* 忽略 */ }
  renderBar();
  renderPhoto();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function applyKnownToggle() {
  // 挂 body：讲解卡片不在照片区里，只切 .pic-figure 的话它们的高亮关不掉
  document.body.classList.toggle("no-known", !$("toggle-known").checked);
}

function boot() {
  const params = new URLSearchParams(location.search);
  // 「← 返回」与练习页（.back-btn）同款：回上一页；直接打开本页时兜底回首页
  $("back-btn").addEventListener("click", () => {
    if (window.history.length > 1) window.history.back();
    else window.location.href = "/";
  });
  $("lesson-select").addEventListener("change", (e) => {
    let p = 1;
    try { p = parseInt(localStorage.getItem(`picPhoto:${e.target.value}`) || "1", 10) || 1; } catch (err) { /* 忽略 */ }
    loadLesson(e.target.value, p);
  });
  $("prev-photo").addEventListener("click", () => gotoPhoto(P.photo - 1));
  $("next-photo").addEventListener("click", () => gotoPhoto(P.photo + 1));
  $("toggle-known").addEventListener("change", applyKnownToggle);
  $("practice-btn").addEventListener("click", () => {
    const refs = (P.lesson && P.lesson.stats.practice_refs) || [];
    if (!refs.length) return toast("本课还没有词进词库：先在单词表里点「＋ 生词本」");
    // 回练习页并自动开练：app.js 认 ?practice_refs=…（refs 点名词条，绕过练习池）。
    // 选「看词选意思」是因为它对词形没有要求——刚挖的词里常有 ポーズ 这类
    // 片假名词（kanji 记 ---），「看汉字写假名」会把它们整批筛掉、题数对不上
    const q = new URLSearchParams({ practice_refs: refs.join(","), practice_mode: "kana_to_cn" });
    location.href = `/?${q.toString()}`;
  });
  $("word-popup").addEventListener("click", (e) => {
    if (e.target === $("word-popup")) closePopup();
  });
  $("mine-popup").querySelectorAll('[data-act="close-mine"]').forEach((b) => {
    b.addEventListener("click", () => { $("mine-popup").hidden = true; });
  });
  $("mine-save").addEventListener("click", () => {
    if (!P.mine) return;
    const reading = $("mine-reading").value.trim();
    const meaning = $("mine-meaning").value.trim();
    if (!meaning) { $("mine-err").textContent = "请填写中文释义"; return; }
    saveMine({ form: P.mine.form, kana: reading, meaning: meaning,
               sentence: P.mine.sentence, photo: P.mine.photo });
  });

  // 左右方向键翻照片（输入框里打字不算）
  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (e.key === "ArrowLeft") gotoPhoto(P.photo - 1);
    else if (e.key === "ArrowRight") gotoPhoto(P.photo + 1);
    else if (e.key === "Escape") { closePopup(); $("mine-popup").hidden = true; }
  });

  (async () => {
    loading(true, "加载课程列表…");
    try {
      const lessons = await loadLessons();
      if (!lessons.length) {
        $("lesson-intro").textContent =
          "还没有导入任何图片课：把课程稿写成 markdown，跑一次 "
          + "python practice\\import_picture_lesson.py --write 即可（见 README）。";
        return;
      }
      const want = params.get("lesson")
        || (() => { try { return localStorage.getItem("picLesson"); } catch (e) { return null; } })();
      const id = lessons.some((l) => l.id === want) ? want : lessons[0].id;
      const p = parseInt(params.get("photo") || "0", 10) || undefined;
      await loadLesson(id, p);
    } finally {
      loading(false);
    }
  })();
}

document.addEventListener("DOMContentLoaded", boot);
