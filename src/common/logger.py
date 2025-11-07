try:
    # Prefer nekro_agent logger if available
    from nekro_agent.core import logger as _nekro_logger
except Exception:
    _nekro_logger = None

import logging


def get_logger(name: str):
    """返回与原项目兼容的 logger 对象。

    如果存在 nekro_agent 的 logger，返回其 child logger；否则返回标准 logging.getLogger。
    """
    if _nekro_logger is not None:
        try:
            return _nekro_logger.getChild(name)
        except Exception:
            return _nekro_logger
    return logging.getLogger(name)
