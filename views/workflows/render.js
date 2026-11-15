/**
 * 工作流视图的 HTML 生成。
 *
 * 只把后端数据拼成字符串：不读 DOM、不发请求，交互与状态都在 app.js。
 * 类名全部来自 styles.css，契约草稿的形状来自 state.js。
 */

import { draftToContract, formatOriginalValue } from "./state.js";

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
const OUTPUT_FIELD_PRESETS = ["images", "videos", "audio", "text", "gifs"];
const OUTPUT_NODE_PATTERN = /Save|Preview|Output/;
const TABS = [
  ["form", "可视化表单"],
  ["inspect", "模型公开视图"],
  ["json", "JSON 源码"],
];
const HTML_ESCAPES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

/**
 * 转义一段要放进 HTML 文本或属性的内容。
 *
 * @param {unknown} text 任意待转义的取值。
 * @returns {string} 转义后的文本。
 */
export function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (char) => HTML_ESCAPES[char]);
}

/**
 * 渲染左侧工作流列表。
 *
 * @param {Array<object>} items 后端列出的工作流状态。
 * @param {string|null} selectedStem 当前选中的工作流名称。
 * @param {string} filterText 搜索词，匹配名称、工具名与描述。
 * @returns {string} `.workflow-list` 的内部 HTML。
 */
export function renderWorkflowList(items, selectedStem, filterText) {
  const list = Array.isArray(items) ? items : [];
  const keyword = String(filterText ?? "").trim().toLowerCase();
  const matched = list.filter((item) => matchesFilter(item, keyword));
  if (matched.length === 0) {
    return `<p class="workflow-item-meta">没有匹配的工作流</p>`;
  }
  return matched
    .map((item) => renderWorkflowItem(item, selectedStem))
    .join("\n");
}

/**
 * 渲染右侧工作区：顶栏、校验结果、三个标签页。
 *
 * @param {object} detail 后端返回的工作流详情。
 * @param {object} draft 表单草稿。
 * @param {object} uiState 视图状态，含当前标签页与确认状态。
 * @returns {string} `#workspace` 的内部 HTML。
 */
export function renderWorkspace(detail, draft, uiState) {
  const state = uiState ?? {};
  const errors = Array.isArray(state.errors) ? state.errors : detail.errors;
  return [
    renderHead(detail, state),
    renderValidation(errors),
    renderTabs(state),
    renderFormPanel(detail, draft, state),
    renderInspectPanel(draft, state),
    renderJsonPanel(draft, state),
  ].join("\n");
}

/**
 * 判断一个工作流条目是否命中搜索词。
 *
 * @param {object} item 工作流状态。
 * @param {string} keyword 已转成小写的搜索词，空串表示不过滤。
 * @returns {boolean} 命中返回 true。
 */
function matchesFilter(item, keyword) {
  if (!keyword) return true;
  return [item.stem, item.tool_name, item.description]
    .filter((part) => typeof part === "string")
    .some((part) => part.toLowerCase().includes(keyword));
}

/**
 * 渲染列表里的一个工作流条目。
 *
 * @param {object} item 工作流状态。
 * @param {string|null} selectedStem 当前选中的工作流名称。
 * @returns {string} 一条 `.workflow-item`。
 */
function renderWorkflowItem(item, selectedStem) {
  const classes = ["workflow-item"];
  if (item.stem === selectedStem) classes.push("is-active");
  const status = workflowStatus(item);
  const errorCount = Array.isArray(item.errors) ? item.errors.length : 0;
  const stats = [
    `参数 ${item.input_count ?? 0}`,
    `对外 ${item.visible_input_count ?? 0}`,
    `产物 ${item.output_count ?? 0}`,
    `${errorCount} 个问题`,
  ].join(" · ");
  return [
    `<div class="${classes.join(" ")}" data-stem="${escapeHtml(item.stem)}">`,
    `<span class="workflow-item-title">`,
    `<span>${escapeHtml(item.stem)}</span>`,
    `<span class="badge ${status.className}">${status.label}</span>`,
    `</span>`,
    `<span class="workflow-item-meta">${escapeHtml(toolText(item))}</span>`,
    `<span class="workflow-item-meta">${escapeHtml(stats)}</span>`,
    `</div>`,
  ].join("");
}

/**
 * 取列表条目上的工具名文字。
 *
 * @param {object} item 工作流状态。
 * @returns {string} 已配置时给出工具名，否则说明没有契约。
 */
function toolText(item) {
  return item.tool_name ? `工具 ${item.tool_name}` : "未配置工具契约";
}

