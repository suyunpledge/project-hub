"use strict";
// Project Hub front end — vanilla JS, no build step.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const h = (tag, attrs = {}, html = "") => { const e = document.createElement(tag); Object.assign(e, attrs); if (html) e.innerHTML = html; return e; };

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Hub": "1" }, body: JSON.stringify(body) };
  const r = await fetch("/api/" + path, opt);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
const qs = (o) => Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");

function toast(msg, err = false) {
  const d = h("div", { className: err ? "err" : "", textContent: msg });
  $("#toast").append(d);
  setTimeout(() => d.remove(), err ? 6000 : 3000);
}
const guard = (fn) => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, true); } };

const SEL = new Set();  // sidebar multi-selection (project ids)
let lastSelected = null;
const S = {
  reg: { groups: [], projects: [] }, cfg: {}, pid: null, proj: null, tab: "source",
  files: [], fileRef: "", openFile: null, openLine: null, editing: false, collapsed: new Set(JSON.parse(localStorage.hubCollapsed || "[]")),
  expanded: {}, unchecked: new Set(), activity: null, problems: [], chgSel: null,
};

/* ------------------------------------------------------------ visual helpers */

const ico = (name, cls = "") => `<svg class="ico ${cls}"><use href="#i-${name}"/></svg>`;
const hue = (s) => { let x = 0; for (const c of String(s)) x = (x * 31 + c.codePointAt(0)) >>> 0; return x % 360; };
const initial = (name) => { const m = String(name).match(/[\p{L}\p{N}]/u); return m ? m[0].toUpperCase() : "?"; };
const avatar = (p, big = false) => `<span class="avatar${big ? " big" : ""}" style="--h:${hue(p.id)}">${esc(initial(p.name))}</span>`;
const tagChip = (t, small = false) => `<span class="tag${small ? " sm" : ""}" data-tag="${esc(t)}" style="--h:${hue(t)}" title="筛选标签 ${esc(t)}">${esc(t)}</span>`;
const groupOf = (gid) => S.reg.groups.find((g) => g.id === gid) || { name: gid, color: "#64748b" };
const KIND_COLORS = { python: "#facc15", node: "#4ade80", web: "#fb923c", git: "#f87171", gradle: "#2dd4bf", rust: "#f97316", go: "#22d3ee" };
const kindChip = (k) => `<span class="kind" style="--kc:${KIND_COLORS[k] || "#94a3b8"}">${esc(k)}</span>`;
// file-type badge: [label, colour]
const FILE_TYPES = {
  py: ["PY", "#facc15"], pyw: ["PY", "#facc15"], js: ["JS", "#fde047"], mjs: ["JS", "#fde047"], cjs: ["JS", "#fde047"],
  ts: ["TS", "#60a5fa"], tsx: ["TX", "#38bdf8"], jsx: ["JX", "#38bdf8"], json: ["{}", "#a3e635"], html: ["<>", "#fb923c"],
  htm: ["<>", "#fb923c"], css: ["#", "#818cf8"], scss: ["#", "#f472b6"], md: ["M↓", "#cbd5e1"], txt: ["TX", "#94a3b8"],
  kt: ["KT", "#a78bfa"], kts: ["KT", "#a78bfa"], java: ["JV", "#f87171"], gradle: ["GR", "#2dd4bf"], xml: ["<>", "#fdba74"],
  rs: ["RS", "#fb923c"], go: ["GO", "#22d3ee"], c: ["C", "#93c5fd"], h: ["H", "#93c5fd"], cpp: ["C+", "#60a5fa"],
  cs: ["C#", "#a78bfa"], sh: ["$", "#4ade80"], bat: ["$", "#4ade80"], cmd: ["$", "#4ade80"], ps1: ["PS", "#60a5fa"],
  toml: ["TM", "#d6d3d1"], yml: ["YM", "#f9a8d4"], yaml: ["YM", "#f9a8d4"], ini: ["IN", "#d6d3d1"], svg: ["SV", "#fbbf24"],
  png: ["IM", "#c084fc"], jpg: ["IM", "#c084fc"], jpeg: ["IM", "#c084fc"], gif: ["IM", "#c084fc"], ico: ["IM", "#c084fc"],
  lock: ["LK", "#78716c"], gitignore: ["GI", "#f87171"],
};
const fileIcon = (name) => {
  const ext = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
  const [label, color] = FILE_TYPES[ext] || [ext.slice(0, 2).toUpperCase() || "··", "#64748b"];
  return `<span class="ficon" style="--fc:${color}">${esc(label)}</span>`;
};

const PLACEHOLDER = `<div class="placeholder"><div>${ico("code")}<div>从左侧选择文件查看源码</div><div class="hint">可切换查看工作区或任一分支的版本</div></div></div>`;

/* ------------------------------------------------------------ sidebar */

let registryRequest = 0, appliedRegistry = 0;
async function loadRegistry() {
  const request = ++registryRequest;
  const reg = await api("registry");
  if (request < appliedRegistry) return;
  appliedRegistry = request;
  if (JSON.stringify(S.reg) !== JSON.stringify(reg)) {
    S.reg = reg;
    renderSidebar();
    if (S.proj) renderProjectGroup();
  }
  showLanding();
}

function renderSidebar() {
  const q = $("#search").value.trim().toLowerCase();
  const nav = $("#groups");
  const words = q.split(/\s+/).filter(Boolean);
  const hay = (p) => [p.name, p.path, p.description || "", ...(p.tags || [])].join("\n").toLowerCase();
  const matching = S.reg.projects.filter((p) => words.every((w) => hay(p).includes(w)));
  nav.innerHTML = "";
  for (const g of S.reg.groups) {
    const ps = matching.filter((p) => p.group === g.id);
    if (q && !ps.length) continue;  // while searching, only groups with hits
    if (g.id === "default" && !ps.length) continue;
    const el = h("div", { className: "group" + (S.collapsed.has(g.id) && !q ? " collapsed" : "") });
    el.style.setProperty("--gc", g.color);
    const head = h("div", { className: "group-head", title: g.description || "" },
      `<svg class="ico chev"><use href="#i-chev"/></svg><span class="dot"></span><span>${esc(g.name)}</span><span class="count">${ps.length}</span>` +
      (g.id !== "default" ? `<span class="gmenu" title="分组设置">⋯</span>` : ""));
    head.onclick = (ev) => {
      if (ev.target.classList.contains("gmenu")) return editGroup(g);
      S.collapsed.has(g.id) ? S.collapsed.delete(g.id) : S.collapsed.add(g.id);
      localStorage.hubCollapsed = JSON.stringify([...S.collapsed]);
      renderSidebar();
    };
    // drop target: drag a project into a group
    el.ondragover = (ev) => { ev.preventDefault(); el.classList.add("drop"); };
    el.ondragleave = () => el.classList.remove("drop");
    el.ondrop = guard(async (ev) => {
      ev.preventDefault(); el.classList.remove("drop");
      const pid = ev.dataTransfer.getData("text/plain");
      const project = S.reg.projects.find((p) => p.id === pid);
      if (!project || project.group === g.id) return;
      try {
        await api(`projects/${pid}/update`, { groupId: g.id });
        S.collapsed.delete(g.id);
        localStorage.hubCollapsed = JSON.stringify([...S.collapsed]);
        toast(`已将「${project.name}」移至「${g.name}」`);
      } finally { await loadRegistry(); }
    });
    const list = h("div", { className: "projects" });
    for (const p of [...ps].sort((x, y) => (y.pinned ? 1 : 0) - (x.pinned ? 1 : 0))) {
      const it = h("button", { type: "button", className: "proj-item" + (p.id === S.pid ? " active" : "") +
        (SEL.has(p.id) ? " selected" : ""), draggable: true, title: p.path + "\nCtrl/Shift+点击可多选" },
        `${avatar(p)}<span class="nm-wrap"><span class="nm">${p.pinned ? '<svg class="ico pin-mark"><use href="#i-star"/></svg>' : ""}${esc(p.name)}</span>` +
        ((p.tags || []).length ? `<span class="tags">${p.tags.slice(0, 2).map((t) => tagChip(t, true)).join("")}` +
          (p.tags.length > 2 ? `<span class="more">+${p.tags.length - 2}</span>` : "") + "</span>" : "") +
        `</span><span class="st">` +
        (p.running ? `<i class="badge-dot run" title="运行中"></i>` : "") +
        (p.watching ? `<i class="badge-dot watch" title="实时检查中"></i>` : "") +
        (!p.exists ? `<i class="badge-dot missing" title="目录不存在"></i>` : "") + "</span>");
      it.dataset.id = p.id;
      it.onclick = guard((ev) => {
        if (ev.ctrlKey || ev.metaKey || ev.shiftKey) { toggleSelect(p.id, ev.shiftKey); return; }
        if (SEL.size) clearSelection();
        return selectProject(p.id);
      });
      it.ondragstart = (ev) => ev.dataTransfer.setData("text/plain", p.id);
      list.append(it);
    }
    el.append(head, list);
    nav.append(el);
  }
  const ps = S.reg.projects;
  if (q && !nav.childElementCount) nav.append(h("div", { className: "note muted", textContent: "没有匹配的项目" }));
  const nGroups = S.reg.groups.filter((g) => g.id !== "default").length;
  $("#side-stats").innerHTML = `<span><b>${ps.length}</b>项目</span><span><b>${nGroups}</b>分组</span>` +
    `<span><b style="color:var(--ok)">${ps.filter((p) => p.running).length}</b>运行中</span>`;
  $("#hero-dash").textContent = ps.length ? `已登记 ${ps.length} 个项目 · 从左侧选择一个开始` : "";
}

/* ------------------------------------------------------------ project */

function canLeaveFile() {
  if (S.saving) return false;
  return !S.editing || $("#editor").value === S.editStart ||
    confirm(`「${S.openFile}」有未保存的修改，放弃修改并继续？`);
}

function resetViewer() {
  S.openFile = null; S.fileContent = null; S.editing = false;
  $("#editor").hidden = true; $("#viewer").hidden = false;
  $("#viewer").innerHTML = PLACEHOLDER; $("#file-name").textContent = ""; $("#file-icon").innerHTML = "";
  ["#btn-edit", "#btn-save", "#btn-file-code"].forEach((x) => ($(x).hidden = true));
}

