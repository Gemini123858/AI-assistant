"use strict";
const $ = (id) => document.getElementById(id);
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const names = { notification: "通知", mail: "邮件", task: "网页任务", draft: "邮件草稿", memory: "画像与规则" };
const toolNames = { search_records: "搜索资料", read_record: "读取资料", save_notification: "保存通知", save_notification_analysis: "保存通知分析", save_mail_analysis: "保存邮件分析", suggest_mail_profile: "提议画像更新", sync_mail: "同步邮件", run_browser: "查询公开网页", read_web_source: "读取网页证据", create_draft: "创建草稿", update_draft: "修改草稿", copy_draft: "复制草稿", update_memory: "更新记忆" };
const statusNames = { draft: "尚未发送", sent: "SMTP 已接受", partial: "部分完成", failed: "失败", unknown: "结果待核查", sending: "发送中", running: "运行中", queued: "待运行", completed: "已完成", cancelled: "已取消", interrupted: "中断", saved: "已保存", pending: "待分析", success: "已分析", analyzed: "已分析" };
let conversation = null, conversations = [], status = null, pollTimer, toastTimer, selection = new Map();
let pickerSelection = new Map(), pickerOffset = 0, previewRef = null;
let opened = null, draft = null, draftBaseVersion = null, draftDirty = false, draftTimer, saveQueue = Promise.resolve();
let loadSequence = 0, pickerSequence = 0, recordSequence = 0;
const refKey = (ref) => `${ref.kind}:${ref.id}`;
const simpleRef = (ref) => ({ kind: ref.kind, id: ref.id });
const formatTime = (value) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "";
function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
}
function button(text, action, className = "quiet") {
  const element = node("button", className, text);
  element.type = "button";
  element.addEventListener("click", () => work(element, action));
  return element;
}
function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").className = error ? "toast error" : "toast";
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 10000 : 4000);
}
async function api(path, method = "GET", payload, inConversation = false) {
  const headers = { "X-CSRF-Token": csrf };
  if (inConversation && conversation) headers["X-Conversation-ID"] = conversation.id;
  const options = { method, headers };
  if (payload !== undefined) { headers["Content-Type"] = "application/json"; options.body = JSON.stringify(payload); }
  const response = await fetch(path, options);
  if (response.status === 401) { window.location.assign("/login"); throw new Error("请重新登录。"); }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "操作失败");
  return data;
}
async function work(element, action) {
  if (element.dataset.busy) return;
  element.dataset.busy = "true";
  element.setAttribute("aria-busy", "true");
  try { await action(); } catch (error) { toast(error.message, true); }
  finally { delete element.dataset.busy; element.removeAttribute("aria-busy"); updateControls(); }
}
function rememberComposer() {
  if (conversation) sessionStorage.setItem(`composer:${conversation.id}`, JSON.stringify({ text: $("chat-input").value, records: [...selection.values()] }));
}
function renderSelection() {
  $("attached-records").replaceChildren(...[...selection.entries()].map(([key, record]) => button(`${names[record.ref.kind]} · ${record.title.slice(0, 55)} ×`, () => {
    selection.delete(key); renderSelection(); rememberComposer();
  })));
}
function addSelected(record) { selection.set(refKey(record.ref), record); renderSelection(); rememberComposer(); }
async function loadList() {
  conversations = (await api("/api/conversations")).conversations;
  $("conversation-list").replaceChildren(...conversations.map((item) => {
    const choice = button(`${item.running ? "● " : ""}${item.title}`, () => selectConversation(item.id), `conversation-choice${item.id === conversation?.id ? " selected" : ""}`);
    choice.setAttribute("aria-current", item.id === conversation?.id ? "true" : "false");
    return choice;
  }));
}
async function newConversation() {
  await flushDraft(); rememberComposer();
  const result = await api("/api/conversations", "POST", {});
  await selectConversation(result.conversation.id);
}
async function selectConversation(id) {
  await flushDraft(); rememberComposer();
  const sequence = ++loadSequence;
  clearTimeout(pollTimer);
  const data = await api(`/api/conversations/${id}`);
  if (sequence !== loadSequence) return;
  conversation = data.conversation;
  localStorage.setItem("current-conversation", id);
  const composer = JSON.parse(sessionStorage.getItem(`composer:${id}`) || "{}");
  $("chat-input").value = composer.text || "";
  selection = new Map((composer.records || []).map((record) => [refKey(record.ref), record]));
  hideRecord(); renderSelection(); renderConversation(true);
  await loadList(); schedulePoll();
}
function recordLinks(records, historical = true) {
  const list = node("div", "record-links");
  const unique = new Map(records.map((record) => [refKey(record.ref), record]));
  for (const record of unique.values()) {
    const choice = button("", () => openRecord(record.ref));
    choice.className = "record-link";
    choice.append(node("span", "", `${names[record.ref.kind]} · ${record.title}`), node("small", "", `${historical && record.ref.version ? "当时 v" + record.ref.version + " · " : ""}打开当前记录`));
    list.append(choice);
  }
  return list;
}
function renderConversation(forceScroll = false) {
  $("conversation-title").textContent = conversation.title;
  const history = $("chat-history"), nearBottom = history.scrollHeight - history.scrollTop - history.clientHeight < 100;
  const expanded = new Set([...history.querySelectorAll("details[open]")].map((element) => element.dataset.seq));
  const entries = [], groups = new Map();
  for (const event of conversation.events) {
    if (event.type === "model" || event.type === "record_update") continue;
    if (event.type === "tool_result" && event.output.finished) continue;
    if (event.type === "tool_result" || event.type === "progress") {
      let group = groups.get(event.turn_id);
      if (!group) {
        const wrapper = node("section", "chat-entry"), details = node("details", "execution"), summary = node("summary"), results = node("div");
        details.dataset.seq = event.turn_id; details.open = expanded.has(event.turn_id); details.append(summary); wrapper.append(details, results);
        group = { details, summary, results, snapshots: new Map(), count: 0 }; groups.set(event.turn_id, group); entries.push(wrapper);
      }
      if (event.type === "tool_result") {
        const model = [...conversation.events].reverse().find((item) => item.seq < event.seq && item.type === "model" && item.message.tool_calls.some((call) => call.id === event.call_id));
        const call = model?.message.tool_calls.find((item) => item.id === event.call_id);
        group.count += 1;
        const detail = node("details");
        detail.append(node("summary", "", `${event.output.failed ? "未完成" : "已执行"} · ${toolNames[call?.function.name] || "操作"}`), node("pre", "", JSON.stringify(event.output, null, 2)));
        group.details.append(detail);
        if (event.output.failed) group.details.append(node("p", "notice", event.output.error));
        if (event.output.matches) group.details.append(recordLinks(event.output.matches, false));
      } else group.details.append(node("p", "", event.text));
      group.summary.textContent = `执行过程 · ${group.count} 项操作`;
      for (const snapshot of event.records || event.output?.records || []) group.snapshots.set(refKey(snapshot.ref), snapshot);
      group.results.replaceChildren(recordLinks([...group.snapshots.values()]));
      continue;
    }
    const entry = node("section", `chat-entry ${event.type === "user" ? "user" : event.type === "answer" ? "answer" : event.type === "state" ? "state" : "execution"}`);
    if (["user", "answer", "state", "user_action", "progress"].includes(event.type)) entry.append(node("div", event.type === "answer" ? "answer-text" : "", event.text));
    const group = event.type === "answer" ? groups.get(event.turn_id) : null;
    if (group) group.results.replaceChildren();
    const records = [...(group ? group.snapshots.values() : []), ...(event.records || [])];
    if (records.length) entry.append(recordLinks(records));
    entries.push(entry);
  }
  if (!entries.length) {
    const welcome = node("div", "welcome");
    welcome.append(node("h2", "", "今天想处理什么？"), node("p", "", "直接提出要求，或添加邮件、通知与已有结果。查资料、分析和起草邮件，都可以在这里继续。"));
    entries.push(welcome);
  }
  history.replaceChildren(...entries);
  if (forceScroll || nearBottom) history.scrollTop = history.scrollHeight;
  updateControls();
}
function updateControls() {
  const active = conversation?.turns.find((turn) => turn.status === "running");
  $("chat-button").disabled = !conversation || !!active;
  $("stop-turn").hidden = !active;
  $("delete-conversation").disabled = !conversation || !!active;
  $("rename-conversation").disabled = !conversation;
  $("run-status").hidden = !active;
  if (active) {
    const note = active.note.replace(/正在执行：(\w+)/, (_, name) => "正在" + (toolNames[name] || "处理"));
    $("run-status").textContent = `${note} · 模型调用 ${active.calls} · 已用时 ${Math.max(0, Math.floor((Date.now() - new Date(active.created_at)) / 1000))} 秒`;
  }
}
function schedulePoll(delay) {
  clearTimeout(pollTimer);
  if (!conversation) return;
  pollTimer = setTimeout(() => poll().catch((error) => { toast(error.message, true); schedulePoll(5000); }), delay || (conversation.turns.some((turn) => turn.status === "running") ? 1500 : 6000));
}
async function poll() {
  if (!conversation) return;
  const id = conversation.id;
  const data = await api(`/api/conversations/${id}/events?after=${conversation.events.length}`);
  if (conversation?.id !== id) return;
  const wasRunning = conversation.turns.some((turn) => turn.status === "running");
  conversation.events.push(...data.events); conversation.turns = data.turns; conversation.title = data.title;
  if (data.events.length) renderConversation(); else updateControls();
  if (data.live_task) $("run-status").textContent += `\n网页：${data.live_task.data.note} · ${data.live_task.data.steps} 步 / ${data.live_task.data.calls} 次模型调用`;
  const finished = wasRunning && !data.turns.some((turn) => turn.status === "running");
  if (finished || data.events.length) await loadList();
  if (finished) {
    await loadStatus();
    if (opened && !draftDirty) await openRecord(opened.ref);
  }
  schedulePoll();
}
async function loadStatus() {
  status = await api("/api/status");
  $("model-status").textContent = `${status.mode === "demo" ? "演示模式 · 未调用真实模型" : status.model} · 今日 ${status.usage.calls}/${status.daily_limit} 次模型调用`;
  $("settings-status").textContent = `本轮最多 ${status.conversation_budget.calls} 次模型调用（含网页内部调用），${Math.round(status.conversation_budget.seconds / 60)} 分钟；完整上下文上限 ${status.conversation_budget.context_chars} 字符，超出时停止，不自动裁剪。`;
}
$("new-conversation").addEventListener("click", () => work($("new-conversation"), newConversation));
$("rename-conversation").addEventListener("click", () => work($("rename-conversation"), async () => {
  const title = window.prompt("会话标题", conversation.title);
  if (!title?.trim()) return;
  conversation = (await api(`/api/conversations/${conversation.id}`, "PATCH", { title })).conversation;
  renderConversation(); await loadList();
}));
$("delete-conversation").addEventListener("click", () => work($("delete-conversation"), async () => {
  if (!window.confirm("删除此会话及其历史？独立保存的邮件、通知、网页记录和草稿会保留。")) return;
  await flushDraft();
  const id = conversation.id;
  await api(`/api/conversations/${id}`, "DELETE");
  sessionStorage.removeItem(`composer:${id}`); conversation = null;
  await loadList();
  if (conversations.length) await selectConversation(conversations[0].id); else await newConversation();
}));
$("chat-input").addEventListener("input", rememberComposer);
$("chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!$("chat-button").disabled) $("chat-form").requestSubmit(); }
});
$("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  work($("chat-button"), async () => {
    await flushDraft();
    const refs = new Map([...selection.values()].map((item) => [refKey(item.ref), simpleRef(item.ref)]));
    if (opened && $("use-open-record").checked) refs.set(refKey(opened.ref), simpleRef(opened.ref));
    const id = conversation.id;
    const result = await api(`/api/conversations/${id}/turns`, "POST", { message: $("chat-input").value, records: [...refs.values()] });
    if (conversation?.id !== id) return;
    conversation = result.conversation; selection.clear(); $("chat-input").value = "";
    rememberComposer(); renderSelection(); renderConversation(true); await loadList(); schedulePoll(100);
  });
});
$("stop-turn").addEventListener("click", () => work($("stop-turn"), async () => {
  const turn = conversation.turns.find((item) => item.status === "running");
  if (turn) { conversation = (await api(`/api/conversations/${conversation.id}/turns/${turn.id}/cancel`, "POST", {})).conversation; updateControls(); schedulePoll(100); }
}));

