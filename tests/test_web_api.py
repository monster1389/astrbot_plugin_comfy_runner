"""插件视图 Web API 的用户可见行为。"""

import asyncio
import json
from pathlib import Path
from urllib.parse import urlencode

from astrbot.api.web import PluginRequest, bind_request_context
from helpers import FakeComfyServer, make_plugin
from starlette.requests import Request

BOUNDARY = "----comfy-runner-test"
ROOT = "/astrbot_plugin_comfy_runner"
LIST_ROUTE = f"{ROOT}/workflows"
DETAIL_ROUTE = f"{ROOT}/workflow"
UPLOAD_ROUTE = f"{ROOT}/workflow/upload"
CONTRACT_ROUTE = f"{ROOT}/workflow/contract"
PREVIEW_ROUTE = f"{ROOT}/workflow/preview"
DELETE_ROUTE = f"{ROOT}/workflow/delete"

WORKFLOW = {
    "1": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": ""},
        "_meta": {"title": "正面提示词"},
    },
    "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
}

CONTRACT = {
    "version": 1,
    "name": "comfy_moss_t2i",
    "description": "使用 MOSS 工作流根据提示词生成图片",
    "enabled": True,
    "inputs": {
        "prompt": {
            "type": "string",
            "description": "提示词",
            "required": True,
            "target": {"node": "1", "field": "text"},
        }
    },
    "outputs": {
        "image": {"type": "image", "target": {"node": "3", "field": "images"}},
    },
}


def admin_overrides() -> dict:
    """管理员在群 2 的访问配置。"""
    return {"admin_ids": ["9"], "whitelist_group_ids": ["2"]}


def workflow_dir_of(tmp_path: Path) -> Path:
    """返回临时插件的数据目录里的工作流目录。"""
    return tmp_path / "plugin_data" / "workflow"


def write_pair(tmp_path: Path, contract: dict | None = None) -> Path:
    """写一份工作流与契约，返回工作流目录。"""
    workflow_dir = workflow_dir_of(tmp_path)
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "moss.json").write_text(json.dumps(WORKFLOW), encoding="utf-8")
    (workflow_dir / "moss.tool.json").write_text(
        json.dumps(contract if contract is not None else CONTRACT),
        encoding="utf-8",
    )
    return workflow_dir


def write_workflow_only(tmp_path: Path) -> Path:
    """只写一份工作流，返回工作流目录。"""
    workflow_dir = workflow_dir_of(tmp_path)
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "moss.json").write_text(json.dumps(WORKFLOW), encoding="utf-8")
    return workflow_dir


