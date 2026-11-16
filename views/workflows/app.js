/**
 * 工作流视图的交互逻辑：状态管理、防抖校验、节点浏览映射与 API 通信。
 *
 * 所有后端请求经 AstrBot bridge 转发给 comfy_runner/web.py；
 * HTML 拼接由 render.js 负责，状态转换与本地校验由 state.js 负责。
 */

import {
  buildInputRowFromField,
  buildOutputRowFromNode,
  collectLocalDraftIssues,
  contractToDraft,
  draftSnapshot,
  draftToContract,
  draftToSafeObject,
} from "./state.js";
import {
  renderModal,
  renderStatusFilters,
  renderValidationStripHtml,
  renderWorkflowList,
  renderWorkspace as renderWorkspaceHtml,
} from "./render.js";

const LIST_ROUTE = "workflows";
const DETAIL_ROUTE = "workflow";
const UPLOAD_ROUTE = "workflow/upload";
const SAVE_ROUTE = "workflow/contract";
const PREVIEW_ROUTE = "workflow/preview";
const DELETE_ROUTE = "workflow/delete";

const REMOTE_TYPE_MAP = {
  INT: "integer",
  FLOAT: "number",
  BOOLEAN: "boolean",
  STRING: "string",
  COMBO: "string",
  IMAGE: "image",
  VIDEO: "video",
  AUDIO: "audio",
};

const STRUCTURAL_INPUT_FIELDS = new Set(["type", "node", "field", "visible"]);
const EMPTY_WORKSPACE_HTML = document.getElementById("workspace")?.innerHTML ?? "";

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const state = {
  items: [],
  selectedStem: null,
  detail: null,
  draft: null,
  baselineSnapshot: "",
  filterText: "",
  statusFilter: "all",
  workflowDir: "",
  uiState: {
    activeTab: "form",
    confirmDelete: null,
    moreMenuOpen: false,
    nodeBrowserOpen: false,
    browserSearch: "",
    browserFilter: "tunable",
    browserShowLinks: false,
    validationExpanded: true,
    showRawSchema: false,
    remoteStatus: "idle",
    expandedInputs: new Set(),
    localIssues: [],
    errors: [],
    inspectPreview: null,
    isDirty: false,
    busy: false,
  },
  modal: null,
  pendingAction: null,
};

const elements = {
  toast: document.getElementById("global-toast"),
  workflowDir: document.getElementById("workflow-dir"),
  workflowDirWrap: document.getElementById("workflow-dir-wrap"),
  summaryStats: document.getElementById("summary-stats"),
  statusFilters: document.getElementById("status-filters"),
  searchInput: document.getElementById("search-input"),
  workflowList: document.getElementById("workflow-list"),
  workspace: document.getElementById("workspace"),
  uploadInput: document.getElementById("upload-input"),
  importButton: document.getElementById("btn-import"),
  checkRemoteButton: document.getElementById("btn-check-remote"),
  modalRoot: document.getElementById("modal-root"),
  dropOverlay: document.getElementById("drop-overlay"),
};

let toastTimer = null;
let previewTimer = null;
let dragCounter = 0;

/**
 * 按 AstrBot URL 参数或系统偏好同步暗/亮色主题。
 */
function syncTheme() {
  const params = new URLSearchParams(window.location.search);
  const urlTheme = params.get("theme");
  if (urlTheme === "dark" || urlTheme === "light") {
    document.documentElement.dataset.theme = urlTheme;
    return;
  }
  if (!document.documentElement.dataset.theme) {
    const dark = window.matchMedia?.("(prefers-color-scheme: dark)")?.matches;
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  }
}

/**
 * 显示顶部非阻断提示条，支持自动收起。
 *
 * @param {string} message 提示文字。
 * @param {"ok"|"warn"|"err"} kind 提示级别。
 */
function showToast(message, kind = "ok") {
  clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.className = `toast toast-${kind}`;
  elements.toast.hidden = false;
  toastTimer = setTimeout(() => {
    elements.toast.hidden = true;
  }, kind === "err" ? 6000 : 3200);
}

function showError(error) {
  showToast(error instanceof Error ? error.message : String(error), "err");
}

function isDirty() {
  if (!state.draft) return false;
  return draftSnapshot(state.draft) !== state.baselineSnapshot;
}

function syncDirtyState() {
  const dirty = isDirty();
  state.uiState.isDirty = dirty;
  const badge = document.getElementById("dirty-badge");
  if (badge) badge.hidden = !dirty;
  const revertBtn = document.getElementById("btn-revert");
  if (revertBtn) revertBtn.disabled = !dirty;
  renderList();
}

