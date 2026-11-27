# ZV-E10 原生 preload 真机验证清单（2.03）

> **2026-11-26 真机结论：本清单只在“真正冷启动”时才有意义。** 相机正常暖启动走
> WBI1 快照恢复（WBI 在 `/dev/nflasha5`，`/proc/ssboot/state=snapshot`），
> 不执行 `init`/`bootin`，因此正常重启后本清单的步骤必然看不到 `LD_PRELOAD`。
> 判断方法与存储位置见
> [运行注意事项](ZVE10_FW203_RUNTIME_NOTES.md) 的 ssboot 一节；让 WBI 失效并完成物理冷启动的步骤见
> [WBI 冷启动 Runbook](WBI_COLDBOOT_RUNBOOK.md)。
>
> 适用：ZV-E10 Gen 1（`ILCE-ZV-E10`）、系统软件 **2.03**（`30110_06.2026060501`）。
> 目标：确认 `bootin.elf` 确实接受了 preload 内容，且插件随开机进入 `im.elf`。
> 全部命令在本仓库根目录执行；USB 命令必须**串行**，任何时刻只允许一个 `cam.py` / `probe_zve10_runtime.py`。

---

## 0. 前置条件（缺一不可）

- [ ] 相机型号为 ZV-E10 Gen 1，固件 2.03（`--check` 会双次读取核对）
- [ ] **SD 卡已插入**（插件门禁要求 `B:` 挂载，未插卡会静默禁用插件，容易误判）
- [ ] 电量充足或使用假电池供电；验证中途不要断电
- [ ] USB 线连接，相机 USB 模式设为「大容量存储」
- [ ] 待部署的 `prebuilt/libzve10plugin.so` 已通过 `verify_elf_abi.py`
- [ ] 没有其它 `cam.py` / `probe_zve10_runtime.py` 进程在运行

> 关于 2.02：本清单**不适用**。2.02 的 `bootin.elf` 尚未取得样本，不能假定同一门禁存在。

---

## 1. 验证前状态采集（只读）

```powershell
uv run python tools/probe_zve10_runtime.py --backend jiritsu --preload-status
```

逐项记录：

- [ ] `dmode=` 为 `3`（原厂）或 `3p`（已配置）
- [ ] `preload=` 为空或旧值
- [ ] `/usr/tool` 列表中**没有** `libmcheckb.so`（这正说明旧版逻辑不会加前缀）
- [ ] 看门狗 `fail_count`：若 `>= 1`，先执行第 2 步的「可选：重置看门狗」

> 说明：该命令不写 `/setting`、不写闪存；但 Jiritsu 后端会临时上传 RAM 加载器，结束时自动清理。

---

## 2. 部署并启用 preload

### 推荐：一条命令完成（校验 → 上传 → 配置 → 重启）

```powershell
uv run python tools/probe_zve10_runtime.py --backend jiritsu --deploy --reboot --so prebuilt/libzve10plugin.so
```

### 可选：分步执行（便于定位失败点）

```powershell
uv run python tools/probe_zve10_runtime.py --backend jiritsu --check
uv run python tools/probe_zve10_runtime.py --backend jiritsu --stage-tmp
uv run python tools/probe_zve10_runtime.py --backend jiritsu --enable-preload --reboot
```

### 可选：重置看门狗（仅当第 1 步 `fail_count >= 1`）

```powershell
uv run python tools/probe_zve10_runtime.py --backend jiritsu --reset-boot-guard
```

> 该命令只删除 `/tmp/zvp_boot_guard`（插件自己的运行期状态），不触碰原厂配置。

### 打勾项（`--deploy` / `--enable-preload` 输出）

- [ ] ELF/ABI 校验通过
- [ ] 固件双次读取一致、原厂基线字节匹配
- [ ] 插件上传后 SHA-256 回读一致
- [ ] 出现 `门禁     = 通过（规则 #3: /usr/tool/libmcheckb.so）`
- [ ] 出现 `写入内容 = /usr/tool/libmcheckb.so.zvp-gate-decoy:/tmp/libzve10plugin.so`
- [ ] 出现 `dmode=3p`
- [ ] 出现 `preload=/usr/tool/libmcheckb.so.zvp-gate-decoy:/tmp/libzve10plugin.so`
- [ ] 出现 `[3/4] 回读校验通过`
- [ ] `state.json` 中 `service_returned=true`、`loader_files_removed=true`

