/**
 * 工作流视图的表单状态转换。
 *
 * 这里只放纯函数：工具契约与工作流节点换成可编辑草稿，草稿再还原成契约。
 * 契约的权威格式见 schemas/tool-contract-v1.schema.json。
 */

const TOOL_NAME_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const TOOL_NAME_MAX_LENGTH = 64;
const TOOL_NAME_PREFIX = "comfy_";
const DEFAULT_INPUT_TYPE = "string";
const DEFAULT_OUTPUT_TYPE = "image";
const ENUM_TYPES = new Set(["string", "integer", "number"]);
const TEXT_TYPES = new Set(["string"]);
const NUMBER_TYPES = new Set(["integer", "number"]);
const OUTPUT_NODE_DEFAULTS = {
  SaveImage: { type: "image", field: "images" },
  SaveAnimatedWEBP: { type: "image", field: "images" },
  SaveVideo: { type: "video", field: "images" },
  VHS_VideoCombine: { type: "video", field: "gifs" },
  SaveAudio: { type: "audio", field: "audio" },
  SaveAudioMP3: { type: "audio", field: "audio" },
  SaveAudioFLAC: { type: "audio", field: "audio" },
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
  return TOOL_NAME_PATTERN.test(clipped) ? clipped : TOOL_NAME_PREFIX.replace(/_$/, "");
}

/**
 * 为没有配套契约的工作流生成初始契约。
 *
 * 返回值是契约本身的形状（`inputs` 是对象、`outputs` 是对象），可以直接交给
 * {@link contractToDraft} 转成表单草稿。产物映射从 SaveImage 一类的输出节点预填，
 * 找不到输出节点时取第一个节点，节点都没有就留一条待填的空映射。
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
    enabled: false,
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
    version: source.version ?? 1,
    name: source.name ?? suggestToolName(stem),
    description: source.description ?? "",
    enabled: Boolean(source.enabled),
    inputs: inputRows(source.inputs),
    outputs: outputRows(source.outputs),
  };
}

/**
 * 把表单草稿还原成可提交的契约。
 *
 * 只写 Schema 有意义的字段：可见与必填只在偏离默认值时出现，长度与范围只给
 * 允许它们的类型，枚举按行或逗号拆成对应类型的数组。没填名字的行不写入。
 *
 * @param {object} draft 表单草稿。
 * @returns {object} 符合契约 Schema 的对象。
 */
export function draftToContract(draft) {
  const inputs = {};
  for (const row of draft.inputs ?? []) {
    const key = String(row.key ?? "").trim();
    if (!key) continue;
    inputs[key] = inputSpec(row);
  }
  const outputs = {};
  for (const row of draft.outputs ?? []) {
    const key = String(row.key ?? "").trim();
    if (!key) continue;
    outputs[key] = {
      type: row.type ?? DEFAULT_OUTPUT_TYPE,
      target: targetOf(row),
    };
  }
  return {
    version: 1,
    name: String(draft.name ?? "").trim(),
    description: String(draft.description ?? "").trim(),
    enabled: Boolean(draft.enabled),
    inputs,
    outputs,
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
 * 按输出节点类型预填一条产物映射。
 *
 * @param {Array<object>} nodes 后端给出的节点列表。
 * @returns {object} 产物名到产物映射的对象。
 */
function defaultOutputs(nodes) {
  const list = Array.isArray(nodes) ? nodes : [];
  const saver = list.find((node) => OUTPUT_NODE_DEFAULTS[node?.class_type]);
  if (saver) {
    const preset = OUTPUT_NODE_DEFAULTS[saver.class_type];
    return {
      [preset.type]: {
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

/**
 * 把契约里的参数对象摊成表单行。
 *
 * @param {object|null} inputs 契约的 `inputs`。
 * @returns {Array<object>} 参数行数组。
 */
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

/**
 * 把契约里的产物对象摊成表单行。
 *
 * @param {object|null} outputs 契约的 `outputs`。
 * @returns {Array<object>} 产物行数组。
 */
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

/**
 * 把可选的数值限制转成输入框里的文本。
 *
 * @param {number|undefined|null} value 契约里的限制值。
 * @returns {string} 限值，未设置时为空串。
 */
function editableLimit(value) {
  return value === undefined || value === null ? "" : String(value);
}

/**
 * 由一行表单生成一个参数映射。
 *
 * @param {object} row 参数行。
 * @returns {object} 契约里的 `inputs` 条目。
 */
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

/**
 * 取一行表单里的节点与字段映射。
 *
 * @param {object} row 参数行或产物行。
 * @returns {{node: string, field: string}} 映射目标。
 */
function targetOf(row) {
  return {
    node: String(row.node ?? "").trim(),
    field: String(row.field ?? "").trim(),
  };
}

/**
 * 把枚举输入框的文本拆成对应类型的数组。
 *
 * @param {string} text 按行或逗号分隔的枚举文本。
 * @param {string} type 参数类型。
 * @returns {Array<string|number>} 枚举值。
 */
function parseEnumText(text, type) {
  return String(text ?? "")
    .split(/[\n,]+/)
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => castEnumValue(part, type));
}

/**
 * 把枚举文本转成参数类型对应的值。
 *
 * @param {string} text 单个枚举值文本。
 * @param {string} type 参数类型。
 * @returns {string|number} 数值类型解析成数字，解析不了就保留原文等后端报错。
 */
function castEnumValue(text, type) {
  if (!NUMBER_TYPES.has(type)) return text;
  const value = Number(text);
  return Number.isFinite(value) ? value : text;
}

/**
 * 写入一个可选的数值限制，空值不写。
 *
 * @param {object} spec 待写入的契约条目。
 * @param {string} name 限制字段名。
 * @param {string|number} raw 表单里的文本。
 */
function assignLimit(spec, name, raw) {
  const text = String(raw ?? "").trim();
  if (!text) return;
  const value = Number(text);
  spec[name] = Number.isFinite(value) ? value : text;
}