def make_request(
    method: str,
    path: str,
    query: dict[str, str] | None = None,
    payload: dict | None = None,
    files: list[tuple[str, str, bytes]] | None = None,
) -> PluginRequest:
    """构造绑定到插件 Web API 的真实请求对象。"""
    headers = [(b"host", b"testserver")]
    body = b""
    if files is not None:
        body, content_type = _multipart(files)
        headers.append((b"content-type", content_type.encode("utf-8")))
    elif payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": urlencode(query or {}).encode("utf-8"),
        "headers": headers,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "http_version": "1.1",
        "root_path": "",
    }
    state = {"sent": False}

    async def receive():
        if state["sent"]:
            return {"type": "http.disconnect"}
        state["sent"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return PluginRequest(
        Request(scope, receive),
        plugin_name="astrbot_plugin_comfy_runner",
    )


def call_api(
    plugin,
    method: str,
    path: str,
    **kwargs,
) -> tuple[int, dict]:
    """调用插件注册的真实 Web API handler 并解析响应。"""
    handler = plugin.context.web_apis[(path, method)]
    plugin_request = make_request(method, path, **kwargs)

    async def scenario():
        with bind_request_context(plugin_request):
            return await handler()

    response = asyncio.run(scenario())
    return response.status_code, json.loads(response.body)


def _multipart(files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    chunks = []
    for name, filename, content in files:
        chunks.append(
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: application/json\r\n\r\n'.encode()
            + content
            + b"\r\n"
        )
    chunks.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={BOUNDARY}"


def test_list_workflows_reports_contract_state(tmp_path, monkeypatch):
    """列表接口报告工作流、契约、启用状态与参数计数。"""
    write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(plugin, "GET", LIST_ROUTE)

    assert status == 200
    assert body["workflow_dir"] == str(workflow_dir_of(tmp_path))
    assert body["active_tools"] == ["comfy_moss_t2i"]
    item = body["items"][0]
    assert item["stem"] == "moss"
    assert item["workflow_file"] == "moss.json"
    assert item["contract_file"] == "moss.tool.json"
    assert item["has_workflow"] is True
    assert item["has_contract"] is True
    assert item["enabled"] is True
    assert item["tool_name"] == "comfy_moss_t2i"
    assert item["description"] == "使用 MOSS 工作流根据提示词生成图片"
    assert item["ok"] is True
    assert item["errors"] == []
    assert item["input_count"] == 1
    assert item["visible_input_count"] == 1
    assert item["hidden_inputs"] == []
    assert item["output_count"] == 1


def test_list_workflows_includes_workflow_without_contract(tmp_path, monkeypatch):
    """没有契约的工作流也出现在列表里。"""
    workflow_dir = write_workflow_only(tmp_path)
    (workflow_dir / "plain.json").write_text(json.dumps(WORKFLOW), encoding="utf-8")
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(plugin, "GET", LIST_ROUTE)

    assert status == 200
    assert [item["stem"] for item in body["items"]] == ["moss", "plain"]
    plain = body["items"][1]
    assert plain["has_contract"] is False
    assert plain["tool_name"] is None
    assert plain["errors"] == []


def test_list_workflows_remote_checks_node_definitions(tmp_path, monkeypatch):
    """Remote 查询参数用 ComfyUI 节点定义核对契约目标。"""
    write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    server = FakeComfyServer().install(monkeypatch)
    server.object_info = {
        "CLIPTextEncode": {"input": {"required": {"other": ["STRING", {}]}}},
        "SaveImage": {"output_node": True, "input": {"required": {}}},
    }

    status, body = call_api(plugin, "GET", LIST_ROUTE, query={"remote": "true"})

    assert status == 200
    item = body["items"][0]
    assert item["ok"] is False
    assert "参数 prompt 的字段 1.text 不在节点类型 CLIPTextEncode 的输入中" in (
        "\n".join(item["errors"])
    )


def test_workflow_detail_returns_nodes_and_contract(tmp_path, monkeypatch):
    """详情接口返回节点、字段、原始契约与模型预览。"""
    write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(plugin, "GET", DETAIL_ROUTE, query={"stem": "moss"})

    assert status == 200
    assert body["has_workflow"] is True
    assert body["has_contract"] is True
    assert [node["id"] for node in body["nodes"]] == ["1", "3"]
    prompt = body["nodes"][0]
    assert prompt["class_type"] == "CLIPTextEncode"
    assert prompt["title"] == "正面提示词"
    assert prompt["fields"] == [
        {"name": "text", "is_link": False, "value": "", "link": None}
    ]
    save = body["nodes"][1]
    assert save["title"] == "SaveImage"
    assert save["fields"] == [
        {"name": "images", "is_link": True, "value": None, "link": ["1", 0]}
    ]
    assert body["contract"] == CONTRACT
    assert body["ok"] is True
    assert body["errors"] == []
    assert body["inspect_preview"]["name"] == "comfy_moss_t2i"
    assert body["inspect_preview"]["parameters"]["properties"]["prompt"]["type"] == (
        "string"
    )
    assert body["active_tools"] == ["comfy_moss_t2i"]


def test_workflow_detail_rejects_unsafe_stem(tmp_path, monkeypatch):
    """带路径穿越的 stem 一律拒绝。"""
    write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(plugin, "GET", DETAIL_ROUTE, query={"stem": "../moss"})

    assert status == 400
    assert "不合法" in body["message"]


def test_workflow_detail_reports_missing_stem(tmp_path, monkeypatch):
    """工作流与契约都不存在时返回 404。"""
    write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(plugin, "GET", DETAIL_ROUTE, query={"stem": "nope"})

    assert status == 404
    assert "不存在" in body["message"]


def test_workflow_detail_remote_attaches_node_definitions(tmp_path, monkeypatch):
    """Remote 时每个节点附带远端定义与字段约束。"""
    write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    server = FakeComfyServer().install(monkeypatch)
    server.object_info = {
        "CLIPTextEncode": {
            "input": {"required": {"text": ["STRING", {"default": ""}]}}
        },
        "SaveImage": {
            "output_node": True,
            "input": {"required": {"images": ["IMAGE", {"min": 0, "max": 8}]}},
        },
    }

    status, body = call_api(
        plugin,
        "GET",
        DETAIL_ROUTE,
        query={"stem": "moss", "remote": "true"},
    )

    assert status == 200
    prompt = body["nodes"][0]["remote"]
    assert prompt["output_node"] is False
    assert prompt["error"] is None
    assert prompt["fields"]["text"]["suggested_type"] == "STRING"
    assert prompt["fields"]["text"]["required"] is True
    save = body["nodes"][1]["remote"]
    assert save["output_node"] is True
    assert save["fields"]["images"]["suggested_type"] == "IMAGE"
    assert save["fields"]["images"]["minimum"] == 0
    assert save["fields"]["images"]["maximum"] == 8
    assert body["ok"] is True


def test_workflow_detail_remote_reports_unreachable_node(tmp_path, monkeypatch):
    """远端读不到节点定义时在节点与 errors 里如实报告。"""
    write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    server = FakeComfyServer().install(monkeypatch)
    server.object_info_status = 500

    status, body = call_api(
        plugin,
        "GET",
        DETAIL_ROUTE,
        query={"stem": "moss", "remote": "true"},
    )

    assert status == 200
    assert body["nodes"][0]["remote"]["error"] is not None
    assert body["ok"] is False
    assert "无法核对" in "\n".join(body["errors"])


def test_upload_workflow_writes_file(tmp_path, monkeypatch):
    """上传通过校验后写入工作流文件并返回详情。"""
    write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    content = json.dumps(WORKFLOW).encode("utf-8")

    status, body = call_api(
        plugin,
        "POST",
        UPLOAD_ROUTE,
        files=[("file", "second.json", content)],
    )

    assert status == 200
    assert body["stem"] == "second"
    assert body["has_workflow"] is True
    assert (workflow_dir_of(tmp_path) / "second.json").is_file()


def test_upload_workflow_requires_file(tmp_path, monkeypatch):
    """没有文件时返回 400。"""
    write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(plugin, "POST", UPLOAD_ROUTE, files=[])

    assert status == 400
    assert "未收到上传的工作流文件" in body["message"]


def test_upload_workflow_rejects_canvas_format(tmp_path, monkeypatch):
    """ComfyUI 画布格式被拒绝并提示使用 API Format。"""
    write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    content = json.dumps({"nodes": [], "links": []}).encode("utf-8")

    status, body = call_api(
        plugin,
        "POST",
        UPLOAD_ROUTE,
        files=[("file", "canvas.json", content)],
    )

    assert status == 400
    assert "画布格式" in body["message"]
    assert not (workflow_dir_of(tmp_path) / "canvas.json").exists()


def test_upload_workflow_rejects_incomplete_node(tmp_path, monkeypatch):
    """节点缺少 class_type 或 inputs 时被拒绝。"""
    write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    content = json.dumps({"1": {"class_type": "CLIPTextEncode"}}).encode("utf-8")

    status, body = call_api(
        plugin,
        "POST",
        UPLOAD_ROUTE,
        files=[("file", "moss.json", content)],
    )

    assert status == 400
    assert "缺少 class_type 或 inputs" in body["message"]


def test_upload_workflow_rejects_auxiliary_filename(tmp_path, monkeypatch):
    """工具契约等附属文件不能当工作流上传。"""
    write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    content = json.dumps(WORKFLOW).encode("utf-8")

    status, body = call_api(
        plugin,
        "POST",
        UPLOAD_ROUTE,
        files=[("file", "moss.tool.json", content)],
    )

    assert status == 400


def test_upload_workflow_rejects_traversal_filename(tmp_path, monkeypatch):
    """带路径穿越的文件名被拒绝。"""
    write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    content = json.dumps(WORKFLOW).encode("utf-8")

    status, body = call_api(
        plugin,
        "POST",
        UPLOAD_ROUTE,
        files=[("file", "../moss.json", content)],
    )

    assert status == 400
    assert "不合法" in body["message"]


def test_upload_workflow_refuses_to_break_enabled_contract(tmp_path, monkeypatch):
    """新工作流会让已启用契约失效时拒绝覆盖。"""
    workflow_dir = write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    broken = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"other": ""}},
        "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }

    status, body = call_api(
        plugin,
        "POST",
        UPLOAD_ROUTE,
        files=[("file", "moss.json", json.dumps(broken).encode("utf-8"))],
    )

    assert status == 400
    assert "新工作流会导致已启用契约失效" in body["message"]
    assert json.loads((workflow_dir / "moss.json").read_text(encoding="utf-8")) == (
        WORKFLOW
    )


