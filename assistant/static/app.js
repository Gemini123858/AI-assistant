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
  if (response.status === 401) { window.location.assign("/login"); throw new Error("请登录。"); }
  if (!response.ok) {
    const error = new Error(data.error || "请求失败");
    error.policy = data.policy;
    throw error;
  }
  return data;
}

async function working(button, action) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  try { await action(); }
  catch (error) {
    if (error.policy?.overridable && error.policy.read_exception) {
      const field = $("task-read-exceptions");
      try {
        const entries = JSON.parse(field.value.trim() || "[]");
        if (Array.isArray(entries)) {
          const entry = error.policy.read_exception;
          if (!entries.some((other) => JSON.stringify(other) === JSON.stringify(entry))) entries.push(entry);
          field.value = JSON.stringify(entries, null, 2);
          field.closest("details").open = true;
          $("task-public-confirm").checked = false;
          error.message += " 已填入候选阅读豁免，尚未放行；请核实后重新确认。";
        }
      } catch (_) { /* Preserve the user's invalid JSON; never grant silently. */ }
    }
    toast(error.message, true);
  }
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
    "真实模型已启用。明确分析、问答及网页任务调用 API；失败也计入额度。邮箱后台只同步新邮件，不自动分析；普通问答没有浏览工具，网页任务不登录、不投递，没有推送或邮件发送。" :
    "当前为演示模式：只验证界面、文件保存和显式记忆命令，不判断真实相关性或日期。配置 API 后重启，可进行真实分析和问答。";
  $("task-button").disabled = !status.capabilities.browser_tasks;
  const budget = status.browser_budget;
  $("task-availability").textContent = status.capabilities.browser_tasks ?
    `本任务配置 ${budget.calls} 次模型调用 / ${budget.steps} 步 / ${Math.round(budget.seconds / 60)} 分钟；受日剩余额度限制，当前有效模型预算 ${budget.effective_calls} 次。视觉截图${status.capabilities.browser_vision ? "已开启（仅匿名页，输入遮罩）" : "未开启"}。一次仅运行一个任务。` :
    "演示模式不执行网页任务，不伪造招聘查询。请配置 API 和 Chromium 后重启。";
}