async function selectProject(pid, keepTab = false) {
  if (pid === S.pid && S.proj) return;
  if (!canLeaveFile()) return;
  S.pid = pid;
  location.hash = pid;
  S.openFile = null; S.fileRef = ""; S.expanded = Object.create(null); S.chgSel = null; S.unchecked = new Set();
  resetViewer();
  S.proj = null; S.files = []; S.problems = [];
  resetActivity(); $("#tree").replaceChildren();
  $("#cm-branch").value = ""; $("#cm-msg").value = ""; $("#src-filter").value = "";
  $("#hist-detail").replaceChildren();
  $("#empty").hidden = true; $("#home").hidden = true; $("#project").hidden = true;
  renderSidebar();
  try { await refreshProject(); }
  catch (e) {
    if (S.pid !== pid) return;
    S.pid = null; location.hash = ""; showLanding(true); renderSidebar();
    throw e;
  }
  if (S.pid !== pid) return;
  $("#project").hidden = false;
  if (!keepTab && !["run", "problems"].includes(S.tab)) switchTab("source");
  pollRun(); pollProblems();
  await loadFiles();
}

function renderProjectGroup() {
  const registered = S.reg.projects.find((p) => p.id === S.pid);
  if (registered) S.proj.group = registered.group;
  const group = groupOf(S.proj.group);
  $("#p-group").textContent = group.name;
  $("#p-group").style.setProperty("--gc", group.color);
}

async function refreshProject() {
  const pid = S.pid;
  const p = await api(`projects/${pid}`);
  if (pid !== S.pid) return;
  S.proj = p;
  $("#p-name").textContent = p.name;
  $("#p-path").textContent = p.path;
  $("#p-avatar").outerHTML = avatar(p, true).replace('class="avatar big"', 'class="avatar big" id="p-avatar"');
  renderProjectGroup();
  $("#p-kinds").innerHTML = p.detect.kinds.filter((k) => k !== "git").map(kindChip).join("");
  const reg = S.reg.projects.find((x) => x.id === p.id) || p;
  $("#p-tags").innerHTML = (reg.tags || p.tags || []).map((t) => tagChip(t)).join("");
  $("#p-desc").textContent = reg.description || p.description || "";
  $("#p-desc").hidden = !$("#p-desc").textContent;
  $("#btn-preview").hidden = !p.preview;
  const gi = p.gitInfo;
  renderStats();
  $("#cnt-changes").textContent = gi.git && gi.changes.length ? gi.changes.length : "";
  const refs = gi.git ? gi.branches.map((b) => b.name) : [];
  $("#src-ref").innerHTML = `<option value="">工作区</option>` + refs.map((r) => `<option value="${esc(r)}">⑂ ${esc(r)}</option>`).join("");
  $("#src-ref").value = S.fileRef;
  $("#hist-ref").innerHTML = refs.map((r) => `<option ${r === gi.branch ? "selected" : ""}>${esc(r)}</option>`).join("");
  $("#branch-list").innerHTML = refs.map((r) => `<option value="${esc(r)}">`).join("");
  if (!$("#run-cmd").dataset.touched || $("#run-cmd").dataset.pid !== S.pid) { $("#run-cmd").value = p.run || ""; $("#run-cmd").dataset.pid = S.pid; delete $("#run-cmd").dataset.touched; }
  if (S.tab === "changes") renderChanges();
}

// four summary cards under the project title
function renderStats() {
  const p = S.proj, gi = p.gitInfo;
  const card = (c, icon, lab, val, sub = "") =>
    `<div class="stat" style="--c:${c}">${ico(icon)}<div><div class="lab">${lab}</div><div class="val">${val}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div></div>`;
  const errs = S.problems.filter((x) => x.severity === "error").length;
  const warns = S.problems.length - errs;
  const live = S.reg.projects.find((x) => x.id === S.pid);
  let html;
  if (gi.git) {
    const opts = gi.branches.map((b) => `<option ${b.name === gi.branch ? "selected" : ""}>${esc(b.name)}</option>`).join("");
    const last = gi.branches.find((b) => b.name === gi.branch);
    // size to the longest branch name (bold text measures short in auto-width selects)
    const longest = Math.max(gi.branch.length, ...gi.branches.map((b) => b.name.length));
    html = card("#6d8cff", "branch", "当前分支", `<select id="branch-switch" title="切换工作区分支" style="width:calc(${(longest * 1.15).toFixed(1)}ch + 36px)">${opts || `<option>${esc(gi.branch)}</option>`}</select>`,
        `${gi.branches.length} 个分支` + (gi.ahead || gi.behind ? ` · ↑${gi.ahead} ↓${gi.behind}` : "")) +
      card(gi.changes.length ? "#fbbf24" : "#34d399", "diff", "未提交改动", gi.changes.length ? `${gi.changes.length} 个文件` : "工作区干净",
        gi.changes.length ? esc(gi.changes.slice(0, 3).map((c) => c.path.split("/").pop()).join("、")) : "") +
      card("#c084fc", "commit", "最新提交", last ? esc(last.subject) : "还没有提交", last ? `<span class="sha">${esc(last.sha)}</span> · ${esc(last.when)}` : "");
  } else {
    html = card("#64748b", "branch", "版本控制", "非 git 仓库", "在「变更」页可初始化") + card("#64748b", "diff", "未提交改动", "—") + card("#64748b", "commit", "最新提交", "—");
  }
  const run = S.activity?.run;
  const status = live?.running ? ["#34d399", "play", "运行中", "运行中", esc(run?.command || p.run || "")]
    : errs ? ["#f87171", "alert", "检查结果", `${errs} 个错误`, warns ? `${warns} 个警告` : ""]
    : run?.failed ? ["#f87171", "alert", "上次运行", `退出码 ${run.code}`, esc(run.command)]
    : warns ? ["#fbbf24", "alert", "检查结果", `${warns} 个警告`, ""]
    : S.activity?.live?.state === "watching" && !S.activity.watchError ? ["#34d399", "check", "检查结果", "没有问题", "实时检查中"]
    : ["#22d3ee", "pulse", "运行 / 检查", p.run ? "就绪" : "未配置命令", esc(p.run || "在「运行」页填写命令")];
  html += card(...status);
  $("#p-stats").innerHTML = html;
}

// delegated, because the stat cards are re-rendered whenever problems/run state change
$("#p-stats").onchange = guard(async (ev) => {
  if (ev.target.id !== "branch-switch") return;
  const gi = S.proj.gitInfo;
  if (!canLeaveFile()) { ev.target.value = gi.branch; return; }
  if (gi.changes.length && !confirm(`工作区有 ${gi.changes.length} 处未提交改动，确定切换到 ${ev.target.value}？（git 会拒绝会覆盖改动的切换）`)) { ev.target.value = gi.branch; return; }
  const pid = S.pid;
  try { await api(`projects/${pid}/checkout`, { branch: ev.target.value }); if (pid === S.pid) resetViewer(); toast(`已切换到 ${ev.target.value}`); }
  finally { await refreshProject(); await loadFiles(); }
});

function switchTab(tab) {
  S.tab = tab;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + tab));
  if (tab === "changes") renderChanges();
  if (tab === "history") loadHistory();
  if (tab === "problems") renderProblems();
}

/* ------------------------------------------------------------ files */

async function loadFiles() {
  const pid = S.pid, ref = S.fileRef;
  const files = await api(`projects/${pid}/files?` + qs({ ref }));
  if (pid !== S.pid || ref !== S.fileRef) return;
  S.files = files;
  renderTree();
}

function buildTree(files) {
  const root = { dirs: Object.create(null), files: [] };
  for (const f of files) {
    const parts = f.split("/");
    let n = root;
    for (let i = 0; i < parts.length - 1; i++) n = n.dirs[parts[i]] ??= { dirs: Object.create(null), files: [] };
    n.files.push(f);
  }
  return root;
}

function renderTree() {
  const q = $("#src-filter").value.trim().toLowerCase();
  const files = q ? S.files.filter((f) => f.toLowerCase().includes(q)) : S.files;
  const marks = Object.create(null);
  if (!S.fileRef && S.proj?.gitInfo.git) for (const c of S.proj.gitInfo.changes) marks[c.path] = c.index === "?" ? "N" : "M";
  for (const p of S.problems) if (p.severity === "error") marks[p.file] = "E";
  const tree = $("#tree");
  tree.innerHTML = "";
  if (!files.length) { tree.innerHTML = `<div class="note muted" style="padding:12px">${q ? "没有匹配的文件" : "没有文件"}</div>`; return; }
  const frag = document.createDocumentFragment();
  let count = 0;
  const walk = (node, prefix, depth) => {
    for (const name of Object.keys(node.dirs).sort((a, b) => a.localeCompare(b))) {
      const path = prefix + name;
      const open = q || S.expanded[path];
      const el = h("div", { className: "tnode dir" }, `<span class="chev">${open ? "▾" : "▸"}</span>${ico("folder")}${esc(name)}`);
      el.style.paddingLeft = 8 + depth * 14 + "px";
      el.onclick = () => { S.expanded[path] = !S.expanded[path]; renderTree(); };
      frag.append(el);
      if (open) walk(node.dirs[name], path + "/", depth + 1);
    }
    for (const f of node.files.sort((a, b) => a.localeCompare(b))) {
      if (++count > 5000) return;
      const name = f.slice(prefix.length);
      const m = marks[f];
      const el = h("div", { className: "tnode" + (f === S.openFile ? " sel" : "") },
        `<span class="chev"></span>${fileIcon(name)}${esc(name)}${m ? `<span class="mark ${m}">${{ M: "M", N: "U", E: "●" }[m]}</span>` : ""}`);
      el.style.paddingLeft = 8 + depth * 14 + "px";
      el.onclick = () => openFile(f);
      frag.append(el);
    }
  };
  walk(buildTree(files), "", 0);
  tree.append(frag);
}

const openFile = guard(async (path, line = null) => {
  if (!canLeaveFile()) return;
  resetViewer();
  const pid = S.pid, ref = S.fileRef;
  S.openFile = path; S.openLine = line;
  // expand parents so the file is visible in the tree
  const parts = path.split("/");
  for (let i = 1; i < parts.length; i++) S.expanded[parts.slice(0, i).join("/")] = true;
  renderTree();
  const f = await api(`projects/${pid}/file?` + qs({ path, ref }));
  if (S.pid !== pid || S.fileRef !== ref || S.openFile !== path || S.editing) return;
  $("#file-name").textContent = (S.fileRef ? `[${S.fileRef}] ` : "") + path;
  $("#file-name").classList.remove("muted");
  $("#file-icon").innerHTML = fileIcon(path.split("/").pop());
  $("#editor").hidden = true; $("#viewer").hidden = false;
  $("#btn-edit").hidden = !!S.fileRef || f.binary || f.tooLarge || f.editable === false;
  $("#btn-save").hidden = true;
  $("#btn-file-code").hidden = !!S.fileRef;
  const v = $("#viewer");
  if (f.binary) { v.innerHTML = `<div class="note">二进制文件（${f.size} 字节）</div>`; return; }
  if (f.tooLarge) { v.innerHTML = `<div class="note">文件过大（${(f.size / 1048576).toFixed(1)} MB），请在 VS Code 中打开</div>`; return; }
  S.fileContent = f.content;
  S.fileVersion = f.version;
  if (!ref && f.editable === false) toast("此文件含混合换行或无法识别的编码，请在 VS Code 中编辑");
  renderCode(v, f.content, path, line);
});