---

## 3. 重启后核心核验（只读）

等待相机重新枚举（约 30 秒），然后：

```powershell
uv run python tools/probe_zve10_runtime.py --backend jiritsu --preload-status
```

**按顺序打勾（前 3 项就是 preload 是否真正生效的判据）：**

- [ ] **A. 开机日志**：`/setting/ulogio.bin` 中出现
      `LD_PRELOAD=/usr/tool/libmcheckb.so.zvp-gate-decoy:/tmp/libzve10plugin.so`
      （等级 `B`；这是 `bootin.elf` 通过门禁后才打印的行）
- [ ] **B. 初始环境**：`/proc/<im.elf pid>/environ` 中出现同一个 `LD_PRELOAD=`
- [ ] **C. 进程映射**：`/proc/<im.elf pid>/maps` 中 `libzve10plugin` 计数 **> 0**
- [ ] **D. 配置仍在**：`dmode=3p`，`preload=` 未被清空
- [ ] **E. 看门狗**：`fail_count=1` 属**正常**（本次会话尚未正常退出前就是 1）；
      只有 `>= 2` 才表示下次加载会触发熔断
- [ ] **F. 插件日志**：`--logs` 中出现
      `GEN_CacheInsImg 640x480 RGBA4444 画布初始化完成` 和
      `zve10plugin UI主线程激活成功: … canvas=1`

---

## 4. 功能确认

- [ ] 拔掉 USB，打开一次原厂 MENU 再退出回取景画面
- [ ] 按 **C1** 键，插件菜单出现
- [ ] 触控/按键操作正常
- [ ] （可选，验证持久性）再软重启一次，重复第 3 步

---

## 5. 失败判读表

| 现象 | 含义 | 下一步 |
|---|---|---|
| ulogio 只有 `M … /tmp/libzve10plugin.so` | 内容被门禁拒绝（缺少白名单前缀） | 检查 preload 文件是否被截断、是否带换行；重新 `--enable-preload` |
| ulogio 既无 `B` 也无 `M` | `dmode` 未含 `p`，或 `/setting` 未挂载 | 核对 `dmode=3p`；确认 `/setting/mode` 存在 |
| 有 `B`，但加载器警告指出 `libzve10plugin` | 插件 `.so` 本身不可加载（ABI/依赖/损坏） | 重新校验并上传 `.so`，看 `--logs` |
| 有 `B`、maps>0，但日志 `卡控门禁拦截启动: DISABLED_NO_CARD` | preload 成功，插件按设计静默禁用 | 插 SD 卡后重启 |
| 日志 `开机看门狗自熔断触发` / preload 被清空 | 连续 >=2 次在门禁评估前异常退出 | `--reset-boot-guard` 后重新 `--enable-preload` |
| `--preload-status` 里 `dmode=3`、`preload=` 为空 | 配置被回滚（熔断或 `--disable-preload`） | 重新启用 |

---

## 6. 回滚到原厂

```powershell
uv run python tools/probe_zve10_runtime.py --backend jiritsu --disable-preload --reboot
```

- [ ] 重启后 `dmode=3`、`preload=` 为空
- [ ] `im.elf` maps 中无 `libzve10plugin`，`/tmp/zve10plugin.log` 无新增

---

## 7. 失败时请回传的证据

1. `--preload-status` 的**完整输出**
2. `--logs` 的完整输出
3. 命令输出里提到的 `state.json`（Jiritsu 会话记录）
4. 说明：**从第几步开始**与预期不符（例如「第 3 步 A 有，但 C 为 0」）

---

## 8. 明确不要做

- 不要写 `/etc/ld.so.preload`
- 不要修改 WBI、原始分区、启动文件
- 不要把 preload 内容改回裸 `/tmp/libzve10plugin.so`
- 不要并发执行 USB 命令
- 不要用 `/proc/<pid>/environ` 推断运行期环境变化（它只反映 `exec` 时的环境）

---

## 9. 预期耗时

约 5 分钟（不含等待重连）；首次失败排查约 10–15 分钟。