function sourceLink(url, label, domains = ["jobs.bytedance.com"]) {
  const link = node("a", "", label);
  let target;
  try { target = new URL(url); } catch (_) { return link; }
  // A stored/model-produced link is still untrusted; never render javascript: URLs.
  if (target.protocol === "https:" && (domains === null || domains.includes(target.hostname)) && (!target.port || target.port === "443") && !target.username && !target.password) {
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
  const domains = task.scope?.public_web ? null : task.scope?.domains || ["jobs.bytedance.com"];
  top.append(node("h3", "", task.request), node("span", "tag", names[task.status] || "未知"));
  card.append(top, node("p", "reason", task.note), node("p", "caption",
    `已保存 ${task.pages.length} 份页面观察 · 动作 ${task.steps}/${task.limits.steps} · 模型 ${task.calls}/${task.limits.calls} · ${task.phase === "analysis" ? "私人分析（无网站动作）" : "公开浏览"} · ${formatTime(task.created_at)}`));
  if (task.scope) card.append(node("p", "caption", domains === null ?
    "阅读范围：任务相关的公网 HTTPS 网页与数据（不授权外部写入）" : "限定访问域：" + domains.join("、")));
  if (task.public_request) card.append(node("p", "caption", "公开任务目标：" + task.public_request));
  if (task.private_conditions) card.append(node("p", "caption", "私人筛选条件：" + task.private_conditions));
  if (task.scope?.read_post_urls?.length) card.append(node("p", "caption", "历史 POST 授权记录（新策略不再依赖）：" + task.scope.read_post_urls.join("、")));
  if (task.scope?.blocked_urls?.length) card.append(node("p", "caption", "禁止访问路径：" + task.scope.blocked_urls.join("、")));
  if (task.scope?.read_exceptions?.length) card.append(node("p", "caption", `本任务精确阅读豁免：${task.scope.read_exceptions.length} 项（不覆盖硬边界）`));
  function exceptionButton(policy) {
    if (!policy?.overridable || !policy.read_exception) return null;
    const button = node("button", "quiet", "填入阅读豁免（需核实后重新发起）");
    button.addEventListener("click", () => {
      const scope = task.scope;
      $("task-url").value = scope.start_url;
      $("task-limit-domains").checked = !scope.public_web;
      $("task-domains").value = scope.domains.join(", ");
      $("task-resources").value = scope.resource_domains.join(", ");
      $("task-blocked-urls").value = (scope.blocked_urls || []).join("\n");
      const entries = [...(scope.read_exceptions || []), policy.read_exception];
      $("task-read-exceptions").value = JSON.stringify(entries.filter((entry, index) =>
        entries.findIndex((other) => JSON.stringify(other) === JSON.stringify(entry)) === index), null, 2);
      $("task-read-exceptions").closest("details").open = true;
      $("task-public-request").value = task.public_request;
      $("task-request").value = task.private_conditions ?? (task.request !== task.public_request ? task.request : "");
      $("task-public-confirm").checked = false;
      $("task-form").scrollIntoView({ behavior: "smooth" });
      toast("已填入新任务，尚未放行。请核实具体URL、方法及POST正文摘要后重新确认。");
    });
    return button;
  }
  for (const error of task.errors || []) {
    const button = exceptionButton(error.policy);
    if (button) card.append(node("p", "notice", error.policy.reason),
      node("pre", "task-evidence", JSON.stringify(error.policy.read_exception, null, 2)), button);
  }
  if (task.actions?.length) {
    const actions = node("details", "task-actions");
    actions.append(node("summary", "", "查看浏览器动作记录"));
    const labels = { open_link: "打开链接", open_url: "打开 URL", observe_page: "观察页面", click_element: "点击元素", fill_field: "填写搜索框", scroll_page: "滚动页面", go_back: "返回", refresh_page: "刷新", wait_page: "等待", press_key: "按键", select_option: "选择筛选项", check_field: "勾选筛选", restart_browser: "重启匿名浏览器", take_screenshot: "截图（输入已遮罩）", read_source: "读取保存证据" };
    const states = { running: "执行中", success: "动作已执行", incomplete: "正文尚不可读", failed: "失败", interrupted: "已中止，结果未验证" };
    for (const action of task.actions) {
      actions.append(node("p", "caption", `${action.step}. ${labels[action.tool] || action.tool} · ${states[action.status] || action.status}${action.page_id ? " · " + action.page_id : ""}${action.error_kind ? " · " + action.error_kind : ""}${action.error ? " · " + action.error : ""}`));
      if (action.read_error) actions.append(node("p", "notice", action.read_error));
      for (const item of action.network_errors || []) actions.append(node("p", "caption", `允许请求的网络失败：${item.target} · ${item.code}`));
      for (const item of action.connection_errors || []) actions.append(node("p", "caption", `${item.kind === "permission" ? "公网连接被拒绝" : "连接失败"}：${item.target} · ${item.reason}`));
      if (action.loop_warning) actions.append(node("p", "caption", action.loop_warning));
      if (action.remaining) actions.append(node("p", "caption", `动作结束时剩余：模型 ${action.remaining.calls} 次 / 动作 ${action.remaining.steps} 步 / 时间 ${action.remaining.seconds} 秒`));
    }
    card.append(actions);
  }
  if (task.result) {
    card.append(node("p", "summary-text", task.result.summary), node("p", "notice", task.result.coverage));
    for (const finding of task.result.findings || []) {
      const result = node("section", "job-result");
      result.append(node("p", "summary-text", finding.text), sourceLink(finding.url, `${finding.page_id} · 原来源`, domains),
        node("p", "task-evidence", `原文（${finding.line_start}–${finding.line_end}行）：\n${finding.quote}`));
      if (finding.uncertainties.length) result.append(node("p", "notice", "未知项：" + finding.uncertainties.join("；")));
      card.append(result);
    }
    for (const job of task.result.jobs || []) {
      const result = node("section", "job-result");
      const fit = { potential_match: "潜在匹配", needs_confirmation: "待确认", not_match: "当前不匹配" }[job.fit];
      result.append(sourceLink(job.url, job.title, domains), node("span", "tag", fit), node("p", "reason", job.reason),
        node("p", "caption", "获取时间：" + formatTime(job.fetched_at)),
        node("p", "task-evidence", "岗位原文：\n" + job.job_evidence));
      if (job.profile_evidence.length) result.append(node("p", "reason", "画像依据：" + job.profile_evidence.join("；")));
      if (job.uncertainties.length) result.append(node("p", "notice", "未知项：" + job.uncertainties.join("；")));
      card.append(result);
    }
    if (task.result.questions.length) card.append(node("p", "notice", "需要你补充：" + task.result.questions.join("；")));
  }
  for (const error of task.errors) {
    card.append(node("p", "notice", `历史失败/恢复线索${error.step ? "（步骤" + error.step + "）" : ""}：${error.kind || "读取失败"} · ${error.url} · ${error.error}`));
    for (const item of error.network_errors || []) card.append(node("p", "caption", `允许请求的网络失败：${item.target} · ${item.code}`));
    for (const item of error.policy_blocks || []) card.append(node("p", "caption", `主动拦截（不是网络故障）：${item.target} · ${item.reason}`));
    for (const item of error.connection_errors || []) card.append(node("p", "caption", `${item.kind === "permission" ? "公网连接被拒绝" : "连接失败"}：${item.target} · ${item.reason}`));
  }
  const evidence = node("details", "task-pages");
  evidence.append(node("summary", "", "查看已访问页面及保存的正文"));
  for (const page of task.pages) {
    const row = node("p");
    row.append(sourceLink(page.url, `${page.id} · ${page.title || page.url}${page.ready === false ? " · 正文尚不可读" : " · 快照可读，内容需核对"}`, domains));
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
      if (page.ready === false) evidence.append(node("p", "notice", page.read_error || "正文尚不可读，不能当成零结果。"));
      if (page.content_note) evidence.append(node("p", "caption", page.content_note));
      if (page.read_warning) evidence.append(node("p", "notice", page.read_warning));
      for (const item of page.network_errors || []) evidence.append(node("p", "caption", `网络失败：${item.target} · ${item.code}`));
      for (const item of page.connection_errors || []) evidence.append(node("p", "caption", `${item.kind === "permission" ? "公网连接被拒绝" : "连接失败"}：${item.target} · ${item.reason}`));
      for (const item of page.policy_blocks || []) {
        evidence.append(node("p", "caption", `主动拦截：${item.target} · ${item.rule || "历史规则"} · ${item.reason}`));
        const button = exceptionButton(item);
        if (button) evidence.append(node("pre", "task-evidence", JSON.stringify(item.read_exception, null, 2)), button);
      }
    }
  }));
  evidence.append(view);
  card.append(evidence);
  const active = ["queued", "running"].includes(task.status);
  const oldScope = task.scope && !Object.hasOwn(task.scope, "public_web");
  const button = node("button", "quiet", active ? "取消任务" : oldScope ? "按新阅读策略重新发起" : "重新发起（重新计费）");
  button.disabled = active && task.cancel_requested;
  button.addEventListener("click", () => working(button, async () => {
    if (!active && oldScope) {
      $("task-url").value = task.scope.start_url;
      $("task-limit-domains").checked = false;
      $("task-domains").value = "";
      $("task-resources").value = "";
      $("task-blocked-urls").value = (task.scope.blocked_urls || []).join("\n");
      $("task-read-exceptions").value = JSON.stringify(task.scope.read_exceptions || [], null, 2);
      $("task-public-request").value = task.public_request;
      $("task-request").value = task.private_conditions ?? (task.request !== task.public_request ? task.request : "");
      $("task-public-confirm").checked = false;
      $("task-form").scrollIntoView({ behavior: "smooth" });
      toast("已填入新任务并关闭旧域名白名单；请检查目标并确认开始。旧记录不变，尚未调用 API。");
      return;
    }
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
    const start = new URL($("task-url").value);
    const splitDomains = (id) => $(id).value.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean);
    const domains = [...new Set([start.hostname, ...splitDomains("task-domains")])];
    await api("/api/tasks", "POST", { goal: $("task-public-request").value,
      private_conditions: $("task-request").value, start_url: start.href,
      public_web: !$("task-limit-domains").checked,
      domains, resource_domains: splitDomains("task-resources"),
      read_exceptions: JSON.parse($("task-read-exceptions").value.trim() || "[]"),
      blocked_urls: $("task-blocked-urls").value.split(/\n/).map((s) => s.trim()).filter(Boolean) });
    toast("已发起公开网页任务，可查看动作、恢复记录或取消。");
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
  $("mail-panel").hidden = tab.dataset.tab !== "mail";
}));
$("refresh-button").addEventListener("click", () => working($("refresh-button"), () => Promise.all([loadMessages(), loadStatus()])));
$("previous-page").addEventListener("click", async () => { offset = Math.max(0, offset - pageSize); await loadMessages().catch((e) => toast(e.message, true)); });
$("next-page").addEventListener("click", async () => { offset += pageSize; await loadMessages().catch((e) => toast(e.message, true)); });
$("close-dialog").addEventListener("click", () => $("source-dialog").close());