def test_upload_workflow_allows_shape_change_for_disabled_contract(
    tmp_path,
    monkeypatch,
):
    """契约停用后允许上传不再匹配它的工作流。"""
    workflow_dir = write_pair(tmp_path, contract=dict(CONTRACT, enabled=False))
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    changed = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"other": ""}},
        "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }

    status, body = call_api(
        plugin,
        "POST",
        UPLOAD_ROUTE,
        files=[("file", "moss.json", json.dumps(changed).encode("utf-8"))],
    )

    assert status == 200
    assert json.loads((workflow_dir / "moss.json").read_text(encoding="utf-8")) == (
        changed
    )


def test_save_contract_writes_file_and_refreshes_tools(tmp_path, monkeypatch):
    """保存启用契约后写盘并注册三个工作流工具。"""
    workflow_dir = write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(
        plugin,
        "POST",
        CONTRACT_ROUTE,
        payload={"stem": "moss", "contract": CONTRACT},
    )

    assert status == 200
    assert body["ok"] is True
    assert body["has_contract"] is True
    assert body["active_tools"] == ["comfy_moss_t2i"]
    assert json.loads((workflow_dir / "moss.tool.json").read_text("utf-8")) == CONTRACT
    assert [tool.name for tool in plugin.context.llm_tools] == [
        "comfy_list",
        "comfy_inspect",
        "comfy_execute",
    ]


