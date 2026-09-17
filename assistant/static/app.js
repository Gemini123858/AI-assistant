"use strict";

const $ = (id) => document.getElementById(id);
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const selected = new Set();
let offset = 0;
let total = 0;
const pageSize = 20;
let toastTimer;
const savedMemory = { profile: "", rules: "" };

function refreshMemory(memory) {
  for (const kind of ["profile", "rules"]) {
    const field = $(kind + "-text");
    if (field.value === savedMemory[kind]) field.value = memory[kind];
    savedMemory[kind] = memory[kind];
  }
}

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function toast(text, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = text;
  $("toast").className = error ? "toast error" : "toast";
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 10000 : 4500);
}

async function api(path, method = "GET", payload) {
  const options = { method, headers: { "X-CSRF-Token": csrf } };
  if (payload !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

async function working(button, action) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  try { await action(); }
  catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = label; }
}

function formatTime(value) {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

function contextLabel() {
  $("chat-context").textContent = selected.size ?
    `对话使用已选择的 ${selected.size} 条通知和最新记忆；模型读取最近 6 轮。` :
    "未选择时使用最近 10 条通知；完整历史保存，模型仅读取最近 6 轮。";
}

function sourceDialog(record) {
  const original = record.original;
  $("source-meta").textContent = `${original.source} · ${original.conversation} · ${original.sender} · ${formatTime(original.sent_at)}`;
  $("source-body").textContent = original.text;
  $("source-attachments").textContent = original.attachments.length ?
    "附件未读取：" + original.attachments.join("、") : "本版仅分析文字，未访问链接或读取图片附件。";
  $("source-id").textContent = `通知 ID：${record.id}`;
  if (!$("source-dialog").open) $("source-dialog").showModal();
}

function messageCard(record) {
  const card = node("article", "message-card");
  card.id = "message-" + record.id;
  const top = node("div", "card-top");
  const title = node("div", "card-title");
  const label = node("label");
  const checkbox = node("input");
  checkbox.type = "checkbox";
  checkbox.checked = selected.has(record.id);
  checkbox.setAttribute("aria-label", "选择通知用于对话：" + record.original.conversation);
  checkbox.addEventListener("change", () => {
    if (checkbox.checked && selected.size >= 10) {
      checkbox.checked = false;
      toast("本版对话最多选择 10 条通知。", true);
      return;
    }
    checkbox.checked ? selected.add(record.id) : selected.delete(record.id);
    contextLabel();
  });
  label.append(checkbox, node("span", "", record.original.conversation));
  title.append(label, node("span", "", record.original.source));
  const analysis = record.analysis;
  const category = record.status === "failed" ? "failed" : analysis ? analysis.importance : "pending";
  const names = { urgent: "及时查看", digest: "汇总", review: "待确认", failed: "分析失败", pending: "待分析" };
  top.append(title, node("span", "tag " + category, names[category] || "状态未知"));
  card.append(top, node("p", "summary-text", analysis ? analysis.summary : record.original.text.slice(0, 180)));
  if (analysis) {
    card.append(node("p", "reason", analysis.relevance_reason));
    if (analysis.actions.length) {
      const list = node("ul", "item-list");
      analysis.actions.forEach((action) => list.append(node("li", "", action)));
      card.append(list);
    }
    analysis.times.forEach((time) => {
      const kind = { deadline: "截止", event: "活动", other: "其他时间" }[time.kind];
      card.append(node("p", "time-row", `${kind}：${time.text}${time.normalized ? " · " + formatTime(time.normalized) : "（精确时间待确认）"}`));
    });
    if (analysis.uncertainties.length) card.append(node("p", "reason", "待确认：" + analysis.uncertainties.join("；")));
    card.append(node("p", "caption", "分析模式：" + (record.analysis_context?.mode === "api" ? record.analysis_context.model : "演示，非真实语义结果")));
  }
  if (record.error) card.append(node("p", "notice", record.error));
  if (record.original.attachments.length) card.append(node("p", "notice", "有附件未处理，不代表没有重要事项。"));
  const actions = node("div", "card-actions");
  const view = node("button", "quiet", "查看原文");
  view.addEventListener("click", () => sourceDialog(record));
  const analyze = node("button", "primary", record.status === "pending" ? "分析通知" : "重新分析");
  analyze.addEventListener("click", () => working(analyze, async () => {
    await api(`/api/messages/${record.id}/analyze`, "POST", { retry: record.status !== "pending" });
    await Promise.all([loadMessages(), loadStatus()]);
    toast("分析结果已保存。");
  }));
  actions.append(view, analyze, node("span", "source-time", formatTime(record.original.sent_at)));
  card.append(actions);
  return card;
}

async function loadMessages() {
  const data = await api(`/api/messages?offset=${offset}&limit=${pageSize}`);
  total = data.total;
  $("message-count").textContent = String(total);
  $("message-list").replaceChildren(...data.messages.map(messageCard));
  if (!data.messages.length) $("message-list").append(node("div", "empty-state", "还没有通知。先导入一条，或载入合成样例。"));
  $("previous-page").disabled = offset === 0;
  $("next-page").disabled = offset + pageSize >= total;
  $("page-info").textContent = total ? `${offset + 1}–${Math.min(offset + pageSize, total)} / ${total}` : "暂无通知";
}

async function loadStatus() {
  const status = await api("/api/status");
  $("mode-badge").textContent = status.mode === "api" ? "真实 API 模式" : "演示模式";
  $("model-name").textContent = status.model;
  $("usage").textContent = `今日 API 尝试 ${status.usage.calls} / ${status.daily_limit}`;
  $("mode-notice").textContent = status.mode === "api" ?
    "真实模型已启用。分析、普通问答和主动发起的网页任务会调用 API，并使用记忆；失败尝试也计入额度。普通问答没有浏览工具，网页任务不登录、不投递。当前没有自动采集、推送或邮件发送。" :
    "当前为演示模式：只验证界面、文件保存和显式记忆命令，不判断真实相关性或日期。配置 API 后重启，可进行真实分析和问答。";
  $("task-button").disabled = !status.capabilities.browser_tasks;
  $("task-availability").textContent = status.capabilities.browser_tasks ?
    "当前支持一个串行网页任务；期间通知分析或普通问答可能提示稍后重试。" :
    "演示模式不执行网页任务，不伪造招聘查询。请配置 API 和 Chromium 后重启。";
}

function sourceLink(url, label) {
  const link = node("a", "", label);
  let target;
  try { target = new URL(url); } catch (_) { return link; }
  // A stored/model-produced link is still untrusted; never render javascript: URLs.
  if (target.protocol === "https:" && target.hostname === "jobs.bytedance.com" && !target.username && !target.password) {
    link.href = target.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
  return link;
}

function taskCard(task) {
  const card = node("article", "message-card task-card");
  card.dataset.taskId = task.id;
  const names = { queued: "待运行", running: "运行中", completed: "已完成", partial: "部分完成",
    failed: "失败", cancelled: "已取消", interrupted: "中断" };
  const top = node("div", "card-top");
  top.append(node("h3", "", task.request), node("span", "tag", names[task.status] || "未知"));
  card.append(top, node("p", "reason", task.note), node("p", "caption",
    `已保存 ${task.pages.length} 份页面观察 · ${task.steps} 次浏览器动作 · 模型尝试 ${task.calls}/${task.limits.calls} · ${formatTime(task.created_at)}`));
  if (task.actions?.length) {
    const actions = node("details", "task-actions");
    actions.append(node("summary", "", "查看浏览器动作记录"));
    const labels = { open_link: "打开链接", observe_page: "观察页面", click_element: "点击元素", fill_field: "填写搜索框", scroll_page: "滚动页面" };
    const states = { running: "执行中", success: "成功", incomplete: "内容未验证", failed: "失败", interrupted: "已中止，结果未验证" };
    for (const action of task.actions) {
      actions.append(node("p", "caption", `${action.step}. ${labels[action.tool] || action.tool} · ${states[action.status] || action.status}${action.page_id ? " · " + action.page_id : ""}${action.error ? " · " + action.error : ""}`));
    }
    card.append(actions);
  }
  if (task.result) {
    card.append(node("p", "summary-text", task.result.summary), node("p", "notice", task.result.coverage));
    for (const job of task.result.jobs) {
      const result = node("section", "job-result");
      const fit = { potential_match: "潜在匹配", needs_confirmation: "待确认", not_match: "当前不匹配" }[job.fit];
      result.append(sourceLink(job.url, job.title), node("span", "tag", fit), node("p", "reason", job.reason),
        node("p", "caption", "获取时间：" + formatTime(job.fetched_at)),
        node("p", "task-evidence", "岗位原文：\n" + job.job_evidence));
      if (job.profile_evidence.length) result.append(node("p", "reason", "画像依据：" + job.profile_evidence.join("；")));
      if (job.uncertainties.length) result.append(node("p", "notice", "未知项：" + job.uncertainties.join("；")));
      card.append(result);
    }
    if (task.result.questions.length) card.append(node("p", "notice", "需要你补充：" + task.result.questions.join("；")));
  }
  for (const error of task.errors) card.append(node("p", "notice", "未覆盖：" + error.url + " · " + error.error));
  const evidence = node("details", "task-pages");
  evidence.append(node("summary", "", "查看已访问页面及保存的正文"));
  for (const page of task.pages) {
    const row = node("p");
    row.append(sourceLink(page.url, `${page.id} · ${page.title || page.url}${page.ready === false ? " · 内容尚未验证" : ""}`));
    evidence.append(row);
  }
  const view = node("button", "quiet", "载入保存的正文");
  view.addEventListener("click", () => working(view, async () => {
    const saved = (await api(`/api/tasks/${task.id}`)).task;
    view.remove();
    for (const page of saved.pages) {
      evidence.append(node("h3", "", page.id + " · " + page.title),
        node("p", "caption", `${formatTime(page.fetched_at)}${page.text_truncated ? " · 正文已截断，覆盖不完整" : ""}`),
        node("pre", "task-evidence", page.text));
      if (page.ready === false) evidence.append(node("p", "notice", page.read_error || "界面观察不等于有效岗位详情。"));
    }
  }));
  evidence.append(view);
  card.append(evidence);
  const active = ["queued", "running"].includes(task.status);
  const button = node("button", "quiet", active ? "取消任务" : "重新发起（重新计费）");
  button.disabled = active && task.cancel_requested;
  button.addEventListener("click", () => working(button, async () => {
    if (!active && !window.confirm("将创建新任务、使用当前画像并重新调用 API；旧记录保留。是否继续？")) return;
    await api(`/api/tasks/${task.id}/${active ? "cancel" : "restart"}`, "POST", {});
    await loadTasks();
  }));
  const actions = node("div", "card-actions");
  actions.append(button);
  card.append(actions);
  return card;
}

let taskPoll;
async function loadTasks() {
  clearTimeout(taskPoll);
  const data = await api("/api/tasks");
  const open = new Set([...$("task-list").querySelectorAll(".task-card")]
    .filter((card) => card.querySelector("details.task-pages")?.open).map((card) => card.dataset.taskId));
  const openActions = new Set([...$("task-list").querySelectorAll(".task-card")]
    .filter((card) => card.querySelector("details.task-actions")?.open).map((card) => card.dataset.taskId));
  const cards = data.tasks.map(taskCard);
  cards.forEach((card) => {
    card.querySelector("details.task-pages").open = open.has(card.dataset.taskId);
    if (card.querySelector("details.task-actions")) card.querySelector("details.task-actions").open = openActions.has(card.dataset.taskId);
  });
  $("task-list").replaceChildren(...cards);
  if (!data.tasks.length) $("task-list").append(node("div", "empty-state", "尚未发起网页任务。后台不会自行启动查询或分析。"));
  if (data.tasks.some((task) => ["queued", "running"].includes(task.status))) {
    taskPoll = setTimeout(() => loadTasks().catch((error) => toast(error.message, true)), 2500);
  } else await loadStatus();
}

$("task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  working($("task-button"), async () => {
    await api("/api/tasks", "POST", { request: $("task-request").value });
    toast("已发起受限只读任务，可查看进度或取消。");
    await loadTasks();
  });
});
$("tasks-refresh").addEventListener("click", () => working($("tasks-refresh"), () => loadTasks()));

