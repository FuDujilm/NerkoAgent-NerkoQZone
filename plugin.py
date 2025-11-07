"""
plugin.py
"""

from pathlib import Path
import asyncio
from typing import Any, Optional, List

from nekro_agent.api.plugin import NekroPlugin, ConfigBase, ExtraField, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.api import core
from pydantic import Field

# 导入原插件实现（用于任务与工具函数）
from .scheduled_tasks import FeedMonitor, ScheduleSender
from . import utils as qzone_utils

import base64
import random
import re
from pathlib import Path
from typing import Literal, Optional, Dict, Any

import aiofiles
import magic
from httpx import AsyncClient, Timeout
from pydantic import Field

from nekro_agent.api import core
from nekro_agent.api.plugin import (
    ConfigBase,
    ExtraField,
    NekroPlugin,
    SandboxMethodType,
)
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.core import logger
from nekro_agent.core.config import config as global_config
from nekro_agent.services.agent.creator import ContentSegment, OpenAIChatMessage
from nekro_agent.services.agent.openai import gen_openai_chat_response
from nekro_agent.tools.common_util import limited_text_output
from nekro_agent.tools.path_convertor import convert_to_host_path


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

