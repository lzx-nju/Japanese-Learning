// 日语练习 - 前端逻辑
const state = {
  config: null,
  modeName: "",
  questions: [],
  index: 0,
  correctCount: 0,
  results: [], // {ref, correct, prompt, your_answer, expected, word}
  // 学习指标
  streak: 0,           // 当前连续答对数
  maxStreak: 0,        // 本次练习最大连续答对数
  questionStartTime: 0,// 当前题目开始作答的时间戳
  reactionTimes: [],   // 每题反应时间（毫秒）
  cardFlipped: false,  // 闪卡：当前卡片是否已翻面
  examAnswered: false, // 真题：当前题是否已作答
  rawInput: false,     // 本组默认禁用罗马字转换（每题判定见 rawInputFor）
  noAudio: false,      // 本组听力题已静音：跳过过听力题后不再自动播放（可手动恢复）
  inProgress: false,   // 一组练习进行中且尚未 /api/finish，用于离开页面提醒
  checking: false,     // /api/check 在途：防止 Enter 连按重复计分
  regenBusy: false,    // /api/tts/regen 在途：防止重复点击重新生成
  choiceCursor: 0,     // 选择题高亮游标：↑/↓ 移动、Enter 确认（数字键仍可直选）
  finishing: false,    // /api/finish 在途：防止交卷重复 POST
  starting: false,     // /api/start 在途：防止开始按钮双击
  sessionId: "",       // 本次会话标识：后端据此识别重复交卷（幂等）
  // 本组练习的交互描述：{task, choice, typing, image, audio, spoiler, raw_input}
  // 由后端按「题干 × 作答」矩阵下发（课程模式另有每题一份，见 uiOf）
  modeUI: null,
};

const $ = (id) => document.getElementById(id);

// 当前题的交互描述：课程模式取该题内层题型的 ui，其余模式取本组的 ui
function uiOf(q) {
  return (q && q.ui) || state.modeUI || {};
}

// 当前题是否要关掉罗马字转换（答案不是假名，如「看假名写汉字」）。
// 必须按**每题一份**的 ui 判定：课程模式的题是混合题型，用本组的下发值判断
// 会把内层写汉字题的输入也实时转成假名（打出来的汉字当场被改写，必然判错）。
function rawInputFor(q) {
  const ui = uiOf(q);
  return ui.raw_input === undefined ? state.rawInput : !!ui.raw_input;
}

// 输入框占位文案：按作答方式提示（与 rawInputFor 同一口径）
function setAnswerPlaceholder(q) {
  $("answer-input").placeholder = rawInputFor(q)
    ? "用日文输入法写汉字（只打假名会判错）"
    : "输入假名或罗马字（如 norikae）…";
}

// 听音题干的占位文案按「作答方式」给出（题干本身不能剧透答案）
const AUDIO_PROMPT = {
  write_kana: "🔊 听音写假名",
  choose_cn: "🔊 听音选意思",
  choose_form: "🔊 听音选汉字",
};

// 当前题：普通模式取 questions[index]，课程模式取 course.q（回炉轮次也一致）
function currentQ() {
  if (state.config && state.config.mode === "course") return course.q;
  return state.questions[state.index];
}

// 当前题是否「只能靠听」作答：题干含发音的题型都是（听音写假名/听音选意思/
// 看图听音选读音）；选项本身是发音的（看词形选发音/看图选发音）听不到同样答不了，
// 一并算进来；真题只有带官方音频的聴解；课程模式按内层题型判断
function isListeningQuestion(q) {
  if (!q) return false;
  const m = state.config ? state.config.mode : "";
  if (m === "exam") return !!q.audio;
  const ui = uiOf(q);
  return ui.audio === true || ui.audio_options === true;
}

// 听力题的「不方便听」行显隐：只在听力题未作答时出现；
// 本组静音后额外亮出「恢复自动发音」
function refreshNoAudioRow() {
  const row = $("no-audio-row");
  if (!row) return;
  const q = currentQ();
  const show = isListeningQuestion(q) && !state.examAnswered;
  row.classList.toggle("hidden", !show);
  if (show) $("restore-audio-btn").classList.toggle("hidden", !state.noAudio);
}

// 「跳过听力题」开关（记在本机）：上课或任何不方便出声的场合，一次把本轮的听力题
// 全部剔掉，不必一道道点「不方便听」。记忆是有意的——一堂课里会开好几组练习，
// 每开一组都要重新点一次就白做了这个功能。
const SKIP_AUDIO_KEY = "skip_audio_v1";

function skipAudioPref() {
  try {
    return localStorage.getItem(SKIP_AUDIO_KEY) === "1";
  } catch (e) {
    return false;   // 隐私模式/禁用存储：退化成本次会话手动开关，不影响使用
  }
}

function setSkipAudioPref(on) {
  try {
    if (on) localStorage.setItem(SKIP_AUDIO_KEY, "1");
    else localStorage.removeItem(SKIP_AUDIO_KEY);
  } catch (e) { /* 存不了就只对本次会话生效 */ }
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// 所有屏幕：新增屏幕只要把 id 加进这个数组（顺序无关，靠 id 显隐）
const SCREENS = ["home-screen", "module-screen", "config-screen", "practice-screen",
                 "learn-screen", "kanji-screen", "conj-screen", "docs-screen",
                 "result-screen", "focus-screen"];

function showScreen(name) {
  for (const s of SCREENS) {
    $(s).style.display = s === name ? "" : "none";
  }
  // 当前屏也记到 body 上：宽屏的版心宽度按屏区分（首页 1120、其余 680，见 style.css）。
  // 只能用属性而不是给 .screen 加样式——显隐是内联 display，会盖掉样式表里的规则。
  document.body.dataset.screen = name;
  syncNavHash(name);
}

// 把当前层级记进 URL hash（replaceState 不新增历史条目）：模块/配置/练习屏时
// 是 /#module=<id>，回首页就清掉。从阅读器等独立页面 history.back() 回来时，
// bootFromHash 按它直接落回原模块页——「← 返回」因此是逐级回退。
function syncNavHash(name) {
  const id = nav.module ? nav.module.id : "";
  const want = (name !== "home-screen" && id)
    ? location.pathname + "#module=" + id
    : location.pathname;
  if (location.pathname + location.hash !== want) {
    history.replaceState(null, "", want);
  }
}

// ---------- 首页导航：四宫格 → 子项 → 配置 → 练习 ----------
// 层级：home（四个方块）→ module（子项列表）→ config（参数）→ 练习/学习/浏览。
// nav.item 决定配置页显示哪些参数行、以及「开始」按钮走哪条流程。
const nav = {
  home: null,    // /api/home 的完整数据（方块 + 概览）
  module: null,  // 当前方块 {id, icon, title, items}
  item: null,    // 当前子项 {id, kind, name, desc}
};

// 今日处方的步骤（/api/plan 下发）：前端不硬编码任何一步，点一步就按该步
// 自带的参数直接开练——处方要解决的正是「每次都得自己去配置页配一遍」。
let planSteps = [];

async function loadHome(opts = {}) {
  try {
    const res = await fetch("/api/home");
    nav.home = await res.json();
  } catch (e) {
    alert("加载首页失败，请确认后端已启动：" + e);
    return;
  }
  await loadPlan();   // 处方拿不到不影响首页其他部分
  renderOverview(nav.home.overview || {});
  renderTiles(nav.home.modules || []);
  // 练完回首页时只刷数据，不重放 #module= 直达（那是给独立页面返回用的）
  if (!opts.skipBoot) bootFromHash();
}

async function loadPlan() {
  try {
    const res = await fetch("/api/plan");
    planSteps = (await res.json()).steps || [];
  } catch (e) {
    planSteps = [];
  }
  renderPlan();
}

function renderPlan() {
  const box = $("plan");
  box.classList.toggle("hidden", !planSteps.length);
  if (!planSteps.length) {
    box.innerHTML = "";
    return;
  }
  // 当任务清单用：从上往下一项一项做。做过的那项换浅底 + ✓，最上面还没做的高亮成
  // 「现在做这一项」。「做过」由后端从当天的练习记录现算（done / done_today），
  // 前端不自己记账；做过之后仍然可点——积压还在，处方按当前积压算，不是一次性打卡表。
  const nextIdx = planSteps.findIndex((s) => !s.done);
  box.innerHTML = `
    <div class="plan-head">
      <span class="plan-title">💊 今日处方</span>
    </div>
    <div class="plan-steps">${planSteps.map((s, i) => `
      <button type="button" class="plan-step${s.done ? " done" : ""}${i === nextIdx ? " next" : ""}"
              data-step="${escapeHtml(s.id)}">
        <span class="ps-idx">${s.done ? "✓" : i + 1}</span>
        <span class="ps-main">
          <span class="ps-title">${escapeHtml(s.title)}</span>
          <span class="ps-desc">${escapeHtml(s.desc)}</span>
        </span>
        <span class="ps-count">${s.done
          ? `今天已做 ${escapeHtml(s.done_today)} ${escapeHtml(s.unit || "题")}`
          : `${escapeHtml(s.count)} ${escapeHtml(s.unit || "题")}`}</span>
      </button>`).join("")}</div>`;
}

function startPlanStep(stepId) {
  const step = planSteps.find((s) => s.id === stepId);
  if (!step || !step.start) return;
  const st = step.start;
  // 从首页直接开练：清掉导航上下文，练完回首页而不是上次进过的模块页
  nav.module = null;
  nav.item = null;
  if (st.kind === "learn") return startLearn({ lesson: st.lesson, count: st.count });
  return startSession(st);
}

// /#module=<id> 直达：从阅读器/图片日语等独立页面 history.back() 回来时，
// 浏览器落在 /#module=<id>，按 hash 恢复原模块页（返回按钮的逐级回退靠它）。
function bootFromHash() {
  const m = /^#module=([\w-]+)$/.exec(location.hash || "");
  if (m) openModule(m[1]);
}

function renderOverview(ov) {
  const chips = [
    // 「待复习」只数练过的词：新词单列在右侧「未学词」。
    // 两个数都计入「今日到期」那个总数，但混着看会以为积压了一千多词
    { label: "待复习", value: ov.review_due, unit: "词", tone: ov.review_due > 0 ? "hot" : "" },
    { label: "今日已练", value: ov.today_done, unit: "题" },
    { label: "连续练习", value: ov.streak_days, unit: "天" },
    { label: "未学词", value: ov.unlearned, unit: "词" },
    { label: "题库到期", value: ov.due_bank, unit: "题" },
    { label: "错题本", value: ov.wrong_words, unit: "词" },
  ];
  $("overview").innerHTML = chips.map((c) => `
    <div class="ov-chip ${c.tone}">
      <span class="ov-value">${escapeHtml(c.value || 0)}<span class="ov-unit">${escapeHtml(c.unit)}</span></span>
      <span class="ov-label">${escapeHtml(c.label)}</span>
    </div>`).join("");
}

function renderTiles(modules) {
  const box = $("tiles");
  box.innerHTML = "";
  modules.forEach((m) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tile";
    btn.dataset.module = m.id;
    // 方块只留「图标 + 标题 + 角标」：那行描述由后端不再下发（见 MODULE_DEFS），
    // 首页要的是「一眼看清有哪几块、每块欠多少」，不是再读一遍目录
    btn.innerHTML = `
      <span class="tile-icon">${escapeHtml(m.icon)}</span>
      <span class="tile-title">${escapeHtml(m.title)}</span>
      <span class="tile-badge">${escapeHtml(m.badge || "")}</span>`;
    box.appendChild(btn);
  });
}

function openModule(id) {
  const m = (nav.home && nav.home.modules || []).find((x) => x.id === id);
  if (!m) return;
  nav.module = m;
  nav.item = null;
  $("module-title").textContent = `${m.icon} ${m.title}`;
  $("module-badge").textContent = m.badge || "";
  renderModuleItems(m);
  showScreen("module-screen");
}

// 子项按后端给的 group 分组展示（首次出现顺序即分组顺序；group 为空则不分组）
function renderModuleItems(m) {
  const box = $("module-items");
  box.innerHTML = "";
  const groups = [];
  const byGroup = new Map();
  (m.items || []).forEach((it) => {
    const g = it.group || "";
    if (!byGroup.has(g)) {
      byGroup.set(g, []);
      groups.push(g);
    }
    byGroup.get(g).push(it);
  });
  groups.forEach((g) => {
    if (g) {
      const h = document.createElement("h3");
      h.className = "group-title";
      h.textContent = g;
      box.appendChild(h);
    }
    const list = document.createElement("div");
    list.className = "item-list";
    byGroup.get(g).forEach((it) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sub-item";
      btn.dataset.item = it.id;
      // 方块 + 悬停弹层：方块里只有「名字 + 角标」，鼠标移上去才在旁边浮出那条
      // 说明。说明是常驻 DOM（不是悬停时才插入），键盘 focus 同样能看到；浮层
      // 不占布局，鼠标扫过网格不会有任何东西被顶动。触屏没有 hover，CSS 里那层
      // 不生效，说明就按普通文字排在方块内。
      btn.innerHTML = `
        <span class="si-main">
          <span class="si-name">${escapeHtml(it.name)}</span>
          ${it.badge ? `<span class="si-badge">${escapeHtml(it.badge)}</span>` : ""}
          <span class="si-desc">${escapeHtml(it.desc || "")}</span>
        </span>`;
      list.appendChild(btn);
    });
    box.appendChild(list);
  });
}

function openItem(itemId) {
  const m = nav.module;
  if (!m) return;
  const it = (m.items || []).find((x) => x.id === itemId);
  if (!it) return;
  nav.item = it;
  if (it.kind === "add_word") return openAddWord();      // 直接弹录入框，没有参数页
  if (it.kind === "rename_lesson") return openRenameLesson();  // 直接弹改名框
  if (it.kind === "stats") { window.location.href = "/stats"; return; }
  if (it.kind === "reader") { window.location.href = "/reader"; return; }
  if (it.kind === "import") { window.location.href = "/import"; return; }
  if (it.kind === "pictures") { window.location.href = "/pictures"; return; }
  if (it.kind === "docs") return openDocs();
  if (it.kind === "focus") return openFocus();   // 重点词清单页（不走配置页）
  if (it.kind === "mode") selectMode(it.id);
  openConfig();
}

// 每批新词数量：与 app.py 的 LEARN_BATCH_DEFAULT 对齐（后端 /api/learn/start
// 也接受 count，两边对不上就会出现「按钮写 7、实际学 5」）
const LEARN_BATCH = 7;

// 配置页：不同子项要填的参数不同（真题要选卷、学习与汉字卡只要课程）
const CONFIG_START_LABEL = {
  learn: `开始学习（${LEARN_BATCH} 个新词）`,
  kanji: "打开汉字卡",
};
// 只写各模式特有的提示；凡是「题干含发音」的通用提示由 ui.audio 统一生成
const CONFIG_HINTS = {
  kana_to_kanji: "此模式需切日文输入法打汉字（只打假名会判错），罗马字转换已自动关闭。",
  dictation: "整句语音首次播放要联网生成（之后按句子缓存，下一题会提前预取）。",
  pair_match: "「题目数量」在此模式下是轮数（每轮 5 对）。",
  conjugation: "同一词先四选一热身、再打字巩固；打字题需切日文输入法，全假名作答（ひらいて）也算对。",
  exam: "可按卷与科目限定范围；聴解题会自动播放官方音频切片。",
  course: "一课约 15 题：先预习新词，再混合练旧词，答错的课末统一回炉重做。",
};
const AUDIO_HINT = "进入题目自动播放一次，按 F2 或 Ctrl+R 重播；不方便听可「跳过本题」。";

// 模式列表（/api/config 拉回，带 ui 描述）：配置页据此生成提示，不再按 id 硬编码
let configModes = [];

function modeUIById(id) {
  const m = configModes.find((x) => x.id === id);
  return (m && m.ui) || {};
}

function openConfig() {
  const it = nav.item;
  if (!it) return showScreen("config-screen");
  const m = nav.module || {};
  $("config-crumb").textContent = `${m.icon || ""} ${m.title} › ${it.name}`;
  $("config-title").textContent = it.name;
  const isMode = it.kind === "mode";
  // 独立题库（助词/真题）不按课程出题，课程范围没有意义
  const bankMode = isMode && (it.id === "particle" || it.id === "exam");
  // 错题本：课程锁死为虚拟课程，练法仍自选，所以模式行保留
  const lockLesson = it.kind === "wrongbook";
  if (lockLesson) $("lesson").value = "lesson_wrong";
  const lessonOnly = it.kind === "learn" || it.kind === "kanji" || it.kind === "conj";
  const withCount = isMode || it.kind === "wrongbook";
  toggleRow("modes-row", it.kind === "wrongbook");
  // 练习范围只对按词库出题的模式开放：助词/真题是独立题库，
  // 学新词与汉字卡走自己的接口，都不吃重点词
  toggleRow("scope-row", isMode && !bankMode);
  // 清单页勾了「只练重点词」是长期开关：进配置页就默认带上，
  // 想临时练整本词库，在这一行切回「按课程范围」即可
  if (isMode && !bankMode && focusState.enabled && focusState.count) selectScope("focus");
  toggleRow("lesson-row", !bankMode && !lockLesson);
  toggleRow("count-row", withCount);
  toggleRow("schedule-row", withCount);
  toggleRow("weak-row", withCount);
  applyScope(false);   // 选中重点词时收起课程行（不自动改题量）
  $("start-btn").textContent = CONFIG_START_LABEL[it.kind] || "开始练习";
  const hint = CONFIG_HINTS[it.id]
    || (modeUIById(it.id).audio ? AUDIO_HINT : "")
    || (lessonOnly ? it.desc : "");
  $("config-hint").textContent = hint;
  $("config-hint").classList.toggle("hidden", !hint);
  refreshModeFields();
  showScreen("config-screen");
}

function toggleRow(id, show) {
  $(id).classList.toggle("hidden", !show);
}

function selectMode(modeId) {
  const el = document.querySelector(`input[name="mode"][value="${modeId}"]`);
  if (el) el.checked = true;
}

function refreshModeFields() {
  updateExamFilterVisibility();
  updateCountField();
}

// 流程结束（交卷/退出/返回）后的落点：回到来时的模块页，没有模块记录就回首页
function backToModule() {
  if (nav.module) return showScreen("module-screen");
  // 回首页顺带刷数据：刚练完，「今日已练」与处方都该按新的词表重算
  showScreen("home-screen");
  loadHome({ skipBoot: true });
}

async function openDocs() {
  showScreen("docs-screen");
  $("docs-progress").textContent = "加载中…";
  $("docs-weaknesses").textContent = "加载中…";
  try {
    const res = await fetch("/api/docs");
    const data = await res.json();
    $("docs-progress").textContent = data.progress || "（空）";
    $("docs-weaknesses").textContent = data.weaknesses || "（空）";
  } catch (e) {
    $("docs-progress").textContent = "加载失败：" + e;
    $("docs-weaknesses").textContent = "";
  }
}

// ---------- 新增单词（前端直接录入生词，无需碰代码/重启） ----------
const addWord = {
  lessons: [],      // 最近一次 /api/config 的真实课程（已剔除错题本虚拟课）
  saving: false,    // 提交在途：防双击重复落盘
  bound: null,      // 正在修改的已有词 ref（null=新增模式）
  boundWord: null,  // 被绑定的候选词信息（banner 展示用）
  similar: [],      // 最近一次相近词匹配结果
  similarTimer: 0,  // 输入防抖计时器
};

const AW_FIELD_IDS = ["aw-kanji", "aw-hiragana", "aw-meaning", "aw-notes",
                      "aw-example-ja", "aw-example-zh", "aw-tip", "aw-new-lesson"];

// 「我的生词本」的课程 id：与后端 app.py 的 DEFAULT_USER_LESSON 同一个值。
// 后端按这个 id 在首次添加时自动建课，前端只用来把下拉默认值指过去。
const MY_WORDS_LESSON = "lesson_mywords";

function openAddWord() {
  const sel = $("aw-lesson");
  sel.innerHTML = "";
  const appendOpt = (value, text) => {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = text;
    sel.appendChild(o);
  };
  // 固定首项：我的生词本（后端在首次添加时自动创建）
  const mine = addWord.lessons.find((l) => l.id === MY_WORDS_LESSON);
  appendOpt(MY_WORDS_LESSON,
    mine ? `📒 ${mine.title}（${mine.word_count} 词）`
         : "📒 我的生词本（首次添加自动创建）");
  addWord.lessons.filter((l) => l.id !== MY_WORDS_LESSON).forEach((l) => {
    appendOpt(l.id, `${l.title}（${l.word_count} 词）`);
  });
  appendOpt("__new__", "➕ 新建课程…");

  // 默认选中配置页当前课程；「全部课程/错题本」回落生词本
  const cur = $("lesson").value;
  sel.value = (cur && cur !== "all" && cur !== "lesson_wrong") ? cur : MY_WORDS_LESSON;
  if (!sel.value) sel.value = MY_WORDS_LESSON;
  toggleAddWordNewLesson();
  AW_FIELD_IDS.forEach((id) => { $(id).value = ""; });
  unbindAddWord(true);
  hideAddWordMsg();
  $("add-word-modal").classList.remove("hidden");
  setTimeout(() => $("aw-hiragana").focus(), 0);
}

function closeAddWord() {
  $("add-word-modal").classList.add("hidden");
}

function toggleAddWordNewLesson() {
  const isNew = $("aw-lesson").value === "__new__";
  $("aw-new-lesson").classList.toggle("hidden", !isNew);
  if (isNew) setTimeout(() => $("aw-new-lesson").focus(), 0);
}

function showAddWordMsg(text, ok) {
  const el = $("aw-msg");
  el.textContent = text;
  el.className = "aw-msg " + (ok ? "aw-ok" : "aw-err");
}

function hideAddWordMsg() {
  const el = $("aw-msg");
  el.className = "aw-msg hidden";
  el.textContent = "";
}

// ---- 相近词匹配：边填边搜，命中后可选「改已有词」或照常新增 ----

function scheduleSimilarSearch() {
  clearTimeout(addWord.similarTimer);
  addWord.similarTimer = setTimeout(searchSimilarWords, 300);
}

async function searchSimilarWords() {
  // 弹窗已关、或正处于「修改已有词」模式时不搜（修改模式下字段本来就来自词库）
  if ($("add-word-modal").classList.contains("hidden") || addWord.bound) return;
  const payload = buildAddWordPayload();
  const kanji = payload.kanji.trim();
  const hiragana = payload.hiragana.trim();
  if (!kanji && !hiragana) {
    renderSimilar([]);
    return;
  }
  try {
    const res = await fetch("/api/words/similar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kanji, hiragana }),
    });
    const data = await res.json();
    // 响应回来时状态可能已变（关弹窗/绑定），不再渲染
    if ($("add-word-modal").classList.contains("hidden") || addWord.bound) return;
    addWord.similar = data.similar || [];
    renderSimilar(addWord.similar);
  } catch (e) {
    /* 匹配是辅助功能，失败静默不打断录入 */
  }
}

function renderSimilar(items) {
  const box = $("aw-similar");
  if (!items.length) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.innerHTML = `<div class="aw-sim-head">词库里可能有它，点「修改」直接改已有单词：</div>`
    + items.map((it) => `
      <div class="aw-sim-item">
        <button class="aw-sim-use" data-ref="${escapeHtml(it.ref)}" type="button">修改</button>
        <span class="aw-sim-word" lang="ja">${escapeHtml(it.display)}</span>
        <span class="aw-sim-meaning">${escapeHtml(it.meaning)}</span>
        <span class="aw-sim-meta">${escapeHtml(it.lesson_title)} · ${escapeHtml(it.relation)}</span>
      </div>`)
      .join("");
  box.classList.remove("hidden");
}

function bindWordToEdit(it) {
  addWord.bound = it.ref;
  addWord.boundWord = it;
  // 候选词的当前值填进表单，改完提交走 /api/words/edit
  $("aw-kanji").value = it.kanji && it.kanji !== "---" ? it.kanji : "";
  $("aw-hiragana").value = it.hiragana;
  $("aw-meaning").value = it.meaning;
  $("aw-notes").value = it.notes || "";
  $("aw-example-ja").value = it.example_ja || "";
  $("aw-example-zh").value = it.example_zh || "";
  $("aw-tip").value = it.tip || "";
  if (it.lesson_id) {
    $("aw-lesson").value = it.lesson_id;
    toggleAddWordNewLesson();
  }
  setAddWordBoundUI();
  hideAddWordMsg();
  $("aw-meaning").focus();
}

function setAddWordBoundUI() {
  const bound = !!addWord.bound;
  $("aw-bound").classList.toggle("hidden", !bound);
  $("aw-similar").classList.toggle("hidden", bound || !addWord.similar.length);
  $("aw-save-more").textContent = bound ? "更新并继续" : "保存并继续添加";
  $("aw-save-close").textContent = bound ? "更新并关闭" : "保存并关闭";
  $("aw-bound-text").textContent = bound
    ? `正在修改已有单词：${addWord.boundWord.display}（${addWord.boundWord.lesson_title}），id 与学习进度会保留`
    : "";
}

function unbindAddWord(keepFields) {
  addWord.bound = null;
  addWord.boundWord = null;
  addWord.similar = [];
  $("aw-similar").classList.add("hidden");
  $("aw-similar").innerHTML = "";
  setAddWordBoundUI();
  if (!keepFields) {
    AW_FIELD_IDS.forEach((id) => { $(id).value = ""; });
    $("aw-hiragana").focus();
  }
}

function buildAddWordPayload() {
  const isNew = $("aw-lesson").value === "__new__";
  return {
    lesson: isNew ? "" : $("aw-lesson").value,
    create_new: isNew,
    new_lesson_title: $("aw-new-lesson").value.trim(),
    kanji: $("aw-kanji").value,
    // 与答题输入一致：罗马字转平假名（finalize 收敛悬空 n），直接输假名原样保留
    hiragana: romajiToKana($("aw-hiragana").value, true),
    meaning: $("aw-meaning").value,
    notes: $("aw-notes").value,
    example_ja: $("aw-example-ja").value,
    example_zh: $("aw-example-zh").value,
    tip: $("aw-tip").value,
  };
}

async function submitAddWord(closeAfter) {
  if (addWord.saving) return;
  const payload = buildAddWordPayload();
  if (!payload.hiragana.trim()) {
    showAddWordMsg("请填写读音", false); $("aw-hiragana").focus(); return;
  }
  if (!payload.meaning.trim()) {
    showAddWordMsg("请填写中文释义", false); $("aw-meaning").focus(); return;
  }
  if (payload.create_new && !payload.new_lesson_title) {
    showAddWordMsg("请填写新课程名称", false); $("aw-new-lesson").focus(); return;
  }
  const editing = !!addWord.bound;
  if (editing) payload.ref = addWord.bound;
  addWord.saving = true;
  ["aw-save-more", "aw-save-close"].forEach((id) => { $(id).disabled = true; });
  let data;
  try {
    const res = await fetch(editing ? "/api/words/edit" : "/api/words/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    data = await res.json();
  } catch (e) {
    showAddWordMsg("提交失败，请确认后端已启动：" + e, false);
    addWord.saving = false;
    ["aw-save-more", "aw-save-close"].forEach((id) => { $(id).disabled = false; });
    return;
  }
  addWord.saving = false;
  ["aw-save-more", "aw-save-close"].forEach((id) => { $(id).disabled = false; });
  if (data.error) {
    showAddWordMsg(data.error, false);
    return;
  }
  // 刷新配置页课程下拉（词数/新课程），并选中刚写入的课程
  await loadConfig();
  $("lesson").value = data.lesson_id;
  if (closeAfter) {
    closeAddWord();
    return;
  }
  // 连续录入：重建模态课程下拉并保持选中，清空词条字段，聚焦读音
  const keepLesson = data.lesson_id;
  openAddWord();
  $("aw-lesson").value = keepLesson;
  toggleAddWordNewLesson();
  showAddWordMsg(editing
    ? "✓ 已更新该词（id 与学习进度保留），继续录入下一个"
    : `✓ 已加入「${data.lesson_title}」（共 ${data.word_count} 词），继续录入下一个`, true);
}


// ---------- 重命名课程（改单词表的显示名，如「第1课」→「标日初级①」） ----------
// 课程名只在 vocabulary.json 的 lessons.<id>.title 存一份，单词 ref / 音频 /
// 配图 / 练习记录都按 lesson_id 走，所以后端改名只动这一个字段，前端改完
// 重新拉 /api/config 刷新各处下拉即可。
const renameLesson = {
  list: [],      // 最近一次 /api/config 的真实课程（已剔除错题本虚拟课）
  saving: false, // 提交在途：防双击重复落盘
};

function openRenameLesson(presetId) {
  const sel = $("rn-lesson");
  sel.innerHTML = renameLesson.list.map((l) =>
    `<option value="${escapeHtml(l.id)}">${escapeHtml(l.title)}（${l.word_count} 词）</option>`).join("");
  if (!renameLesson.list.length) {
    $("rename-modal").classList.remove("hidden");
    showRenameMsg("词表里还没有课程，先去「新增单词」建一个吧", false);
    return;
  }
  // 配置页的「✏️ 改名」会带上当前课程；「全部课程 / 错题本」不是真实课程，不预设
  const cur = presetId || $("lesson").value;
  if (cur && renameLesson.list.some((l) => l.id === cur)) sel.value = cur;
  syncRenameTitle();
  hideRenameMsg();
  $("rename-modal").classList.remove("hidden");
  setTimeout(() => { $("rn-title").focus(); $("rn-title").select(); }, 0);
}

function closeRenameLesson() {
  $("rename-modal").classList.add("hidden");
}

// 换课程就把输入框填成它现在的名字：改名多半是「改几个字」，不必重打
function syncRenameTitle() {
  const cur = renameLesson.list.find((l) => l.id === $("rn-lesson").value);
  $("rn-title").value = cur ? cur.title : "";
}

function showRenameMsg(text, ok) {
  const el = $("rn-msg");
  el.textContent = text;
  el.className = "aw-msg " + (ok ? "aw-ok" : "aw-err");
}

function hideRenameMsg() {
  const el = $("rn-msg");
  el.className = "aw-msg hidden";
  el.textContent = "";
}

async function submitRename() {
  if (renameLesson.saving) return;
  const lessonId = $("rn-lesson").value;
  const title = $("rn-title").value.trim();
  if (!lessonId) { showRenameMsg("请选择要改名的课程", false); return; }
  if (!title) { showRenameMsg("请填写新的课程名称", false); $("rn-title").focus(); return; }
  renameLesson.saving = true;
  $("rn-save").disabled = true;
  try {
    const res = await fetch("/api/lesson/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lesson_id: lessonId, title }),
    });
    const data = await res.json();
    if (data.error) {
      showRenameMsg(data.error, false);
      return;
    }
    // 下拉（配置页课程范围、新增单词的「加入课程」）都按名字显示，必须重拉
    await loadConfig();
    syncRenameTitle();
    showRenameMsg(`✓ 已改名为「${data.title}」`, true);
    setTimeout(closeRenameLesson, 900);
  } catch (e) {
    showRenameMsg("保存失败，请确认后端已启动：" + e, false);
  } finally {
    renameLesson.saving = false;
    $("rn-save").disabled = false;
  }
}


// ---------- 重点词（聚焦练习） ----------
// 词库上千词时按「到期优先」练，很多词练一次要等十几天才再见——印象还没建立
// 就凉了。自己圈一小撮（10-20 个）反复练：清单存在后端 data/focus_words.json，
// 换浏览器、重启服务都还在；启用后各练习模式只出这些词。
const focusState = { count: 0, enabled: false };  // 配置页用的清单概况
const focus = {
  mounted: false,
  refs: [],       // 当前清单（有序）
  words: [],      // 清单词条详情（含掌握度）
  picks: [],      // 挑词面板当前候选
  searchTimer: 0,
  saving: false,  // 提交在途：防双击重复写清单
};

// 配置页的「练习范围」：按课程 / 只练重点词（后者与课程下拉互斥）
function selectedScope() {
  // 该子项不支持重点词时（错题本 / 学新词 / 汉字卡 / 独立题库）行是隐藏的，
  // 此时残留的单选值不能生效——否则错题本练习会被悄悄换成重点词
  const row = $("scope-row");
  if (row && row.classList.contains("hidden")) return "lesson";
  const el = document.querySelector('input[name="scope"]:checked');
  return el ? el.value : "lesson";
}

function selectScope(value) {
  const el = document.querySelector(`input[name="scope"][value="${value}"]`);
  if (el && !el.disabled) el.checked = true;
}

function refreshScopeRow() {
  const n = focusState.count || 0;
  const radio = document.querySelector('input[name="scope"][value="focus"]');
  if (!radio) return;
  radio.disabled = n === 0;
  $("scope-focus-desc").textContent = n
    ? `只出清单里的 ${n} 个词（题量超过清单时循环补齐，同一词反复出现）`
    : "清单为空：先到「🎯 重点词（聚焦练习）」里挑词";
  // 清单被清空后还勾着这一项会出不了题，悄悄落回「按课程范围」
  if (n === 0 && radio.checked) {
    const lessonRadio = document.querySelector('input[name="scope"][value="lesson"]');
    if (lessonRadio) lessonRadio.checked = true;
  }
}

// autoCount：切到重点词时顺手把题量改成清单长度（一轮过完）；
// 打开配置页时传 false，免得每次进页面都悄悄改掉用户填的题量
function applyScope(autoCount = true) {
  const it = nav.item || {};
  const bankMode = it.kind === "mode" && (it.id === "particle" || it.id === "exam");
  const lockLesson = it.kind === "wrongbook";
  const isFocus = selectedScope() === "focus";
  toggleRow("lesson-row", !bankMode && !lockLesson && !isFocus);
  // 练习池（到期复习 / 新词 / 全部）只对按词库出题的模式有意义：助词与真题是
  // 独立题库，课程模式自己分新词与复习，错题本里全是练过的词、重点词清单是
  // 用户点名的——这四种都绕过池子，于是这一行也一并收起
  toggleRow("pool-row", it.kind === "mode" && !bankMode
    && it.id !== "course" && !lockLesson && !isFocus);
  if (!isFocus) return;
  // 聚焦练习要的是重复：间隔重复会把刚答对的词推到几天后，默认改用随机
  const rnd = document.querySelector('input[name="schedule"][value="random"]');
  if (rnd) rnd.checked = true;
  const n = focusState.count || 0;
  // 配对模式下这个框是「轮数」，不能拿词数覆盖
  if (autoCount && n && selectedMode() !== "pair_match") {
    $("count").value = Math.min(100, Math.max(1, n));
  }
}

function fillFocusLessons(lessons) {
  const sel = $("focus-lesson");
  const keep = sel.value;
  sel.innerHTML = '<option value="all">全部课程</option>';
  (lessons || []).forEach((l) => {
    const o = document.createElement("option");
    o.value = l.id;
    o.textContent = `${l.title}（${l.word_count} 词）`;
    sel.appendChild(o);
  });
  if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
}

async function openFocus() {
  showScreen("focus-screen");
  if (!focus.mounted) mountFocusScreen();
  await Promise.all([loadFocusList(), loadFocusPicks()]);
}

function mountFocusScreen() {
  focus.mounted = true;
  $("focus-back-btn").addEventListener("click", backToModule);
  $("focus-enabled").addEventListener("change",
    () => setFocusEnabled($("focus-enabled").checked));
  $("focus-clear-btn").addEventListener("click", () => {
    if (!focus.refs.length) return;
    if (confirm(`清空重点词清单（${focus.refs.length} 个词）？掌握度与复习计划不受影响。`)) {
      postFocus({ action: "clear" });
    }
  });
  $("focus-lesson").addEventListener("change", loadFocusPicks);
  $("focus-search").addEventListener("input", () => {
    clearTimeout(focus.searchTimer);
    focus.searchTimer = setTimeout(loadFocusPicks, 250);
  });
  document.querySelectorAll('input[name="focus-filter"]').forEach((el) => {
    el.addEventListener("change", loadFocusPicks);
  });
  $("focus-quick-first").addEventListener("click", () => quickPickFocus("first"));
  $("focus-quick-random").addEventListener("click", () => quickPickFocus("random"));
  $("focus-quick-page").addEventListener("click",
    () => addFocusRefs(focus.picks.slice(0, FOCUS_PICK_LIMIT).map((w) => w.ref)));
  // 事件委托：列表每次重绘，不能逐行绑事件
  $("focus-list").addEventListener("click", (e) => {
    const btn = e.target.closest(".fc-del");
    if (btn && btn.dataset.ref) removeFocusRef(btn.dataset.ref);
  });
  $("focus-pick-list").addEventListener("click", (e) => {
    const row = e.target.closest("[data-ref]");
    if (row) toggleFocusRef(row.dataset.ref);
  });
}

