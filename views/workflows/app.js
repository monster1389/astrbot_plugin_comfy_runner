/**
 * 工作流视图的交互：读取工作流、编辑契约草稿、调用后端 Web API。
 *
 * 页面不直接读写工作流目录：全部请求经 AstrBot bridge 转给 comfy_runner/web.py，
 * HTML 生成交给 render.js，草稿与契约的转换交给 state.js。
 */

import { contractToDraft, draftToContract } from "./state.js";
import {
  escapeHtml,
  renderWorkspace as renderWorkspaceHtml,
  renderWorkflowList,
} from "./render.js";

const LIST_ROUTE = "workflows";
const DETAIL_ROUTE = "workflow";
const UPLOAD_ROUTE = "workflow/upload";
const SAVE_ROUTE = "workflow/contract";
const PREVIEW_ROUTE = "workflow/preview";
const DELETE_ROUTE = "workflow/delete";

const EMPTY_WORKSPACE = `<div class="empty-state">
  <p>请从左侧选择一个工作流，或上传新的 ComfyUI API 格式工作流文件</p>
</div>`;

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

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const state = {
  items: [],
  selectedStem: null,
  detail: null,
  draft: null,
  filterText: "",
  workflowDir: "",
  activeTools: [],
  uiState: {
    activeTab: "form",
    confirmDelete: null,
    errors: [],
    inspectPreview: null,
    busy: false,
  },
};

const elements = {
  banner: document.getElementById("global-banner"),
  workflowDir: document.getElementById("workflow-dir"),
  activeTools: document.getElementById("active-tools"),
  searchInput: document.getElementById("search-input"),
  workflowList: document.getElementById("workflow-list"),
  workspace: document.getElementById("workspace"),
  uploadInput: document.getElementById("upload-input"),
  uploadButton: document.getElementById("btn-upload"),
  checkLocalButton: document.getElementById("btn-check-local"),
  checkRemoteButton: document.getElementById("btn-check-remote"),
};

/**
 * 在全局横幅里显示一条提示。
 *
 * @param {string} message 提示文字。
 * @param {"ok"|"warn"|"err"} kind 提示等级。
 */
function showMessage(message, kind) {
  elements.banner.textContent = message;
  elements.banner.className = `banner banner-${kind ?? "ok"}`;
  elements.banner.hidden = false;
}

/**
 * 把异常显示成错误横幅。
 *
 * @param {unknown} error 捕获到的异常。
 */
function showError(error) {
  showMessage(error instanceof Error ? error.message : String(error), "err");
}

/**
 * 渲染左侧列表。
 */
function renderList() {
  elements.workflowList.innerHTML = renderWorkflowList(
    state.items,
    state.selectedStem,
    state.filterText,
  );
}

/**
 * 渲染顶栏的工作流目录与已启用工具。
 */
function renderTopbar() {
  elements.workflowDir.textContent = state.workflowDir || "未读取";
  if (state.activeTools.length === 0) {
    elements.activeTools.innerHTML = `<span class="chip chip-muted">无启用工具</span>`;
    return;
  }
  elements.activeTools.innerHTML = state.activeTools
    .map((name) => `<span class="chip">${escapeHtml(name)}</span>`)
    .join("");
}

/**
 * 渲染右侧工作区，没有选中工作流时显示空状态。
 */
function renderWorkspace() {
  if (!state.detail || !state.draft) {
    elements.workspace.innerHTML = EMPTY_WORKSPACE;
    return;
  }
  elements.workspace.innerHTML = renderWorkspaceHtml(
    state.detail,
    state.draft,
    state.uiState,
  );
}

/**
 * 清空选中状态。
 */
function clearSelection() {
  state.selectedStem = null;
  state.detail = null;
  state.draft = null;
  state.uiState.confirmDelete = null;
  state.uiState.errors = [];
  state.uiState.inspectPreview = null;
  renderList();
  renderWorkspace();
}

/**
 * 读取工作流列表，并同步顶栏与列表。
 *
 * @param {object} options 读取选项。
 * @param {boolean} options.remote 是否用 ComfyUI 节点定义核对。
 * @returns {Promise<object>} 后端返回的列表响应。
 */
