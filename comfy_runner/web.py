"""插件视图的 Web API：浏览工作流、编辑契约、上传与删除文件。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.web import JSONResponse, error_response, json_response, request

from .comfy import ComfyError
from .contract import (
    CONTRACT_SUFFIX,
    WORKFLOW_SUFFIX,
    ContractError,
    ToolContract,
    contract_view,
    graph_target_errors,
    is_link_value,
    load_contract,
    load_contract_schema,
    parse_contract_data,
    read_workflow,
    remote_target_errors,
    validate_contract,
    validate_workflow_data,
)
from .paths import PLUGIN_DIR, is_aux_file
from .registry import ContractReport, discover_contracts, inspect_contracts

if TYPE_CHECKING:
    from ..main import ComfyUIPlugin

PLUGIN_NAME = "astrbot_plugin_comfy_runner"
BASE_ROUTE = f"/{PLUGIN_NAME}"


class WebController:
    """插件视图的 Web API 入口。"""

    def __init__(self, plugin: ComfyUIPlugin) -> None:
        """记录 Web API 需要的插件经营状态。

        Args:
            plugin: 已装配的插件。
        """
        self.plugin = plugin

    def register(self, context: Context) -> None:
        """把固定路由注册到 AstrBot 的插件 Web API。

        Args:
            context: 插件上下文。
        """
        routes = (
            (
                f"{BASE_ROUTE}/workflows",
                self.list_workflows,
                ["GET"],
                "列出工作流与工具契约",
            ),
            (
                f"{BASE_ROUTE}/workflow",
                self.workflow_detail,
                ["GET"],
                "读取一个工作流的节点与契约",
            ),
            (
                f"{BASE_ROUTE}/workflow/upload",
                self.upload_workflow,
                ["POST"],
                "上传工作流文件",
            ),
            (
                f"{BASE_ROUTE}/workflow/contract",
                self.save_contract,
                ["POST"],
                "保存工具契约",
            ),
            (
                f"{BASE_ROUTE}/workflow/preview",
                self.preview_contract,
                ["POST"],
                "预览工具契约",
            ),
            (
                f"{BASE_ROUTE}/workflow/delete",
                self.delete_workflow,
                ["POST"],
                "删除工作流或工具契约",
            ),
        )
        for route, handler, methods, desc in routes:
            context.register_web_api(route, handler, methods, desc)

    async def list_workflows(self) -> JSONResponse:
        """列出工作流目录里的全部工作流与契约。

        Returns:
            含目录、启用工具与每个工作流状态的响应。
        """
        workflow_dir = self.plugin.cfg.workflow_dir
        reports = _reports(workflow_dir)
        remote = _remote_flag(request.query.get("remote"))
        cache: dict[str, dict[str, Any] | str] = {}
        items = [
            await self._item(stem, reports, remote, cache)
            for stem in _stems(workflow_dir)
        ]
        return json_response(
            {
                "workflow_dir": str(workflow_dir),
                "active_tools": _active_tools(self.plugin.contracts),
                "items": items,
            }
        )

    async def workflow_detail(self) -> JSONResponse:
        """读取一个工作流的节点、契约与模型视图。

        Returns:
            工作流详情；名称非法时 400，文件都不存在时 404。
        """
        stem = request.query.get("stem", "").strip()
        if not _is_safe_stem(stem):
            return error_response("工作流名称不合法", status_code=400)
        workflow_dir = self.plugin.cfg.workflow_dir
        has_workflow = (workflow_dir / f"{stem}{WORKFLOW_SUFFIX}").is_file()
        has_contract = (workflow_dir / f"{stem}{CONTRACT_SUFFIX}").is_file()
        if not has_workflow and not has_contract:
            return error_response(f"工作流 {stem} 不存在", status_code=404)
        remote = _remote_flag(request.query.get("remote"))
        return json_response(await self._detail(stem, remote=remote))

    async def upload_workflow(self) -> JSONResponse:
        """上传一份工作流文件，校验通过后写入并热刷新契约。

        Returns:
            写入后的工作流详情；文件缺失、格式不符或会破坏已启用契约时返回 400。
        """
        upload = (await request.files()).get("file")
        if upload is None:
            return error_response("未收到上传的工作流文件", status_code=400)
        filename = upload.filename or ""
        if not filename.endswith(WORKFLOW_SUFFIX):
            return error_response("工作流文件必须以 .json 结尾", status_code=400)
        if is_aux_file(filename):
            return error_response("不能上传工具契约等附属文件", status_code=400)
        stem = filename[: -len(WORKFLOW_SUFFIX)]
        if not _is_safe_stem(stem):
            return error_response("工作流名称不合法", status_code=400)

        content = await upload.read()
        try:
            data = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return error_response(f"工作流不是合法 UTF-8 JSON: {exc}", status_code=400)
        try:
            validate_workflow_data(data, filename)
        except ContractError as exc:
            return error_response(str(exc), status_code=400)

        workflow_dir = self.plugin.cfg.workflow_dir
        contract_path = workflow_dir / f"{stem}{CONTRACT_SUFFIX}"
        if contract_path.is_file():
            refusal = _upload_conflict(contract_path, data)
            if refusal is not None:
                return error_response(refusal, status_code=400)

        workflow_path = workflow_dir / f"{stem}{WORKFLOW_SUFFIX}"
        workflow_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.plugin.reload_contracts()
        return json_response(await self._detail(stem, remote=False))

    async def save_contract(self) -> JSONResponse:
        """保存一份工具契约，热刷新成功后返回该工作流的最新详情。

        Returns:
            保存后的工作流详情；校验失败时 400 且不写盘、不改内存。
        """
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        stem = payload.get("stem")
        contract_data = payload.get("contract")
        if not isinstance(stem, str) or not _is_safe_stem(stem):
            return error_response("工作流名称不合法", status_code=400)
        if not isinstance(contract_data, dict):
            return error_response("工具契约必须是 JSON 对象", status_code=400)

        workflow_dir = self.plugin.cfg.workflow_dir
        workflow_path = workflow_dir / f"{stem}{WORKFLOW_SUFFIX}"
        if not workflow_path.is_file():
            return error_response(
                f"工作流 {workflow_path.name} 不存在",
                status_code=404,
            )
        contract_path = workflow_dir / f"{stem}{CONTRACT_SUFFIX}"
        schema = load_contract_schema(PLUGIN_DIR)
        try:
            contract = parse_contract_data(contract_path, contract_data, schema)
        except ContractError as exc:
            return error_response(str(exc), status_code=400)

        if contract.enabled:
            problems = validate_contract(contract)
            problems.extend(
                _duplicate_name_errors(workflow_dir, contract_path, contract, schema)
            )
            if problems:
                return error_response("\n".join(problems), status_code=400)

        serialized = json.dumps(contract_data, ensure_ascii=False, indent=2) + "\n"
        previous = (
            contract_path.read_text(encoding="utf-8")
            if contract_path.is_file()
            else None
        )
        contract_path.write_text(serialized, encoding="utf-8")
        try:
            self.plugin.reload_contracts()
        except ContractError as exc:
            _restore(contract_path, previous)
            return error_response(str(exc), status_code=400)
        return json_response(await self._detail(stem, remote=False))

    async def preview_contract(self) -> JSONResponse:
        """在不写盘的情况下校验一份契约并生成模型视图。

        Returns:
            含校验结果与模型视图的响应。
        """
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        stem = payload.get("stem")
        contract_data = payload.get("contract")
        if not isinstance(stem, str) or not _is_safe_stem(stem):
            return error_response("工作流名称不合法", status_code=400)
        if not isinstance(contract_data, dict):
            return error_response("工具契约必须是 JSON 对象", status_code=400)

        workflow_dir = self.plugin.cfg.workflow_dir
        contract_path = workflow_dir / f"{stem}{CONTRACT_SUFFIX}"
        schema = load_contract_schema(PLUGIN_DIR)
        try:
            contract = parse_contract_data(contract_path, contract_data, schema)
        except ContractError as exc:
            return json_response(
                {
                    "ok": False,
                    "errors": str(exc).splitlines(),
                    "inspect_preview": None,
                }
            )

        errors = validate_contract(contract)
        errors.extend(
            _duplicate_name_errors(workflow_dir, contract_path, contract, schema)
        )
        graph: dict[str, Any] = {}
        try:
            graph = read_workflow(contract.workflow_path)
        except ContractError:
            graph = {}
        if bool(payload.get("remote")) and graph:
            node_defs, failures = await self._node_definitions(
                _class_types(graph, _contract_node_ids(contract)),
                {},
            )
            errors.extend(remote_target_errors(contract, graph, node_defs, failures))

        preview = contract_view(contract, graph) if not errors and graph else None
        return json_response(
            {
                "ok": not errors,
                "errors": errors,
                "inspect_preview": preview,
            }
        )

    async def delete_workflow(self) -> JSONResponse:
        """删除工具契约，或连同工作流文件一起删除。

        Returns:
            含删除范围、工作流与启用工具的响应。
        """
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        stem = payload.get("stem")
        scope = payload.get("scope")
        if not isinstance(stem, str) or not _is_safe_stem(stem):
            return error_response("工作流名称不合法", status_code=400)

        workflow_dir = self.plugin.cfg.workflow_dir
        workflow_path = workflow_dir / f"{stem}{WORKFLOW_SUFFIX}"
        contract_path = workflow_dir / f"{stem}{CONTRACT_SUFFIX}"

        if scope == "contract":
            if not contract_path.is_file():
                return error_response(
                    f"工具契约 {contract_path.name} 不存在",
                    status_code=404,
                )
            contract_path.unlink()
            self.plugin.reload_contracts()
            return json_response(
                {
                    "deleted": "contract",
                    "stem": stem,
                    "active_tools": _active_tools(self.plugin.contracts),
                }
            )

        if scope == "all":
            if contract_path.is_file() and not _is_disabled(contract_path):
                return error_response(
                    "该工作流关联的工具契约仍处于启用状态，"
                    "请先停用或仅删除工具契约后再删除工作流文件",
                    status_code=400,
                )
            if not contract_path.is_file() and not workflow_path.is_file():
                return error_response(f"工作流 {stem} 不存在", status_code=404)
            contract_path.unlink(missing_ok=True)
            workflow_path.unlink(missing_ok=True)
            self.plugin.reload_contracts()
            return json_response(
                {
                    "deleted": "all",
                    "stem": stem,
                    "active_tools": _active_tools(self.plugin.contracts),
                }
            )

        return error_response("删除范围只能是 contract 或 all", status_code=400)

    async def _detail(self, stem: str, *, remote: bool) -> dict[str, Any]:
        """汇总一个工作流的节点、契约、校验结果与模型视图。

        Args:
            stem: 工作流文件名去掉 ``.json`` 的部分。
            remote: 是否用 ComfyUI 节点定义核对。

        Returns:
            工作流详情字典。
        """
        workflow_dir = self.plugin.cfg.workflow_dir
        workflow_path = workflow_dir / f"{stem}{WORKFLOW_SUFFIX}"
        contract_path = workflow_dir / f"{stem}{CONTRACT_SUFFIX}"
        has_workflow = workflow_path.is_file()
        has_contract = contract_path.is_file()

        report = _reports(workflow_dir).get(contract_path)
        contract = report.contract if report is not None else None
        errors = list(report.errors) if report is not None else []

        graph: dict[str, Any] = {}
        nodes: list[dict[str, Any]] = []
        if has_workflow:
            try:
                graph = read_workflow(workflow_path)
            except ContractError as exc:
                if contract is None:
                    errors.append(str(exc))
            else:
                nodes = _nodes(graph)

        if remote and graph:
            await self._attach_remote(nodes, graph, contract, errors)

        preview = None
        if contract is not None and graph and not errors:
            preview = contract_view(contract, graph)

        raw_contract = _read_json_object(contract_path) if has_contract else None
        return {
            "stem": stem,
            "has_workflow": has_workflow,
            "has_contract": has_contract,
            "nodes": nodes,
            "contract": raw_contract,
            "ok": not errors,
            "errors": errors,
            "inspect_preview": preview,
            "active_tools": _active_tools(self.plugin.contracts),
        }

    async def _item(
        self,
        stem: str,
        reports: dict[Path, ContractReport],
        remote: bool,
        cache: dict[str, dict[str, Any] | str],
    ) -> dict[str, Any]:
        """生成列表里的一个工作流条目。

        Args:
            stem: 工作流文件名去掉 ``.json`` 的部分。
            reports: 契约文件路径到检查结果的映射。
            remote: 是否用 ComfyUI 节点定义核对。
            cache: 本次请求内节点定义与失败原因的缓存。

        Returns:
            工作流状态字典。
        """
        workflow_dir = self.plugin.cfg.workflow_dir
        workflow_path = workflow_dir / f"{stem}{WORKFLOW_SUFFIX}"
        contract_path = workflow_dir / f"{stem}{CONTRACT_SUFFIX}"
        report = reports.get(contract_path)
        contract = report.contract if report is not None else None
        errors = list(report.errors) if report is not None else []

        if remote and contract is not None:
            errors.extend(await self._remote_contract_errors(contract, cache))

        hidden = (
            sorted(name for name, item in contract.inputs.items() if not item.visible)
            if contract is not None
            else []
        )
        return {
            "stem": stem,
            "workflow_file": workflow_path.name,
            "has_workflow": workflow_path.is_file(),
            "contract_file": contract_path.name,
            "has_contract": contract_path.is_file(),
            "enabled": contract.enabled if contract is not None else False,
            "tool_name": contract.name if contract is not None else None,
            "description": contract.description if contract is not None else None,
            "ok": not errors,
            "errors": errors,
            "input_count": len(contract.inputs) if contract is not None else 0,
            "visible_input_count": (
                sum(1 for item in contract.inputs.values() if item.visible)
                if contract is not None
                else 0
            ),
            "hidden_inputs": hidden,
            "output_count": len(contract.outputs) if contract is not None else 0,
        }

    async def _remote_contract_errors(
        self,
        contract: ToolContract,
        cache: dict[str, dict[str, Any] | str],
    ) -> list[str]:
        """用远端节点定义核对一个契约的映射目标。

        Args:
            contract: 工具契约。
            cache: 本次请求内的节点定义缓存。

        Returns:
            面向管理员的问题描述。
        """
        try:
            graph = read_workflow(contract.workflow_path)
        except ContractError:
            return []
        node_defs, failures = await self._node_definitions(
            _class_types(graph, _contract_node_ids(contract)),
            cache,
        )
        return remote_target_errors(contract, graph, node_defs, failures)

    async def _attach_remote(
        self,
        nodes: list[dict[str, Any]],
        graph: dict[str, Any],
        contract: ToolContract | None,
        errors: list[str],
    ) -> None:
        """给每个节点附上远端定义信息，并核对契约目标。

        Args:
            nodes: 已解析的节点列表。
            graph: 工作流图。
            contract: 配对的工具契约。
            errors: 要追加问题的列表。
        """
        class_types = {node["class_type"] for node in nodes if node["class_type"]}
        node_defs, failures = await self._node_definitions(class_types, {})
        for node in nodes:
            class_type = node["class_type"]
            problem = failures.get(class_type)
            definition = node_defs.get(class_type)
            if problem is not None:
                node["remote"] = {
                    "output_node": False,
                    "error": problem,
                    "fields": {},
                }
                continue
            if definition is None:
                node["remote"] = {
                    "output_node": False,
                    "error": f"节点类型 {class_type} 不存在",
                    "fields": {},
                }
                continue
            node["remote"] = {
                "output_node": definition.get("output_node") is True,
                "error": None,
                "fields": _remote_fields(definition),
            }
        if contract is not None:
            errors.extend(remote_target_errors(contract, graph, node_defs, failures))

    async def _node_definitions(
        self,
        class_types: set[str],
        cache: dict[str, dict[str, Any] | str],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """读取节点类型定义，成功与失败分别聚合。

        Args:
            class_types: 要读取的节点类型。
            cache: 本次请求内的缓存，值为定义字典或失败原因。

        Returns:
            节点类型到定义的映射，以及节点类型到失败原因的映射。
        """
        node_defs: dict[str, dict[str, Any]] = {}
        failures: dict[str, str] = {}
        for class_type in sorted(class_types):
            if class_type in cache:
                cached = cache[class_type]
                if isinstance(cached, str):
                    failures[class_type] = cached
                else:
                    node_defs[class_type] = cached
                continue
            try:
                definition = await self.plugin.client.object_info(class_type)
            except ComfyError as exc:
                cache[class_type] = str(exc)
                failures[class_type] = str(exc)
                logger.warning(f"[ComfyUI] 读取节点定义失败 {class_type}: {exc}")
                continue
            cache[class_type] = definition
            node_defs[class_type] = definition
        return node_defs, failures


def _is_safe_stem(stem: str) -> bool:
    """判断工作流 stem 能否安全地拼成目录里的文件名。

    Args:
        stem: 工作流文件名去掉 ``.json`` 的部分。

    Returns:
        非空、不含路径分隔与 ``..``、不以点开头、不是附属文件时返回 True。
    """
    if not stem or stem.startswith("."):
        return False
    if "/" in stem or "\\" in stem or ".." in stem:
        return False
    return not is_aux_file(f"{stem}{WORKFLOW_SUFFIX}")


def _remote_flag(value: str | None) -> bool:
    return value is not None and value.lower() in ("true", "1")


def _stems(workflow_dir: Path) -> list[str]:
    stems: list[str] = []
    if workflow_dir.exists():
        for path in sorted(workflow_dir.glob(f"*{WORKFLOW_SUFFIX}")):
            if is_aux_file(path.name):
                continue
            stems.append(path.name[: -len(WORKFLOW_SUFFIX)])
    for path in discover_contracts(workflow_dir):
        stem = path.name[: -len(CONTRACT_SUFFIX)]
        if stem not in stems:
            stems.append(stem)
    return stems


def _reports(workflow_dir: Path) -> dict[Path, ContractReport]:
    return {
        report.path: report for report in inspect_contracts(workflow_dir, PLUGIN_DIR)
    }


def _active_tools(contracts: list[ToolContract]) -> list[str]:
    return [contract.name for contract in contracts]


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _upload_conflict(contract_path: Path, data: dict[str, Any]) -> str | None:
    schema = load_contract_schema(PLUGIN_DIR)
    try:
        contract = load_contract(contract_path, schema)
    except ContractError as exc:
        return f"同名工具契约 {contract_path.name} 无法读取，请先修复或删除:\n{exc}"
    if not contract.enabled:
        return None
    problems = graph_target_errors(contract, data, contract_path.name)
    if not problems:
        return None
    return "新工作流会导致已启用契约失效:\n" + "\n".join(problems)


def _duplicate_name_errors(
    workflow_dir: Path,
    contract_path: Path,
    contract: ToolContract,
    schema: dict[str, Any],
) -> list[str]:
    others: list[str] = []
    for path in discover_contracts(workflow_dir):
        if path == contract_path:
            continue
        try:
            other = load_contract(path, schema)
        except ContractError:
            continue
        if other.enabled and other.name == contract.name:
            others.append(path.name)
    if not others:
        return []
    return [f"{contract_path.name}: 工具名 {contract.name} 与 {', '.join(others)} 重复"]


def _is_disabled(contract_path: Path) -> bool:
    try:
        contract = load_contract(contract_path, load_contract_schema(PLUGIN_DIR))
    except ContractError:
        return False
    return not contract.enabled


def _restore(path: Path, previous: str | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(previous, encoding="utf-8")


def _contract_node_ids(contract: ToolContract) -> list[str]:
    node_ids = [parameter.node for parameter in contract.inputs.values()]
    node_ids.extend(output.node for output in contract.outputs.values())
    return node_ids


def _class_types(graph: dict[str, Any], node_ids: list[str]) -> set[str]:
    found: set[str] = set()
    for node_id in node_ids:
        node = graph.get(node_id)
        if isinstance(node, dict) and isinstance(node.get("class_type"), str):
            found.add(node["class_type"])
    return found


def _nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for node_id in sorted(graph, key=_node_sort_key):
        node = graph[node_id]
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type", ""))
        nodes.append(
            {
                "id": node_id,
                "class_type": class_type,
                "title": _node_title(node, class_type),
                "fields": _node_fields(node.get("inputs")),
            }
        )
    return nodes


def _node_sort_key(node_id: str) -> tuple[int, int, str]:
    return (0, int(node_id), "") if node_id.isdigit() else (1, 0, node_id)


def _node_title(node: dict[str, Any], class_type: str) -> str:
    meta = node.get("_meta")
    if isinstance(meta, dict) and isinstance(meta.get("title"), str):
        return meta["title"]
    return class_type


def _node_fields(inputs: Any) -> list[dict[str, Any]]:
    if not isinstance(inputs, dict):
        return []
    fields: list[dict[str, Any]] = []
    for name, value in inputs.items():
        link = is_link_value(value)
        fields.append(
            {
                "name": str(name),
                "is_link": link,
                "value": None if link else value,
                "link": value if link else None,
            }
        )
    return fields


def _remote_fields(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    declared = definition.get("input")
    if not isinstance(declared, dict):
        return fields
    for section in ("required", "optional"):
        specs = declared.get(section)
        if not isinstance(specs, dict):
            continue
        for name, spec in specs.items():
            fields[str(name)] = _remote_field(spec, section == "required")
    return fields


def _remote_field(spec: Any, required: bool) -> dict[str, Any]:
    suggested: str | None = None
    options: dict[str, Any] = {}
    enum: list[Any] | None = None
    if isinstance(spec, list) and spec:
        head = spec[0]
        if isinstance(head, str):
            suggested = head
        elif isinstance(head, list):
            suggested = "COMBO"
            enum = head
        if len(spec) > 1 and isinstance(spec[1], dict):
            options = spec[1]
    if enum is None and isinstance(options.get("options"), list):
        enum = options["options"]
    return {
        "suggested_type": suggested,
        "required": required,
        "enum": enum,
        "minimum": options.get("min"),
        "maximum": options.get("max"),
    }