function showFocusMsg(text) {
  const el = $("focus-msg");
  el.textContent = text || "";
  el.classList.toggle("hidden", !text);
}

async function loadFocusList() {
  try {
    const res = await fetch("/api/focus");
    const data = await res.json();
    applyFocusData(data);
    renderFocusList();
  } catch (e) {
    showFocusMsg("加载重点词失败：" + e);
  }
}

function applyFocusData(data) {
  focus.refs = data.refs || [];
  focus.words = data.words || [];
  focusState.count = data.count || 0;
  focusState.enabled = !!data.enabled;
}

function renderFocusList() {
  const box = $("focus-list");
  box.innerHTML = focus.words.length
    ? focus.words.map((w) => {
      const hasForm = !!(w.kanji && w.kanji !== "---");  // 纯假名词没写法，不重复显示一遍
      const form = hasForm ? w.kanji : w.hiragana;
      const cls = masteryClass(w.mastery) || "";
      return `
        <span class="focus-chip ${cls}">
          <span class="fc-form">${escapeHtml(form)}</span>
          ${hasForm ? `<span class="fc-hira">${escapeHtml(w.hiragana)}</span>` : ""}
          <span class="fc-mean">${escapeHtml(w.meaning)}</span>
          <button class="fc-del" type="button" data-ref="${escapeHtml(w.ref)}" title="移出清单">✕</button>
        </span>`;
    }).join("")
    : '<p class="hint">清单还是空的：在下面挑 10-20 个词加进来。</p>';
  $("focus-badge").textContent = focus.words.length ? `已选 ${focus.words.length} 词` : "";
  $("focus-enabled").checked = !!focusState.enabled;
  $("focus-enabled").disabled = !focus.words.length;
  $("focus-clear-btn").disabled = !focus.words.length;
}

// 挑词请求序号：改课程/筛选、连打搜索框会并发发多份请求，
// 只认最后一次的结果——否则先发的慢回来会把列表覆盖成上一个筛选条件的词。
let focusPickSeq = 0;

async function loadFocusPicks() {
  const lesson = $("focus-lesson").value || "all";
  const q = ($("focus-search").value || "").trim();
  const checked = document.querySelector('input[name="focus-filter"]:checked');
  const flt = checked ? checked.value : "all";
  const url = "/api/focus/pick?lesson=" + encodeURIComponent(lesson)
    + "&filter=" + encodeURIComponent(flt) + "&q=" + encodeURIComponent(q);
  const seq = ++focusPickSeq;
  try {
    const res = await fetch(url);
    const data = await res.json();
    if (seq !== focusPickSeq) return;
    focus.picks = data.words || [];
    renderFocusPicks();
  } catch (e) {
    showFocusMsg("加载候选词失败：" + e);
  }
}

const FOCUS_PICK_LIMIT = 300;  // 候选太多只渲染前 300 行（全部课程 1100+ 词）

function renderFocusPicks() {
  const box = $("focus-pick-list");
  const picked = new Set(focus.refs);
  const shown = focus.picks.slice(0, FOCUS_PICK_LIMIT);
  box.innerHTML = shown.length
    ? shown.map((w) => {
      const hasForm = !!(w.kanji && w.kanji !== "---");
      const form = hasForm ? w.kanji : w.hiragana;
      const on = picked.has(w.ref);
      return `
        <button type="button" class="pick-row ${on ? "picked" : ""}" data-ref="${escapeHtml(w.ref)}">
          <span class="pr-form">${escapeHtml(form)}</span>
          ${hasForm ? `<span class="pr-hira">${escapeHtml(w.hiragana)}</span>` : ""}
          <span class="pr-mean">${escapeHtml(w.meaning)}</span>
          <span class="pr-flag">${on ? "已选 ✓" : "＋"}</span>
        </button>`;
    }).join("")
    : '<p class="hint">没有符合条件的词。</p>';
  showFocusMsg(focus.picks.length > FOCUS_PICK_LIMIT
    ? `候选 ${focus.picks.length} 个词，只显示前 ${FOCUS_PICK_LIMIT} 个（换课程或用搜索缩小范围）`
    : "");
}

async function postFocus(payload) {
  if (focus.saving) return;
  focus.saving = true;
  try {
    const res = await fetch("/api/focus", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.error) {
      showFocusMsg(data.error);
      return;
    }
    applyFocusData(data);
    renderFocusList();
    renderFocusPicks();   // 勾选态跟着清单变，不重新拉候选
    refreshScopeRow();    // 配置页那一行也要同步（词数 / 置灰）
    showFocusMsg(data.missing ? `清单里有 ${data.missing} 个词已不在词库中` : "");
  } catch (e) {
    showFocusMsg("保存失败：" + e);
  } finally {
    focus.saving = false;
  }
}

function addFocusRefs(refs) {
  if (!refs || !refs.length) return showFocusMsg("没有可加入的词");
  return postFocus({ action: "add", refs });
}

function removeFocusRef(ref) {
  return postFocus({ action: "remove", refs: [ref] });
}

function toggleFocusRef(ref) {
  return focus.refs.includes(ref) ? removeFocusRef(ref) : addFocusRefs([ref]);
}

function setFocusEnabled(on) {
  return postFocus({ action: "enable", enabled: !!on });
}

function quickPickFocus(which) {
  const n = parseInt($("focus-quick-n").value, 10) || 20;
  const list = focus.picks.slice(0, FOCUS_PICK_LIMIT);
  if (which === "random") {
    for (let i = list.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [list[i], list[j]] = [list[j], list[i]];
    }
  }
  return addFocusRefs(list.slice(0, n).map((w) => w.ref));
}


// ---------- 初始化配置 ----------
async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    const data = await res.json();
    addWord.lessons = (data.lessons || []).filter((l) => !l.is_wrong_book);
    renameLesson.list = addWord.lessons;  // 改名弹窗的课程下拉（同样排除虚拟错题本）
    // 重点词概况（配置页「只练重点词」那一行）与挑词页的课程下拉
    focusState.count = (data.focus || {}).count || 0;
    focusState.enabled = !!(data.focus || {}).enabled;
    fillFocusLessons(addWord.lessons);
    refreshScopeRow();
    const lessonSel = $("lesson");
    // 下拉重建会丢掉当前选择（改完课程名重拉配置时不该悄悄跳回「全部课程」）
    const keepLesson = lessonSel.value;
    lessonSel.innerHTML = '<option value="all">全部课程</option>';
    data.lessons.forEach((l) => {
      const o = document.createElement("option");
      o.value = l.id;
      if (l.is_wrong_book) {
        // 错题本：词数为 0 时也显示，但标注空
        const tag = l.word_count > 0 ? `（${l.word_count} 词 · 待复习）` : "（空）";
        o.textContent = `📖 ${l.title}${tag}`;
      } else {
        const dueTag = l.due_count > 0 ? ` · ${l.due_count} 待复习` : "";
        o.textContent = `${l.title}（${l.word_count} 词${dueTag}）`;
      }
      lessonSel.appendChild(o);
    });
    if (keepLesson && [...lessonSel.options].some((o) => o.value === keepLesson)) {
      lessonSel.value = keepLesson;
    }
    // 真题范围过滤（真题实战模式用）
    const srcSel = $("exam-source");
    const secSel = $("exam-section");
    srcSel.innerHTML = (data.exam_sources || [{ id: "all", name: "全部真题卷" }])
      .map((v) => `<option value="${escapeHtml(v.id)}">${escapeHtml(v.name)}</option>`).join("");
    secSel.innerHTML = (data.exam_sections || [{ id: "all", name: "全部科目" }])
      .map((v) => `<option value="${escapeHtml(v.id)}">${escapeHtml(v.name)}</option>`).join("");

    const modeDiv = $("modes");
    modeDiv.innerHTML = "";
    configModes = data.modes || [];
    data.modes.forEach((m, i) => {
      const label = document.createElement("label");
      label.className = "mode-option";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "mode";
      radio.value = m.id;
      if (i === 0) radio.checked = true;
      label.appendChild(radio);
      const span = document.createElement("span");
      span.innerHTML = `<strong>${escapeHtml(m.name)}</strong><small>${escapeHtml(m.desc)}</small>`;
      label.appendChild(span);
      modeDiv.appendChild(label);
    });
    // 切换模式时：真题显示范围过滤；题目数量框按模式切换语义。
    // 用 onchange 赋值而不是 addEventListener：#modes 是常驻节点（上面只是
    // innerHTML="" 清掉子节点，容器上的监听器不会跟着没），而每次存词/改课程名
    // 都会重跑 loadConfig —— addEventListener 会把回调叠到一次切换跑几十遍。
    modeDiv.onchange = () => {
      updateExamFilterVisibility();
      updateCountField();
    };
    updateExamFilterVisibility();
    updateCountField();
    // 新增单词后会重跑 loadConfig（刷新课程下拉）：单选列表被重建，
    // 这里把当前选中的模式重新勾上，否则会悄悄退回第一个模式
    if (nav.item && nav.item.kind === "mode") selectMode(nav.item.id);
  } catch (e) {
    alert("加载配置失败，请确认后端已启动：" + e);
  }
}

function selectedMode() {
  const el = document.querySelector('input[name="mode"]:checked');
  return el ? el.value : "kanji_to_kana";
}

function updateExamFilterVisibility() {
  $("exam-filter-row").style.display = selectedMode() === "exam" ? "" : "none";
}

// 题目数量框的语义随模式变：词义配对按「轮数」出题（每轮 5 对，后端上限 8 轮），
// 沿用「题目数量」(1-100) 会出现「填 20 只出 8 题」的错位，干脆把框本身改成轮数
const COUNT_FIELD = {
  _default: { label: "题目数量", min: 1, max: 100 },
  pair_match: { label: "轮数（每轮 5 对）", min: 2, max: 8 },
};
// 非配对模式的题目数量：切到配对时先存下，切回来恢复，避免来回切换后被 clamp 掉
let genericCount = null;
// 上一次应用的模式：只在「切进配对」那一刻记录原值，重复调用不会把 clamp 后的值记回去
let countFieldMode = null;

function updateCountField() {
  const m = selectedMode();
  const cfg = COUNT_FIELD[m] || COUNT_FIELD._default;
  const input = $("count");
  $("count-label").textContent = cfg.label;
  if (m !== "pair_match" && genericCount !== null) {
    input.value = genericCount;  // 从配对切回来：恢复原本的题目数量
    genericCount = null;
  }
  const n = parseInt(input.value, 10);
  if (m === "pair_match" && countFieldMode !== "pair_match") {
    genericCount = n || null;
  }
  countFieldMode = m;
  input.min = cfg.min;
  input.max = cfg.max;
  if (!n || n < cfg.min) input.value = cfg.min;
  else if (n > cfg.max) input.value = cfg.max;
}

// ---------- 开始会话 ----------
// overrides：学习结束页「立即练习这批词」用——覆盖 lesson/mode/count/schedule 并携带 refs
async function startSession(overrides = {}) {
  if (state.finishing) return;  // 交卷在途，等结果页出来再开新组
  if (state.starting) return;   // 防止双击/Enter 连按发出两个 /api/start
  state.starting = true;
  try {
    const lesson = overrides.lesson !== undefined ? overrides.lesson : $("lesson").value;
    const modeEl = document.querySelector('input[name="mode"]:checked');
    const mode = overrides.mode || (modeEl ? modeEl.value : "kanji_to_kana");
    const count = overrides.count || (parseInt($("count").value, 10) || 20);
    if (count < 1) throw new Error("题目数量至少为 1");
    const focus_weak = overrides.focus_weak !== undefined
      ? overrides.focus_weak : $("focus_weak").checked;
    const scheduleEl = document.querySelector('input[name="schedule"]:checked');
    const schedule = overrides.schedule || (scheduleEl ? scheduleEl.value : "due");
    // 练习池：到期复习 / 新词 / 全部。学习完成页「立即练习这批词」是点名词条，
    // 一律用「全部」——否则刚学的词会被「到期复习」筛掉，练了个空；
    // 今日处方的每一步也点名要哪个池（「复习到期词」必须走 review，
    // 否则配置页停在「新词」时那一步就出成新词了）
    const poolEl = document.querySelector('input[name="pool"]:checked');
    const pool = overrides.pool
      || (overrides.refs ? "all" : (poolEl ? poolEl.value : "all"));

    // 真题实战：附带卷/科目过滤；练习记录按过滤条件分组，便于同条件对比
    const exam_source = mode === "exam" ? $("exam-source").value : undefined;
    const exam_section = mode === "exam" ? $("exam-section").value : undefined;
    let lessonId = lesson;
    if (mode === "exam" && exam_source !== undefined &&
        !(exam_source === "all" && exam_section === "all")) {
      lessonId = `exam·${exam_source === "all" ? "全部卷" : exam_source}·${exam_section === "all" ? "全科" : exam_section}`;
    }

    const res = await fetch("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lesson: lessonId, mode, count, focus_weak, schedule, pool, exam_source, exam_section,
        // 重点词：显式带上，后端据此决定是否只出清单里的词（不传时按清单开关走）；
        // 外部入口（如图片日语「练本课单词」）点名词条时可以显式关掉——
        // 否则「已在重点词清单里」会把点名要练的词整批筛空、报错回来
        focus: overrides.focus !== undefined
          ? overrides.focus : selectedScope() === "focus",
        ...(overrides.refs ? { refs: overrides.refs } : {}),
      }),
    });
    const data = await res.json();
    if (data.error) {
      alert(data.error);
      return;
    }
    state.config = { lesson: lessonId, mode, count, focus_weak, exam_source, exam_section };
    state.modeName = data.mode_name;
    state.rawInput = !!data.raw_input;   // 本组默认值；每题以 ui.raw_input 为准
    // 本组题型的交互描述（题干呈现 + 作答方式），课程模式另有每题一份
    state.modeUI = data.ui || modeUIById(mode) || null;
    state.noAudio = false;  // 每轮重新开始，听力题自动播放复位
    state.questions = data.questions;
    state.index = 0;
    state.correctCount = 0;
    state.results = [];
    // 本次会话的唯一标识：交卷时带给后端做幂等去重
    state.sessionId = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    // 重置学习指标
    state.streak = 0;
    state.maxStreak = 0;
    state.reactionTimes = [];
    state.questionStartTime = 0;
    state.checking = false;
    state.finishing = false;
    showScreen("practice-screen");
    paintMuteBtn();   // 头部「跳过听力题」开关的状态跟着本机偏好走
    if (mode === "course") {
      // 课程模式：新词预习 → 混合题型 → 错题回炉，走独立的渲染/判分流程
      course.queue = data.questions;
      course.retryList = [];
      course.pos = 0;
      course.round = 0;
      course.reviews = 0;
      course.q = null;
      // 开着「跳过听力题」：本轮听力题整批剔掉（与逐题「不方便听」同一语义）
      if (skipAudioPref()) {
        state.noAudio = true;
        dropListeningQuestions();
        if (!course.queue.length) {
          alert("这一组全是听力题，已被「跳过听力题」跳过。关掉右上角那个开关再练，或换个模式。");
          return endSessionOrBackHome();
        }
      }
      course.intros = course.queue.filter((q) => q.intro);
      course.introIdx = 0;
      state.inProgress = true;
      if (course.intros.length > 0) renderCourseIntro();
      else renderCourseQuestion();
      return;  // finally 复位 starting
    }
    // 独立模式同理：整批剔掉听力题，剔空了就说清楚再退回去（不然是默默回到上一页）
    if (skipAudioPref()) {
      state.noAudio = true;
      dropListeningQuestions();
      if (!state.questions.length) {
        alert("这一组全是听力题，已被「跳过听力题」跳过。关掉右上角那个开关再练，或换个模式。");
        return endSessionOrBackHome();
      }
    }
    renderQuestion();
    state.inProgress = true;  // 进入练习：作答后中途关页会先提醒
  } catch (e) {
    alert("开始练习失败，请确认后端已启动：" + e);
  } finally {
    state.starting = false;
  }
}

// ---------- 播放按钮可见性 ----------
// 「播放发音」会读出正确读音=答案的题型，作答前必须隐藏（防一键作弊）；
// 答题后反馈区已揭示答案，恢复按钮供听音巩固。
// 哪些题型算剧透由后端按「题干 × 作答」矩阵判定（ui.spoiler），前端不再列白名单。

// 课程模式运行时状态：主队列 → 错题回炉循环（直到全部答对）→ 交卷。
// 掌握度只由首次作答驱动（results 仅含首次作答），回炉重做只计数不计分。
const course = {
  queue: [],      // 当前轮次的题目
  retryList: [],  // 本轮答错、待回炉的题目
  pos: 0,
  round: 0,       // 0=主轮次，>0=回炉轮次
  reviews: 0,     // 回炉重做次数（仅留档 review_count）
  q: null,        // 当前题（含内层题型 mode 字段）
  intros: [],     // 新词预习卡（带 intro 载荷的题）
  introIdx: 0,
  phase: "main",  // intro | main | retry
  dragChip: null, // 组句拖拽中的词块
};

function canPlayQuestionAudio(q, answered) {
  const m = state.config ? state.config.mode : "";
  if (m === "particle") return false;                      // 助词题无单词音频
  // 配对题没有「当前词」的概念（本轮 5 个词同时在屏），播放按钮只能固定读第一个词，
  // 点了还会让人以为读的是刚选中的那个；点词瓦片时已自动发音，这里整体关掉
  if (m === "pair_match") return false;
  if (m === "exam") return !!q.audio;                      // 只有聴解真题带官方音频
  // 课程模式按内层题型判断（读音对打字题=剧透，同独立模式规则）
  if (uiOf(q).spoiler && !answered) return false;          // 播放即剧透读音
  return true;
}

function updateAudioBox(q, answered) {
  const playBtn = $("play-audio-btn");
  $("q-audio-box").classList.toggle("hidden", !canPlayQuestionAudio(q, answered));
  playBtn.textContent = (state.config && state.config.mode === "exam" && q.audio)
    ? "🔊 播放音频" : "🔊 播放发音";
  // 重置语音只对词库 TTS 有意义：聴解真题是官方音频切片，助词/配对题无单词音频；
  // 例句听写读的是整句（q.audio），重置的是词条发音，对不上，一并隐藏
  const m = state.config ? state.config.mode : "";
  $("regen-audio-btn").classList.toggle(
    "hidden", m === "exam" || m === "particle" || !!q.audio);
}