async function showPicker(manage = false) {
  pickerSelection = new Map(selection); pickerOffset = 0;
  $("records-title").textContent = manage ? "资料管理" : "添加资料";
  $("records-dialog").showModal(); await searchRecords();
}
async function searchRecords() {
  const sequence = ++pickerSequence;
  $("record-results").replaceChildren(node("p", "caption", "正在读取资料…"));
  const query = new URLSearchParams({ kind: $("record-kind").value, query: $("record-query").value, offset: pickerOffset, limit: 20 });
  const data = await api(`/api/records?${query}`);
  if (sequence !== pickerSequence) return;
  const rows = data.records.map((record) => {
    const row = node("div", "picker-row"), label = node("label"), check = document.createElement("input"), description = node("span", "record-description");
    check.type = "checkbox"; check.checked = pickerSelection.has(refKey(record.ref));
    check.setAttribute("aria-label", `选择${record.title}`);
    check.addEventListener("change", () => { check.checked ? pickerSelection.set(refKey(record.ref), record) : pickerSelection.delete(refKey(record.ref)); });
    description.append(node("strong", "", record.title), node("small", "", `${names[record.ref.kind]} · ${record.sender || statusNames[record.status] || record.status} · ${formatTime(record.updated_at)}`));
    label.append(check, description); row.append(label, button("预览", () => previewRecord(record.ref))); return row;
  });
  if (!rows.length) rows.push(node("p", "caption", "没有匹配的已保存资料。可以保存通知、新建草稿，或先同步邮件。"));
  $("record-results").replaceChildren(...rows);
  $("records-page").textContent = `共 ${data.total} 项${data.total ? ` · ${pickerOffset + 1}–${Math.min(pickerOffset + 20, data.total)}` : ""}`;
  $("records-previous").disabled = pickerOffset === 0; $("records-next").disabled = pickerOffset + 20 >= data.total;
}
async function previewRecord(ref) {
  const data = await api(`/api/records/${ref.kind}/${ref.id}`);
  previewRef = ref; $("preview-title").textContent = data.record.title;
  const value = data.record.data;
  $("preview-content").textContent = value.original?.text || value.text || value.body || value.content || JSON.stringify(value.result || value, null, 2);
  $("preview-dialog").showModal();
}
$("add-records").addEventListener("click", () => work($("add-records"), () => showPicker()));
$("manage-records").addEventListener("click", () => work($("manage-records"), () => showPicker(true)));
$("close-picker").addEventListener("click", () => $("records-dialog").close());
$("close-preview").addEventListener("click", () => $("preview-dialog").close());
$("preview-open").addEventListener("click", () => work($("preview-open"), async () => {
  $("preview-dialog").close(); $("records-dialog").close(); await openRecord(previewRef);
}));
$("record-search").addEventListener("submit", (event) => { event.preventDefault(); pickerOffset = 0; work(event.submitter, searchRecords); });
$("record-kind").addEventListener("change", () => { pickerOffset = 0; searchRecords().catch((error) => toast(error.message, true)); });
$("records-previous").addEventListener("click", () => work($("records-previous"), async () => { pickerOffset = Math.max(0, pickerOffset - 20); await searchRecords(); }));
$("records-next").addEventListener("click", () => work($("records-next"), async () => { pickerOffset += 20; await searchRecords(); }));
$("picker-add").addEventListener("click", () => { selection = new Map(pickerSelection); renderSelection(); rememberComposer(); $("records-dialog").close(); $("chat-input").focus(); });
$("new-draft").addEventListener("click", () => work($("new-draft"), async () => {
  await flushDraft();
  const result = await api("/api/drafts", "POST", { source: { kind: "manual", keys: [] } }, true);
  $("records-dialog").close(); await openRecord({ kind: "draft", id: result.draft.id }); await poll();
}));
$("open-notification-import").addEventListener("click", () => $("notification-dialog").showModal());
$("close-notification-import").addEventListener("click", () => $("notification-dialog").close());
$("import-form").addEventListener("submit", (event) => {
  event.preventDefault(); work(event.submitter, async () => {
    const result = await api("/api/messages", "POST", { conversation: $("notification-conversation").value, text: $("notification-text").value, sent_at: new Date($("sent-at").value).toISOString() });
    const data = await api(`/api/records/notification/${result.message.id}`);
    addSelected(data.record); $("notification-dialog").close(); $("records-dialog").close(); toast("通知已保存并添加，尚未分析。");
  });
});
$("json-file").addEventListener("change", async (event) => {
  const file = event.target.files[0]; if (!file) return;
  try {
    if (file.size > 100000) throw new Error("JSON 文件请小于100KB。");
    const input = JSON.parse(await file.text()), batch = Array.isArray(input) ? input : [input];
    if (!batch.length || batch.length > 20) throw new Error("每批导入1～20条通知。");
    for (const item of batch) await api("/api/messages", "POST", item);
    await searchRecords(); toast("通知已保存，尚未分析。");
  } catch (error) { toast(`导入未全部完成，已成功项保留：${error.message}`, true); }
  finally { event.target.value = ""; }
});