/**
 * 按状态优先级给出列表徽章。
 *
 * @param {object} item 工作流状态。
 * @returns {{className: string, label: string}} 徽章样式与文字。
 */
function workflowStatus(item) {
  if (!item.has_workflow) {
    return { className: "badge-err", label: "缺少工作流" };
  }
  if (!item.has_contract) return { className: "badge-warn", label: "无契约" };
  if (!item.ok) return { className: "badge-err", label: "异常" };
  if (!item.enabled) return { className: "badge-muted", label: "已禁用" };
  return { className: "badge-ok", label: "启用正常" };
}

/**
 * 渲染工作区顶栏：工作流名、配对状态与全部操作按钮。
 *
 * @param {object} detail 工作流详情。
 * @param {object} state 视图状态。
 * @returns {string} 顶栏卡片。
 */
function renderHead(detail, state) {
  const pairing = [
    detail.has_workflow
      ? `工作流文件 ${detail.stem}.json 已就位`
      : `缺少工作流文件 ${detail.stem}.json`,
    detail.has_contract
      ? `工具契约 ${detail.stem}.tool.json 已配置`
      : `尚无工具契约 ${detail.stem}.tool.json`,
  ];
  const buttons = [
    renderActionButton("fetch-remote", "拉取 ComfyUI 节点定义"),
    renderActionButton("preview-local", "本地检查"),
    renderActionButton("preview-remote", "远端检查"),
    renderActionButton("save-contract", "保存契约", "btn btn-primary"),
    renderDeleteControls(detail, state),
  ].join("");
  return [
    `<div class="card">`,
    `<div class="card-head">`,
    `<div class="field-row">`,
    `<h2>${escapeHtml(detail.stem)}</h2>`,
    renderMetaRow(pairing),
    `</div>`,
    `<div class="topbar-actions">${buttons}</div>`,
    `</div>`,
    `</div>`,
  ].join("");
}

/**
 * 把若干说明文字渲染成一行元信息。
 *
 * @param {Array<string>} texts 说明文字。
 * @returns {string} `.meta-row`。
 */
function renderMetaRow(texts) {
  const spans = texts
    .map((text) => `<span>${escapeHtml(text)}</span>`)
    .join("");
  return `<div class="meta-row">${spans}</div>`;
}

/**
 * 渲染一个带 `data-action` 的按钮。
 *
 * @param {string} action 动作名。
 * @param {string} label 按钮文字。
 * @param {string} className 按钮样式类。
 * @returns {string} 按钮。
 */
function renderActionButton(action, label, className = "btn") {
  return (
    `<button type="button" class="${className}"` +
    ` data-action="${action}">${label}</button>`
  );
}

/**
 * 渲染删除操作，确认态用内联按钮而不是浏览器弹窗。
 *
 * @param {object} detail 工作流详情。
 * @param {object} state 视图状态。
 * @returns {string} 删除按钮或确认组。
 */
function renderDeleteControls(detail, state) {
  if (state.confirmDelete === "contract" && detail.has_contract) {
    return renderConfirmGroup("确认删除工具契约？模型将看不到这个工作流。");
  }
  if (state.confirmDelete === "all" && detail.has_workflow) {
    return renderConfirmGroup("确认删除工作流文件？工具契约会一起删除。");
  }
  const buttons = [];
  if (detail.has_contract) {
    buttons.push(
      renderActionButton("delete-contract", "仅删除契约", "btn btn-sm btn-danger"),
    );
  }
  if (detail.has_workflow) {
    buttons.push(
      renderActionButton(
        "delete-workflow",
        "删除工作流文件",
        "btn btn-sm btn-danger",
      ),
    );
  }
  return buttons.join("");
}

/**
 * 渲染二次确认组。
 *
 * @param {string} hint 确认提示文字。
 * @returns {string} `.confirm-group`。
 */
function renderConfirmGroup(hint) {
  return [
    `<span class="confirm-group">`,
    `<span class="confirm-group-hint">${escapeHtml(hint)}</span>`,
    `<span class="confirm-actions">`,
    renderActionButton("confirm-delete", "确认删除", "btn btn-sm btn-danger"),
    renderActionButton("cancel-delete", "取消", "btn btn-sm"),
    `</span>`,
    `</span>`,
  ].join("");
}

/**
 * 渲染校验结果面板。
 *
 * @param {Array<string>} errors 校验问题列表。
 * @returns {string} 校验卡片。
 */
