import unittest

from nat_planner import (
    ConnectionSpec,
    PortRange,
    PublicAddress,
    Scenario,
    Simulator,
    StaticMapping,
    TimeoutConfig,
)


def make_timeouts(**kw):
    base = dict(tcp_time_wait=60.0, tcp_close=10.0, udp=30.0, reuse_safe_wait=30.0)
    base.update(kw)
    return TimeoutConfig(**base)


def tcp_conn(conn_id, start=0.0, duration=60.0, state="established", **kw):
    return ConnectionSpec(
        conn_id=conn_id, protocol="tcp", src_ip="10.0.0.1", src_port=5000,
        dst_ip="93.184.216.34", dst_port=443, state=state,
        start=start, duration=duration, **kw
    )


def udp_conn(conn_id, start=0.0, duration=60.0, **kw):
    return ConnectionSpec(
        conn_id=conn_id, protocol="udp", src_ip="10.0.0.1", src_port=5300,
        dst_ip="8.8.8.8", dst_port=53, start=start, duration=duration, **kw
    )


def single_pool_scenario(conns, start_port=1000, end_port=1099, **kw):
    addr = PublicAddress("203.0.113.1", [PortRange(start_port, end_port)])
    return Scenario(addresses=[addr], connections=conns, **kw)


def held_at(report, time):
    snaps = [s for s in report.timeline if s.time <= time]
    return snaps[-1].total_held if snaps else 0


class TcpEstablishedTest(unittest.TestCase):
    def test_held_through_active_and_time_wait(self):
        # 活跃 60s + TIME_WAIT 60s, 之后释放
        sc = single_pool_scenario([tcp_conn("c1", duration=60.0)],
                                  timeouts=make_timeouts())
        report = Simulator(sc).run()
        self.assertEqual(report.peak_ports_used, 1)
        self.assertEqual(held_at(report, 59.0), 1)   # 活跃期
        self.assertEqual(held_at(report, 119.0), 1)  # TIME_WAIT 中
        self.assertEqual(held_at(report, 120.0), 0)  # 已释放
        states = [t[3] for t in report.transitions]
        self.assertIn("time_wait", states)
        self.assertEqual(states[-1], "closed")

    def test_peak_concurrent(self):
        conns = [tcp_conn("c%d" % i, start=0, duration=100) for i in range(5)]
        conns[2].start = 200  # 错开, 不并发
        report = Simulator(single_pool_scenario(conns)).run()
        self.assertEqual(report.peak_concurrent_connections, 4)
        self.assertEqual(report.peak_by_protocol["tcp"], 4)


class TimeWaitStateTest(unittest.TestCase):
    def test_initial_time_wait_releases_after_duration(self):
        # 初始即 TIME_WAIT, duration 为剩余等待时间, 到期直接释放
        sc = single_pool_scenario([tcp_conn("c1", duration=45, state="time_wait")])
        report = Simulator(sc).run()
        self.assertEqual(held_at(report, 44.0), 1)
        self.assertEqual(held_at(report, 45.0), 0)


class UdpLongConnectionTest(unittest.TestCase):
    def test_udp_held_for_duration_plus_timeout(self):
        sc = single_pool_scenario([udp_conn("u1", duration=3600.0)],
                                  timeouts=make_timeouts(udp=30.0))
        report = Simulator(sc).run()
        self.assertEqual(held_at(report, 3600.0), 1)   # 长连接活跃期
        self.assertEqual(held_at(report, 3629.0), 1)   # 静默超时内
        self.assertEqual(held_at(report, 3630.0), 0)   # 超时释放
        self.assertEqual(report.peak_by_protocol["udp"], 1)
        self.assertGreater(report.avg_by_protocol["udp"], 0.9)


class PortRangeBoundaryTest(unittest.TestCase):
    def test_boundary_ports_used_and_exhaustion(self):
        # 池只有 3 个端口 [1024, 1026], 第 4 条连接失败
        conns = [tcp_conn("c%d" % i, duration=1000) for i in range(4)]
        sc = single_pool_scenario(conns, start_port=1024, end_port=1026)
        report = Simulator(sc).run()
        self.assertEqual(report.capacity, 3)
        self.assertEqual(report.peak_ports_used, 3)
        self.assertEqual(report.failed_allocations, 1)
        self.assertEqual(report.first_exhaustion_time, 0.0)

    def test_allocated_ports_within_range(self):
        conns = [tcp_conn("c%d" % i, duration=10) for i in range(3)]
        sc = single_pool_scenario(conns, start_port=1024, end_port=1026)
        sim = Simulator(sc)
        sim.run()
        pool = sim.layer.pools[0]
        for port in pool.allocated:
            self.assertTrue(1024 <= port <= 1026)


class SafeWaitReuseTest(unittest.TestCase):
    def test_released_port_not_reused_before_safe_wait(self):
        # 池只有 1 个端口; c1 在 t=10 释放, 冷却 30s; c2 在 t=20 来则失败, c3 在 t=40 成功
        conns = [
            tcp_conn("c1", start=0, duration=10, state="syn_sent"),
            tcp_conn("c2", start=20, duration=10, state="syn_sent"),
            tcp_conn("c3", start=40, duration=10, state="syn_sent"),
        ]
        sc = single_pool_scenario(conns, start_port=1024, end_port=1024,
                                  timeouts=make_timeouts(reuse_safe_wait=30.0))
        report = Simulator(sc).run()
        self.assertEqual(report.failed_allocations, 1)
        self.assertEqual(report.first_exhaustion_time, 20.0)
        self.assertGreater(report.reuse_conflicts, 0)
        # c3 在冷却期结束后成功复用同一端口
        self.assertEqual(held_at(report, 41.0), 1)