def test_save_contract_rejects_schema_error_without_writing(tmp_path, monkeypatch):
    """Schema 不通过时 400，且不写盘、不改内存。"""
    workflow_dir = write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    invalid = dict(CONTRACT, name="Bad Name")

    status, body = call_api(
        plugin,
        "POST",
        CONTRACT_ROUTE,
        payload={"stem": "moss", "contract": invalid},
    )

    assert status == 400
    assert not (workflow_dir / "moss.tool.json").exists()


def test_save_contract_rejects_enabled_mapping_error_without_writing(
    tmp_path,
    monkeypatch,
):
    """启用契约的映射错误阻止保存。"""
    workflow_dir = write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    invalid = json.loads(json.dumps(CONTRACT))
    invalid["inputs"]["prompt"]["target"] = {"node": "99", "field": "text"}

    status, body = call_api(
        plugin,
        "POST",
        CONTRACT_ROUTE,
        payload={"stem": "moss", "contract": invalid},
    )

    assert status == 400
    assert "目标节点 99 不存在" in body["message"]
    assert not (workflow_dir / "moss.tool.json").exists()


def test_save_contract_rejects_duplicate_enabled_name(tmp_path, monkeypatch):
    """启用契约的工具名不得与其他启用契约重复。"""
    workflow_dir = write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    (workflow_dir / "other.json").write_text(json.dumps(WORKFLOW), encoding="utf-8")
    (workflow_dir / "other.tool.json").write_text(
        json.dumps(CONTRACT),
        encoding="utf-8",
    )

    status, body = call_api(
        plugin,
        "POST",
        CONTRACT_ROUTE,
        payload={"stem": "other", "contract": CONTRACT},
    )

    assert status == 400
    assert "重复" in body["message"]


def test_save_contract_allows_disabled_draft_with_mapping_warnings(
    tmp_path,
    monkeypatch,
):
    """停用契约作为草稿保存，映射警告随响应返回。"""
    workflow_dir = write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    draft = json.loads(json.dumps(CONTRACT))
    draft["enabled"] = False
    draft["inputs"]["prompt"]["target"] = {"node": "99", "field": "text"}

    status, body = call_api(
        plugin,
        "POST",
        CONTRACT_ROUTE,
        payload={"stem": "moss", "contract": draft},
    )

    assert status == 200
    assert body["ok"] is False
    assert "目标节点 99 不存在" in "\n".join(body["errors"])
    assert body["active_tools"] == []
    assert (workflow_dir / "moss.tool.json").is_file()


def test_save_contract_rolls_back_when_other_contract_breaks_reload(
    tmp_path,
    monkeypatch,
):
    """其他契约导致热刷新失败时回滚本次写入。"""
    workflow_dir = write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    original = (workflow_dir / "moss.tool.json").read_text(encoding="utf-8")
    (workflow_dir / "broken.tool.json").write_text("{", encoding="utf-8")

    status, body = call_api(
        plugin,
        "POST",
        CONTRACT_ROUTE,
        payload={"stem": "moss", "contract": CONTRACT},
    )

    assert status == 400
    assert body["message"]
    assert (workflow_dir / "moss.tool.json").read_text(encoding="utf-8") == original
    listed = call_api(plugin, "GET", LIST_ROUTE)[1]
    assert listed["active_tools"] == ["comfy_moss_t2i"]


def test_preview_contract_returns_model_view_without_writing(tmp_path, monkeypatch):
    """预览接口不写盘即可得到模型视图。"""
    workflow_dir = write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(
        plugin,
        "POST",
        PREVIEW_ROUTE,
        payload={"stem": "moss", "contract": CONTRACT},
    )

    assert status == 200
    assert body["ok"] is True
    assert body["errors"] == []
    preview = body["inspect_preview"]
    assert preview["name"] == "comfy_moss_t2i"
    assert preview["outputs"] == {"image": {"type": "image"}}
    assert not (workflow_dir / "moss.tool.json").exists()


def test_preview_contract_reports_schema_errors(tmp_path, monkeypatch):
    """Schema 错误以 errors 返回，不抛 400。"""
    write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(
        plugin,
        "POST",
        PREVIEW_ROUTE,
        payload={"stem": "moss", "contract": dict(CONTRACT, name="Bad Name")},
    )

    assert status == 200
    assert body["ok"] is False
    assert body["errors"]
    assert body["inspect_preview"] is None


def test_preview_contract_remote_reports_target_errors(tmp_path, monkeypatch):
    """预览的 remote 选项并入远端字段核对结果。"""
    write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    server = FakeComfyServer().install(monkeypatch)
    server.object_info = {
        "CLIPTextEncode": {"input": {"required": {"other": ["STRING", {}]}}},
        "SaveImage": {"output_node": True, "input": {"required": {}}},
    }

    status, body = call_api(
        plugin,
        "POST",
        PREVIEW_ROUTE,
        payload={"stem": "moss", "contract": CONTRACT, "remote": True},
    )

    assert status == 200
    assert body["ok"] is False
    assert "参数 prompt 的字段 1.text 不在节点类型 CLIPTextEncode 的输入中" in (
        "\n".join(body["errors"])
    )
    assert body["inspect_preview"] is None


def test_delete_contract_scope_removes_tool(tmp_path, monkeypatch):
    """只删工具契约时保留工作流并移除工具。"""
    workflow_dir = write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    asyncio.run(plugin.initialize())

    status, body = call_api(
        plugin,
        "POST",
        DELETE_ROUTE,
        payload={"stem": "moss", "scope": "contract"},
    )

    assert status == 200
    assert body["deleted"] == "contract"
    assert body["active_tools"] == []
    assert not (workflow_dir / "moss.tool.json").exists()
    assert (workflow_dir / "moss.json").exists()
    assert plugin.context.llm_tools == []


def test_delete_contract_scope_reports_missing_contract(tmp_path, monkeypatch):
    """没有工具契约可删时返回 404。"""
    write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(
        plugin,
        "POST",
        DELETE_ROUTE,
        payload={"stem": "moss", "scope": "contract"},
    )

    assert status == 404


def test_delete_all_refuses_enabled_contract(tmp_path, monkeypatch):
    """启用中的契约不允许随工作流一起删除。"""
    workflow_dir = write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(
        plugin,
        "POST",
        DELETE_ROUTE,
        payload={"stem": "moss", "scope": "all"},
    )

    assert status == 400
    assert "仍处于启用状态" in body["message"]
    assert (workflow_dir / "moss.json").exists()