function renderValidation(errors) {
  const list = Array.isArray(errors) ? errors : [];
  const badge = list.length
    ? `<span class="badge badge-err">${list.length} 个问题</span>`
    : `<span class="badge badge-ok">通过</span>`;
  const body = list.length
    ? renderErrorList(list)
    : `<p>✅ 契约检查通过</p>`;
  return [
    `<div class="card">`,
    `<div class="card-head"><h3>契约检查</h3>${badge}</div>`,
    body,
    `</div>`,
  ].join("");
}

/**
 * 渲染校验问题清单。
 *
 * @param {Array<string>} errors 校验问题列表。
 * @returns {string} `.error-list`。
 */
function renderErrorList(errors) {
  const items = errors
    .map((text) => `<li>${escapeHtml(text)}</li>`)
    .join("");
  return `<ul class="error-list">${items}</ul>`;
}

/**
 * 渲染标签页按钮。
 *
 * @param {object} state 视图状态。
 * @returns {string} `.tabs`。
 */
function renderTabs(state) {
  const active = state.activeTab ?? "form";
  const buttons = TABS.map(([name, label]) =>
    renderTabButton(name, label, active),
  ).join("");
  return `<div class="tabs">${buttons}</div>`;
}

/**
 * 渲染一个标签页按钮。
 *
 * @param {string} name 标签页名称。
 * @param {string} label 按钮文字。
 * @param {string} active 当前标签页名称。
 * @returns {string} `.tab-btn`。
 */
function renderTabButton(name, label, active) {
  const classes = ["tab-btn"];
  if (name === active) classes.push("is-active");
  return (
    `<button type="button" class="${classes.join(" ")}"` +
    ` data-action="switch-tab" data-tab="${name}">${label}</button>`
  );
}

/**
 * 判断一个标签页面板是否要隐藏。
 *
 * @param {object} state 视图状态。
 * @param {string} name 标签页名称。
 * @returns {string} 隐藏时返回 ` hidden`，否则空串。
 */
function panelHidden(state, name) {
  return (state.activeTab ?? "form") === name ? "" : " hidden";
}

/**
 * 渲染可视化表单标签页。
 *
 * @param {object} detail 工作流详情。
 * @param {object} draft 表单草稿。
 * @param {object} state 视图状态。
 * @returns {string} `form` 面板。
 */
function renderFormPanel(detail, draft, state) {
  return [
    `<div class="tab-panel" data-panel="form"${panelHidden(state, "form")}>`,
    renderBasicsCard(draft),
    renderInputsCard(detail, draft),
    renderOutputsCard(detail, draft),
    `</div>`,
  ].join("\n");
}

/**
 * 渲染基础信息卡片。
 *
 * @param {object} draft 表单草稿。
 * @returns {string} 基础信息卡片。
 */
function renderBasicsCard(draft) {
  return [
    `<div class="card">`,
    `<div class="card-head"><h3>基础信息</h3></div>`,
    `<div class="grid-2">`,
    `<div class="field-row">`,
    `<label for="contract-name">工具名称</label>`,
    `<input id="contract-name" type="text" data-basic-field="name"` +
      ` autocomplete="off" value="${escapeHtml(draft.name)}">`,
    `</div>`,
    `<div class="field-row field-row-inline">`,
    `<input id="contract-enabled" type="checkbox" data-basic-field="enabled"` +
      `${draft.enabled ? " checked" : ""}>`,
    `<label for="contract-enabled">启用，保存后模型即可调用</label>`,
    `</div>`,
    `</div>`,
    `<div class="field-row">`,
    `<label for="contract-description">工具描述</label>`,
    `<input id="contract-description" type="text" data-basic-field="description"` +
      ` autocomplete="off" value="${escapeHtml(draft.description)}">`,
    `</div>`,
    `</div>`,
  ].join("\n");
}

/**
 * 渲染参数映射卡片。
 *
 * @param {object} detail 工作流详情。
 * @param {object} draft 表单草稿。
 * @returns {string} 参数映射卡片。
 */
function renderInputsCard(detail, draft) {
  const rows = draft.inputs
    .map((row, index) => renderInputRow(detail, row, index))
    .join("\n");
  return [
    `<div class="card">`,
    `<div class="card-head">`,
    `<h3>参数映射</h3>`,
    renderActionButton("add-input", "添加参数", "btn btn-sm"),
    `</div>`,
    rows || `<p class="field-label">还没有参数映射</p>`,
    `</div>`,
  ].join("\n");
}

