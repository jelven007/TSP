# TSP 在 Windows 10 上完整运行 — 优化方案

> 适用版本：v0.4.9（本机部署形态：多租户 full-product，`MULTITENANCY_ENABLED=true`、`SERVICE_ROLE=api`）
> 本方案只描述「怎么让它在 Windows 10 上稳定跑起来」，不改变任何既有产品契约。
> 每条结论都对应一个可复现的证据（文件行号或实测输出），不含推测性描述。

---

## 0. 结论（先看这里）

1. **产品主体在原生 Windows 10 上是可以完整运行的**：3018 平台 + 3030 API + 3012 前端 + 4 类常驻 worker + PostgreSQL 双库 + 公共版本发布，全部可用。当前失败的原因集中在 **4 个可精确定位的工程细节**，不涉及架构缺陷。
2. **`compute_broker` 及全部重型计算在原生 Windows 10 上不可用，且不可能可用**：本机 Python 无 `socket.AF_UNIX`，且未安装 Docker / Podman / WSL；`compute_broker` 模块在 Windows 上连 `import` 都会失败。仓库自身已就此书面声明（见 `backend/tests/test_full_compute.py`）。**按既定决策：不改代码，保持 `error`。**
3. 因此 `/health/ready` 在原生 Windows 上**不可能全绿**（只要 `compute_broker` 恒为 `error`，`ready` 就恒为 `false`）。要让 readiness 全绿，唯一「零代码侵入」的路径是 **WSL2 + Docker**（与 Linux 生产同构）。

**两条交付轨道：**

| 轨道 | 形态 | 完整度 | 代价 |
|---|---|---|---|
| **A. 原生 Windows 10** | 基础设施 + 4 worker 常驻自愈 | 除 compute 外全部可用；`/health/ready` 保留 1 项 `error` | 需实现进程托管；2 处建议补丁 |
| **B. WSL2 + Docker** | `deploy/full/compose*.yml` 原样起 | 与 Linux 生产完全同构，含 compute | 需安装 WSL2 + Docker Desktop |

---

## 1. 现状与实测证据

| # | 阻塞点 | 证据 | 实测结论 |
|---|---|---|---|
| E1 | 4 类 worker 全部 `exit 1`，日志 `dispatch progress expired` | `backend/app/multitenancy/worker_process.py` `supervise()` | 心跳停止推进超过 `MAX_AGE`（research/notifications 120s、realtime 420s、scheduled 660s）。research 实测启动 16:41:37 → 停止 16:43:36 ≈ 119s，与 120s 阈值吻合 |
| E2 | 心跳原子写在 Windows 上会被并发读者阻塞 | `worker_process.write_json()` 使用裸 `temporary.replace(path)` | **已实测**：目标文件持有读句柄时 `os.replace` 抛 `PermissionError(winerror=5)`；模拟 2Hz 写 / 2Hz 读，8 次写入中 1 次被阻塞（≈12.5%） |
| E3 | 仓库已有该问题的既有解法，但未用在这里 | `tickflow/repository.py` `replace_with_retry()`（注释明确写「Windows 下目标正被并发读取时由 replace_with_retry 短退避穿过」）、`services/fs_utils.py` | 说明这是既定认知内的 Windows 差异，`worker_process` 是遗漏点 |
| E4 | scheduled worker 在 Windows 首次启动即 `NotImplementedError` | `scheduled_worker.py` `main()` → `loop.add_signal_handler(signum, stopped.set)` | Windows 的 ProactorEventLoop 不支持 `add_signal_handler` |
| E5 | `worker_process.check()` 在 Windows 上会**杀掉**被检查的进程 | `worker_process.py` `check()` → `os.kill(owner["pid"], 0)` | CPython 在 Windows 上对「非 CTRL_C/CTRL_BREAK」的信号走 `OpenProcess + TerminateProcess`，`sig=0` 同样会终止进程 |
| E6 | `--directory` 默认值 `/tmp/tsp-worker` 在 Windows 解析到当前盘符 | `worker_process.py` `main()` | 会创建 `E:\tmp\tsp-worker`，必须显式传参 |
| E7 | 本机无 `AF_UNIX` | `python -c "import socket; print(hasattr(socket,'AF_UNIX'))"` | `False` |
| E8 | 本机无容器运行时 | `Get-Command docker/podman/nerdctl` | 均不存在；无 `C:\Program Files\Docker`；无 WSL |
| E9 | `compute_broker` 在 Windows 不可导入 | `socketserver.ThreadingUnixStreamServer` 由 CPython 用 `hasattr(socket,"AF_UNIX")` 守卫定义 | 配合 E7，模块 import 即失败 |
| E10 | 客户端传输层硬编码 Unix socket | `multitenancy/executor.py` `socket.socket(socket.AF_UNIX, ...)`，默认 `/run/tsp/compute.sock` | 无平台分支 |
| E11 | `worker_process.progress()` 的 pid 校验在 venv 启动器下必然失败 | `worker_process.py` `progress()`；比对 `owner.json` 的 `pid` 与 `Popen` 直接子进程 pid | **已实测**：`backend\.venv\Scripts\python.exe` 是 uv trampoline，以相同 argv 再派生 `C:\Program Files\Python312\python.exe`；4 类 worker 的 `owner.json` pid 与 `progress.json` pid 全部不等（realtime 13812 vs 144、research 5996 vs 3468、notifications 6896 vs 11312、scheduled 12240 vs 11756），`progress()` 恒 `None` → 120s 后 `exit 1` |