function renderTopbar() {
  const dir = state.workflowDir || "未读取";
  elements.workflowDir.textContent = dir;
  elements.workflowDirWrap.title = `工作流目录: ${dir}`;

  const total = state.items.length;
  const enabled = state.items.filter((i) => i.has_workflow && i.has_contract && i.ok && i.enabled).length;
  const broken = state.items.filter((i) => !i.has_workflow || (i.has_contract && !i.ok)).length;
  const parts = [`${total} 个工作流`, `${enabled} 个启用`];
  if (broken > 0) parts.push(`${broken} 个异常`);
  elements.summaryStats.textContent = parts.join(" · ");

  elements.statusFilters.innerHTML = renderStatusFilters(state.items, state.statusFilter);
}

function renderList() {
  const dirtyStem = state.uiState.isDirty ? state.selectedStem : null;
  elements.workflowList.innerHTML = renderWorkflowList(
    state.items,
    state.selectedStem,
    state.filterText,
    state.statusFilter,
    dirtyStem,
  );
}

function renderWorkspace(options = {}) {
  if (!state.detail || !state.draft) {
    elements.workspace.innerHTML = EMPTY_WORKSPACE_HTML;
    return;
  }
  const mainEl = elements.workspace.querySelector(".ws-main");
  const browserEl = elements.workspace.querySelector(".browser-list");
  const mainScroll = mainEl ? mainEl.scrollTop : 0;
  const browserScroll = browserEl ? browserEl.scrollTop : 0;

  state.uiState.isDirty = isDirty();
  state.uiState.localIssues = collectLocalDraftIssues(state.draft, state.detail.nodes);
  elements.workspace.innerHTML = renderWorkspaceHtml(state.detail, state.draft, state.uiState);

  if (!options.resetScroll) {
    const nextMain = elements.workspace.querySelector(".ws-main");
    if (nextMain) nextMain.scrollTop = mainScroll;
    const nextBrowser = elements.workspace.querySelector(".browser-list");
    if (nextBrowser) nextBrowser.scrollTop = browserScroll;
  }
}

function refreshValidationStripInPlace() {
  state.uiState.localIssues = collectLocalDraftIssues(state.draft, state.detail?.nodes);
  const current = document.getElementById("validation-strip");
  if (!current) return;
  const wrapper = document.createElement("div");
  wrapper.innerHTML = renderValidationStripHtml(state.uiState);
  if (wrapper.firstElementChild) {
    current.replaceWith(wrapper.firstElementChild);
  }
}

function renderModalLayer() {
  if (!state.modal) {
    elements.modalRoot.hidden = true;
    elements.modalRoot.innerHTML = "";
    return;
  }
  elements.modalRoot.innerHTML = renderModal(state.modal);
  elements.modalRoot.hidden = false;
}

function clearSelection() {
  state.selectedStem = null;
  state.detail = null;
  state.draft = null;
  state.baselineSnapshot = "";
  state.uiState.confirmDelete = null;
  state.uiState.moreMenuOpen = false;
  state.uiState.errors = [];
  state.uiState.localIssues = [];
  state.uiState.inspectPreview = null;
  state.uiState.isDirty = false;
  state.uiState.expandedInputs = new Set();
  renderList();
  renderWorkspace({ resetScroll: true });
}

async function loadWorkflows(options = {}) {
  const params = {};
  if (options.remote) params.remote = "true";
  if (options.refresh) params.refresh = "true";
  const data = await bridge.apiGet(LIST_ROUTE, params);
  state.items = Array.isArray(data.items) ? data.items : [];
  state.workflowDir = typeof data.workflow_dir === "string" ? data.workflow_dir : "";
  renderTopbar();

  const stillListed =
    state.selectedStem !== null &&
    state.items.some((item) => item.stem === state.selectedStem);
  if (state.selectedStem !== null && !stillListed) {
    clearSelection();
  } else {
    renderList();
  }
  return data;
}

async function selectWorkflow(stem, options = {}) {
  if (!stem) return;
  state.uiState.remoteStatus = "loading";
  const params = { stem, remote: "true" };
  if (options.refresh) params.refresh = "true";
  const detail = await bridge.apiGet(DETAIL_ROUTE, params);
  state.uiState.remoteStatus = "done";
  applyDetail(detail, { resetUi: true });
}

