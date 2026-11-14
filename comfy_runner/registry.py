"""契约扫描、启动校验与工作流工具集合。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .contract import (
    CONTRACT_SUFFIX,
    ContractError,
    ToolContract,
    build_parameter_schema,
    load_contract,
    load_contract_schema,
    validate_contract,
)


@dataclass(frozen=True, slots=True)
class ContractReport:
    """一个契约文件的检查结果。"""

    path: Path
    contract: ToolContract | None
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """没有任何问题时为 True。"""
        return not self.errors

    @property
    def enabled(self) -> bool:
        """契约可用且声明为启用时为 True。"""
        return self.contract is not None and self.contract.enabled


@dataclass(frozen=True, slots=True)
class WorkflowTool:
    """一个待注册的工作流工具。"""

    contract: ToolContract
    parameters: dict[str, Any]


def discover_contracts(workflow_dir: Path) -> list[Path]:
    """列出目录里的全部契约文件。

    Args:
        workflow_dir: 工作流目录。

    Returns:
        按文件名排序的契约路径；目录不存在时为空列表。
    """
    if not workflow_dir.exists():
        return []
    return sorted(workflow_dir.glob(f"*{CONTRACT_SUFFIX}"))


def inspect_contracts(workflow_dir: Path, plugin_dir: Path) -> list[ContractReport]:
    """检查全部契约，包含禁用契约。

    Args:
        workflow_dir: 工作流目录。
        plugin_dir: 插件根目录，用于定位契约 Schema。

    Returns:
        每个契约文件的检查结果。

    Raises:
        ContractError: 契约 Schema 文件本身不可用。
    """
    schema = load_contract_schema(plugin_dir)
    reports: list[ContractReport] = []
    for path in discover_contracts(workflow_dir):
        try:
            contract = load_contract(path, schema)
        except ContractError as exc:
            problems = tuple(str(exc).splitlines())
            reports.append(ContractReport(path, None, problems))
            continue
        reports.append(
            ContractReport(path, contract, tuple(validate_contract(contract))),
        )
    return _with_name_errors(reports)


def build_tools(workflow_dir: Path, plugin_dir: Path) -> list[WorkflowTool]:
    """构建待注册的工作流工具集合。

    Args:
        workflow_dir: 工作流目录。
        plugin_dir: 插件根目录。

    Returns:
        按工具名排序的待注册工具；没有启用契约时为空列表。

    Raises:
        ContractError: 任一文件不符合格式，或任一启用契约语义不成立。
    """
    reports = inspect_contracts(workflow_dir, plugin_dir)
    blocking = [
        report
        for report in reports
        if not report.ok and (report.contract is None or report.contract.enabled)
    ]
    if blocking:
        detail = "\n".join(error for report in blocking for error in report.errors)
        raise ContractError(f"工具契约不合法:\n{detail}")

    tools = [
        WorkflowTool(
            contract=report.contract,
            parameters=build_parameter_schema(report.contract),
        )
        for report in reports
        if report.enabled and report.contract is not None
    ]
    tools.sort(key=lambda tool: tool.contract.name)
    return tools


def _with_name_errors(reports: list[ContractReport]) -> list[ContractReport]:
    grouped: dict[str, list[ContractReport]] = {}
    for report in reports:
        contract = report.contract
        if contract is None or not contract.enabled:
            continue
        grouped.setdefault(contract.name, []).append(report)

    duplicates: dict[Path, str] = {}
    for name in sorted(grouped):
        group = grouped[name]
        if len(group) < 2:
            continue
        for report in group:
            others = [item.path.name for item in group if item.path != report.path]
            duplicates[report.path] = (
                f"{report.path.name}: 工具名 {name} 与 {', '.join(others)} 重复"
            )

    if not duplicates:
        return reports
    return [
        replace(report, errors=report.errors + (duplicates[report.path],))
        if report.path in duplicates
        else report
        for report in reports
    ]
