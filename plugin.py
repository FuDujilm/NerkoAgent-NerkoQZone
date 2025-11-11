"""Nerko Agent plugin entry point for the Maizone (QZone) integration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, List, Optional

from nekro_agent.api import core
from nekro_agent.api.plugin import ConfigBase, NekroPlugin, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from pydantic import Field

from . import utils as qzone_utils
from .qzone_api import ensure_qzone_api
from .scheduled_tasks import (
    FeedMonitor,
    ScheduleSender,
    _load_processed_list,
    _save_processed_list,
)
from src.plugin_system.apis import config_api, llm_api
from src.plugin_system.core import component_registry


plugin = NekroPlugin(
    name="Maizone (QZone)",
    module_name="qzone_sender",
    description="让Nerko Agent实现QQ空间点赞、评论、发说说",
    version="2.4.5",
    author="limeneko",
    url="",
    is_package=True,   # 如果你的插件是一个复杂的包结构，可能需要调整
)


@plugin.mount_config()
class QzoneConfig(ConfigBase):
    """插件配置（仅列出常用字段，必要时可补充）"""
    # plugin
    plugin_http_host: str = Field(default="127.0.0.1", title="Napcat HTTP Host")
    plugin_http_port: str = Field(default="9999", title="Napcat HTTP Port")
    plugin_napcat_token: str = Field(default="", title="Napcat Token")

    # models
    # 模型配置 - 保持向后兼容的同时提供模型组选择
    group_models: str = Field(
        default="--- 模型配置 ---",
        title="Model configuration",
        description="Model configuration (visual separator for model-related settings)"
    )

    # 可从系统的模型组中选择一个模型组用于文本生成（若设置则优先使用）
    TEXT_MODEL_GROUP: str = Field(
        default="default-chat",
        title="Text model group",
        description="Select a system model group for text generation (if set it will be used preferentially)",
        json_schema_extra={"ref_model_groups": True, "required": False, "model_type": "chat"},
    )

    # 兼容旧配置 / other settings are defined below with English titles
    models_text_model: str = Field(default="replyer", title="Text model", description="Legacy text model name")
    models_api_key: str = Field(default="", title="API Key", description="API key for model providers")
    models_show_prompt: bool = Field(default=False, title="Show prompt", description="Whether to show generated prompts in logs/UI")
    send_enable_image: bool = Field(default=False, title="Enable image sending", description="Enable attaching/generated images when sending posts")
    send_image_mode: str = Field(default="random", title="Image mode", description="Image mode: only_ai, only_emoji, or random")
    send_ai_probability: float = Field(default=0.5, title="AI probability", description="When mode is random, probability to use AI for image generation (0.0-1.0)")
    send_image_number: int = Field(default=1, title="Number of images", description="Number of images to generate/send (1-4)")
    send_history_number: int = Field(default=5, title="History count", description="Number of past posts to include when building history prompts")
    models_image_provider: str = Field(default="SiliconFlow", title="Image provider", description="Provider used for AI image generation")
    models_image_model: str = Field(default="Kwai-Kolors/Kolors", title="Image model", description="Model name for AI image generation")
    models_image_ref: bool = Field(default=False, title="Enable reference image", description="Allow sending reference image for AI prompts")
    read_permission: List[str] = Field(default_factory=list, title="Read permission list", description="List of identifiers allowed/denied for read actions")
    read_permission_type: str = Field(default="blacklist", title="Read permission type", description="Permission mode: blacklist or whitelist")
    read_read_number: int = Field(default=5, title="Read number", description="Default number of posts to read when performing read actions")
    read_like_possibility: float = Field(default=1.0, title="Like probability", description="Probability to like posts during automated read/monitor actions (0.0-1.0)")
    read_comment_possibility: float = Field(default=1.0, title="Comment probability", description="Probability to comment on posts during automated read/monitor actions (0.0-1.0)")
    monitor_enable_auto_monitor: bool = Field(default=False, title="Enable auto-monitor", description="Whether to start automatic monitoring of friends' posts")
    monitor_enable_auto_reply: bool = Field(default=False, title="Enable auto-reply", description="Whether to auto-reply to comments/posts when monitoring")
    monitor_self_readnum: int = Field(default=5, title="Self read number", description="Number of your own latest posts to include when monitoring")
    monitor_interval_minutes: int = Field(default=15, title="Monitor interval (minutes)", description="How often to run the monitor loop in minutes")
    schedule_enable_schedule: bool = Field(default=False, title="Enable scheduled sending", description="Whether scheduled posting is enabled")
    schedule_schedule_times: List[str] = Field(default_factory=lambda: ["08:00", "20:00"], title="Scheduled times", description="List of times of day to run scheduled posts")
    schedule_fluctuation_minutes: int = Field(default=0, title="Schedule fluctuation (minutes)", description="Random jitter in minutes to apply to scheduled times")
    schedule_random_topic: bool = Field(default=True, title="Schedule random topic", description="Whether to pick a random topic for scheduled posts")
    schedule_fixed_topics: List[str] = Field(default_factory=lambda: ["日常生活", "心情分享"], title="Fixed topics", description="Fallback topics for scheduled posts (e.g. ['Daily life','Mood sharing'])") 


_monitor_instance: Optional[FeedMonitor] = None
_scheduler_instance: Optional[ScheduleSender] = None
_monitor_task: Optional[asyncio.Task] = None
_scheduler_task: Optional[asyncio.Task] = None


class AdapterPlugin:
    """适配器：为原始代码提供 get_config(section.field, default) 接口"""

    def __init__(self, nekro_plugin: NekroPlugin):
        self.nekro_plugin = nekro_plugin

    def get_config(self, key: str, default: Any = None) -> Any:
        section, _, field = key.partition('.')
        attr = f"{section}_{field}" if field else section
        try:
            cfg = self.nekro_plugin.get_config(QzoneConfig)
            return getattr(cfg, attr, default)
        except Exception:
            return default


# 将 Nerko 插件暴露给兼容层，供旧代码查询配置与模型
component_registry.register_plugin(
    "MaizonePlugin",
    plugin_factory=lambda: plugin,
    config_model=QzoneConfig,
)
@plugin.mount_init_method()
async def initialize_plugin():
    """插件初始化：根据配置启动监控/定时任务（适配原有实现）。"""
    global _monitor_instance, _scheduler_instance, _monitor_task, _scheduler_task

    core.logger.info(f"初始化插件 {plugin.name}...")
    cfg = plugin.get_config(QzoneConfig)
    adapter = AdapterPlugin(plugin)

    try:
        if cfg.monitor_enable_auto_monitor:
            _monitor_instance = FeedMonitor(adapter)
            _monitor_task = asyncio.create_task(_monitor_instance.start())
            core.logger.info("已启动 QZone 监控任务")
        else:
            core.logger.info("QZone 监控任务未启用")

        if cfg.schedule_enable_schedule:
            _scheduler_instance = ScheduleSender(adapter)
            _scheduler_task = asyncio.create_task(_scheduler_instance.start())
            core.logger.info("已启动 QZone 定时发送任务")
        else:
            core.logger.info("QZone 定时发送任务未启用")

    except Exception as e:
        core.logger.error(f"初始化 qzone_sender 失败: {e}")


@plugin.mount_cleanup_method()
async def cleanup_plugin():
    """插件清理：停止并取消后台任务"""
    global _monitor_instance, _scheduler_instance, _monitor_task, _scheduler_task
    core.logger.info(f"清理插件 {plugin.name}...")
    try:
        if _monitor_instance:
            await _monitor_instance.stop()
        if _monitor_task and not _monitor_task.done():
            _monitor_task.cancel()
            try:
                await _monitor_task
            except asyncio.CancelledError:
                pass
        if _scheduler_instance:
            await _scheduler_instance.stop()
        if _scheduler_task and not _scheduler_task.done():
            _scheduler_task.cancel()
            try:
                await _scheduler_task
            except asyncio.CancelledError:
                pass
    except Exception as e:
        core.logger.error(f"清理 qzone_sender 失败: {e}")


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, name="发送说说")
async def send_feed_tool(_ctx: AgentCtx, message: str) -> str:
    """工具方法：发送一条说说（使用插件配置的图片与 AI 选项）"""
    try:
        cfg = plugin.get_config(QzoneConfig)
        image_dir = str(Path(__file__).parent.resolve() / "images")
        enable_image = cfg.send_enable_image
        image_mode = cfg.send_image_mode
        ai_prob = cfg.send_ai_probability
        image_num = cfg.send_image_number

        ok = await qzone_utils.send_feed(message, image_dir, enable_image, image_mode, ai_prob, image_num)
        return "success" if ok else "failed"
    except Exception as e:
        core.logger.error(f"send_feed_tool 失败: {e}")
        return "error"


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, name="读取说说")
async def read_feed_tool(_ctx: AgentCtx, target_qq: str, num: int = 5) -> str:
    try:
        feeds = await qzone_utils.read_feed(target_qq, num)
        if isinstance(feeds, list):
            return str(feeds)
        return "[]"
    except Exception as e:
        core.logger.error(f"read_feed_tool 失败: {e}")
        return "error"


@plugin.mount_sandbox_method(SandboxMethodType.TEST, name="测试Napcat连接")
async def test_napcat_connection(_ctx: AgentCtx) -> str:
    """测试 Napcat HTTP 服务与 Cookie 刷新能力是否正常。"""
    try:
        qzone = await ensure_qzone_api()
        if qzone is None:
            return "failed: 无法创建 QzoneAPI，请检查 Napcat/Cookie 配置"

        uin = getattr(qzone, "uin", 0)
        nickname = getattr(qzone, "qq_nickname", "")
        parts = []
        if uin:
            parts.append(f"uin={uin}")
        if nickname:
            parts.append(f"昵称={nickname}")
        extra = "，".join(parts) if parts else ""
        return "success" + (f"（{extra}）" if extra else "")
    except Exception as e:
        core.logger.error(f"test_napcat_connection 失败: {e}")
        return f"error: {e}"


@plugin.mount_sandbox_method(SandboxMethodType.TEST, name="测试模型调用")
async def test_model_generation(_ctx: AgentCtx, prompt: str = "请简单介绍一下你自己") -> str:
    """调用配置的文本模型生成一句话，验证模型配置是否可用。"""

    try:
        models = llm_api.get_available_models()
        if not models:
            return "failed: 未找到可用的文本模型，请检查模型组/模型名称配置"

        model_name, model_cfg = next(iter(models.items()))
        success, output, _reasoning, used_model = await llm_api.generate_with_model(
            prompt=prompt,
            model_config=model_cfg,
            request_type="maizone.test",
            temperature=0.1,
            max_tokens=128,
        )

        if not success:
            return f"failed: 模型 {used_model or model_name} 调用失败"

        preview = (output or "").strip().replace("\n", " ")
        if len(preview) > 80:
            preview = preview[:77] + "..."
        return f"success: 使用 {used_model or model_name} 生成 → {preview or '（无输出）'}"
    except Exception as e:
        core.logger.error(f"test_model_generation 失败: {e}")
        return f"error: {e}"


async def _run_qzone_diag_async() -> str:
    """依次调用测试沙箱方法，返回简明诊断结果。"""

    napcat_result = await test_napcat_connection(None)
    model_result = await test_model_generation(None)

    lines = [f"Napcat: {napcat_result}", f"Model: {model_result}"]
    return "\n".join(lines)


def qzone_diag() -> str:
    """兼容旧版 `/exec qzone_diag()` 调试命令的同步入口。"""

    try:
        return asyncio.run(_run_qzone_diag_async())
    except RuntimeError as exc:  # pragma: no cover - 仅在已有事件循环时触发
        if "asyncio.run()" not in str(exc):
            raise

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run_qzone_diag_async())
    finally:
        loop.close()


async def _run_check_feeds_async() -> str:
    """兼容旧版 `/exec check_feeds()` 调试命令的异步实现。"""

    adapter = AdapterPlugin(plugin)
    monitor = FeedMonitor(adapter)
    processed = _load_processed_list()

    try:
        result = await monitor.check_feeds(processed)
    except Exception as exc:  # pragma: no cover - 网络/模型异常
        core.logger.error(f"check_feeds 运行失败: {exc}")
        return f"error: {exc}"
    finally:
        try:
            _save_processed_list(processed)
        except Exception as exc:  # pragma: no cover - IO 异常
            core.logger.warning(f"保存 check_feeds 处理记录失败: {exc}")

    if isinstance(result, tuple):
        success = bool(result[0])
        detail = ""
        if len(result) > 1 and result[1] is not None:
            detail = str(result[1])
    else:  # pragma: no cover - 理论上返回 tuple
        success = bool(result)
        detail = ""

    if success:
        if detail and detail != "success":
            return f"success: {detail}"
        return "success"

    return f"failed: {detail or '未知错误'}"


def check_feeds() -> str:
    """兼容旧版 `/exec check_feeds()` 调试命令的同步入口。"""

    try:
        return asyncio.run(_run_check_feeds_async())
    except RuntimeError as exc:  # pragma: no cover - 仅在已有事件循环时触发
        if "asyncio.run()" not in str(exc):
            raise

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run_check_feeds_async())
    finally:
        loop.close()


async def _run_qzone_live_post_async(content: Optional[str] = None) -> str:
    """向后兼容的实时发说说入口。"""

    try:
        cfg = plugin.get_config(QzoneConfig)
    except Exception as exc:  # pragma: no cover - 配置异常极少发生
        core.logger.error(f"获取插件配置失败: {exc}")
        return f"error: {exc}"

    if content:
        try:
            image_dir = str(Path(__file__).parent.resolve() / "images")
            ok = await qzone_utils.send_feed(
                content,
                image_dir,
                cfg.send_enable_image,
                cfg.send_image_mode,
                cfg.send_ai_probability,
                cfg.send_image_number,
            )
            return "success" if ok else "failed: 发送说说失败"
        except Exception as exc:  # pragma: no cover - 网络/IO 异常
            core.logger.error(f"qzone_live_post 发送自定义说说失败: {exc}")
            return f"error: {exc}"

    adapter = AdapterPlugin(plugin)
    scheduler = ScheduleSender(adapter)

    try:
        result = await scheduler.send_scheduled_feed()
    except Exception as exc:  # pragma: no cover - 捕获定时任务内部异常
        core.logger.error(f"qzone_live_post 生成定时说说失败: {exc}")
        return f"error: {exc}"

    if isinstance(result, tuple):
        success = bool(result[0])
        detail_msg = result[1] if len(result) > 1 else ""
        if not success:
            detail = str(detail_msg or "未知错误")
            return f"failed: {detail}"

    return "success"


def qzone_live_post(content: Optional[str] = None) -> str:
    """兼容旧版 `/exec qzone_live_post()` 调试命令的同步入口。"""

    try:
        return asyncio.run(_run_qzone_live_post_async(content))
    except RuntimeError as exc:  # pragma: no cover - 仅在已有事件循环时触发
        if "asyncio.run()" not in str(exc):
            raise

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run_qzone_live_post_async(content))
    finally:
        loop.close()


async def _run_qzone_live_post_llm_async(prompt: Optional[str] = None) -> str:
    """调用 LLM 生成说说内容但不发送，便于测试模型输出。"""

    adapter = AdapterPlugin(plugin)
    models = llm_api.get_available_models()
    if not models:
        return "failed: 未找到可用的文本模型，请检查模型组或模型名称配置"

    text_model = adapter.get_config("models.text_model", "replyer")
    model_config = models.get(text_model)
    chosen_model = text_model

    if model_config is None:
        chosen_model, model_config = next(iter(models.items()))

    final_prompt = prompt
    if not final_prompt:
        personality = config_api.get_global_config("personality.personality", "一个热情的QQ空间博主")
        style = config_api.get_global_config("personality.reply_style", "内容积极向上")
        final_prompt = (
            "请以第一人称撰写一段适合发布在QQ空间的短文，限制在80字以内。"
            f"保持语气：{style}。你的设定是：{personality}。"
            "避免使用表情、引号、括号或@，只输出正文。"
        )

    if adapter.get_config("models.show_prompt", False):
        core.logger.info(f"qzone_live_post_llm prompt → {final_prompt}")

    try:
        result = await llm_api.generate_with_model(
            prompt=final_prompt,
            model_config=model_config,
            request_type="maizone.live_post_llm_test",
            temperature=0.35,
            max_tokens=512,
        )
    except Exception as exc:  # pragma: no cover - sandbox 运行时异常
        core.logger.error(f"qzone_live_post_llm 调用模型失败: {exc}")
        return f"error: {exc}"

    success = False
    output = ""
    used_model = chosen_model

    if isinstance(result, tuple):
        if len(result) >= 1:
            success = bool(result[0])
        if len(result) >= 2 and result[1] is not None:
            output = str(result[1])
        if len(result) >= 4 and result[3]:
            used_model = str(result[3])
    else:  # pragma: no cover - 兼容非 tuple 返回
        output = str(result)

    if not success:
        return f"failed: 模型 {used_model or chosen_model} 调用失败"

    preview = (output or "").strip()
    if not preview:
        return f"failed: 模型 {used_model or chosen_model} 未返回内容"

    single_line = preview.replace("\n", " ")
    if len(single_line) > 80:
        single_line = single_line[:77] + "..."

    return f"success: 使用 {used_model or chosen_model} 生成 → {single_line}"


def qzone_live_post_llm(prompt: Optional[str] = None) -> str:
    """兼容旧版 `/exec qzone_live_post_llm()` 命令的同步入口。"""

    try:
        return asyncio.run(_run_qzone_live_post_llm_async(prompt))
    except RuntimeError as exc:  # pragma: no cover - 仅在已有事件循环时触发
        if "asyncio.run()" not in str(exc):
            raise

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run_qzone_live_post_llm_async(prompt))
    finally:
        loop.close()

