/**
 * 工作流视图的状态与纯数据转换。
 *
 * 负责工具契约与表单草稿之间的无损转换、节点字段智能推断、
 * 实时本地校验提示以及模型公开视图的本地预览构建。
 */

const TOOL_NAME_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const TOOL_NAME_MAX_LENGTH = 64;
const TOOL_NAME_PREFIX = "comfy_";
const DEFAULT_INPUT_TYPE = "string";
const DEFAULT_OUTPUT_TYPE = "image";
const ENUM_TYPES = new Set(["string", "integer", "number"]);
const TEXT_TYPES = new Set(["string"]);
const NUMBER_TYPES = new Set(["integer", "number"]);
const REFERENCE_TYPES = new Set(["image", "video", "audio", "file"]);

export const OUTPUT_NODE_DEFAULTS = {
  SaveImage: { type: "image", field: "images", key: "image" },
  PreviewImage: { type: "image", field: "images", key: "image" },
  SaveAnimatedWEBP: { type: "image", field: "images", key: "image" },
  SaveVideo: { type: "video", field: "images", key: "video" },
  VHS_VideoCombine: { type: "video", field: "gifs", key: "video" },
  SaveAudio: { type: "audio", field: "audio", key: "audio" },
  SaveAudioMP3: { type: "audio", field: "audio", key: "audio" },
  SaveAudioFLAC: { type: "audio", field: "audio", key: "audio" },
};

const LOADER_FIELD_HINTS = {
  LoadImage: { image: "image" },
  LoadImageMask: { image: "image" },
  VHS_LoadVideo: { video: "video" },
  LoadVideo: { file: "video", video: "video" },
  LoadAudio: { audio: "audio" },
};

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

const JSON_TYPES = {
  string: "string",
  integer: "integer",
  number: "number",
  boolean: "boolean",
  image: "string",
  video: "string",
  audio: "string",
  file: "string",
};

/**
 * 由工作流文件名推出合法的工具名。
 *
 * @param {string} stem 工作流文件名去掉 `.json` 的部分。
 * @returns {string} 符合 `^[a-z][a-z0-9_]{0,63}$` 的工具名。
 */
export function suggestToolName(stem) {
  const normalized = String(stem ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "");
  const prefixed = /^[a-z]/.test(normalized)
    ? normalized
    : `${TOOL_NAME_PREFIX}${normalized}`;
  const clipped = prefixed
    .slice(0, TOOL_NAME_MAX_LENGTH)
    .replace(/_+$/, "");
  return TOOL_NAME_PATTERN.test(clipped) ? clipped : "comfy";
}

/**
 * 由字段名推出合法且不重复的蛇形标识符。
 *
 * @param {string} rawName 原始字段名或预设名。
 * @param {Iterable<string>} usedNames 已被占用的名称集合。
 * @param {string} fallback 无法规范化时的兜底前缀。
 * @returns {string} 合法且不重复的标识符。
 */
