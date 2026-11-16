/**
 * 工作流视图的 HTML 生成。
 *
 * 只把状态与数据拼成模板字符串：不读 DOM、不发请求。
 * 界面术语严格对齐 GLOSSARY.md，所有图标均使用内联 SVG。
 */

import {
  buildInspectPreview,
  draftToSafeObject,
  formatOriginalValue,
  formatRawValue,
} from "./state.js";

const INPUT_TYPES = [
  "string",
  "integer",
  "number",
  "boolean",
  "image",
  "video",
  "audio",
  "file",
];
const OUTPUT_TYPES = ["image", "video", "audio", "text", "file"];
const OUTPUT_FIELD_PRESETS = ["images", "gifs", "videos", "audio", "text"];
const OUTPUT_NODE_PATTERN = /Save|Preview|Output/;

const STATUS_FILTERS = [
  ["all", "全部"],
  ["enabled", "启用中"],
  ["disabled", "已禁用"],
  ["error", "有问题"],
  ["unconfigured", "无契约"],
];

const WORKSPACE_TABS = [
  ["form", "契约编辑"],
  ["inspect", "模型公开视图"],
  ["json", "JSON 源码"],
];

const BROWSER_FILTERS = [
  ["tunable", "可调输入"],
  ["output", "输出节点"],
  ["all", "全部节点"],
];

const HTML_ESCAPES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

const ICONS = {
  check: `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5L6.5 11.5L12.5 4.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  alert: `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2L14.5 13.5H1.5L8 2Z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M8 6.2v3.6M8 11.6v.4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  chevron: `<svg class="icon chevron-icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  plus: `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M3 8h10" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  trash: `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4.5h10M6 4.5V3h4v1.5M5 4.5l.6 8.5h4.8L11 4.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  up: `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 9.5L8 6l3.5 3.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  down: `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 6.5L8 10l3.5-3.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  nodes: `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><rect x="1.8" y="2.5" width="4.5" height="4.5" rx="1" fill="none" stroke="currentColor" stroke-width="1.3"/><rect x="9.7" y="9" width="4.5" height="4.5" rx="1" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M6.3 4.8H8v6.5h1.7" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  refresh: `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M13.5 8a5.5 5.5 0 1 1-1.4-3.66M13.5 2.5v2.75h-2.75" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  more: `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><circle cx="3.5" cy="8" r="1.2" fill="currentColor"/><circle cx="8" cy="8" r="1.2" fill="currentColor"/><circle cx="12.5" cy="8" r="1.2" fill="currentColor"/></svg>`,
  close: `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  copy: `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M10.5 5.5V3.7A1.2 1.2 0 0 0 9.3 2.5H3.7a1.2 1.2 0 0 0-1.2 1.2v5.6a1.2 1.2 0 0 0 1.2 1.2h1.8" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>`,
};

/**
 * 转义 HTML 文本或属性值。
 *
 * @param {unknown} text 任意待转义值。
 * @returns {string} 转义后的安全字符串。
 */
export function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (char) => HTML_ESCAPES[char]);
}

/**
 * 渲染左侧状态筛选标签条。
 *
 * @param {Array<object>} items 工作流列表。
 * @param {string} activeFilter 当前选中的状态筛选值。
 * @returns {string} 筛选按钮列表 HTML。
 */
export function renderStatusFilters(items, activeFilter) {
  const list = Array.isArray(items) ? items : [];
  const counts = {
    all: list.length,
    enabled: list.filter((i) => i.has_workflow && i.has_contract && i.ok && i.enabled).length,
    disabled: list.filter((i) => i.has_workflow && i.has_contract && i.ok && !i.enabled).length,
    error: list.filter((i) => !i.has_workflow || (i.has_contract && !i.ok)).length,
    unconfigured: list.filter((i) => i.has_workflow && !i.has_contract).length,
  };
  return STATUS_FILTERS.map(([key, label]) => {
    const active = key === activeFilter ? " is-active" : "";
    return (
      `<button type="button" class="filter-chip${active}" data-status-filter="${key}">` +
      `<span>${label}</span>` +
      `<span class="filter-count">${counts[key]}</span>` +
      `</button>`
    );
  }).join("");
}

/**
 * 渲染左侧工作流列表。
 *
 * @param {Array<object>} items 后端列出的工作流状态。
 * @param {string|null} selectedStem 当前选中的工作流名称。
 * @param {string} filterText 搜索词。
 * @param {string} statusFilter 状态筛选键。
 * @param {string|null} dirtyStem 当前有未保存更改的工作流名称。
 * @returns {string} 列表内部 HTML。
 */
export function renderWorkflowList(
  items,
  selectedStem,
  filterText,
  statusFilter = "all",
  dirtyStem = null,
) {
  const list = Array.isArray(items) ? items : [];
  const keyword = String(filterText ?? "").trim().toLowerCase();
  const matched = list.filter(
    (item) => matchesStatus(item, statusFilter) && matchesFilter(item, keyword),
  );
  if (matched.length === 0) {
    return `<div class="list-empty">没有符合条件的工作流</div>`;
  }
  return matched
    .map((item) => renderWorkflowItem(item, selectedStem, dirtyStem))
    .join("\n");
}

/**
 * 渲染右侧工作区完整内容。
 *
 * @param {object} detail 工作流详情。
 * @param {object} draft 当前草稿。
 * @param {object} uiState 视图状态。
 * @returns {string} `#workspace` 内部 HTML。
 */
export function renderWorkspace(detail, draft, uiState) {
  const state = uiState ?? {};
  const splitClass = state.nodeBrowserOpen ? " is-browser-open" : "";
  return [
    renderWorkspaceHeader(detail, draft, state),
    renderDeleteBanner(detail, state),
    renderValidationStrip(state),
    renderWorkspaceTabs(detail, state),
    `<div class="ws-body${splitClass}">`,
    `<div class="ws-main">`,
    renderFormPanel(detail, draft, state),
    renderInspectPanel(detail, draft, state),
    renderJsonPanel(draft, state),
    `</div>`,
    state.nodeBrowserOpen ? renderNodeBrowser(detail, draft, state) : "",
    `</div>`,
  ]
    .filter(Boolean)
    .join("\n");
}