---

## 2. 目标分层与验收标准

| 层级 | 内容 | 验收标准 |
|---|---|---|
| **L1 基础设施** | 3018 平台、3030 API、3012 前端、双库 PG、公共版本发布 | `/health/ready` 中除 `compute_broker` 外全部 `ok`；`/api/health` HTTP 200 |
| **L2 业务可用** | 登录、租户、行情、K线、策略、监控、通知、定时任务 | `admin/Admin123` 登录成功；业务接口无 409/401；前端可交互 |
| **L3 常驻自愈** | 4 类 worker 脱离终端常驻，心跳持续刷新，异常自动重启 | 连续 ≥30 分钟，`/api/health` 四项 worker 恒为 `ok`，`age_seconds` 不接近阈值 |
| **L4 全绿（仅轨道 B）** | 补齐 compute | `/health/ready` → `ready: true` |

---

## 3. P0：worker 常驻失败的三条根因与处置

### P0-1　心跳原子写在 Windows 上被并发读者阻塞（根因，已实测）

**机制**：`WorkerHealth.record()` 每 ≤5s 调 `worker_process.record()` → `write_json()` → `temporary.replace(path)`。同一时刻，监督进程（或下游健康检查）正在用 `Path.read_text()` 打开 `progress.json`。CPython 在 Windows 上打开文件不带 `FILE_SHARE_DELETE`，因此 `MoveFileEx(REPLACE_EXISTING)` 抛 `PermissionError(WinError 5)`。

**影响链（决定 worker 生死）**：

| worker | 调用点 | 异常后果 |
|---|---|---|
| research | `research.py` `health.record()` 在 `try` 内 | 进 `except` → `health.record(False)` → `progress.json` 标记 `healthy=false` → 监督进程 `progress()` 返回 `None` → 心跳不再续期 → **`MAX_AGE`(120s) 后 `exit 1`**（与实测 119s 完全吻合） |
| notifications | `notification_worker.py` `health.record()` 未包在 `try` 内 | 异常直接穿出 `while` → `finally: db.close()` → 进程退出 → 监督进程 `exit 1` |
| realtime | `runtime.py` `health.record()` 在 `try` 内 | 记录 `full worker tick failed`，停 5s 后继续；单次不致命，密集发生则累积成 E1 |
| scheduled | `asyncio.to_thread(schedules.health.record)` | 异常被上层 `except Exception` 捕获记日志，但心跳同样失效 |

**处置（二选一）**

- **A（推荐，根治）**：给 `write_json` 加 Windows 短退避重试，与仓库既有 `replace_with_retry` 同一纪律：

```python
def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(value), encoding="utf-8")
        if os.name != "nt":
            temporary.replace(path)
            return
        # Windows 不允许替换仍被读者打开的目标文件 (WinError 5)；
        # 监督/健康检查按 0.5s 频度读取 progress.json，短退避即可穿过读窗口。
        for attempt in range(20):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(.05)
    finally:
        temporary.unlink(missing_ok=True)
```

- **B（不改代码）**：绕开监督进程的 2Hz 读。即不跑 `worker_process`，直接托管 worker 模块（见 P0-4 的推荐形态），心跳文件不再是存活判定依据（readiness 读的是 `control.full_worker_health` 表）。

### P0-2　scheduled worker 无法在 Windows 启动

`loop.add_signal_handler` 在 Windows ProactorEventLoop 上抛 `NotImplementedError`，`main()` 直接崩。