/**
 * 渲染一条参数映射。
 *
 * @param {object} detail 工作流详情。
 * @param {object} row 参数行。
 * @param {number} index 行下标。
 * @returns {string} 一张参数卡片。
 */
function renderInputRow(detail, row, index) {
  const id = (suffix) => `input-${index}-${suffix}`;
  const node = findNode(detail.nodes, row.node);
  const remoteButton = remoteFieldOf(node, row.field)
    ? renderActionButton(
        "apply-remote-field",
        "从节点定义填入约束",
        "btn btn-sm",
      )
    : "";
  return [
    `<div class="card" data-input-index="${index}">`,
    `<div class="card-head">`,
    `<h4>参数 ${index + 1}</h4>`,
    renderActionButton("remove-input", "删除", "btn btn-sm btn-danger"),
    `</div>`,
    `<div class="grid-3">`,
    renderTextRow(id("key"), "参数名", 'data-input-field="key"', row.key),
    renderSelectRow(
      id("type"),
      "类型",
      'data-input-field="type"',
      renderOptions(INPUT_TYPES, row.type),
    ),
    renderSelectRow(
      id("node"),
      "映射节点",
      'data-input-field="node"',
      renderNodeOptions(detail.nodes, row.node, false),
    ),
    `</div>`,
    `<div class="grid-2">`,
    renderSelectRow(
      id("field"),
      "映射字段",
      'data-input-field="field"',
      renderFieldOptions(node?.fields, row.field),
    ),
    renderTextRow(
      id("description"),
      "参数描述",
      'data-input-field="description"',
      row.description,
    ),
    `</div>`,
    remoteButton,
    `<div class="field-row field-row-inline">`,
    `<input id="${id("visible")}" type="checkbox" data-input-field="visible"` +
      `${row.visible ? " checked" : ""}>`,
    `<label for="${id("visible")}">对模型可见</label>`,
    `<input id="${id("required")}" type="checkbox" data-input-field="required"` +
      `${row.required ? " checked" : ""}` +
      `${row.visible ? "" : " disabled"}>`,
    `<label for="${id("required")}">必填</label>`,
    `</div>`,
    renderInputConstraints(row, id),
    `</div>`,
  ]
    .filter(Boolean)
    .join("\n");
}

/**
 * 渲染一行文本输入。
 *
 * @param {string} elementId 输入框 id。
 * @param {string} label 显示名。
 * @param {string} binding 草稿绑定属性，例如 `data-input-field="key"`。
 * @param {string} value 当前值。
 * @returns {string} 一行 `.field-row`。
 */
function renderTextRow(elementId, label, binding, value) {
  return [
    `<div class="field-row">`,
    `<label for="${elementId}">${label}</label>`,
    `<input id="${elementId}" type="text" ${binding}` +
      ` autocomplete="off" value="${escapeHtml(value)}">`,
    `</div>`,
  ].join("");
}

/**
 * 渲染一行下拉选择。
 *
 * @param {string} elementId 选择框 id。
 * @param {string} label 显示名。
 * @param {string} binding 草稿绑定属性，例如 `data-input-field="type"`。
 * @param {string} options 已经渲染好的 `<option>` 列表。
 * @returns {string} 一行 `.field-row`。
 */
function renderSelectRow(elementId, label, binding, options) {
  return [
    `<div class="field-row">`,
    `<label for="${elementId}">${label}</label>`,
    `<select id="${elementId}" ${binding}>${options}</select>`,
    `</div>`,
  ].join("");
}

/**
 * 按参数类型渲染长度、范围与枚举输入。
 *
 * @param {object} row 参数行。
 * @param {(suffix: string) => string} id 生成元素 id 的函数。
 * @returns {string} 约束输入；该类型没有约束时返回空串。
 */