class StaticMappingTest(unittest.TestCase):
    def test_static_port_never_allocated(self):
        mapping = StaticMapping("203.0.113.1", 1024, "10.0.0.10", 80)
        conns = [tcp_conn("c%d" % i, duration=1000) for i in range(2)]
        sc = single_pool_scenario(conns, start_port=1024, end_port=1025,
                                  static_mappings=[mapping])
        sim = Simulator(sc)
        report = sim.run()
        # 容量扣除静态端口, 2 条连接只能分到 1025, 第 2 条失败但静态端口不被抢占
        self.assertEqual(report.capacity, 1)
        self.assertEqual(report.failed_allocations, 1)
        self.assertNotIn(1024, sim.layer.pools[0].allocated)

    def test_requested_port_conflict_with_static(self):
        mapping = StaticMapping("203.0.113.1", 1024, "10.0.0.10", 80)
        conn = tcp_conn("c1", duration=100, requested_port=1024)
        sc = single_pool_scenario([conn], start_port=1024, end_port=1026,
                                  static_mappings=[mapping])
        sim = Simulator(sc)
        report = sim.run()
        self.assertEqual(report.static_conflicts, 1)
        self.assertEqual(report.failed_allocations, 0)  # 回退动态分配成功
        self.assertNotIn(1024, sim.layer.pools[0].allocated)


class HairpinTest(unittest.TestCase):
    def test_hairpin_counted_and_allocates(self):
        mapping = StaticMapping("203.0.113.1", 8080, "10.0.0.10", 80)
        addr = PublicAddress("203.0.113.1", [PortRange(1024, 2048)])
        conn = tcp_conn("h1", duration=60, hairpin=True)
        conn.dst_ip, conn.dst_port = "203.0.113.1", 8080
        sc = Scenario(addresses=[addr], connections=[conn],
                      static_mappings=[mapping])
        report = Simulator(sc).run()
        self.assertEqual(report.hairpin_connections, 1)
        self.assertEqual(report.peak_ports_used, 1)


class MultiAddressTest(unittest.TestCase):
    def test_round_robin_across_addresses(self):
        addrs = [
            PublicAddress("203.0.113.1", [PortRange(1024, 1025)]),
            PublicAddress("203.0.113.2", [PortRange(1024, 1025)]),
        ]
        conns = [tcp_conn("c%d" % i, duration=1000) for i in range(5)]
        sc = Scenario(addresses=addrs, connections=conns)
        report = Simulator(sc).run()
        self.assertEqual(report.capacity, 4)
        self.assertEqual(report.peak_by_address["203.0.113.1"], 2)
        self.assertEqual(report.peak_by_address["203.0.113.2"], 2)
        self.assertEqual(report.failed_allocations, 1)

    def test_reserved_ports_excluded(self):
        addr = PublicAddress("203.0.113.1", [PortRange(1024, 1026)],
                             reserved_ports={1024})
        conns = [tcp_conn("c%d" % i, duration=1000) for i in range(3)]
        sc = Scenario(addresses=[addr], connections=conns)
        sim = Simulator(sc)
        report = sim.run()
        self.assertEqual(report.capacity, 2)
        self.assertEqual(report.failed_allocations, 1)
        self.assertNotIn(1024, sim.layer.pools[0].allocated)


class DoubleNatTest(unittest.TestCase):
    def test_upstream_pool_consumed_and_exhaustion(self):
        inner = [PublicAddress("203.0.113.1", [PortRange(1024, 2048)])]
        upstream = [PublicAddress("198.51.100.1", [PortRange(4096, 4097)])]
        conns = [tcp_conn("c%d" % i, duration=1000) for i in range(3)]
        sc = Scenario(addresses=inner, connections=conns,
                      upstream_addresses=upstream)
        report = Simulator(sc).run()
        self.assertTrue(report.double_nat)
        self.assertEqual(report.upstream_peak_ports_used, 2)
        # 第 3 条连接因上游耗尽失败
        self.assertEqual(report.failed_allocations, 1)
        self.assertEqual(report.peak_ports_used, 2)


class CapacityMarginTest(unittest.TestCase):
    def test_conservative_margin(self):
        # 池 5 端口, 峰值 4 -> 需要 4*1.25=5, 刚好满足
        conns = [tcp_conn("c%d" % i, duration=1000) for i in range(4)]
        sc = single_pool_scenario(conns, start_port=1024, end_port=1028,
                                  safety_margin=0.25)
        report = Simulator(sc).run()
        self.assertEqual(report.peak_ports_used, 4)
        self.assertEqual(report.conservative_required, 5)
        self.assertTrue(report.headroom_ok)
        self.assertAlmostEqual(report.utilization, 0.8)

    def test_insufficient_headroom(self):
        conns = [tcp_conn("c%d" % i, duration=1000) for i in range(4)]
        sc = single_pool_scenario(conns, start_port=1024, end_port=1027)
        report = Simulator(sc).run()
        self.assertFalse(report.headroom_ok)


if __name__ == "__main__":
    unittest.main()