**处置（二选一）**

- **A（推荐）** 4 行平台守卫，跨平台一致、不改变 Linux 行为：

```python
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, stopped.set)
            except NotImplementedError:      # Windows ProactorEventLoop
                signal.signal(signum, lambda *_: stopped.set())
```

- **B（不改代码）**：在 `python -c` 包装器里把 `asyncio.BaseEventLoop.add_signal_handler` 降级为 no-op，再 `runpy.run_module("app.multitenancy.scheduled_worker", run_name="__main__")`。仅作为「暂不允许改仓库」时的过渡手段。

### P0-3　`worker_process` 在 Windows 上的三个缺陷

| 缺陷 | 影响 | 处置 |
|---|---|---|
| `progress()` 要求 `progress.json` 的 `pid` 等于 `Popen` 直接子进程 pid | **在 uv/venv 启动器下永远不成立**（E11）：真正执行模块并写 `progress.json` 的是孙进程，`progress()` 恒返回 `None` → `MAX_AGE` 宽限期后 `exit 1`。这是「上一节 C2 修复后仍 120s 必死」的真正剩余根因 | 改代码（C5）：`progress()` 改为按 `instance + kind + healthy + 新鲜度` 判定；`instance` 是每次 `supervise()` 新生成的 uuid4，注入子进程环境后由孙进程继承，足以区分新旧实例。`pid` 仅作诊断字段保留 |
| `check()` 用 `os.kill(pid, 0)` 探活 | **会真的杀掉 worker**（E5）。一旦把它挂进计划任务/看门狗，等于定时自杀 | 不改代码：Windows 上不调用 `--check`，看门狗改为读 `progress.json`（`healthy` + 文件 `LastWriteTime`）。改代码：Windows 分支用 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess == STILL_ACTIVE` |
| `--directory` 默认 `/tmp/tsp-worker` | 在 `E:` 盘工作目录下创建 `E:\tmp\tsp-worker`，多实例互相污染 | 托管脚本**必须**显式传 `--directory`；或改默认值为 `os.environ.get("TSP_WORKER_HEARTBEAT_DIR_ROOT", "/tmp/tsp-worker")` |

### P0-4　进程托管缺失（这是「2 分钟后全体消失」的环境侧原因）

**现状事实**：4 个 worker 是挂在交互式终端上启动的，终端被复用后子进程被强杀（`taskkill /F`），心跳随之停摆。这不是产品缺陷，而是缺少「脱离终端的托管层」——Compose 场景下由 Docker 守护进程承担，原生 Windows 下没有对应物。

**推荐托管形态**：**直接托管 worker 模块，而不是 `worker_process`**。

理由：`worker_process` 的价值是「容器内监督 + 心跳文件」，而它的 Windows 缺陷（E5/E6/P0-1）恰好都在这一层。readiness 判定 worker 是否健康读的是 `control.full_worker_health` 表，该表由 `WorkerHealth.record()` 直接写库，**与 `worker_process` 无关**。因此直接运行 worker 模块即可满足 L1/L3，且不必触碰上述缺陷。

```
python -m app.multitenancy.research_worker
python -m app.multitenancy.runtime
python -m app.multitenancy.notification_worker
python -m app.multitenancy.scheduled_worker      # 需 P0-2 的 A 或 B
```

若坚持走官方入口 `worker_process <kind>`（保留进度文件契约与容器语义一致性），则必须同时落地 P0-1(A) + P0-3 两个处置，并在 Windows 上放弃 `--check`。

**托管脚本设计要点（新增 `deploy/windows/`，不改产品代码）**

- 启动：`Start-Process -FilePath <venv>\Scripts\python.exe -ArgumentList ... -WindowStyle Hidden -RedirectStandardOutput <logs>\<kind>.out.log -RedirectStandardError <logs>\<kind>.err.log -PassThru`，PID 落 `<run>\<kind>.pid`。`Start-Process` 创建的进程不绑定当前控制台，终端关闭/复用不会再误杀。
- 环境：把本机凭据文件（`secrets.env`）逐行解析为进程环境（当前 3030 用 `uvicorn --env-file`，worker 没有等价入口，必须在启动前 `Set-Item Env:`）。worker 复用与 3030 相同的环境契约（`deploy/full/compose.yml` 的 `x-common`：`TSP_CELL_*`、`DATA_DIR=<public_market>/current`、`PLATFORM_CONFIG_DIR`、`SERVICE_ROLE=api`、`MULTITENANCY_ENABLED=true`、`TSP_CELL_SCHEMA_VERSION=6`、平台 token 等），额外设置 `PYTHONUNBUFFERED=1`、UTF-8 输出编码。
- 看门狗（每 30s）：`Get-Process -Id <pid>` 用于确认进程存活；`progress.json` 的 `healthy` 与 `LastWriteTime` 用于确认心跳（**不要**在 PowerShell 里比较 `at`，它是 `time.monotonic()`，跨进程无意义）。任一不满足即停止并重启该 kind。
- 开机自启 / 崩溃自愈：用 Windows 计划任务（`Register-ScheduledTask`，触发器「登录时/启动时」+「重复间隔」）或 NSSM 注册为服务。两者都不需要修改仓库代码。
- 日志：每 kind 独立 `stdout/stderr` 文件，避免多进程共写终端导致乱码/互杀。

---

## 4. P1：路径与权限差异

| 项 | 位置 | Windows 影响 | 处置 |
|---|---|---|---|
| `/run/tsp/compute.sock` | `executor.py` 默认值 | 无 `AF_UNIX`，不可用 | 见第 6 节（不支持，保持 `error`） |
| `/tmp/tsp-worker` | `worker_process.py` 默认 `--directory` | 落在当前盘符 | 显式传 `--directory`（P0-3） |
| `/market` `/app` `/dev/null` | `compute_worker.py`、`compute_broker.py` 容器内路径 | 仅容器内语义 | 不适用（compute 不在 Windows 跑） |
| `0o660` / `0o750` / `0o700` | `compute_broker.py`、`research_snapshot.py`、`api_tokens.py`、`fs_utils.py` | Windows 只能表达只读位，其余忽略 | 无害。仓库已在 `fs_utils.py`、`api_tokens.py` 用 `try/except OSError` 兜住 |
| `os.symlink`（目录） | `multitenancy/publication.py` 发布 `current` | 需「开发者模式」或管理员权限 | **已实测本机可创建**（release `74c09768…` + `current` 已就绪）；写入前提条件 |
| `os.replace` 覆盖符号链接 | `publication.py` `_swap_current()` | `MoveFileEx` 不能替换目录项 | **仓库已处理**（`os.name == "nt"` 分支：先删旧 link 再替换） |
| 目录 `fsync` | `second_quote_archive.py` | 不支持 | **仓库已处理**（`os.name != "nt"` 守卫） |
| `resource.setrlimit` | `cell/compute.py` | 不可用 | **仓库已处理**（`sys.platform != "win32"` 守卫） |

---

## 5. P2：POSIX 专属假设全量扫描结论

对 `backend/app` 全量检索 `fcntl / pwd / grp / resource / termios / os.fork / os.setsid / SIGKILL / SIGHUP / os.getuid / chmod / symlink / AF_UNIX / add_signal_handler / os.kill` 后，结论如下：

| 模块 | 假设 | 状态 |
|---|---|---|
| `services/fs_utils.py`、`tickflow/repository.py`、`services/kline_sync.py`、`services/mining_process_lock.py`、`enriched_generation.py` | 文件锁 `fcntl.flock` / `os.replace` 覆盖被读文件 | **已双实现**：`msvcrt.locking` + `replace_with_retry` |
| `services/second_quote_archive.py` | 目录 `fsync` | **已守卫** |
| `cell/compute.py` | `resource.setrlimit` | **已守卫** |
| `multitenancy/publication.py` | `os.replace` 覆盖符号链接 | **已守卫** |
| `desktop.py`、`services/api_tokens.py`、`services/ai_provider.py`、`services/notify_adapter.py`、`services/data_release.py` | 权限位、`ctypes` 弹框、PowerShell `.ps1` 解析、Windows 通知 | **已双实现** |
| `multitenancy/executor.py` | `AF_UNIX` | **未守卫** → compute 在 Windows 不支持（既定，不改） |
| `multitenancy/worker_process.py` | `os.kill(pid,0)`、裸 `os.replace`、默认 `/tmp` | **未守卫** → P0-1 / P0-3 |
| `multitenancy/scheduled_worker.py` | `loop.add_signal_handler` | **未守卫** → P0-2 |

即：**除 compute 链路之外，仓库其余 POSIX 依赖都已有 Windows 分支或已被证明无害**；剩余 3 处未守卫点全部落在 `multitenancy` 的 worker 托管层，且都有可落地的处置。

---

## 6. compute 子系统：Windows 原生不支持（按既定决策，不改代码）

**为什么不可能在原生 Windows 上打通（不是配置问题）**

1. `ComputeExecutor` 硬编码 `socket.AF_UNIX`（`executor.py`），而 Windows CPython 无此常量 → 客户端层无法改造，除非换传输层（TCP/named pipe）；
2. `ComputeBroker` 继承 `socketserver.ThreadingUnixStreamServer`，该类在无 `AF_UNIX` 的平台**根本不存在** → `compute_broker` 模块 import 即 `AttributeError`；
3. 每个任务的沙箱是 `docker run --network none --read-only --cap-drop ALL --pids-limit --memory --cpus --tmpfs`（`compute_broker.py`）→ 需要容器运行时，本机没有。

**仓库自身的书面结论**：`backend/tests/test_full_compute.py` 顶部即 `pytest.skip("Unix domain sockets are unavailable on this platform", allow_module_level=True)`。

**影响面**

- `/health/ready` 的 `compute_broker` 恒为 `error` → `ready` 恒为 `false`。
- 经该通道承载的任务类型（`executor.py` 的 `KINDS`）：`describe / plan / strategy / backtest / optimize / walkforward / mining / signal_backtest / factor_backtest / factor_batch / auto_screen / monitor`。调用这些能力时客户端抛 `AccessError(503, "计算服务暂不可用，请稍后重试")`，表现为接口 503，**不会影响其余功能**。
- research worker 仍能正常心跳与调度；只是它认领到的重计算任务会以失败/重试收场。

**两条路径**

- **推荐**：需要这些能力时切到 **WSL2 + Docker**（轨道 B），`deploy/full/compose.isolated.yml` 原样起 broker + sandbox。
- 原生 Windows：接受 `compute_broker: error`，并在运维文档/前端提示中把该状态标注为「本平台不受支持」，避免被误读成故障。

---

## 7. 推荐实施步骤

### 7.1 轨道 A：原生 Windows 10（本轮目标）

1. **前置**：开启 Windows 开发者模式（或管理员运行）以保证 `publication.py` 的 `os.symlink` 可创建目录链接；确认 `E:\pg-tsp\secrets.env` 存在且双库可达；`uv sync --frozen --inexact`（`dev.ps1` 已内置）与 `pnpm install` 完成。
2. **落地 P0-2(A)**（4 行平台守卫），使 scheduled worker 可启动。
3. **落地 P0-1(A)**（`write_json` 短退避重试）。若本阶段不接受改代码，则采用 P0-4 的推荐形态（直接托管 worker 模块）并跳过本项。
4. **新增 `deploy/windows/` 托管脚本**（不改产品代码）：
   - `tsp-full.ps1 start|stop|status|restart`
   - `tsp-watchdog.ps1`（读 PID + 心跳文件，异常重启并滚动日志）
   - 可选：`Register-ScheduledTask` 注册开机自启 / NSSM 注册为服务（已落地：NSSM 服务 `TSPWorkerWatchdog`，`SERVICE_AUTO_START`，无需用户登录即可随开机启动；原计划任务已注销）
5. **启动顺序**：PostgreSQL 双库 → 3018 平台（唯一行情写入者 + 公共版本发布，必须等 `current` 就绪）→ 3030 API（`DATA_DIR=<public_market>/current`）→ 4 类 worker → 3012 前端。四项（PostgreSQL、3018 平台、3030 API、3012 前端）均已注册为 Windows / NSSM 服务并通过 `DependOnService` 串成依赖链（`TSPPostgres → TSPPlatform → TSPApi → TSPFrontend / TSPWorkerWatchdog`），开机无需用户登录即可自动按序拉起。
6. **观察 30 分钟**，按第 9 节验收。

### 7.2 轨道 B：WSL2 + Docker（要「全绿」时）

1. `wsl --install` + Docker Desktop（或 WSL 内 Docker Engine）；
2. 在 WSL 内 `deploy/full/` 起 compose（生产拓扑）或 `compose.isolated.yml`（隔离 compute）；
3. 按 `deploy/full/README.md` 验证 `/health/ready` → `ready: true`（含 `compute_broker`）。

---

## 8. 代码侧最小补丁清单（需评审后实施）

| 编号 | 文件 | 改动 | 必要性 |
|---|---|---|---|
| C1 | `backend/app/multitenancy/scheduled_worker.py` | `main()` 中 `add_signal_handler` 加 `NotImplementedError` 回退到 `signal.signal` | **必需**（否则 scheduled 无法启动） |
| C2 | `backend/app/multitenancy/worker_process.py` | `write_json()` 增加 Windows `PermissionError` 短退避重试 | 强烈建议（根治 E1 主因）；采用 7.1 第 3 步替代形态时可不做 |
| C3 | `backend/app/multitenancy/worker_process.py` | `check()` 在 Windows 改用 `OpenProcess + GetExitCodeProcess` 探活 | 建议（避免 `--check` 误杀） |
| C4 | `backend/app/multitenancy/worker_process.py` | `--directory` 默认值支持环境变量覆盖 | 可选（托管脚本显式传参即可规避） |
| C5 | `backend/app/multitenancy/worker_process.py` | `progress()` 去掉 pid 相等校验，改按 `instance + kind + healthy + 新鲜度` 判定；同步 `supervise()`/`check()` 调用点与 `tests/test_multitenancy_worker_process.py` | **必需**（否则 E11 使 worker 恒在 `MAX_AGE` 后 `exit 1`） |

实施情况：C1/C2/C3/C5 已按上述最小改动落地，`tests/test_multitenancy_worker_process.py tests/test_full_schedules.py` 实测 13 passed / 7 skipped；4 类 worker 已在 `deploy/windows/tsp-full.ps1` 托管下稳定运行并越过各 `MAX_AGE` 宽限期。C4 未实施（托管脚本已显式传 `--directory`）。

托管层已注册为 **NSSM 服务 `TSPWorkerWatchdog`**（原计划任务 `TSP-Worker-Watchdog` 已注销）：

- 可执行体：`C:\Tools\nssm\nssm.exe`（NSSM 2.24-101，64 位，不属于仓库内容）。
- 服务账户 `NT AUTHORITY\LocalSystem`，`Start = SERVICE_AUTO_START`，**无需用户登录即可随开机启动**。
- `Application = C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`；`AppParameters = -NoProfile -ExecutionPolicy Bypass -File "E:\Projects\TSP\deploy\windows\tsp-watchdog.ps1" -IntervalSeconds 30 -CooldownSeconds 120`；`AppDirectory = E:\Projects\TSP`。
- 自愈：`AppExit Default Restart` + `AppRestartDelay 5000` + `AppThrottle 10000`；`AppKillProcessTree` 保持默认 1。
- 日志：`E:\pg-tsp\logs\watchdog-service.out.log` / `watchdog-service.err.log`，`AppRotateFiles 1`、`AppRotateOnline 1`、`AppRotateBytes 10485760`。
- 常用运维：`Get-Service TSPWorkerWatchdog`、`Restart-Service TSPWorkerWatchdog`、`& C:\Tools\nssm\nssm.exe edit TSPWorkerWatchdog`、`& C:\Tools\nssm\nssm.exe remove TSPWorkerWatchdog confirm`。

后端三项同样已服务化（2026-10-19 落地，均为 `SERVICE_AUTO_START`）：

- `TSPPostgres`：`pg_ctl register -N TSPPostgres -D E:\pg-tsp\data -S auto -l E:\pg-tsp\logs\postgres.log` 注册；服务本体为 `"E:\pg-tsp\pgsql\bin\pg_ctl.exe" runservice -N "TSPPostgres" -D "E:\pg-tsp\data" -w`，账户 `NT AUTHORITY\LocalSystem`。数据目录与日志目录对 SYSTEM 已有完全控制，无需额外授权。
- `TSPPlatform`（3018）：NSSM 服务，`Application = powershell.exe`，`AppParameters = -NoProfile -ExecutionPolicy Bypass -File "E:\Projects\TSP\deploy\windows\tsp-service.ps1" -Role platform`，`AppDirectory = E:\Projects\TSP`，账户 `LocalSystem`。
- `TSPApi`（3030）：同上，`-Role api`。
- `tsp-service.ps1` 是新增长驻入口（不是 worker 托管脚本）：显式读取 `E:\pg-tsp\secrets.env` 注入进程环境，再按角色设置变量——platform 为 `SERVICE_ROLE=platform` + `TSP_PUBLIC_MARKET_DIR=E:\pg-tsp\public_market`；api 为 `SERVICE_ROLE=api` + `DATA_DIR=E:\pg-tsp\public_market\current` + `PLATFORM_CONFIG_DIR=E:\Projects\TSP\data\user_data`。两者最终都在 `backend` 目录执行 `.venv\Scripts\python.exe -m uvicorn app.main:app --env-file E:\Projects\TSP\.env --host 0.0.0.0 --port 3018|3030`。**不能依赖继承环境**：交互终端里并没有 `TSP_CELL_CONTROL_DSN` 等变量，服务必须以 secrets 文件为准。
- 日志：`E:\pg-tsp\logs\platform-service.out.log` / `.err.log`、`api-service.out.log` / `.err.log`，均开启 10 MB 在线轮转；NSSM 重启策略与看门狗一致（`AppExit Default Restart`、`AppRestartDelay 5000`、`AppThrottle 10000`）。
- 依赖链（`DependOnService`）：`TSPPlatform` → `TSPPostgres`；`TSPApi` → `TSPPostgres`；`TSPFrontend` → `TSPApi`；`TSPWorkerWatchdog` → `TSPPostgres, TSPPlatform, TSPApi`。开机顺序由 SCM 保证，无需再手工等待。
- 启停顺序：启动直接 `Start-Service TSPPostgres` 后依次 `TSPPlatform`、`TSPApi`、`TSPFrontend`、`TSPWorkerWatchdog`（依赖已满足时 SCM 会自动带起前置服务）；停止必须先 `tsp-full.ps1 stop` 停 4 类 worker，再 `Stop-Service TSPWorkerWatchdog`、`TSPFrontend`、`TSPApi`、`TSPPlatform`、`TSPPostgres`。
- **已知限制**：NSSM 的 `AppKillProcessTree` 不会清理看门狗用 `Start-Process` 拉起的 worker supervisor，直接 `Stop-Service TSPWorkerWatchdog` 会停在 `STOP_PENDING`，直到这些 supervisor 退出为止。因此停止顺序必须先停 worker；系统关机时由 SCM 超时强杀，不影响下次开机。
- 验收结果：五项服务均 `Running / Automatic`；3018、3030、3012 均在监听；`3018/health/live`、`3030/health/live` 返回 200；`3030/health/ready` 仍为 503，检查项中只有 `compute_broker=error`（既定决策），`databases`、4 类 worker、`public_files`、`platform`、`indicator_cache` 全部 `ok`。服务化后的自愈验证：`taskkill` research supervisor 后先因 120s 冷却 `[hold ]`，冷却结束（21:56:48）自动拉起新 supervisor，4 类 worker 恢复全绿。

前端 `TSPFrontend`（3012）也已服务化（2026-10-19 落地）：

- 直接以 `C:\Program Files\nodejs\node.exe` 作为 `Application`，`AppParameters = node_modules\vite\bin\vite.js --host 0.0.0.0 --port 3012 --strictPort`，`AppDirectory = E:\Projects\TSP\frontend`。不用 `pnpm`：其 shim 位于 `C:\Users\Administrator\AppData\Roaming\npm`，不在 LocalSystem 的 `PATH` 中；直接调用 Vite CLI 可少一层 `cmd`，进程树更干净。
- `AppEnvironmentExtra = BACKEND_HOST=127.0.0.1 BACKEND_PORT=3030`，`/api` 与 `/health` 由 Vite 代理到 3030，SSE 头处理沿用 `vite.config.ts`。
- `--strictPort` 保证端口被占用时启动失败并由 NSSM 重试，而不是静默换到 3013。
- 账户 `LocalSystem`、`SERVICE_AUTO_START`、`DependOnService = TSPApi`；日志 `E:\pg-tsp\logs\frontend-service.out.log` / `.err.log`，10 MB 在线轮转，重启策略与后端一致。
- 验证：`http://127.0.0.1:3012/` 与 `/src/main.tsx` 均 200；`/health/live` 经代理返回 200；`/api/health` 与直连 3030 的响应完全一致（503 仅来自 `compute_broker`）；`Restart-Service TSPFrontend` 后 8 秒内恢复监听。
- 仍然是 **Vite 开发服务器**（与之前手动启动的形态一致），不是生产构建。若后续需要生产形态，应另行走 `pnpm build` + 静态服务器 + 反向代理，不要直接用 `vite preview` 替代。生产形态的构建、Edge、证书、备份与回滚步骤见 [Windows 10 生产构建与部署](./windows10-production-deployment.md)。

