
"""Package exports for the QZone Nerko plugin."""

from .plugin import (
    check_feeds,
    plugin,
    qzone_diag,
    qzone_live_post,
    qzone_live_post_llm,
    test_model_generation,
    test_napcat_connection,
)

__all__ = [
    "plugin",
    "qzone_diag",
    "check_feeds",
    "qzone_live_post",
    "qzone_live_post_llm",
    "test_napcat_connection",
    "test_model_generation",
]


def _expose_legacy_global(name: str, obj: object) -> None:
    """Expose legacy entry points as implicit globals for `/exec` scripts.

    Older MaiBot 脚本常通过 `/exec foo` 直接引用函数名称而不显式导入模
    块。如果运行环境在执行脚本前仅注入内置命名空间，`foo` 未注册到
    `builtins` 就会触发 `NameError`。为了兼容这些历史脚本，我们把重
    要的入口函数挂载到 `builtins`，并在目标名字已存在时保持原样。
    """

    try:
        import builtins

        if getattr(builtins, name, None) is None:
            setattr(builtins, name, obj)
    except Exception:  # pragma: no cover - 极端情况下忽略构建期环境异常
        pass


for _legacy_name in __all__:
    if _legacy_name != "plugin":  # 插件实例无需注入 builtins
        _expose_legacy_global(_legacy_name, globals()[_legacy_name])

del _legacy_name