async function loadWorkflows(options = {}) {
  const params = options.remote ? { remote: "true" } : {};
  const data = await bridge.apiGet(LIST_ROUTE, params);
  state.items = Array.isArray(data.items) ? data.items : [];
  state.workflowDir =
    typeof data.workflow_dir === "string" ? data.workflow_dir : "";
  state.activeTools = Array.isArray(data.active_tools) ? data.active_tools : [];
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

/**
 * 选中一个工作流并读取详情。
 *
 * @param {string} stem 工作流文件名去掉 `.json` 的部分。
 */
async function selectWorkflow(stem) {
  if (!stem) return;
  const detail = await bridge.apiGet(DETAIL_ROUTE, { stem });
  applyDetail(detail);
}

/**
 * 把后端返回的详情铺成选中状态与草稿。
 *
 * @param {object} detail 工作流详情。
 */
function applyDetail(detail) {
  state.selectedStem = detail.stem ?? state.selectedStem;
  state.detail = detail;
  state.draft = contractToDraft(detail.contract, state.selectedStem, detail.nodes);
  state.uiState.confirmDelete = null;
  state.uiState.errors = Array.isArray(detail.errors) ? detail.errors : [];
  state.uiState.inspectPreview = detail.inspect_preview ?? null;
  renderList();
  renderWorkspace();
}

/**
 * 用 ComfyUI 节点定义刷新节点下拉与被引用的字段。
 */
async function fetchRemoteNodes() {
  const fresh = await bridge.apiGet(DETAIL_ROUTE, {
    stem: state.selectedStem,
    remote: "true",
  });
  state.detail = {
    ...state.detail,
    nodes: Array.isArray(fresh.nodes) ? fresh.nodes : [],
    has_workflow: fresh.has_workflow,
    has_contract: fresh.has_contract,
  };
  state.uiState.errors = Array.isArray(fresh.errors) ? fresh.errors : [];
  state.uiState.inspectPreview = fresh.inspect_preview ?? null;
  renderWorkspace();
  showMessage("已拉取 ComfyUI 节点定义", "ok");
}

/**
 * 上传一份工作流文件，成功后刷新列表并选中它。
 *
 * @param {File} file 用户选择的工作流文件。
 */
async function uploadWorkflow(file) {
  const detail = await bridge.upload(UPLOAD_ROUTE, file);
  await loadWorkflows();
  applyDetail(detail);
  showMessage(`已上传 ${detail.stem}`, "ok");
}

/**
 * 检查全部工作流。
 *
 * @param {boolean} remote 是否用 ComfyUI 节点定义核对。
 */
async function checkAll(remote) {
  await loadWorkflows({ remote });
  const total = state.items.length;
  const failed = state.items.filter((item) => !item.ok).length;
  const scope = remote ? "远端" : "本地";
  showMessage(
    `${scope}检查完成：${total} 个工作流，${failed} 个有问题`,
    failed ? "warn" : "ok",
  );
  const selected = state.items.find((item) => item.stem === state.selectedStem);
  if (!selected) return;
  state.uiState.errors = Array.isArray(selected.errors) ? selected.errors : [];
  renderWorkspace();
}

/**
 * 保存当前草稿对应的工具契约。
 */
async function saveContract() {
  const contract = draftToContract(state.draft);
  const detail = await bridge.apiPost(SAVE_ROUTE, {
    stem: state.selectedStem,
    contract,
  });
  await loadWorkflows();
  applyDetail(detail);
  showMessage("契约已保存，工具已刷新", "ok");
}

/**
 * 用后端校验当前草稿并生成模型视图。
 *
 * @param {boolean} remote 是否用 ComfyUI 节点定义核对。
 */
async function refreshPreview(remote) {
  const contract = draftToContract(state.draft);
  const result = await bridge.apiPost(PREVIEW_ROUTE, {
    stem: state.selectedStem,
    contract,
    remote,
  });
  state.uiState.errors = Array.isArray(result.errors) ? result.errors : [];
  state.uiState.inspectPreview = result.inspect_preview ?? null;
  renderWorkspace();
  showMessage(
    result.ok ? "契约检查通过" : "契约检查未通过",
    result.ok ? "ok" : "err",
  );
}

/**
 * 按当前内联确认的范围删除契约或工作流。
 */
async function deleteSelection() {
  const scope = state.uiState.confirmDelete;
  if (scope !== "contract" && scope !== "all") return;
  const result = await bridge.apiPost(DELETE_ROUTE, {
    stem: state.selectedStem,
    scope,
  });
  state.uiState.confirmDelete = null;
  await loadWorkflows();
  if (state.selectedStem === result.stem) {
    await selectWorkflow(result.stem);
  }
  showMessage(
    scope === "all"
      ? `已删除工作流 ${result.stem}`
      : `已删除工具契约 ${result.stem}`,
    "ok",
  );
}

/**
 * 执行一次异步操作，期间拦住重复点击，失败时把原因显示在横幅里。
 *
 * @param {() => Promise<void>} task 待执行的操作。
 * @param {object} options 执行选项。
 * @param {boolean} options.showErrors 失败时把错误逐行写进校验面板。
 */
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

/**
 * 按节点 id 找节点。
 *
 * @param {string} nodeId 节点 id。
 * @returns {object|null} 命中节点，找不到返回 null。
 */
function findNode(nodeId) {
  const nodes = Array.isArray(state.detail?.nodes) ? state.detail.nodes : [];
  return nodes.find((node) => String(node.id) === String(nodeId)) ?? null;
}

/**
 * 取节点定义里的第一个字段名。
 *
 * @param {string} nodeId 节点 id。
 * @returns {string} 字段名，节点不存在或没有字段时为空串。
 */
function firstFieldName(nodeId) {
  const node = findNode(nodeId);
  const fields = Array.isArray(node?.fields) ? node.fields : [];
  return fields.length ? fields[0].name : "";
}

/**
 * 取当前行映射的远端字段定义。
 *
 * @param {object} row 参数行。
 * @returns {object|null} 远端字段定义，没有拉取过节点定义时返回 null。
 */
function remoteFieldOf(row) {
  if (!row) return null;
  const node = findNode(row.node);
  return node?.remote?.fields?.[row.field] ?? null;
}

/**
 * 把远端字段定义里的类型、枚举与范围填进当前行。
 *
 * @param {number} index 参数行下标。
 */
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
}

