"""ComfyUI 客户端、工作流与 LoRA 管理器的组装。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .comfy import (
    ArtifactRef,
    ComfyClient,
    ComfyError,
    OutputError,
    artifact_ref,
    ensure_success,
)
from .config import Config
from .lora import LoraManager
from .workflow import Workflow, inject_prompt


class ComfyUI:
    """旧出图路径的装配：HTTP 客户端、当前工作流、LoRA 管理器。"""

    def __init__(self, config: Config) -> None:
        """按配置装配客户端。

        Args:
            config: 插件配置。
        """
        self.client = ComfyClient(
            config.server_address,
            timeout=config.execution.timeout_seconds,
            poll_interval=config.execution.poll_interval_seconds,
        )
        self.workflow = Workflow(
            directory=config.workflow_dir,
            filename=config.workflow.json_file,
            bindings=config.workflow.bindings,
        )
        self.lora = LoraManager(config.llm.lora_control, self.workflow)

        logger.info(
            f"[ComfyUI API] loaded | workflow_dir: {self.workflow.directory} | "
            f"workflow: {self.workflow.filename}"
        )
        if self.lora.scan_roots:
            logger.info(
                "[ComfyUI API] LoRA scan roots: %s",
                ", ".join(str(path) for path in self.lora.scan_roots),
            )

    def switch_workflow(self, filename: str) -> tuple[bool, str]:
        """切换当前工作流并应用该文件名的节点绑定。

        Args:
            filename: 新的工作流文件名。

        Returns:
            文件是否存在，以及给用户看的提示文本。
        """
        exists = self.workflow.switch(filename)
        self.lora.clear_caches()

        status = "exists" if exists else "missing"
        logger.info(
            f"[ComfyUI] switch workflow -> {filename} [{status}] | "
            f"Input:{self.workflow.input_node_id or '-'} | "
            f"Neg:{self.workflow.neg_node_id or '-'} | "
            f"Output:{self.workflow.output_node_id or 'auto'} | "
            f"registered:{self.workflow.registered}"
        )
        if self.workflow.registered:
            detail = (
                "当前节点设置: "
                f"Positive={self.workflow.input_node_id}, "
                f"Negative={self.workflow.neg_node_id or '未绑定'}, "
                f"Output={self.workflow.output_node_id or '自动'}"
            )
        else:
            detail = "⚠ 该工作流未在 bindings 中登记，出图前请补上节点绑定"
        return exists, f"已切换至 {filename}，文件状态：{status}\n{detail}"

    def inject_params(
        self,
        workflow: dict[str, Any],
        prompt: str,
    ) -> None:
        """把提示词写进工作流。

        Args:
            workflow: 工作流图。
            prompt: 正面提示词。
        """
        inject_prompt(workflow, self.workflow.input_node_id, prompt)

    async def generate(
        self,
        prompt: str,
        lora_selections: list[dict[str, Any]] | None = None,
    ) -> tuple[bytes | None, str | None]:
        """按当前工作流出图。

        Args:
            prompt: 正面提示词。
            lora_selections: LLM 给出的 LoRA 选择。

        Returns:
            图片字节与错误文本，成功时错误文本为 None。
        """
        if not self.workflow.filename:
            return None, (
                "未配置工作流文件；请用 /comfy ls 查看可用工作流，并用 /comfy_use 切换"
            )
        if not self.workflow.registered:
            return None, (
                f"工作流 {self.workflow.filename} 未在 bindings 中登记，"
                "请在 WebUI 的 ComfyUI 工作流节点配置里补上正面与输出节点"
            )

        try:
            workflow = self.workflow.load()
        except (OSError, ValueError) as exc:
            return None, (f"{exc}；请用 /comfy ls 查看可用工作流，并用 /comfy_use 切换")

        resolved = (
            self.lora.resolve_selections(lora_selections)
            if self.lora.settings.enabled
            else []
        )
        prompt, hints = self.lora.inject_prompt_hints(str(prompt or ""), resolved)
        if hints:
            logger.info(f"[ComfyUI] injected LoRA prompt hints: {', '.join(hints)}")

        self.inject_params(workflow, prompt)

        try:
            prompt_id = await self.client.submit(workflow)
            entry = await self.client.wait(prompt_id)
            ref = _first_image(entry, self.workflow.output_node_id)
            image = await self.client.download(ref)
        except ComfyError as exc:
            return None, str(exc)
        return image, None


def _first_image(entry: dict[str, Any], output_node_id: str) -> ArtifactRef:
    """旧路径取图：指定节点或第一个有图的节点。"""
    ensure_success(entry)
    outputs = entry.get("outputs")
    if not isinstance(outputs, dict):
        raise OutputError("执行结果缺少 outputs")

    candidates = [outputs.get(output_node_id)] if output_node_id else []
    if not output_node_id:
        candidates = list(outputs.values())

    for node_output in candidates:
        if not isinstance(node_output, dict):
            continue
        for item in node_output.get("images") or []:
            ref = artifact_ref(item)
            if ref is not None:
                return ref

    if output_node_id:
        raise OutputError(f"输出节点 {output_node_id} 没有图片")
    raise OutputError("工作流执行完成，但未找到输出图片")