function applyDetail(detail, options = {}) {
  state.selectedStem = detail.stem ?? state.selectedStem;
  state.detail = detail;
  state.draft = contractToDraft(detail.contract, state.selectedStem, detail.nodes);
  state.baselineSnapshot = draftSnapshot(state.draft);
  state.uiState.confirmDelete = null;
  state.uiState.moreMenuOpen = false;
  state.uiState.errors = Array.isArray(detail.errors) ? detail.errors : [];
  state.uiState.inspectPreview = detail.inspect_preview ?? null;
  if (options.resetUi) {
    state.uiState.expandedInputs = new Set(
      !detail.has_contract && state.draft.inputs.length <= 2
        ? state.draft.inputs.map((_, idx) => idx)
        : [],
    );
  }
  renderList();
  renderWorkspace({ resetScroll: Boolean(options.resetUi) });
}

function guardUnsaved(action) {
  if (!isDirty()) {
    action();
    return;
  }
  state.pendingAction = action;
  state.modal = { kind: "unsaved", stem: state.selectedStem };
  renderModalLayer();
}

async function checkRemoteCurrent() {
  if (!state.selectedStem) return;
  const draftBackup = state.draft;
  const baselineBackup = state.baselineSnapshot;
  const wasDirty = isDirty();
  state.uiState.remoteStatus = "loading";
  renderWorkspace();
  const fresh = await bridge.apiGet(DETAIL_ROUTE, {
    stem: state.selectedStem,
    remote: "true",
    refresh: "true",
  });
  state.uiState.remoteStatus = "done";
  state.detail = {
    ...state.detail,
    nodes: Array.isArray(fresh.nodes) ? fresh.nodes : [],
    has_workflow: fresh.has_workflow,
    has_contract: fresh.has_contract,
  };
  if (wasDirty) {
    state.draft = draftBackup;
    state.baselineSnapshot = baselineBackup;
    await runServerPreview(true);
  } else {
    state.uiState.errors = Array.isArray(fresh.errors) ? fresh.errors : [];
    state.uiState.inspectPreview = fresh.inspect_preview ?? null;
  }
  renderWorkspace();
  const unreachable = (state.detail.nodes ?? []).every((n) => n.remote?.error);
  if (state.detail.nodes?.length && unreachable) {
    showToast("ComfyUI 离线或节点定义不可达，当前保留本地校验", "warn");
  } else {
    showToast("已完成 ComfyUI 远端节点定义核对", "ok");
  }
}

async function uploadWorkflow(file) {
  const detail = await bridge.upload(UPLOAD_ROUTE, file);
  await loadWorkflows();
  applyDetail(detail, { resetUi: true });
  showToast(`已导入工作流 ${detail.stem}`, "ok");
}

async function checkAllRemote() {
  elements.checkRemoteButton.disabled = true;
  try {
    await loadWorkflows({ remote: true, refresh: true });
    const total = state.items.length;
    const failed = state.items.filter((item) => !item.ok).length;
    showToast(
      `全部远端核对完成：${total} 个工作流，${failed} 个存在问题`,
      failed ? "warn" : "ok",
    );
    const selected = state.items.find((item) => item.stem === state.selectedStem);
    if (selected && !isDirty()) {
      state.uiState.errors = Array.isArray(selected.errors) ? selected.errors : [];
      renderWorkspace();
    }
  } finally {
    elements.checkRemoteButton.disabled = false;
  }
}

async function saveContract() {
  if (!state.draft || !state.selectedStem) return;
  const contract = draftToContract(state.draft);
  const detail = await bridge.apiPost(SAVE_ROUTE, {
    stem: state.selectedStem,
    contract,
  });
  await loadWorkflows();
  applyDetail(detail);
  showToast("工具契约已保存并热刷新生效", "ok");
}

function scheduleLivePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => {
    runServerPreview(false).catch(() => {});
  }, 260);
}

async function runServerPreview(remote) {
  if (!state.draft || !state.selectedStem) return;
  let contract;
  try {
    contract = draftToContract(state.draft);
  } catch {
    state.uiState.inspectPreview = null;
    refreshValidationStripInPlace();
    return;
  }
  const result = await bridge.apiPost(PREVIEW_ROUTE, {
    stem: state.selectedStem,
    contract,
    remote,
  });
  state.uiState.errors = Array.isArray(result.errors) ? result.errors : [];
  state.uiState.inspectPreview = result.inspect_preview ?? null;
  refreshValidationStripInPlace();
}

async function deleteSelection(forceDisableFirst = false) {
  const scope = state.uiState.confirmDelete;
  if (scope !== "contract" && scope !== "all") return;
  if (forceDisableFirst && scope === "all" && state.detail?.contract) {
    const disabledContract = { ...state.detail.contract, enabled: false };
    await bridge.apiPost(SAVE_ROUTE, {
      stem: state.selectedStem,
      contract: disabledContract,
    });
  }
  const result = await bridge.apiPost(DELETE_ROUTE, {
    stem: state.selectedStem,
    scope,
  });
  state.uiState.confirmDelete = null;
  await loadWorkflows();
  if (state.selectedStem === result.stem) {
    await selectWorkflow(result.stem);
  }
  showToast(
    scope === "all"
      ? `已删除工作流 ${result.stem}`
      : `已删除工具契约 ${result.stem}`,
    "ok",
  );
}