/**
 * 单独渲染工作区头部与校验条所需信息（用于无刷新增量更新时复用）。
 *
 * @param {object} state 视图状态。
 * @returns {string} 校验条 HTML。
 */
export function renderValidationStripHtml(state) {
  return renderValidationStrip(state);
}

/**
 * 渲染弹窗内容：导入工作流或未保存更改拦截。
 *
 * @param {object|null} modal 弹窗状态。
 * @returns {string} 弹窗 HTML。
 */
export function renderModal(modal) {
  if (!modal) return "";
  if (modal.kind === "import") {
    const mode = modal.mode ?? "file";
    return [
      `<div class="modal-dialog" role="dialog" aria-modal="true" aria-labelledby="modal-title">`,
      `<div class="modal-head">`,
      `<h2 id="modal-title">导入工作流</h2>`,
      `<button type="button" class="icon-btn" data-modal-action="close" aria-label="关闭">${ICONS.close}</button>`,
      `</div>`,
      `<div class="modal-tabs">`,
      `<button type="button" class="tab-btn${mode === "file" ? " is-active" : ""}" data-modal-action="import-mode" data-mode="file">上传 .json 文件</button>`,
      `<button type="button" class="tab-btn${mode === "paste" ? " is-active" : ""}" data-modal-action="import-mode" data-mode="paste">粘贴 JSON 新建</button>`,
      `</div>`,
      mode === "file"
        ? [
            `<div class="modal-body">`,
            `<div class="drop-zone" data-modal-action="pick-file">`,
            `<p class="drop-zone-title">点击选择或把 ComfyUI API 格式 <code>.json</code> 拖到这里</p>`,
            `<p class="muted-text">支持与已有工作流同名覆盖（若启用契约不受破坏）</p>`,
            `<button type="button" class="btn btn-primary">选择本地 .json 文件</button>`,
            `</div>`,
            `</div>`,
          ].join("")
        : [
            `<div class="modal-body">`,
            `<div class="field-row">`,
            `<label for="paste-stem">工作流名称（不含 .json）</label>`,
            `<input id="paste-stem" type="text" autocomplete="off" placeholder="例如 flux2_portrait" value="${escapeHtml(modal.stem ?? "")}">`,
            `</div>`,
            `<div class="field-row">`,
            `<label for="paste-json">ComfyUI API 格式工作流 JSON</label>`,
            `<textarea id="paste-json" class="json-editor json-editor-sm" spellcheck="false" placeholder='{"1": {"class_type": "...", "inputs": {...}}}'>${escapeHtml(modal.jsonText ?? "")}</textarea>`,
            `</div>`,
            modal.error ? `<p class="inline-error">${escapeHtml(modal.error)}</p>` : "",
            `<div class="modal-foot">`,
            `<button type="button" class="btn" data-modal-action="close">取消</button>`,
            `<button type="button" class="btn btn-primary" data-modal-action="submit-paste">导入工作流</button>`,
            `</div>`,
            `</div>`,
          ].join(""),
      `</div>`,
    ].join("");
  }

  if (modal.kind === "unsaved") {
    return [
      `<div class="modal-dialog modal-dialog-sm" role="dialog" aria-modal="true" aria-labelledby="modal-title">`,
      `<div class="modal-head">`,
      `<h2 id="modal-title">有未保存的更改</h2>`,
      `<button type="button" class="icon-btn" data-modal-action="close" aria-label="关闭">${ICONS.close}</button>`,
      `</div>`,
      `<div class="modal-body">`,
      `<p>工作流 <code>${escapeHtml(modal.stem)}</code> 的工具契约草稿尚未保存，切换后这些编辑将会丢失。</p>`,
      `<div class="modal-foot">`,
      `<button type="button" class="btn" data-modal-action="close">留在当前</button>`,
      `<button type="button" class="btn btn-danger-ghost" data-modal-action="discard-and-continue">放弃更改并继续</button>`,
      `<button type="button" class="btn btn-primary" data-modal-action="save-and-continue">保存并继续</button>`,
      `</div>`,
      `</div>`,
      `</div>`,
    ].join("");
  }
  return "";
}

function matchesStatus(item, statusFilter) {
  switch (statusFilter) {
    case "enabled":
      return item.has_workflow && item.has_contract && item.ok && item.enabled;
    case "disabled":
      return item.has_workflow && item.has_contract && item.ok && !item.enabled;
    case "error":
      return !item.has_workflow || (item.has_contract && !item.ok);
    case "unconfigured":
      return item.has_workflow && !item.has_contract;
    default:
      return true;
  }
}

function matchesFilter(item, keyword) {
  if (!keyword) return true;
  return [item.stem, item.tool_name, item.description]
    .filter((part) => typeof part === "string")
    .some((part) => part.toLowerCase().includes(keyword));
}

function renderWorkflowItem(item, selectedStem, dirtyStem) {
  const classes = ["workflow-item"];
  if (item.stem === selectedStem) classes.push("is-active");
  const status = workflowStatus(item);
  const errorCount = Array.isArray(item.errors) ? item.errors.length : 0;
  const dirtyDot =
    item.stem === dirtyStem
      ? `<span class="dirty-dot" title="有未保存更改"></span>`
      : "";
  const toolLabel = item.tool_name || "未配置工具契约";
  const countText = errorCount
    ? `${errorCount} 个问题`
    : `${item.input_count ?? 0}入 · ${item.output_count ?? 0}出`;
  const countClass = errorCount ? "workflow-item-count is-err" : "workflow-item-count";

  return [
    `<button type="button" class="${classes.join(" ")}" data-stem="${escapeHtml(item.stem)}">`,
    `<span class="workflow-item-top">`,
    `<span class="workflow-item-name" title="${escapeHtml(item.stem)}">${dirtyDot}${escapeHtml(item.stem)}</span>`,
    `<span class="badge ${status.className}">${status.label}</span>`,
    `</span>`,
    `<span class="workflow-item-bottom">`,
    `<span class="workflow-item-tool" title="${escapeHtml(toolLabel)}">${escapeHtml(toolLabel)}</span>`,
    `<span class="${countClass}">${escapeHtml(countText)}</span>`,
    `</span>`,
    `</button>`,
  ].join("");
}