function renderCode(el, text, path, line) {
  const probLines = new Map(S.problems.filter((p) => p.file === path).map((p) => [p.line, p]));
  const lines = text.length < 400000 ? highlight(text, path) : text.split("\n").map(esc);
  el.innerHTML = "<table>" + lines.map((l, i) => {
    const pr = probLines.get(i + 1);
    const cls = (i + 1 === line ? "hl " : "") + (pr ? "perr" : "");
    return `<tr class="${cls}"${pr ? ` title="${esc(pr.message)}"` : ""}><td class="ln">${i + 1}</td><td class="cd">${l || " "}</td></tr>`;
  }).join("") + "</table>";
  if (line) el.querySelectorAll("tr")[line - 1]?.scrollIntoView({ block: "center" });
}

/* tiny syntax highlighter: tokenises the whole text, then splits tokens into lines */
const LANGS = {
  py: { c: /#.*/, s: /"""[\s\S]*?"""|'''[\s\S]*?'''|[rbfu]*"(?:\\.|[^"\\\n])*"|[rbfu]*'(?:\\.|[^'\\\n])*'/, k: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield self match case" },
  js: { c: /\/\/.*|\/\*[\s\S]*?\*\//, s: /`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'/, k: "await break case catch class const continue debugger default delete do else export extends false finally for from function if import in instanceof let new null of return static super switch this throw true try typeof undefined var void while yield async interface type enum implements private public protected readonly" },
  c: { c: /\/\/.*|\/\*[\s\S]*?\*\//, s: /"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'/, k: "auto break case char class const continue default do double else enum extern float for goto if int long return short signed sizeof static struct switch typedef union unsigned void volatile while public private protected fun val var package import object override when is in null true false this new interface namespace using fn let mut impl pub use mod match struct trait func go defer chan" },
  sh: { c: /(?:^|\s)#.*|^::.*|^rem .*/im, s: /"(?:\\.|[^"\\\n])*"|'[^'\n]*'/, k: "if then else fi for do done while case esac function in echo set call goto exit return export local" },
  html: { c: /<!--[\s\S]*?-->/, s: /"[^"]*"|'[^']*'/, t: /<\/?[\w-]+|\/?>/ },
  css: { c: /\/\*[\s\S]*?\*\//, s: /"[^"]*"|'[^']*'/, k: "", a: /[\w-]+(?=\s*:)/ },
  json: { s: /"(?:\\.|[^"\\\n])*"(?=\s*:)|"(?:\\.|[^"\\\n])*"/, k: "true false null" },
  md: { c: /^#{1,6} .*/m, s: /`[^`\n]+`/ },
};
const EXT = { py: "py", pyw: "py", js: "js", mjs: "js", cjs: "js", ts: "js", tsx: "js", jsx: "js", vue: "html", c: "c", h: "c", cpp: "c", hpp: "c", cs: "c", java: "c", kt: "c", kts: "c", go: "c", rs: "c", swift: "c", gradle: "c", sh: "sh", bash: "sh", bat: "sh", cmd: "sh", ps1: "sh", toml: "sh", yaml: "sh", yml: "sh", ini: "sh", html: "html", htm: "html", xml: "html", svg: "html", css: "css", scss: "css", json: "json", md: "md" };

function highlight(text, path) {
  const lang = LANGS[EXT[path.split(".").pop().toLowerCase()]];
  if (!lang) return text.split("\n").map(esc);
  const parts = [];
  const names = [];
  for (const [k, re] of Object.entries(lang)) if (re instanceof RegExp) { parts.push(`(${re.source})`); names.push(k); }
  parts.push(`(\\b\\d[\\d_.xXa-fA-F]*\\b)`); names.push("n");
  const kw = lang.k ? new Set(lang.k.split(" ")) : null;
  if (kw) { parts.push(`(\\b[A-Za-z_]\\w*\\b)`); names.push("w"); }
  const re = new RegExp(parts.join("|"), "gm");
  const lines = [""];
  const push = (cls, s) => {
    s.split("\n").forEach((seg, i) => {
      if (i) lines.push("");
      if (seg) lines[lines.length - 1] += cls ? `<span class="tk-${cls}">${esc(seg)}</span>` : esc(seg);
    });
  };
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m[0] === "") { re.lastIndex++; continue; }
    push("", text.slice(last, m.index));
    const gi = m.slice(1).findIndex((x) => x !== undefined);
    let cls = names[gi];
    if (cls === "w") cls = kw.has(m[0]) ? "k" : "";
    push(cls, m[0]);
    last = re.lastIndex;
  }
  push("", text.slice(last));
  return lines;
}

/* ------------------------------------------------------------ changes / commit */

function renderChanges() {
  const gi = S.proj?.gitInfo;
  const list = $("#chg-list");
  $("#no-git").hidden = !!gi?.git;
  $("#commit-form").hidden = !gi?.git;
  if (!gi?.git) { list.innerHTML = ""; $("#chg-diff").innerHTML = ""; return; }
  list.innerHTML = gi.changes.length ? "" : `<div class="ok-msg">${ico("check")}<b>工作区干净</b><span class="hint">没有未提交的改动</span></div>`;
  if (!S.chgSel || !gi.changes.some((c) => c.path === S.chgSel)) {
    $("#chg-diff").innerHTML = `<div class="placeholder"><div>${ico("diff")}<div>${gi.changes.length ? "点左侧文件查看差异" : "没有可查看的差异"}</div><div class="hint">勾选文件后可提交到当前或任意分支</div></div></div>`;
  }
  for (const c of gi.changes) {
    const row = h("div", { className: "row" + (S.chgSel === c.path ? " sel" : "") },
      `<input type="checkbox" ${S.unchecked.has(c.path) ? "" : "checked"} data-path="${esc(c.path)}">${fileIcon(c.path.split("/").pop())}<span class="path" title="${esc(c.path)}">${esc(c.path)}</span><span class="spacer"></span><span class="lbl ${esc(c.label)}">${esc(c.label)}</span>`);
    row.onclick = (ev) => { if (ev.target.tagName !== "INPUT") showDiff(c.path); };
    const check = row.querySelector("input");
    check.setAttribute("aria-label", c.path);
    check.onchange = () => {
      if (check.checked) S.unchecked.delete(c.path); else S.unchecked.add(c.path);
      syncSelectAll(gi.changes);
    };
    list.append(row);
  }
  $("#cm-branch").placeholder = `当前分支：${gi.branch}`;
  syncSelectAll(gi.changes);
  updateCommitHint();
}

function syncSelectAll(changes) {
  const count = changes.filter((c) => !S.unchecked.has(c.path)).length;
  const toggle = $("#chg-all");
  toggle.checked = count > 0 && count === changes.length;
  toggle.indeterminate = count > 0 && count < changes.length;
  toggle.disabled = !changes.length;
}

function updateCommitHint() {
  const gi = S.proj.gitInfo;
  const b = $("#cm-branch").value.trim();
  const exists = gi.branches.some((x) => x.name === b);
  $("#cm-hint").textContent = !b || b === gi.branch ? `直接提交到当前分支 ${gi.branch}` :
    exists ? `提交到分支 ${b}：在它的最新提交上叠加所选文件的当前内容，不切换工作区` :
      `将从 ${gi.branch} 新建分支 ${b} 并提交，不切换工作区`;
}

const showDiff = guard(async (path) => {
  const pid = S.pid;
  S.chgSel = path;
  $$("#chg-list .row").forEach((r) => r.classList.toggle("sel", r.querySelector("input")?.dataset.path === path));
  const d = await api(`projects/${S.pid}/diff?` + qs({ path }));
  if (pid !== S.pid || S.chgSel !== path) return;
  $("#chg-diff").innerHTML = renderDiff(d.diff) || `<div class="note">没有可显示的差异</div>`;
});

function renderDiff(diff) {
  if (!diff) return "";
  return "<pre>" + diff.split("\n").map((l) => {
    const c = l.startsWith("+++") || l.startsWith("---") || l.startsWith("diff ") || l.startsWith("index ") ? "meta" :
      l.startsWith("@@") ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "";
    return c ? `<span class="${c}">${esc(l)}</span>` : esc(l);
  }).join("\n") + "</pre>";
}

/* ------------------------------------------------------------ history */

const loadHistory = guard(async () => {
  const pid = S.pid, ref = $("#hist-ref").value;
  $("#hist-detail").replaceChildren();
  if (!S.proj?.gitInfo.git) { $("#hist-list").innerHTML = `<div class="note muted" style="padding:12px">非 git 仓库</div>`; return; }
  const log = await api(`projects/${pid}/log?` + qs({ ref: ref || "HEAD" }));
  if (pid !== S.pid || ref !== $("#hist-ref").value) return;
  const list = $("#hist-list");
  list.innerHTML = log.length ? "" : `<div class="note muted" style="padding:12px">还没有提交</div>`;
  for (const c of log) {
    const refs = c.refs ? c.refs.split(", ").map((r) => `<span class="ref-chip${r.startsWith("HEAD") ? " head" : ""}">${esc(r.replace("HEAD -> ", "HEAD → "))}</span>`).join("") : "";
    const row = h("div", { className: "row commit-row" },
      `<div>${esc(c.subject)}</div><div class="sub"><span class="sha">${c.short}</span> · ${esc(c.author)} · ${esc(c.when)}</div>` +
      (refs ? `<div class="refs">${refs}</div>` : ""));
    row.onclick = guard(async () => {
      $$("#hist-list .row").forEach((r) => r.classList.remove("sel")); row.classList.add("sel");
      const d = await api(`projects/${pid}/commit?sha=${c.sha}`);
      if (pid !== S.pid || !row.isConnected || !row.classList.contains("sel")) return;
      const adds = d.files.reduce((n, f) => n + (+f.add || 0), 0), dels = d.files.reduce((n, f) => n + (+f.del || 0), 0);
      const who = d.author.replace(/\s*<.*>$/, "");
      $("#hist-detail").innerHTML = `<div class="commit-meta"><div class="commit-top">${avatar({ id: who, name: who })}<div><h4>${esc(d.message)}</h4>` +
        `<div class="muted">${esc(d.author)} · ${esc(d.date)}</div></div></div>` +
        `<div class="commit-nums"><span class="sha">${d.sha.slice(0, 12)}</span><span>${d.files.length} 个文件</span><span class="plus">+${adds}</span><span class="minus">−${dels}</span></div></div>` + renderDiff(d.diff);
    });
    list.append(row);
  }
  list.querySelector(".row")?.click();
});

/* ------------------------------------------------------------ run */

const fullChecks = new Map();
const pendingRuns = new Map();
const pendingWatch = new Set();

function resetActivity() {
  S.activity = { runId: null, next: 0, live: null };
  for (const id of ["console", "run-problems", "run-state", "check-state", "cnt-run", "cnt-problems"]) {
    $("#" + id).replaceChildren();
  }
  delete $("#console").dataset.ended;
  $("#btn-run span").textContent = "运行";
  $("#btn-stop").disabled = true;
  $("#btn-run").disabled = pendingRuns.has(S.pid);
  $("#run-state").textContent = pendingRuns.get(S.pid) || "";
  $("#watch-toggle").checked = false;
  $("#watch-toggle").disabled = pendingWatch.has(S.pid);
  renderChecks();
}

async function pollRun() {
  const view = S.activity, pid = S.pid;
  if (!S.proj || view.runPoll || pendingRuns.has(pid)) return;
  const token = view.runPoll = Symbol();
  let r;
  try {
    r = await api(`projects/${pid}/run?` + qs({ from: view.next, id: view.runId }));
  } catch {
    if (view === S.activity && token === view.runPoll) {
      $("#run-state").textContent = "运行状态暂不可用，正在重试";
      $("#btn-stop").disabled = true;
    }
    return;
  } finally {
    if (view.runPoll === token) view.runPoll = null;
    else r = null;
  }
  if (!r || view !== S.activity || pid !== S.pid) return;
  const con = $("#console");
  const atBottom = con.scrollHeight - con.scrollTop - con.clientHeight < 40;
  if (r.id !== view.runId) {
    view.runId = r.id; view.next = 0; con.replaceChildren(); delete con.dataset.ended;
    if (r.id) con.append(h("div", { className: "sys", textContent: `$ ${r.command}` }));
  }
  if (r.from > view.next) con.append(h("div", { className: "sys", textContent: `— 较早的 ${r.from - view.next} 行已超出日志缓存 —` }));
  const frag = document.createDocumentFragment();
  for (const ln of r.lines) frag.append(h("div", { className: ln.err ? "e" : "", textContent: ln.t || " " }));
  con.append(frag);
  while (con.childElementCount > 5000) con.firstChild.remove();
  view.next = r.next;
  $("#btn-stop").disabled = !r.running;
  $("#btn-run span").textContent = r.running ? "重新运行" : "运行";
  const errs = r.problems.filter((p) => p.severity === "error").length;
  $("#cnt-run").textContent = r.running ? "运行中" : errs || "";
  $("#cnt-run").classList.toggle("err", !r.running && errs > 0);
  $("#cnt-run").classList.toggle("live", r.running);
  const project = S.reg.projects.find((p) => p.id === pid);
  const failed = !r.running && !r.stopped && !!r.code;
  const refreshStats = view.run?.failed !== failed || view.run?.command !== r.command || view.run?.code !== r.code;
  view.run = { failed, command: r.command, code: r.code };
  if (project && project.running !== r.running) { project.running = r.running; renderStats(); renderSidebar(); }
  else if (refreshStats) renderStats();
  if (r.id) {
    const dur = ((r.ended || Date.now() / 1000) - r.started).toFixed(1);
    const state = r.running ? "运行中" : r.stopped ? "已手动停止" : r.code === 0 ? "正常结束" : `运行失败 · 退出码 ${r.code}`;
    const color = r.running ? "run" : r.stopped ? "stopped" : r.code === 0 ? "ok" : "fail";
    $("#run-state").innerHTML = `<span class="state-pill ${color}">${state} · ${dur}s</span>`;
    if (!r.running && con.dataset.ended !== r.id) {
      con.dataset.ended = r.id;
      con.append(h("div", { className: "sys", textContent: `— ${state} —` }));
    }
  } else $("#run-state").textContent = "尚未运行";
  if (atBottom) con.scrollTop = con.scrollHeight;
  renderProblemList($("#run-problems"), r.problems, true);
}

const changeRun = guard(async (action) => {
  const view = S.activity, project = S.proj;
  if (!project || pendingRuns.has(project.id)) return;
  const command = $("#run-cmd").value.trim();
  if (action === "run" && !command) return toast("先填写运行命令", true);
  if (action === "stop" && !view.runId) return;
  view.runPoll = null;
  $("#btn-run").disabled = $("#btn-stop").disabled = true;
  $("#run-state").textContent = action === "stop" ? "正在停止…" : view.runId ? "正在启动新任务…" : "正在启动…";
  pendingRuns.set(project.id, $("#run-state").textContent);
  try {
    if (action === "run" && command !== project.run && confirm(`把「${command}」保存为该项目的默认运行命令？`)) {
      await api(`projects/${project.id}/update`, { run: command }); project.run = command;
    }
    await api(`projects/${project.id}/${action}`, action === "stop" ? { id: view.runId } : { command });
  } catch (e) { toast(`${project.name}：${e.message}`, true); }
  finally {
    pendingRuns.delete(project.id);
    if (project.id === S.pid) { $("#btn-run").disabled = false; await pollRun(); }
  }
});

/* ------------------------------------------------------------ problems */

async function pollProblems() {
  const view = S.activity, pid = S.pid;
  if (!S.proj || view.watchPoll || pendingWatch.has(pid)) return;
  const token = view.watchPoll = Symbol();
  try {
    const live = await api(`projects/${pid}/problems`);
    if (view !== S.activity || pid !== S.pid || view.watchPoll !== token) return;
    const changed = !view.live || live.version !== view.live.version || live.state !== view.live.state || view.watchError;
    view.live = live; view.watchError = false;
    $("#watch-toggle").checked = live.state !== "off";
    if (changed) renderChecks();
  } catch {
    if (view === S.activity && view.watchPoll === token) {
      view.watchError = true;
      renderChecks();
    }
  } finally { if (view.watchPoll === token) view.watchPoll = null; }
}

function renderChecks() {
  const live = S.activity.live, full = fullChecks.get(S.pid);
  const state = live?.state;
  const result = full?.result;
  let status = "";
  if (S.activity.watchError) status = "实时检查状态暂不可用";
  else if (state === "watching") status = `实时检查中 · 最近 ${new Date(live.checkedAt * 1000).toLocaleTimeString()}`;
  else if (state === "starting" || state === "checking") status = "正在检查文件…";
  else if (state?.startsWith("error:")) status = "实时检查失败：" + state.slice(6);
  $("#check-state").textContent = status;
  $("#full-state").textContent = full?.busy ? "正在完整检查…" : full?.error || (result ? `上次完整检查：${result.files} 个文件，${result.seconds}s` : "");
  $("#btn-check").disabled = !!full?.busy;
  $("#check-output").hidden = !result?.output;
  $("#check-output").textContent = result?.output || "";
  const probs = state && state !== "off" ? [...live.problems, ...(result?.problems || []).filter(p => p.source === "check")] : result?.problems || [];
  setProblems(probs);
}

function setProblems(probs) {
  const before = JSON.stringify(S.problems);
  S.problems = probs;
  const errs = probs.filter((p) => p.severity === "error").length;
  $("#cnt-problems").textContent = probs.length ? errs || probs.length : "";
  $("#cnt-problems").classList.toggle("err", errs > 0);
  if (S.proj) renderStats();
  if (S.tab === "problems") renderProblems();
  if (before !== JSON.stringify(probs)) {
    renderTree();
    if (S.openFile && !S.editing && !S.fileRef && S.fileContent != null) renderCode($("#viewer"), S.fileContent, S.openFile, S.openLine);
  }
}

function renderProblems() { renderProblemList($("#problems"), S.problems, false); }

function renderProblemList(el, probs, compact) {
  if (!probs.length) {
    const live = S.activity.live, full = fullChecks.get(S.pid);
    const checked = !full?.busy && !full?.error && !S.activity.watchError &&
      (live?.state === "watching" || (live?.state === "off" && full?.result));
    const hint = checked ? "" : full?.busy || ["starting", "checking"].includes(live?.state) ? "检查中…" :
      S.activity.watchError || full?.error || live?.state?.startsWith("error:") ? "检查结果暂不可用" : "打开「实时检查」或点「完整检查」";
    el.innerHTML = compact ? "" : `<div class="ok-msg">${checked ? ico("check") + "<b>没有发现问题</b>" : ""}<span class="hint">${hint}</span></div>`;
    return;
  }
  el.innerHTML = "";
  for (const p of probs) {
    const row = h("div", { className: "prob " + p.severity },
      `<span class="sev ${p.severity}">${p.severity === "error" ? "✗" : "!"}</span><span class="loc">${esc(p.file || "(项目)")}${p.line ? ":" + p.line : ""}</span><span>${esc(p.message)}</span><span class="src">${esc(p.source)}</span>`);
    if (p.file) row.onclick = () => { switchTab("source"); if (S.fileRef) { S.fileRef = ""; $("#src-ref").value = ""; loadFiles(); } openFile(p.file, p.line); };
    el.append(row);
  }
}

/* ------------------------------------------------------------ dialogs */

function dialog(title, bodyHtml, okText = "确定", wire) {
  return new Promise((resolve) => {
    const dlg = $("#dlg");
    $("#dlg-title").textContent = title;
    $("#dlg-body").innerHTML = bodyHtml;
    $("#dlg-ok").textContent = okText;
    $("#dlg-ok").disabled = false;
    $("#dlg-ok").hidden = !okText;
    wire?.($("#dlg-body"));
    dlg.onclose = () => resolve(dlg.returnValue === "ok" ? $("#dlg-body") : null);
    dlg.returnValue = "";
    dlg.showModal();
  });
}
const groupOptions = (sel) => S.reg.groups.map((g) => `<option value="${esc(g.id)}" ${g.id === sel ? "selected" : ""}>${esc(g.name)}</option>`).join("") + `<option value="__new">＋ 新建分组…</option>`;
async function resolveGroup(val, newName) {
  if (val !== "__new") return val;
  if (!newName) throw new Error("请填写新分组名");
  return (await api("groups", { name: newName })).id;
}
const groupField = (sel) => `<label class="field">分组<select name="group">${groupOptions(sel)}</select></label><label class="field" data-newgroup hidden>新分组名<input name="newgroup" placeholder="例如：Forge 全家桶"></label>`;
const wireGroup = (b) => { b.querySelector("[name=group]").onchange = (e) => (b.querySelector("[data-newgroup]").hidden = e.target.value !== "__new"); };

const folderField = (label, value = "") => `<label class="field">${label}<span class="row-inline"><input name="path" required value="${esc(value)}" placeholder="C:\\Users\\…\\my-project"><button type="button" class="ghost" data-browse>浏览…</button></span></label>`;
const wireBrowse = (b) => {
  b.querySelector("[data-browse]").onclick = guard(async () => {
    const r = await api("pick-folder", { title: "选择文件夹" });
    if (r.path) b.querySelector("[name=path]").value = r.path;
  });
};

const addDialog = guard(async () => {
  const b = await dialog("添加项目", `${folderField("项目目录")}${groupField("default")}<label class="field">显示名（可选）<input name="name"></label>`, "添加", (x) => { wireGroup(x); wireBrowse(x); });
  if (!b) return;
  const group = await resolveGroup(b.querySelector("[name=group]").value, b.querySelector("[name=newgroup]").value.trim());
  const p = await api("projects", { path: b.querySelector("[name=path]").value.trim(), group, name: b.querySelector("[name=name]").value.trim() || undefined });
  await loadRegistry(); await selectProject(p.id); toast(`已添加 ${p.name}`);
});

const scanDialog = guard(async () => {
  const pick = await dialog("扫描目录", folderField("在这个目录下查找项目（向下 2 层）", localStorage.hubScanRoot || ""), "扫描", wireBrowse);
  if (!pick) return;
  const root = pick.querySelector("[name=path]").value.trim();
  if (!root) return;
  localStorage.hubScanRoot = root;
  toast("扫描中…");
  const found = await api("scan?" + qs({ root, depth: 2 }));
  if (!found.length) return toast("没找到项目（含 .git / package.json / pyproject.toml 等的目录）", true);
  const rows = found.map((f, i) => `<label><input type="checkbox" data-i="${i}" ${f.registered ? "disabled checked" : ""}>${esc(f.name)} <span class="muted">${esc(f.kind.join(" "))}</span><span class="path" title="${esc(f.path)}">${esc(f.path)}</span></label>`).join("");
  const hasNew = found.some((f) => !f.registered);
  const hint = hasNew ? "勾选要登记的项目（已登记的置灰）。" : "这些项目都已登记。";
  const b = await dialog(`找到 ${found.length} 个项目`, `<p class="hint">${hint}</p><div class="scan-list">${rows}</div>${hasNew ? groupField("default") : ""}`, hasNew ? "添加所选" : "关闭", (el) => {
    if (!hasNew) return;
    wireGroup(el);
    const update = () => { $("#dlg-ok").disabled = !el.querySelector("input[data-i]:checked:not(:disabled)"); };
    el.querySelector(".scan-list").onchange = update;
    update();
  });
  if (!b || !hasNew) return;
  const picks = $$("input[data-i]:checked:not(:disabled)", b).map((x) => found[+x.dataset.i]);
  const groupId = b.querySelector("[name=group]").value;
  const target = groupId === "__new" ? { group: b.querySelector("[name=newgroup]").value.trim() } : { groupId };
  const { added } = await api("scan", { paths: picks.map((f) => f.path), ...target });
  await loadRegistry(); toast(`新增 ${added.length} 个项目` + (added.length < picks.length ? `，${picks.length - added.length} 个已登记` : ""));
});

const groupDialog = guard(async () => {
  const b = await dialog("新建分组", `<label class="field">分组名<input name="name" required placeholder="相关项目放一起，例如：Forge 全家桶"></label><label class="field">颜色<input type="color" name="color" value="#6d8cff"></label>`, "创建");
  if (!b) return;
  await api("groups", { name: b.querySelector("[name=name]").value.trim(), color: b.querySelector("[name=color]").value });
  await loadRegistry();
});

const editGroup = guard(async (g) => {
  const b = await dialog(`分组：${g.name}`, `<label class="field">分组名<input name="name" value="${esc(g.name)}"></label><label class="field">颜色<input type="color" name="color" value="${esc(g.color)}"></label><label><input type="checkbox" name="del"> 删除分组（项目移回「未分组」）</label>`, "保存");
  if (!b) return;
  await api(`groups/${g.id}`, b.querySelector("[name=del]").checked ? { delete: true } : { name: b.querySelector("[name=name]").value.trim(), color: b.querySelector("[name=color]").value });
  await loadRegistry();
});

const settingsDialog = guard(async () => {
  const p = S.proj;
  const initialGroup = p.group;
  const f = (k, label, ph = "") => `<label class="field">${label}<input name="${k}" value="${esc(p[k] || "")}" placeholder="${esc(ph)}"></label>`;
  const b = await dialog(`项目设置：${p.name}`,
    f("name", "显示名") + groupField(p.group) + f("run", "运行命令", "python main.py / npm run dev") +
    f("check", "检查命令（可选，完整检查时运行；输出中的 文件:行 会被解析）", "npx tsc --noEmit / pytest -x -q") +
    f("preview", "预览入口（相对路径的 HTML，可选）", "index.html") + f("description", "备注") +
    `<label><input type="checkbox" name="remove"> 从仓库移除（只移除登记，不删除文件）</label>`, "保存", wireGroup);
  if (!b) return;
  const v = (k) => b.querySelector(`[name=${k}]`).value.trim();
  if (b.querySelector("[name=remove]").checked) {
    if (!canLeaveFile()) return;
    await api(`projects/${p.id}/remove`, {});
    if (S.pid === p.id) {
      resetViewer(); S.pid = null; S.proj = null; location.hash = ""; $("#project").hidden = true;
    }
    await loadRegistry(); return;
  }
  const changes = { name: v("name"), run: v("run"), check: v("check"), preview: v("preview"), description: v("description") };
  if (v("group") !== initialGroup) changes.groupId = await resolveGroup(v("group"), v("newgroup"));
  await api(`projects/${p.id}/update`, changes);
  delete $("#run-cmd").dataset.touched;
  await loadRegistry(); await refreshProject();
});

function agentHelp() {
  const pid = S.pid ? "id:" + S.pid : "<项目>";
  dialog("给 Agent 的用法", `<p class="hint">Agent 在终端里调用（无需本页打开）。&lt;项目&gt; 可以是 id、名称或路径；加 <code>--json</code> 得到机器可读输出。</p><pre>cd ${esc(S.cfg.root)}
python hub.py find "关键词" --json
python hub.py tag ${esc(pid)} --add 待发布
python hub.py find --tag 待发布 --json
python hub.py status ${esc(pid)}

# 提交到指定分支（不切换工作区；分支不存在会从 HEAD 新建）
python hub.py commit ${esc(pid)} -b agent/feature-x -m "feat: 说明" [文件...]

# 核对远端和分支，用户要求推送时带上本次预演返回的编号
python hub.py info ${esc(pid)} --json
python hub.py push ${esc(pid)} -b agent/feature-x --remote origin --dry-run --json
python hub.py push ${esc(pid)} -b agent/feature-x --remote origin --preview-id &lt;previewId&gt; --json

# 语法/静态检查，有错误时退出码为 1
python hub.py check ${esc(pid)}

# 运行项目 N 秒，捕获报错（Traceback / 文件:行），有错误退出码为 1
python hub.py run ${esc(pid)} --timeout 20
python hub.py run ${esc(pid)} --cmd "python -m pytest -x -q"

# 分组 & 登记
python hub.py add D:\\path\\to\\proj --group "Forge 全家桶"
python hub.py group "Forge 全家桶" forge forge-gui</pre><p class="hint">详细说明见仓库里的 AGENTS.md。</p>`, "");
}

/* ------------------------------------------------------------ wiring */

$("#search").oninput = renderSidebar;
$("#btn-add").onclick = $("#btn-add2").onclick = addDialog;
$("#btn-scan").onclick = $("#btn-scan2").onclick = scanDialog;
$("#btn-group").onclick = groupDialog;
$("#btn-agent").onclick = agentHelp;
$("#btn-settings").onclick = settingsDialog;
$$("#tabs button").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));
$$("[data-open]").forEach((b) => (b.onclick = guard(() => api(`projects/${S.pid}/open`, { target: b.dataset.open }))));
$("#btn-file-code").onclick = guard(() => api(`projects/${S.pid}/open`, { target: "vscode", file: S.openFile, line: S.openLine || 1 }));
$("#btn-preview").onclick = guard(() => api(`projects/${S.pid}/preview`, {}));
$("#src-filter").oninput = renderTree;
$("#src-ref").onchange = guard(async (e) => {
  if (!canLeaveFile()) { e.target.value = S.fileRef; return; }
  S.fileRef = e.target.value; resetViewer(); S.files = []; renderTree(); await loadFiles();
});
$("#btn-edit").onclick = () => {
  S.editing = true;
  $("#editor").value = S.fileContent; S.editStart = $("#editor").value;
  $("#editor").hidden = false; $("#viewer").hidden = true;
  $("#btn-edit").hidden = true; $("#btn-save").hidden = false; $("#editor").focus();
};
$("#btn-save").onclick = guard(async () => {
  if (!S.editing || S.fileRef || !S.openFile || S.saving) return;
  S.saving = true; $("#btn-save").disabled = true; $("#editor").readOnly = true;
  try {
    await api(`projects/${S.pid}/save`, { path: S.openFile, content: $("#editor").value, version: S.fileVersion });
    S.editing = false;
  } finally {
    S.saving = false; $("#btn-save").disabled = false; $("#editor").readOnly = false;
  }
  toast("已保存"); await Promise.all([openFile(S.openFile, S.openLine), refreshProject()]);
  renderTree();
});
$("#editor").onkeydown = (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); $("#btn-save").click(); }
  if (e.key === "Tab") { e.preventDefault(); document.execCommand("insertText", false, "    "); }
};
$("#chg-all").onchange = (e) => {
  const changes = S.proj.gitInfo.changes;
  S.unchecked = new Set(e.target.checked ? [] : changes.map((c) => c.path));
  renderChanges();
};
$("#btn-refresh-chg").onclick = guard(refreshProject);
$("#cm-branch").oninput = updateCommitHint;
$("#commit-form").onsubmit = guard(async (e) => {
  e.preventDefault();
  const all = $$("#chg-list input[type=checkbox]");
  const selected = new Set(all.filter((c) => c.checked).map((c) => c.dataset.path));
  const files = S.proj.gitInfo.changes.filter((c) => selected.has(c.path))
    .flatMap((c) => c.index === "R" && c.orig ? [c.orig, c.path] : [c.path]);
  if (!files.length) return toast("没有勾选文件", true);
  const pid = S.pid;
  const submit = $("#commit-form button[type=submit]");
  if (submit.disabled) return;
  submit.disabled = true;
  let r;
  try { r = await api(`projects/${pid}/commit`, { branch: $("#cm-branch").value.trim(), message: $("#cm-msg").value, files }); }
  finally { submit.disabled = false; }
  if (pid !== S.pid) return;
  toast(`已提交到 ${r.branch}${r.created ? "（新分支）" : ""} · ${r.sha.slice(0, 8)}`);
  $("#cm-msg").value = ""; S.unchecked.clear();
  await refreshProject(); $("#chg-diff").innerHTML = "";
});
$("#btn-git-init").onclick = guard(async () => { await api(`projects/${S.pid}/git-init`, {}); await refreshProject(); toast("已初始化 git 仓库"); });
$("#hist-ref").onchange = loadHistory;
$("#run-cmd").oninput = (e) => (e.target.dataset.touched = "1");
$("#run-cmd").onkeydown = (e) => { if (e.key === "Enter") changeRun("run"); };
$("#btn-run").onclick = () => changeRun("run");
$("#btn-stop").onclick = () => changeRun("stop");
$("#watch-toggle").onchange = guard(async (e) => {
  const view = S.activity, project = S.proj, on = e.target.checked;
  if (!project || pendingWatch.has(project.id)) return;
  pendingWatch.add(project.id); view.watchPoll = null;
  $("#watch-toggle").disabled = true;
  try { await api(`projects/${project.id}/watch`, { on }); }
  catch (err) { toast(`${project.name}：${err.message}`, true); }
  finally {
    pendingWatch.delete(project.id);
    if (S.pid === project.id) {
      $("#watch-toggle").disabled = false;
      await pollProblems();
    }
    await loadRegistry();
  }
});
$("#btn-check").onclick = guard(async () => {
  const pid = S.pid;
  if (!pid || fullChecks.get(pid)?.busy) return;
  const full = { busy: true };
  fullChecks.set(pid, full); renderChecks();
  try { full.result = await api(`projects/${pid}/check`, {}); }
  catch (e) { full.error = "完整检查失败：" + e.message; }
  finally {
    full.busy = false;
    if (pid === S.pid) renderChecks();
  }
});

