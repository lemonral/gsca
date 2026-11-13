"use strict";

const $ = id => document.getElementById(id);
const ACTIVE = new Set(["queued", "running", "cancelling", "pending"]);
const statusNames = {queued:"排队中",pending:"排队中",running:"执行中",cancelling:"正在停止",completed:"已完成",succeeded:"已完成",failed:"执行失败",cancelled:"已停止",interrupted:"已中断",timed_out:"已超时"};
const eventNames = {model_started:"请求模型",model_completed:"模型返回",tool_call:"调用工具",tool_result:"工具返回",run_status:"运行状态",error:"发生错误",assistant:"生成回复",run_started:"开始任务",run_completed:"完成任务",run_failed:"任务失败",run_cancelled:"任务停止"};
const state = {sessionId:null,epoch:0,sessions:[],runs:[],viewRunId:null,activeRunId:null,viewRun:null,messages:[],timer:null,sending:false,navigating:false,navigationToken:0,sessionReady:true,filePath:".",tab:"trace",authenticated:false};
let toastTimer;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function safeText(value) {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "";
  return JSON.stringify(value, null, 2);
}
function dateValue(value) {
  if (!value) return null;
  const date = new Date(typeof value === "number" ? value * (value < 1e12 ? 1000 : 1) : value);
  return Number.isNaN(date.getTime()) ? null : date;
}
function timeText(value, full = false) {
  const date = dateValue(value);
  if (!date) return "";
  return date.toLocaleString("zh-CN", full ? {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"} : {hour:"2-digit",minute:"2-digit",second:"2-digit"});
}
function statusText(status) {return statusNames[status] || status || "就绪";}
function notify(message) {
  $("notice-text").textContent = message;
  $("notice").hidden = false;
}
function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => {$("toast").hidden = true;}, 4000);
}
async function api(path, options = {}) {
  const headers = {Accept:"application/json", ...(options.headers || {})};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(path, {...options, headers, credentials:"same-origin"});
  } catch (_) {
    throw new Error("连接中断，请检查网络后重试。任务可能仍在服务器执行。");
  }
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") showLogin();
    const detail = typeof body === "object" ? (body.detail || body.error || body.message) : body;
    const error = new Error(safeText(detail).slice(0, 1200) || `请求失败（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return body;
}
function showLogin() {
  state.authenticated = false;
  clearTimeout(state.timer);
  state.epoch++;
  state.navigationToken++;
  state.navigating = false;
  state.sending = false;
  state.sessionReady = false;
  $("connection-screen").hidden = true;
  $("workspace").hidden = true;
  $("login-screen").hidden = false;
}
function updateControls() {
  const busy = Boolean(state.activeRunId || state.sending);
  const blocked = busy || state.navigating || !state.sessionReady;
  $("send").disabled = blocked;
  $("upload-file").disabled = blocked;
  $("prompt").disabled = state.navigating || !state.sessionReady;
  $("new-session").disabled = state.navigating || state.sending;
  for (const button of $("sessions").querySelectorAll("button")) button.disabled = state.navigating || state.sending;
  $("cancel-run").hidden = !state.activeRunId;
  $("cancel-run").disabled = !state.activeRunId;
  $("working").hidden = !busy || state.navigating;
  $("prompt").placeholder = state.navigating ? "正在载入会话，请稍候…" : !state.sessionReady ? "会话未能载入，请点击左侧会话重试。" : busy ? "当前任务执行中，完成后可继续对话…" : "描述你的任务，或先上传一个文本文件…";
  const status = busy ? (state.viewRun?.status && ACTIVE.has(state.viewRun.status) ? state.viewRun.status : "running") : state.viewRun?.status;
  $("run-state").textContent = state.navigating ? "载入中" : !state.sessionReady ? "会话未载入" : statusText(status);
  $("run-state").className = "run-state" + (busy && !state.navigating ? " active" : !state.sessionReady && !state.navigating || ["failed","interrupted","timed_out"].includes(status) ? " error" : "");
}
function beginNavigation() {
  const token = ++state.navigationToken;
  state.navigating = true;
  updateControls();
  return token;
}
function endNavigation(token) {
  if (token !== state.navigationToken) return;
  state.navigating = false;
  updateControls();
}
function setInspector(open) {
  $("workspace").classList.toggle("inspector-collapsed", !open);
  $("toggle-inspector").setAttribute("aria-expanded", String(open));
}
function setSidebar(open) {
  $("workspace").classList.toggle("sidebar-open", open);
  $("sidebar-backdrop").hidden = !open;
}
function setTab(name) {
  state.tab = name;
  for (const tab of ["trace", "files", "tools"]) {
    $("tab-" + tab).setAttribute("aria-selected", String(tab === name));
    $("tab-" + tab).tabIndex = tab === name ? 0 : -1;
    $("panel-" + tab).hidden = tab !== name;
  }
  if (name === "files") loadFiles().catch(error => notify(error.message));
}
async function refreshSessions() {
  const sessions = await api("/api/sessions");
  state.sessions = Array.isArray(sessions) ? sessions : (sessions.sessions || []);
  renderSessions();
}
function renderSessions() {
  const host = $("sessions");
  host.replaceChildren();
  $("session-count").textContent = state.sessions.length;
  if (!state.sessions.length) host.append(element("p", "sessions-empty", "创建第一个会话，开始一个新任务。"));
  for (const session of state.sessions) {
    const button = element("button", "session" + (session.id === state.sessionId ? " active" : ""));
    button.type = "button";
    button.disabled = state.navigating || state.sending;
    button.setAttribute("aria-current", session.id === state.sessionId ? "true" : "false");
    button.title = session.title || "新会话";
    button.append(element("span", "session-icon", "◧"));
    const copy = element("span", "session-copy");
    copy.append(element("strong", "", session.title || "新会话"), element("small", "", timeText(session.updated_at || session.created_at, true)));
    button.append(copy);
    button.addEventListener("click", () => loadSession(session.id).catch(error => notify(error.message)));
    host.append(button);
  }
}
function renderMessages() {
  const host = $("messages");
  host.replaceChildren();
  const visible = state.messages.filter(message => ["user","assistant","system"].includes(message.role) && message.content);
  $("welcome").hidden = visible.length > 0;
  for (const message of visible) {
    const article = element("article", "message " + message.role);
    const avatar = element("div", "message-avatar", message.role === "user" ? "你" : message.role === "assistant" ? "A" : "·");
    avatar.setAttribute("aria-hidden", "true");
    const content = element("div", "message-content");
    const meta = element("div", "message-meta");
    meta.append(element("span", "", message.role === "user" ? "你" : message.role === "assistant" ? "Agent" : "系统"));
    const time = element("time", "", timeText(message.created_at));
    const value = dateValue(message.created_at);
    if (value) time.dateTime = value.toISOString();
    meta.append(time);
    content.append(meta, element("div", "message-body", safeText(message.content)));
    article.append(avatar, content);
    host.append(article);
  }
}
function sortedRuns(runs) {
  return [...runs].sort((a,b) => (dateValue(b.created_at)?.getTime() || 0) - (dateValue(a.created_at)?.getTime() || 0));
}
function renderRunSelect() {
  const select = $("run-select");
  select.replaceChildren();
  select.disabled = !state.runs.length;
  if (!state.runs.length) select.append(element("option", "", "尚无运行记录"));
  for (const run of state.runs) {
    const option = element("option", "", `${timeText(run.created_at, true)}  ${statusText(run.status)}`);
    option.value = run.id;
    select.append(option);
  }
  if (state.viewRunId) select.value = state.viewRunId;
}
function eventSummary(event) {
  const payload = event.payload || {};
  if (typeof payload === "string") return payload.slice(0,160);
  if (event.kind === "tool_call") return `${payload.name || payload.tool || "工具"}  ${safeText(payload.arguments || payload.args || {}).replace(/\n\s*/g," ").slice(0,100)}`;
  if (event.kind === "tool_result") {
    const result = payload.result;
    const successful = result?.ok === false || payload.error ? "返回错误" : "已返回结果";
    return `${payload.name || payload.tool || "工具"} · ${successful}`;
  }
  if (event.kind === "run_status") return statusText(payload.status || payload.state) + (payload.error ? " · " + payload.error : "");
  if (event.kind === "model_started") return payload.model ? String(payload.model) : "模型正在根据对话与工具结果选择下一步。";
  if (event.kind === "model_completed") return payload.tool_calls ? `返回 ${Array.isArray(payload.tool_calls) ? payload.tool_calls.length : payload.tool_calls} 个工具调用` : "模型请求完成。";
  if (payload.error || payload.message || payload.status) return safeText(payload.error || payload.message || payload.status).slice(0,180);
  return "";
}
function renderRun(run, events = []) {
  state.viewRun = run;
  const host = $("events");
  const oldDetails = new Set(Array.from(host.querySelectorAll("details[open]")).map(detail => detail.dataset.eventId));
  host.replaceChildren();
  $("trace-empty").hidden = Boolean(run || events.length);
  const summary = $("run-summary");
  summary.replaceChildren();
  summary.hidden = !run;
  if (run) {
    summary.append(element("span", "metric status" + (["failed","interrupted","timed_out"].includes(run.status) ? " error" : ""), statusText(run.status)));
    summary.append(element("span", "metric", `步骤 ${run.steps || 0}`));
    const usage = run.usage || {};
    const tokens = usage.total_tokens ?? ((usage.input_tokens || usage.prompt_tokens || 0) + (usage.output_tokens || usage.completion_tokens || 0));
    if (tokens) summary.append(element("span", "metric", `${tokens.toLocaleString()} tokens`));
    if (run.error) {
      const error = element("span", "metric error", "查看错误详情");
      error.title = String(run.error);
      summary.append(error);
    }
  }
  for (const event of events) {
    const item = element("li", "event-item" + (["tool_call","tool_result"].includes(event.kind) ? " tool" : event.kind.includes("error") || event.kind.includes("failed") ? " error" : ""));
    const heading = element("div", "event-heading");
    heading.append(element("strong", "", eventNames[event.kind] || event.kind), element("time", "", timeText(event.created_at)));
    item.append(heading);
    const summaryText = eventSummary(event);
    if (summaryText) item.append(element("div", "event-summary", summaryText));
    const detail = element("details");
    detail.dataset.eventId = String(event.id);
    detail.open = oldDetails.has(String(event.id));
    detail.append(element("summary", "", "查看记录"), element("pre", "", safeText(event.payload)));
    item.append(detail);
    host.append(item);
  }
  const error = run?.error;
  $("run-error").hidden = !error;
  $("run-error").textContent = error ? `任务${statusText(run.status)}\n${safeText(error)}` : "";
  const latest = events[events.length-1];
  if (state.activeRunId) {
    $("working-title").textContent = latest ? (eventNames[latest.kind] || "任务执行中") : "任务执行中";
    $("working-description").textContent = latest?.kind === "tool_call" ? `${latest.payload?.name || "工具"} 正在执行，完成后将继续处理任务。` : "正在使用模型与工具，执行记录会持续更新。";
  }
  updateControls();
}
function scrollToBottom() {
  requestAnimationFrame(() => {$("conversation").scrollTop = $("conversation").scrollHeight;});
}
async function loadSession(id, navigationToken) {
  if (navigationToken === undefined && (state.navigating || state.sending)) return;
  const ownsNavigation = navigationToken === undefined;
  const token = ownsNavigation ? beginNavigation() : navigationToken;
  if (token !== state.navigationToken || !state.authenticated) return;
  const epoch = ++state.epoch;
  clearTimeout(state.timer);
  state.sessionId = id;
  state.sessionReady = false;
  state.activeRunId = null;
  state.viewRunId = null;
  state.viewRun = null;
  state.filePath = ".";
  state.messages = [];
  state.runs = [];
  $("prompt").value = "";
  $("prompt").style.height = "";
  $("notice").hidden = true;
  $("upload-status").hidden = true;
  setSidebar(false);
  $("session-title").textContent = "正在载入…";
  renderMessages();renderRunSelect();renderRun(null,[]);renderSessions();
  try {
    const data = await api(`/api/sessions/${encodeURIComponent(id)}`);
    if (epoch !== state.epoch || token !== state.navigationToken) return;
    $("session-title").textContent = data.session?.title || "新会话";
    state.sessionReady = true;
    state.messages = data.messages || [];
    state.runs = sortedRuns(data.runs || []);
    state.activeRunId = state.runs.find(run => ACTIVE.has(run.status))?.id || null;
    state.viewRunId = state.activeRunId || state.runs[0]?.id || null;
    renderMessages();renderRunSelect();updateControls();scrollToBottom();
    if (state.viewRunId) await fetchRun(state.viewRunId,epoch);
    if (state.activeRunId) schedulePoll(state.activeRunId,epoch);
    if (state.tab === "files") await loadFiles();
  } catch (error) {
    if (epoch === state.epoch && !state.sessionReady) $("session-title").textContent = "会话载入失败";
    throw error;
  } finally {
    if (ownsNavigation && epoch === state.epoch) endNavigation(token);
  }
}
async function createSession() {
  if (state.navigating) return null;
  const token = beginNavigation();
  try {
    const session = await api("/api/sessions",{method:"POST",body:JSON.stringify({title:"新会话"})});
    if (token !== state.navigationToken || !state.authenticated) return null;
    await refreshSessions();
    if (token !== state.navigationToken || !state.authenticated) return null;
    await loadSession(session.id, token);
    if (token !== state.navigationToken || !state.authenticated) return null;
    return session;
  } finally {
    endNavigation(token);
    if (token === state.navigationToken && state.sessionReady) $("prompt").focus();
  }
}
async function fetchRun(id,epoch) {
  const data = await api(`/api/runs/${encodeURIComponent(id)}`);
  if (state.epoch !== epoch) return null;
  const run = data.run || data;
  const index = state.runs.findIndex(item => item.id === id);
  if (index >= 0) state.runs[index] = run;
  else state.runs.unshift(run);
  if (state.viewRunId === id) renderRun(run,data.events || []);
  renderRunSelect();
  return run;
}
function schedulePoll(id,epoch,delay=1000) {
  clearTimeout(state.timer);
  state.timer = setTimeout(async () => {
    if (state.epoch !== epoch || !state.authenticated) return;
    try {
      const run = await fetchRun(id,epoch);
      if (!run || state.epoch !== epoch) return;
      if (ACTIVE.has(run.status)) {schedulePoll(id,epoch);return;}
      state.activeRunId = null;
      const data = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}`);
      if (state.epoch !== epoch) return;
      state.messages = data.messages || [];
      state.runs = sortedRuns(data.runs || []);
      $("session-title").textContent = data.session?.title || "新会话";
      renderMessages();renderRunSelect();updateControls();scrollToBottom();
      await refreshSessions();
      if (state.tab === "files") await loadFiles();
    } catch (error) {
      if (state.epoch !== epoch || !state.authenticated) return;
      notify(error.message + " 正在尝试重新获取任务状态。");
      schedulePoll(id,epoch,4000);
    }
  },delay);
}
async function submitPrompt(event) {
  event.preventDefault();
  const prompt = $("prompt").value.trim();
  if (!prompt || state.activeRunId || state.sending || state.navigating || !state.sessionReady) return;
  state.sending = true;updateControls();$("notice").hidden = true;
  let epoch;
  try {
    if (!state.sessionId && !await createSession()) return;
    state.sending = true;updateControls();
    epoch = state.epoch;
    const id = state.sessionId;
    const run = await api(`/api/sessions/${encodeURIComponent(id)}/runs`,{method:"POST",body:JSON.stringify({prompt})});
    if (state.epoch !== epoch) return;
    state.activeRunId = run.id;
    state.viewRunId = run.id;
    state.viewRun = run;
    state.runs.unshift(run);
    $("prompt").value = "";$("prompt").style.height = "";
    const data = await api(`/api/sessions/${encodeURIComponent(id)}`);
    if (state.epoch !== epoch) return;
    state.messages = data.messages || [];
    state.runs = sortedRuns(data.runs || state.runs);
    $("session-title").textContent = data.session?.title || "新会话";
    renderMessages();renderRunSelect();renderRun(run,[]);scrollToBottom();
    if (window.innerWidth > 1080) setInspector(true);
    setTab("trace");
    schedulePoll(run.id,epoch,100);
    await refreshSessions();
  } catch (error) {
    if (epoch !== undefined && state.epoch !== epoch) return;
    notify(error.message);
    if (state.activeRunId) schedulePoll(state.activeRunId,state.epoch);
  } finally {
    if (epoch === undefined || state.epoch === epoch) {state.sending = false;updateControls();}
  }
}
async function loadFiles() {
  const id = state.sessionId;
  const epoch = state.epoch;
  const path = state.filePath;
  const host = $("files");
  if (!id) {host.replaceChildren(element("p","file-empty","创建会话后，可上传文本文件或让 Agent 生成文件。"));$("file-breadcrumb").replaceChildren();return;}
  const data = await api(`/api/sessions/${encodeURIComponent(id)}/files?path=${encodeURIComponent(path)}`);
  if (epoch !== state.epoch || path !== state.filePath) return;
  const files = Array.isArray(data) ? data : data.files || data.entries || [];
  host.replaceChildren();
  const breadcrumb = $("file-breadcrumb");breadcrumb.replaceChildren();
  const root = element("button","","工作区");root.addEventListener("click",()=>openDirectory("."));breadcrumb.append(root);
  if (path !== ".") {
    let partial = "";
    for (const part of path.split("/").filter(Boolean)) {
      partial += (partial ? "/" : "") + part;
      const target = partial;
      breadcrumb.append(element("span","","/"));
      const button = element("button","",part);button.addEventListener("click",()=>openDirectory(target));breadcrumb.append(button);
    }
  }
  if (!files.length) host.append(element("p","file-empty","目录中暂无文件。可在对话框添加文本文件，或让 Agent 创建一个文件。"));
  for (const file of files) {
    const filePath = file.path || (path === "." ? file.name : path + "/" + file.name);
    const directory = ["dir","directory","folder"].includes(file.type) || file.is_dir === true;
    const row = element("div","file-row");row.append(element("span","file-icon",directory ? "▱" : "≡"));
    const info = element("div","file-info");
    const item = element(directory ? "button" : "a","",file.name || filePath.split("/").pop());item.title = filePath;
    if (directory) item.addEventListener("click",()=>openDirectory(filePath));
    else {item.href=`/api/sessions/${encodeURIComponent(id)}/files/content?path=${encodeURIComponent(filePath)}`;item.target="_blank";item.rel="noopener noreferrer";}
    info.append(item,element("small","",directory ? "目录" : Number.isFinite(file.size) ? (file.size < 1024 ? file.size + " B" : (file.size / 1024).toFixed(1) + " KB") : "文本文件"));row.append(info);
    if (!directory) {const link=element("a","file-link","↗");link.href=item.href;link.target="_blank";link.rel="noopener noreferrer";link.setAttribute("aria-label","打开文件 "+filePath);row.append(link);}
    host.append(row);
  }
}
function openDirectory(path) {state.filePath=path;loadFiles().catch(error=>notify(error.message));}
async function uploadFile(file) {
  if (!file || state.activeRunId || state.sending || state.navigating || !state.sessionReady) return;
  const selectedEpoch = state.epoch;
  if (file.size > 65536) {notify("文件超过64 KB，请选择较小的文本文件。");return;}
  let content;
  try {content = new TextDecoder("utf-8",{fatal:true}).decode(await file.arrayBuffer());}
  catch (_) {notify("当前仅支持 UTF-8 编码的文本文件。");return;}
  if (selectedEpoch !== state.epoch || state.navigating || state.sending || state.activeRunId || !state.sessionReady) {
    toast("会话状态已改变，请在当前会话重新选择文件。");
    return;
  }
  if (content.includes("\u0000")) {notify("文件包含二进制内容，请选择 UTF-8 文本文件。");return;}
  state.sending=true;updateControls();
  let epoch;
  try {
    if (!state.sessionId && !await createSession()) return;
    state.sending=true;updateControls();epoch=state.epoch;
    await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/files`,{method:"POST",body:JSON.stringify({path:file.name,content})});
    if (epoch !== state.epoch) return;
    $("upload-status").textContent=`已添加 ${file.name}，可在任务中直接提到此文件名。`;
    $("upload-status").hidden=false;
    state.filePath=".";
    setTab("files");if(window.innerWidth>1080)setInspector(true);
    toast("文件已添加至当前会话工作区。");
    $("prompt").focus();
  } catch (error) {if(epoch===undefined||state.epoch===epoch)notify(error.message);}
  finally {if(epoch===undefined||state.epoch===epoch){state.sending=false;updateControls();}$("file-input").value="";}
}
async function loadTools() {
  const data = await api("/api/tools");
  const tools = Array.isArray(data) ? data : data.tools || [];
  const host = $("tools");host.replaceChildren();
  for (const raw of tools) {
    const tool = raw.function || raw;
    const item=element("article","tool-item");
    item.append(element("div","tool-name",tool.name),element("p","",tool.description || ""));host.append(item);
  }
}
async function enterWorkspace(health) {
  state.authenticated=true;
  $("login-screen").hidden=true;$("connection-screen").hidden=true;$("workspace").hidden=false;
  $("model-label").textContent=health.model || "千问模型";
  $("model-label").title="已载入模型配置，正在检查连接。";
  $("connection-dot").classList.add("offline");
  api("/api/model-health").then(result=>{
    if (!state.authenticated) return;
    $("connection-dot").classList.toggle("offline",result.ok !== true);
    $("model-label").title=result.ok === true ? "模型接口可达。" : "模型接口当前不可用，运行时会返回具体错误。";
  }).catch(()=>{
    if (!state.authenticated) return;
    $("connection-dot").classList.add("offline");
    $("model-label").title="模型已配置，但暂未确认接口可达。";
  });
  setInspector(window.innerWidth>1080);
  await Promise.all([refreshSessions(),loadTools()]);
  const remembered=sessionStorage.getItem("general-agent-session");
  const id=state.sessions.find(session=>session.id===remembered)?.id || state.sessions[0]?.id;
  if(id)await loadSession(id);
  else {state.sessionId=null;state.sessionReady=true;$("session-title").textContent="新的任务";renderMessages();renderRun(null,[]);updateControls();}
}
async function connect() {
  $("connection-screen").hidden=false;$("retry-connection").hidden=true;$("connection-message").textContent="正在连接工作台…";
  try {
    const health=await api("/api/health");
    if(health.authenticated===false){showLogin();return;}
    await enterWorkspace(health);
  } catch(error) {
    if(error.status===401){showLogin();return;}
    $("workspace").hidden=true;$("login-screen").hidden=true;$("connection-screen").hidden=false;
    $("connection-message").textContent=error.message;$("retry-connection").hidden=false;
  }
}

$("login-form").addEventListener("submit",async event=>{
  event.preventDefault();const token=$("access-code").value.trim();if(!token)return;
  $("login-button").disabled=true;$("login-error").hidden=true;
  try {
    await api("/api/login",{method:"POST",body:JSON.stringify({token})});$("access-code").value="";
    const health=await api("/api/health");await enterWorkspace(health);
  } catch(error) {$("login-error").textContent=error.message;$("login-error").hidden=false;}
  finally {$("login-button").disabled=false;}
});
$("logout").addEventListener("click",async()=>{try{await api("/api/logout",{method:"POST"});sessionStorage.removeItem("general-agent-session");showLogin();}catch(error){notify(error.message);}});
$("retry-connection").addEventListener("click",connect);
$("new-session").addEventListener("click",async()=>{if(state.navigating||state.sending)return;try{await createSession();}catch(error){notify(error.message);}finally{updateControls();}});
$("composer").addEventListener("submit",submitPrompt);
$("prompt").addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.shiftKey&&!event.isComposing){event.preventDefault();$("composer").requestSubmit();}});
$("prompt").addEventListener("input",()=>{$("prompt").style.height="auto";$("prompt").style.height=Math.min($("prompt").scrollHeight,180)+"px";});
$("cancel-run").addEventListener("click",async()=>{const id=state.activeRunId;if(!id)return;$("cancel-run").disabled=true;try{await api(`/api/runs/${encodeURIComponent(id)}/cancel`,{method:"POST"});toast("已请求停止任务。");schedulePoll(id,state.epoch,100);}catch(error){notify(error.message);$("cancel-run").disabled=false;}});
$("run-select").addEventListener("change",async event=>{const id=event.target.value;state.viewRunId=id;try{await fetchRun(id,state.epoch);}catch(error){notify(error.message);}});
$("upload-file").addEventListener("click",()=>$("file-input").click());
$("file-input").addEventListener("change",event=>uploadFile(event.target.files[0]));
$("refresh-files").addEventListener("click",()=>loadFiles().catch(error=>notify(error.message)));
$("dismiss-notice").addEventListener("click",()=>{$("notice").hidden=true;});
$("mobile-menu").addEventListener("click",()=>setSidebar(true));$("sidebar-backdrop").addEventListener("click",()=>setSidebar(false));
$("toggle-inspector").addEventListener("click",()=>setInspector($("workspace").classList.contains("inspector-collapsed")));
$("close-inspector").addEventListener("click",()=>setInspector(false));
for(const button of document.querySelectorAll("[data-tab]"))button.addEventListener("click",()=>setTab(button.dataset.tab));
for(const button of document.querySelectorAll(".suggestion"))button.addEventListener("click",()=>{$("prompt").value=button.dataset.prompt;$("prompt").focus();$("prompt").dispatchEvent(new Event("input"));});
document.addEventListener("keydown",event=>{if(event.key==="Escape"){setSidebar(false);if(window.innerWidth<=1080)setInspector(false);}});
window.addEventListener("beforeunload",()=>{if(state.sessionId)sessionStorage.setItem("general-agent-session",state.sessionId);});
document.querySelector(".inspector-tabs").addEventListener("keydown",event=>{
  if(!["ArrowLeft","ArrowRight"].includes(event.key))return;event.preventDefault();
  const tabs=["trace","files","tools"];const index=tabs.indexOf(state.tab);const name=tabs[(index+(event.key==="ArrowRight"?1:2))%3];setTab(name);$("tab-"+name).focus();
});
connect();