function workflowStatus(item) {
  if (!item.has_workflow) return { className: "badge-err", label: "缺工作流" };
  if (!item.has_contract) return { className: "badge-warn", label: "无契约" };
  if (!item.ok) return { className: "badge-err", label: "异常" };
  if (!item.enabled) return { className: "badge-muted", label: "已禁用" };
  return { className: "badge-ok", label: "启用正常" };
}

function renderWorkspaceHeader(detail, draft, state) {
  const remotePill = renderRemotePill(detail, state);
  const dirtyBadge = state.isDirty
    ? `<span id="dirty-badge" class="badge badge-warn">未保存更改</span>`
    : `<span id="dirty-badge" class="badge badge-warn" hidden>未保存更改</span>`;
  const fileBadges = [
    detail.has_workflow
      ? `<span class="meta-pill">${escapeHtml(detail.stem)}.json</span>`
      : `<span class="meta-pill is-missing">缺少 ${escapeHtml(detail.stem)}.json</span>`,
    detail.has_contract
      ? `<span class="meta-pill">${escapeHtml(detail.stem)}.tool.json</span>`
      : `<span class="meta-pill is-muted">尚无 .tool.json</span>`,
  ].join("");

  const menuOpen = state.moreMenuOpen ? "" : " hidden";

  return [
    `<div class="ws-header">`,
    `<div class="ws-header-left">`,
    `<div class="ws-title-row">`,
    `<h2 title="${escapeHtml(detail.stem)}">${escapeHtml(detail.stem)}</h2>`,
    dirtyBadge,
    remotePill,
    `</div>`,
    `<div class="ws-meta-row">${fileBadges}</div>`,
    `</div>`,
    `<div class="ws-header-actions">`,
    `<label class="switch-label header-switch" title="启用后该工作流工具对模型可用">`,
    `<input type="checkbox" class="switch-input" data-basic-field="enabled"${draft.enabled ? " checked" : ""}>`,
    `<span class="switch-track"></span>`,
    `<span class="switch-text">启用工具</span>`,
    `</label>`,
    `<button type="button" class="btn btn-sm" data-action="check-remote" title="重新拉取 ComfyUI 节点定义并核对">${ICONS.refresh}<span>连接 ComfyUI 核对</span></button>`,
    `<button type="button" id="btn-revert" class="btn btn-sm" data-action="revert-draft"${state.isDirty ? "" : " disabled"}>还原</button>`,
    `<button type="button" class="btn btn-sm btn-primary" data-action="save-contract" title="保存工具契约 (Ctrl+S / Cmd+S)">保存契约</button>`,
    `<div class="dropdown-wrap">`,
    `<button type="button" class="btn btn-sm icon-only-btn" data-action="toggle-more-menu" aria-label="更多操作" title="更多操作">${ICONS.more}</button>`,
    `<div class="dropdown-menu"${menuOpen}>`,
    `<button type="button" class="dropdown-item" data-action="copy-contract">${ICONS.copy}<span>复制契约 JSON</span></button>`,
    `<button type="button" class="dropdown-item" data-action="export-contract"><span>导出 .tool.json</span></button>`,
    `<div class="dropdown-divider"></div>`,
    `<button type="button" class="dropdown-item is-danger" data-action="delete-contract"${detail.has_contract ? "" : " disabled"}>${ICONS.trash}<span>仅删除工具契约</span></button>`,
    `<button type="button" class="dropdown-item is-danger" data-action="delete-workflow"${detail.has_workflow ? "" : " disabled"}>${ICONS.trash}<span>删除工作流文件</span></button>`,
    `</div>`,
    `</div>`,
    `</div>`,
    `</div>`,
  ].join("");
}

function renderRemotePill(detail, state) {
  if (!detail.has_workflow) return "";
  if (state.remoteStatus === "loading") {
    return `<span class="badge badge-muted">正在获取节点定义...</span>`;
  }
  const nodes = Array.isArray(detail.nodes) ? detail.nodes : [];
  if (nodes.length === 0) return "";
  const withRemote = nodes.filter((n) => n.remote);
  if (withRemote.length === 0) {
    return `<span class="badge badge-muted">尚未核对远端节点</span>`;
  }
  const failed = withRemote.filter((n) => n.remote.error);
  if (failed.length === withRemote.length) {
    return `<span class="badge badge-warn" title="${escapeHtml(failed[0].remote.error)}">ComfyUI 离线（仅本地校验）</span>`;
  }
  if (failed.length > 0) {
    return `<span class="badge badge-warn">${failed.length} 个节点类型未核对</span>`;
  }
  return `<span class="badge badge-ok">远端节点已核对</span>`;
}

function renderDeleteBanner(detail, state) {
  if (state.confirmDelete === "contract" && detail.has_contract) {
    return renderInlineConfirm(
      "确认删除工具契约（.tool.json）？工作流文件会保留，但模型将不再看到该工具。",
    );
  }
  if (state.confirmDelete === "all" && detail.has_workflow) {
    const enabledHint = detail.contract?.enabled
      ? "当前已保存的工具契约仍处于启用状态，后端要求先停用或仅删除契约后才能删除工作流文件。"
      : "确认删除工作流文件与配套工具契约？此操作不可撤销。";
    return renderInlineConfirm(enabledHint, detail.contract?.enabled);
  }
  return "";
}

function renderInlineConfirm(hint, blocked = false) {
  return [
    `<div class="inline-confirm">`,
    `<span class="inline-confirm-text">${ICONS.alert}<span>${escapeHtml(hint)}</span></span>`,
    `<span class="inline-confirm-actions">`,
    blocked
      ? `<button type="button" class="btn btn-sm" data-action="disable-and-delete">先停用再删除</button>`
      : `<button type="button" class="btn btn-sm btn-danger" data-action="confirm-delete">确认删除</button>`,
    `<button type="button" class="btn btn-sm" data-action="cancel-delete">取消</button>`,
    `</span>`,
    `</div>`,
  ].join("");
}

function mergeValidationItems(state) {
  const items = [];
  const seen = new Set();
  for (const issue of state.localIssues ?? []) {
    if (!seen.has(issue.message)) {
      seen.add(issue.message);
      items.push({ message: issue.message, target: issue.target, source: "本地" });
    }
  }
  for (const msg of state.errors ?? []) {
    if (!seen.has(msg)) {
      seen.add(msg);
      items.push({ message: msg, target: inferTargetFromMessage(msg), source: "校验" });
    }
  }
  return items;
}