// 题干区渲染：按 ui 的题干呈现方式（图片 / 发音 / 文字）统一处理，
// 独立模式与课程内题型共用这一段（真题插图随选项走，不经过这里）
function renderPrompt(promptEl, q, m) {
  const ui = uiOf(q);
  if (ui.image && q.image && m !== "exam") {
    // 配图题干：图本身即题干，补一行提示说明怎么作答
    // （只看到一张图，不知道要选什么 / 要听哪一段）
    const tip = ui.audio_options ? '<p class="hint">🔊 听四个发音，选出和图对应的词</p>'
      : (ui.audio ? '<p class="hint">🔊 听发音，选读音</p>' : "");
    promptEl.innerHTML = `<img class="q-image" src="${escapeHtml(q.image)}" alt="单词配图">`
      + tip;
    promptEl.classList.remove("audio-mode");
    return "text";
  }
  if (ui.audio) {
    // 听音类：显示占位提示（不剧透答案），自动播放
    promptEl.textContent = ui.order
      ? "🔊 听整句，用下面的词块拼出这句日语"
      : (AUDIO_PROMPT[ui.task] || "🔊 听音题");
    promptEl.classList.add("audio-mode");
    // 用户点过「跳过本题」后本组不再自动播，避免在安静场合突然出声
    if (!state.noAudio) playQuestionAudio(q.ref);
    return "audio";
  }
  promptEl.textContent = q.prompt;
  promptEl.classList.remove("audio-mode");
  // 题干是文字而选项是发音：光看一个词不知道要干什么，补一行说明
  if (ui.audio_options) {
    promptEl.innerHTML = `${escapeHtml(q.prompt)}`
      + '<p class="hint">🔊 听四个发音，选出这个词的读音</p>';
  }
  return "text";
}

// ---------- 渲染当前题目 ----------
function renderQuestion() {
  const q = state.questions[state.index];
  $("q-mode").textContent = state.modeName;
  $("q-progress").textContent = `第 ${state.index + 1} / ${state.questions.length} 题`;
  $("q-score").textContent = `正确 ${state.correctCount}`;
  $("q-lesson").textContent = q.lesson_title;

  const m = state.config ? state.config.mode : "";
  const promptEl = $("q-prompt");
  renderPrompt(promptEl, q, m);
  // 长题干（真题文章/例句填空）用小号左对齐样式并保留换行；组句与课程内一致
  promptEl.classList.toggle(
    "exam-stem",
    m === "exam" || m === "cloze_cn" || uiOf(q).order === true
  );
  // 播放按钮：剧透模式作答前隐藏；聴解真题保留（播官方音频切片）
  updateAudioBox(q, false);

  const input = $("answer-input");
  input.value = "";
  input.disabled = false;
  setAnswerPlaceholder(q);
  $("submit-btn").disabled = false;
  $("skip-btn").disabled = false;
  $("skip-btn").style.display = "";  // 课程模式会隐藏它，普通模式恢复
  $("next-btn").style.display = "none";
  const fb = $("feedback");
  fb.className = "feedback hidden";
  fb.innerHTML = "";
  // 闪卡模式：隐藏打字区，显示翻面控件；选择题/组句：显示选项区；配对：显示瓦片；打字模式反之
  const isFlash = m === "flashcard";
  // 是否四选一由后端下的 ui 决定（真题是官方四选一，不在这套矩阵里）
  const isChoice = m === "exam" || uiOf(q).choice === true;
  // 词块拼句由后端 ui 标记（组句排序与例句听写共用同一套 UI，不按模式 id 分叉）
  const isOrder = uiOf(q).order === true;
  const isPair = m === "pair_match";
  state.cardFlipped = false;
  state.examAnswered = false;
  state.checking = false;  // 新题开始：清掉上一题的在途闸（若有）
  resetRegenUI();          // 新题开始：重置语音面板与按钮状态
  pairState.selWord = null;
  pairState.selMeaning = null;
  pairState.mistakes = {};
  pairState.matched = 0;
  refreshNoAudioRow();
  $("input-phase").classList.toggle("hidden", isFlash || isChoice || isOrder || isPair);
  $("flashcard-controls").classList.toggle("hidden", !isFlash);
  $("exam-controls").classList.toggle("hidden", !(isChoice || isOrder || isPair));
  $("flip-btn").classList.remove("hidden");
  $("assess-row").classList.add("hidden");
  if (isChoice) {
    buildExamOptions(q);
    // 聴解真题进题自动播官方切片（听音类题干已在 renderPrompt 里统一播过）
    if (!state.noAudio && m === "exam" && q.audio) {
      playQuestionAudio(q.ref);
    }
  } else if (isOrder) {
    // 组句排序：词块点选/拖拽 UI（与课程内共用，检查时按模式分发判分）
    buildOrderUI(q);
  } else if (isPair) {
    // 词义配对：词↔释义瓦片，点选配对，客户端判定后按词计结果
    buildPairUI(q);
  } else if (isFlash) {
    // 自动播放发音，建立「字形→读音→意思」关联；发音不剧透（考的是意思）
    playQuestionAudio(q.ref);
  } else {
    input.focus();
  }
  // 记录本题开始作答时间（用于反应时间统计）
  state.questionStartTime = Date.now();
  // 预取下一题音频：听音/闪卡/聴解真题切题时要自动播，提前拉取降低等待
  prefetchNextAudio();
}

function prefetchNextAudio() {
  // 课程模式预取 course.queue 下一题；普通模式预取 questions 下一题
  const isCourse = state.config && state.config.mode === "course";
  const nq = isCourse ? course.queue[course.pos + 1] : state.questions[state.index + 1];
  if (!nq) return;
  // 预取下一题配图，切题时图片已在缓存里，不闪白
  if (nq.image) {
    const im = new Image();
    im.src = nq.image;
  }
  // 课程模式按下一题的内层题型判断；普通模式按当前模式判断
  const m = isCourse ? (nq.mode || "") : (state.config ? state.config.mode : "");
  const willAutoPlay = uiOf(nq).audio === true || m === "flashcard"
    || (m === "exam" && !!nq.audio);
  if (!willAutoPlay) return;
  fetchTTSBlob(nq.audio || `/api/tts?ref=${encodeURIComponent(nq.ref)}`).catch(() => {});
}

// ---------- 真题实战：选择题 ----------
function buildExamOptions(q) {
  const ec = $("exam-controls");
  let html = q.qtype ? `<div class="exam-qtype">${escapeHtml(q.qtype)}</div>` : "";
  // 真题插图随选项渲染；单词配图模式的图已在题干区（q-image），这里不重复
  if (q.image && state.config && state.config.mode === "exam") {
    html += `<img class="exam-img" src="${escapeHtml(q.image)}" alt="题目插图">`;
  }
  if (q.option_audio) {
    // 选项是发音：瓦片上只有喇叭（选项文本是读音=答案，不能显示出来），
    // 点哪个就听哪个并作答（多邻国式）；作答后再点只重听，不重复判分。
    html += q.options
      .map((_, i) =>
        `<button class="exam-opt audio-opt" data-idx="${i}" aria-label="播放发音 ${i + 1}">`
        + `<span class="opt-num">${i + 1}</span><span class="opt-text opt-speaker">🔊</span></button>`)
      .join("");
  } else {
    html += q.options
      .map((o, i) =>
        `<button class="exam-opt" data-idx="${i}"><span class="opt-num">${i + 1}</span><span class="opt-text">${escapeHtml(o)}</span></button>`)
      .join("");
  }
  ec.innerHTML = html;
  state.choiceCursor = 0;
  paintChoiceCursor(0);
  // 选项音频提前拉取：点下去立刻出声，不等网络（blob 缓存与播放共用一份）
  (q.option_audio || []).forEach((u) => {
    if (u) fetchTTSBlob(u).catch(() => {});
  });
}

// 选项是发音时：点选/回车前先把这段发音播出来（点击即作答，与多邻国一致）
function playOptionAudio(idx) {
  const q = currentQ();
  const url = q && q.option_audio ? q.option_audio[idx] : "";
  if (url) playTTS(url);
}

// 选择题高亮游标：只画未作答的选项（作答后由正误色接管，游标清除）
function paintChoiceCursor(cursor) {
  document.querySelectorAll("#exam-controls .exam-opt").forEach((b, i) => {
    b.classList.toggle("opt-cursor", i === cursor && !b.classList.contains("answered"));
  });
}

async function submitExam(idx) {
  if (state.examAnswered) return;
  const q = state.questions[state.index];
  const answer = q.options[idx];
  state.examAnswered = true;
  const reaction = state.questionStartTime > 0 ? Date.now() - state.questionStartTime : 0;
  let data;
  try {
    const res = await fetch("/api/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: q.ref, mode: state.config.mode, answer, form: q.conj_form || "" }),
    });
    data = await res.json();
  } catch (e) {
    alert("校验失败：" + e);
    state.examAnswered = false;
    return;
  }
  if (data.error) {
    alert(data.error);
    state.examAnswered = false;
    return;
  }
  state.results.push({
    ref: q.ref,
    correct: data.correct,
    skipped: false,
    prompt: q.prompt,
    your_answer: answer,
    expected: data.expected,
    word: data.word,
  });
  if (data.correct) {
    state.correctCount++;
    state.streak++;
    if (state.streak > state.maxStreak) state.maxStreak = state.streak;
  } else {
    state.streak = 0;
  }
  if (reaction > 0) state.reactionTimes.push(reaction);
  $("q-score").textContent = `正确 ${state.correctCount} · 连续 ${state.streak}`;
  // 高亮选项：正确项绿色，误选项红色
  const correctIdx = q.options.indexOf(data.expected);
  document.querySelectorAll("#exam-controls .exam-opt").forEach((b, i) => {
    b.classList.add("answered");
    b.classList.remove("opt-cursor");
    if (i === correctIdx) b.classList.add("opt-correct");
    else if (i === idx && !data.correct) b.classList.add("opt-wrong");
  });
  showFeedback(data, answer, false, q.ref);
}

// ---------- 闪卡：翻面 / 自评 ----------
function flipCard() {
  if (state.cardFlipped) return;
  const q = state.questions[state.index];
  const w = q.word || {};
  state.cardFlipped = true;
  $("flip-btn").classList.add("hidden");
  $("assess-row").classList.remove("hidden");
  const fb = $("feedback");
  const kanjiDisplay = w.kanji && w.kanji !== "---" ? w.kanji : w.hiragana;
  fb.className = "feedback reveal";
  fb.innerHTML = `
    <div class="fb-word ${masteryClass(w.mastery) || ""}">
      ${masteryTag(w.mastery)}
      ${w.image ? `<img class="fb-img" src="${escapeHtml(w.image)}" alt="配图">` : ""}
      <div class="fb-kanji">${escapeHtml(kanjiDisplay)}</div>
      <div class="fb-hira">${escapeHtml(w.hiragana)}<span class="fb-roma">${escapeHtml(w.romaji)}</span></div>
      <div class="fb-meaning">${escapeHtml(w.meaning)}</div>
      ${w.notes ? `<div class="fb-notes">${escapeHtml(w.notes)}</div>` : ""}
      ${w.tip ? `<div class="fb-tip"><span class="fb-tip-label">💡 用法</span>${escapeHtml(w.tip)}</div>` : ""}
    </div>
    ${exampleHtml(w, q.ref)}
  `;
}

function judgeCard(know) {
  if (!state.cardFlipped) return;
  const q = state.questions[state.index];
  const w = q.word || {};
  const reaction = state.questionStartTime > 0 ? Date.now() - state.questionStartTime : 0;
  state.results.push({
    ref: q.ref,
    correct: !!know,
    skipped: false,
    prompt: q.prompt,
    your_answer: "",
    expected: w.hiragana || "",
    word: w,
  });
  if (know) {
    state.correctCount++;
    state.streak++;
    if (state.streak > state.maxStreak) state.maxStreak = state.streak;
  } else {
    state.streak = 0;
  }
  if (reaction > 0) state.reactionTimes.push(reaction);
  nextQuestion();
}

// ---------- 学习模块（先学后练，仿多邻国：卡片教学 → 再认小测 → 衔接练习） ----------
const learn = {
  lesson: "",
  words: [],      // [{ref, lesson_id, lesson_title, word, distractors}]
  index: 0,       // 卡片阶段：当前张
  quizCursor: 0,  // 小测高亮游标：↑/↓ 移动、Enter 确认
  queue: [],      // 小测队列：答错的词重排队尾，循环到全部答对
  quizTotal: 0,
  quizLocked: false,
};

function learnWordDisplay(w) {
  return w.kanji && w.kanji !== "---" ? w.kanji : w.hiragana;
}

async function startLearn(overrides = {}) {
  // overrides：今日处方点了「学一批新词」时按处方带的课程与数量开，不走配置页
  const lesson = overrides.lesson !== undefined ? overrides.lesson : $("lesson").value;
  const count = overrides.count || LEARN_BATCH;
  try {
    const res = await fetch("/api/learn/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lesson, count }),
    });
    const data = await res.json();
    if (data.error) {
      alert(data.error);
      return;
    }
    learn.lesson = data.lesson;
    learn.words = data.words;
    learn.index = 0;
    showScreen("learn-screen");
    $("learn-cards").classList.remove("hidden");
    $("learn-quiz").classList.add("hidden");
    $("learn-done").classList.add("hidden");
    renderLearnCard();
  } catch (e) {
    alert("加载学习内容失败：" + e);
  }
}

function renderLearnCard() {
  const item = learn.words[learn.index];
  const w = item.word;
  $("learn-lesson").textContent = item.lesson_title;
  $("learn-progress").textContent = `第 ${learn.index + 1} / ${learn.words.length} 张`;
  $("learn-card").innerHTML = `
    <div class="fb-word ${masteryClass(w.mastery) || ""}">
      ${masteryTag(w.mastery)}
      ${w.image ? `<img class="fb-img" src="${escapeHtml(w.image)}" alt="配图">` : ""}
      <div class="fb-kanji">${escapeHtml(learnWordDisplay(w))}</div>
      <div class="fb-hira">${escapeHtml(w.hiragana)}<span class="fb-roma">${escapeHtml(w.romaji)}</span></div>
      <div class="fb-meaning">${escapeHtml(w.meaning)}</div>
      ${w.notes ? `<div class="fb-notes">${escapeHtml(w.notes)}</div>` : ""}
      ${w.tip ? `<div class="fb-tip"><span class="fb-tip-label">💡 用法</span>${escapeHtml(w.tip)}</div>` : ""}
    </div>
    ${exampleHtml(w, item.ref)}
  `;
  $("learn-prev").disabled = learn.index === 0;
  $("learn-next").textContent =
    learn.index === learn.words.length - 1 ? "进入小测 →" : "认识，下一个 (Enter)";
  resetRegenUI();  // 换卡片：清掉上一张的重置语音试听面板
  playTTS(`/api/tts?ref=${encodeURIComponent(item.ref)}`);
}

function learnNext() {
  if (learn.index >= learn.words.length - 1) {
    startLearnQuiz();
  } else {
    learn.index += 1;
    renderLearnCard();
  }
}

function learnPrev() {
  if (learn.index > 0) {
    learn.index -= 1;
    renderLearnCard();
  }
}

function startLearnQuiz() {
  learn.queue = [...learn.words].sort(() => Math.random() - 0.5);
  learn.quizTotal = learn.queue.length;
  $("learn-cards").classList.add("hidden");
  $("learn-quiz").classList.remove("hidden");
  renderLearnQuiz();
}

function renderLearnQuiz() {
  const w = learn.queue[0].word;
  $("learn-quiz-progress").textContent =
    `再认小测 ${learn.quizTotal - learn.queue.length + 1} / ${learn.quizTotal}`;
  $("learn-quiz-prompt").innerHTML =
    `「<strong>${escapeHtml(learnWordDisplay(w))}</strong>」的意思是？`;
  learn.quizLocked = false;
  // 正确释义 + 3 个干扰项：后端已按义项剔掉与本词同义的释义，不会出现两个正确项
  const opts = [{ text: w.meaning, ok: true }];
  const pool = [...(learn.queue[0].distractors || [])];
  while (opts.length < 4 && pool.length > 0) {
    opts.push({ text: pool.splice(Math.floor(Math.random() * pool.length), 1)[0], ok: false });
  }
  opts.sort(() => Math.random() - 0.5);
  $("learn-quiz-opts").innerHTML = opts
    .map((o, i) =>
      `<button class="exam-opt" data-ok="${o.ok ? 1 : 0}"><span class="opt-num">${i + 1}</span><span class="opt-text">${escapeHtml(o.text)}</span></button>`)
    .join("");
  learn.quizCursor = 0;
  paintLearnCursor(0);
}

function paintLearnCursor(cursor) {
  document.querySelectorAll("#learn-quiz-opts .exam-opt").forEach((b, i) => {
    b.classList.toggle("opt-cursor", i === cursor && !b.classList.contains("answered"));
  });
}

function answerLearnQuiz(btn) {
  if (learn.quizLocked) return;
  learn.quizLocked = true;
  const ok = btn.dataset.ok === "1";
  document.querySelectorAll("#learn-quiz-opts .exam-opt").forEach((b) => {
    b.classList.add("answered");
    b.classList.remove("opt-cursor");
    if (b.dataset.ok === "1") b.classList.add("opt-correct");
    else if (b === btn && !ok) b.classList.add("opt-wrong");
  });
  if (!ok) learn.queue.push(learn.queue[0]);  // 答错重排队尾，循环到答对为止
  setTimeout(() => {
    learn.queue.shift();
    if (learn.queue.length === 0) {
      finishLearn();
    } else {
      renderLearnQuiz();
    }
  }, ok ? 500 : 1200);
}

async function finishLearn() {
  const refs = learn.words.map((x) => x.ref);
  try {
    const res = await fetch("/api/learn/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lesson: learn.lesson, refs }),
    });
    const data = await res.json();
    if (data.error) {
      alert(data.error);
      return;
    }
    $("learn-quiz").classList.add("hidden");
    $("learn-done").classList.remove("hidden");
    $("learn-done-msg").textContent =
      `本批 ${data.learned} 个新词已标记为「见过」并安排今天到期。趁热打铁练一组，记忆效果最好。`;
  } catch (e) {
    alert("保存学习记录失败：" + e);
  }
}

function practiceLearned() {
  startSession({
    lesson: learn.lesson,
    mode: "kanji_to_kana",  // 刚学完从「看汉字写假名」起步（再认 → 产出）
    count: learn.words.length,
    schedule: "due",     // 学习时已设今天到期，due 调度天然优先出这批
    refs: learn.words.map((x) => x.ref),
  });
}

// ---------- 课程模式（多邻国式混合课） ----------
// 主队列答完后进入错题回炉：答错的题重新作答直到全部答对；
// 掌握度只由首次作答驱动（results 仅含首次作答），回炉重做只计数不计分。