function chatTurn(turn) {
  const wrapper = node("div", "chat-turn");
  wrapper.append(node("div", "bubble user", turn.user), node("div", "bubble assistant", turn.reply));
  if (turn.cited_ids?.length) {
    const citations = node("div");
    turn.cited_ids.forEach((key, index) => {
      const button = node("button", "citation-button", `原文 ${index + 1}`);
      button.addEventListener("click", () => working(button, async () => sourceDialog((await api(`/api/messages/${key}`)).message)));
      citations.append(button);
    });
    wrapper.append(citations);
  }
  return wrapper;
}

async function loadChat() {
  const data = await api("/api/chat");
  $("chat-history").replaceChildren(...data.turns.map(chatTurn));
  if (!data.turns.length) $("chat-history").append(node("div", "bubble assistant", "先保存一条通知，再问我需要做什么。也可以先告诉我你的个人信息：/记住 我目前正在找实习。"));
  $("chat-history").scrollTop = $("chat-history").scrollHeight;
}

$("import-form").addEventListener("submit", (event) => {
  event.preventDefault();
  working($("import-button"), async () => {
    const payload = { source: $("source").value, conversation: $("conversation").value,
      sender: $("sender").value || "未知", sent_at: new Date($("sent-at").value).toISOString(),
      message_id: $("message-id").value, text: $("notification-text").value };
    const result = await api("/api/messages", "POST", payload);
    offset = 0;
    await loadMessages();
    toast(result.created ? "通知已保存，请点击分析。" : "该通知已存在，未重复导入。");
  });
});

