"""契约校验、工具注册与检查命令的用户可见行为。"""

import asyncio
import json
from pathlib import Path

import pytest
from astrbot_plugin_comfy_runner.comfy_runner.contract import ContractError
from helpers import FakeEvent, collect, make_plugin

WORKFLOW = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    "3": {
        "class_type": "KSampler",
        "inputs": {"seed": 1, "steps": 20, "positive": ["1", 0]},
    },
    "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0]}},
}

CONTRACT = {
    "version": 1,
    "name": "comfy_moss_t2i",
    "description": "使用 MOSS 工作流根据提示词生成图片",
    "enabled": True,
    "inputs": {
        "prompt": {
            "type": "string",
            "description": "描述期望画面的完整提示词",
            "required": True,
            "minLength": 1,
            "target": {"node": "1", "field": "text"},
        },
        "seed": {
            "type": "integer",
            "description": "随机种子",
            "minimum": 0,
            "target": {"node": "3", "field": "seed"},
        },
    },
    "outputs": {
        "image": {"type": "image", "target": {"node": "4", "field": "images"}},
    },
}


def write_contract(
    tmp_path: Path,
    stem: str = "moss",
    contract: dict | None = None,
    workflow: dict | None = None,
) -> Path:
    """在插件数据目录里写一对工作流与契约。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / f"{stem}.json").write_text(
        json.dumps(workflow if workflow is not None else WORKFLOW),
        encoding="utf-8",
    )
    path = workflow_dir / f"{stem}.tool.json"
    path.write_text(
        json.dumps(contract if contract is not None else CONTRACT),
        encoding="utf-8",
    )
    return path


def register(plugin) -> list:
    """跑一次插件初始化并返回注册的工具。"""
    asyncio.run(plugin.initialize())
    return plugin.context.llm_tools


def admin_overrides() -> dict:
    """管理员在群 2 的访问配置。"""
    return {"admin_ids": ["9"], "whitelist_group_ids": ["2"]}


def test_valid_contract_registers_tool(tmp_path, monkeypatch):
    """合法契约注册出名称、描述与参数 Schema 正确的独立工具。"""
    write_contract(tmp_path)

    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    tools = register(plugin)

    assert [tool.name for tool in tools] == ["comfy_moss_t2i"]
    assert tools[0].description == "使用 MOSS 工作流根据提示词生成图片"
    assert tools[0].parameters == {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "描述期望画面的完整提示词",
                "minLength": 1,
            },
            "seed": {
                "type": "integer",
                "description": "随机种子",
                "minimum": 0,
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    }


def test_no_contract_loads_without_tools(tmp_path, monkeypatch):
    """没有契约时插件正常加载但不注册工具。"""
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    assert register(plugin) == []


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda c: c["inputs"]["prompt"]["target"].update(node="99"),
            id="missing-node",
        ),
        pytest.param(
            lambda c: c["inputs"]["prompt"]["target"].update(field="nope"),
            id="missing-field",
        ),
        pytest.param(
            lambda c: c["inputs"].update(
                style={
                    "type": "string",
                    "description": "风格",
                    "target": {"node": "3", "field": "positive"},
                }
            ),
            id="linked-target",
        ),
        pytest.param(
            lambda c: c["outputs"]["image"].update(type="video"),
            id="unsupported-output",
        ),
        pytest.param(
            lambda c: c["outputs"]["image"]["target"].update(node="99"),
            id="missing-output-node",
        ),
        pytest.param(
            lambda c: c["inputs"]["prompt"].update(maxLength=0),
            id="inverted-limits",
        ),
    ],
)
def test_invalid_enabled_contract_prevents_load(tmp_path, monkeypatch, mutate):
    """启用契约有问题时插件拒绝加载。"""
    contract = json.loads(json.dumps(CONTRACT))
    mutate(contract)
    write_contract(tmp_path, contract=contract)

    with pytest.raises(ContractError):
        make_plugin(tmp_path, monkeypatch, control=admin_overrides())


def test_duplicate_tool_names_prevent_load(tmp_path, monkeypatch):
    """两个启用契约声明同一工具名时插件拒绝加载。"""
    write_contract(tmp_path, stem="first")
    write_contract(tmp_path, stem="second")

    with pytest.raises(ContractError) as error:
        make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    assert "重复" in str(error.value)


def test_disabled_contract_is_not_registered(tmp_path, monkeypatch):
    """禁用契约不注册，其余契约照常注册。"""
    disabled = json.loads(json.dumps(CONTRACT))
    disabled.update(enabled=False, name="comfy_disabled")
    write_contract(tmp_path, stem="disabled", contract=disabled)
    write_contract(tmp_path)

    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    tools = register(plugin)

    assert [tool.name for tool in tools] == ["comfy_moss_t2i"]


def test_disabled_contract_with_broken_mapping_still_loads(tmp_path, monkeypatch):
    """禁用契约的映射错误不阻止加载，留给检查命令报告。"""
    disabled = json.loads(json.dumps(CONTRACT))
    disabled.update(enabled=False, name="comfy_disabled")
    disabled["inputs"]["prompt"]["target"].update(node="99")
    write_contract(tmp_path, stem="disabled", contract=disabled)

    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    assert register(plugin) == []


def test_unparsable_contract_file_prevents_load(tmp_path, monkeypatch):
    """契约文件不是合法 JSON 时插件拒绝加载。"""
    path = write_contract(tmp_path)
    path.write_text("{", encoding="utf-8")

    with pytest.raises(ContractError):
        make_plugin(tmp_path, monkeypatch, control=admin_overrides())


def test_check_command_reports_disabled_contract_errors(tmp_path, monkeypatch):
    """检查命令覆盖禁用契约并给节点级定位。"""
    disabled = json.loads(json.dumps(CONTRACT))
    disabled.update(enabled=False, name="comfy_disabled")
    disabled["inputs"]["prompt"]["target"].update(node="99")
    write_contract(tmp_path, stem="disabled", contract=disabled)
    write_contract(tmp_path)

    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    results = collect(plugin.cmd_comfy_check(FakeEvent("/comfy check", sender="9")))

    assert "共 2 个契约文件，启用 1 个" in results[0][1]
    assert "moss.tool.json" in results[0][1]
    assert "参数 prompt 的目标节点 99 不存在" in results[0][1]


def test_check_command_requires_admin(tmp_path, monkeypatch):
    """普通用户不能运行检查命令。"""
    write_contract(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    results = collect(plugin.cmd_comfy_check(FakeEvent("/comfy check", sender="5")))

    assert "权限不足" in results[0][1]