注册过程中修复了看门狗自身的一个阻塞缺陷：`Invoke-Restart` 原先用管道读取 `tsp-full.ps1 restart` 的输出，而它拉起的 python supervisor 会继承管道写句柄且永不退出，导致管道等不到 EOF、看门狗在第一次重启后永久卡死（20:18 与 20:22 两次实测均复现）。现改为 `Start-Process -RedirectStandardOutput/-RedirectStandardError` + `WaitForExit(120000)`（只等进程句柄，不等待流 EOF）。端到端验证：服务启动后手工 `taskkill` research supervisor，21:01:22 出现 `[down ]`→`[heal ]`，21:01:24 新 supervisor（pid 8528）拉起，随后状态表 4 类 worker 全部 `Running=True / Healthy=True`。注意：服务以 LocalSystem 运行，由它自愈拉起的 worker 也属于 `NT AUTHORITY\SYSTEM`，与服务启动前以当前用户运行的 worker 会短暂并存；如遇权限差异，请用管理员身份执行 `tsp-full.ps1`。

约束：以上均为平台分支，**不改变 Linux/容器行为**（Linux 下 venv python 不是启动器，pid 校验本就成立，放宽后仍由每次新生成的 `instance` 防串扰）；按仓库规则，本地迭代不递增应用版本号，合并 `main` 后由 CI 统一递增。