$("json-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    if (file.size > 100000) throw new Error("JSON 文件超过本版 100KB 上限。");
    const input = JSON.parse(await file.text());
    const batch = Array.isArray(input) ? input : [input];
    if (!batch.length || batch.length > 20) throw new Error("每批请导入 1～20 条通知。");
    let created = 0;
    for (const payload of batch) created += (await api("/api/messages", "POST", payload)).created ? 1 : 0;
    offset = 0;
    await loadMessages();
    toast(`新增 ${created} 条通知，其余已存在。`);
  } catch (error) {
    toast("导入未全部完成，之前成功项已保留，可重复导入重试。" + error.message, true);
    await loadMessages().catch(() => {});
  } finally { event.target.value = ""; }
});

$("sample-button").addEventListener("click", () => working($("sample-button"), async () => {
  const sample = await api("/static/examples.json");
  for (const payload of sample) await api("/api/messages", "POST", payload);
  offset = 0;
  await loadMessages();
  toast("合成样例已载入，尚未调用模型。");
}));

$("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  working($("chat-button"), async () => {
    const payload = { message: $("chat-input").value };
    if (selected.size) payload.message_ids = [...selected];
    await api("/api/chat", "POST", payload);
    $("chat-input").value = "";
    await Promise.all([loadChat(), loadStatus()]);
    const memory = await api("/api/memory");
    // Refresh server changes without overwriting unsaved local edits.
    refreshMemory(memory);
  });
});