function renderCourseIntro() {
  course.phase = "intro";
  const item = course.intros[course.introIdx];
  const w = item.intro;
  $("q-mode").textContent = "课程 · 新词预习";
  $("q-progress").textContent = `新词 ${course.introIdx + 1} / ${course.intros.length}`;
  $("q-score").textContent = `正确 ${state.correctCount}`;
  $("q-lesson").textContent = item.lesson_title;
  const promptEl = $("q-prompt");
  promptEl.textContent = "";
  promptEl.classList.remove("audio-mode", "exam-stem");
  $("exam-controls").classList.remove("hidden");
  $("exam-controls").innerHTML = `
    <div class="fb-word ${masteryClass(w.mastery) || ""}">
      ${masteryTag(w.mastery)}
      ${w.image ? `<img class="fb-img" src="${escapeHtml(w.image)}" alt="配图">` : ""}
      <div class="fb-kanji">${escapeHtml(learnWordDisplay(w))}</div>
      <div class="fb-hira">${escapeHtml(w.hiragana)}<span class="fb-roma">${escapeHtml(w.romaji)}</span></div>
      <div class="fb-meaning">${escapeHtml(w.meaning)}</div>
      ${w.notes ? `<div class="fb-notes">${escapeHtml(w.notes)}</div>` : ""}
      ${w.tip ? `<div class="fb-tip"><span class="fb-tip-label">💡 用法</span>${escapeHtml(w.tip)}</div>` : ""}
    </div>
    ${exampleHtml(w, item.ref)}`;
  $("input-phase").classList.add("hidden");
  $("flashcard-controls").classList.add("hidden");
  $("skip-btn").style.display = "none";
  $("no-audio-row").classList.add("hidden");  // 预习卡不是听力题，不显示跳过
  const fb = $("feedback");
  fb.className = "feedback hidden";
  fb.innerHTML = "";
  const nb = $("next-btn");
  nb.style.display = "";
  nb.textContent = course.introIdx === course.intros.length - 1
    ? "开始答题 (Enter)" : "认识了 (Enter)";
  playTTS(`/api/tts?ref=${encodeURIComponent(item.ref)}`);
}

function courseIntroNext() {
  course.introIdx += 1;
  if (course.introIdx < course.intros.length) {
    renderCourseIntro();
  } else {
    course.phase = "main";
    renderCourseQuestion();
  }
}

function renderCourseQuestion() {
  const q = course.q = course.queue[course.pos];
  course.phase = course.round > 0 ? "retry" : "main";
  state.examAnswered = false;
  state.checking = false;
  resetRegenUI();
  refreshNoAudioRow();
  // 题干与作答方式由后端按内层题型下发（与独立模式同一套 ui 描述）
  const ui = uiOf(q);
  const typing = ui.typing === true;
  const choice = ui.choice === true;
  const order = ui.order === true;
  $("q-mode").textContent = (course.round > 0 ? "错题回炉 · " : "课程 · ")
    + (q.mode_name || "");
  $("q-progress").textContent = course.round > 0
    ? `回炉 ${course.pos + 1} / ${course.queue.length}`
    : `第 ${course.pos + 1} / ${course.queue.length} 题`;
  $("q-score").textContent = `正确 ${state.correctCount}`;
  $("q-lesson").textContent = q.lesson_title;
  const promptEl = $("q-prompt");
  renderPrompt(promptEl, q, "course");
  promptEl.classList.toggle("exam-stem", order);
  updateAudioBox(q, false);
  const input = $("answer-input");
  input.value = "";
  input.disabled = false;
  setAnswerPlaceholder(q);   // 课程内层题型混合：占位文案随每题重设
  $("submit-btn").disabled = false;
  $("skip-btn").style.display = "none";  // 课程内「我不会」用 Esc 触发
  $("next-btn").style.display = "none";
  $("next-btn").textContent = "下一题";
  const fb = $("feedback");
  fb.className = "feedback hidden";
  fb.innerHTML = "";
  $("input-phase").classList.toggle("hidden", !typing);
  $("flashcard-controls").classList.add("hidden");
  $("exam-controls").classList.toggle("hidden", !(choice || order));
  if (choice) {
    buildExamOptions(q);
  } else if (order) {
    buildOrderUI(q);
  }
  // 听音题干的自动播放在 renderPrompt 里统一处理，这里只管打字框聚焦
  if (typing && !(ui.audio && !state.noAudio)) {
    input.focus();
  }
  state.questionStartTime = Date.now();
  // 预取下一题音频（课程内含听音题型，切题自动播，提前拉取降低等待）
  prefetchNextAudio();
}

// ---- 组句排序：词块点选 + 拖拽，拼回完整例句 ----

// 拖放落点：给一组按 DOM 顺序排列的词块矩形和指针坐标，返回应插入的位置
// （索引 0..n，n 表示末尾）。抽成纯函数是为了脱离浏览器也能测这段几何逻辑。
function orderDropIndex(rects, x, y) {
  if (!rects.length) return 0;
  // 词块按 flex-wrap 排布，可能折行：top 相差不到半个块高的算同一行
  const rows = [];
  for (let i = 0; i < rects.length; i++) {
    const r = rects[i];
    const row = rows.find((g) => Math.abs(g.top - r.top) < r.height / 2);
    if (row) row.items.push({ i, r });
    else rows.push({ top: r.top, items: [{ i, r }] });
  }
  // 落在哪一行：y 在该行范围内最好，否则取 top 最接近的一行
  let row = rows.find(
    (g) => y >= g.top - 2 && y <= g.top + g.items[0].r.height + 2);
  if (!row) {
    row = rows.reduce((a, b) => (Math.abs(b.top - y) < Math.abs(a.top - y) ? b : a));
  }
  for (const it of row.items) {
    if (x < it.r.left + it.r.width / 2) return it.i;  // 指针在这个块左半边：插到它前面
  }
  return row.items[row.items.length - 1].i + 1;       // 指针在该行最右侧：插到行尾
}

function orderInsertAt(zone, chip, x, y) {
  // 落点只在「除被拖块以外」的块之间算：把自己算进去会插到自己身上，等于没动
  const chips = [...zone.querySelectorAll(".order-chip")].filter((c) => c !== chip);
  const rects = chips.map((c) => {
    const r = c.getBoundingClientRect();
    return { left: r.left, top: r.top, width: r.width, height: r.height };
  });
  const ref = chips[orderDropIndex(rects, x, y)] || null;
  // 位置没变就别动：dragover 每几毫秒触发一次，反复 insertBefore 会让布局抽搐
  if (chip.parentElement === zone && chip.nextElementSibling === ref) return;
  zone.insertBefore(chip, ref);
}

function buildOrderUI(q) {
  const ec = $("exam-controls");
  ec.innerHTML = `
    <p class="hint">点词块放进句子，或直接拖到想要的位置；句子里的词块可以再拖动调换顺序</p>
    <div class="order-line" id="order-line"></div>
    <div class="order-tray" id="order-tray">
      ${q.blocks.map((b, i) =>
        `<button class="order-chip" draggable="true" data-i="${i}">${escapeHtml(b)}</button>`).join("")}
    </div>
    <div class="btn-row"><button id="order-check" class="primary" type="button" disabled>检查 (Enter)</button></div>`;
  const line = $("order-line"), tray = $("order-tray"), check = $("order-check");
  const sync = () => { check.disabled = tray.children.length > 0; };
  // 点击：在两个区域之间来回放（追加到末尾），适合先快速拼完
  const toggleChip = (chip) => {
    if (!chip) return;
    (chip.parentElement === tray ? line : tray).appendChild(chip);
    sync();
  };
  // 拖拽：按指针落点插入——在句子内拖就是换语序，跨区域拖才是放进/拿走
  const dropChipAt = (zone, chip, x, y) => {
    if (!chip) return;
    orderInsertAt(zone, chip, x, y);
    sync();
  };
  ec.querySelectorAll(".order-chip").forEach((chip) => {
    chip.addEventListener("click", () => toggleChip(chip));
    chip.addEventListener("dragstart", (e) => {
      course.dragChip = chip;
      chip.classList.add("dragging");
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = "move";
        // 不 setData 的话部分浏览器不派发 drop
        e.dataTransfer.setData("text/plain", chip.textContent);
      }
    });
    chip.addEventListener("dragend", () => {
      chip.classList.remove("dragging");
      course.dragChip = null;
      sync();
    });
  });
  [line, tray].forEach((zone) => {
    zone.addEventListener("dragover", (e) => {
      if (!course.dragChip) return;
      e.preventDefault();  // 声明此处可放置，否则浏览器不派发 drop
      // 拖动过程中就实时就位：拖到哪插到哪，句子顺序一眼可见
      dropChipAt(zone, course.dragChip, e.clientX, e.clientY);
    });
    zone.addEventListener("drop", (e) => {
      e.preventDefault();  // 阻止浏览器把拖放的文本插进页面
      dropChipAt(zone, course.dragChip, e.clientX, e.clientY);
    });
  });
  check.addEventListener("click", checkOrderAnswer);
  sync();
}

function orderAnswerText() {
  return [...$("order-line").querySelectorAll(".order-chip")]
    .map((c) => c.textContent).join("");
}

// ---- 作答与判分（课程统一入口；/api/check 按内层题型判分） ----
async function courseCheck(q, answer, skipped) {
  state.examAnswered = true;
  const reaction = state.questionStartTime > 0 ? Date.now() - state.questionStartTime : 0;
  let data;
  try {
    const res = await fetch("/api/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: q.ref, mode: q.mode, answer, form: q.conj_form || "" }),
    });
    data = await res.json();
  } catch (e) {
    state.examAnswered = false;
    alert("校验失败：" + e);
    return;
  }
  if (data.error) {
    state.examAnswered = false;
    alert(data.error);
    return;
  }
  if (course.round > 0) {
    course.reviews += 1;  // 回炉重做：只计数，不进 results（不动掌握度）
  } else {
    state.results.push({
      ref: q.ref, correct: data.correct, skipped: skipped,
      prompt: q.prompt, your_answer: answer, expected: data.expected,
      word: data.word, qtype: q.mode,  // qtype：后端按题型决定计分规则
    });
    if (data.correct) {
      state.correctCount += 1;
      state.streak += 1;
      if (state.streak > state.maxStreak) state.maxStreak = state.streak;
    } else {
      state.streak = 0;
    }
    if (reaction > 0) state.reactionTimes.push(reaction);
    $("q-score").textContent = `正确 ${state.correctCount} · 连续 ${state.streak}`;
  }
  if (!data.correct) course.retryList.push(q);  // 错题回炉，重做到答对为止
  // 课程选择题：与独立模式一致地高亮正误选项（键盘或点选都需要看到结果）
  if (Array.isArray(q.options) && q.options.includes(String(answer))) {
    const clicked = q.options.indexOf(String(answer));
    const correctIdx = q.options.indexOf(data.expected);
    document.querySelectorAll("#exam-controls .exam-opt").forEach((b, i) => {
      b.classList.add("answered");
      b.classList.remove("opt-cursor");
      if (i === correctIdx) b.classList.add("opt-correct");
      else if (i === clicked && !data.correct) b.classList.add("opt-wrong");
    });
  }
  showFeedback(data, answer, skipped, q.ref);
}

function courseAnswerChoice(idx) {
  if (state.examAnswered) return;
  const q = course.q;
  courseCheck(q, q.options[idx], false);
}

async function courseSubmitTyping() {
  if (state.examAnswered) return;
  const input = $("answer-input");
  const answer = romajiToKana(input.value, true);  // 结尾单 n 收为 ん，与练习模式一致
  if (!answer.trim()) return;
  courseCheck(course.q, answer, false);
}

function checkOrderAnswer() {
  // 组句「检查」的统一入口：课程内走 courseCheck（回炉计一次），
  // 独立模式走 submitWithAnswer（普通打字判分流程）
  if (state.examAnswered || state.checking) return;
  const text = orderAnswerText();
  if (!text) return;
  if (state.config && state.config.mode === "course") {
    courseCheck(course.q, text, false);
  } else {
    state.examAnswered = true;  // 反馈期间再点检查/按 Enter 不重复计分
    submitWithAnswer(text, false);
  }
}

// ---- 词义配对：词↔释义瓦片点选，客户端判定，按词计结果（连对闸与选择题一致） ----
const pairState = {
  selWord: null,    // 当前选中的词瓦片
  selMeaning: null, // 当前选中的释义瓦片
  mistakes: {},     // ref -> 配错次数
  matched: 0,
};