function renderInputConstraints(row, id) {
  if (row.type === "string") {
    const templateRow = row.visible
      ? ""
      : renderTextRow(
          id("valueTemplate"),
          "隐藏值模板（可选，支持 %date:yyyy-MM-dd%）",
          'data-input-field="valueTemplate"',
          row.valueTemplate,
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

/**
 * 渲染一个数值约束输入。
 *
 * @param {string} elementId 输入框 id。
 * @param {string} label 显示名。
 * @param {string} field 草稿字段名。
 * @param {string} value 当前值。
 * @returns {string} 一行 `.field-row`。
 */
function renderLimitField(elementId, label, field, value) {
  return [
    `<div class="field-row">`,
    `<label for="${elementId}">${label}</label>`,
    `<input id="${elementId}" type="number" data-input-field="${field}"` +
      ` value="${escapeHtml(value)}">`,
    `</div>`,
  ].join("");
}

/**
 * 渲染枚举输入，一行一个值。
 *
 * @param {string} elementId 文本框 id。
 * @param {string} value 当前枚举文本。
 * @returns {string} 一行 `.field-row`。
 */
function renderEnumField(elementId, value) {
  return [
    `<div class="field-row">`,
    `<label for="${elementId}">枚举值（每行一个）</label>`,
    `<textarea id="${elementId}" rows="2" data-input-field="enumText">` +
      `${escapeHtml(value)}</textarea>`,
    `</div>`,
  ].join("");
}

/**
 * 渲染产物映射卡片。
 *
 * @param {object} detail 工作流详情。
 * @param {object} draft 表单草稿。
 * @returns {string} 产物映射卡片。
 */
function renderOutputsCard(detail, draft) {
  const rows = draft.outputs
    .map((row, index) => renderOutputRow(detail, row, index))
    .join("\n");
  const presets = OUTPUT_FIELD_PRESETS.map(
    (name) => `<option value="${name}"></option>`,
  ).join("");
  return [
    `<div class="card">`,
    `<div class="card-head">`,
    `<h3>产物映射</h3>`,
    renderActionButton("add-output", "添加产物", "btn btn-sm"),
    `</div>`,
    rows || `<p class="field-label">还没有产物映射</p>`,
    `<datalist id="output-field-presets">${presets}</datalist>`,
    `</div>`,
  ].join("\n");
}

/**
 * 渲染一条产物映射。
 *
 * @param {object} detail 工作流详情。
 * @param {object} row 产物行。
 * @param {number} index 行下标。
 * @returns {string} 一张产物卡片。
 */
function renderOutputRow(detail, row, index) {
  const id = (suffix) => `output-${index}-${suffix}`;
  return [
    `<div class="card" data-output-index="${index}">`,
    `<div class="card-head">`,
    `<h4>产物 ${index + 1}</h4>`,
    renderActionButton("remove-output", "删除", "btn btn-sm btn-danger"),
    `</div>`,
    `<div class="grid-3">`,
    renderTextRow(id("key"), "产物名", 'data-output-field="key"', row.key),
    renderSelectRow(
      id("type"),
      "产物类型",
      'data-output-field="type"',
      renderOptions(OUTPUT_TYPES, row.type),
    ),
    renderSelectRow(
      id("node"),
      "映射节点",
      'data-output-field="node"',
      renderNodeOptions(detail.nodes, row.node, true),
    ),
    `</div>`,
    `<div class="field-row">`,
    `<label for="${id("field")}">映射字段</label>`,
    `<input id="${id("field")}" type="text" list="output-field-presets"` +
      ` data-output-field="field" autocomplete="off"` +
      ` value="${escapeHtml(row.field)}">`,
    `</div>`,
    `</div>`,
  ].join("\n");
}

/**
 * 渲染一组固定的下拉选项。
 *
 * @param {Array<string>} values 可选值。
 * @param {string} selected 当前值；不在可选值里时追加保留项。
 * @returns {string} `<option>` 列表。
 */
function renderOptions(values, selected) {
  const list = values.slice();
  if (selected && !list.includes(selected)) list.push(selected);
  return list
    .map((value) => {
      const chosen = value === selected ? " selected" : "";
      return (
        `<option value="${escapeHtml(value)}"${chosen}>` +
        `${escapeHtml(value)}</option>`
      );
    })
    .join("");
}

/**
 * 渲染节点下拉选项。
 *
 * @param {Array<object>} nodes 后端给出的节点列表。
 * @param {string} selected 当前节点 id。
 * @param {boolean} markOutput 是否给输出节点加标注。
 * @returns {string} `<option>` 列表。
 */
function renderNodeOptions(nodes, selected, markOutput) {
  const list = Array.isArray(nodes) ? nodes : [];
  const options = list.map((node) => {
    const suffix = markOutput && isOutputNode(node) ? " [输出节点]" : "";
    const label = `${node.id} | ${node.title} (${node.class_type})${suffix}`;
    const chosen = String(node.id) === String(selected) ? " selected" : "";
    return (
      `<option value="${escapeHtml(node.id)}"${chosen}>` +
      `${escapeHtml(label)}</option>`
    );
  });
  if (selected && !list.some((node) => String(node.id) === String(selected))) {
    const label = `${selected} [节点不存在]`;
    options.push(
      `<option value="${escapeHtml(selected)}" selected>` +
        `${escapeHtml(label)}</option>`,
    );
  }
  return [`<option value="">未选择节点</option>`, ...options].join("");
}

/**
 * 渲染字段下拉选项。
 *
 * @param {Array<object>} fields 节点字段列表。
 * @param {string} selected 当前字段名。
 * @returns {string} `<option>` 列表。
 */
function renderFieldOptions(fields, selected) {
  const list = Array.isArray(fields) ? fields : [];
  const options = list.map((field) => {
    const chosen = field.name === selected ? " selected" : "";
    const label = `${field.name} (${formatOriginalValue(field)})`;
    return (
      `<option value="${escapeHtml(field.name)}"${chosen}>` +
      `${escapeHtml(label)}</option>`
    );
  });
  if (selected && !list.some((field) => field.name === selected)) {
    const label = `${selected} [字段不存在]`;
    options.push(
      `<option value="${escapeHtml(selected)}" selected>` +
        `${escapeHtml(label)}</option>`,
    );
  }
  return [`<option value="">未选择字段</option>`, ...options].join("");
}

/**
 * 判断一个节点是否能产出文件。
 *
 * @param {object} node 节点。
 * @returns {boolean} 远端标记为输出节点，或节点类型名像输出节点时返回 true。
 */
function isOutputNode(node) {
  if (node?.remote?.output_node === true) return true;
  return OUTPUT_NODE_PATTERN.test(String(node?.class_type ?? ""));
}

/**
 * 按节点 id 找节点。
 *
 * @param {Array<object>} nodes 节点列表。
 * @param {string} nodeId 节点 id。
 * @returns {object|null} 命中节点，找不到返回 null。
 */
function findNode(nodes, nodeId) {
  const list = Array.isArray(nodes) ? nodes : [];
  return list.find((node) => String(node.id) === String(nodeId)) ?? null;
}

/**
 * 取远端节点定义里的一个字段。
 *
 * @param {object|null} node 节点。
 * @param {string} field 字段名。
 * @returns {object|null} 远端字段定义，没有时返回 null。
 */
function remoteFieldOf(node, field) {
  return node?.remote?.fields?.[field] ?? null;
}

/**
 * 渲染隐藏参数提示。
 *
 * @param {Array<string>} names 隐藏参数名。
 * @returns {string} 一条警告横幅。
 */
function renderHiddenHint(names) {
  const text = `隐藏参数：${names.join("、")}，不出现在模型视图里，也传不进来`;
  return `<p class="banner banner-warn">${escapeHtml(text)}</p>`;
}

/**
 * 渲染模型公开视图标签页。
 *
 * @param {object} draft 表单草稿。
 * @param {object} state 视图状态。
 * @returns {string} `inspect` 面板。
 */
function renderInspectPanel(draft, state) {
  const hidden = draft.inputs
    .filter((row) => row.key && !row.visible)
    .map((row) => row.key);
  const hint = hidden.length ? renderHiddenHint(hidden) : "";
  const preview = state.inspectPreview
    ? `<pre class="code-box">` +
      `${escapeHtml(JSON.stringify(state.inspectPreview, null, 2))}</pre>`
    : `<p class="banner">尚未生成模型视图，先执行本地检查或刷新模型视图。</p>`;
  return [
    `<div class="tab-panel" data-panel="inspect"${panelHidden(state, "inspect")}>`,
    `<div class="card">`,
    `<div class="card-head">`,
    `<h3>模型公开视图</h3>`,
    renderActionButton("refresh-inspect", "刷新模型视图", "btn btn-sm"),
    `</div>`,
    hint,
    preview,
    `</div>`,
    `</div>`,
  ]
    .filter(Boolean)
    .join("\n");
}

/**
 * 渲染 JSON 源码标签页。
 *
 * @param {object} draft 表单草稿。
 * @param {object} state 视图状态。
 * @returns {string} `json` 面板。
 */
function renderJsonPanel(draft, state) {
  const text = JSON.stringify(draftToContract(draft), null, 2);
  return [
    `<div class="tab-panel" data-panel="json"${panelHidden(state, "json")}>`,
    `<div class="card">`,
    `<div class="card-head">`,
    `<h3>契约 JSON</h3>`,
    renderActionButton("apply-json", "应用 JSON 到表单"),
    `</div>`,
    `<textarea id="raw-json-editor" class="json-editor"` +
      ` spellcheck="false">${escapeHtml(text)}</textarea>`,
    `</div>`,
    `</div>`,
  ].join("\n");
}