const current = new Date();
$("sent-at").value = new Date(current.getTime() - current.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
Promise.all([loadMessages(), loadStatus(), loadTasks(), loadChat(), loadMail(), api("/api/memory").then(refreshMemory)])
  .catch((error) => toast(error.message, true));

function mailSource(record) {
  $("source-meta").textContent = `${record.subject} · 发件人 ${record.sender} · 邮件日期 ${record.date_raw || "未知"} · 同步 ${formatTime(record.synced_at)}`;
  $("source-body").textContent = record.text || "暂无可读取文字。";
  $("source-attachments").textContent = `附件未分析：${record.attachments.join("、") || "未发现"}。${record.body_truncated ? "正文超过大小限制，内容不完整。" : "聊天原时间不能由邮件转发日期替代。"}`;
  $("source-id").textContent = record.id;
  $("source-dialog").showModal();
}

function mailCard(record) {
  const card = node("article", "message-card");
  card.append(node("h3", "", record.subject), node("p", "caption", `${record.sender || "未知发件人"} · ${record.date_raw || "日期未知"}${record.self_forwarded_candidate ? " · 疑似自己转发（不证明每位发言人是你）" : ""}`));
  if (record.triage) card.append(node("p", "notice", `${record.triage.decision}：${record.triage.reason}`));
  if (record.error) card.append(node("p", "notice", record.error));
  if (record.analysis) {
    card.append(node("p", "summary-text", record.analysis.summary));
    card.append(node("p", "caption", `仅分析邮件文字；附件和链接页面尚未覆盖。${record.body_truncated ? "正文受大小限制截断。" : ""}`));
    for (const text of record.analysis.uncertainties) card.append(node("p", "caption", `待确认：${text}`));
    for (const link of record.analysis.link_requests) {
      card.append(node("p", "caption", `建议查看：${link.url}；${link.reason}`));
      const browse = node("button", "quiet", "到网页任务确认此链接");
      browse.addEventListener("click", () => {
        $("task-url").value = link.url;
        $("task-limit-domains").checked = false;
        $("task-domains").value = ""; $("task-resources").value = ""; $("task-blocked-urls").value = ""; $("task-read-exceptions").value = "";
        $("task-public-request").value = "读取这个公开链接，整理页面中的重要事项与原文来源。不要填写个人信息。";
        $("task-request").value = "";
        $("task-public-confirm").checked = false;
        document.querySelector('[data-tab="tasks"]').click();
        toast("请检查链接（尤其参数中的私有令牌）和公开说明，确认访问范围后再开始。不会自动访问。");
      });
      card.append(browse);
    }
  }
  for (const [index, update] of record.profile_updates.entries()) {
    card.append(node("p", "notice", `画像${update.applied ? "已补充" : "建议待确认"}：${update.text}；${update.reason}`));
    card.append(node("p", "caption", `来源：${update.evidence}`));
    if (!update.applied) {
      const confirm = node("button", "quiet", "确认属于我并保存画像");
      confirm.addEventListener("click", () => working(confirm, async () => {
        await api(`/api/mail/${record.id}/profile/${index}/confirm`, "POST", {});
        await loadMail(); refreshMemory(await api("/api/memory"));
      }));
      card.append(confirm);
    }
  }
  const actions = node("div", "card-actions");
  const read = node("button", "quiet", "只读查看正文");
  read.addEventListener("click", () => working(read, async () => mailSource((await api(`/api/mail/${record.id}/read`, "POST", {})).mail)));
  const analyze = node("button", "primary", record.analysis ? "重新分析（调用模型）" : "细读分析（调用模型）");
  analyze.addEventListener("click", () => working(analyze, async () => {
    await api(`/api/mail/${record.id}/analyze`, "POST", {});
    await Promise.all([loadMail(), loadStatus()]); refreshMemory(await api("/api/memory"));
  }));
  actions.append(read, analyze); card.append(actions);
  return card;
}

async function loadMail() {
  const data = await api("/api/mail");
  const status = data.status;
  $("mail-status").textContent = status.enabled ? `${status.started ? "新邮件同步已启用" : "尚未启用；不会读取历史"} · 检查间隔 ${status.poll_seconds} 秒 · ${status.error || "后台不调用模型"}` : "未配置：在私有 .env 填写 QQ_IMAP_USER、QQ_IMAP_AUTH_CODE 和 ASSISTANT_ACCESS_TOKEN，重启后启用。不要把授权码贴到网页/聊天。";
  $("mail-start").disabled = !status.enabled || status.started;
  $("mail-sync").disabled = !status.enabled || !status.started;
  $("mail-latest").disabled = !status.enabled;
  $("mail-triage").disabled = !status.enabled;
  $("mail-count").textContent = `已保存 ${data.total} 封；显示最近100封，不含启用前历史。`;
  $("mail-list").replaceChildren(...data.mails.map(mailCard));
  if (!data.mails.length) $("mail-list").append(node("p", "caption", "启用同步后发送一封新测试邮件，再点击同步。"));
  const table = node("table");
  const head = node("thead"), row = node("tr");
  for (const label of ["事项", "重要性 / 相关性", "时间类型 / 时间", "需要做什么", "出处 / 待确认"]) row.append(node("th", "", label));
  head.append(row); table.append(head);
  const body = node("tbody");
  for (const mail of data.mails) for (const event of mail.analysis?.events || []) {
    const line = node("tr");
    const kind = { deadline: "截止", event: "活动", other: "其他/未知" }[event.time_kind];
    for (const value of [event.title, `${event.importance}：${event.relevance}`, `${kind}：${event.normalized || event.time_text || "待确认"}`, event.action || "无需动作", `${mail.subject}\n原文：${event.evidence}\n${event.uncertainties.join("；")}`]) line.append(node("td", "", value));
    body.append(line);
  }
  table.append(body); $("mail-events").replaceChildren(table);
  if (!body.children.length) $("mail-events").append(node("p", "caption", "分析后显示事件；尚未分析不代表没有重要事项。"));
}

for (const [id, path] of [["mail-start", "start"], ["mail-sync", "sync"], ["mail-triage", "triage"]]) {
  $(id).addEventListener("click", () => working($(id), async () => {
    await api(`/api/mail/${path}`, "POST", {}); await Promise.all([loadMail(), loadStatus()]);
  }));
}
$("mail-latest").addEventListener("click", () => working($("mail-latest"), async () => {
  if (!window.confirm("只读导入收件箱最新一封邮件（可能是历史邮件），不自动调用模型、不改变新邮件同步基线。继续？")) return;
  await api("/api/mail/latest", "POST", {confirm: true});
  await loadMail();
}));
$("mail-refresh").addEventListener("click", () => working($("mail-refresh"), loadMail));
$("logout-button")?.addEventListener("click", async () => { await api("/api/logout", "POST", {}); window.location.assign("/login"); });