function shuffleArr(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

function buildPairUI(q) {
  const ec = $("exam-controls");
  const words = shuffleArr(q.pairs.map((p) => ({ ...p })));
  const meanings = shuffleArr(q.pairs.map((p) => ({ ...p })));
  ec.innerHTML = `
    <p class="hint">点击一个词和一个释义配对；配错会计一次失误（该词本组需零失误才算掌握）</p>
    <div class="pair-grid">
      <div class="pair-col">${words.map((p) =>
        `<button class="pair-tile" data-kind="word" data-ref="${escapeHtml(p.ref)}">${escapeHtml(p.form)}</button>`).join("")}</div>
      <div class="pair-col">${meanings.map((p) =>
        `<button class="pair-tile" data-kind="meaning" data-ref="${escapeHtml(p.ref)}">${escapeHtml(p.meaning)}</button>`).join("")}</div>
    </div>`;
  ec.querySelectorAll(".pair-tile").forEach((tile) => {
    tile.addEventListener("click", () => pairTileTap(tile, q));
  });
}

function pairTileTap(tile, q) {
  if (state.examAnswered || tile.classList.contains("pair-matched")) return;
  const kind = tile.dataset.kind;
  const slotKey = kind === "word" ? "selWord" : "selMeaning";
  if (pairState[slotKey] === tile) {
    tile.classList.remove("pair-selected");  // 再点一次取消选择
    pairState[slotKey] = null;
    return;
  }
  if (pairState[slotKey]) {
    pairState[slotKey].classList.remove("pair-selected");
  }
  pairState[slotKey] = tile;
  tile.classList.add("pair-selected");
  if (kind === "word") playTTS(`/api/tts?ref=${encodeURIComponent(tile.dataset.ref)}`);
  if (!(pairState.selWord && pairState.selMeaning)) return;

  // 两端都已选中：判定
  const wordTile = pairState.selWord, meaningTile = pairState.selMeaning;
  pairState.selWord = null;
  pairState.selMeaning = null;
  if (wordTile.dataset.ref === meaningTile.dataset.ref) {
    wordTile.classList.remove("pair-selected");
    meaningTile.classList.remove("pair-selected");
    wordTile.classList.add("pair-matched");
    meaningTile.classList.add("pair-matched");
    pairState.matched += 1;
    if (pairState.matched === q.pairs.length) completePairRound(q);
  } else {
    pairState.mistakes[wordTile.dataset.ref] =
      (pairState.mistakes[wordTile.dataset.ref] || 0) + 1;
    wordTile.classList.add("pair-wrong");
    meaningTile.classList.add("pair-wrong");
    setTimeout(() => {
      wordTile.classList.remove("pair-selected", "pair-wrong");
      meaningTile.classList.remove("pair-selected", "pair-wrong");
    }, 450);
  }
}

function completePairRound(q) {
  state.examAnswered = true;
  for (const p of q.pairs) {
    const correct = !(pairState.mistakes[p.ref] > 0);
    state.results.push({
      ref: p.ref, correct, skipped: false,
      prompt: p.form, your_answer: p.meaning, expected: p.meaning,
      word: {
        kanji: p.form, hiragana: p.hiragana, romaji: "",
        meaning: p.meaning, notes: "", example_ja: "", example_zh: "",
      },
      qtype: "pair_match",
    });
    if (correct) {
      state.correctCount += 1;
      state.streak += 1;
      if (state.streak > state.maxStreak) state.maxStreak = state.streak;
    } else {
      state.streak = 0;
    }
  }
  $("q-score").textContent = `正确 ${state.correctCount} · 连续 ${state.streak}`;
  const wrongPairs = q.pairs.filter((p) => pairState.mistakes[p.ref] > 0);
  const fb = $("feedback");
  fb.className = "feedback " + (wrongPairs.length ? "wrong" : "correct");
  fb.innerHTML = `
    <div class="fb-status">${wrongPairs.length
      ? `本组配对完成，失误 ${wrongPairs.length} 处`
      : "本组全部配对成功 ✓"}</div>
    ${wrongPairs.map((p) =>
      `<div class="fb-your">⚠ ${escapeHtml(p.form)} → ${escapeHtml(p.meaning)}</div>`).join("")}`;
  $("next-btn").style.display = "";
  $("next-btn").focus({ preventScroll: true });   // 同上：别让页面跳到底部
}

// ---------- 汉字识字卡（按词条聚合汉字，浏览学习，不排调度） ----------
const kstate = { list: [], index: 0, lesson: "", title: "" };
// 浏览位置按课程记在 localStorage：全部课程有 500+ 个字，一关页面就回到第 1 个，
// 翻到第几百个字的进度全丢，等于每次都得从头翻
const KANJI_POS_PREFIX = "kanji_pos_v1:";

function kanjiSavedPos(lesson) {
  try {
    return parseInt(localStorage.getItem(KANJI_POS_PREFIX + lesson) || "0", 10) || 0;
  } catch (e) {
    return 0;  // 隐私模式/禁用存储：退化为从头开始，不影响使用
  }
}

async function startKanji() {
  const lesson = $("lesson").value;
  try {
    const res = await fetch(`/api/kanji?lesson=${encodeURIComponent(lesson)}`);
    const data = await res.json();
    if (data.error) {
      alert(data.error);
      return;
    }
    if (!data.kanji.length) {
      alert("该课程没有含汉字的词条");
      return;
    }
    kstate.list = data.kanji;
    // 接着上次的位置继续（词表变动导致越界时从头开始）
    kstate.index = Math.min(kanjiSavedPos(data.lesson), data.kanji.length - 1);
    kstate.lesson = data.lesson;
    kstate.title = data.title || data.lesson;
    showScreen("kanji-screen");
    renderKanjiCard();
  } catch (e) {
    alert("加载汉字卡失败：" + e);
  }
}

function renderKanjiCard() {
  const k = kstate.list[kstate.index];
  $("k-progress").textContent = `第 ${kstate.index + 1} / ${kstate.list.length} 字`;
  $("k-lesson").textContent = kstate.title;
  $("k-char").textContent = k.char;
  $("k-count").textContent = `出现在 ${k.words.length} 个词里`;
  $("k-words").innerHTML = k.words.map((w) => `
    <div class="k-word ${masteryClass(w.mastery) || ""}">
      ${masteryTag(w.mastery)}
      <button class="ex-audio-btn k-audio-btn" data-ref="${escapeHtml(w.ref)}"
              type="button" title="读这个词">🔊</button>
      <div class="k-word-text">
        <div class="k-word-form">${escapeHtml(w.form)}<span class="fb-roma">${escapeHtml(w.hiragana)}</span></div>
        <div class="k-word-meaning">${escapeHtml(w.meaning)}</div>
      </div>
    </div>`).join("");
  $("k-prev").disabled = kstate.index === 0;
  $("k-next").disabled = kstate.index === kstate.list.length - 1;
  try {
    localStorage.setItem(KANJI_POS_PREFIX + kstate.lesson, String(kstate.index));
  } catch (e) { /* 存储不可用时只影响续读，不影响浏览 */ }
  resetRegenUI();  // 换字：清掉上一个字的重置语音试听面板
  $("k-regen-btn").classList.toggle("hidden", !k.words.length);  // 无词可读时不留死按钮
  // 自动读第一个词：建立「字→音」的第一印象
  if (k.words.length) playTTS(`/api/tts?ref=${encodeURIComponent(k.words[0].ref)}`);
}

function kanjiNext() {
  if (kstate.index < kstate.list.length - 1) {
    kstate.index += 1;
    renderKanjiCard();
  }
}

function kanjiPrev() {
  if (kstate.index > 0) {
    kstate.index -= 1;
    renderKanjiCard();
  }
}

// ---------- 动词活用卡（逐词浏览 ます形/て形/た形/ない形，不排调度） ----------
const cstate = { list: [], index: 0, lesson: "", title: "" };
// 浏览位置按课程记在 localStorage（与汉字卡同理：上百个词每次从头翻等于白逛）
const CONJ_POS_PREFIX = "conj_pos_v1:";

function conjSavedPos(lesson) {
  try {
    return parseInt(localStorage.getItem(CONJ_POS_PREFIX + lesson) || "0", 10) || 0;
  } catch (e) {
    return 0;  // 隐私模式/禁用存储：退化为从头开始，不影响使用
  }
}

async function startConj() {
  const lesson = $("lesson").value;
  try {
    const res = await fetch(`/api/conj?lesson=${encodeURIComponent(lesson)}`);
    const data = await res.json();
    if (data.error) {
      alert(data.error);
      return;
    }
    if (!data.verbs.length) {
      alert("该课程没有动词词条");
      return;
    }
    cstate.list = data.verbs;
    // 接着上次的位置继续（数据变动导致越界时从头开始）
    cstate.index = Math.min(conjSavedPos(data.lesson), data.verbs.length - 1);
    cstate.lesson = data.lesson;
    cstate.title = data.title || data.lesson;
    showScreen("conj-screen");
    renderConjCard();
  } catch (e) {
    alert("加载动词活用卡失败：" + e);
  }
}

const CONJ_ROW_DEFS = [
  ["masu", "ます形"], ["te", "て形"], ["ta", "た形"], ["nai", "ない形"],
];

function renderConjCard() {
  const c = cstate.list[cstate.index];
  $("c-progress").textContent = `第 ${cstate.index + 1} / ${cstate.list.length} 词`;
  $("c-lesson").textContent = cstate.title;
  $("c-basic").textContent = c.basic;
  // 多邻国词条的写法是ます形（起きます）：基本形为主，写法标注在旁
  $("c-meta").textContent = c.written === c.basic
    ? `${c.reading} · ${c.meaning}`
    : `词条写法 ${c.written} · ${c.reading} · ${c.meaning}`;
  $("c-infl").textContent = c.note ? `${c.infl_type}｜${c.note}` : c.infl_type;
  $("c-rows").innerHTML = CONJ_ROW_DEFS.map(([key, label]) => `
    <div class="conj-row">
      <button class="conj-audio-btn" data-text="${escapeHtml(c.forms[key] || "")}"
              type="button" title="读${label}">🔊</button>
      <span class="conj-label">${label}</span>
      <span class="conj-form" lang="ja">${escapeHtml(c.forms[key] || "—")}</span>
    </div>`).join("");
  // 例句从词表按 ref 现取（/api/conj 下发），无例句不渲染
  $("c-example").innerHTML = c.example_ja
    ? `例句：${escapeHtml(c.example_ja)}${c.example_zh ? `（${escapeHtml(c.example_zh)}）` : ""}`
    : "";
  $("c-prev").disabled = cstate.index === 0;
  $("c-next").disabled = cstate.index === cstate.list.length - 1;
  try {
    localStorage.setItem(CONJ_POS_PREFIX + cstate.lesson, String(cstate.index));
  } catch (e) { /* 存储不可用时只影响续读，不影响浏览 */ }
  // 自动读基本形：建立「词→音」的第一印象（与汉字卡自动读第一个词同理）
  playConjBasic();
}

// 读基本形：词条写法就是基本形（開く）时直接用词表预生成音频；多邻国转来的
// 词条写法是ます形（起きます），ref 音频读出来是 okimasu，须按基本形现读
function playConjBasic() {
  const c = cstate.list[cstate.index];
  if (!c) return;
  if (c.written === c.basic) {
    playTTS(`/api/tts?ref=${encodeURIComponent(c.ref)}`);
  } else {
    playTTS(`/api/tts/text?text=${encodeURIComponent(c.basic)}`);
  }
}

function conjNext() {
  if (cstate.index < cstate.list.length - 1) {
    cstate.index += 1;
    renderConjCard();
  }
}

function conjPrev() {
  if (cstate.index > 0) {
    cstate.index -= 1;
    renderConjCard();
  }
}

// ---- 流程推进：主队列 → 回炉轮次（循环）→ 交卷 ----
function courseNext() {
  course.pos += 1;
  if (course.pos < course.queue.length) return renderCourseQuestion();
  return courseQueueTail();
}

// 课程队列走完的统一收尾：有待回炉题开下一轮，否则交卷；
// 「正常答完」与「跳过听力题把当前轮跳空」共用（跳空时 pos 不动、queue 已空）
function courseQueueTail() {
  if (course.retryList.length > 0) {
    course.queue = course.retryList;
    course.retryList = [];
    course.pos = 0;
    course.round += 1;
    return renderCourseQuestion();
  }
  return endSessionOrBackHome();  // mode=course，掌握度只由首次作答驱动
}

// ---------- 播放音频（下载成 blob 再播，避免流式播放时开头音节被吞） ----------
// blob 按 URL 缓存：F2 重播/错题重听不再重新下载；上限防止长时间练习占用过多内存
const ttsBlobCache = new Map();
const TTS_CACHE_MAX = 300;
// 播放请求序号：切题后慢返回的旧请求不得覆盖当前题的音频（听音模式会答错题）
let playSeq = 0;

function fetchTTSBlob(url) {
  const hit = ttsBlobCache.get(url);
  if (hit) return Promise.resolve(hit);
  return fetch(url)
    .then((res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.blob();
    })
    .then((blob) => {
      ttsBlobCache.set(url, blob);
      if (ttsBlobCache.size > TTS_CACHE_MAX) {
        ttsBlobCache.delete(ttsBlobCache.keys().next().value);
      }
      return blob;
    });
}

function showAudioError() {
  // 听音题干把提示直接写在题面位置（那里没有播放按钮可提示）
  if (uiOf(currentQ()).audio === true) {
    $("q-prompt").textContent = "⚠️ 音频加载失败，按 F2 或点播放按钮重试";
    return;
  }
  const btn = $("play-audio-btn");
  if (btn && !$("q-audio-box").classList.contains("hidden")) {
    const prev = btn.textContent;
    btn.textContent = "⚠️ 加载失败";
    setTimeout(() => { btn.textContent = prev; }, 2000);
  }
}

// 播一个 blob；seq 传入时作旧请求丢弃（切题后慢返回的旧音频不得覆盖新题）
function playBlob(blob, seq) {
  let audio = document.getElementById("hidden-audio");
  if (!audio) {
    audio = document.createElement("audio");
    audio.id = "hidden-audio";
    audio.style.display = "none";
    document.body.appendChild(audio);
  }
  if (seq === undefined) seq = ++playSeq;
  else if (seq !== playSeq) return;
  audio.pause();
  // 释放上一次的 blob URL，避免内存泄漏（缓存里存的是 blob 本体，不受影响）
  if (audio.src && audio.src.startsWith("blob:")) {
    URL.revokeObjectURL(audio.src);
  }
  audio.removeAttribute("src");
  const objUrl = URL.createObjectURL(blob);
  audio.src = objUrl;
  audio.currentTime = 0;
  audio.play().catch((err) => {
    console.warn("音频播放失败:", err);
    if (seq === playSeq) showAudioError();
  });
}

function playTTS(url) {
  const seq = ++playSeq;
  fetchTTSBlob(url)
    .then((blob) => playBlob(blob, seq))
    .catch((err) => {
      console.warn("音频加载失败:", err);
      if (seq === playSeq) showAudioError();
    });
}

function playQuestionAudio(ref) {
  // 聴解真题：优先播放题库自带的官方音频切片。
  // 用 currentQ() 而非 state.questions[state.index]——课程模式下后者
  // 恒为第 1 题（推进用的是 course.pos），会播错词。
  const q = currentQ();
  if (q && q.audio) {
    playTTS(q.audio);
    return;
  }
  playTTS(`/api/tts?ref=${encodeURIComponent(ref)}`);
}

// 播放例句发音（/api/tts field=example）
function playExampleAudio(ref) {
  playTTS(`/api/tts?ref=${encodeURIComponent(ref)}&field=example`);
}

// ---------- 重置语音：发音不对/残缺时重新生成，先试听再决定是否覆盖 ----------
function base64ToBlob(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: "audio/mpeg" });
}

// 练习页（当前题）、学习卡片（当前张）、汉字卡（当前字的第一个词）共用同一套
// 流程，差别只在「当前词是哪一个」与「状态写到哪块 DOM」——把这两点抽成 ctx，
// 三处各传自己的即可。
const REGEN_CTX = {
  practice: { btnId: "regen-audio-btn", panelId: "regen-panel" },
  learn: { btnId: "learn-regen-btn", panelId: "learn-regen-panel" },
  kanji: { btnId: "k-regen-btn", panelId: "k-regen-panel" },
};

function regenRef(which) {
  if (which === "learn") {
    const item = learn.words[learn.index];
    return item ? item.ref : "";
  }
  if (which === "kanji") {
    // 与「读第一个词 (F2)」同一口径：汉字卡默认发音的就是这个词的音
    const k = kstate.list[kstate.index];
    return k && k.words.length ? k.words[0].ref : "";
  }
  const q = currentQ();
  return q ? q.ref : "";
}

async function regenCurrentAudio(which = "practice") {
  const ctx = REGEN_CTX[which];
  const ref = regenRef(which);
  if (state.regenBusy || !ref) return;
  state.regenBusy = true;
  $(ctx.btnId).disabled = true;
  const panel = $(ctx.panelId);
  panel.classList.remove("hidden");
  panel.innerHTML = '<span class="regen-status">正在重新生成（自动多生成几次取最完整的一版）…</span>';
  let data;
  try {
    const res = await fetch("/api/tts/regen", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref }),
    });
    data = await res.json();
  } catch (e) {
    panel.innerHTML = `<span class="regen-status">生成失败：${escapeHtml(String(e))}</span>`;
    state.regenBusy = false;
    $(ctx.btnId).disabled = false;
    return;
  }
  state.regenBusy = false;
  $(ctx.btnId).disabled = false;
  if (data.error) {
    panel.innerHTML = `<span class="regen-status">${escapeHtml(data.error)}</span>`;
    return;
  }
  playBlob(base64ToBlob(data.audio));
  panel.innerHTML = `
    <span class="regen-status">新版发音试听中（${Math.round(data.size / 1024)} KB）：</span>
    <button class="regen-btn regen-primary" data-act="apply" type="button">✓ 用这个（覆盖）</button>
    <button class="regen-btn" data-act="discard" type="button">✕ 保留原来的</button>
    <button class="regen-btn" data-act="again" type="button">↻ 再生成一次</button>`;
  panel.querySelector('[data-act="apply"]').addEventListener("click", () => applyRegen("new", which));
  panel.querySelector('[data-act="discard"]').addEventListener("click", () => applyRegen("old", which));
  panel.querySelector('[data-act="again"]').addEventListener("click", () => regenCurrentAudio(which));
}

async function applyRegen(keep, which = "practice") {
  const ctx = REGEN_CTX[which];
  const ref = regenRef(which);
  if (!ref) return;
  const panel = $(ctx.panelId);
  try {
    const res = await fetch("/api/tts/regen/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref, keep }),
    });
    const data = await res.json();
    if (data.error) {
      panel.innerHTML = `<span class="regen-status">${escapeHtml(data.error)}</span>`;
      return;
    }
    if (keep === "new") {
      // 覆盖后必须清掉前端 blob 缓存，否则重播（F2）还是旧音频
      ttsBlobCache.delete(`/api/tts?ref=${encodeURIComponent(ref)}`);
      panel.innerHTML = '<span class="regen-status">✓ 已覆盖，之后播放的就是新发音</span>';
    } else {
      panel.innerHTML = '<span class="regen-status">已保留原来的发音</span>';
    }
  } catch (e) {
    panel.innerHTML = `<span class="regen-status">操作失败：${escapeHtml(String(e))}</span>`;
    return;
  }
  setTimeout(() => { panel.classList.add("hidden"); panel.innerHTML = ""; }, 2500);
}

// 切题/切卡片时清掉上一张的试听面板（练习页、学习卡片一起清，避免留残影）
function resetRegenUI() {
  state.regenBusy = false;
  Object.values(REGEN_CTX).forEach((ctx) => {
    const btn = $(ctx.btnId);
    if (btn) btn.disabled = false;
    const panel = $(ctx.panelId);
    if (!panel) return;
    panel.classList.add("hidden");
    panel.innerHTML = "";
  });
}

// 立即停止正在播放/加载中的音频（跳过听力题、切题时用）
function stopTTS() {
  playSeq++;  // 作废在途的加载/播放请求，防止旧音频覆盖新题
  const audio = document.getElementById("hidden-audio");
  if (!audio) return;
  audio.pause();
  if (audio.src) {
    if (audio.src.startsWith("blob:")) URL.revokeObjectURL(audio.src);
    audio.removeAttribute("src");
  }
}

// ---------- 提交答案 ----------
async function submitAnswer() {
  const input = $("answer-input");
  // rawInput 模式（写汉字）原样提交；其余模式 finalize=true 把结尾悬着的单 n 收为 ん
  // （与 IME 回车确认行为一致）
  const answer = rawInputFor(currentQ()) ? input.value : romajiToKana(input.value, true);
  if (!answer.trim()) return;
  await submitWithAnswer(answer, false);
}

// ---------- 我不会：跳过作答，直接揭示答案（计为错误） ----------
async function skipAnswer() {
  await submitWithAnswer("", true);
}

// ⏭ 听力题「不方便听」：把当前题从本轮剔除——不判对错、不改掌握度、
// 不进错题本；并静音本组剩余听力的自动播放（避免安静场合突然出声）。
// 被跳过的词不更新复习时间，按原调度下次还会再来。
function skipNoAudio() {
  if (state.checking || state.finishing) return;  // 防与判分/交卷在途并发
  if (!isListeningQuestion(currentQ())) return;
  stopTTS();
  state.noAudio = true;
  if (state.config && state.config.mode === "course") {
    course.queue.splice(course.pos, 1);
    if (course.pos < course.queue.length) return renderCourseQuestion();
    return courseQueueTail();  // 当前轮被跳空：按课程统一收尾逻辑推进
  }
  state.questions.splice(state.index, 1);
  // 剔掉的正是最后一张：index 已在末尾之外，而它前面全部答过 → 与正常
  // 推进到组尾同样收尾（空组时 endSessionOrBackHome 会静默回设置页）。
  // 只判 length===0 会漏掉这种越界，renderQuestion 取到 undefined 直接抛错，
  // 本屏卡住且已答结果不再交卷。
  if (state.index >= state.questions.length) return endSessionOrBackHome();
  renderQuestion();
}

// 跳空后的收尾：还有已答结果就正常交卷；一题未答则像「退出」一样静默回设置页
function endSessionOrBackHome() {
  if (state.results.length > 0) return finishSession();
  stopTTS();
  state.inProgress = false;
  backToModule();
}

// 反悔入口：恢复本组自动发音，并对当前听力题立即重播一遍
function restoreAudio() {
  state.noAudio = false;
  refreshNoAudioRow();
  const q = currentQ();
  if (q && isListeningQuestion(q) && !state.examAnswered && !state.checking) {
    playQuestionAudio(q.ref);
  }
}

// 「跳过听力题」打开时，把本轮**还没做**的听力题整批剔除：与逐题「不方便听」同一
// 语义——不判对错、不计分、不改掌握度、不进错题本（跳过的词按原调度下次还会来）。
// 只从「当前题及其后」剔：前面的都已答过，它们在 results 里，是不是听力题无关。
function dropListeningQuestions() {
  if (state.config && state.config.mode === "course") {
    course.queue = course.queue.slice(0, course.pos)
      .concat(course.queue.slice(course.pos).filter((q) => !isListeningQuestion(q)));
    return;
  }
  state.questions = state.questions.filter(
    (q, i) => i < state.index || !isListeningQuestion(q));
}

// 头部那个开关的显示：状态跟着本机偏好走
function paintMuteBtn() {
  const btn = $("mute-btn");
  if (!btn) return;
  const on = skipAudioPref();
  btn.classList.toggle("on", on);
  btn.textContent = on ? "🔇 已跳过听力题" : "🔇 跳过听力题";
  btn.title = on
    ? "本轮不出听力题（点一下恢复；开关记在本机，下次开练仍然生效）"
    : "本轮不出听力题：上课或任何不方便出声时用；开关记在本机，下次开练仍然生效";
}

// 点开关：立刻改本机偏好、停掉自动发音，并把剩余听力题剔掉。
// 当前题没变就不重画（别把用户已经敲了一半的答案擦掉）。
function toggleSkipAudio() {
  if (state.checking || state.finishing) return;
  const on = !skipAudioPref();
  setSkipAudioPref(on);
  state.noAudio = on;
  paintMuteBtn();
  if (!on) return;
  const cur = currentQ();
  dropListeningQuestions();
  if (state.config && state.config.mode === "course") {
    if (course.pos >= course.queue.length) return courseQueueTail();
    if (currentQ() !== cur) return renderCourseQuestion();
    return refreshNoAudioRow();
  }
  if (state.index >= state.questions.length) return endSessionOrBackHome();
  if (currentQ() !== cur) return renderQuestion();
  refreshNoAudioRow();
}

