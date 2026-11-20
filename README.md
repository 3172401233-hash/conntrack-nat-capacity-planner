# conntrack-nat-capacity-planner

连接跟踪(conntrack)与 NAT 端口池容量规划器。纯 Python 3.8.2 仿真,
不操作真实网络: 输入连接五元组、协议、状态、预计持续时间、超时配置、
公网地址与端口范围、保留端口、静态映射、hairpin 与双层 NAT 场景,
在时间序列上模拟端口分配、状态转换、超时释放与安全等待期后的回收复用。

## 快速开始

```bash
python -m unittest discover -s tests        # 运行测试
python -m nat_planner examples/scenario.json  # 运行示例场景
```

场景以 JSON 描述(见 `examples/scenario.json`), 也可在代码中直接用
`nat_planner.models` 的数据类构建 `Scenario` 后交给 `Simulator` 运行。

## 输出指标

- `peak_concurrent_connections` / `peak_ports_used`: 峰值并发连接数与端口占用
- `first_exhaustion_time` / `failed_allocations`: 首次端口耗尽时间与失败分配数
- `peak_by_protocol` / `avg_by_protocol`: TCP/UDP 各自的峰值与时间加权平均占用
- `peak_by_address`: 多公网地址各自的峰值占用(轮询分配)
- `reuse_conflicts`: 分配时因端口仍在安全等待期(冷却中)被跳过的次数
- `static_conflicts`: 连接显式请求的端口与静态映射/保留端口冲突的次数
- `utilization` / `conservative_required` / `headroom_ok`: 峰值利用率、
  按保守余量(默认峰值 × 1.25)所需端口数、现有容量是否满足
- 双层 NAT 时另报 `upstream_peak_ports_used`, 任一层耗尽即计分配失败

## 超时与容量假设

- 时间单位统一为秒; 端口范围为闭区间; 单连接同一时间占用一个公网端口。
- TCP `established`: 活跃 `duration` 秒后进入 TIME_WAIT, 再保持
  `tcp_time_wait`(默认 60s, 对齐 Linux `nf_conntrack_tcp_timeout_time_wait`
  常见值 120s 的保守下界)后释放。
- TCP `syn_sent`/`close_wait`: 活跃期结束即释放 / 再保持 `tcp_close`(默认 10s)。
- TCP 初始状态为 `time_wait` 时, `duration` 即剩余等待时间, 到期直接释放。
- UDP 无状态: 最后活跃(`start + duration`)后再保持 `udp` 超时(默认 30s,
  对齐 Linux `nf_conntrack_udp_timeout`)后释放; 长连接通过大 `duration` 建模。
- 端口释放后进入 `reuse_safe_wait`(默认 30s)冷却期, 期满才允许复用,
  防止四元组残留导致串话; 冷却中的端口计入 `reuse_conflicts`。
- 静态映射与保留端口从可分配容量中永久扣除, 动态连接绝不抢占;
  连接显式请求(`requested_port`)冲突时回退为动态分配并计一次冲突。
- 多公网地址按轮询 + 首次适应分配; 容量 = 各地址端口段之和 − 保留 − 静态映射。
- 保守容量余量: 建议容量 ≥ 峰值 × (1 + `safety_margin`), 默认余量 25%。
- hairpin 连接(内网访问本 NAT 公网地址)同样消耗源端口, 单独计数。

## 目录结构

- `nat_planner/models.py`: 场景、五元组连接、超时、地址池、静态映射模型
- `nat_planner/simulator.py`: 事件驱动仿真器与指标汇总
- `nat_planner/__main__.py`: JSON 场景命令行入口
- `tests/test_simulator.py`: 15 个单元测试(TCP established / TIME_WAIT /
  UDP 长连接 / 端口段边界 / 安全等待复用 / 静态映射冲突 / hairpin /
  多公网地址 / 双层 NAT / 容量余量)