async function runTask(task, options = {}) {
  if (state.uiState.busy) return;
  state.uiState.busy = true;
  try {
    await task();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (options.showErrors) {
      state.uiState.errors = message.split("\n").filter(Boolean);
      renderWorkspace();
    }
    showError(error);
  } finally {
    state.uiState.busy = false;
  }
}

function findNode(nodeId) {
  const nodes = Array.isArray(state.detail?.nodes) ? state.detail.nodes : [];
  return nodes.find((node) => String(node.id) === String(nodeId)) ?? null;
}

function firstTunableFieldName(nodeId) {
  const node = findNode(nodeId);
  const fields = Array.isArray(node?.fields) ? node.fields : [];
  const nonLink = fields.find((f) => !f.is_link);
  return nonLink ? nonLink.name : fields[0]?.name ?? "";
}

function remoteFieldOf(row) {
  if (!row) return null;
  const node = findNode(row.node);
  return node?.remote?.fields?.[row.field] ?? null;
}

function applyRemoteField(index) {
  const row = state.draft.inputs[index];
  const remote = remoteFieldOf(row);
  if (!row || !remote) return;
  const mapped = REMOTE_TYPE_MAP[String(remote.suggested_type ?? "").toUpperCase()];
  if (mapped) row.type = mapped;
  if (Array.isArray(remote.enum) && remote.enum.length > 0) {
    row.enumText = remote.enum.map((value) => String(value)).join("\n");
  }
  if (row.type === "integer" || row.type === "number") {
    row.minimum = limitText(remote.minimum);
    row.maximum = limitText(remote.maximum);
  }
  renderWorkspace();
  scheduleLivePreview();
  showToast("已同步远端节点约束", "ok");
}

function limitText(value) {
  return value === null || value === undefined ? "" : String(value);
}

function newInputRow() {
  return {
    key: "",
    type: "string",
    description: "",
    visible: true,
    required: false,
    node: "",
    field: "",
    enumText: "",
    minLength: "",
    maxLength: "",
    minimum: "",
    maximum: "",
    valueTemplate: "",
  };
}

function newOutputRow() {
  return { key: "", type: "image", node: "", field: "images" };
}

function addInputFromNodeBrowser(nodeId, fieldName) {
  const node = findNode(nodeId);
  const field = (node?.fields ?? []).find((f) => f.name === fieldName);
  if (!node || !field || field.is_link) return;
  const row = buildInputRowFromField(node, field, state.draft.inputs);
  const index = state.draft.inputs.length;
  state.draft.inputs.push(row);
  state.uiState.activeTab = "form";
  state.uiState.expandedInputs.add(index);
  renderWorkspace();
  syncDirtyState();
  scheduleLivePreview();
  focusElement(`#input-${index}-description`);
}

function addOutputFromNodeBrowser(nodeId) {
  const node = findNode(nodeId);
  if (!node) return;
  const row = buildOutputRowFromNode(node, state.draft.outputs);
  const index = state.draft.outputs.length;
  state.draft.outputs.push(row);
  state.uiState.activeTab = "form";
  renderWorkspace();
  syncDirtyState();
  scheduleLivePreview();
  focusElement(`#output-${index}-key`);
}

function focusElement(selector) {
  requestAnimationFrame(() => {
    const el = document.querySelector(selector);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.focus();
  });
}

function focusIssueTarget(issueIndex) {
  const items = [];
  const seen = new Set();
  for (const issue of state.uiState.localIssues ?? []) {
    if (!seen.has(issue.message)) {
      seen.add(issue.message);
      items.push(issue);
    }
  }
  for (const msg of state.uiState.errors ?? []) {
    if (!seen.has(msg)) {
      seen.add(msg);
      const paramMatch = /参数\s+([a-z0-9_]+)/.exec(msg);
      const outputMatch = /输出\s+([a-z0-9_]+)/.exec(msg);
      items.push({
        message: msg,
        target: paramMatch
          ? { kind: "inputKey", key: paramMatch[1] }
          : outputMatch
            ? { kind: "outputKey", key: outputMatch[1] }
            : null,
      });
    }
  }
  const target = items[issueIndex]?.target;
  if (!target) return;

  state.uiState.activeTab = "form";
  if (target.kind === "basic") {
    renderWorkspace();
    focusElement(`#contract-${target.field}`);
    return;
  }
  if (target.kind === "input" || target.kind === "inputKey") {
    const index =
      target.kind === "input"
        ? target.index
        : state.draft.inputs.findIndex((r) => r.key === target.key);
    if (index < 0) return;
    state.uiState.expandedInputs.add(index);
    renderWorkspace();
    const fieldId = target.field ? `#input-${index}-${target.field}` : `#input-${index}-key`;
    focusElement(fieldId);
    return;
  }
  if (target.kind === "output" || target.kind === "outputKey") {
    const index =
      target.kind === "output"
        ? target.index
        : state.draft.outputs.findIndex((r) => r.key === target.key);
    if (index < 0) {
      renderWorkspace();
      return;
    }
    renderWorkspace();
    const fieldId = target.field ? `#output-${index}-${target.field}` : `#output-${index}-key`;
    focusElement(fieldId);
  }
}