function inferTargetFromMessage(message) {
  const paramMatch = /参数\s+([a-z0-9_]+)/.exec(message);
  if (paramMatch) return { kind: "inputKey", key: paramMatch[1] };
  const outputMatch = /输出\s+([a-z0-9_]+)/.exec(message);
  if (outputMatch) return { kind: "outputKey", key: outputMatch[1] };
  return null;
}

function renderValidationStrip(state) {
  const items = mergeValidationItems(state);
  if (items.length === 0) {
    return (
      `<div id="validation-strip" class="validation-strip is-ok">` +
      `<span class="validation-summary">${ICONS.check}<span>契约校验通过</span></span>` +
      `</div>`
    );
  }
  const expanded = state.validationExpanded !== false;
  const listHtml = expanded
    ? `<ul class="validation-list">${items
        .map((item, idx) => {
          const clickable = item.target ? ` data-action="focus-issue" data-issue-index="${idx}"` : "";
          const cls = item.target ? "validation-item is-clickable" : "validation-item";
          return (
            `<li><button type="button" class="${cls}"${clickable}>` +
            `<span class="validation-tag">${escapeHtml(item.source)}</span>` +
            `<span>${escapeHtml(item.message)}</span>` +
            `</button></li>`
          );
        })
        .join("")}</ul>`
    : "";

  return [
    `<div id="validation-strip" class="validation-strip is-err">`,
    `<div class="validation-head">`,
    `<span class="validation-summary">${ICONS.alert}<span>发现 ${items.length} 个问题${items.some((i) => i.target) ? "（点击可定位）" : ""}</span></span>`,
    `<button type="button" class="link-btn" data-action="toggle-validation">${expanded ? "收起" : "展开详情"}</button>`,
    `</div>`,
    listHtml,
    `</div>`,
  ].join("");
}

function renderWorkspaceTabs(detail, state) {
  const active = state.activeTab ?? "form";
  const tabHtml = WORKSPACE_TABS.map(([id, label]) => {
    const cls = id === active ? "tab-btn is-active" : "tab-btn";
    return `<button type="button" class="${cls}" data-action="switch-tab" data-tab="${id}">${label}</button>`;
  }).join("");
  const nodeCount = Array.isArray(detail.nodes) ? detail.nodes.length : 0;
  const browserActive = state.nodeBrowserOpen ? " is-active" : "";
  return [
    `<div class="ws-tabs-bar">`,
    `<div class="tabs">${tabHtml}</div>`,
    `<button type="button" class="btn btn-sm browser-toggle-btn${browserActive}" data-action="toggle-node-browser">`,
    `${ICONS.nodes}<span>工作流节点浏览器</span><span class="pill-count">${nodeCount}</span>`,
    `</button>`,
    `</div>`,
  ].join("");
}

function renderFormPanel(detail, draft, state) {
  return [
    `<div class="tab-panel" data-panel="form"${panelHidden(state, "form")}>`,
    renderBasicsSection(draft),
    renderInputsSection(detail, draft, state),
    renderOutputsSection(detail, draft),
    `</div>`,
  ].join("\n");
}

function renderBasicsSection(draft) {
  return [
    `<section class="section">`,
    `<div class="section-head"><h3>基础信息</h3></div>`,
    `<div class="section-body">`,
    `<div class="grid-basics">`,
    `<div class="field-row">`,
    `<label for="contract-name">工具名称</label>`,
    `<input id="contract-name" type="text" data-basic-field="name" autocomplete="off" placeholder="例如 comfy_flux2_i2i" value="${escapeHtml(draft.name)}">`,
    `</div>`,
    `<div class="field-row">`,
    `<label for="contract-description">工具描述（供模型判断是否调用此工作流工具）</label>`,
    `<input id="contract-description" type="text" data-basic-field="description" autocomplete="off" placeholder="说明适用场景、输入要求与产出效果" value="${escapeHtml(draft.description)}">`,
    `</div>`,
    `</div>`,
    `</div>`,
    `</section>`,
  ].join("\n");
}

function renderInputsSection(detail, draft, state) {
  const expandedSet = state.expandedInputs ?? new Set();
  const issueSet = new Set(
    (state.localIssues ?? [])
      .filter((i) => i.target?.kind === "input")
      .map((i) => i.target.index),
  );
  const rowsHtml = draft.inputs.length
    ? draft.inputs
        .map((row, index) =>
          renderInputRow(
            detail,
            row,
            index,
            draft.inputs.length,
            expandedSet.has(index),
            issueSet.has(index),
          ),
        )
        .join("\n")
    : `<div class="empty-inline">还没有参数映射。可从右侧「工作流节点浏览器」一键挑选字段，或点击下方手动添加。</div>`;

  return [
    `<section class="section">`,
    `<div class="section-head">`,
    `<div class="section-title-group">`,
    `<h3>参数映射</h3>`,
    `<span class="pill-count">${draft.inputs.length}</span>`,
    `</div>`,
    `<div class="section-actions">`,
    draft.inputs.length > 0
      ? `<button type="button" class="link-btn" data-action="expand-all-inputs">全部展开</button>` +
        `<button type="button" class="link-btn" data-action="collapse-all-inputs">全部收起</button>`
      : "",
    `<button type="button" class="btn btn-sm" data-action="open-node-browser-input">${ICONS.nodes}<span>从节点添加</span></button>`,
    `<button type="button" class="btn btn-sm" data-action="add-input">${ICONS.plus}<span>手动添加</span></button>`,
    `</div>`,
    `</div>`,
    `<div class="mapping-list">${rowsHtml}</div>`,
    `</section>`,
  ].join("\n");
}

