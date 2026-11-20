"""场景与配置的数据模型。

所有时间单位统一为秒(float)。端口范围闭区间 [start, end]。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

TCP_STATES = ("syn_sent", "established", "close_wait", "time_wait", "closed")
PROTOCOLS = ("tcp", "udp")


@dataclass
class TimeoutConfig:
    """各类超时与安全等待期(秒)。

    tcp_time_wait: TCP 连接活跃期结束后进入 TIME_WAIT 的保持时长。
    tcp_close:     非 established 的 TCP 半开/关闭中间态保持时长。
    udp:           UDP “连接”在最后一次活跃后的超时释放时长。
    reuse_safe_wait: 端口释放后到允许再次分配的安全等待期。
    """

    tcp_established: float = 7440.0
    tcp_time_wait: float = 60.0
    tcp_close: float = 10.0
    udp: float = 30.0
    reuse_safe_wait: float = 30.0


@dataclass
class PortRange:
    start: int
    end: int

    def __post_init__(self):
        if not (0 < self.start <= self.end <= 65535):
            raise ValueError("非法端口段: [%d, %d]" % (self.start, self.end))

    def __len__(self):
        return self.end - self.start + 1

    def contains(self, port):
        return self.start <= port <= self.end


@dataclass
class PublicAddress:
    """一个公网地址及其可分配端口池。"""

    ip: str
    port_ranges: List[PortRange]
    reserved_ports: Set[int] = field(default_factory=set)

    def capacity(self, static_ports=None):
        static_ports = static_ports or set()
        total = sum(len(r) for r in self.port_ranges)
        excluded = 0
        for p in self.reserved_ports | set(static_ports):
            if any(r.contains(p) for r in self.port_ranges):
                excluded += 1
        return total - excluded


@dataclass
class StaticMapping:
    """静态端口映射(端口转发), 占用 (公网地址, 公网端口, 协议)。

    静态映射占用的端口不可被普通动态连接抢占。
    """

    public_ip: str
    public_port: int
    internal_ip: str
    internal_port: int
    protocol: str = "tcp"

    def __post_init__(self):
        if self.protocol not in PROTOCOLS:
            raise ValueError("未知协议: %s" % self.protocol)


@dataclass
class ConnectionSpec:
    """一条连接的五元组与生命周期描述。

    start:    连接建立时刻(秒, 相对仿真起点)。
    duration: 活跃期时长。TCP established 指数据传输期; UDP 指到最后一个报文。
    state:    初始 TCP 状态(established/syn_sent/close_wait/time_wait);
              UDP 连接忽略该字段。
    requested_port: 可选, 连接期望使用的公网端口(如端口复用/固定源端口场景),
              与静态映射或保留端口冲突时计入 static_conflicts。
    hairpin:  是否为 hairpin(内网访问本 NAT 公网地址)连接。
    """

    conn_id: str
    protocol: str
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int
    state: str = "established"
    start: float = 0.0
    duration: float = 60.0
    requested_port: Optional[int] = None
    hairpin: bool = False

    def __post_init__(self):
        if self.protocol not in PROTOCOLS:
            raise ValueError("未知协议: %s" % self.protocol)
        if self.protocol == "tcp" and self.state not in TCP_STATES:
            raise ValueError("未知 TCP 状态: %s" % self.state)
        if self.duration < 0 or self.start < 0:
            raise ValueError("start/duration 必须非负")

    def five_tuple(self):
        return (self.protocol, self.src_ip, self.src_port, self.dst_ip, self.dst_port)


@dataclass
class Scenario:
    """完整仿真场景。

    addresses:          第一级 NAT 的公网地址池。
    upstream_addresses: 双层 NAT 时第二级(上游)的公网地址池; 为空表示单层。
    safety_margin:      保守容量余量系数(如 0.25 表示按峰值的 125% 规划)。
    """

    addresses: List[PublicAddress]
    connections: List[ConnectionSpec] = field(default_factory=list)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    static_mappings: List[StaticMapping] = field(default_factory=list)
    upstream_addresses: List[PublicAddress] = field(default_factory=list)
    safety_margin: float = 0.25

    @property
    def double_nat(self):
        return bool(self.upstream_addresses)

    def static_ports_by_ip(self):
        ports = {}  # type: Dict[str, Set[int]]
        for m in self.static_mappings:
            ports.setdefault(m.public_ip, set()).add(m.public_port)
        return ports

    @staticmethod
    def from_dict(data):
        """从 JSON 字典构建场景(见 examples/scenario.json)。"""
        timeouts = TimeoutConfig(**data.get("timeouts", {}))
        addresses = [
            PublicAddress(
                ip=a["ip"],
                port_ranges=[PortRange(r[0], r[1]) for r in a["port_ranges"]],
                reserved_ports=set(a.get("reserved_ports", [])),
            )
            for a in data["addresses"]
        ]
        upstream = [
            PublicAddress(
                ip=a["ip"],
                port_ranges=[PortRange(r[0], r[1]) for r in a["port_ranges"]],
                reserved_ports=set(a.get("reserved_ports", [])),
            )
            for a in data.get("upstream_addresses", [])
        ]
        mappings = [StaticMapping(**m) for m in data.get("static_mappings", [])]
        connections = [ConnectionSpec(**c) for c in data.get("connections", [])]
        return Scenario(
            addresses=addresses,
            connections=connections,
            timeouts=timeouts,
            static_mappings=mappings,
            upstream_addresses=upstream,
            safety_margin=data.get("safety_margin", 0.25),
        )