function toggleInputExpanded(index) {
  if (state.uiState.expandedInputs.has(index)) {
    state.uiState.expandedInputs.delete(index);
  } else {
    state.uiState.expandedInputs.add(index);
  }
  renderWorkspace();
}

function moveInputRow(index, delta) {
  const nextIndex = index + delta;
  if (nextIndex < 0 || nextIndex >= state.draft.inputs.length) return;
  const list = state.draft.inputs;
  [list[index], list[nextIndex]] = [list[nextIndex], list[index]];
  const nextExpanded = new Set();
  for (const idx of state.uiState.expandedInputs) {
    if (idx === index) nextExpanded.add(nextIndex);
    else if (idx === nextIndex) nextExpanded.add(index);
    else nextExpanded.add(idx);
  }
  state.uiState.expandedInputs = nextExpanded;
  renderWorkspace();
  syncDirtyState();
}

function removeInputRow(index) {
  state.draft.inputs.splice(index, 1);
  const nextExpanded = new Set();
  for (const idx of state.uiState.expandedInputs) {
    if (idx < index) nextExpanded.add(idx);
    else if (idx > index) nextExpanded.add(idx - 1);
  }
  state.uiState.expandedInputs = nextExpanded;
  renderWorkspace();
  syncDirtyState();
  scheduleLivePreview();
}

function switchTab(tab) {
  if (!tab || tab === state.uiState.activeTab) return;
  state.uiState.activeTab = tab;
  renderWorkspace();
}

function applyJsonFromEditor() {
  const editor = document.getElementById("raw-json-editor");
  if (!editor) return;
  try {
    const parsed = JSON.parse(editor.value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("契约 JSON 必须是一个对象");
    }
    state.draft = contractToDraft(parsed, state.selectedStem, state.detail?.nodes ?? []);
    state.uiState.activeTab = "form";
    renderWorkspace();
    syncDirtyState();
    scheduleLivePreview();
    showToast("已把 JSON 应用到表单，保存后生效", "ok");
  } catch (error) {
    showError(error);
  }
}

function formatJsonInEditor() {
  const editor = document.getElementById("raw-json-editor");
  if (!editor) return;
  try {
    const parsed = JSON.parse(editor.value);
    editor.value = JSON.stringify(parsed, null, 2);
  } catch (error) {
    showError(error);
  }
}

async function copyText(text, successMsg) {
  try {
    await navigator.clipboard.writeText(text);
    showToast(successMsg, "ok");
  } catch {
    showToast("浏览器剪贴板写入失败，请手动复制", "warn");
  }
}

function exportContractFile() {
  const safe = draftToSafeObject(state.draft);
  const blob = new Blob([JSON.stringify(safe.contract, null, 2) + "\n"], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${state.selectedStem}.tool.json`;
  link.click();
  URL.revokeObjectURL(url);
  showToast(`已导出 ${state.selectedStem}.tool.json`, "ok");
}

function revertDraft() {
  if (!state.detail) return;
  state.draft = contractToDraft(state.detail.contract, state.selectedStem, state.detail.nodes);
  state.baselineSnapshot = draftSnapshot(state.draft);
  state.uiState.errors = Array.isArray(state.detail.errors) ? state.detail.errors : [];
  state.uiState.inspectPreview = state.detail.inspect_preview ?? null;
  renderWorkspace();
  syncDirtyState();
  showToast("已还原为上次保存的状态", "ok");
}

function updateBasics(field, target) {
  if (!state.draft) return;
  if (field === "enabled") {
    state.draft.enabled = target.checked;
    syncDirtyState();
    scheduleLivePreview();
    return;
  }
  if (field === "name" || field === "description") {
    state.draft[field] = target.value;
    syncDirtyState();
    refreshValidationStripInPlace();
    scheduleLivePreview();
  }
}

function updateInputRow(index, field, target) {
  const row = state.draft?.inputs?.[index];
  if (!row || !field) return;
  if (field === "visible" || field === "required") {
    row[field] = target.checked;
    if (field === "visible" && !target.checked) row.required = false;
    if (field === "visible" && target.checked) row.valueTemplate = "";
  } else if (field === "node") {
    row.node = target.value;
    row.field = firstTunableFieldName(target.value);
  } else {
    row[field] = target.value;
  }
  if (STRUCTURAL_INPUT_FIELDS.has(field)) {
    renderWorkspace();
  } else {
    const keyEl = document.querySelector(`[data-input-index="${index}"] .param-key`);
    if (keyEl && field === "key") {
      keyEl.textContent = row.key || `未命名参数 #${index + 1}`;
      keyEl.classList.toggle("is-empty", !row.key);
    }
    refreshValidationStripInPlace();
  }
  syncDirtyState();
  scheduleLivePreview();
}