// Only the visible project consumes log and diagnostic snapshots.
setInterval(() => { if (S.proj && (S.tab === "run" || !$("#btn-stop").disabled)) pollRun(); }, 800);
setInterval(() => { if (S.proj && (S.tab === "problems" || $("#watch-toggle").checked || S.activity.watchError)) pollProblems(); }, 1200);
setInterval(() => loadRegistry().catch(() => {}), 5000);
setInterval(() => { if (S.pid && S.tab === "changes" && document.hasFocus()) refreshProject().catch(() => {}); }, 4000);

// deep links: #<project>[:<tab>[:<file>]]   e.g. #py-tool:source:main.py
async function openFromHash() {
  const [pid, tab, ...file] = decodeURIComponent(location.hash.slice(1)).split(":");
  if (!pid || !S.reg.projects.some((p) => p.id === pid)) return false;
  if (pid !== S.pid) {
    await selectProject(pid, !!tab);
    if (pid !== S.pid) return false;
  }
  if (tab && tab !== S.tab) switchTab(tab);
  if (file.length) await openFile(file.join(":"));
  return true;
}
window.addEventListener("hashchange", guard(openFromHash));
window.addEventListener("beforeunload", (ev) => {
  if (S.editing && $("#editor").value !== S.editStart) { ev.preventDefault(); ev.returnValue = ""; }
});

