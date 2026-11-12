"""ComfyUI 客户端、工作流与 LoRA 管理器的组装。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .comfy_runner.comfy import ComfyClient, ComfyError
from .comfy_runner.config import Config
from .comfy_runner.lora import LoraManager
from .comfy_runner.workflow import (
    Workflow,
    apply_lora_selections,
    apply_steps_override,
    inject_prompt,
    inject_seeds,
    load_steps_override,
)


class ComfyUI:
    """一次出图所需的三件东西：HTTP 客户端、当前工作流、LoRA 管理器。"""

    def __init__(self, config: Config) -> None:
        """按配置装配客户端。

        Args:
            config: 插件配置。
        """
        self.client = ComfyClient(config.server_address)
        self.workflow = Workflow(
            directory=config.workflow_dir,
            filename=config.workflow.json_file,
            input_node_id=config.workflow.input_node_id,
            neg_node_id=config.workflow.neg_node_id,
            output_node_id=config.workflow.output_node_id,
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

    def switch_workflow(
        self,
        filename: str,
        input_id: str | None = None,
        output_id: str | None = None,
        neg_node_id: str | None = None,
    ) -> tuple[bool, str]:
        """切换当前工作流并可选地覆盖节点绑定。

        Args:
            filename: 新的工作流文件名。
            input_id: 新的正面提示词节点 ID。
            output_id: 新的输出节点 ID。
            neg_node_id: 新的负面提示词节点 ID。

        Returns:
            文件是否存在，以及给用户看的提示文本。
        """
        exists = self.workflow.switch(
            filename,
            input_node_id=input_id,
            neg_node_id=neg_node_id,
            output_node_id=output_id,
        )
        self.lora.clear_caches()

        status = "exists" if exists else "missing"
        logger.info(
            f"[ComfyUI] switch workflow -> {filename} [{status}] | "
            f"Input:{self.workflow.input_node_id} | "
            f"Neg:{self.workflow.neg_node_id} | "
            f"Output:{self.workflow.output_node_id or 'auto'}"
        )
        return exists, (
            f"已切换至 {filename}，文件状态：{status}\n"
            f"当前节点设置: Positive={self.workflow.input_node_id}, "
            f"Negative={self.workflow.neg_node_id}, "
            f"Output={self.workflow.output_node_id or '自动'}"
        )

    def inject_params(
        self,
        workflow: dict[str, Any],
        prompt: str,
        lora_selections: list[dict[str, Any]] | None = None,
    ) -> None:
        """把提示词、LoRA、步数覆盖与随机种子写进工作流。

        Args:
            workflow: 工作流图。
            prompt: 正面提示词。
            lora_selections: 已解析的 LoRA 选择。
        """
        inject_prompt(workflow, self.workflow.input_node_id, prompt)

        if self.lora.settings.enabled and lora_selections:
            applied = apply_lora_selections(
                workflow,
                lora_selections,
                keep_defaults=self.lora.settings.keep_workflow_defaults_when_selected,
            )
            if applied:
                logger.info(f"[ComfyUI] applied LoRA selections: {', '.join(applied)}")
            else:
                logger.info(
                    "[ComfyUI] LoRA selections detected, but no compatible LoRA stack node was found"
                )

        overrides = load_steps_override(self.workflow.steps_override_path)
        if overrides:
            count = apply_steps_override(workflow, overrides)
            if count > 0:
                override_info = ", ".join([f"{key}:{value}步" for key, value in overrides.items()])
                logger.info(
                    f"[ComfyUI] steps override applied: {override_info} (updated {count} targets)"
                )
            else:
                logger.info(
                    "[ComfyUI] steps override configured but no matching reference was found"
                )

        inject_seeds(workflow)

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
        try:
            workflow = self.workflow.load()
        except (OSError, ValueError) as exc:
            return None, str(exc)

        resolved = (
            self.lora.resolve_selections(lora_selections)
            if self.lora.settings.enabled
            else []
        )
        prompt, hints = self.lora.inject_prompt_hints(str(prompt or ""), resolved)
        if hints:
            logger.info(f"[ComfyUI] injected LoRA prompt hints: {', '.join(hints)}")

        self.inject_params(workflow, prompt, resolved)

        try:
            image = await self.client.generate(workflow, self.workflow.output_node_id)
        except ComfyError as exc:
            return None, str(exc)
        return image, None