async function submitWithAnswer(answer, skipped) {
  // 在途闸：/api/check 未返回前再按 Enter/Esc 不发第二个请求
  //（否则同题计两次分）。新题渲染时在 renderQuestion 里复位。
  if (state.checking) return;
  const q = state.questions[state.index];
  state.checking = true;
  // 计算反应时间：从本题渲染到提交的耗时
  const reaction = state.questionStartTime > 0
    ? Date.now() - state.questionStartTime
    : 0;
  let data;
  try {
    const res = await fetch("/api/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: q.ref, mode: state.config.mode, answer, form: q.conj_form || "" }),
    });
    data = await res.json();
  } catch (e) {
    state.checking = false;
    alert("校验失败：" + e);
    return;
  }
  if (data.error) {
    state.checking = false;
    alert(data.error);
    return;
  }
  state.results.push({
    ref: q.ref,
    correct: data.correct,
    skipped: skipped,
    prompt: q.prompt,
    your_answer: answer,
    expected: data.expected,
    word: data.word,
  });
  // 更新连续答对数和反应时间
  if (data.correct) {
    state.correctCount++;
    state.streak++;
    if (state.streak > state.maxStreak) state.maxStreak = state.streak;
  } else {
    state.streak = 0;
  }
  if (reaction > 0) state.reactionTimes.push(reaction);
  // 实时更新得分显示
  $("q-score").textContent = `正确 ${state.correctCount} · 连续 ${state.streak}`;
  showFeedback(data, answer, skipped, q.ref);
}

// 掌握度三档标注：0=新(m-new)、1-2=在学(m-learn)、3-5=会了(m-known)。
// 掌握度在 /api/finish 才落盘，接口下发的是「作答前」档位。
// 真题/助词的 word 是虚拟构造、没有 mastery 字段：缺数据或非法值返回 null，
// 不能 fallback 成 0——否则那些题会被误标成「新」。
function masteryClass(m) {
  if (!Number.isInteger(m) || m < 0 || m > 5) return null;
  if (m === 0) return "m-new";
  if (m <= 2) return "m-learn";
  return "m-known";
}

function masteryTag(m) {
  const cls = masteryClass(m);
  if (!cls) return "";
  const label = { "m-new": "新", "m-learn": "在学", "m-known": "会了" }[cls];
  return `<span class="m-tag ${cls}">${label}</span>`;
}

function exampleHtml(word, ref) {
  // 例句块：日中原句 + 试听按钮（无例句返回空串）
  if (!word || !word.example_ja) return "";
  return `
    <div class="fb-example">
      <button class="ex-audio-btn" data-ref="${escapeHtml(ref)}" title="播放例句">🔊</button>
      <div class="fb-ex-text">
        <div class="fb-ex-ja">${escapeHtml(word.example_ja)}</div>
        <div class="fb-ex-zh">${escapeHtml(word.example_zh || "")}</div>
      </div>
    </div>`;
}

function showFeedback(data, userAnswer, skipped, ref) {
  const fb = $("feedback");
  fb.className = "feedback " + (data.correct ? "correct" : "wrong");
  $("no-audio-row").classList.add("hidden");  // 已作答，跳过行随之隐藏
  const w = data.word;
  const kanjiDisplay = w.kanji && w.kanji !== "---" ? w.kanji : w.hiragana;
  let statusLine;
  if (skipped) {
    statusLine = "— 跳过（计为错误）";
  } else {
    statusLine = data.correct ? "✓ 正确" : "✗ 错误";
  }
  const yourLine = skipped
    ? ""
    : `<div class="fb-your">你的答案：<span>${escapeHtml(userAnswer)}</span></div>`;
  const correctLine = data.correct ? "" : `<div class="fb-correct">正确答案：<span>${escapeHtml(data.expected)}</span></div>`;
  // 错因提示：后端判断出差异只落在浊点/促音/长音这类假名写法上时给出
  const kinds = (!data.correct && !skipped && Array.isArray(data.mismatch)) ? data.mismatch : [];
  const EXAMPLE = { 浊点: "かこう ≠ がっこう", 促音: "いっかい ≠ いかい", 长音: "おばさん ≠ おばあさん" };
  const example = kinds.map((k) => EXAMPLE[k]).filter(Boolean)[0];
  const hintLine = kinds.length
    ? `<div class="fb-hint">差异只出在「${escapeHtml(kinds.join("、"))}」写法上（${escapeHtml(example || "这类差别会改变词义")}），仍按错处理。</div>`
    : "";
  // 配图模式的题干区已展示图片，反馈里不再重复；其余模式答错时补图加深记忆
  const answeredQ = currentQ();
  const imgOnScreen = !!(answeredQ && answeredQ.image);
  fb.innerHTML = `
    <div class="fb-status">${statusLine}</div>
    ${yourLine}
    ${correctLine}
    ${hintLine}
    <div class="fb-word ${masteryClass(w.mastery) || ""}">
      ${masteryTag(w.mastery)}
      ${w.image && !imgOnScreen ? `<img class="fb-img" src="${escapeHtml(w.image)}" alt="配图">` : ""}
      <div class="fb-kanji">${escapeHtml(kanjiDisplay)}</div>
      <div class="fb-hira">${escapeHtml(w.hiragana)}<span class="fb-roma">${escapeHtml(w.romaji)}</span></div>
      <div class="fb-meaning">${escapeHtml(w.meaning)}</div>
      ${w.notes ? `<div class="fb-notes">${escapeHtml(w.notes)}</div>` : ""}
      ${w.tip ? `<div class="fb-tip"><span class="fb-tip-label">💡 用法</span>${escapeHtml(w.tip)}</div>` : ""}
      ${w.conj ? `<div class="fb-tip"><span class="fb-tip-label">🔤 活用</span>て形 ${escapeHtml(w.conj.te)} ・ ない形 ${escapeHtml(w.conj.nai)}</div>` : ""}
    </div>
    ${exampleHtml(w, ref)}
  `;
  $("answer-input").disabled = true;
  $("submit-btn").disabled = true;
  $("skip-btn").disabled = true;
  const orderCheck = $("order-check");  // 组句题：反馈后禁用检查按钮（防重复计分）
  if (orderCheck) orderCheck.disabled = true;
  $("next-btn").style.display = "";
  // preventScroll：next-btn 是练习屏的最后一个元素，focus() 默认会把它滚进视野——
  // 每答完一题页面就滚到底，下一题的输入框又在顶部把页面拉回来，一题一跳。
  // 焦点照给（Enter 由全局监听推进，不依赖焦点在哪），只是别让浏览器动滚动条。
  $("next-btn").focus({ preventScroll: true });
  // 答案已揭示：剧透模式的播放按钮现在可以恢复（听音加深记忆）
  if (answeredQ) updateAudioBox(answeredQ, true);

  // 听音打字题答错/跳过时自动重播一次，加深听觉记忆（课程按内层题型判断）
  const innerUI = uiOf(answeredQ);
  if (innerUI.audio && innerUI.typing && !data.correct && !state.noAudio) {
    setTimeout(() => {
      const q = currentQ();
      if (q) playQuestionAudio(q.ref);
    }, 400);
  }
}

// ---------- 下一题 / 结束 ----------
function nextQuestion() {
  state.index++;
  if (state.index >= state.questions.length) {
    finishSession();
  } else {
    renderQuestion();
  }
}

async function finishSession() {
  if (state.finishing) return;  // 交卷在途：防双击/连按重复 POST
  state.finishing = true;
  // 计算平均反应时间（毫秒）
  const avgReaction = state.reactionTimes.length > 0
    ? Math.round(state.reactionTimes.reduce((a, b) => a + b, 0) / state.reactionTimes.length)
    : 0;
  let data;
  try {
    const res = await fetch("/api/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: state.config.mode,
        lesson: state.config.lesson,
        // 完整作答详情（含你的答案/正确答案/单词快照），便于事后分析错因与混淆词
        results: state.results,
        max_streak: state.maxStreak,
        avg_reaction_ms: avgReaction,
        session_id: state.sessionId,  // 后端据此识别重复交卷（幂等）
        // 课程模式：回炉重做次数（仅留档，不计掌握度）
        ...(state.config.mode === "course" ? { review_count: course.reviews } : {}),
      }),
    });
    data = await res.json();
  } catch (e) {
    state.finishing = false;
    alert("保存失败：" + e + "。可点「下一题」重试，不会重复计分。");
    return;
  }
  state.finishing = false;
  showResult(data);
}