def test_delete_all_removes_workflow_and_disabled_contract(tmp_path, monkeypatch):
    """契约停用后可以把工作流与契约一起删除。"""
    workflow_dir = write_pair(tmp_path, contract=dict(CONTRACT, enabled=False))
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(
        plugin,
        "POST",
        DELETE_ROUTE,
        payload={"stem": "moss", "scope": "all"},
    )

    assert status == 200
    assert body["deleted"] == "all"
    assert body["active_tools"] == []
    assert not (workflow_dir / "moss.json").exists()
    assert not (workflow_dir / "moss.tool.json").exists()


def test_delete_all_reports_missing_files(tmp_path, monkeypatch):
    """工作流与契约都不存在时返回 404。"""
    write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(
        plugin,
        "POST",
        DELETE_ROUTE,
        payload={"stem": "nope", "scope": "all"},
    )

    assert status == 404


def test_delete_rejects_unknown_scope(tmp_path, monkeypatch):
    """未知删除范围返回 400。"""
    write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(
        plugin,
        "POST",
        DELETE_ROUTE,
        payload={"stem": "moss", "scope": "everything"},
    )

    assert status == 400


def test_list_workflows_includes_orphan_contract(tmp_path, monkeypatch):
    """找不到配对工作流的契约为单独条目并带错误。"""
    workflow_dir = write_workflow_only(tmp_path)
    (workflow_dir / "orphan.tool.json").write_text(
        json.dumps(dict(CONTRACT, enabled=False)),
        encoding="utf-8",
    )
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(plugin, "GET", LIST_ROUTE)

    assert status == 200
    orphan = body["items"][1]
    assert orphan["stem"] == "orphan"
    assert orphan["has_workflow"] is False
    assert orphan["has_contract"] is True
    assert orphan["ok"] is False
    assert "缺少配对工作流 orphan.json" in "\n".join(orphan["errors"])


def test_upload_workflow_rejects_non_json_filename(tmp_path, monkeypatch):
    """非 .json 文件名被拒绝。"""
    write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(
        plugin,
        "POST",
        UPLOAD_ROUTE,
        files=[("file", "moss.txt", json.dumps(WORKFLOW).encode("utf-8"))],
    )

    assert status == 400
    assert "必须以 .json 结尾" in body["message"]


def test_upload_workflow_refuses_broken_contract(tmp_path, monkeypatch):
    """同名契约无法读取时不允许覆盖工作流。"""
    workflow_dir = write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    (workflow_dir / "moss.tool.json").write_text("{", encoding="utf-8")

    status, body = call_api(
        plugin,
        "POST",
        UPLOAD_ROUTE,
        files=[("file", "moss.json", json.dumps(WORKFLOW).encode("utf-8"))],
    )

    assert status == 400
    assert "无法读取" in body["message"]


def test_save_contract_reports_missing_workflow(tmp_path, monkeypatch):
    """没有配对工作流时拒绝保存契约。"""
    write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    status, body = call_api(
        plugin,
        "POST",
        CONTRACT_ROUTE,
        payload={"stem": "nope", "contract": CONTRACT},
    )

    assert status == 404
    assert "不存在" in body["message"]


def test_preview_contract_reports_duplicate_enabled_name(tmp_path, monkeypatch):
    """预览时也检查与其他启用契约的重名。"""
    workflow_dir = write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    (workflow_dir / "other.json").write_text(json.dumps(WORKFLOW), encoding="utf-8")

    status, body = call_api(
        plugin,
        "POST",
        PREVIEW_ROUTE,
        payload={"stem": "other", "contract": CONTRACT},
    )

    assert status == 200
    assert body["ok"] is False
    assert "重复" in "\n".join(body["errors"])


def test_delete_all_refuses_unreadable_contract(tmp_path, monkeypatch):
    """契约无法读取时不允许随工作流一起删除。"""
    workflow_dir = write_pair(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    (workflow_dir / "moss.tool.json").write_text("{", encoding="utf-8")

    status, body = call_api(
        plugin,
        "POST",
        DELETE_ROUTE,
        payload={"stem": "moss", "scope": "all"},
    )

    assert status == 400
    assert "仍处于启用状态" in body["message"]
    assert (workflow_dir / "moss.json").exists()
