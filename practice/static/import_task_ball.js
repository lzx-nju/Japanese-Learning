/* 服务器 ASR 导入任务 · 全局悬浮球
 *
 * 「导入作品」选服务器引擎后任务在后台跑，用户可以去练单词。页面右下角
 * 出现一个小球（脉冲圆点 + 活跃任务数），点开看每项任务的详细 info：
 * 阶段 / 引擎 / GPU / 进度条 / 消息 / 更新时间；完成变绿、失败变红并显示
 * 原因（可点「去阅读」直接进作品）。
 *
 * 每 5s 轮询 /api/works/import/tasks（最近活跃任务列表）；没有任何任务时
 * 自动隐藏，不打扰。
 *
 * 关掉浏览器（Flask 服务还开着）任务照跑：worker 线程在服务进程里，状态
 * 落盘 data/import_tasks/，重开页面小球自动恢复跟踪；Flask 整个重启也没
 * 关系——服务器转写本身是远端 nohup，重启后服务端扫描恢复未完成任务。
 */
(function () {
  "use strict";
  if (window.__importTaskBallLoaded) return;
  window.__importTaskBallLoaded = true;

  var PHASES = ["collecting", "uploading", "transcribing", "pulling",
                "aligning", "importing", "done"];
  var PHASE_CN = {
    collecting: "准备中", uploading: "上传音轨到服务器",
    transcribing: "服务器转写中", pulling: "拉回转写结果",
    aligning: "组装时间轴并导入", importing: "写入作品",
    done: "完成", error: "失败"
  };

  var css = "#task-ball-root{position:fixed;right:18px;bottom:18px;z-index:9999;" +
    "font-family:inherit}#task-ball-root:not(.tb-active){display:none}" +
    "#tb-ball{width:52px;height:52px;border-radius:50%;background:var(--primary,#7c5cff);" +
    "color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;" +
    "box-shadow:0 4px 14px rgba(0,0,0,.28);position:relative;transition:transform .15s}" +
    "#tb-ball:hover{transform:scale(1.06)}" +
    "#tb-ball .tb-dot{position:absolute;top:6px;right:6px;width:10px;height:10px;" +
    "border-radius:50%;background:#ef4444;border:2px solid #fff;" +
    "animation:tb-pulse 1.6s infinite}" +
    "@keyframes tb-pulse{0%{box-shadow:0 0 0 0 rgba(239,68,68,.55)}" +
    "70%{box-shadow:0 0 0 9px rgba(239,68,68,0)}100%{box-shadow:0 0 0 0 rgba(239,68,68,0)}}" +
    "#tb-count{font-size:1rem;font-weight:700}" +
    "#tb-panel{position:absolute;right:0;bottom:62px;width:330px;max-height:70vh;overflow:auto;" +
    "background:var(--bg-card,#fff);color:var(--text,#222);border:1px solid var(--border,#e5e7eb);" +
    "border-radius:14px;box-shadow:0 10px 34px rgba(0,0,0,.22);padding:10px 12px}" +
    "#tb-panel[hidden]{display:none}" +
    ".tb-head{display:flex;align-items:center;justify-content:space-between;" +
    "font-weight:700;font-size:.92rem;margin-bottom:8px}" +
    ".tb-close{border:0;background:transparent;font-size:1.15rem;cursor:pointer;" +
    "color:var(--muted,#888);line-height:1}" +
    ".tb-empty{color:var(--muted,#888);font-size:.85rem;padding:14px 0;text-align:center}" +
    ".tb-card{border:1px solid var(--border,#eee);border-radius:10px;padding:8px 10px;" +
    "margin-bottom:8px;background:var(--bg,#fafafa)}" +
    ".tb-card.ok{border-color:#16a34a;background:#f0fdf4}" +
    ".tb-card.err{border-color:#ef4444;background:#fef2f2}" +
    ".tb-row{display:flex;justify-content:space-between;gap:8px;align-items:baseline}" +
    ".tb-title{font-size:.88rem;font-weight:600;overflow:hidden;text-overflow:ellipsis;" +
    "white-space:nowrap}" +
    ".tb-phase{font-size:.72rem;padding:1px 7px;border-radius:999px;background:#eef2ff;" +
    "color:#4f46e5;flex-shrink:0}" +
    ".tb-card.ok .tb-phase{background:#dcfce7;color:#15803d}" +
    ".tb-card.err .tb-phase{background:#fee2e2;color:#b91c1c}" +
    ".tb-sub{font-size:.72rem;color:var(--muted,#888);margin-top:3px}" +
    ".tb-bar{height:5px;border-radius:4px;background:var(--border,#eee);margin:6px 0 4px}" +
    ".tb-bar i{display:block;height:100%;border-radius:4px;background:var(--primary,#7c5cff)}" +
    ".tb-card.ok .tb-bar i{background:#16a34a}" +
    ".tb-card.err .tb-bar i{background:#ef4444}" +
    ".tb-msg{font-size:.78rem;color:var(--muted,#777)}" +
    ".tb-err{font-size:.78rem;color:#b91c1c;margin-top:4px;border-top:1px dashed #fca5a5;" +
    "padding-top:4px}" +
    ".tb-done{font-size:.78rem;color:#15803d;margin-top:4px}" +
    ".tb-done a{color:#15803d;font-weight:600}";
  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  var root = document.createElement("div");
  root.id = "task-ball-root";
  root.innerHTML =
    '<div id="tb-ball" role="button" tabindex="0" title="查看服务器导入任务进度">' +
    '<span class="tb-dot"></span><span id="tb-count">0</span></div>' +
    '<div id="tb-panel" hidden><div class="tb-head">' +
    '<span>服务器导入任务</span>' +
    '<button type="button" class="tb-close" title="收起">×</button></div>' +
    '<div id="tb-list"></div></div>';
  document.body.appendChild(root);

  var ball = document.getElementById("tb-ball");
  var panel = document.getElementById("tb-panel");
  var countEl = document.getElementById("tb-count");
  var listEl = document.getElementById("tb-list");
  var open = false;

  ball.addEventListener("click", toggle);
  ball.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  panel.querySelector(".tb-close").addEventListener("click", function () {
    open = false; render(last || []);
  });

  function toggle() { open = !open; render(last || []); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function relTime(ts) {
    if (!ts) return "";
    var s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 5) return "刚刚";
    if (s < 60) return s + " 秒前";
    var m = Math.round(s / 60);
    if (m < 60) return m + " 分钟前";
    var h = Math.round(m / 60);
    if (h < 24) return h + " 小时前";
    return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
  }

  function phaseIdx(p) {
    var i = PHASES.indexOf(p);
    return i < 0 ? 0 : i;
  }

  function card(t) {
    var pct = (t.phase === "done" || t.phase === "error")
      ? 100 : phaseIdx(t.phase) / (PHASES.length - 1) * 100;
    var cls = t.phase === "error" ? "tb-card err"
      : t.phase === "done" ? "tb-card ok" : "tb-card";
    var extra = "";
    if (t.phase === "error") {
      extra = '<div class="tb-err">' + esc(t.error || t.msg) + "</div>";
    } else if (t.phase === "done") {
      extra = '<div class="tb-done">已导入《' + esc(t.title) + "》" +
        (t.chapters || 0) + " 章 · " + (t.sentences || 0) + " 句";
      if (t.work_id) {
        extra += ' · <a href="/reader?work=' + encodeURIComponent(t.work_id) +
          '">去阅读</a>';
      }
      extra += "</div>";
    }
    return '<div class="' + cls + '">' +
      '<div class="tb-row"><span class="tb-title">' +
      esc(t.title || "（未命名作品）") + '</span>' +
      '<span class="tb-phase">' + esc(PHASE_CN[t.phase] || t.phase) + "</span></div>" +
      '<div class="tb-sub">' + esc(t.engine || "") +
      (t.gpu ? " · GPU " + esc(t.gpu) : "") + " · " + relTime(t.updated) + "</div>" +
      '<div class="tb-bar"><i style="width:' + pct.toFixed(0) + '%"></i></div>' +
      '<div class="tb-msg">' + esc(t.msg || "") + "</div>" + extra + "</div>";
  }

  function render(tasks) {
    var active = (tasks || []).length;
    root.classList.toggle("tb-active", active > 0);
    countEl.textContent = active;
    if (!open) { panel.hidden = true; return; }
    panel.hidden = false;
    if (!active) {
      listEl.innerHTML = '<div class="tb-empty">暂无服务器导入任务</div>';
      return;
    }
    listEl.innerHTML = tasks.map(card).join("");
  }

  var inflight = false;
  var last = null;
  async function poll() {
    if (inflight) return;
    inflight = true;
    try {
      var res = await fetch("/api/works/import/tasks", { cache: "no-store" });
      if (!res.ok) throw new Error("http " + res.status);
      var data = await res.json();
      last = data.tasks || [];
    } catch (e) {
      // 服务没起来 / 网络抖动：保留上一次结果，下轮再试
    }
    inflight = false;
    render(last || []);
  }
  poll();
  setInterval(poll, 5000);
})();