guard(async () => {
  S.cfg = await api("config");
  await loadRegistry();
  if (!(await openFromHash())) showLanding(true);
})();

document.addEventListener("click", (ev) => {
  const tag = ev.target.closest?.(".tag[data-tag]");
  if (!tag) return;
  ev.stopPropagation(); ev.preventDefault();
  $("#search").value = tag.dataset.tag; renderSidebar(); $("#search").focus();
}, true);

/* ------------------------------------------------------------ starry sky */

(function initStarfield() {
  const canvas = $("#starfield");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const COLORS = [
    [244, 248, 255], // pure starlight
    [194, 220, 255], // blue-white giant
    [224, 210, 255], // cosmic lavender
    [255, 236, 198], // warm starlight
    [172, 236, 255], // cyan tint
  ];
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)");

  let w = 0, h = 0, dpr = 1;
  let stars = [];
  let meteors = [];
  let rafId = 0;
  let lastTs = 0;
  let nextMeteorAt = 2400;

  function makeStars() {
    const area = w * h;
    const count = Math.max(120, Math.min(320, Math.round(area / 6200)));
    stars = [];
    for (let i = 0; i < count; i++) {
      const roll = Math.random();
      const tier = roll < 0.66 ? 0 : roll < 0.93 ? 1 : 2;
      const r = tier === 0 ? 0.45 + Math.random() * 0.55
        : tier === 1 ? 1.0 + Math.random() * 0.6
        : 1.65 + Math.random() * 0.75;
      const color = COLORS[Math.floor(Math.random() * COLORS.length)];
      stars.push({
        x: Math.random() * w,
        y: Math.random() * h,
        r,
        tier,
        color,
        base: tier === 0 ? 0.22 + Math.random() * 0.36
          : tier === 1 ? 0.48 + Math.random() * 0.38
          : 0.72 + Math.random() * 0.28,
        amp: tier === 0 ? 0.14 + Math.random() * 0.18 : 0.2 + Math.random() * 0.24,
        speed: 0.0005 + Math.random() * 0.0014,
        phase: Math.random() * Math.PI * 2,
        driftX: (tier === 0 ? 0.0025 : tier === 1 ? 0.005 : 0.008) * (Math.random() < 0.5 ? 1 : 0.6),
        driftY: -0.0015 - tier * 0.0015,
        sparkle: tier === 2 && Math.random() < 0.65,
      });
    }
  }

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = window.innerWidth || 1280;
    h = window.innerHeight || 800;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    makeStars();
    draw(performance.now(), 0);
  }

  function spawnMeteor(now) {
    const startX = w * (0.2 + Math.random() * 0.78);
    const startY = h * (0.04 + Math.random() * 0.36);
    const angle = Math.PI * (0.68 + Math.random() * 0.14);
    const speed = 0.52 + Math.random() * 0.32;
    meteors.push({
      x: startX,
      y: startY,
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed,
      len: 75 + Math.random() * 85,
      life: 0,
      maxLife: 620 + Math.random() * 380,
    });
    nextMeteorAt = now + 4500 + Math.random() * 6500;
  }

  function draw(now, dt) {
    ctx.clearRect(0, 0, w, h);

    // Subtle galactic band glow across the diagonal
    const band = ctx.createLinearGradient(0, h * 0.12, w, h * 0.88);
    band.addColorStop(0, "rgba(96, 130, 255, 0)");
    band.addColorStop(0.35, "rgba(104, 138, 255, 0.032)");
    band.addColorStop(0.55, "rgba(168, 120, 255, 0.042)");
    band.addColorStop(0.78, "rgba(56, 189, 248, 0.024)");
    band.addColorStop(1, "rgba(56, 189, 248, 0)");
    ctx.fillStyle = band;
    ctx.fillRect(0, 0, w, h);

    for (const s of stars) {
      if (dt > 0) {
        s.x += s.driftX * dt;
        s.y += s.driftY * dt;
        if (s.x > w + 4) s.x = -4;
        if (s.y < -4) s.y = h + 4;
      }
      const tw = Math.sin(now * s.speed + s.phase);
      const alpha = Math.max(0.08, Math.min(1, s.base + tw * s.amp));
      const [cr, cg, cb] = s.color;

      if (s.tier >= 1) {
        const haloR = s.r * (s.tier === 2 ? 4.6 : 2.8);
        const g = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, haloR);
        g.addColorStop(0, `rgba(${cr},${cg},${cb},${(alpha * 0.55).toFixed(3)})`);
        g.addColorStop(1, `rgba(${cr},${cg},${cb},0)`);
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(s.x, s.y, haloR, 0, Math.PI * 2);
        ctx.fill();
      }

      ctx.fillStyle = `rgba(${cr},${cg},${cb},${alpha.toFixed(3)})`;
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      ctx.fill();

      if (s.sparkle && alpha > 0.62) {
        const arm = s.r * (2.6 + tw * 0.7);
        ctx.strokeStyle = `rgba(${cr},${cg},${cb},${(alpha * 0.42).toFixed(3)})`;
        ctx.lineWidth = 0.75;
        ctx.beginPath();
        ctx.moveTo(s.x - arm, s.y);
        ctx.lineTo(s.x + arm, s.y);
        ctx.moveTo(s.x, s.y - arm);
        ctx.lineTo(s.x, s.y + arm);
        ctx.stroke();
      }
    }

    // Shooting stars
    if (dt > 0) {
      if (now >= nextMeteorAt && meteors.length < 2) spawnMeteor(now);
      for (let i = meteors.length - 1; i >= 0; i--) {
        const m = meteors[i];
        m.life += dt;
        m.x += m.vx * dt;
        m.y += m.vy * dt;
        const t = m.life / m.maxLife;
        if (t >= 1) { meteors.splice(i, 1); continue; }
        const fade = t < 0.2 ? t / 0.2 : 1 - (t - 0.2) / 0.8;
        const spd = Math.hypot(m.vx, m.vy) || 1;
        const tx = m.x - (m.vx / spd) * m.len;
        const ty = m.y - (m.vy / spd) * m.len;
        const grad = ctx.createLinearGradient(m.x, m.y, tx, ty);
        grad.addColorStop(0, `rgba(255, 255, 255, ${(0.92 * fade).toFixed(3)})`);
        grad.addColorStop(0.28, `rgba(173, 210, 255, ${(0.48 * fade).toFixed(3)})`);
        grad.addColorStop(1, "rgba(139, 92, 246, 0)");
        ctx.strokeStyle = grad;
        ctx.lineWidth = 1.6;
        ctx.lineCap = "round";
        ctx.beginPath();
        ctx.moveTo(m.x, m.y);
        ctx.lineTo(tx, ty);
        ctx.stroke();

        ctx.fillStyle = `rgba(255, 255, 255, ${(0.95 * fade).toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(m.x, m.y, 1.4, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  function tick(now) {
    if (document.hidden || reduceMotion?.matches) { rafId = 0; return; }
    const dt = lastTs ? Math.min(48, now - lastTs) : 16;
    lastTs = now;
    draw(now, dt);
    rafId = requestAnimationFrame(tick);
  }

  function start() {
    if (reduceMotion?.matches) { draw(performance.now(), 0); return; }
    if (!rafId && !document.hidden) { lastTs = 0; rafId = requestAnimationFrame(tick); }
  }

  window.addEventListener("resize", resize);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    else start();
  });
  reduceMotion?.addEventListener?.("change", () => {
    if (reduceMotion.matches && rafId) { cancelAnimationFrame(rafId); rafId = 0; draw(performance.now(), 0); }
    else start();
  });

  resize();
  start();
})();

/* ================================================================ v4: overview, palette, GitHub, management */

const relTime = (ts) => {
  if (!ts) return "—";
  const d = Date.now() / 1000 - ts;
  return d < 60 ? "刚刚" : d < 3600 ? `${Math.floor(d / 60)} 分钟前` : d < 86400 ? `${Math.floor(d / 3600)} 小时前`
    : d < 86400 * 30 ? `${Math.floor(d / 86400)} 天前` : new Date(ts * 1000).toLocaleDateString();
};

/* ---------------- landing: overview dashboard (or welcome hero when nothing is registered) */
let homeLoadedAt = 0;
function showLanding(force = false) {
  const landing = !S.pid;
  const hasProjects = S.reg.projects.length > 0;
  $("#empty").hidden = !landing || hasProjects;
  $("#home").hidden = !landing || !hasProjects;
  if (landing && hasProjects && (force || Date.now() - homeLoadedAt > 30000)) loadHome();
}

const loadHome = guard(async () => {
  homeLoadedAt = Date.now();
  $("#home-sub").textContent = "正在读取各项目状态…";
  const r = await api("overview");
  if (S.pid) return;
  const sm = r.summary;
  $("#home-sub").textContent = `${sm.projects} 个项目 · 读取用时 ${r.seconds}s`;
  const card = (c, icon, n, label, filter) =>
    `<button class="home-card${n ? "" : " zero"}" style="--c:${c}" data-filter="${filter}">${ico(icon)}<b>${n}</b><span>${label}</span></button>`;
  $("#home-cards").innerHTML =
    card("#6d8cff", "layers", sm.projects, "全部项目", "all") +
    card("#fbbf24", "diff", sm.dirty, "有未提交改动", "dirty") +
    card("#34d399", "upload", sm.ahead, "待推送", "ahead") +
    card("#22d3ee", "history", sm.behind, "落后上游", "behind") +
    card("#c084fc", "github", sm.noRemote, "未配置远端", "noremote") +
    card("#f87171", "alert", sm.missing, "目录失效", "missing");
  S.homeRows = r.projects;
  renderHomeTable(S.homeFilter || "all");
  $$("#home-cards .home-card").forEach((b) => (b.onclick = () => renderHomeTable(b.dataset.filter)));
});

function renderHomeTable(filter) {
  S.homeFilter = filter;
  $$("#home-cards .home-card").forEach((b) => b.classList.toggle("on", b.dataset.filter === filter));
  const test = {
    all: () => true, dirty: (p) => p.changes, ahead: (p) => p.ahead, behind: (p) => p.behind,
    noremote: (p) => p.git && !p.remote, missing: (p) => !p.exists,
  }[filter];
  const rows = (S.homeRows || []).filter(test);
  const g = (id) => groupOf(id);
  $("#home-table").innerHTML = `<thead><tr><th>项目</th><th>分组</th><th>分支</th><th>状态</th><th>最近提交</th><th>远端</th></tr></thead><tbody>` +
    (rows.length ? rows.map((p) => {
      const state = !p.exists ? `<span class="st-chip bad">目录失效</span>` : !p.git ? `<span class="st-chip">非 git</span>` :
        (p.changes ? `<span class="st-chip warn">✎ ${p.changes}</span>` : `<span class="st-chip ok">干净</span>`) +
        (p.ahead ? `<span class="st-chip ok">↑${p.ahead}</span>` : "") + (p.behind ? `<span class="st-chip info">↓${p.behind}</span>` : "");
      const remote = p.remote ? (p.remote.includes("github.com") ? `${ico("github", "sm")} ${esc(p.remote.replace(/^https:\/\/github.com\//, "").replace(/\.git$/, ""))}` : esc(p.remote))
        : p.git ? `<span class="muted">—</span>` : "";
      return `<tr data-id="${esc(p.id)}"><td><div class="cell-name">${avatar(p)}<div><b>${p.pinned ? ico("star", "pin-mark") : ""}${esc(p.name)}</b>` +
        `<div class="tags">${(p.tags || []).slice(0, 3).map((t) => tagChip(t, true)).join("")}</div></div></div></td>` +
        `<td><span class="group-chip" style="--gc:${g(p.group).color}">${esc(g(p.group).name)}</span></td>` +
        `<td>${p.git ? `<code>${esc(p.branch || "—")}</code>` : ""}</td><td class="st-cell">${state}</td>` +
        `<td><div class="ellipsis">${esc(p.lastCommit?.subject || "—")}</div><div class="muted small">${relTime(p.lastCommit?.time)}</div></td>` +
        `<td class="remote-cell">${remote}</td></tr>`;
    }).join("") : `<tr><td colspan="6" class="muted empty-row">没有符合条件的项目</td></tr>`) + "</tbody>";
  $$("#home-table tbody tr[data-id]").forEach((tr) => (tr.onclick = guard(() => selectProject(tr.dataset.id))));
}
$("#btn-home").onclick = guard(async () => {
  if (S.pid && !canLeaveFile()) return;
  S.pid = null; S.proj = null; location.hash = ""; $("#project").hidden = true; renderSidebar(); showLanding(true);
});
$("#home-refresh").onclick = () => loadHome();
$("#home-scan").onclick = () => scanDialog();

/* ---------------- project header: pin, publish, missing folder */
const _refreshProject = refreshProject;
refreshProject = async function () {
  await _refreshProject();
  const p = S.proj;
  if (!p) return;
  const reg = S.reg.projects.find((x) => x.id === p.id) || p;
  $("#btn-pin").classList.toggle("on", !!reg.pinned);
  $("#btn-pin").title = reg.pinned ? "取消置顶" : "置顶";
  const gi = p.gitInfo;
  const onGitHub = gi.git && /github\.com/.test(gi.remote || "");
  $("#btn-publish").hidden = !gi.git || onGitHub;
  $("#btn-publish span").textContent = gi.remote ? "另建 GitHub 仓库" : "发布到 GitHub";
  $("#p-missing").hidden = reg.exists !== false;
};
$("#btn-pin").onclick = guard(async () => {
  const reg = S.reg.projects.find((x) => x.id === S.pid);
  await api(`projects/${S.pid}/pin`, { pinned: !reg?.pinned });
  await loadRegistry(); await refreshProject();
});
$("#btn-relocate").onclick = guard(async () => {
  const r = await api("pick-folder", { title: `为「${S.proj.name}」选择新位置` });
  if (!r.path) return;
  await api(`projects/${S.pid}/relocate`, { path: r.path });
  toast(`已更新路径：${r.path}`);
  await loadRegistry(); const pid = S.pid; S.pid = null; S.proj = null; await selectProject(pid);
});
$("#btn-unregister").onclick = guard(async () => {
  if (!confirm(`从仓库移除「${S.proj.name}」的登记？（不会删除任何文件）`)) return;
  await api(`projects/${S.pid}/remove`, {});
  S.pid = null; S.proj = null; location.hash = ""; $("#project").hidden = true;
  await loadRegistry(); showLanding(true);
});

$("#btn-publish").onclick = guard(async () => {
  const gh = await api("github?verify=0");
  if (!gh.connected) { toast("请先连接 GitHub", true); return githubDialog(); }
  const p = S.proj;
  const slug = p.name.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[-.]+|[-.]+$/g, "") || "project";
  const b = await dialog("发布到 GitHub",
    `<p class="hint">在 <b>${esc(gh.login || "你的账号")}</b> 下新建仓库，添加为远端 <code>origin</code>，并推送当前分支 <code>${esc(p.gitInfo.branch)}</code>。</p>` +
    `<label class="field">仓库名<input name="repo" value="${esc(slug)}" pattern="[A-Za-z0-9._-]{1,100}" required></label>` +
    `<label class="field">描述<input name="desc" value="${esc(p.description || "")}"></label>` +
    `<label class="check"><input type="checkbox" name="private" ${gh.defaultPrivate ? "checked" : ""}> 私有仓库</label>` +
    `<label class="check"><input type="checkbox" name="existing"> 仓库已存在，只连接并推送</label>` +
    (p.gitInfo.remote ? `<p class="hint">已有远端 origin，将添加为远端 <code>github</code>。</p>` : ""), "发布");
  if (!b) return;
  const v = (n) => b.querySelector(`[name=${n}]`);
  toast("正在发布…");
  const r = await api(`projects/${S.pid}/publish`, {
    repoName: v("repo").value.trim(), description: v("desc").value.trim(), private: v("private").checked,
    existing: v("existing").checked, remote: p.gitInfo.remote ? "github" : "origin" });
  toast(`${r.created ? "已新建并推送" : "已推送"}：${r.repo}`);
  await refreshProject(); loadHome();
});