/**
 * 把远端限值转成输入框文本。
 *
 * @param {unknown} value 远端限值。
 * @returns {string} 限值文本，未提供时为空串。
 */
function limitText(value) {
  return value === null || value === undefined ? "" : String(value);
}

/**
 * 生成一条空参数行。
 *
 * @returns {object} 参数行。
 */
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

/**
 * 生成一条空产物行。
 *
 * @returns {object} 产物行。
 */
function newOutputRow() {
  return { key: "", type: "image", node: "", field: "" };
}

/**
 * 切换标签页。
 *
 * @param {string} tab 标签页名称。
 */
function switchTab(tab) {
  if (!tab || tab === state.uiState.activeTab) return;
  state.uiState.activeTab = tab;
  renderWorkspace();
}

/**
 * 把原始 JSON 文本应用成表单草稿。
 */
function applyJsonFromEditor() {
  const editor = document.getElementById("raw-json-editor");
  if (!editor) return;
  try {
    const parsed = JSON.parse(editor.value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("契约 JSON 必须是一个对象");
    }
    state.draft = contractToDraft(
      parsed,
      state.selectedStem,
      state.detail?.nodes ?? [],
    );
    state.uiState.activeTab = "form";
    renderWorkspace();
    showMessage("已把 JSON 应用到表单，保存后生效", "ok");
  } catch (error) {
    showError(error);
  }
}

/**
 * 同步基础信息。
 *
 * @param {string} field 草稿字段名。
 * @param {HTMLInputElement} target 触发事件的输入框。
 */
function updateBasics(field, target) {
  if (!state.draft) return;
  if (field === "enabled") {
    state.draft.enabled = target.checked;
    return;
  }
  if (field === "name" || field === "description") {
    state.draft[field] = target.value;
  }
}

/**
 * 同步一条参数行，结构变化时重绘工作区。
 *
 * @param {number} index 参数行下标。
 * @param {string} field 草稿字段名。
 * @param {HTMLInputElement|HTMLSelectElement} target 触发事件的控件。
 */
function updateInputRow(index, field, target) {
  const row = state.draft?.inputs?.[index];
  if (!row || !field) return;
  if (field === "visible" || field === "required") {
    row[field] = target.checked;
    if (field === "visible" && !target.checked) row.required = false;
    if (field === "visible" && target.checked) row.valueTemplate = "";
  } else if (field === "node") {
    row.node = target.value;
    row.field = firstFieldName(target.value);
  } else {
    row[field] = target.value;
  }
  if (STRUCTURAL_INPUT_FIELDS.has(field)) renderWorkspace();
}

/**
 * 同步一条产物行。
 *
 * @param {number} index 产物行下标。
 * @param {string} field 草稿字段名。
 * @param {HTMLInputElement|HTMLSelectElement} target 触发事件的控件。
 */