function renderInputRow(detail, row, index, total, expanded, hasIssue) {
  const id = (suffix) => `input-${index}-${suffix}`;
  const node = findNode(detail.nodes, row.node);
  const fieldObj = node?.fields?.find((f) => f.name === row.field) ?? null;
  const remoteField = node?.remote?.fields?.[row.field] ?? null;
  const origRaw = fieldObj ? formatRawValue(fieldObj) : "";
  const classes = ["param-row"];
  if (expanded) classes.push("is-expanded");
  if (hasIssue) classes.push("has-error");

  const targetSummary =
    row.node && row.field ? `${row.node}.${row.field}` : "未绑定节点字段";
  const visibilityTag = row.visible
    ? `<span class="tag tag-vis">可见参数</span>`
    : `<span class="tag tag-hidden">隐藏参数</span>`;
  const reqTag =
    row.visible && row.required ? `<span class="tag tag-req">必填</span>` : "";
  const errTag = hasIssue ? `<span class="tag tag-err">待完善</span>` : "";

  const summary = [
    `<div class="param-summary" data-action="toggle-input">`,
    `<span class="param-chevron">${ICONS.chevron}</span>`,
    `<span class="param-key${row.key ? "" : " is-empty"}">${escapeHtml(row.key || `未命名参数 #${index + 1}`)}</span>`,
    `<span class="tag tag-type">${escapeHtml(row.type)}</span>`,
    `<span class="param-target" title="${escapeHtml(targetSummary)}">${escapeHtml(targetSummary)}</span>`,
    origRaw
      ? `<span class="param-orig" title="工作流原值: ${escapeHtml(origRaw)}">${escapeHtml(origRaw)}</span>`
      : "",
    `<span class="param-tags">${errTag}${visibilityTag}${reqTag}</span>`,
    `<span class="param-row-actions">`,
    `<button type="button" class="icon-btn" data-action="move-input-up" title="上移"${index === 0 ? " disabled" : ""}>${ICONS.up}</button>`,
    `<button type="button" class="icon-btn" data-action="move-input-down" title="下移"${index === total - 1 ? " disabled" : ""}>${ICONS.down}</button>`,
    `<button type="button" class="icon-btn is-danger" data-action="remove-input" title="删除参数">${ICONS.trash}</button>`,
    `</span>`,
    `</div>`,
  ].join("");

  if (!expanded) {
    return `<div class="${classes.join(" ")}" data-input-index="${index}">${summary}</div>`;
  }

  const contextHtml = renderFieldContextBar(fieldObj, remoteField);

  return [
    `<div class="${classes.join(" ")}" data-input-index="${index}">`,
    summary,
    `<div class="param-detail">`,
    `<div class="grid-3">`,
    renderTextRow(id("key"), "参数名", 'data-input-field="key"', row.key, "如 prompt / seed"),
    renderSelectRow(id("type"), "类型", 'data-input-field="type"', renderOptions(INPUT_TYPES, row.type)),
    renderSelectRow(id("node"), "映射节点", 'data-input-field="node"', renderNodeOptions(detail.nodes, row.node, false)),
    `</div>`,
    `<div class="grid-2">`,
    renderSelectRow(id("field"), "映射字段", 'data-input-field="field"', renderFieldOptions(node?.fields, row.field)),
    renderTextRow(
      id("description"),
      "参数描述（模型可见）",
      'data-input-field="description"',
      row.description,
      "说明该参数的含义与格式；可选时可交代省略后使用工作流原值",
    ),
    `</div>`,
    contextHtml,
    `<div class="switch-bar">`,
    `<label class="switch-label">`,
    `<input id="${id("visible")}" type="checkbox" class="switch-input" data-input-field="visible"${row.visible ? " checked" : ""}>`,
    `<span class="switch-track"></span>`,
    `<span class="switch-text">可见参数（出现在模型工具契约中）</span>`,
    `</label>`,
    `<label class="switch-label${row.visible ? "" : " is-disabled"}">`,
    `<input id="${id("required")}" type="checkbox" class="switch-input" data-input-field="required"${row.required ? " checked" : ""}${row.visible ? "" : " disabled"}>`,
    `<span class="switch-track"></span>`,
    `<span class="switch-text">必填</span>`,
    `</label>`,
    `</div>`,
    renderInputConstraints(row, id),
    `</div>`,
    `</div>`,
  ]
    .filter(Boolean)
    .join("\n");
}

function renderFieldContextBar(fieldObj, remoteField) {
  const parts = [];
  if (fieldObj) {
    parts.push(`<span class="ctx-item"><strong>工作流原值</strong>：<code>${escapeHtml(formatRawValue(fieldObj))}</code></span>`);
  }
  if (remoteField) {
    const hints = [];
    if (remoteField.suggested_type) hints.push(remoteField.suggested_type);
    if (remoteField.minimum !== undefined && remoteField.minimum !== null) {
      hints.push(`min ${remoteField.minimum}`);
    }
    if (remoteField.maximum !== undefined && remoteField.maximum !== null) {
      hints.push(`max ${remoteField.maximum}`);
    }
    if (Array.isArray(remoteField.enum) && remoteField.enum.length > 0) {
      hints.push(`${remoteField.enum.length} 个枚举值`);
    }
    parts.push(
      `<span class="ctx-item"><strong>ComfyUI 节点定义</strong>：<code>${escapeHtml(hints.join(" · ") || "无额外约束")}</code></span>`,
    );
  }
  if (parts.length === 0) return "";
  const syncBtn = remoteField
    ? `<button type="button" class="btn btn-sm" data-action="apply-remote-field">同步远端约束</button>`
    : "";
  return `<div class="field-context"><div class="field-context-info">${parts.join("")}</div>${syncBtn}</div>`;
}

function renderInputConstraints(row, id) {
  if (row.type === "string") {
    const templateRow = row.visible
      ? ""
      : renderTextRow(
          id("valueTemplate"),
          "隐藏参数值模板（可选，支持 %date:yyyy-MM-dd%）",
          'data-input-field="valueTemplate"',
          row.valueTemplate,
          "例如 output/%date:yyyy-MM-dd%/img",
        );
    return [
      templateRow,
      `<div class="grid-3">`,
      renderLimitField(id("minLength"), "最小长度", "minLength", row.minLength),
      renderLimitField(id("maxLength"), "最大长度", "maxLength", row.maxLength),
      renderEnumField(id("enumText"), row.enumText),
      `</div>`,
    ]
      .filter(Boolean)
      .join("\n");
  }
  if (row.type === "integer" || row.type === "number") {
    return [
      `<div class="grid-3">`,
      renderLimitField(id("minimum"), "最小值", "minimum", row.minimum),
      renderLimitField(id("maximum"), "最大值", "maximum", row.maximum),
      renderEnumField(id("enumText"), row.enumText),
      `</div>`,
    ].join("\n");
  }
  return "";
}

