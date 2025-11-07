from enum import Enum
from typing import Any, Dict


class ActionActivationType(Enum):
    KEYWORD = 'keyword'
    MANUAL = 'manual'


class BaseAction:
    """最小兼容 BaseAction，用于让旧插件类能被导入但不承担完整运行时行为。"""

    action_name = "base_action"

    def __init__(self, *args, **kwargs):
        self.action_data = {}
        self.chat_stream = None

    # 兼容方法（尽量返回安全的默认值）
    def get_config(self, key: str, default: Any = None) -> Any:
        return default

    async def send_text(self, content: str, typing: bool = False):
        return True

    async def store_action_info(self, **kwargs):
        return True

    @classmethod
    def get_action_info(cls):
        return {
            'name': getattr(cls, 'action_name', cls.__name__),
        }


class BaseCommand:
    def __init__(self, *args, **kwargs):
        pass

    @classmethod
    def get_command_info(cls) -> Dict:
        return {'name': getattr(cls, 'command_name', cls.__name__)}


class ComponentInfo:
    pass