function updateOutputRow(index, field, target) {
  const row = state.draft?.outputs?.[index];
  if (!row || !field) return;
  row[field] = target.value;
  syncDirtyState();
  refreshValidationStripInPlace();
  scheduleLivePreview();
}

function handleFormEvent(event) {
  const target = event.target;
  if (target.dataset?.browserSearch) {
    state.uiState.browserSearch = target.value;
    const caret = target.selectionStart;
    renderWorkspace();
    requestAnimationFrame(() => {
      const next = document.querySelector("[data-browser-search]");
      if (next) {
        next.focus();
        if (typeof caret === "number") next.setSelectionRange(caret, caret);
      }
    });
    return;
  }
  if (target.dataset?.browserLinks) {
    if (event.type !== "change") return;
    state.uiState.browserShowLinks = target.checked;
    renderWorkspace();
    return;
  }

  if (
    !(
      target instanceof HTMLInputElement ||
      target instanceof HTMLSelectElement ||
      target instanceof HTMLTextAreaElement
    )
  ) {
    return;
  }
  const toggle =
    target instanceof HTMLSelectElement ||
    (target instanceof HTMLInputElement && target.type === "checkbox");
  if (event.type === "input" ? toggle : !toggle) return;

  const inputCard = target.closest("[data-input-index]");
  if (inputCard) {
    updateInputRow(Number(inputCard.dataset.inputIndex), target.dataset.inputField, target);
    return;
  }
  const outputCard = target.closest("[data-output-index]");
  if (outputCard) {
    updateOutputRow(Number(outputCard.dataset.outputIndex), target.dataset.outputField, target);
    return;
  }
  if (target.dataset.basicField) {
    updateBasics(target.dataset.basicField, target);
  }
}

