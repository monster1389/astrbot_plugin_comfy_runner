import os
import re
import shutil
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.message_components import *
from astrbot.api import llm_tool, logger

from .comfy_runner.access import (
    AccessController,
    load_lexicon,
)
from .comfy_runner.config import Config
from .comfy_runner import commands
from .comfy_runner.commands import CommandContext
from .comfy_runner.draw import Drawer
from .comfyui_api import ComfyUI
# 获取插件目录（用于读取默认文件）
PLUGIN_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
class ComfyUIPlugin(Star):
    _FORCE_DRAW_CONTEXT_LIMIT = 12
    _FORCE_DRAW_MIN_REPLY_LENGTH = 100
    _DRAW_FAILURE_CONTEXT_TTL_SECONDS = 600
    _DRAW_FAILURE_CONTEXT_LIMIT = 5
    _PIC_PROMPT_TAG_PATTERN = re.compile(
        r'<pic\b[^>]*\bprompt=(?P<quote>["\'])(?P<prompt>.*?)(?P=quote)[^>]*?/?>\s*(?:</pic>)?',
        flags=re.DOTALL | re.IGNORECASE,
    )
    _PIC_PROMPT_UNCLOSED_PATTERN = re.compile(
        r'<pic\b(?P<prefix>[^>]*\bprompt=)(?P<quote>["\'])(?P<prompt>.*?)</pic>',
        flags=re.DOTALL | re.IGNORECASE,
    )

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # ====== 1. 获取持久化数据目录 ======
        self.data_dir = self._get_persistent_dir()
        logger.info(f"[ComfyUI] 📂 数据目录: {self.data_dir}")
        
        # ====== 2. 初始化目录结构 ======
        self._init_data_directories()
        
        # ====== 3. 设置路径变量 ======
        self.workflow_dir = self.data_dir / "workflow"
        self.output_dir = self.data_dir / "output"
        self.sensitive_words_path = self.data_dir / "sensitive_words.json"
        
        # ====== 4. 更新 UI 配置 ======
        
        self.cfg = Config.from_mapping(config, data_dir=self.data_dir)
        control = self.cfg.control
        llm = self.cfg.llm

        self._recent_draw_failures = {}

        self.multi_image_mode = llm.multi_image_mode
        logger.info(f"[ComfyUI] 多图模式: {'开启' if self.multi_image_mode else '关闭'}")
        self.discard_prompt_from_history = llm.discard_prompt_from_history
        self.force_draw_when_no_prompt = llm.force_draw_when_no_prompt
        self.target_image_count = llm.target_image_count
        self.lora_control_enabled = llm.lora_control.enabled
        if self.discard_prompt_from_history:
            logger.info("[ComfyUI] 绘图提示词历史丢弃: 开启")
        if self.force_draw_when_no_prompt:
            logger.info(
                f"[ComfyUI] 强制画图兜底: 开启（回复正文不少于 {self._FORCE_DRAW_MIN_REPLY_LENGTH} 字时生效）",
            )
        if self.target_image_count > 1:
            logger.info(f"[ComfyUI] 目标出图数量: {self.target_image_count}")

        self.access = AccessController(control, load_lexicon(self.sensitive_words_path))
        word_count = sum(len(words) for words in self.access.lexicon.values())
        logger.info(f"[ComfyUI] 敏感词库已加载: {word_count} 个词条")
        logger.info(
            f"[ComfyUI] 超级管理员: {len(control.admin_ids)} 个 | "
            f"白名单群: {len(control.whitelist_group_ids)} 个"
        )
        if self.access.lockdown:
            logger.warning("[ComfyUI] 绘图功能全局锁定已启用，仅超级管理员可用")
        logger.info(
            f"[ComfyUI] 锁定命令开关: "
            f"{'开启' if control.lockdown_command_enabled else '关闭'}"
        )
        
        self.api = ComfyUI(self.cfg)
        self.drawer = Drawer(self.api, self.access, self.output_dir)
        self.cmd_ctx = CommandContext(
            self.cfg, self.access, self.api, self.drawer, self.context
        )
        logger.info("[ComfyUI] ComfyUI API 初始化成功")

    # ====== 获取持久化目录 ======
    def _get_persistent_dir(self) -> Path:
        """获取插件的持久化数据目录"""
        return StarTools.get_data_dir("astrbot_plugin_comfy_runner")

    # ====== 初始化目录结构 ======
    def _init_data_directories(self):
        """初始化持久化目录，首次安装时复制默认文件"""
        workflow_dir = self.data_dir / "workflow"
        output_dir = self.data_dir / "output"
        
        workflow_dir.mkdir(exist_ok=True)
        output_dir.mkdir(exist_ok=True)
        
        # 复制默认敏感词文件
        sensitive_dst = self.data_dir / "sensitive_words.json"
        sensitive_src = PLUGIN_DIR / "sensitive_words.json"
        if not sensitive_dst.exists() and sensitive_src.exists():
            try:
                shutil.copy2(sensitive_src, sensitive_dst)
                logger.info("[ComfyUI] 📋 已复制默认敏感词文件")
            except Exception as e:
                logger.error(f"[ComfyUI] 复制敏感词文件失败: {e}")

    # ====== 更新 Schema ======

    # ====== 权限检查（返回原因）======




    @staticmethod
    def _is_workflow_aux_file(filename: str) -> bool:
        return filename.endswith(".steps.json") or filename.endswith(".lora.json")









    async def initialize(self):
        self.context.activate_llm_tool("comfyui_txt2img")
        logger.info("[ComfyUI] 🎨 插件初始化完成，LLM 工具已激活")

    # ====== 核心绘图逻辑 ======

    # ===== 探针：测试 event.send() 是否触发 on_decorating_result =====
    @filter.command("comfy_probe_send")
    async def cmd_probe_send(self, event: AstrMessageEvent):
        """探测 event.send() 是否会触发 on_decorating_result"""
        user_id = str(event.get_sender_id())
        if user_id not in self.access.admin_ids:
            yield event.plain_result("🚫 仅管理员可用")
            return

        # 设置一个标记
        event.set_extra("_probe_send_test", True)
        logger.info("[探针] 已设置 _probe_send_test 标记")

        # 通过 event.send 发送一条消息
        try:
            await event.send(event.plain_result("探针消息：这是通过 event.send() 发出的"))
            logger.info("[探针] event.send() 调用完成")
        except Exception as e:
            logger.error(f"[探针] event.send() 失败: {e}")

        yield event.plain_result("探针完成，请检查日志中是否出现 '[探针] on_decorating_result 被 event.send 触发'")
    # ===== 探针结束 =====
    @filter.command("comfy帮助")
    async def cmd_comfyui_help(self, event: AstrMessageEvent):
        async for result in commands.help_command(self.cmd_ctx, event):
            yield result
    @filter.command("comfy_test_send2")
    async def cmd_test_send2(self, event: AstrMessageEvent):
        """测试主动发送 - 第二轮"""
    
        user_id = str(event.get_sender_id())
        if user_id not in self.access.admin_ids:
            yield event.plain_result("🚫 仅管理员可用")
            return
    
        from astrbot.api.message_components import Plain
    
        results = []
    
        # 测试 1: event.send 传入 MessageEventResult
        try:
            msg_result = event.plain_result("测试1: send + plain_result")
            await event.send(msg_result)
            results.append("✅ event.send(event.plain_result(...)) 可用")
        except Exception as e:
            results.append(f"❌ send+plain_result: {type(e).__name__}: {e}")
    
        # 测试 2: event.send 传入 chain_result
        try:
            msg_result = event.chain_result([Plain("测试2: send + chain_result")])
            await event.send(msg_result)
            results.append("✅ event.send(event.chain_result([...])) 可用")
        except Exception as e:
            results.append(f"❌ send+chain_result: {type(e).__name__}: {e}")
    
        # 测试 3: event.send_message 带 target
        try:
            await event.send_message(
                event.unified_msg_origin,
                event.chain_result([Plain("测试3: send_message 两参数")])
            )
            results.append("✅ event.send_message(origin, chain_result) 可用")
        except Exception as e:
            results.append(f"❌ send_message两参数: {type(e).__name__}: {e}")
    
        # 测试 4: context.send_message 用 chain_result
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                event.chain_result([Plain("测试4: context + chain_result")])
            )
            results.append("✅ context.send_message(origin, chain_result) 可用")
        except Exception as e:
            results.append(f"❌ context+chain_result: {type(e).__name__}: {e}")
    
        # 测试 5: 查看 MessageChain 是否存在
        try:
            from astrbot.api.message_components import MessageChain
            chain = MessageChain([Plain("测试5: MessageChain")])
            await event.send(chain)
            results.append("✅ event.send(MessageChain([...])) 可用")
        except ImportError:
            results.append("ℹ️ MessageChain 不可导入")
        except Exception as e:
            results.append(f"❌ MessageChain: {type(e).__name__}: {e}")
    
        # 测试 6: 直接查看 send 的签名
        try:
            import inspect
            sig = inspect.signature(event.send)
            results.append(f"ℹ️ event.send 签名: {sig}")
        except Exception as e:
            results.append(f"ℹ️ 无法获取签名: {e}")
    
        # 测试 7: 查看 send_message 签名
        try:
            import inspect
            sig = inspect.signature(event.send_message)
            results.append(f"ℹ️ event.send_message 签名: {sig}")
        except Exception as e:
            results.append(f"ℹ️ 无法获取签名: {e}")
    
        yield event.plain_result("\n".join(["📋 发送测试结果 v2：", ""] + results))
    @filter.command("api_test_all")
    async def cmd_api_test_all(self, event: AstrMessageEvent):
        """一次性测试所有API和命令相关功能"""
    
        import inspect
        results = []
    
        results.append("=" * 50)
        results.append("🔍 ASTRBOT API 完整探测报告")
        results.append("=" * 50)
    
        # ========== 1. filter 模块所有成员 ==========
        results.append("\n📦 【filter 模块成员】")
        results.append("-" * 40)
        try:
            for name in sorted(dir(filter)):
                if name.startswith('_'):
                    continue
                try:
                    obj = getattr(filter, name)
                    if callable(obj):
                        try:
                            sig = str(inspect.signature(obj))
                            results.append(f"  ✅ filter.{name}{sig}")
                        except:
                            results.append(f"  ✅ filter.{name}() [callable]")
                    else:
                        results.append(f"  📌 filter.{name} = {repr(obj)[:30]}")
                except Exception as e:
                    results.append(f"  ❌ filter.{name}: {e}")
        except Exception as e:
            results.append(f"  ❌ 探测失败: {e}")

        # ========== 2. event 对象成员 ==========
        results.append("\n📦 【event 常用成员】")
        results.append("-" * 40)
    
        event_attrs = [
            'message_str', 'get_sender_id', 'get_sender_name', 
            'unified_msg_origin', 'session_id', 'message_obj',
            'plain_result', 'chain_result', 'send', 'send_message',
            'get_messages', 'is_private', 'is_group'
        ]
        for attr in event_attrs:
            try:
                obj = getattr(event, attr, None)
                if obj is None:
                    results.append(f"  ❌ event.{attr} 不存在")
                elif callable(obj):
                    try:
                        sig = str(inspect.signature(obj))
                        results.append(f"  ✅ event.{attr}{sig}")
                    except:
                        results.append(f"  ✅ event.{attr}() [callable]")
                else:
                    val = repr(obj)[:30]
                    results.append(f"  📌 event.{attr} = {val}")
            except Exception as e:
                results.append(f"  ❌ event.{attr}: {e}")

        # ========== 3. context 对象成员 ==========
        results.append("\n📦 【context 常用成员】")
        results.append("-" * 40)
    
        context_attrs = [
            'send_message', 'get_config', 'register_command',
            'get_all_stars', 'get_platform', 'llm_request'
        ]
        for attr in context_attrs:
            try:
                obj = getattr(self.context, attr, None)
                if obj is None:
                    results.append(f"  ❌ context.{attr} 不存在")
                elif callable(obj):
                    try:
                        sig = str(inspect.signature(obj))
                        results.append(f"  ✅ context.{attr}{sig}")
                    except:
                        results.append(f"  ✅ context.{attr}() [callable]")
                else:
                    results.append(f"  📌 context.{attr} = {type(obj).__name__}")
            except Exception as e:
                results.append(f"  ❌ context.{attr}: {e}")

        # ========== 4. 探测所有 context 成员 ==========
        results.append("\n📦 【context 全部成员】")
        results.append("-" * 40)
        try:
            for name in sorted(dir(self.context)):
                if name.startswith('_'):
                    continue
                try:
                    obj = getattr(self.context, name)
                    obj_type = type(obj).__name__
                    results.append(f"  • {name} ({obj_type})")
                except:
                    results.append(f"  • {name}")
        except Exception as e:
            results.append(f"  ❌ 探测失败: {e}")

        # ========== 5. 可用的消息组件 ==========
        results.append("\n📦 【消息组件探测】")
        results.append("-" * 40)
    
        components = [
            'Plain', 'Image', 'At', 'AtAll', 'Reply', 
            'Face', 'Voice', 'Video', 'File', 'MessageChain'
        ]
        for comp in components:
            try:
                exec(f"from astrbot.api.message_components import {comp}")
                results.append(f"  ✅ {comp} 可导入")
            except ImportError:
                results.append(f"  ❌ {comp} 不可导入")
            except Exception as e:
                results.append(f"  ❌ {comp}: {e}")

        # ========== 6. 其他可用模块 ==========
        results.append("\n📦 【其他模块探测】")
        results.append("-" * 40)
    
        modules = [
            ('astrbot.api', 'logger'),
            ('astrbot.api.event', 'filter'),
            ('astrbot.api.event', 'AstrMessageEvent'),
            ('astrbot.api.star', 'Context'),
            ('astrbot.api.star', 'Star'),
            ('astrbot.api.star', 'register'),
        ]
        for module, name in modules:
            try:
                exec(f"from {module} import {name}")
                results.append(f"  ✅ from {module} import {name}")
            except Exception as e:
                results.append(f"  ❌ {module}.{name}: {e}")

        # ========== 7. 当前事件信息 ==========
        results.append("\n📦 【当前事件信息】")
        results.append("-" * 40)
    
        try:
            results.append(f"  • message_str: {event.message_str[:50]}")
        except:
            pass
        try:
            results.append(f"  • sender_id: {event.get_sender_id()}")
        except:
            pass
        try:
            results.append(f"  • unified_msg_origin: {event.unified_msg_origin}")
        except:
            pass
        try:
            results.append(f"  • session_id: {event.session_id}")
        except:
            pass

        results.append("\n" + "=" * 50)
        results.append("🔍 探测完成")
        results.append("=" * 50)

        # 输出结果
        full_result = "\n".join(results)
    
        # 如果太长，分段发送
        if len(full_result) > 2000:
            chunks = [results[i:i+30] for i in range(0, len(results), 30)]
            for i, chunk in enumerate(chunks):
                yield event.plain_result(f"📋 第{i+1}部分:\n" + "\n".join(chunk))
        else:
            yield event.plain_result(full_result)

    @filter.command("违禁级别", aliases={"banlevel", "敏感级别"})
    async def cmd_set_policy(self, event: AstrMessageEvent):
        async for result in commands.set_policy(self.cmd_ctx, event):
            yield result

    @filter.command("comfy_lock", aliases=["全局锁定", "锁图", "绘图锁定"])
    async def cmd_comfy_lock(self, event: AstrMessageEvent):
        async for result in commands.toggle_lockdown(self.cmd_ctx, event):
            yield result

    @filter.command("comfy_ls")
    async def cmd_comfy_list(self, event: AstrMessageEvent):
        async for result in commands.list_workflows(self.cmd_ctx, event):
            yield result

    @filter.command("comfy_use")
    async def cmd_comfy_use(self, event: AstrMessageEvent):
        async for result in commands.use_workflow(self.cmd_ctx, event):
            yield result

    @filter.command("comfy_save")
    async def cmd_comfy_save(self, event: AstrMessageEvent):
        async for result in commands.save_workflow(self.cmd_ctx, event):
            yield result
    @filter.command("comfy_add")
    async def cmd_comfy_add(self, event: AstrMessageEvent):
        async for result in commands.add_steps(self.cmd_ctx, event):
            yield result


    @filter.command("当前工作流", aliases=["comfy_current", "当前wf"])
    async def cmd_comfy_current(self, event: AstrMessageEvent):
        async for result in commands.current_workflow(self.cmd_ctx, event):
            yield result

    @filter.command("测试lora", aliases=["lora_test", "lora测试"])
    async def cmd_lora_test(self, event: AstrMessageEvent):
        async for result in commands.lora_test(self.cmd_ctx, event):
            yield result

    @filter.command("重绘", aliases=["重抽", "reroll"])
    async def cmd_reroll(self, event: AstrMessageEvent):
        async for result in commands.paint(
            self.cmd_ctx,
            event,
            direct_send=True,
            strip_at=True,
            usage="📖 用法: /重绘 <提示词>\n示例: /重绘 1girl, silver hair, cinematic lighting",
        ):
            yield result

    @filter.command("画图", aliases=["绘画"])
    async def cmd_paint(self, event: AstrMessageEvent):
        async for result in commands.paint(self.cmd_ctx, event, direct_send=False):
            yield result

    @filter.command("画图no")
    async def cmd_paint_no(self, event: AstrMessageEvent):
        async for result in commands.paint(self.cmd_ctx, event, direct_send=True):
            yield result

    # ====== 辅助方法 ======


    # ====== 修改提取逻辑 ======

    # ====== 自动绘图逻辑保持不变 ======
    

    @filter.on_llm_request(priority=100)
    async def inject_system_prompt(self, event: AstrMessageEvent, req):
        await commands.on_llm_request(self.cmd_ctx, event, req)

    @filter.on_llm_response(priority=70)
    async def _extract_prompt_before_filter(self, event: AstrMessageEvent, resp):
        await commands.on_llm_response(self.cmd_ctx, event, resp)

    @filter.on_decorating_result(priority=99)
    async def _auto_paint_from_llm(self, event: AstrMessageEvent):
        await commands.stage_build_chain(self.cmd_ctx, event)

    @filter.on_decorating_result(priority=5)
    async def _cleanup_history_prompts(self, event: AstrMessageEvent):
        await commands.stage_clean_history(self.cmd_ctx, event)

    @filter.on_decorating_result(priority=10)
    async def _send_multi_image_results(self, event: AstrMessageEvent):
        await commands.stage_send_images(self.cmd_ctx, event)

    @llm_tool(name="comfyui_txt2img")
    async def comfyui_txt2img(self, event: AstrMessageEvent, prompt: str = None, text: str = None, direct_send: bool = False) -> MessageEventResult:
        async for result in commands.draw_tool(self.cmd_ctx, event, prompt, text, direct_send):
            yield result