function updateOutputRow(index, field, target) {
  const row = state.draft?.outputs?.[index];
  if (!row || !field) return;
  row[field] = target.value;
}

/**
 * 把表单输入同步进草稿。
 *
 * @param {InputEvent|Event} event 输入或变更事件。
 */
function handleFormEvent(event) {
  const target = event.target;
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
    updateInputRow(
      Number(inputCard.dataset.inputIndex),
      target.dataset.inputField,
      target,
    );
    return;
  }
  const outputCard = target.closest("[data-output-index]");
  if (outputCard) {
    updateOutputRow(
      Number(outputCard.dataset.outputIndex),
      target.dataset.outputField,
      target,
    );
    return;
  }
  if (target.dataset.basicField) {
    updateBasics(target.dataset.basicField, target);
  }
}

/**
 * 处理工作区里的按钮点击。
 *
 * @param {MouseEvent} event 点击事件。
 */
function handleActionClick(event) {
  if (!(event.target instanceof Element)) return;
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  const inputIndex = button.closest("[data-input-index]")?.dataset.inputIndex;
  const outputIndex = button.closest("[data-output-index]")?.dataset.outputIndex;
  switch (action) {
    case "add-input":
      state.draft.inputs.push(newInputRow());
      renderWorkspace();
      return;
    case "remove-input":
      state.draft.inputs.splice(Number(inputIndex), 1);
      renderWorkspace();
      return;
    case "add-output":
      state.draft.outputs.push(newOutputRow());
      renderWorkspace();
      return;
    case "remove-output":
      state.draft.outputs.splice(Number(outputIndex), 1);
      renderWorkspace();
      return;
    case "apply-remote-field":
      applyRemoteField(Number(inputIndex));
      return;
    case "switch-tab":
      switchTab(button.dataset.tab);
      return;
    case "fetch-remote":
      runTask(fetchRemoteNodes);
      return;
    case "preview-local":
      runTask(() => refreshPreview(false), { showErrors: true });
      return;
    case "preview-remote":
      runTask(() => refreshPreview(true), { showErrors: true });
      return;
    case "refresh-inspect":
      runTask(() => refreshPreview(false), { showErrors: true });
      return;
    case "save-contract":
      runTask(saveContract, { showErrors: true });
      return;
    case "delete-contract":
      state.uiState.confirmDelete = "contract";
      renderWorkspace();
      return;
    case "delete-workflow":
      state.uiState.confirmDelete = "all";
      renderWorkspace();
      return;
    case "confirm-delete":
      runTask(deleteSelection);
      return;
    case "cancel-delete":
      state.uiState.confirmDelete = null;
      renderWorkspace();
      return;
    case "apply-json":
      applyJsonFromEditor();
      return;
  }
}

/**
 * 绑定顶栏、列表与工作区的事件。
 */
function bindEvents() {
  elements.uploadButton.addEventListener("click", () => {
    elements.uploadInput.click();
  });
  elements.uploadInput.addEventListener("change", () => {
    const file = elements.uploadInput.files?.[0];
    elements.uploadInput.value = "";
    if (!file) return;
    runTask(() => uploadWorkflow(file));
  });
  elements.checkLocalButton.addEventListener("click", () => {
    runTask(() => checkAll(false));
  });
  elements.checkRemoteButton.addEventListener("click", () => {
    runTask(() => checkAll(true));
  });
  elements.searchInput.addEventListener("input", () => {
    state.filterText = elements.searchInput.value;
    renderList();
  });
  elements.workflowList.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const item = event.target.closest(".workflow-item");
    const stem = item?.dataset.stem;
    if (!stem || stem === state.selectedStem) return;
    runTask(() => selectWorkflow(stem));
  });
  elements.workspace.addEventListener("click", handleActionClick);
  elements.workspace.addEventListener("input", handleFormEvent);
  elements.workspace.addEventListener("change", handleFormEvent);
}

/**
 * 页面入口：等 bridge 就绪后读取工作流列表。
 */
async function start() {
  if (!bridge) {
    showMessage("未检测到 AstrBot 视图 bridge，请从 AstrBot WebUI 的插件页打开本视图", "err");
    return;
  }
  await bridge.ready();
  bindEvents();
  try {
    await loadWorkflows();
  } catch (error) {
    showError(error);
  }
  renderWorkspace();
}

await start();