/* ---------------- GitHub connection dialog */
const githubDialog = guard(async () => {
  const st = await api("github");
  const mcp = await api("mcp-config").catch(() => null);
  const grants = st.grants || {};
  const connected = st.connected;
  const head = connected
    ? `<div class="gh-card ${st.ok === false ? "bad" : ""}">${st.avatar ? `<img src="${esc(st.avatar)}" alt="">` : ico("github")}
        <div><b>${esc(st.login || "已连接")}</b><div class="muted small">${{ token: "个人访问令牌（已加密保存）", "git-credential": "复用 Git 的 GitHub 登录", env: "环境变量 GITHUB_TOKEN" }[st.mode] || ""}</div>
        ${st.ok === false ? `<div class="err-text small">${esc(st.error)}</div>` : ""}</div></div>`
    : `<p class="hint">授权后，你可以一键把项目发布到 GitHub；Agent 也能通过 MCP 新建仓库、推送更新（以下权限可随时关闭）。</p>
       <div class="seg"><label><input type="radio" name="mode" value="git-credential" checked> 复用 Git 已登录的账号</label>
       <label><input type="radio" name="mode" value="token"> 粘贴个人访问令牌</label></div>
       <label class="field" data-token hidden>令牌（需要 repo 权限，或细粒度令牌的 Administration + Contents 读写）
         <input name="token" type="password" autocomplete="off" placeholder="ghp_… 或 github_pat_…"></label>
       <p class="hint small"><a href="https://github.com/settings/tokens/new?scopes=repo&description=Project%20Hub" target="_blank">在 GitHub 创建令牌 ↗</a></p>`;
  const body = head + `
    <h4 class="sec">允许 Agent</h4>
    <label class="check"><input type="checkbox" name="createRepo" ${!connected || grants.createRepo ? "checked" : ""}> 新建 GitHub 仓库</label>
    <label class="check"><input type="checkbox" name="push" ${!connected || grants.push ? "checked" : ""}> 推送到 GitHub 仓库</label>
    <label class="check"><input type="checkbox" name="private" ${st.defaultPrivate !== false ? "checked" : ""}> 新建仓库默认私有</label>
    ${mcp ? `<h4 class="sec">在 Agent 中注册 MCP</h4>
      <div class="snip"><span>Claude Code</span><code>${esc(mcp.claude)}</code><button type="button" class="ghost small icon-only" data-copy="${esc(mcp.claude)}">${ico("copy")}</button></div>
      <div class="snip"><span>Codex</span><code>${esc(mcp.codex)}</code><button type="button" class="ghost small icon-only" data-copy="${esc(mcp.codex)}">${ico("copy")}</button></div>` : ""}
    ${connected ? `<p><button type="button" class="ghost small danger-text" data-disconnect>断开 GitHub</button></p>` : ""}`;
  const b = await dialog("GitHub", body, connected ? "保存" : "连接", (el) => {
    el.querySelectorAll("[name=mode]").forEach((r) => (r.onchange = () => (el.querySelector("[data-token]").hidden = r.value !== "token" || !r.checked)));
    el.querySelectorAll("[data-copy]").forEach((btn) => (btn.onclick = () => { navigator.clipboard.writeText(btn.dataset.copy); toast("已复制"); }));
    const dis = el.querySelector("[data-disconnect]");
    if (dis) dis.onclick = guard(async () => {
      if (!confirm("断开后 Agent 将无法新建仓库或推送到 GitHub。继续？")) return;
      await api("github/disconnect", {}); $("#dlg").close(); toast("已断开 GitHub"); refreshGithubDot();
    });
  });
  if (!b) return;
  const v = (n) => b.querySelector(`[name=${n}]`);
  const grantsNew = { createRepo: v("createRepo").checked, push: v("push").checked };
  if (connected) {
    await api("github/grants", { grants: grantsNew, defaultPrivate: v("private").checked });
    toast("已保存 GitHub 权限");
  } else {
    const mode = b.querySelector("[name=mode]:checked").value;
    const r = await api("github/connect", { mode, token: mode === "token" ? v("token").value : undefined,
      grants: grantsNew, defaultPrivate: v("private").checked });
    toast(`已连接 GitHub：${r.login}`);
  }
  refreshGithubDot();
});
async function refreshGithubDot() {
  const st = await api("github?verify=0").catch(() => null);
  $("#gh-dot").className = "gh-dot" + (st?.connected ? " on" : "");
  $("#btn-github").title = st?.connected ? `GitHub：${st.login || "已连接"}` : "连接 GitHub";
}
$("#btn-github").onclick = githubDialog;
refreshGithubDot();