---

## 9. 验收清单（命令级）

**L1 基础设施**

```powershell
curl.exe -s http://127.0.0.1:3030/health/ready
# 期望：research/realtime/notifications/scheduled/public_files/platform/indicator_cache = ok
#       compute_broker = error（既定），ready = false
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:3030/api/health).StatusCode   # 期望 200
```

**L2 业务可用**

```powershell
curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:3012/    # 前端 200
# admin/Admin123 登录 → GET /api/account/tenants → 期望 200 且含「本地工作区」
# GET /api/overview/market → 期望 200（非 409「请选择目标租户」）
```

**L3 常驻自愈**

```powershell
Get-Content E:\pg-tsp\run\workers\*\progress.json | ConvertFrom-Json | Select-Object kind,healthy
# 期望：4 项 healthy = true，且每 ≤5s 刷新一次 LastWriteTime
curl.exe -s http://127.0.0.1:3030/api/health   # 期望 4 项 worker ok，age_seconds 远小于阈值
```

**L4（仅轨道 B）**

```powershell
curl.exe -s http://127.0.0.1:3030/health/ready | Select-String '"ready": true'
```

---

## 10. 风险、回滚与「明确不做」

- **风险 R1**：Windows 上 `subprocess.terminate()` 是硬杀（无信号语义），worker 不会走优雅关闭 → 依赖「重启即恢复」而非 graceful shutdown。看门狗需在重启前清理 `owner.json`/`progress.json`，避免新旧实例心跳互串（`worker_process.progress()` 用每次 `supervise()` 新生成的 `instance` 校验，同一目录下的旧实例心跳一律判为无效，安全）。
- **风险 R2**：`progress.json` 的 `at` 是 `time.monotonic()`，**跨进程不可比较**；运维脚本只能依据文件时间戳与 `healthy` 字段，不要试图换算。
- **风险 R3**：多 worker 实例会各自写 `control.full_worker_health`（按 `(cell_id, kind)` upsert）。同一 kind 只允许一个实例，托管脚本需做单实例保护。
- **回滚**：托管脚本停止 → 恢复为交互式前台启动即可；代码补丁均为平台分支，单独 revert 不影响 Linux。
- **明确不做**：不为 Windows 实现 compute 的替代传输层或本地无沙箱计算通道（会移除租户隔离，且与既定决策相悖）。

---

## 附：环境变量契约（worker 与 3030 同源）

以 `deploy/full/compose.yml` 的 `x-common` 为准（`SERVICE_ROLE=api`、`MULTITENANCY_ENABLED=true`、`TSP_CELL_SCHEMA_VERSION=6`、`DATA_DIR=<public_market>/current`、`PLATFORM_CONFIG_DIR`、`TSP_CELL_MARKET_URL` / `TSP_CELL_MARKET_TOKEN` / `TSP_PLATFORM_*_TOKEN`、`TSP_CELL_POOL_SIZE`、`TSP_CELL_WORKER_CONCURRENCY`、`POLARS_MAX_THREADS` 等）。本机事实源为 `E:\pg-tsp\secrets.env`（`TSP_CELL_ID=local` 等）；该文件不含 `DATA_DIR` / `PLATFORM_CONFIG_DIR` / `TSP_WORKER_HEARTBEAT_DIR`，托管脚本必须显式补齐，否则 worker 会以错误的行情根目录启动。