export function suggestUniqueKey(rawName, usedNames, fallback = "param") {
  let base = String(rawName ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (!/^[a-z]/.test(base)) {
    base = base ? `${fallback}_${base}` : fallback;
  }
  base = base.slice(0, 56).replace(/_+$/, "") || fallback;
  const taken = new Set(usedNames);
  if (!taken.has(base)) return base;
  let seq = 2;
  while (taken.has(`${base}_${seq}`)) {
    seq += 1;
  }
  return `${base}_${seq}`;
}

/**
 * 为没有配套契约的工作流生成初始契约。
 *
 * @param {string} stem 工作流文件名去掉 `.json` 的部分。
 * @param {Array<object>} nodes 后端给出的节点列表。
 * @returns {object} 初始契约。
 */
export function createDraftContract(stem, nodes) {
  return {
    version: 1,
    name: suggestToolName(stem),
    description: "",
    model_visible: false,
    inputs: {},
    outputs: defaultOutputs(nodes),
  };
}

/**
 * 把一份契约转成表单编辑用的草稿。
 *
 * @param {object|null} rawContract 后端返回的契约；缺失时按无契约处理。
 * @param {string} stem 工作流文件名去掉 `.json` 的部分。
 * @param {Array<object>} nodes 后端给出的节点列表。
 * @returns {object} 含 `inputs` 与 `outputs` 数组的草稿。
 */
export function contractToDraft(rawContract, stem, nodes) {
  const source =
    rawContract && typeof rawContract === "object"
      ? rawContract
      : createDraftContract(stem, nodes);
  return {
    name: source.name ?? suggestToolName(stem),
    description: source.description ?? "",
    model_visible: Boolean(source.model_visible),
    inputs: inputRows(source.inputs),
    outputs: outputRows(source.outputs),
  };
}

/**
 * 把草稿序列化成稳定快照字符串，用于对比是否有未保存更改。
 *
 * @param {object|null} draft 表单草稿。
 * @returns {string} 快照字符串。
 */
export function draftSnapshot(draft) {
  if (!draft) return "";
  return JSON.stringify(draft);
}

/**
 * 把表单草稿转成契约对象（严格校验空名与重名）。
 *
 * @param {object} draft 表单草稿。
 * @returns {object} 符合契约 Schema 的对象。
 */
export function draftToContract(draft) {
  const errors = [];
  const inputs = collectNamedEntries(draft.inputs, "参数", inputSpec, errors);
  const outputs = collectNamedEntries(
    draft.outputs,
    "输出",
    (row) => ({
      type: row.type ?? DEFAULT_OUTPUT_TYPE,
      target: targetOf(row),
    }),
    errors,
  );
  if (errors.length > 0) {
    throw new Error(errors.join("\n"));
  }
  return {
    version: 1,
    name: String(draft.name ?? "").trim(),
    description: String(draft.description ?? "").trim(),
    model_visible: Boolean(draft.model_visible),
    inputs,
    outputs,
  };
}

/**
 * 安全生成 JSON 源码展示用的对象，草稿存在空名或重名时不抛错。
 *
 * @param {object} draft 表单草稿。
 * @returns {{ contract: object, warnings: Array<string> }} 契约对象与非阻断提醒。
 */
export function draftToSafeObject(draft) {
  const warnings = [];
  const inputs = {};
  const outputs = {};

  for (const [index, row] of (draft?.inputs ?? []).entries()) {
    let key = String(row.key ?? "").trim();
    if (!key) {
      key = `__unnamed_input_${index + 1}`;
      warnings.push(`第 ${index + 1} 个参数未填写名称（源码中暂以占位名展示）`);
    } else if (Object.hasOwn(inputs, key)) {
      warnings.push(`参数名「${key}」重复`);
      key = `${key}__dup_${index + 1}`;
    }
    inputs[key] = inputSpec(row);
  }

  for (const [index, row] of (draft?.outputs ?? []).entries()) {
    let key = String(row.key ?? "").trim();
    if (!key) {
      key = `__unnamed_output_${index + 1}`;
      warnings.push(`第 ${index + 1} 个输出未填写名称（源码中暂以占位名展示）`);
    } else if (Object.hasOwn(outputs, key)) {
      warnings.push(`输出名「${key}」重复`);
      key = `${key}__dup_${index + 1}`;
    }
    outputs[key] = {
      type: row.type ?? DEFAULT_OUTPUT_TYPE,
      target: targetOf(row),
    };
  }

  return {
    contract: {
      version: 1,
      name: String(draft?.name ?? "").trim(),
      description: String(draft?.description ?? "").trim(),
      model_visible: Boolean(draft?.model_visible),
      inputs,
      outputs,
    },
    warnings,
  };
}

/**
 * 在本地做即时基础校验，返回可直接定位到表单行的问题列表。
 *
 * @param {object} draft 表单草稿。
 * @param {Array<object>} nodes 工作流节点列表。
 * @returns {Array<{ message: string, target?: { kind: "basic"|"input"|"output", index?: number, field?: string } }>} 问题列表。
 */
export function collectLocalDraftIssues(draft, nodes) {
  const issues = [];
  if (!draft) return issues;
  const name = String(draft.name ?? "").trim();
  if (!TOOL_NAME_PATTERN.test(name)) {
    issues.push({
      message: "工具名称需以小写字母开头，仅含小写字母、数字与下划线（最长 64 字符）",
      target: { kind: "basic", field: "name" },
    });
  }
  if (!String(draft.description ?? "").trim()) {
    issues.push({
      message: "请填写工具描述，帮助模型理解何时调用该工作流工具",
      target: { kind: "basic", field: "description" },
    });
  }

  const nodeMap = new Map((nodes ?? []).map((n) => [String(n.id), n]));
  const inputKeys = new Set();

  for (const [index, row] of (draft.inputs ?? []).entries()) {
    const key = String(row.key ?? "").trim();
    const label = key ? `参数「${key}」` : `第 ${index + 1} 个参数`;
    if (!key) {
      issues.push({
        message: `第 ${index + 1} 个参数未填写名称`,
        target: { kind: "input", index, field: "key" },
      });
    } else if (!TOOL_NAME_PATTERN.test(key)) {
      issues.push({
        message: `${label} 名称不合法，需以小写字母开头且仅含小写字母、数字和下划线`,
        target: { kind: "input", index, field: "key" },
      });
    } else if (inputKeys.has(key)) {
      issues.push({
        message: `参数名「${key}」重复`,
        target: { kind: "input", index, field: "key" },
      });
    } else {
      inputKeys.add(key);
    }

    if (!String(row.description ?? "").trim()) {
      issues.push({
        message: `${label} 未填写参数描述`,
        target: { kind: "input", index, field: "description" },
      });
    }

    const nodeId = String(row.node ?? "").trim();
    const fieldName = String(row.field ?? "").trim();
    if (!nodeId || !fieldName) {
      issues.push({
        message: `${label} 尚未选择映射节点与字段`,
        target: { kind: "input", index, field: nodeId ? "field" : "node" },
      });
    } else if (nodeMap.size > 0) {
      const node = nodeMap.get(nodeId);
      if (!node) {
        issues.push({
          message: `${label} 映射的节点 ${nodeId} 在工作流中不存在`,
          target: { kind: "input", index, field: "node" },
        });
      } else {
        const field = (node.fields ?? []).find((f) => f.name === fieldName);
        if (!field) {
          issues.push({
            message: `${label} 映射的字段 ${nodeId}.${fieldName} 在节点中不存在`,
            target: { kind: "input", index, field: "field" },
          });
        } else if (field.is_link) {
          issues.push({
            message: `${label} 映射的 ${nodeId}.${fieldName} 是节点连线，不能直接作为输入参数`,
            target: { kind: "input", index, field: "field" },
          });
        }
      }
    }
  }

  const outputKeys = new Set();
  if (!draft.outputs || draft.outputs.length === 0) {
    issues.push({
      message: "至少需要配置一条输出映射，否则无法交付产物",
      target: { kind: "output", index: -1 },
    });
  }
  for (const [index, row] of (draft.outputs ?? []).entries()) {
    const key = String(row.key ?? "").trim();
    const label = key ? `输出「${key}」` : `第 ${index + 1} 个输出`;
    if (!key) {
      issues.push({
        message: `第 ${index + 1} 个输出未填写名称`,
        target: { kind: "output", index, field: "key" },
      });
    } else if (!TOOL_NAME_PATTERN.test(key)) {
      issues.push({
        message: `${label} 名称不合法，需以小写字母开头且仅含小写字母、数字和下划线`,
        target: { kind: "output", index, field: "key" },
      });
    } else if (outputKeys.has(key)) {
      issues.push({
        message: `输出名「${key}」重复`,
        target: { kind: "output", index, field: "key" },
      });
    } else {
      outputKeys.add(key);
    }

    const nodeId = String(row.node ?? "").trim();
    const fieldName = String(row.field ?? "").trim();
    if (!nodeId || !fieldName) {
      issues.push({
        message: `${label} 尚未填写完整映射节点与字段`,
        target: { kind: "output", index, field: nodeId ? "field" : "node" },
      });
    } else if (nodeMap.size > 0 && !nodeMap.has(nodeId)) {
      issues.push({
        message: `${label} 映射的节点 ${nodeId} 在工作流中不存在`,
        target: { kind: "output", index, field: "node" },
      });
    }
  }

  return issues;
}

/**
 * 基于当前草稿生成本地模型公开视图预览（无需等待后端校验通过）。
 *
 * @param {object} draft 表单草稿。
 * @returns {object} 包含名称、描述、参数 Schema 及隐藏参数清单的对象。
 */
export function buildInspectPreview(draft) {
  const properties = {};
  const required = [];
  const visibleRows = [];
  const hiddenKeys = [];

  for (const row of draft?.inputs ?? []) {
    const key = String(row.key ?? "").trim();
    if (!key) continue;
    if (!row.visible) {
      hiddenKeys.push(key);
      continue;
    }
    const spec = inputSpec(row);
    const prop = {
      type: JSON_TYPES[spec.type] ?? "string",
      description: REFERENCE_TYPES.has(spec.type)
        ? `${spec.description}${spec.description ? "。" : ""}传文件引用（附件、路径、file:// 或链接）`
        : spec.description,
    };
    if (spec.enum) prop.enum = spec.enum;
    if (spec.minLength !== undefined) prop.minLength = spec.minLength;
    if (spec.maxLength !== undefined) prop.maxLength = spec.maxLength;
    if (spec.minimum !== undefined) prop.minimum = spec.minimum;
    if (spec.maximum !== undefined) prop.maximum = spec.maximum;
    properties[key] = prop;
    if (spec.required) required.push(key);
    visibleRows.push({
      key,
      type: spec.type,
      required: Boolean(spec.required),
      description: spec.description,
      constraints: summarizeConstraints(spec),
    });
  }

  const parameters = {
    type: "object",
    properties,
  };
  if (required.length > 0) {
    parameters.required = required;
  }

  return {
    name: String(draft?.name ?? "").trim(),
    description: String(draft?.description ?? "").trim(),
    model_visible: Boolean(draft?.model_visible),
    parameters,
    visibleRows,
    hiddenKeys,
    outputs: (draft?.outputs ?? []).map((o) => ({
      key: String(o.key ?? "").trim(),
      type: o.type ?? DEFAULT_OUTPUT_TYPE,
      node: String(o.node ?? "").trim(),
      field: String(o.field ?? "").trim(),
    })),
  };
}

/**
 * 从节点字段与远端节点定义推断一条新参数草稿行。
 *
 * @param {object} node 工作流节点。
 * @param {object} field 节点字段对象。
 * @param {Array<object>} existingInputs 当前已有参数行。
 * @returns {object} 预填好的参数草稿行（description 故意留空）。
 */
export function buildInputRowFromField(node, field, existingInputs) {
  const used = (existingInputs ?? []).map((r) => String(r.key ?? "").trim()).filter(Boolean);
  const remote = node?.remote?.fields?.[field.name] ?? null;
  const inferredType = inferInputType(node, field, remote);
  const row = {
    key: suggestUniqueKey(field.name, used, "param"),
    type: inferredType,
    description: "",
    visible: true,
    required: false,
    node: String(node.id),
    field: String(field.name),
    enumText: "",
    minLength: "",
    maxLength: "",
    minimum: "",
    maximum: "",
    valueTemplate: "",
  };
  if (remote) {
    if (Array.isArray(remote.enum) && remote.enum.length > 0) {
      row.enumText = remote.enum.map((v) => String(v)).join("\n");
    }
    if (row.type === "integer" || row.type === "number") {
      row.minimum = editableLimit(remote.minimum);
      row.maximum = editableLimit(remote.maximum);
    }
  }
  return row;
}

/**
 * 从工作流节点推断一条新输出映射行。
 *
 * @param {object} node 工作流节点。
 * @param {Array<object>} existingOutputs 当前已有输出行。
 * @returns {object} 预填好的输出映射草稿行。
 */
export function buildOutputRowFromNode(node, existingOutputs) {
  const used = (existingOutputs ?? []).map((r) => String(r.key ?? "").trim()).filter(Boolean);
  const preset = OUTPUT_NODE_DEFAULTS[node?.class_type];
  const type = preset?.type ?? DEFAULT_OUTPUT_TYPE;
  const field = preset?.field ?? "images";
  const baseKey = preset?.key ?? "output";
  return {
    key: suggestUniqueKey(baseKey, used, "output"),
    type,
    node: String(node.id),
    field,
  };
}

/**
 * 把节点字段的取值格式化成一行可读文字。
 *
 * @param {object|null} fieldObj 节点字段，形如 `{ is_link, value, link }`。
 * @returns {string} 连线写成 `连线 [30:8, 0]`，常量写成 `原值: "9:16"`。
 */
export function formatOriginalValue(fieldObj) {
  if (!fieldObj || typeof fieldObj !== "object") return "";
  if (fieldObj.is_link) {
    const link = Array.isArray(fieldObj.link) ? fieldObj.link : [];
    return `连线 [${link.map((part) => String(part)).join(", ")}]`;
  }
  return `原值: ${JSON.stringify(fieldObj.value ?? null)}`;
}

/**
 * 把节点字段的纯值格式化为紧凑预览字符串（不含前缀）。
 *
 * @param {object|null} fieldObj 节点字段对象。
 * @returns {string} 值文本。
 */
export function formatRawValue(fieldObj) {
  if (!fieldObj || typeof fieldObj !== "object") return "";
  if (fieldObj.is_link) {
    const link = Array.isArray(fieldObj.link) ? fieldObj.link : [];
    return `[${link.map((part) => String(part)).join(", ")}]`;
  }
  return JSON.stringify(fieldObj.value ?? null);
}

/**
 * 推断节点字段对应的参数类型。
 *
 * @param {object} node 节点对象。
 * @param {object} field 字段对象。
 * @param {object|null} remote 远端字段定义。
 * @returns {string} 契约参数类型。
 */
export function inferInputType(node, field, remote) {
  const loaderType = LOADER_FIELD_HINTS[node?.class_type]?.[field?.name];
  if (loaderType) return loaderType;
  if (remote?.suggested_type) {
    const mapped = REMOTE_TYPE_MAP[String(remote.suggested_type).toUpperCase()];
    if (mapped) return mapped;
  }
  const value = field?.value;
  if (typeof value === "boolean") return "boolean";
  if (typeof value === "number") {
    return Number.isInteger(value) ? "integer" : "number";
  }
  return DEFAULT_INPUT_TYPE;
}

/**
 * 汇总单个参数的约束描述，用于摘要行和预览表格展示。
 *
 * @param {object} spec 契约参数条目。
 * @returns {string} 简洁约束摘要。
 */
export function summarizeConstraints(spec) {
  const parts = [];
  if (Array.isArray(spec.enum) && spec.enum.length > 0) {
    parts.push(`枚举 ${spec.enum.length} 项`);
  }
  if (spec.minLength !== undefined || spec.maxLength !== undefined) {
    parts.push(`长度 ${spec.minLength ?? 0}..${spec.maxLength ?? "∞"}`);
  }
  if (spec.minimum !== undefined || spec.maximum !== undefined) {
    parts.push(`范围 ${spec.minimum ?? "-∞"}..${spec.maximum ?? "∞"}`);
  }
  if (spec.value_template) {
    parts.push(`模板 ${spec.value_template}`);
  }
  return parts.join(" · ");
}

function collectNamedEntries(rows, kindLabel, buildSpec, errors) {
  const entries = {};
  for (const [index, row] of (rows ?? []).entries()) {
    const key = String(row.key ?? "").trim();
    if (!key) {
      errors.push(`第 ${index + 1} 个${kindLabel}未填写名称`);
      continue;
    }
    if (Object.hasOwn(entries, key)) {
      errors.push(`${kindLabel}名「${key}」重复`);
      continue;
    }
    entries[key] = buildSpec(row);
  }
  return entries;
}

function defaultOutputs(nodes) {
  const list = Array.isArray(nodes) ? nodes : [];
  const saver = list.find((node) => OUTPUT_NODE_DEFAULTS[node?.class_type]);
  if (saver) {
    const preset = OUTPUT_NODE_DEFAULTS[saver.class_type];
    return {
      [preset.key]: {
        type: preset.type,
        target: { node: saver.id, field: preset.field },
      },
    };
  }
  const first = list[0];
  return {
    output: {
      type: DEFAULT_OUTPUT_TYPE,
      target: { node: first ? String(first.id) : "", field: "" },
    },
  };
}

function inputRows(inputs) {
  if (!inputs || typeof inputs !== "object") return [];
  return Object.entries(inputs).map(([key, spec]) => {
    const target = spec?.target ?? {};
    return {
      key,
      type: spec?.type ?? DEFAULT_INPUT_TYPE,
      description: spec?.description ?? "",
      visible: spec?.visible ?? true,
      required: Boolean(spec?.required),
      node: target.node ?? "",
      field: target.field ?? "",
      enumText: Array.isArray(spec?.enum) ? spec.enum.map(String).join("\n") : "",
      minLength: editableLimit(spec?.minLength),
      maxLength: editableLimit(spec?.maxLength),
      minimum: editableLimit(spec?.minimum),
      maximum: editableLimit(spec?.maximum),
      valueTemplate: spec?.value_template ?? "",
    };
  });
}

function outputRows(outputs) {
  if (!outputs || typeof outputs !== "object") return [];
  return Object.entries(outputs).map(([key, spec]) => {
    const target = spec?.target ?? {};
    return {
      key,
      type: spec?.type ?? DEFAULT_OUTPUT_TYPE,
      node: target.node ?? "",
      field: target.field ?? "",
    };
  });
}

function editableLimit(value) {
  return value === undefined || value === null ? "" : String(value);
}

function inputSpec(row) {
  const type = row.type ?? DEFAULT_INPUT_TYPE;
  const spec = {
    type,
    description: String(row.description ?? "").trim(),
  };
  if (!row.visible) spec.visible = false;
  if (row.required) spec.required = true;
  if (ENUM_TYPES.has(type)) {
    const values = parseEnumText(row.enumText, type);
    if (values.length) spec.enum = values;
  }
  if (TEXT_TYPES.has(type)) {
    assignLimit(spec, "minLength", row.minLength);
    assignLimit(spec, "maxLength", row.maxLength);
    const valueTemplate = String(row.valueTemplate ?? "").trim();
    if (!row.visible && valueTemplate) spec.value_template = valueTemplate;
  }
  if (NUMBER_TYPES.has(type)) {
    assignLimit(spec, "minimum", row.minimum);
    assignLimit(spec, "maximum", row.maximum);
  }
  spec.target = targetOf(row);
  return spec;
}

function targetOf(row) {
  return {
    node: String(row.node ?? "").trim(),
    field: String(row.field ?? "").trim(),
  };
}

function parseEnumText(text, type) {
  return String(text ?? "")
    .split(/[\n,]+/)
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => castEnumValue(part, type));
}

function castEnumValue(text, type) {
  if (!NUMBER_TYPES.has(type)) return text;
  const value = Number(text);
  return Number.isFinite(value) ? value : text;
}

function assignLimit(spec, name, raw) {
  const text = String(raw ?? "").trim();
  if (!text) return;
  const value = Number(text);
  spec[name] = Number.isFinite(value) ? value : text;
}