/* ---------------- sidebar multi-select + batch actions */
function toggleSelect(id, range) {
  if (range && lastSelected) {
    const order = $$("#groups .proj-item").map((el) => el.dataset.id);
    const [a, b] = [order.indexOf(lastSelected), order.indexOf(id)].sort((x, y) => x - y);
    if (a >= 0 && b >= 0) order.slice(a, b + 1).forEach((x) => SEL.add(x));
  } else if (SEL.has(id)) SEL.delete(id); else SEL.add(id);
  lastSelected = id;
  renderSidebar(); renderBatchBar();
}
function clearSelection() { SEL.clear(); lastSelected = null; renderSidebar(); renderBatchBar(); }
function renderBatchBar() {
  $("#batch-bar").hidden = !SEL.size;
  $("#batch-count").textContent = SEL.size;
}
$("#batch-clear").onclick = clearSelection;
$("#batch-group").onclick = guard(async () => {
  const b = await dialog(`移动 ${SEL.size} 个项目`, groupField("default"), "移动", wireGroup);
  if (!b) return;
  const gid = b.querySelector("[name=group]").value;
  const group = gid === "__new" ? b.querySelector("[name=newgroup]").value.trim() : gid;
  await api("batch", { projects: [...SEL].map((x) => "id:" + x), group });
  toast(`已移动 ${SEL.size} 个项目`); clearSelection(); await loadRegistry();
});
$("#batch-tag").onclick = guard(async () => {
  const b = await dialog(`为 ${SEL.size} 个项目编辑标签`,
    `<label class="field">添加标签（空格或逗号分隔）<input name="add" placeholder="例如：客户端 待发布"></label>
     <label class="field">移除标签<input name="remove"></label>`, "应用");
  if (!b) return;
  const split = (x) => x.split(/[\s,，]+/).filter(Boolean);
  await api("batch", { projects: [...SEL].map((x) => "id:" + x), addTags: split(b.querySelector("[name=add]").value),
    removeTags: split(b.querySelector("[name=remove]").value) });
  toast("已更新标签"); clearSelection(); await loadRegistry();
});
$("#batch-pin").onclick = guard(async () => {
  const allPinned = [...SEL].every((id) => S.reg.projects.find((p) => p.id === id)?.pinned);
  await api("batch", { projects: [...SEL].map((x) => "id:" + x), pinned: !allPinned });
  toast(allPinned ? "已取消置顶" : "已置顶"); clearSelection(); await loadRegistry();
});