function safeLink(url, text) {
  try { const parsed = new URL(url); if (parsed.protocol !== "https:" || parsed.username || parsed.password) return node("span", "", text); }
  catch (_) { return node("span", "", text); }
  const link = node("a", "", text); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; return link;
}
function section(title, text) { const element = node("section", "result-section"); element.append(node("h3", "", title), node("p", "", text)); return element; }
function hideRecord() {
  opened = null; draft = null; draftDirty = false; clearTimeout(draftTimer);
  $("record-panel").hidden = true; $("focus-record-label").hidden = true;
}
async function openRecord(ref) {
  await flushDraft();
  const sequence = ++recordSequence;
  const data = await api(`/api/records/${ref.kind}/${ref.id}`);
  if (sequence !== recordSequence) return;
  opened = data.record; draft = ref.kind === "draft" ? data.detail : null;
  $("record-panel").hidden = false; $("focus-record-label").hidden = false; $("use-open-record").checked = true;
  $("record-title").textContent = opened.title;
  $("record-meta").textContent = `${names[ref.kind]} · ${statusNames[opened.status] || opened.status} · ${formatTime(opened.updated_at)}`;
  $("record-content").replaceChildren(); $("draft-form").hidden = !draft;
  if (draft) {
    if (opened.source_refs.length) {
      $("record-content").append(node("p", "caption", "草稿来源"));
      for (const source of opened.source_refs) $("record-content").append(button(`查看来源${names[source.kind]}`, () => openRecord(source)));
    }
    renderDraft(); return;
  }
  const content = $("record-content"), value = opened.data;
  for (const note of opened.coverage.notes) content.append(node("p", "notice", note));
  if (ref.kind === "notification" || ref.kind === "mail") {
    if (value.triage) content.append(section("邮件头初筛建议", `${value.triage.decision}：${value.triage.reason}`));
    if (value.analysis) {
      content.append(section("分析", value.analysis.summary));
      for (const event of value.analysis.events || []) content.append(section(event.title,
        `${event.time_kind === "deadline" ? "截止" : "时间"}：${event.normalized || event.time_text || "待确认"}\n${event.relevance}\n${event.action}\n原文：${event.evidence}\n${event.uncertainties.join("；")}`));
      if (value.analysis.actions?.length) content.append(section("行动", value.analysis.actions.join("\n")));
      if (value.analysis.relevance_reason) content.append(section("相关性", value.analysis.relevance_reason));
      for (const time of value.analysis.times || []) content.append(section(time.kind === "deadline" ? "截止时间" : "事项时间", `${time.text}${time.normalized ? " · " + formatTime(time.normalized) : " · 精确时间待确认"}`));
      if (value.analysis.uncertainties?.length) content.append(section("待确认", value.analysis.uncertainties.join("\n")));
    }
    content.append(node("h3", "", "原文"), node("pre", "", value.original?.text || value.text || "尚无正文"));
    for (const [index, update] of (value.profile_updates || []).entries()) {
      content.append(section(update.applied ? "已补充画像" : "画像建议待确认", `${update.text}\n来源：${update.evidence}\n${update.reason}`));
      if (!update.applied) content.append(button("确认属于我并保存", async () => {
        await api(`/api/mail/${ref.id}/profile/${index}/confirm`, "POST", {}, true); await openRecord(ref); await poll();
      }));
    }
  } else if (ref.kind === "task") {
    if (value.result) {
      content.append(section("查询结果", value.result.summary));
      content.append(node("p", "caption", value.result.coverage || value.note));
      for (const result of [...(value.result.findings || []), ...(value.result.jobs || [])]) {
        const block = section(result.title || "发现", result.text || result.reason);
        block.append(safeLink(result.url, "打开原来源"), node("pre", "", result.quote || result.job_evidence || "")); content.append(block);
      }
      if (value.result.questions?.length) content.append(section("待确认", value.result.questions.join("\n")));
    } else content.append(node("p", "notice", value.note));
    const evidence = node("details"); evidence.append(node("summary", "", "已保存网页证据"));
    for (const page of data.detail.pages) evidence.append(node("h3", "", `${page.id} · ${page.title}`), safeLink(page.url, page.url), node("pre", "", page.text));
    content.append(evidence);
    const logs = node("details"); logs.append(node("summary", "", "浏览动作与诊断"), node("pre", "", JSON.stringify({ actions: data.detail.actions, errors: data.detail.errors }, null, 2))); content.append(logs);
    if (["running", "queued"].includes(opened.status)) content.append(button("取消网页任务", async () => { await api(`/api/tasks/${ref.id}/cancel`, "POST", {}); await openRecord(ref); }));
  } else content.append(node("pre", "", value.content));
}
$("close-record").addEventListener("click", () => work($("close-record"), async () => { await flushDraft(); hideRecord(); }));
$("attach-open-record").addEventListener("click", () => { if (opened) { addSelected(opened); toast("已添加到下一条消息。"); } });
function draftFields() {
  const split = (name) => $("draft-" + name).value.split(/[,;，；\n]/).map((value) => value.trim()).filter(Boolean);
  return { to: split("to"), cc: split("cc"), bcc: split("bcc"), subject: $("draft-subject").value, body: $("draft-body").value };
}
function updateDraftStatus(text) {
  if (!draft) return;
  $("draft-status").textContent = text || `${statusNames[draft.status] || draft.status} · v${draft.version}${draft.send?.detail ? " · " + draft.send.detail : ""}${draft.send?.refused?.length ? " · 被拒收件人：" + draft.send.refused.join("、") : ""}`;
  $("draft-send").disabled = !status?.capabilities.send_email || !["draft", "failed"].includes(draft.status);
}
function renderDraft() {
  draftDirty = false;
  draftBaseVersion = draft.version;
  $("draft-from").value = draft.from;
  for (const kind of ["to", "cc", "bcc"]) $("draft-" + kind).value = draft[kind].join(", ");
  $("draft-subject").value = draft.subject; $("draft-body").value = draft.body;
  const editable = ["draft", "failed"].includes(draft.status);
  for (const id of ["draft-to", "draft-cc", "draft-bcc", "draft-subject", "draft-body", "draft-save", "draft-delete"]) $(id).disabled = !editable;
  updateDraftStatus(); renderRevisions();
  const cached = sessionStorage.getItem(`draft-input:${draft.id}`);
  if (cached && editable) {
    const local = JSON.parse(cached);
    draftBaseVersion = local.base_version || draft.version;
    for (const kind of ["to", "cc", "bcc"]) $("draft-" + kind).value = local[kind].join(", ");
    $("draft-subject").value = local.subject; $("draft-body").value = local.body;
    draftDirty = true; updateDraftStatus("已恢复未保存的本地输入，请检查后保存。当前服务端 v" + draft.version);
  }
  if (!status?.capabilities.send_email) $("draft-status").textContent += " · SMTP 尚未配置";
}
function renderRevisions() {
  $("draft-revisions").replaceChildren(...[...draft.revisions].reverse().map((revision) => {
    const row = node("div", "revision-row");
    row.append(node("span", "", `v${revision.version} · ${revision.subject || "无主题"} · ${formatTime(revision.created_at)}`));
    const restore = button(revision.version === draft.version ? "当前版本" : "恢复", async () => {
      await flushDraft();
      const result = await api(`/api/drafts/${draft.id}/restore`, "POST", { version: draft.version, target_version: revision.version }, true);
      await openRecord({ kind: "draft", id: result.draft.id }); await poll();
    });
    restore.disabled = revision.version === draft.version || !["draft", "failed"].includes(draft.status);
    row.append(restore); return row;
  }));
}
async function flushDraft() {
  clearTimeout(draftTimer);
  // Serialize autosaves; content typed during an in-flight save remains in the editor.
  const pending = saveQueue.catch(() => {}).then(async () => {
    if (!draft || !draftDirty) return;
    const id = draft.id, fields = draftFields(), encoded = JSON.stringify(fields);
    updateDraftStatus("保存中…");
    try {
      const result = await api(`/api/drafts/${id}`, "PUT", { version: draftBaseVersion, ...fields }, true);
      if (draft?.id !== id) return;
      draft = result.draft;
      draftBaseVersion = draft.version;
      opened = { ...opened, ref: { ...opened.ref, version: draft.version }, title: draft.subject || "（无主题草稿）" };
      $("record-title").textContent = opened.title;
      draftDirty = JSON.stringify(draftFields()) !== encoded;
      if (!draftDirty) sessionStorage.removeItem(`draft-input:${id}`);
      else sessionStorage.setItem(`draft-input:${id}`, JSON.stringify({ ...draftFields(), base_version: draftBaseVersion }));
      updateDraftStatus(draftDirty ? "仍有修改待保存" : `已保存 · v${draft.version}`); renderRevisions();
    } catch (error) { updateDraftStatus(`保存失败，本地输入保留：${error.message}`); throw error; }
  });
  saveQueue = pending;
  await pending;
  if (draftDirty) return flushDraft();
}
for (const id of ["draft-to", "draft-cc", "draft-bcc", "draft-subject", "draft-body"]) $(id).addEventListener("input", () => {
  draftDirty = true; sessionStorage.setItem(`draft-input:${draft.id}`, JSON.stringify({ ...draftFields(), base_version: draftBaseVersion }));
  updateDraftStatus("未保存修改"); clearTimeout(draftTimer);
  draftTimer = setTimeout(() => flushDraft().catch((error) => toast(error.message, true)), 800);
});
$("draft-form").addEventListener("submit", (event) => { event.preventDefault(); work($("draft-save"), async () => { await flushDraft(); await poll(); }); });
$("draft-send").addEventListener("click", () => work($("draft-send"), async () => {
  if (draft.status === "failed") draftDirty = true;
  await flushDraft();
  const id = draft.id, conversationId = conversation.id;
  for (const field of ["draft-to", "draft-cc", "draft-bcc", "draft-subject", "draft-body", "draft-save"]) $(field).disabled = true;
  updateDraftStatus("正在发送当前版本…"); $("draft-send").disabled = true;
  try {
    const result = await api(`/api/drafts/${id}/send`, "POST", { version: draft.version }, true);
    if (conversation?.id === conversationId && draft?.id === id) await openRecord({ kind: "draft", id });
    await poll();
    toast(result.draft.status === "sent" ? "SMTP 已接受邮件。" : "请核对面板中的发送结果。", result.draft.status !== "sent");
  } catch (error) { if (draft?.id === id) renderDraft(); throw error; }
}));
$("draft-copy").addEventListener("click", () => work($("draft-copy"), async () => {
  await flushDraft(); const result = await api(`/api/drafts/${draft.id}/copy`, "POST", {}, true);
  await openRecord({ kind: "draft", id: result.draft.id }); await poll(); toast("已复制为独立草稿。");
}));
$("draft-delete").addEventListener("click", () => work($("draft-delete"), async () => {
  if (!window.confirm("永久删除此草稿及全部版本？会话历史快照保留，发送记录不受影响。")) return;
  await flushDraft(); const id = draft.id;
  await api(`/api/drafts/${id}`, "DELETE", { version: draft.version }, true);
  sessionStorage.removeItem(`draft-input:${id}`); hideRecord(); await poll();
}));
$("draft-reload").addEventListener("click", () => work($("draft-reload"), async () => {
  if (draftDirty && !window.confirm("放弃未保存的本地修改，重新载入服务端当前版本？")) return;
  clearTimeout(draftTimer); const ref = opened.ref;
  sessionStorage.removeItem(`draft-input:${draft.id}`); draftDirty = false; await saveQueue.catch(() => {}); await openRecord(ref);
}));
window.addEventListener("beforeunload", (event) => { rememberComposer(); if (draftDirty) { event.preventDefault(); event.returnValue = ""; } });

