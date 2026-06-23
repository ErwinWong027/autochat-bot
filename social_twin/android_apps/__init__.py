from .momo import MomoConnector
from .qianshou import QianshouConnector
from .red import RedConnector
from .tantan import TantanConnector
from .wechat import WeChatConnector

REGISTRY: dict[str, type] = {
    "wechat": WeChatConnector,
    "tantan": TantanConnector,
    "momo": MomoConnector,
    "red": RedConnector,
    "qianshou": QianshouConnector,
}

__all__ = [
    "WeChatConnector",
    "TantanConnector",
    "MomoConnector",
    "RedConnector",
    "QianshouConnector",
    "REGISTRY",
]