document.querySelectorAll(".memory-form").forEach((form) => {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const kind = form.dataset.kind;
    working(form.querySelector("button"), async () => {
      const content = $(kind + "-text").value;
      const memory = await api(`/api/memory/${kind}`, "PUT", { content });
      savedMemory[kind] = memory[kind];
      if ($(kind + "-text").value === content) $(kind + "-text").value = memory[kind];
      toast("已保存并立即生效；旧通知不会自动重新分析。");
    });
  });
});

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((item) => {
    const active = item === tab;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
  });
  $("notifications-panel").hidden = tab.dataset.tab !== "notifications";
  $("memory-panel").hidden = tab.dataset.tab !== "memory";
  $("tasks-panel").hidden = tab.dataset.tab !== "tasks";
}));
$("refresh-button").addEventListener("click", () => working($("refresh-button"), () => Promise.all([loadMessages(), loadStatus()])));
$("previous-page").addEventListener("click", async () => { offset = Math.max(0, offset - pageSize); await loadMessages().catch((e) => toast(e.message, true)); });
$("next-page").addEventListener("click", async () => { offset += pageSize; await loadMessages().catch((e) => toast(e.message, true)); });
$("close-dialog").addEventListener("click", () => $("source-dialog").close());

const current = new Date();
$("sent-at").value = new Date(current.getTime() - current.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
Promise.all([loadMessages(), loadStatus(), loadTasks(), loadChat(), api("/api/memory").then(refreshMemory)])
  .catch((error) => toast(error.message, true));