function formatReaction(ms) {
  if (!ms || ms <= 0) return "—";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

function formatAccuracyDelta(cur, last) {
  if (last == null) return "";
  const delta = (cur - last) * 100;
  if (Math.abs(delta) < 0.1) return `<span class="cmp-same">（持平）</span>`;
  const cls = delta > 0 ? "cmp-up" : "cmp-down";
  const arrow = delta > 0 ? "↑" : "↓";
  return `<span class="${cls}">（${arrow} ${Math.abs(delta).toFixed(1)}%）</span>`;
}

function showResult(finishData) {
  state.inProgress = false;  // 已成功保存，离开页面不再提醒
  showScreen("result-screen");
  const stats = finishData.stats;
  const last = finishData.last;
  // 主统计行
  $("r-stats").innerHTML = `
    <div class="stat"><span class="num">${stats.correct}</span><span class="lbl">正确</span></div>
    <div class="stat"><span class="num">${stats.total - stats.correct}</span><span class="lbl">错误</span></div>
    <div class="stat"><span class="num">${(stats.accuracy * 100).toFixed(1)}%</span><span class="lbl">正确率</span></div>
  `;
  // 学习指标行
  const lastAcc = last ? last.accuracy : null;
  const lastStreak = last ? last.max_streak : null;
  const lastReaction = last ? last.avg_reaction_ms : null;
  $("r-metrics").innerHTML = `
    <div class="metric">
      <span class="num">${stats.max_streak}</span>
      <span class="lbl">最大连续答对</span>
      ${lastStreak != null ? `<span class="cmp">上次 ${lastStreak}</span>` : ""}
    </div>
    <div class="metric">
      <span class="num">${formatReaction(stats.avg_reaction_ms)}</span>
      <span class="lbl">平均反应</span>
      ${lastReaction ? `<span class="cmp">上次 ${formatReaction(lastReaction)}</span>` : ""}
    </div>
    <div class="metric">
      <span class="num">${(stats.accuracy * 100).toFixed(1)}%</span>
      <span class="lbl">对比上次</span>
      ${lastAcc != null ? formatAccuracyDelta(stats.accuracy, lastAcc) : '<span class="cmp-same">首次练习</span>'}
    </div>
  `;
  const wrongs = state.results.filter((r) => !r.correct);
  const wl = $("r-wrong-list");
  if (wrongs.length === 0) {
    wl.innerHTML = `<p class="all-correct">全部正确，太棒了！</p>`;
  } else {
    wl.innerHTML = wrongs
      .map((r) => {
        const k = r.word.kanji && r.word.kanji !== "---" ? `（${r.word.kanji}）` : "";
        const yourLabel = r.skipped ? "（跳过）" : escapeHtml(r.your_answer || "（自评不认识）");
        const yourTag = r.skipped ? "跳过" : (r.your_answer ? "你答" : "自评");
        // 配图题的 prompt 记的是图片 URL：错题回顾里还原成缩略图
        const isImgPrompt = r.prompt && r.prompt.indexOf("/static/images/") === 0;
        const promptHtml = isImgPrompt
          ? `<img class="wi-img" src="${escapeHtml(r.prompt)}" alt="配图">`
          : escapeHtml(r.prompt);
        return `
        <div class="wrong-item ${masteryClass(r.word.mastery) || ""}">
          ${masteryTag(r.word.mastery)}
          <div class="wi-prompt">${promptHtml}</div>
          <div class="wi-answer">${escapeHtml(r.word.hiragana)} ${escapeHtml(k)}</div>
          <div class="wi-meaning">${escapeHtml(r.word.meaning)}</div>
          <div class="wi-your">${yourTag}：${yourLabel}</div>
        </div>`;
      })
      .join("");
  }
  $("r-record").textContent = "记录已保存：" + finishData.record_file;
}

// ---------- 事件绑定 ----------
document.addEventListener("DOMContentLoaded", () => {
  // 首页是初始屏：不显式标一次的话它拿不到宽屏版心（showScreen 只在切屏时调用，
  // 而首次进入首页没人调用它——除非 hash 带着模块直达，那时 bootFromHash 会标）
  document.body.dataset.screen = "home-screen";
  loadConfig();
  loadHome();

  // ---- 外部入口：?practice_refs=ref1,ref2 直接开练（图片日语「练本课单词」）----
  // 点名词条（refs）绕过练习池——刚在图片课里挖的词还不该被「到期复习」筛掉；
  // focus 显式关掉，重点词清单开着时也不会把这些点名要练的词筛空。
  const bootParams = new URLSearchParams(location.search);
  const bootRefs = (bootParams.get("practice_refs") || "").split(",")
    .map((r) => r.trim()).filter(Boolean);
  if (bootRefs.length) {
    history.replaceState(null, "", location.pathname);   // 刷新不再重复开练
    startSession({
      lesson: bootParams.get("practice_lesson") || "all",
      mode: bootParams.get("practice_mode") || "kanji_to_kana",
      count: bootRefs.length,
      refs: bootRefs,
      focus: false,
    });
  }

  // ---- 首页导航 ----
  $("tiles").addEventListener("click", (e) => {
    const tile = e.target.closest(".tile");
    if (tile) openModule(tile.dataset.module);
  });
  $("module-items").addEventListener("click", (e) => {
    const btn = e.target.closest(".sub-item");
    if (btn) openItem(btn.dataset.item);
  });
  // 今日处方：事件委托——步骤列表每次刷新都会重绘
  $("plan").addEventListener("click", (e) => {
    const btn = e.target.closest(".plan-step");
    if (btn) startPlanStep(btn.dataset.step);
  });
  $("module-back-btn").addEventListener("click", () => showScreen("home-screen"));
  $("config-back-btn").addEventListener("click", backToModule);
  $("docs-back-btn").addEventListener("click", backToModule);
  $("scopes").addEventListener("change", () => applyScope(true));
  // 配置页的主按钮按子项类型分发（练习 / 学习 / 汉字卡）
  $("start-btn").addEventListener("click", () => {
    const kind = nav.item ? nav.item.kind : "mode";
    if (kind === "learn") return startLearn();
    if (kind === "kanji") return startKanji();
    if (kind === "conj") return startConj();
    return startSession();
  });
  $("submit-btn").addEventListener("click", submitAnswer);
  $("skip-btn").addEventListener("click", skipAnswer);
  $("skip-audio-btn").addEventListener("click", skipNoAudio);
  $("restore-audio-btn").addEventListener("click", restoreAudio);
  $("mute-btn").addEventListener("click", toggleSkipAudio);
  $("next-btn").addEventListener("click", () => {
    if (state.config && state.config.mode === "course") courseNext();
    else nextQuestion();
  });
  $("again-btn").addEventListener("click", startSession);
  // 退出本组练习：已作答的题提前交卷保存（计入掌握度），一题未答直接返回
  $("exit-btn").addEventListener("click", () => {
    if (state.finishing || state.checking) return;
    if (state.results.length === 0) {
      state.inProgress = false;
      backToModule();
      return;
    }
    const ok = confirm(
      `结束本组练习？已作答的 ${state.results.length} 题会保存并计入掌握度，` +
      `未答的 ${state.questions.length - state.results.length} 题不再出现。`);
    if (!ok) return;
    // inProgress 保持 true：交卷失败时关页提醒仍在；成功后 showResult 置 false
    finishSession();
  });
  $("home-btn").addEventListener("click", () => {
    state.inProgress = false;  // 主动返回：放弃本组，不再提醒
    backToModule();
  });
  $("flip-btn").addEventListener("click", flipCard);
  $("know-btn").addEventListener("click", () => judgeCard(true));
  $("dontknow-btn").addEventListener("click", () => judgeCard(false));

  // 学习模块（入口在「课程与新词」方块里）
  $("learn-prev").addEventListener("click", learnPrev);
  $("learn-next").addEventListener("click", learnNext);
  $("learn-play").addEventListener("click", () => {
    const item = learn.words[learn.index];
    if (item) playTTS(`/api/tts?ref=${encodeURIComponent(item.ref)}`);
  });
  $("learn-regen-btn").addEventListener("click", () => regenCurrentAudio("learn"));
  $("learn-quit-btn").addEventListener("click", () => {
    if (confirm("放弃本批学习？学习进度只有在完成小测后才会保存。")) {
      backToModule();
    }
  });
  $("learn-quiz-opts").addEventListener("click", (e) => {
    const btn = e.target.closest(".exam-opt");
    if (btn) answerLearnQuiz(btn);
  });
  $("learn-practice-btn").addEventListener("click", practiceLearned);
  $("learn-home-btn").addEventListener("click", backToModule);

  // 汉字识字卡（入口在「课程与新词」方块里）
  $("k-prev").addEventListener("click", kanjiPrev);
  $("k-next").addEventListener("click", kanjiNext);
  $("k-back-btn").addEventListener("click", backToModule);
  $("k-play").addEventListener("click", () => {
    const k = kstate.list[kstate.index];
    if (k && k.words.length) playTTS(`/api/tts?ref=${encodeURIComponent(k.words[0].ref)}`);
  });
  $("k-regen-btn").addEventListener("click", () => regenCurrentAudio("kanji"));
  // 动词活用卡（入口同在「课程与新词」方块）：上一张/下一张/返回/读基本形
  $("c-prev").addEventListener("click", conjPrev);
  $("c-next").addEventListener("click", conjNext);
  $("c-back-btn").addEventListener("click", backToModule);
  $("c-play").addEventListener("click", playConjBasic);
  // 新增单词模态框（入口在「课程与新词」方块里）
  $("add-word-close").addEventListener("click", closeAddWord);
  $("aw-lesson").addEventListener("change", toggleAddWordNewLesson);
  $("aw-save-more").addEventListener("click", () => submitAddWord(false));
  $("aw-save-close").addEventListener("click", () => submitAddWord(true));
  // 写法/读音变化时节流搜相近词：词库里可能已有（如 中国 / 中国語），
  // 提前提示，可选「改已有词」或照常新增
  $("aw-kanji").addEventListener("input", scheduleSimilarSearch);
  $("aw-hiragana").addEventListener("input", scheduleSimilarSearch);
  $("aw-similar").addEventListener("click", (e) => {
    const btn = e.target.closest(".aw-sim-use");
    if (!btn) return;
    const it = addWord.similar.find((x) => x.ref === btn.dataset.ref);
    if (it) bindWordToEdit(it);
  });
  $("aw-unbind").addEventListener("click", () => unbindAddWord(false));
  // 重命名课程弹窗（入口：「课程与新词」方块的子项 + 配置页课程范围旁的「✏️ 改名」）
  $("rename-lesson-btn").addEventListener("click", () => openRenameLesson($("lesson").value));
  $("rename-close").addEventListener("click", closeRenameLesson);
  $("rn-cancel").addEventListener("click", closeRenameLesson);
  $("rn-lesson").addEventListener("change", syncRenameTitle);
  $("rn-save").addEventListener("click", submitRename);
  $("rename-modal").addEventListener("click", (e) => {
    if (e.target === $("rename-modal")) closeRenameLesson();
  });
  // 弹窗内 Enter=保存、Esc=关闭；截获冒泡避免触发全局快捷键（如练习页的「我不会」）
  $("rename-modal").addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      submitRename();
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      closeRenameLesson();
    }
  });
  // 点遮罩空白处关闭（点卡片内部不关闭）
  $("add-word-modal").addEventListener("click", (e) => {
    if (e.target === $("add-word-modal")) closeAddWord();
  });
  // 模态框内 Enter=保存并继续（连续录入提速），Esc=关闭；截获冒泡避免触发全局快捷键
  $("add-word-modal").addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      submitAddWord(false);
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      closeAddWord();
    }
  });
  // 读音框：罗马字实时转平假名（与答题输入同一套规则，IME 组合态不动）
  const awHira = $("aw-hiragana");
  awHira.addEventListener("input", (e) => {
    if (e.isComposing) return;
    const converted = romajiToKana(awHira.value);
    if (converted !== awHira.value) {
      const start = awHira.selectionStart;
      const end = awHira.selectionEnd;
      awHira.value = converted;
      try { awHira.setSelectionRange(start, end); } catch (err) { /* 不支持时忽略 */ }
    }
  });

  // 练习进行中（已作答但未到 /api/finish）关闭/刷新页面时弹原生确认，
  // 防止误关丢掉整组；已交卷或主动回首页时 inProgress 已置 false，不打扰。
  window.addEventListener("beforeunload", (e) => {
    if (state.inProgress && state.results.length > 0) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  // 例句试听按钮 + 真题选项按钮（事件委托）
  document.addEventListener("click", (e) => {
    // 汉字卡的 🔊 读的是词条本身（例句按钮读的是例句）
    const kwBtn = e.target.closest(".k-audio-btn");
    if (kwBtn && kwBtn.dataset.ref) {
      playTTS(`/api/tts?ref=${encodeURIComponent(kwBtn.dataset.ref)}`);
      return;
    }
    const conjBtn = e.target.closest(".conj-audio-btn");
    if (conjBtn && conjBtn.dataset.text) {
      // 变形串不在词表里（開いて）：走整句朗读接口（内容哈希缓存）
      playTTS(`/api/tts/text?text=${encodeURIComponent(conjBtn.dataset.text)}`);
      return;
    }
    const exBtn = e.target.closest(".ex-audio-btn");
    if (exBtn && exBtn.dataset.ref) {
      playExampleAudio(exBtn.dataset.ref);
      return;
    }
    const opt = e.target.closest(".exam-opt");
    // 仅练习屏的选项走判分；学习小测的选项由 #learn-quiz-opts 自己的委托处理
    if (opt && $("practice-screen").style.display !== "none") {
      const idx = Number(opt.dataset.idx);
      playOptionAudio(idx);   // 选项是发音时先播（其余题型 option_audio 为空，无副作用）
      // 已作答：选项只当播放器用（对照四个发音），不再判分
      if (opt.classList.contains("answered")) return;
      if (state.config && state.config.mode === "course") {
        courseAnswerChoice(idx);
      } else {
        submitExam(idx);
      }
    }
  });

  // 罗马字→假名实时转换：英文键盘直接打 norikae → のりかえ，无需日文输入法。
  // 直接输入/粘贴的假名不受影响（非罗马字字符原样保留）。
  const answerInput = $("answer-input");
  answerInput.addEventListener("input", (e) => {
    // 写汉字模式不转换；输入法正在组合时也不能改写 value，
    // 否则会打断候选词选择（用输入法直接打假名是允许的路径）
    if (rawInputFor(currentQ()) || e.isComposing) return;
    const converted = romajiToKana(answerInput.value);
    if (converted !== answerInput.value) {
      // 赋值会把光标拽到末尾：先存后恢复，用户回头改中间假名时不再跳位
      const start = answerInput.selectionStart;
      const end = answerInput.selectionEnd;
      answerInput.value = converted;
      try { answerInput.setSelectionRange(start, end); } catch (err) { /* 输入框类型不支持时忽略 */ }
    }
  });
  $("play-audio-btn").addEventListener("click", () => {
    const q = currentQ();  // 课程模式下 state.index 恒为 0，必须用 currentQ() 取当前题
    if (q) playQuestionAudio(q.ref);
  });
  // 不能直接传函数引用：click 事件对象会被当成 which 参数
  $("regen-audio-btn").addEventListener("click", () => regenCurrentAudio("practice"));

  // 重播发音：F2 或 Ctrl+R（可用性随模式：无音频/剧透未作答时忽略，同播放按钮）。
  // 选这两个键的原因：普通键会被日语 IME 拦截（空格/方向键用于候选转换），
  // 而 F 键在不少笔记本上默认是媒体键（需 Fn+F2 才是 F2），
  // Ctrl 组合键既不进输入法组合，也不受媒体键映射影响，作为备用最稳。
  document.addEventListener("keydown", (e) => {
    const isF2 = e.key === "F2";
    const isCtrlR = e.ctrlKey && !e.shiftKey && !e.altKey && (e.key === "r" || e.key === "R");
    if (!isF2 && !isCtrlR) return;
    e.preventDefault();  // 阻止 Ctrl+R 的浏览器刷新
    if ($("practice-screen").style.display !== "none") {
      const q = currentQ();
      // 与播放按钮同一套规则：无音频的模式/未作答的剧透模式不发无意义请求
      const answered = $("next-btn").style.display !== "none";
      if (q && canPlayQuestionAudio(q, answered)) playQuestionAudio(q.ref);
      $("answer-input").focus();
    } else if ($("learn-screen").style.display !== "none" &&
               !$("learn-cards").classList.contains("hidden")) {
      // 学习卡片阶段：重读当前词
      const item = learn.words[learn.index];
      if (item) playTTS(`/api/tts?ref=${encodeURIComponent(item.ref)}`);
    } else if ($("kanji-screen").style.display !== "none") {
      // 汉字卡：重读当前字的第一个词
      const k = kstate.list[kstate.index];
      if (k && k.words.length) playTTS(`/api/tts?ref=${encodeURIComponent(k.words[0].ref)}`);
    } else if ($("conj-screen").style.display !== "none") {
      // 活用卡：重读当前词的基本形（多邻国词条 ref 音频是ます形，见 playConjBasic）
      playConjBasic();
    }
  });

  // Enter 键：练习页未作答时提交/已揭示答案时进入下一题；结果页练一组
  // consumeEnter()：本监听真正吃掉这次 Enter 时用。除了 preventDefault 还要
  // stopImmediatePropagation，否则后面「选择题 Enter 确认」那个监听会在同一个事件里
  // 接着跑——已作答时本题 Enter 已经渲染出新题、examAnswered 复位，选择题分支就会
  // 拿游标替新题直接判答案（看图选汉字等四选一模式：回车想翻页却被自动选了 A）。
  const consumeEnter = (e) => {
    e.preventDefault();
    e.stopImmediatePropagation();
  };
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    // 输入法组合态的 Enter 是「确认候选词」，不能当成提交
    //（写汉字等 rawInput 模式主路径，否则会提交半截答案）
    if (e.isComposing || e.keyCode === 229) return;
    if ($("result-screen").style.display !== "none") {
      e.preventDefault();
      startSession();
      return;
    }
    if ($("learn-screen").style.display !== "none") {
      // 学习卡片阶段：Enter = 认识下一个（小测阶段用点击/数字键）
      if (!$("learn-cards").classList.contains("hidden")) {
        e.preventDefault();
        learnNext();
      }
      return;
    }
    if ($("kanji-screen").style.display !== "none") {
      // 汉字卡：Enter 下一个（最后一张时按钮已禁用，函数内 no-op）
      e.preventDefault();
      kanjiNext();
      return;
    }
    if ($("conj-screen").style.display !== "none") {
      // 活用卡：Enter 下一个（最后一张时按钮已禁用，函数内 no-op）
      e.preventDefault();
      conjNext();
      return;
    }
    if ($("practice-screen").style.display === "none") return;
    // 闪卡：Enter 只用于翻面；翻面后不动作（判断用 1/2，避免误触）
    if (state.config && state.config.mode === "flashcard") {
      if (!state.cardFlipped) {
        e.preventDefault();
        flipCard();
      }
      return;
    }
    // 课程：预习卡推进 → 答题 → 回炉 → 交卷（next-btn 可见时一律 courseNext）
    // 课程里的选择题未作答时不消费 Enter，留给下面的选择题监听去确认选项
    if (state.config && state.config.mode === "course") {
      e.preventDefault();
      if (course.phase === "intro") {
        consumeEnter(e);
        courseIntroNext();
      } else if ($("next-btn").style.display !== "none") {
        consumeEnter(e);
        courseNext();
      } else if (course.q) {
        if (uiOf(course.q).order === true) {
          if (!$("order-check").disabled) {
            consumeEnter(e);
            checkOrderAnswer();
          }
        } else if (uiOf(course.q).typing) {
          consumeEnter(e);
          courseSubmitTyping();
        }
      }
      return;
    }
    const nextBtn = $("next-btn");
    if (nextBtn.style.display !== "none") {
      consumeEnter(e);
      nextQuestion();
    } else if (uiOf(currentQ()).order === true) {
      // 独立词块题（组句排序 / 例句听写）：Enter 触发「检查」
      // （打字框是隐藏的，不会走进下面的输入框分支）
      consumeEnter(e);
      checkOrderAnswer();
    } else if (document.activeElement === $("answer-input")) {
      consumeEnter(e);
      submitAnswer();
    }
  });

  // 闪卡：空格翻面（无输入框时不会与 IME/滚动冲突）
  document.addEventListener("keydown", (e) => {
    if (e.key !== " ") return;
    if ($("practice-screen").style.display === "none") return;
    if (!state.config || state.config.mode !== "flashcard") return;
    if (state.cardFlipped) return;
    e.preventDefault();
    flipCard();
  });

  // 闪卡自评：1 = 不认识，2 = 认识（无输入框，普通按键不会被 IME 拦截）
  document.addEventListener("keydown", (e) => {
    if ($("practice-screen").style.display === "none") return;
    if (!state.config || state.config.mode !== "flashcard" || !state.cardFlipped) return;
    if (e.key === "1") {
      e.preventDefault();
      judgeCard(false);
    } else if (e.key === "2") {
      e.preventDefault();
      judgeCard(true);
    }
  });

  // 首页方块与模块页子项：方向键在方块之间移动焦点，Enter 打开。
  // 两者都是 <button>，Enter/Space 本来就管用，缺的是「移动」——原先按方向键
  // 整个页面上下滚。落点按方块在屏幕上的实际几何位置算，所以列数随窗口变也不用改。
  // 还没选中方块时，第一次按 ↓/→ 就落在第一个方块上（不然方向键永远只是滚页面）。
  document.addEventListener("keydown", (e) => {
    const dir = { ArrowLeft: [-1, 0], ArrowRight: [1, 0],
                  ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
    if (!dir || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    const items = [...document.querySelectorAll(".tile, .sub-item")].filter((el) => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;   // 隐藏屏里的方块尺寸为 0，天然被排除
    });
    if (!items.length) return;              // 练习页/配置页各有自己的键盘处理
    const cur = items.includes(document.activeElement) ? document.activeElement : null;
    if (!cur) {
      if (dir[0] < 0 || dir[1] < 0) return; // 往上的第一下让页面照常滚，别抢
      e.preventDefault();
      items[0].focus();
      return;
    }
    const next = arrowNeighbor(items, cur, dir[0], dir[1]);
    if (!next) return;                      // 这个方向没有方块了：交回浏览器滚动
    e.preventDefault();
    next.focus();
  });

  // 方向键的下一个落点：先在同一行/列里挑正对着的邻居（垂直偏移不超过半个方块），
  // 没有再放宽到整个半平面——一行末尾按 → 落到下一行第一个，像文本光标那样。
  function arrowNeighbor(items, cur, dx, dy) {
    const c = cur.getBoundingClientRect();
    const cx = c.left + c.width / 2;
    const cy = c.top + c.height / 2;
    let near = null;
    let far = null;
    for (const el of items) {
      if (el === cur) continue;
      const r = el.getBoundingClientRect();
      const x = r.left + r.width / 2;
      const y = r.top + r.height / 2;
      const along = dx ? (x - cx) * dx : (y - cy) * dy;
      if (along < 1) continue;              // 不在这个方向上
      const across = dx ? Math.abs(y - cy) : Math.abs(x - cx);
      const score = along + across * 2;
      if (across <= (dx ? c.height : c.width) / 2) {
        if (!near || score < near.score) near = { el, score };
      } else if (!far || score < far.score) far = { el, score };
    }
    const best = near || far;
    return best ? best.el : null;
  }

  // 选择题（真题 + MCQ 模式 + 课程内选择题）：↑/↓ 移动高亮、Enter 确认——
  // 小键盘 1234 是 L 形排布，与屏幕选项的上下顺序对不上，方向键更合直觉；
  // 数字键 1-4 保留为直选快捷键
  document.addEventListener("keydown", (e) => {
    if ($("practice-screen").style.display === "none") return;
    if (!state.config || state.examAnswered) return;
    const m = state.config.mode;
    const q = m === "course" ? course.q : state.questions[state.index];
    if (!q || !q.options) return;  // 四选一 UI 才有游标（真题与矩阵里的选xx题型）
    const n = document.querySelectorAll("#exam-controls .exam-opt").length;
    if (!n) return;   // 选项还没渲染（正常轮不到这里）：别让下面的取模变成 NaN
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      // 首尾相接：末项再按 ↓ 绕回第一项、首项再按 ↑ 绕到末项。四选一只有 4 个选项，
      // 「按到底就没反应、得反着按回来」比绕一圈更别扭
      const step = e.key === "ArrowDown" ? 1 : -1;
      state.choiceCursor = (state.choiceCursor + step + n) % n;
      paintChoiceCursor(state.choiceCursor);
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      playOptionAudio(state.choiceCursor);
      if (m === "course") courseAnswerChoice(state.choiceCursor);
      else submitExam(state.choiceCursor);
      return;
    }
    const idx = ["1", "2", "3", "4"].indexOf(e.key);
    if (idx >= 0) {
      e.preventDefault();
      state.choiceCursor = idx;
      playOptionAudio(idx);
      if (m === "course") courseAnswerChoice(idx);
      else submitExam(idx);
    }
  });

  // 学习小测：↑/↓ 移动高亮、Enter 确认，数字键 1-4 保留直选
  document.addEventListener("keydown", (e) => {
    if ($("learn-screen").style.display === "none") return;
    if ($("learn-quiz").classList.contains("hidden") || learn.quizLocked) return;
    const btns = document.querySelectorAll("#learn-quiz-opts .exam-opt");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      // 与选择题同一口径：首尾相接（小测也是 4 个选项，按到末项不该没反应）
      const step = e.key === "ArrowDown" ? 1 : -1;
      learn.quizCursor = (learn.quizCursor + step + btns.length) % btns.length;
      paintLearnCursor(learn.quizCursor);
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      if (btns[learn.quizCursor]) answerLearnQuiz(btns[learn.quizCursor]);
      return;
    }
    const idx = ["1", "2", "3", "4"].indexOf(e.key);
    if (idx >= 0 && btns[idx]) {
      e.preventDefault();
      learn.quizCursor = idx;
      answerLearnQuiz(btns[idx]);
    }
  });

  // Escape 键：导航页回上一级；练习页触发"我不会"；结果页返回；闪卡模式无打字，不响应
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (e.isComposing || e.keyCode === 229) return;  // 组合态 Esc 是取消候选，不跳题
    // 内容页：Esc 一律回上一级（录入弹窗自己截获了 Esc，不会走到这里）
    if ($("docs-screen").style.display !== "none") {
      e.preventDefault();
      backToModule();
      return;
    }
    if ($("config-screen").style.display !== "none") {
      e.preventDefault();
      backToModule();
      return;
    }
    if ($("module-screen").style.display !== "none") {
      e.preventDefault();
      showScreen("home-screen");
      return;
    }
    if ($("result-screen").style.display !== "none") {
      e.preventDefault();
      backToModule();
      return;
    }
    if ($("learn-screen").style.display !== "none") {
      e.preventDefault();
      if (confirm("放弃本批学习？学习进度只有在完成小测后才会保存。")) {
        backToModule();
      }
      return;
    }
    if ($("kanji-screen").style.display !== "none") {
      e.preventDefault();
      backToModule();
      return;
    }
    if ($("conj-screen").style.display !== "none") {
      e.preventDefault();
      backToModule();
      return;
    }
    if (state.config && state.config.mode === "course") {
      // 课程：预习阶段 Esc 弃课（无已保存内容）；打字题未答时 Esc = 我不会（计错+回炉）
      e.preventDefault();
      if (course.phase === "intro") {
        backToModule();
        return;
      }
      if (state.examAnswered || !course.q) return;
      // 打字题（含听音写假名）未答时 Esc = 我不会（计错 + 课末回炉）
      if (uiOf(course.q).typing) {
        courseCheck(course.q, "", true);
      }
      return;
    }
    if ($("practice-screen").style.display === "none") return;
    // 词义配对没有「我不会」：Esc 不动作（配错本身就计失误）
    if (state.config && state.config.mode === "pair_match") return;
    if (state.config && state.config.mode === "flashcard") return;
    const nextBtn = $("next-btn");
    if (nextBtn.style.display !== "none") return;  // 已揭示答案时 Escape 无效
    e.preventDefault();
    skipAnswer();
  });
});