async function loadMailStatus() {
  const data = (await api("/api/mail")).status;
  $("mail-status").textContent = !data.enabled ? "未配置 QQ 邮箱。请在 .env 设置账号、IMAP授权码和访问口令。" : `${data.started ? "已启用新邮件同步" : "未启用同步"} · ${data.poll_seconds}秒检查一次${data.error ? " · " + data.error : ""}`;
  $("mail-start").disabled = !data.enabled || data.started; $("mail-sync").disabled = !data.enabled || !data.started; $("mail-latest").disabled = !data.enabled;
}
$("open-settings").addEventListener("click", () => work($("open-settings"), async () => {
  const [memory] = await Promise.all([api("/api/memory"), loadStatus(), loadMailStatus()]);
  for (const kind of ["public_profile", "profile", "rules"]) $(kind + "-text").value = memory[kind];
  $("settings-dialog").showModal();
}));
$("close-settings").addEventListener("click", () => $("settings-dialog").close());
for (const form of document.querySelectorAll(".memory-form")) form.addEventListener("submit", (event) => {
  event.preventDefault(); work(event.submitter, async () => {
    const kind = form.dataset.kind; await api(`/api/memory/${kind}`, "PUT", { content: $(kind + "-text").value }, true);
    toast("已保存并记录变更，旧分析不自动重算。"); await poll();
  });
});
for (const form of document.querySelectorAll(".memory-form")) {
  const history = node("div", "memory-history-list"), kind = form.dataset.kind;
  form.append(button("查看修改历史", async () => {
    if (history.childElementCount) { history.replaceChildren(); return; }
    const data = await api(`/api/memory/${kind}/history`);
    const rows = data.revisions.map((revision) => {
      const row = node("details");
      row.append(node("summary", "", formatTime(revision.created_at)), node("p", "caption", "变更前"), node("pre", "", revision.before || "（空）"), node("p", "caption", "变更后"), node("pre", "", revision.after || "（空）"));
      if (revision.source?.user_quote) row.append(node("p", "caption", "用户原话：" + revision.source.user_quote));
      row.append(button("恢复到变更前", async () => {
        if (!window.confirm("将当前内容替换为这里展示的变更前内容，之后的修改也会被覆盖。继续？")) return;
        await api(`/api/memory/${kind}`, "PUT", { content: revision.before }, true);
        $(kind + "-text").value = revision.before; history.replaceChildren(); toast("已恢复，原变更历史保留。"); await poll();
      }));
      return row;
    });
    history.replaceChildren(...(rows.length ? rows : [node("p", "caption", "尚无修改历史。") ]));
  }), history);
}
for (const [id, path] of [["mail-start", "start"], ["mail-sync", "sync"]]) $(id).addEventListener("click", () => work($(id), async () => {
  await api(`/api/mail/${path}`, "POST", {}); await loadMailStatus(); toast("操作完成，后台不会自动分析。");
}));
$("mail-latest").addEventListener("click", () => work($("mail-latest"), async () => {
  if (!window.confirm("只读导入收件箱最新一封邮件（可能是历史邮件），不改变新邮件同步范围、不自动分析。继续？")) return;
  await api("/api/mail/latest", "POST", { confirm: true }); toast("最新一封邮件已保存，可从添加资料中选择。");
}));
$("logout-button")?.addEventListener("click", () => work($("logout-button"), async () => {
  await flushDraft(); await api("/api/logout", "POST", {}); window.location.assign("/login");
}));
const currentTime = new Date();
$("sent-at").value = new Date(currentTime.getTime() - currentTime.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
(async () => {
  await Promise.all([loadStatus(), loadList()]);
  const previous = localStorage.getItem("current-conversation");
  const chosen = conversations.find((item) => item.id === previous) || conversations[0];
  if (chosen) await selectConversation(chosen.id); else await newConversation();
})().catch((error) => toast(error.message, true));
