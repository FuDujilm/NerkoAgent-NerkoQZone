"""Compatibility API layer used by the legacy MaiZone implementation."""

from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from nekro_agent.api import core as _agent_core
except Exception:  # pragma: no cover - running outside Nerko Agent
    _agent_core = None

try:
    from nekro_agent.core.config import config as _nekro_config
except Exception:  # pragma: no cover - running outside Nerko Agent
    _nekro_config = None

from src.plugin_system.core import component_registry


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
        # plugin_config is a plugin-specific config object in nekro; support
        # dotted keys used by the legacy MaiZone implementation.
        if plugin_config is None:
            return default

        attr = key.replace('.', '_') if isinstance(key, str) else key
        try:
            return getattr(plugin_config, attr, default)
        except Exception:
            return default


class _LLMAdapter:
    """Thin adapter around Nerko Agent's model sandbox."""

    _PLUGIN_KEY = "MaizonePlugin"

    def _get_plugin(self):
        return component_registry.get_plugin(self._PLUGIN_KEY)

    def get_available_models(self) -> Dict[str, Dict[str, Any]]:
        """Return chat models discoverable via the Nerko runtime."""

        if _agent_core is None:
            return {}

        list_models = getattr(_agent_core, "list_model_group_models", None)
        if not callable(list_models):
            return {}

        config = component_registry.get_plugin_config(self._PLUGIN_KEY)
        group_name = getattr(config, "TEXT_MODEL_GROUP", None) if config else None

        try:
            if group_name:
                result = list_models(group_name, model_type="chat")
            else:
                result = list_models(model_type="chat")
        except TypeError:
            try:
                result = list_models(group_name) if group_name else list_models()
            except Exception:
                return {}
        except Exception:
            return {}

        if isinstance(result, dict):
            return result

        return {}

    async def generate_with_model(
        self,
        *,
        prompt: str,
        model_config: Optional[Dict[str, Any]] = None,
        request_type: str = "story.generate",
        temperature: float = 0.3,
        max_tokens: int = 1024,
        **kwargs: Any,
    ):
        plugin = self._get_plugin()
        if plugin is None or _agent_core is None:
            return False, "", "", ""

        sandbox_call_model = getattr(_agent_core, "sandbox_call_model", None)
        if not callable(sandbox_call_model):
            return False, "", "", ""

        call_payload: Dict[str, Any] = {
            "prompt": prompt,
            "request_type": request_type,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if model_config:
            call_payload["model_config"] = model_config
        call_payload.update(kwargs)

        try:
            result = await sandbox_call_model(plugin, **call_payload)
        except Exception:
            return False, "", "", ""

        # Result can be either a tuple (success, text, reasoning, model)
        # or a mapping with the same semantic fields.
        if isinstance(result, (list, tuple)) and len(result) >= 4:
            return bool(result[0]), result[1], result[2], result[3]

        if isinstance(result, dict):
            return (
                bool(result.get("success", False)),
                result.get("output")
                or result.get("text")
                or result.get("message", ""),
                result.get("reasoning", ""),
                result.get("model") or result.get("model_name", ""),
            )

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


llm_api = _LLMAdapter()
config_api = _ConfigAPI()
person_api = _PersonStub()
generator_api = _GeneratorStub()
emoji_api = _EmojiStub()
database_api = _DatabaseStub()