function handleActionClick(event) {
  if (!(event.target instanceof Element)) return;

  if (event.target.closest("[data-empty-import]")) {
    openImportModal("file");
    return;
  }

  const button = event.target.closest("[data-action]");
  if (!button) return;

  const action = button.dataset.action;
  const inputIndex = button.closest("[data-input-index]")?.dataset.inputIndex;
  const outputIndex = button.closest("[data-output-index]")?.dataset.outputIndex;

  if (action === "toggle-input" && event.target.closest(".param-row-actions")) {
    return;
  }

  switch (action) {
    case "toggle-more-menu":
      state.uiState.moreMenuOpen = !state.uiState.moreMenuOpen;
      renderWorkspace();
      return;
    case "check-remote":
      state.uiState.moreMenuOpen = false;
      runTask(checkRemoteCurrent, { showErrors: true });
      return;
    case "revert-draft":
      revertDraft();
      return;
    case "save-contract":
      runTask(saveContract, { showErrors: true });
      return;
    case "copy-contract":
      state.uiState.moreMenuOpen = false;
      renderWorkspace();
      copyText(
        JSON.stringify(draftToSafeObject(state.draft).contract, null, 2),
        "已复制工具契约 JSON",
      );
      return;
    case "copy-schema": {
      const schemaObj = state.uiState.inspectPreview ?? {};
      copyText(JSON.stringify(schemaObj, null, 2), "已复制模型 Schema");
      return;
    }
    case "export-contract":
      state.uiState.moreMenuOpen = false;
      renderWorkspace();
      exportContractFile();
      return;
    case "delete-contract":
      state.uiState.moreMenuOpen = false;
      state.uiState.confirmDelete = "contract";
      renderWorkspace();
      return;
    case "delete-workflow":
      state.uiState.moreMenuOpen = false;
      state.uiState.confirmDelete = "all";
      renderWorkspace();
      return;
    case "confirm-delete":
      runTask(() => deleteSelection(false));
      return;
    case "disable-and-delete":
      runTask(() => deleteSelection(true));
      return;
    case "cancel-delete":
      state.uiState.confirmDelete = null;
      renderWorkspace();
      return;
    case "toggle-validation":
      state.uiState.validationExpanded = state.uiState.validationExpanded === false;
      renderWorkspace();
      return;
    case "focus-issue":
      focusIssueTarget(Number(button.dataset.issueIndex));
      return;
    case "switch-tab":
      switchTab(button.dataset.tab);
      return;
    case "toggle-node-browser":
      state.uiState.nodeBrowserOpen = !state.uiState.nodeBrowserOpen;
      renderWorkspace();
      return;
    case "open-node-browser-input":
      state.uiState.nodeBrowserOpen = true;
      state.uiState.browserFilter = "tunable";
      renderWorkspace();
      return;
    case "open-node-browser-output":
      state.uiState.nodeBrowserOpen = true;
      state.uiState.browserFilter = "output";
      renderWorkspace();
      return;
    case "close-node-browser":
      state.uiState.nodeBrowserOpen = false;
      renderWorkspace();
      return;
    case "browser-filter":
      state.uiState.browserFilter = button.dataset.filter;
      renderWorkspace();
      return;
    case "browser-add-input":
      addInputFromNodeBrowser(button.dataset.nodeId, button.dataset.fieldName);
      return;
    case "browser-add-output":
      addOutputFromNodeBrowser(button.dataset.nodeId);
      return;
    case "jump-input": {
      const idx = Number(button.dataset.inputIndex);
      state.uiState.activeTab = "form";
      state.uiState.expandedInputs.add(idx);
      renderWorkspace();
      focusElement(`#input-${idx}-key`);
      return;
    }
    case "add-input": {
      const idx = state.draft.inputs.length;
      state.draft.inputs.push(newInputRow());
      state.uiState.expandedInputs.add(idx);
      renderWorkspace();
      syncDirtyState();
      focusElement(`#input-${idx}-key`);
      return;
    }
    case "toggle-input":
      toggleInputExpanded(Number(inputIndex));
      return;
    case "expand-all-inputs":
      state.uiState.expandedInputs = new Set(state.draft.inputs.map((_, i) => i));
      renderWorkspace();
      return;
    case "collapse-all-inputs":
      state.uiState.expandedInputs = new Set();
      renderWorkspace();
      return;
    case "move-input-up":
      moveInputRow(Number(inputIndex), -1);
      return;
    case "move-input-down":
      moveInputRow(Number(inputIndex), 1);
      return;
    case "remove-input":
      removeInputRow(Number(inputIndex));
      return;
    case "apply-remote-field":
      applyRemoteField(Number(inputIndex));
      return;
    case "add-output": {
      const idx = state.draft.outputs.length;
      state.draft.outputs.push(newOutputRow());
      renderWorkspace();
      syncDirtyState();
      focusElement(`#output-${idx}-key`);
      return;
    }
    case "remove-output":
      state.draft.outputs.splice(Number(outputIndex), 1);
      renderWorkspace();
      syncDirtyState();
      scheduleLivePreview();
      return;
    case "set-output-field": {
      const row = state.draft.outputs[Number(outputIndex)];
      if (!row) return;
      row.field = button.dataset.value ?? "";
      renderWorkspace();
      syncDirtyState();
      scheduleLivePreview();
      return;
    }
    case "toggle-raw-schema":
      state.uiState.showRawSchema = !state.uiState.showRawSchema;
      renderWorkspace();
      return;
    case "format-json":
      formatJsonInEditor();
      return;
    case "apply-json":
      applyJsonFromEditor();
      return;
  }
}

function openImportModal(mode = "file") {
  state.modal = { kind: "import", mode, stem: "", jsonText: "", error: "" };
  renderModalLayer();
}

