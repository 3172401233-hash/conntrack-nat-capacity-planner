"""NAT 连接跟踪容量规划器。

在时间序列上模拟 NAT 端口分配、TCP/UDP 状态转换、超时释放与安全等待期复用,
用于容量规划,不操作真实网络。
"""

from .models import (
    ConnectionSpec,
    PortRange,
    PublicAddress,
    Scenario,
    StaticMapping,
    TimeoutConfig,
)
from .simulator import SimReport, Simulator

__all__ = [
    "ConnectionSpec",
    "PortRange",
    "PublicAddress",
    "Scenario",
    "SimReport",
    "Simulator",
    "StaticMapping",
    "TimeoutConfig",
]

__version__ = "0.1.0"
