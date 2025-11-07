"""简易兼容层：导出老插件期望的 apis 对象（llm_api, config_api, person_api, generator_api, emoji_api, database_api）

这些实现尽量在可用时代理到 nekro_agent 的相应模块，否则提供安全的空实现以避免导入错误。
"""
from typing import Any, Dict

try:
    from nekro_agent.core.config import config as _nekro_config
except Exception:
    _nekro_config = None


class _ConfigAPI:
    def get_global_config(self, key: str, default: Any = None) -> Any:
        if _nekro_config is None:
            return default

        # Support dotted keys like 'bot.qq_account'. Try attribute access first,
        # then mapping access. If any step fails, return default.
        parts = key.split('.') if isinstance(key, str) else [key]
        obj = _nekro_config
        try:
            for part in parts:
                if hasattr(obj, part):
                    obj = getattr(obj, part)
                elif isinstance(obj, dict) and part in obj:
                    obj = obj[part]
                else:
                    # try mapping-style access
                    try:
                        obj = obj[part]
                    except Exception:
                        return default
            return obj
        except Exception:
            return default

    def get_plugin_config(self, plugin_config, key: str, default: Any = None) -> Any:
        # plugin_config is a plugin-specific config object in nekro; try attribute access
        try:
            return getattr(plugin_config, key, default)
        except Exception:
            return default


class _LLMStub:
    def get_available_models(self) -> Dict:
        return {}

    async def generate_with_model(self, *args, **kwargs):
        return False, "", "", ""


class _PersonStub:
    def get_person_id_by_name(self, name: str) -> str:
        return ""

    async def get_person_value(self, person_id: str, key: str, default=None):
        return default

    def get_person_id(self, key: str, value: str) -> str:
        return ""


class _GeneratorStub:
    async def generate_reply(self, *args, **kwargs):
        return False, None


class _EmojiStub:
    async def get_by_description(self, desc: str):
        return None


class _DatabaseStub:
    pass


llm_api = _LLMStub()
config_api = _ConfigAPI()
person_api = _PersonStub()
generator_api = _GeneratorStub()
emoji_api = _EmojiStub()
database_api = _DatabaseStub()