function renderOutputsSection(detail, draft) {
  const rowsHtml = draft.outputs.length
    ? draft.outputs
        .map((row, index) => renderOutputRow(detail, row, index))
        .join("\n")
    : `<div class="empty-inline">至少配置一条输出映射，告诉插件执行后从哪个节点交付正式产物。</div>`;

  return [
    `<section class="section">`,
    `<div class="section-head">`,
    `<div class="section-title-group">`,
    `<h3>输出映射</h3>`,
    `<span class="pill-count">${draft.outputs.length}</span>`,
    `</div>`,
    `<div class="section-actions">`,
    `<button type="button" class="btn btn-sm" data-action="open-node-browser-output">${ICONS.nodes}<span>从输出节点添加</span></button>`,
    `<button type="button" class="btn btn-sm" data-action="add-output">${ICONS.plus}<span>手动添加</span></button>`,
    `</div>`,
    `</div>`,
    `<div class="mapping-list">${rowsHtml}</div>`,
    `</section>`,
  ].join("\n");
}

function renderOutputRow(detail, row, index) {
  const id = (suffix) => `output-${index}-${suffix}`;
  const presetChips = OUTPUT_FIELD_PRESETS.map((name) => {
    const active = row.field === name ? " is-active" : "";
    return `<button type="button" class="preset-chip${active}" data-action="set-output-field" data-value="${name}">${name}</button>`;
  }).join("");

  return [
    `<div class="output-row" data-output-index="${index}">`,
    `<div class="output-row-head">`,
    `<span class="output-row-title">输出 #${index + 1}</span>`,
    `<button type="button" class="icon-btn is-danger" data-action="remove-output" title="删除输出映射">${ICONS.trash}</button>`,
    `</div>`,
    `<div class="grid-4">`,
    renderTextRow(id("key"), "产物名", 'data-output-field="key"', row.key, "如 image / video"),
    renderSelectRow(id("type"), "产物类型", 'data-output-field="type"', renderOptions(OUTPUT_TYPES, row.type)),
    renderSelectRow(id("node"), "输出节点", 'data-output-field="node"', renderNodeOptions(detail.nodes, row.node, true)),
    `<div class="field-row">`,
    `<label for="${id("field")}">输出字段</label>`,
    `<input id="${id("field")}" type="text" data-output-field="field" autocomplete="off" placeholder="如 images" value="${escapeHtml(row.field)}">`,
    `</div>`,
    `</div>`,
    `<div class="preset-row"><span class="muted-text">常用字段：</span>${presetChips}</div>`,
    `</div>`,
  ].join("\n");
}

function renderNodeBrowser(detail, draft, state) {
  const nodes = Array.isArray(detail.nodes) ? detail.nodes : [];
  const query = String(state.browserSearch ?? "").trim().toLowerCase();
  const filter = state.browserFilter ?? "tunable";
  const showLinks = Boolean(state.browserShowLinks);

  const mappedInputs = new Map();
  for (const [idx, row] of (draft.inputs ?? []).entries()) {
    if (row.node && row.field) {
      mappedInputs.set(`${row.node}::${row.field}`, { key: row.key || `#${idx + 1}`, index: idx });
    }
  }
  const mappedOutputs = new Map();
  for (const [idx, row] of (draft.outputs ?? []).entries()) {
    if (row.node) {
      mappedOutputs.set(String(row.node), row.key || `#${idx + 1}`);
    }
  }

  const filterTabs = BROWSER_FILTERS.map(([id, label]) => {
    const active = id === filter ? " is-active" : "";
    return `<button type="button" class="filter-chip${active}" data-action="browser-filter" data-filter="${id}">${label}</button>`;
  }).join("");

  const visibleNodes = nodes.filter((node) => {
    const isOut = isOutputNode(node);
    const tunableFields = (node.fields ?? []).filter((f) => !f.is_link);
    if (filter === "output" && !isOut) return false;
    if (filter === "tunable" && tunableFields.length === 0 && !showLinks) return false;
    if (!query) return true;
    const textMatch = [node.id, node.title, node.class_type]
      .filter(Boolean)
      .some((part) => String(part).toLowerCase().includes(query));
    if (textMatch) return true;
    return (node.fields ?? []).some((f) => {
      if (!showLinks && f.is_link) return false;
      return f.name.toLowerCase().includes(query);
    });
  });

  const nodesHtml = visibleNodes.length
    ? visibleNodes
        .map((node) => renderBrowserNodeCard(node, showLinks, mappedInputs, mappedOutputs, query, filter))
        .join("\n")
    : `<div class="list-empty">没有匹配的节点或字段</div>`;

  return [
    `<aside class="node-browser" aria-label="工作流节点浏览器">`,
    `<div class="browser-head">`,
    `<div class="browser-title"><h4>工作流节点浏览器</h4><span class="muted-text">点击字段直接添加到契约</span></div>`,
    `<button type="button" class="icon-btn" data-action="close-node-browser" aria-label="关闭节点浏览器">${ICONS.close}</button>`,
    `</div>`,
    `<div class="browser-controls">`,
    `<input type="search" class="search-input" data-browser-search="true" placeholder="搜索节点 ID、标题、类型或字段..." autocomplete="off" value="${escapeHtml(state.browserSearch ?? "")}">`,
    `<div class="filter-row">${filterTabs}</div>`,
    `<label class="switch-label switch-compact">`,
    `<input type="checkbox" class="switch-input" data-browser-links="true"${showLinks ? " checked" : ""}>`,
    `<span class="switch-track"></span>`,
    `<span class="switch-text">显示节点连线字段</span>`,
    `</label>`,
    `</div>`,
    `<div class="browser-list">${nodesHtml}</div>`,
    `</aside>`,
  ].join("\n");
}

