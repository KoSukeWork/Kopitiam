// ACP controls share the page's safe DOM helpers; agent content is always text.
let acpInfo = {available:false, sessions:{}}, acpBarKey = "", acpCardsKey = "";
const acpActivityCache = new Map();
const ACP_STATES = {not_loaded:"空闲 · 待唤醒", idle:"空闲", working:"工作中", waiting_permission:"等你确认", error:"出错", handed_off:"已交给终端"};
const ACP_POLICY_NOTE = "只决定 agy 来问的操作。读文件、写系统临时目录由 agy 自行放行。";
function acpLabel(record) { return record ? (ACP_STATES[record.state] || record.state) : ""; }
function acpNotice(message) { $("acpNotice").textContent = message || ""; $("acpNotice").hidden = !message; }
function acpPolicySelect(value) {
  const input = el("select", "tin acp-policy"); input.setAttribute("aria-label", "权限模式");
  for (const [id, label] of [["workspace","工作区允许"],["all","全部允许"]]) {
    const option = el("option", "", label); option.value = id; input.append(option);
  }
  input.value = value; return input;
}
function acpConfirmAll() { return confirm("全部允许：别的 agent 发一条消息就能让它运行任何命令。确定启用吗？"); }
function acpCommands(input) {
  const found = [];
  function visit(value) {
    if (!value || typeof value !== "object") return;
    for (const [key, item] of Object.entries(value)) {
      if (["commandline", "command", "cmd"].includes(key.replace(/_/g, "").toLowerCase()) && typeof item === "string" && item.trim()) found.push(item);
      else if (item && typeof item === "object") visit(item);
    }
  }
  visit(input); return [...new Set(found)];
}
async function acpAction(action, data, button) {
  if (button) button.disabled = true;
  acpNotice("");
  try {
    const result = await post("/api/acp/" + action, data);
    if (!result.ok) throw new Error(result.message || "操作失败");
    await poll(); return result;
  } catch (error) { acpNotice(error.message); return null; }
  finally { if (button) button.disabled = false; }
}
function openAgySession() {
  closeSide();
  const body = el("div", "acp-create"), intro = el("div", "acp-intro"), introText = el("div");
  introText.append(el("strong", "", "在这里开始一项工作"), el("p", "", "选择项目，发出任务。过程和需要你确认的操作都会留在这里。"));
  intro.append(avatar("agy@new-project", true), introText); body.append(intro);
  const form = el("form"), fields = el("div", "acp-fields");
  function field(label, input, wide=false) {
    const wrap = el("label", "acp-field" + (wide ? " wide" : ""));
    wrap.append(el("span", "", label), input); fields.append(wrap); return input;
  }
  const cwd = field("工作目录", el("input", "tin"), true); cwd.required = true; cwd.placeholder = "C:\\Projects\\shop-web";
  const name = field("名字（可选）", el("input", "tin")); name.placeholder = "按项目名称自动取名"; name.maxLength = 32;
  const policy = field("权限", acpPolicySelect("workspace"));
  policy.onchange = () => { if (policy.value === "all" && !acpConfirmAll()) policy.value = "workspace"; };
  const model = field("模型", el("select", "tin"));
  for (const [id, label] of [["","使用 agy 默认"],["gemini-4-argon","Gemini 4 Argon"],["gemini-4-hydrogen","Gemini 4 Hydrogen"],
    ["gemini-pro-orchestrator-g1-pro","Orchestrator (Argon)"],["gemini-3.8-flash","Gemini 3.8 Flash"],
    ["claude-sonnet-5-5","Claude Sonnet 5.5"],["claude-opus-5-5","Claude Opus 5.5"],["gpt-oss-120b-medium","GPT-OSS 120B"]]) {
    const option = el("option", "", label); option.value = id; model.append(option);
  }
  const effort = field("思考档位", el("select", "tin"));
  function efforts() {
    const previous = effort.value; effort.replaceChildren();
    for (const [id,label] of [["","默认"],["low","低"],["medium","中"],["high","高"], ...(model.value === "gemini-4-hydrogen" ? [["xhigh","更高"]] : [])]) {
      const option = el("option", "", label); option.value = id; effort.append(option);
    }
    if ([...effort.options].some(o => o.value === previous)) effort.value = previous;
  }
  efforts(); model.onchange = efforts;
  const task = field("第一条任务（可选）", el("textarea", "tin"), true);
  task.placeholder = "例如：梳理这个项目，先给我一个修改建议。"; task.maxLength = 4000;
  const note = el("p", "acp-warning", ACP_POLICY_NOTE), status = el("div", "acp-notice");
  status.setAttribute("role", "status");
  const actions = el("div", "acp-submit"), cancel = el("button", "btn", "取消"), create = el("button", "btn acp-allow", "创建会话");
  cancel.type = "button"; cancel.onclick = closeModal; create.type = "submit";
  actions.append(cancel, create); form.append(fields, note, status, actions); body.append(form);
  if (!acpInfo.available) { status.textContent = "当前网页未启用托管，请重新启动 kopitiam web（不加 --no-wake）。"; create.disabled = true; }
  form.onsubmit = async event => {
    event.preventDefault(); if (!cwd.value.trim()) { cwd.focus(); return; }
    create.disabled = true; create.textContent = "正在创建…"; status.textContent = "";
    try {
      const result = await post("/api/acp/sessions", {backend:"agy", cwd:cwd.value.trim(), name:name.value.trim() || null,
        policy:policy.value, model:model.value || null, effort:effort.value || null, task:task.value});
      if (!result.ok) throw new Error(result.message || "创建失败");
      const active = $("mbody").contains(body); if (active) closeModal();
      await poll(); if (active) select(result.agent);
    } catch (error) { status.textContent = error.message; }
    finally { create.disabled = false; create.textContent = "创建会话"; }
  };
  openModal("新建 agy 会话", body); setTimeout(() => cwd.focus(), 50);
}
function renderACP() {
  const record = !selectedGroup && selected && acpInfo.sessions[selected];
  $("acpPanel").hidden = !record;
  if (!record) { acpBarKey = ""; acpCardsKey = ""; return; }
  const name = selected, key = JSON.stringify([name, record.state, record.policy, record.error, record.queued, acpInfo.available]);
  if (key !== acpBarKey) {
    acpBarKey = key;
    const bar = $("acpToolbar"), label = el("span", "acp-label"), dot = el("span", "acp-state-dot " + record.state);
    label.append(dot, document.createTextNode("agy · " + acpLabel(record))); bar.replaceChildren(label);
    if (record.queued) bar.append(el("span", "acp-queue", record.queued + " 条任务排队中"));
    const policy = acpPolicySelect(record.policy); policy.title = ACP_POLICY_NOTE; policy.disabled = !acpInfo.available;
    policy.onchange = async () => {
      if (policy.value === "all" && !acpConfirmAll()) { policy.value = record.policy; return; }
      if (!await acpAction("policy", {agent:name, policy:policy.value}, policy)) policy.value = record.policy;
    };
    bar.append(policy);
    if (record.state === "working" || record.state === "waiting_permission") {
      const stop = el("button", "btn", "停止这一轮"); stop.onclick = () => acpAction("cancel", {agent:name}, stop); bar.append(stop);
    }
    const handoff = el("button", "btn", record.state === "handed_off" ? "接回来" : "交给终端");
    handoff.disabled = !acpInfo.available;
    handoff.onclick = async () => {
      const result = await acpAction(record.state === "handed_off" ? "takeback" : "handoff", {agent:name}, handoff);
      if (result?.commands) {
        const body = el("div", "acp-create"); body.append(el("p", "tsub", "已结束网页中的会话。在终端依次运行："));
        const code = el("pre", "acp-commands", result.commands.join("\n")), copy = el("button", "btn", "复制命令");
        copy.onclick = async () => { try { await navigator.clipboard.writeText(result.commands.join("\n")); copy.textContent = "已复制"; } catch { copy.textContent = "请选中上方命令复制"; } };
        body.append(code, copy, el("p", "acp-warning", "终端退出后，在这里点「接回来」，恢复自动唤醒。")); openModal("交给终端", body);
      }
    };
    bar.append(handoff);
    if (record.state === "error" && record.session_id) {
      const retry = el("button", "btn", "重试连接"); retry.disabled = !acpInfo.available;
      retry.onclick = () => acpAction("retry", {agent:name}, retry); bar.append(retry);
    }
    $("acpError").textContent = (record.error || "") + (record.state === "error" && !record.session_id ? "；请删除后重新创建" : ""); $("acpError").hidden = !record.error;
  }
  const cards = record.permissions || [], cardsKey = JSON.stringify([name, cards]);
  if (cardsKey !== acpCardsKey) {
    acpCardsKey = cardsKey; $("acpPermissions").replaceChildren();
    for (const pending of cards) {
      const call = pending.toolCall || {}, card = el("section", "acp-permission"), head = el("div", "acp-permission-head");
      head.append(icon("bolt", 15), el("span", "", "需要你确认"), el("span", "kind", call.kind || "操作"));
      card.append(head, el("h4", "", String(call.title || call.name || "确认这项操作").slice(0, 500)));
      const commands = acpCommands(call.rawInput);
      for (const command of commands) {
        const block = el("pre", "acp-command");
        block.append(el("code", "", command.slice(0, 6000) + (command.length > 6000 ? "\n…命令已截短（共 " + command.length + " 字符），请核对完整命令后再允许。" : "")));
        card.append(block);
      }
      for (const path of (pending.paths || []).slice(0, 20)) card.append(el("code", commands.length ? "acp-command-path" : "acp-path", String(path).slice(0, 1000)));
      const details = el("details"), raw = JSON.stringify(call.rawInput || {}, null, 2);
      details.open = call.kind === "execute" && !commands.length;
      details.append(el("summary", "", "查看操作参数"), el("pre", "acp-raw", raw.slice(0, 6000) + (raw.length > 6000 ? "\n…已截短" : "")));
      card.append(details);
      const actions = el("div", "acp-permission-actions"), errorText = el("div", "acp-notice");
      for (const option of pending.options || []) {
        const labels = {allow_once:"允许这一次", reject_once:"拒绝这一次", allow_always:"始终允许", reject_always:"始终拒绝"};
        const button = el("button", "btn" + (option.kind === "allow_once" ? " acp-allow" : ""), labels[option.kind] || option.name || option.optionId);
        button.onclick = async () => {
          actions.querySelectorAll("button").forEach(b => b.disabled = true); errorText.textContent = "";
          try {
            const result = await post("/api/acp/permission", {agent:name, request:pending.request, option:option.optionId});
            if (!result.ok) throw new Error(result.message || "确认失败");
            await poll();
          } catch (error) { errorText.textContent = error.message; actions.querySelectorAll("button").forEach(b => b.disabled = false); }
        };
        actions.append(button);
      }
      card.append(actions, errorText, el("div", "acp-footnote", "这项操作会一直等待，直到你确认或停止这一轮。")); $("acpPermissions").append(card);
    }
  }
  if ($("acpActivity").open) loadACPActivity(name);
}
async function loadACPActivity(name) {
  let cache = acpActivityCache.get(name);
  if (!cache) { cache = {seq:0, events:new Map(), busy:false}; acpActivityCache.set(name, cache); }
  if (selected === name && !selectedGroup) renderACPActivity(cache);
  if (cache.busy) return;
  cache.busy = true;
  try {
    const result = await api("/api/acp/activity?agent=" + encodeURIComponent(name) + "&after=" + cache.seq);
    if (!Array.isArray(result.events)) return;
    if (result.reset) cache.events.clear();
    for (const event of result.events) cache.events.set(event.id, event);
    cache.seq = result.seq;
    // Match the server's three-turn retention even before a cursor reset is needed.
    const turns = [...new Set([...cache.events.values()].map(e => e.turn))].sort((a,b) => a-b).slice(-3);
    for (const [id,event] of cache.events) if (!turns.includes(event.turn)) cache.events.delete(id);
    for (const turn of turns) {
      const ids = [...cache.events.values()].filter(e => e.turn === turn).map(e => e.id).sort((a,b) => b-a);
      for (const id of ids.slice(500)) cache.events.delete(id);
    }
    if (selected === name && !selectedGroup) renderACPActivity(cache);
  } catch { /* Connection status is handled by the main poll. */ }
  finally { cache.busy = false; }
}
function renderACPActivity(cache) {
  const entries = [], tools = new Map();
  for (const event of [...cache.events.values()].sort((a,b) => a.id-b.id)) {
    const update = event.update || {}, kind = update.sessionUpdate;
    if (kind === "tool_call" || kind === "tool_call_update") {
      const key = event.turn + ":" + update.toolCallId;
      if (tools.has(key)) Object.assign(tools.get(key).update, update);
      else { const item = {...event, update:{...update}}; tools.set(key, item); entries.push(item); }
    } else if (["agent_message_chunk", "agent_thought_chunk", "plan"].includes(kind)) entries.push(event);
  }
  const box = $("acpEvents"); box.replaceChildren(); let turn = null, n = 0;
  for (const event of entries) {
    if (event.turn !== turn) { turn = event.turn; box.append(el("div", "acp-turn", "第 " + (++n) + " 轮")); }
    const update = event.update;
    if (update.sessionUpdate.startsWith("agent_")) {
      if (update.content?.type === "text") box.append(el("div", "acp-text" + (update.sessionUpdate === "agent_thought_chunk" ? " acp-thought" : ""), update.content.text));
    } else if (update.sessionUpdate === "plan") {
      for (const item of update.entries || []) box.append(el("div", "acp-text", (item.status === "completed" ? "✓ " : "○ ") + item.content));
    } else {
      const card = el("div", "acp-tool"), line = el("div", "acp-tool-line"), status = {pending:"待确认", in_progress:"进行中", completed:"完成", failed:"未完成"};
      line.append(icon(update.status === "completed" ? "check" : "bolt", 13), el("span", "grow", update.title || update.name || "工具调用"),
        el("span", "acp-tool-status " + (update.status || ""), status[update.status] || update.status || ""));
      card.append(line);
      for (const location of (update.locations || []).slice(0, 4)) card.append(el("div", "acp-footnote", location.path || ""));
      box.append(card);
    }
  }
  if (!entries.length) box.append(el("div", "acp-empty", "会话开始工作后，回复和操作会出现在这里。"));
}
$("acpActivity").ontoggle = () => { if ($("acpActivity").open && selected) loadACPActivity(selected); };