/* ---------------- command palette: projects + code search across projects */
const PAL = { mode: "projects", items: [], index: 0, seq: 0 };
function openPalette(mode = "projects") {
  $("#palette").hidden = false;
  setPalMode(mode);
  $("#pal-q").focus(); $("#pal-q").select();
  runPalette();
}
function closePalette() { $("#palette").hidden = true; }
function setPalMode(mode) {
  PAL.mode = mode;
  $("#pal-mode").textContent = mode === "code" ? "代码" : "项目";
  $("#pal-mode").classList.toggle("code", mode === "code");
  $("#pal-opts").hidden = mode !== "code";
  $("#pal-q").placeholder = mode === "code" ? "在所有项目中搜索代码…  回车搜索" : "项目名、标签、备注…  Tab 切换到代码搜索";
}
let palTimer = 0;
function runPalette(now = false) {
  clearTimeout(palTimer);
  if (PAL.mode === "code" && !now) return;  // code search runs on Enter
  palTimer = setTimeout(guard(async () => {
    const q = $("#pal-q").value.trim();
    const seq = ++PAL.seq;
    if (PAL.mode === "projects") {
      const res = await api("search?" + qs({ q }));
      if (seq !== PAL.seq) return;
      PAL.items = res.map((p) => ({ kind: "project", p }));
      $("#pal-results").innerHTML = res.length ? res.map((p, i) =>
        `<div class="pal-item" data-i="${i}">${avatar(p)}<div class="pal-main"><b>${esc(p.name)}</b>` +
        `<span class="muted small">${esc(p.groupName)} · ${esc(p.path)}</span></div>` +
        `<div class="pal-tags">${(p.tags || []).slice(0, 3).map((t) => tagChip(t, true)).join("")}${p.fuzzy ? '<span class="st-chip">模糊</span>' : ""}</div></div>`).join("")
        : `<div class="pal-empty">${q ? `没有匹配「${esc(q)}」的项目。按 Tab 改为在代码中搜索。` : "还没有登记项目"}</div>`;
    } else {
      if (!q) { $("#pal-results").innerHTML = `<div class="pal-empty">输入要找的文字，回车搜索所有项目的文件内容</div>`; PAL.items = []; return; }
      $("#pal-stat").textContent = "搜索中…";
      const r = await api("grep?" + qs({ pattern: q, regex: $("#pal-regex").checked ? 1 : "", case: $("#pal-case").checked ? 1 : "",
        glob: $("#pal-glob").value.trim(), max: 20 }));
      if (seq !== PAL.seq) return;
      $("#pal-stat").textContent = `${r.total} 处 · ${r.matched}/${r.projects} 个项目 · ${r.seconds}s`;
      PAL.items = [];
      let html = "";
      for (const res of r.results) {
        html += `<div class="pal-group">${esc(res.name)} <span class="muted small">${res.hits.length}${res.truncated ? "+" : ""}</span>${res.error ? `<span class="err-text small"> ${esc(res.error)}</span>` : ""}</div>`;
        for (const hit of res.hits) {
          const i = PAL.items.push({ kind: "hit", id: res.id, hit }) - 1;
          const text = esc(hit.text);
          html += `<div class="pal-item hit" data-i="${i}">${fileIcon(hit.file.split("/").pop())}<code class="pal-file">${esc(hit.file)}:${hit.line}</code><span class="pal-line">${text}</span></div>`;
        }
      }
      $("#pal-results").innerHTML = html || `<div class="pal-empty">没有找到「${esc(q)}」</div>`;
    }
    PAL.index = 0; markPal();
    $$("#pal-results .pal-item").forEach((el) => {
      el.onmouseenter = () => { PAL.index = +el.dataset.i; markPal(false); };
      el.onclick = () => choosePal(+el.dataset.i);
    });
  }), now ? 0 : 120);
}
function markPal(scroll = true) {
  $$("#pal-results .pal-item").forEach((el) => el.classList.toggle("on", +el.dataset.i === PAL.index));
  if (scroll) $(`#pal-results .pal-item[data-i="${PAL.index}"]`)?.scrollIntoView({ block: "nearest" });
}
const choosePal = guard(async (i) => {
  const it = PAL.items[i];
  if (!it) return;
  closePalette();
  if (it.kind === "project") return selectProject(it.p.id);
  if (S.pid !== it.id) await selectProject(it.id);
  if (S.pid !== it.id) return;
  switchTab("source");
  await openFile(it.hit.file, it.hit.line);
});
$("#btn-palette").onclick = () => openPalette();
$("#palette").onclick = (e) => { if (e.target.id === "palette") closePalette(); };
$("#pal-q").oninput = () => runPalette();
["#pal-regex", "#pal-case"].forEach((x) => ($(x).onchange = () => runPalette(true)));
$("#pal-glob").onkeydown = (e) => { if (e.key === "Enter") runPalette(true); };
$("#pal-q").onkeydown = (e) => {
  if (e.key === "Escape") { closePalette(); return; }
  if (e.key === "Tab") { e.preventDefault(); setPalMode(PAL.mode === "code" ? "projects" : "code"); runPalette(true); return; }
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const n = PAL.items.length;
    if (n) { PAL.index = (PAL.index + (e.key === "ArrowDown" ? 1 : n - 1)) % n; markPal(); }
    return;
  }
  if (e.key === "Enter") {
    e.preventDefault();
    if (PAL.mode === "code" && (!PAL.items.length || PAL.lastQ !== $("#pal-q").value)) { PAL.lastQ = $("#pal-q").value; runPalette(true); }
    else choosePal(PAL.index);
  }
};
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
  if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "f") { e.preventDefault(); openPalette("code"); }
  if (e.key === "Escape" && !$("#palette").hidden) closePalette();
  if (e.key === "Escape" && SEL.size && $("#palette").hidden) clearSelection();
});