async function handleModalClick(event) {
  if (!(event.target instanceof Element)) return;
  if (event.target === elements.modalRoot) {
    state.modal = null;
    state.pendingAction = null;
    renderModalLayer();
    return;
  }
  const btn = event.target.closest("[data-modal-action]");
  if (!btn) return;
  const action = btn.dataset.modalAction;

  switch (action) {
    case "close":
      state.modal = null;
      state.pendingAction = null;
      renderModalLayer();
      return;
    case "import-mode":
      state.modal.mode = btn.dataset.mode;
      state.modal.error = "";
      renderModalLayer();
      return;
    case "pick-file":
      elements.uploadInput.click();
      return;
    case "submit-paste": {
      const stem = document.getElementById("paste-stem")?.value.trim() ?? "";
      const jsonText = document.getElementById("paste-json")?.value ?? "";
      state.modal.stem = stem;
      state.modal.jsonText = jsonText;
      if (!stem) {
        state.modal.error = "请填写工作流名称";
        renderModalLayer();
        return;
      }
      if (!jsonText.trim()) {
        state.modal.error = "请粘贴工作流 JSON 内容";
        renderModalLayer();
        return;
      }
      try {
        JSON.parse(jsonText);
      } catch (err) {
        state.modal.error = `JSON 格式有误: ${err.message}`;
        renderModalLayer();
        return;
      }
      const file = new File([jsonText], `${stem}.json`, { type: "application/json" });
      state.modal = null;
      renderModalLayer();
      await runTask(() => uploadWorkflow(file));
      return;
    }
    case "discard-and-continue": {
      const next = state.pendingAction;
      state.modal = null;
      state.pendingAction = null;
      renderModalLayer();
      if (next) next();
      return;
    }
    case "save-and-continue": {
      const next = state.pendingAction;
      await runTask(async () => {
        await saveContract();
        state.modal = null;
        state.pendingAction = null;
        renderModalLayer();
        if (next) next();
      }, { showErrors: true });
      return;
    }
  }
}

function bindEvents() {
  elements.importButton.addEventListener("click", () => openImportModal("file"));

  elements.uploadInput.addEventListener("change", () => {
    const file = elements.uploadInput.files?.[0];
    elements.uploadInput.value = "";
    if (!file) return;
    state.modal = null;
    renderModalLayer();
    guardUnsaved(() => runTask(() => uploadWorkflow(file)));
  });

  elements.checkRemoteButton.addEventListener("click", () => {
    runTask(checkAllRemote);
  });

  elements.searchInput.addEventListener("input", () => {
    state.filterText = elements.searchInput.value;
    renderList();
  });

  elements.statusFilters.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const chip = event.target.closest("[data-status-filter]");
    if (!chip) return;
    state.statusFilter = chip.dataset.statusFilter;
    renderTopbar();
    renderList();
  });

  elements.workflowList.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const item = event.target.closest(".workflow-item");
    const stem = item?.dataset.stem;
    if (!stem || stem === state.selectedStem) return;
    guardUnsaved(() => runTask(() => selectWorkflow(stem)));
  });

  elements.workspace.addEventListener("click", handleActionClick);
  elements.workspace.addEventListener("input", handleFormEvent);
  elements.workspace.addEventListener("change", handleFormEvent);
  elements.modalRoot.addEventListener("click", handleModalClick);

  document.addEventListener("click", (event) => {
    if (!state.uiState.moreMenuOpen) return;
    if (event.target instanceof Element && event.target.closest(".dropdown-wrap")) return;
    state.uiState.moreMenuOpen = false;
    renderWorkspace();
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      if (state.selectedStem && state.draft) {
        event.preventDefault();
        runTask(saveContract, { showErrors: true });
      }
      return;
    }
    if (event.key === "Escape") {
      if (state.modal) {
        state.modal = null;
        state.pendingAction = null;
        renderModalLayer();
      } else if (state.uiState.moreMenuOpen) {
        state.uiState.moreMenuOpen = false;
        renderWorkspace();
      }
    }
  });

  window.addEventListener("dragenter", (event) => {
    if (!event.dataTransfer?.types?.includes("Files")) return;
    event.preventDefault();
    dragCounter += 1;
    elements.dropOverlay.hidden = false;
  });

  window.addEventListener("dragover", (event) => {
    if (!event.dataTransfer?.types?.includes("Files")) return;
    event.preventDefault();
  });

  window.addEventListener("dragleave", () => {
    dragCounter = Math.max(0, dragCounter - 1);
    if (dragCounter === 0) elements.dropOverlay.hidden = true;
  });

  window.addEventListener("drop", (event) => {
    event.preventDefault();
    dragCounter = 0;
    elements.dropOverlay.hidden = true;
    const file = event.dataTransfer?.files?.[0];
    if (!file) return;
    if (!file.name.endsWith(".json")) {
      showToast("请拖入 .json 格式的 ComfyUI 工作流文件", "err");
      return;
    }
    state.modal = null;
    renderModalLayer();
    guardUnsaved(() => runTask(() => uploadWorkflow(file)));
  });

  window.addEventListener("beforeunload", (event) => {
    if (isDirty()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
}

async function start() {
  syncTheme();
  if (!bridge) {
    showToast("未检测到 AstrBot 视图 bridge，请从 AstrBot WebUI 插件页打开本视图", "err");
    return;
  }
  await bridge.ready();
  bindEvents();
  try {
    await loadWorkflows();
  } catch (error) {
    showError(error);
  }
  renderWorkspace({ resetScroll: true });
}

await start();
