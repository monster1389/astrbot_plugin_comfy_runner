"""工具契约：格式校验、语义校验与模型参数 Schema 生成。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

CONTRACT_SUFFIX = ".tool.json"
WORKFLOW_SUFFIX = ".json"
SCHEMA_RELATIVE_PATH = Path("schemas") / "tool-contract-v1.schema.json"
JSON_TYPES = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "image": "string",
    "video": "string",
    "audio": "string",
    "file": "string",
}
TEXT_OUTPUT_TYPE = "text"
FILE_OUTPUT_TYPES = frozenset({"image", "video", "audio", "file"})
REFERENCE_HINT = (
    "取值可以是 attachment 或 attachment:N"
    "（该类型第 N 个附件，本条消息的排在引用消息前面），"
    "也可以是本地绝对路径、file:// URI 或 http(s) 链接"
)
NUMERIC_MARKERS = frozenset({"INT", "FLOAT", "BOOLEAN"})
DECLARED_MARKERS = NUMERIC_MARKERS | {"STRING", "COMBO"}


class ContractError(ValueError):
    """工具契约不合法，消息面向管理员。"""


@dataclass(frozen=True, slots=True)
class ParameterMapping:
    """一个参数（可见或隐藏）到工作流节点输入字段的映射。"""

    name: str
    type: str
    description: str
    required: bool
    visible: bool
    node: str
    field: str
    enum: tuple[Any, ...] = ()
    min_length: int | None = None
    max_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None

    @property
    def target(self) -> str:
        """参数映射目标的显示写法。"""
        return f"{self.node}.{self.field}"

    @property
    def is_reference(self) -> bool:
        """是否是需要上传附件的文件类参数。"""
        return self.type in FILE_OUTPUT_TYPES


@dataclass(frozen=True, slots=True)
class OutputMapping:
    """一个正式产物到工作流输出字段的映射。"""

    name: str
    type: str
    node: str
    field: str

    @property
    def target(self) -> str:
        """输出映射目标的显示写法。"""
        return f"{self.node}.{self.field}"


@dataclass(frozen=True, slots=True)
class ToolContract:
    """一个工作流工具的完整契约。"""

    path: Path
    version: int
    name: str
    description: str
    enabled: bool
    inputs: dict[str, ParameterMapping]
    outputs: dict[str, OutputMapping]

    @property
    def workflow_path(self) -> Path:
        """配对的工作流文件路径。"""
        stem = self.path.name[: -len(CONTRACT_SUFFIX)]
        return self.path.with_name(f"{stem}{WORKFLOW_SUFFIX}")


def load_contract_schema(plugin_dir: Path) -> dict[str, Any]:
    """读取契约 Schema 文件。

    Args:
        plugin_dir: 插件根目录。

    Returns:
        Schema 字典。

    Raises:
        ContractError: 文件缺失或不是合法 JSON 对象。
    """
    path = plugin_dir / SCHEMA_RELATIVE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContractError(f"读取契约 Schema 失败: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"契约 Schema 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(f"契约 Schema 顶层必须是对象: {path}")
    return data


def load_contract(path: Path, schema: dict[str, Any]) -> ToolContract:
    """读取一个契约文件并做单文件 Schema 校验。

    Args:
        path: 契约文件路径。
        schema: 契约 Schema。

    Returns:
        契约对象。

    Raises:
        ContractError: 文件不可读、不是合法 JSON 对象或不符合 Schema。
    """
    label = path.name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContractError(f"{label}: 无法读取文件: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"{label}: 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(f"{label}: 顶层必须是对象")

    errors = sorted(
        Draft202012Validator(schema).iter_errors(data),
        key=lambda error: str(error.json_path),
    )
    if errors:
        detail = "\n".join(
            f"{label}: {_schema_error_location(error)} {error.message}"
            for error in errors
        )
        raise ContractError(detail)

    return _build_contract(path, data)


def read_workflow(path: Path) -> dict[str, Any]:
    """读取工作流文件。

    Args:
        path: 工作流文件路径。

    Returns:
        工作流图。

    Raises:
        ContractError: 文件缺失、不可读或不是 JSON 对象。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"缺少配对工作流 {path.name}") from exc
    except OSError as exc:
        raise ContractError(f"无法读取工作流 {path.name}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"工作流 {path.name} 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(f"工作流 {path.name} 顶层必须是对象")
    return data


def validate_contract(contract: ToolContract) -> list[str]:
    """检查单文件 Schema 之外的语义约束。

    Args:
        contract: 已通过单文件 Schema 校验的契约。

    Returns:
        面向管理员的问题描述；为空表示契约可用。
    """
    label = contract.path.name
    errors = _limit_errors(contract, label)
    errors.extend(_visibility_errors(contract, label))

    try:
        graph = read_workflow(contract.workflow_path)
    except ContractError as exc:
        return errors + [f"{label}: {exc}"]

    errors.extend(input_target_errors(contract, graph, label))
    errors.extend(_output_target_errors(contract, graph, label))
    errors.extend(_optional_value_errors(contract, graph, label))
    return errors


def remote_target_errors(
    contract: ToolContract,
    graph: dict[str, Any],
    node_defs: dict[str, dict[str, Any]],
    failures: dict[str, str],
) -> list[str]:
    """用 ComfyUI 节点定义核对参数与产物映射的目标字段。

    Args:
        contract: 工具契约。
        graph: 配对的工作流图。
        node_defs: 取到定义的节点类型到 ``/object_info`` 条目的映射。
        failures: 取不到定义的节点类型到失败原因的映射。

    Returns:
        面向管理员的问题描述；为空表示远端核对通过。
    """
    label = contract.path.name
    errors: list[str] = []
    for parameter in contract.inputs.values():
        class_type, definition, problem = _remote_definition(
            graph,
            parameter.node,
            node_defs,
            failures,
        )
        if problem is not None:
            errors.append(f"{label}: 参数 {parameter.name} {problem}")
            continue
        if parameter.field not in _declared_fields(definition):
            errors.append(
                f"{label}: 参数 {parameter.name} 的字段 {parameter.target} "
                f"不在节点类型 {class_type} 的输入中"
            )
            continue
        marker = _scalar_marker(_field_spec(definition, parameter.field))
        if parameter.is_reference and marker in NUMERIC_MARKERS:
            errors.append(
                f"{label}: 参数 {parameter.name} 是文件类参数，"
                f"但目标字段 {parameter.target} 的值类型是 {marker}"
            )

    for output in contract.outputs.values():
        class_type, definition, problem = _remote_definition(
            graph,
            output.node,
            node_defs,
            failures,
        )
        if problem is not None:
            errors.append(f"{label}: 产物 {output.name} {problem}")
            continue
        if definition.get("output_node") is not True:
            errors.append(
                f"{label}: 产物 {output.name} 的节点类型 {class_type} 不是输出节点"
            )
    return errors


def build_parameter_schema(
    contract: ToolContract,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成给模型看的参数 Schema。

    Args:
        contract: 工具契约。
        defaults: 参数名到工作流原值的映射；只对这里的参数写 ``default``。

    Returns:
        根对象为 ``type: object`` 且带 ``additionalProperties: false`` 的 Schema；
        只含模型可见的参数。
    """
    shown = defaults or {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name in sorted(contract.inputs):
        parameter = contract.inputs[name]
        if not parameter.visible:
            continue
        schema = _parameter_schema(parameter)
        if name in shown:
            schema["default"] = shown[name]
        properties[name] = schema
        if parameter.required:
            required.append(name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def contract_view(
    contract: ToolContract,
    graph: dict[str, Any],
) -> dict[str, Any]:
    """生成给模型看的契约视图。

    Args:
        contract: 工具契约。
        graph: 配对的工作流图，提供可选参数的工作流原值。

    Returns:
        只含名称、说明、参数 Schema 与产物类型的字典。
    """
    return {
        "name": contract.name,
        "description": contract.description,
        "parameters": build_parameter_schema(
            contract,
            parameter_defaults(contract, graph),
        ),
        "outputs": {
            name: {"type": contract.outputs[name].type}
            for name in sorted(contract.outputs)
        },
    }


def parameter_defaults(
    contract: ToolContract,
    graph: dict[str, Any],
) -> dict[str, Any]:
    """取出可省略参数在工作流里的原值。

    必填参数不会被省略，文件类参数的原理值是 ComfyUI input 目录里的文件名，
    两者都不展示；目标是连线的字段没有值可展示。

    Args:
        contract: 工具契约。
        graph: 配对的工作流图。

    Returns:
        可省略且能展示原值的参数名到工作流原值的映射。

    Raises:
        ContractError: 参数的目标节点或字段在本次读取的工作流里不存在。
    """
    label = contract.path.name
    defaults: dict[str, Any] = {}
    for name in sorted(contract.inputs):
        parameter = contract.inputs[name]
        if parameter.required or not parameter.visible or parameter.is_reference:
            continue
        node = graph.get(parameter.node)
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise ContractError(
                f"{label}: 参数 {name} 的目标节点 {parameter.node} 不存在"
            )
        inputs = node["inputs"]
        if parameter.field not in inputs:
            raise ContractError(
                f"{label}: 参数 {name} 的目标字段 {parameter.target} 不存在"
            )
        value = inputs[parameter.field]
        if _is_link(value):
            continue
        defaults[name] = value
    return defaults


def validate_arguments(contract: ToolContract, arguments: dict[str, Any]) -> None:
    """按生成的参数 Schema 校验一次调用的实参。

    Args:
        contract: 工具契约。
        arguments: 模型给出的参数。

    Raises:
        ContractError: 缺少必填参数、出现未声明参数或取值不合法。
    """
    validator = Draft202012Validator(build_parameter_schema(contract))
    problems = [
        _argument_error(error, contract) for error in validator.iter_errors(arguments)
    ]
    if problems:
        raise ContractError("\n".join(problems))


def _build_contract(path: Path, data: dict[str, Any]) -> ToolContract:
    inputs = {
        name: ParameterMapping(
            name=name,
            type=spec["type"],
            description=spec["description"],
            required=bool(spec.get("required", False)),
            visible=bool(spec.get("visible", True)),
            node=str(spec["target"]["node"]),
            field=str(spec["target"]["field"]),
            enum=tuple(spec.get("enum", ())),
            min_length=spec.get("minLength"),
            max_length=spec.get("maxLength"),
            minimum=spec.get("minimum"),
            maximum=spec.get("maximum"),
        )
        for name, spec in data["inputs"].items()
    }
    outputs = {
        name: OutputMapping(
            name=name,
            type=spec["type"],
            node=str(spec["target"]["node"]),
            field=str(spec["target"]["field"]),
        )
        for name, spec in data["outputs"].items()
    }
    return ToolContract(
        path=path,
        version=data["version"],
        name=data["name"],
        description=data["description"],
        enabled=data["enabled"],
        inputs=inputs,
        outputs=outputs,
    )


def _schema_error_location(error: ValidationError) -> str:
    location = str(error.json_path)
    if location in ("", "$"):
        return "顶层"
    return location[2:] if location.startswith("$.") else location


def _argument_error(error: ValidationError, contract: ToolContract) -> str:
    if error.validator == "required":
        missing = [name for name in error.validator_value if name not in error.instance]
        detail = f"缺少必填参数: {', '.join(missing)}"
        hints = [name for name in missing if contract.inputs[name].is_reference]
        if hints:
            detail = f"{detail}（{', '.join(hints)} 的 {REFERENCE_HINT}）"
        return detail
    if error.validator == "additionalProperties":
        allowed = set(error.schema.get("properties", {}))
        extra = [name for name in error.instance if name not in allowed]
        return f"出现未声明参数: {', '.join(extra)}"
    name = next(iter(error.absolute_path), "")
    return f"参数 {name} 取值不合法: {error.message}"


def _limit_errors(contract: ToolContract, label: str) -> list[str]:
    errors: list[str] = []
    for name in sorted(contract.inputs):
        parameter = contract.inputs[name]
        if (
            parameter.min_length is not None
            and parameter.max_length is not None
            and parameter.max_length < parameter.min_length
        ):
            errors.append(f"{label}: 参数 {name} 的 maxLength 小于 minLength")
        if (
            parameter.minimum is not None
            and parameter.maximum is not None
            and parameter.maximum < parameter.minimum
        ):
            errors.append(f"{label}: 参数 {name} 的 maximum 小于 minimum")
        errors.extend(_enum_errors(parameter, label))
    return errors


def _enum_errors(parameter: ParameterMapping, label: str) -> list[str]:
    errors: list[str] = []
    for value in parameter.enum:
        if parameter.type == "string" and isinstance(value, str):
            too_short = (
                parameter.min_length is not None and len(value) < parameter.min_length
            )
            too_long = (
                parameter.max_length is not None and len(value) > parameter.max_length
            )
            if too_short or too_long:
                errors.append(
                    f"{label}: 参数 {parameter.name} 的枚举值 {value} 超出长度范围"
                )
        elif parameter.type in ("integer", "number") and isinstance(value, int | float):
            too_small = parameter.minimum is not None and value < parameter.minimum
            too_large = parameter.maximum is not None and value > parameter.maximum
            if too_small or too_large:
                errors.append(
                    f"{label}: 参数 {parameter.name} 的枚举值 {value} 超出取值范围"
                )
    return errors


def input_target_errors(
    contract: ToolContract,
    graph: dict[str, Any],
    label: str,
) -> list[str]:
    """检查参数映射在当前工作流里是否成立。

    目标字段可以是连线：提供值就替成常量，省略就保持连线；但必填参数不能
    落在连线上，因为没有原值可回退。

    Args:
        contract: 工具契约。
        graph: 工作流图。
        label: 出现在错误里的文件名。

    Returns:
        面向管理员的问题描述；为空表示映射成立。
    """
    errors: list[str] = []
    for name in sorted(contract.inputs):
        parameter = contract.inputs[name]
        node = graph.get(parameter.node)
        if node is None:
            errors.append(f"{label}: 参数 {name} 的目标节点 {parameter.node} 不存在")
            continue
        if not isinstance(node, dict):
            errors.append(
                f"{label}: 参数 {name} 的目标节点 {parameter.node} 不是节点对象"
            )
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            errors.append(
                f"{label}: 参数 {name} 的目标节点 {parameter.node} 没有 inputs 对象"
            )
            continue
        if parameter.field not in inputs:
            errors.append(f"{label}: 参数 {name} 的目标字段 {parameter.target} 不存在")
            continue
        if _is_link(inputs[parameter.field]) and parameter.required:
            errors.append(
                f"{label}: 参数 {name} 是必填参数，但目标字段 {parameter.target} "
                "是连线，省略时没有原值可用"
            )
    return errors


def _output_target_errors(
    contract: ToolContract,
    graph: dict[str, Any],
    label: str,
) -> list[str]:
    errors: list[str] = []
    for name in sorted(contract.outputs):
        output = contract.outputs[name]
        if not isinstance(graph.get(output.node), dict):
            errors.append(f"{label}: 产物 {name} 的输出节点 {output.node} 不存在")
    return errors


def _optional_value_errors(
    contract: ToolContract,
    graph: dict[str, Any],
    label: str,
) -> list[str]:
    errors: list[str] = []
    for name in sorted(contract.inputs):
        parameter = contract.inputs[name]
        if parameter.required:
            continue
        node = graph.get(parameter.node)
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        inputs = node["inputs"]
        if parameter.field not in inputs:
            continue
        value = inputs[parameter.field]
        if _is_link(value):
            continue
        validator = Draft202012Validator(_parameter_schema(parameter))
        for error in validator.iter_errors(value):
            errors.append(
                f"{label}: 参数 {name} 的工作流原值不满足契约: {error.message}"
            )
    return errors


def _visibility_errors(contract: ToolContract, label: str) -> list[str]:
    errors: list[str] = []
    for name in sorted(contract.inputs):
        parameter = contract.inputs[name]
        if not parameter.visible and parameter.required:
            errors.append(
                f"{label}: 参数 {name} 对模型不可见，不能声明为必填"
            )
    return errors


def _parameter_schema(parameter: ParameterMapping) -> dict[str, Any]:
    description = parameter.description
    if parameter.is_reference:
        description = f"{description}。{REFERENCE_HINT}"
    schema: dict[str, Any] = {
        "type": JSON_TYPES[parameter.type],
        "description": description,
    }
    if parameter.enum:
        schema["enum"] = list(parameter.enum)
    if parameter.min_length is not None:
        schema["minLength"] = parameter.min_length
    if parameter.max_length is not None:
        schema["maxLength"] = parameter.max_length
    if parameter.minimum is not None:
        schema["minimum"] = parameter.minimum
    if parameter.maximum is not None:
        schema["maximum"] = parameter.maximum
    return schema


def _remote_definition(
    graph: dict[str, Any],
    node_id: str,
    node_defs: dict[str, dict[str, Any]],
    failures: dict[str, str],
) -> tuple[str, dict[str, Any], str | None]:
    node = graph.get(node_id)
    if not isinstance(node, dict):
        return "", {}, f"的目标节点 {node_id} 不是节点对象"
    class_type = str(node.get("class_type"))
    problem = failures.get(class_type)
    if problem is not None:
        return class_type, {}, f"的节点类型 {class_type} 无法核对: {problem}"
    definition = node_defs.get(class_type)
    if definition is None:
        return class_type, {}, f"的节点类型 {class_type} 不存在"
    return class_type, definition, None


def _declared_fields(definition: dict[str, Any]) -> set[str]:
    declared: set[str] = set()
    for section in ("required", "optional"):
        fields = definition.get("input", {}).get(section, {})
        if isinstance(fields, dict):
            declared.update(str(name) for name in fields)
    return declared


def _field_spec(definition: dict[str, Any], field: str) -> Any:
    for section in ("required", "optional"):
        fields = definition.get("input", {}).get(section, {})
        if isinstance(fields, dict) and field in fields:
            return fields[field]
    return None


def _scalar_marker(spec: Any) -> str | None:
    if not isinstance(spec, list) or not spec:
        return None
    head = spec[0]
    return head if isinstance(head, str) and head in DECLARED_MARKERS else None


def _is_link(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )
