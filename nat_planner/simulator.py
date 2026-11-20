"""事件驱动的 NAT 端口池仿真器。

事件序列: start -> active_end -> (TCP: TIME_WAIT / UDP: 静默超时) -> expire
端口释放后进入冷却期(reuse_safe_wait), 冷却结束才可复用。
"""

import heapq
import itertools
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .models import ConnectionSpec, Scenario

# 事件类型
_START = "start"
_ACTIVE_END = "active_end"
_EXPIRE = "expire"

# TCP 各状态在活跃期结束后, 释放前还需保持的时长来源
_TCP_TRAILING = {
    "established": "tcp_time_wait",   # 活跃结束后进入 TIME_WAIT
    "close_wait": "tcp_close",
    "syn_sent": None,                 # 活跃期(建连尝试)结束即释放
    "time_wait": None,                # duration 即剩余 TIME_WAIT 时间
}


@dataclass
class Snapshot:
    """某一事件时刻处理完毕后的池占用快照。"""

    time: float
    total_held: int
    active_connections: int
    by_protocol: Dict[str, int]
    by_address: Dict[str, int]


@dataclass
class SimReport:
    """仿真结果汇总。"""

    capacity: int
    peak_concurrent_connections: int = 0
    peak_ports_used: int = 0
    peak_by_protocol: Dict[str, int] = field(default_factory=dict)
    peak_by_address: Dict[str, int] = field(default_factory=dict)
    avg_by_protocol: Dict[str, float] = field(default_factory=dict)
    first_exhaustion_time: Optional[float] = None
    failed_allocations: int = 0
    reuse_conflicts: int = 0
    static_conflicts: int = 0
    hairpin_connections: int = 0
    double_nat: bool = False
    upstream_peak_ports_used: int = 0
    sim_end_time: float = 0.0
    safety_margin: float = 0.25
    timeline: List[Snapshot] = field(default_factory=list)
    transitions: List[Tuple[float, str, str, str]] = field(default_factory=list)

    @property
    def utilization(self):
        return self.peak_ports_used / self.capacity if self.capacity else 1.0

    @property
    def conservative_required(self):
        """按保守余量规划所需端口数(向上取整)。"""
        return int(math.ceil(self.peak_ports_used * (1.0 + self.safety_margin)))

    @property
    def headroom_ok(self):
        """现有容量是否满足保守余量要求。"""
        return self.capacity >= self.conservative_required

    def to_dict(self):
        return {
            "capacity": self.capacity,
            "peak_concurrent_connections": self.peak_concurrent_connections,
            "peak_ports_used": self.peak_ports_used,
            "peak_by_protocol": dict(self.peak_by_protocol),
            "peak_by_address": dict(self.peak_by_address),
            "avg_by_protocol": dict(self.avg_by_protocol),
            "first_exhaustion_time": self.first_exhaustion_time,
            "failed_allocations": self.failed_allocations,
            "reuse_conflicts": self.reuse_conflicts,
            "static_conflicts": self.static_conflicts,
            "hairpin_connections": self.hairpin_connections,
            "double_nat": self.double_nat,
            "upstream_peak_ports_used": self.upstream_peak_ports_used,
            "utilization": round(self.utilization, 4),
            "safety_margin": self.safety_margin,
            "conservative_required": self.conservative_required,
            "headroom_ok": self.headroom_ok,
        }


class PortPool:
    """单个公网地址的端口池(一级 NAT 的一层)。"""

    def __init__(self, address, static_ports):
        self.address = address
        self.static_ports = set(static_ports)
        self.allocated = {}   # type: Dict[int, str]  port -> conn_id
        self.cooling = {}     # type: Dict[int, float] port -> reusable_at

    def capacity(self):
        return self.address.capacity(self.static_ports)

    def held(self):
        return len(self.allocated)

    def is_static_or_reserved(self, port):
        if port in self.static_ports or port in self.address.reserved_ports:
            return any(r.contains(port) for r in self.address.port_ranges)
        return False

    def allocate(self, conn_id, now, requested_port=None):
        """分配一个端口。

        返回 (port, skipped_cooling)。port 为 None 表示该地址耗尽。
        skipped_cooling 为扫描中跳过的冷却中端口数(复用冲突度量)。
        """
        skipped = 0
        if requested_port is not None:
            ok = (
                any(r.contains(requested_port) for r in self.address.port_ranges)
                and not self.is_static_or_reserved(requested_port)
                and requested_port not in self.allocated
                and self.cooling.get(requested_port, 0.0) <= now
            )
            if ok:
                self.allocated[requested_port] = conn_id
                return requested_port, 0
            # 指定端口不可用则回退到动态分配
        for pr in self.address.port_ranges:
            for port in range(pr.start, pr.end + 1):
                if port in self.static_ports or port in self.address.reserved_ports:
                    continue
                if port in self.allocated:
                    continue
                reusable_at = self.cooling.get(port)
                if reusable_at is not None and reusable_at > now:
                    skipped += 1
                    continue
                self.allocated[port] = conn_id
                return port, skipped
        return None, skipped

    def release(self, conn_id, now, safe_wait):
        """释放连接占用的端口, 进入冷却期。返回端口号。"""
        for port, owner in list(self.allocated.items()):
            if owner == conn_id:
                del self.allocated[port]
                self.cooling[port] = now + safe_wait
                return port
        return None


class _Layer:
    """一级 NAT: 多个公网地址的端口池集合, 轮询选择地址。"""

    def __init__(self, addresses, static_ports_by_ip):
        self.pools = [
            PortPool(a, static_ports_by_ip.get(a.ip, set())) for a in addresses
        ]
        self._rr = 0

    def capacity(self):
        return sum(p.capacity() for p in self.pools)

    def held(self):
        return sum(p.held() for p in self.pools)

    def allocate(self, conn_id, now, requested_port=None):
        """轮询各地址做首次适应分配, 返回 (pool, port, skipped_cooling)。"""
        n = len(self.pools)
        total_skipped = 0
        for i in range(n):
            pool = self.pools[(self._rr + i) % n]
            port, skipped = pool.allocate(conn_id, now, requested_port)
            total_skipped += skipped
            if port is not None:
                self._rr = (self._rr + i + 1) % n
                return pool, port, total_skipped
        return None, None, total_skipped

    def release(self, conn_id, now, safe_wait):
        for pool in self.pools:
            port = pool.release(conn_id, now, safe_wait)
            if port is not None:
                return pool, port
        return None, None


class Simulator:
    """对 Scenario 做时间序列仿真。"""

    def __init__(self, scenario):
        # type: (Scenario) -> None
        self.scenario = scenario
        static_by_ip = scenario.static_ports_by_ip()
        self.layer = _Layer(scenario.addresses, static_by_ip)
        self.upstream = (
            _Layer(scenario.upstream_addresses, static_by_ip)
            if scenario.double_nat
            else None
        )
        self.report = SimReport(
            capacity=self.layer.capacity(),
            double_nat=scenario.double_nat,
            safety_margin=scenario.safety_margin,
        )
        self._events = []  # heap of (time, seq, kind, conn_id)
        self._seq = itertools.count()
        self._conns = {}  # type: Dict[str, ConnectionSpec]
        self._conn_protocol = {}  # type: Dict[str, str]
        self._held_by_protocol = {"tcp": 0, "udp": 0}
        self._area_by_protocol = {"tcp": 0.0, "udp": 0.0}  # 端口·秒积分
        self._last_time = 0.0
        self._active = 0

    def _push(self, time, kind, conn_id):
        heapq.heappush(self._events, (time, next(self._seq), kind, conn_id))

    def run(self):
        # type: () -> SimReport
        for conn in self.scenario.connections:
            self._conns[conn.conn_id] = conn
            self._push(conn.start, _START, conn.conn_id)
        while self._events:
            time, _, kind, conn_id = heapq.heappop(self._events)
            self._accumulate(time)
            if kind == _START:
                self._on_start(conn_id, time)
            elif kind == _ACTIVE_END:
                self._on_active_end(conn_id, time)
            else:
                self._on_expire(conn_id, time)
            self._snapshot(time)
        self.report.sim_end_time = self._last_time
        if self._last_time > 0:
            for proto in self._held_by_protocol:
                self.report.avg_by_protocol[proto] = (
                    self._area_by_protocol[proto] / self._last_time
                )
        return self.report

    # ---- 事件处理 ----

    def _on_start(self, conn_id, now):
        conn = self._conns[conn_id]
        timeouts = self.scenario.timeouts
        if conn.hairpin:
            self.report.hairpin_connections += 1

        # 静态映射/保留端口冲突: 连接显式请求了被静态映射或保留的端口
        requested = conn.requested_port
        if requested is not None:
            for pool in self.layer.pools:
                if pool.is_static_or_reserved(requested):
                    self.report.static_conflicts += 1
                    requested = None  # 回退动态分配, 不抢占静态映射
                    break

        pool, port, skipped = self.layer.allocate(conn_id, now, requested)
        self.report.reuse_conflicts += skipped
        if port is None:
            self.report.failed_allocations += 1
            if self.report.first_exhaustion_time is None:
                self.report.first_exhaustion_time = now
            return

        if self.upstream is not None:
            up_pool, up_port, up_skipped = self.upstream.allocate(
                conn_id, now, requested_port=None
            )
            self.report.reuse_conflicts += up_skipped
            if up_port is None:
                # 上游耗尽: 回滚本级分配, 计一次失败
                self.layer.release(conn_id, now, 0.0)
                self.report.failed_allocations += 1
                if self.report.first_exhaustion_time is None:
                    self.report.first_exhaustion_time = now
                return

        self._active += 1
        self._conn_protocol[conn_id] = conn.protocol
        self._held_by_protocol[conn.protocol] += 1
        self.report.peak_concurrent_connections = max(
            self.report.peak_concurrent_connections, self._active
        )

        # 调度生命周期后续事件
        if conn.protocol == "tcp" and conn.state == "time_wait":
            # duration 即剩余 TIME_WAIT 时间, 到期直接释放
            self._push(now + conn.duration, _EXPIRE, conn_id)
        else:
            self._push(now + conn.duration, _ACTIVE_END, conn_id)

    def _on_active_end(self, conn_id, now):
        conn = self._conns[conn_id]
        timeouts = self.scenario.timeouts
        if conn.protocol == "udp":
            # 最后活跃后开始 UDP 静默超时
            self.report.transitions.append((now, conn_id, "udp-active", "udp-idle"))
            self._push(now + timeouts.udp, _EXPIRE, conn_id)
            return
        trailing_name = _TCP_TRAILING[conn.state]
        if trailing_name is None:
            self.report.transitions.append((now, conn_id, conn.state, "closed"))
            self._release(conn_id, now)
            return
        trailing = getattr(timeouts, trailing_name)
        next_state = "time_wait" if conn.state == "established" else "closing"
        self.report.transitions.append((now, conn_id, conn.state, next_state))
        if trailing <= 0:
            self._release(conn_id, now)
        else:
            self._push(now + trailing, _EXPIRE, conn_id)

    def _on_expire(self, conn_id, now):
        conn = self._conns[conn_id]
        prev = "time_wait" if conn.protocol == "tcp" else "udp-idle"
        self.report.transitions.append((now, conn_id, prev, "closed"))
        self._release(conn_id, now)

    def _release(self, conn_id, now):
        safe_wait = self.scenario.timeouts.reuse_safe_wait
        self.layer.release(conn_id, now, safe_wait)
        if self.upstream is not None:
            self.upstream.release(conn_id, now, safe_wait)
        proto = self._conn_protocol.pop(conn_id, None)
        if proto is not None:
            self._held_by_protocol[proto] -= 1
            self._active -= 1

    # ---- 指标 ----

    def _accumulate(self, now):
        dt = now - self._last_time
        if dt > 0:
            for proto, held in self._held_by_protocol.items():
                self._area_by_protocol[proto] += held * dt
            self._last_time = now

    def _snapshot(self, now):
        rep = self.report
        held = self.layer.held()
        rep.peak_ports_used = max(rep.peak_ports_used, held)
        for proto, n in self._held_by_protocol.items():
            rep.peak_by_protocol[proto] = max(rep.peak_by_protocol.get(proto, 0), n)
        by_addr = {}
        for pool in self.layer.pools:
            by_addr[pool.address.ip] = pool.held()
            rep.peak_by_address[pool.address.ip] = max(
                rep.peak_by_address.get(pool.address.ip, 0), pool.held()
            )
        if self.upstream is not None:
            rep.upstream_peak_ports_used = max(
                rep.upstream_peak_ports_used, self.upstream.held()
            )
        rep.timeline.append(
            Snapshot(
                time=now,
                total_held=held,
                active_connections=self._active,
                by_protocol=dict(self._held_by_protocol),
                by_address=by_addr,
            )
        )