function renderBrowserNodeCard(node, showLinks, mappedInputs, mappedOutputs, query, filter) {
  const isOut = isOutputNode(node);
  const outBadge = isOut ? `<span class="badge badge-ok">输出节点</span>` : "";
  const mappedOutKey = mappedOutputs.get(String(node.id));
  let outAction = "";
  if (mappedOutKey) {
    outAction = `<span class="tag tag-mapped">已设输出: ${escapeHtml(mappedOutKey)}</span>`;
  } else if (isOut || filter === "output" || filter === "all") {
    outAction = `<button type="button" class="btn btn-xs${isOut ? " btn-primary" : ""}" data-action="browser-add-output" data-node-id="${escapeHtml(node.id)}">${ICONS.plus}<span>设为输出映射</span></button>`;
  }

  const fields = (node.fields ?? []).filter((f) => {
    if (!showLinks && f.is_link) return false;
    if (!query) return true;
    const nodeMatched = [node.id, node.title, node.class_type]
      .filter(Boolean)
      .some((part) => String(part).toLowerCase().includes(query));
    return nodeMatched || f.name.toLowerCase().includes(query);
  });

  const remoteErr = node.remote?.error
    ? `<div class="browser-remote-err" title="${escapeHtml(node.remote.error)}">远端节点定义不可用</div>`
    : "";

  const fieldsHtml = fields.length
    ? fields
        .map((field) => {
          const mapInfo = mappedInputs.get(`${node.id}::${field.name}`);
          const remoteField = node.remote?.fields?.[field.name] ?? null;
          const remoteHint = summarizeRemoteField(remoteField);
          const valueText = field.is_link
            ? `连线 ${formatRawValue(field)}`
            : formatRawValue(field);
          let actionHtml = "";
          if (field.is_link) {
            actionHtml = `<span class="tag tag-muted">连线</span>`;
          } else if (mapInfo) {
            actionHtml = `<button type="button" class="tag tag-mapped is-clickable" data-action="jump-input" data-input-index="${mapInfo.index}">已映射: ${escapeHtml(mapInfo.key)}</button>`;
          } else {
            actionHtml = `<button type="button" class="btn btn-xs" data-action="browser-add-input" data-node-id="${escapeHtml(node.id)}" data-field-name="${escapeHtml(field.name)}">${ICONS.plus}<span>参数</span></button>`;
          }

          return [
            `<div class="browser-field${field.is_link ? " is-link" : ""}">`,
            `<div class="browser-field-info">`,
            `<div class="browser-field-top"><code>${escapeHtml(field.name)}</code>${remoteHint ? `<span class="remote-hint" title="${escapeHtml(remoteHint)}">${escapeHtml(remoteHint)}</span>` : ""}</div>`,
            `<div class="browser-field-val" title="${escapeHtml(valueText)}">${escapeHtml(valueText)}</div>`,
            `</div>`,
            `<div class="browser-field-action">${actionHtml}</div>`,
            `</div>`,
          ].join("");
        })
        .join("")
    : `<div class="browser-empty-fields">该节点无可调常量字段</div>`;

  return [
    `<div class="browser-node">`,
    `<div class="browser-node-head">`,
    `<div class="browser-node-title">`,
    `<span class="node-id">#${escapeHtml(node.id)}</span>`,
    `<span class="node-name" title="${escapeHtml(node.title)}">${escapeHtml(node.title)}</span>`,
    outBadge,
    `</div>`,
    `<div class="browser-node-sub"><code>${escapeHtml(node.class_type)}</code>${outAction}</div>`,
    `</div>`,
    remoteErr,
    `<div class="browser-fields">${fieldsHtml}</div>`,
    `</div>`,
  ].join("");
}

function summarizeRemoteField(remote) {
  if (!remote) return "";
  const parts = [];
  if (remote.suggested_type) parts.push(remote.suggested_type);
  if (Array.isArray(remote.enum) && remote.enum.length > 0) {
    parts.push(`${remote.enum.length}项`);
  } else if (remote.minimum !== undefined && remote.minimum !== null && remote.maximum !== undefined && remote.maximum !== null) {
    parts.push(`${remote.minimum}..${remote.maximum}`);
  }
  return parts.join(" ");
}

function renderInspectPanel(detail, draft, state) {
  const localPreview = buildInspectPreview(draft);
  const schemaObj = state.inspectPreview ?? {
    name: localPreview.name,
    description: localPreview.description,
    parameters: localPreview.parameters,
  };
  const hiddenBanner = localPreview.hiddenKeys.length
    ? `<div class="notice-warn">${ICONS.alert}<span>隐藏参数（不会出现在模型工具契约中，也无法从模型传参）：<code>${escapeHtml(localPreview.hiddenKeys.join("、"))}</code></span></div>`
    : "";

  const paramRows = localPreview.visibleRows.length
    ? `<table class="preview-table"><thead><tr><th>参数名</th><th>类型</th><th>必填</th><th>约束</th><th>模型看到的描述</th></tr></thead><tbody>${localPreview.visibleRows
        .map(
          (r) =>
            `<tr><td><code>${escapeHtml(r.key)}</code></td>` +
            `<td><span class="tag tag-type">${escapeHtml(r.type)}</span></td>` +
            `<td>${r.required ? '<span class="tag tag-req">是</span>' : '<span class="muted-text">否</span>'}</td>` +
            `<td class="muted-text">${escapeHtml(r.constraints || "—")}</td>` +
            `<td>${escapeHtml(r.description || "（未填写描述）")}</td></tr>`,
        )
        .join("")}</tbody></table>`
    : `<p class="muted-text">当前没有可见参数，模型可直接无参调用该工作流工具。</p>`;

  const outputChips = localPreview.outputs
    .map(
      (o) =>
        `<span class="meta-pill">${escapeHtml(o.key || "未命名")} (${escapeHtml(o.type)}) ← ${escapeHtml(o.node || "?")}.${escapeHtml(o.field || "?")}</span>`,
    )
    .join("");

  const showRaw = Boolean(state.showRawSchema);
  const statusHint = state.inspectPreview
    ? `<span class="badge badge-ok">与后端模型视图一致</span>`
    : `<span class="badge badge-muted">实时草稿预览</span>`;

  return [
    `<div class="tab-panel" data-panel="inspect"${panelHidden(state, "inspect")}>`,
    hiddenBanner,
    `<section class="section">`,
    `<div class="section-head">`,
    `<div class="section-title-group"><h3>模型工具签名</h3>${statusHint}</div>`,
    `<button type="button" class="btn btn-sm" data-action="copy-schema">${ICONS.copy}<span>复制 Schema</span></button>`,
    `</div>`,
    `<div class="section-body">`,
    `<div class="inspect-summary">`,
    `<div class="inspect-name"><code>${escapeHtml(localPreview.name || "未命名工具")}</code>` +
      `<span class="badge ${localPreview.enabled ? "badge-ok" : "badge-muted"}">${localPreview.enabled ? "已启用" : "已禁用"}</span></div>`,
    `<p class="inspect-desc">${escapeHtml(localPreview.description || "（暂未填写工具描述）")}</p>`,
    `</div>`,
    `<h4 class="subhead">可见参数</h4>`,
    paramRows,
    `<h4 class="subhead">交付产物（输出映射）</h4>`,
    `<div class="ws-meta-row">${outputChips || '<span class="muted-text">暂无输出映射</span>'}</div>`,
    `</div>`,
    `</section>`,
    `<section class="section">`,
    `<div class="section-head">`,
    `<h3>原始 JSON Schema</h3>`,
    `<button type="button" class="link-btn" data-action="toggle-raw-schema">${showRaw ? "收起" : "展开查看"}</button>`,
    `</div>`,
    showRaw
      ? `<div class="section-body"><pre class="code-box">${escapeHtml(JSON.stringify(schemaObj, null, 2))}</pre></div>`
      : "",
    `</section>`,
    `</div>`,
  ]
    .filter(Boolean)
    .join("\n");
}

function renderJsonPanel(draft, state) {
  const safe = draftToSafeObject(draft);
  const text = JSON.stringify(safe.contract, null, 2);
  const warningHtml = safe.warnings.length
    ? `<div class="notice-warn">${ICONS.alert}<span>${escapeHtml(safe.warnings.join("；"))}</span></div>`
    : "";

  return [
    `<div class="tab-panel" data-panel="json"${panelHidden(state, "json")}>`,
    warningHtml,
    `<section class="section">`,
    `<div class="section-head">`,
    `<h3>工具契约 JSON 源码</h3>`,
    `<div class="section-actions">`,
    `<button type="button" class="btn btn-sm" data-action="format-json">格式化</button>`,
    `<button type="button" class="btn btn-sm" data-action="copy-contract">${ICONS.copy}<span>复制 JSON</span></button>`,
    `<button type="button" class="btn btn-sm btn-primary" data-action="apply-json">应用 JSON 到表单</button>`,
    `</div>`,
    `</div>`,
    `<div class="section-body">`,
    `<textarea id="raw-json-editor" class="json-editor" spellcheck="false">${escapeHtml(text)}</textarea>`,
    `</div>`,
    `</section>`,
    `</div>`,
  ]
    .filter(Boolean)
    .join("\n");
}

function panelHidden(state, name) {
  return (state.activeTab ?? "form") === name ? "" : " hidden";
}

function renderTextRow(elementId, label, binding, value, placeholder = "") {
  return [
    `<div class="field-row">`,
    `<label for="${elementId}">${label}</label>`,
    `<input id="${elementId}" type="text" ${binding} autocomplete="off" placeholder="${escapeHtml(placeholder)}" value="${escapeHtml(value)}">`,
    `</div>`,
  ].join("");
}

function renderSelectRow(elementId, label, binding, options) {
  return [
    `<div class="field-row">`,
    `<label for="${elementId}">${label}</label>`,
    `<select id="${elementId}" ${binding}>${options}</select>`,
    `</div>`,
  ].join("");
}

function renderLimitField(elementId, label, field, value) {
  return [
    `<div class="field-row">`,
    `<label for="${elementId}">${label}</label>`,
    `<input id="${elementId}" type="number" step="any" data-input-field="${field}" value="${escapeHtml(value)}">`,
    `</div>`,
  ].join("");
}

function renderEnumField(elementId, value) {
  return [
    `<div class="field-row">`,
    `<label for="${elementId}">枚举值（每行一个）</label>`,
    `<textarea id="${elementId}" rows="2" data-input-field="enumText" placeholder="每行填一个允许值">${escapeHtml(value)}</textarea>`,
    `</div>`,
  ].join("");
}

function renderOptions(values, selected) {
  const list = values.slice();
  if (selected && !list.includes(selected)) list.push(selected);
  return list
    .map((value) => {
      const chosen = value === selected ? " selected" : "";
      return `<option value="${escapeHtml(value)}"${chosen}>${escapeHtml(value)}</option>`;
    })
    .join("");
}

function renderNodeOptions(nodes, selected, outputFirst) {
  const list = Array.isArray(nodes) ? nodes.slice() : [];
  if (outputFirst) {
    list.sort((a, b) => Number(isOutputNode(b)) - Number(isOutputNode(a)));
  }
  const options = list.map((node) => {
    const suffix = outputFirst && isOutputNode(node) ? " [输出节点]" : "";
    const label = `${node.id} · ${node.title} (${node.class_type})${suffix}`;
    const chosen = String(node.id) === String(selected) ? " selected" : "";
    return `<option value="${escapeHtml(node.id)}"${chosen}>${escapeHtml(label)}</option>`;
  });
  if (selected && !list.some((node) => String(node.id) === String(selected))) {
    options.push(
      `<option value="${escapeHtml(selected)}" selected>${escapeHtml(selected)} [节点不存在]</option>`,
    );
  }
  return [`<option value="">未选择节点</option>`, ...options].join("");
}

function renderFieldOptions(fields, selected) {
  const list = Array.isArray(fields) ? fields : [];
  const options = list.map((field) => {
    const chosen = field.name === selected ? " selected" : "";
    const label = `${field.name} (${formatOriginalValue(field)})`;
    return `<option value="${escapeHtml(field.name)}"${chosen}>${escapeHtml(label)}</option>`;
  });
  if (selected && !list.some((field) => field.name === selected)) {
    options.push(
      `<option value="${escapeHtml(selected)}" selected>${escapeHtml(selected)} [字段不存在]</option>`,
    );
  }
  return [`<option value="">未选择字段</option>`, ...options].join("");
}

function isOutputNode(node) {
  if (node?.remote?.output_node === true) return true;
  return OUTPUT_NODE_PATTERN.test(String(node?.class_type ?? ""));
}

function findNode(nodes, nodeId) {
  const list = Array.isArray(nodes) ? nodes : [];
  return list.find((node) => String(node.id) === String(nodeId)) ?? null;
}
