# ZVE10-Plugin 冷启动启用：逐步执行清单

> 目标：让相机**真冷启动**一次，从而让已写好的工厂 `bootin` preload 把插件加载进 `im.elf`。
> 之后日常开关机是 STR（挂起到 RAM），插件一直在内存里；只有拔电池/断电才需要重新加载，而那时同样走冷启动。
>
> 原理与证据：本地逆向记录（未随仓库发布）。
> 实现：`tools/wbi_coldboot.py`（已离线自检 + 假后端干跑通过）；原厂工具哈希见 `tools/payload/README.md`。

**每步单独一条命令，等它跑完再跑下一条。任何一步报错就停下来，把整段输出贴出来。**

---

## 前置：先完成备份预检（必做）

在跑本清单的 S0 之前，先按 [`BACKUP_PREFLIGHT_RUNBOOK.md`](BACKUP_PREFLIGHT_RUNBOOK.md) 完成一次
完整的 Pico/Fastboot 救援备份（user area + Boot0/Boot1 双读、nflasha2/5/10 独立双读、配置文件、
`pico-kit`）。备份目录就绪后再回来执行 S0；S1 的 `--wbi-status` 与备份会话使用同一套退出验证。

---

## 路径约定（先看这个，避免误解）

| 名字 | 在哪 | 是什么 |
|---|---|---|
| `--wbi-dir` 指向的文件夹 | **你的 PC** | 脚本自动创建的**输出目录**，存放读回来的 60 MiB 备份、回读镜像和 JSON 清单。名字随意（本文用 `ZVE10-Backups\2026-11-27-WBI`）。**不会被传到相机。** |
| `--baseline` 指向的文件 | **你的 PC** | 11-26 那次会话在这台相机上读出的 60 MiB 整分区备份（`aa584ec9…`，头 `WBI1`）。只用于"现在的闪存是否和昨天一模一样"的比对。**不会被写到相机。** |
| `tools\payload\ud_wbi_drop.elf` | 你的 PC | 原厂 6200 B 工具（`f2a5b27a…`），第 4 步会被上传到**相机 RAM** 后执行。 |
| `/dev/nflasha5` | **相机** | 60 MiB 的 WBI 分区。第 4 步只清它偏移 0 的 512 B。 |

数据流：

```
第 3 步：相机 /dev/nflasha5 ──只读──▶ PC  wbi-before-*.bin（60 MiB，与 baseline 比对）
第 4 步：PC ud_wbi_drop.elf ──上传──▶ 相机 /tmp_network/… ──执行──▶ 写 /dev/nflasha5 头 512 B
        相机 /dev/nflasha5 ──回读──▶ PC  wbi-after-*.bin（校验只有头 512 B 变了）
```

---

## 前置条件

| 项 | 要求 |
|---|---|
| 机型/固件 | 第一代 ZV-E10，2.03（`30110_06.2026060501`） |
| 连接 | 数据线直连；相机开机；`设置 → USB 连接模式 → 大容量存储` |
| 驱动 | Zadig 装 **libusbK**；相机显示"请检查所连接的设备"是正常的，**别拔线** |
| SD 卡 | 必须插着（插件的卡控门禁要求） |
| 磁盘 | ≥ 200 MB 空闲（一份 60 MiB 备份 + 一份回读） |
| 工作目录 | 所有命令在仓库根目录（`ZVE10-Plugin`）下执行 |
| 耗时 | 单次全分区读约 5–30 分钟；整条流程约 1–1.5 小时 |

---

## S0 · 离线自检（不用连相机）

```powershell
uv run python tools\wbi_coldboot.py --selftest --tool tools\payload\ud_wbi_drop.elf
```

**作用**：验证校验逻辑（头解析、512 B 边界、拒绝非法状态）和原厂工具哈希，不碰相机。
**期望**：最后一行 `SELFTEST OK`。
**若失败**：把输出贴出来，不要继续。

---

## S1 · 只读体检（WBI 头 + 运行态）

```powershell
uv run python tools\probe_zve10_runtime.py --backend jiritsu --wbi-status
```

**作用**：读 512 B 头、`/proc/ssboot/state`、`/proc/udm/upm_bootmode`、`/proc/uptime`、WARMBOOT 计数。只读。
**期望**：

```
version      = 'WBI1'  -> valid (steady-state fast-start image)
sections     = 664
sector_size  = 512
__STATE__ … snapshot
__WARMBOOT__ … 1
```

**判读**：
- `WBI1` → 正常，继续 S2。
- 已经是全零 → 说明之前就失效了，**跳过 S3/S4**，直接从 S2 开始（装完插件断电冷启动）。
- 其它值（`prof`/`WBI0`/`invl`）→ 停下来发我。

**S1 同时验证会话退出流程**（这是本步的第二个目的）。命令末尾必须出现：

```
=== 会话退出验证（同一会话内完成，不重开会话） ===
  loader_upload_attempted = True
  loader_files_removed    = True
  service_returned        = True
  usb_service_stopped     = True
  usb_pids_after_stop     = [...]
  session_state           = closed
  exit_verification       = OK
```

- `loader_files_removed=True`：三个临时 loader 文件已用 GET_FILE 回读确认不存在；
- `service_returned=True`：服务停止调用已返回；
- `exit_verification=OK`：本会话干净退出，可以继续 S2。
- 任一项不是 `True`/`OK`：**停止后续实验**，把输出和 `state.json`（第一行的会话目录里）发我。
- **禁止为确认关闭状态而再开一次 Jiritsu 会话**——S1 的输出就是验证。

---

## S2 · 装插件（上传 + 写 dmode/preload，不重启）

```powershell
uv run python tools\probe_zve10_runtime.py --backend jiritsu --deploy --so prebuilt\libzve10plugin.so
```

**作用**：ABI 校验 → 双次固件版本 → 原厂基线字节 → 分块上传插件并 SHA-256 回读 → 写 `/setting/mode/dmode=3p` 与 `preload`（门禁装饰串）并回读核对。
**期望**：`preload=` 回读一致，结尾 `service_returned=true`。
**注意**：**不要加 `--reboot`**——现在重启是暖恢复，preload 不会执行，纯属浪费一次会话。插件要等到 S5 冷启动才会真正被加载。

可选（清掉历史看门狗计数，让本次验收的 `fail_count` 从 0 开始）：

```powershell
uv run python tools\probe_zve10_runtime.py --backend jiritsu --reset-boot-guard
```

---

## S3 · 清零前核对（只读，60 MiB 读回 + 与 baseline 逐字节比对）

```powershell
uv run python tools\probe_zve10_runtime.py --backend jiritsu --wbi-prepare `
  --wbi-dir <备份目录> `
  --baseline <baseline.bin>
```

**作用**：把整分区读回 PC（`wbi-before-<时间>.bin`），并与 11-26 的备份逐字节比对；头部必须是 `WBI1`。
**期望**：

```
  已保存 …\wbi-before-20261127-xxxxxx.bin  sha256=aa584ec9…
  与给定 baseline 逐字节一致（跳过第二次全读）
[3/3] 头部 WBI1，sections=664，指纹通过
准备完成: …\wbi-manifest-20261127-xxxxxx.json
```

**若报 `fresh read differs from --baseline`**：说明这台的闪存和昨天不一样。**去掉 `--baseline` 重跑**，做完整双读；然后把两份哈希贴给我，再决定是否继续。
**耗时**：一次全读，最长约 30 分钟。

---

## S4 · 写入（唯一动 eMMC 的一步）

确认 S3 已输出"指纹通过"后再执行：

```powershell
uv run python tools\probe_zve10_runtime.py --backend jiritsu --wbi-drop `
  --wbi-dir <备份目录> `
  --i-authorize-emmc-write
```

**作用**：
1. 再整分区读一遍，与 S3 的备份逐字节比对（不一致就停）；
2. 上传 `ud_wbi_drop.elf` + `partinf.conf` 到相机 RAM 的 `/tmp_network/zve10-xxxx/wbi-<token>/`；
3. 执行 `./ud_wbi_drop.elf partinf.conf`；
4. 期望输出**恰好** `[UD_WBI_DROP] /dev/nflasha5 wbi_drop`；
5. `busybox sync` 并校验返回码 0（工具本身以 `O_RDWR|O_SYNC` 打开分区）；
6. 整分区回读，校验**只有前 512 字节变化**；
7. 清理 RAM 临时文件，写 `wbi-drop-<token>.json` 清单。

**期望结尾**：

```
  仅前 512 字节变化，其余 62914048 字节不变
=== WBI 写入验证 ===
  sony_output     = [UD_WBI_DROP] /dev/nflasha5 wbi_drop
  filesystem_sync = ok
  readback_bytes  = 62914560
  changed_bytes   = 512（仅头 512 B）
  write_verified  = True
写入完成且校验通过。
=== 会话退出验证（同一会话内完成，不重开会话） ===
  loader_files_removed    = True
  service_returned        = True
  usb_service_stopped     = True
  session_state           = closed
  exit_verification       = OK
```

**若第 6 项失败**（例如 `write touched byte … outside the allowed sector`）：脚本会先把回读镜像和清单落盘再报错。**不要重跑**，保留 `--wbi-dir` 目录和完整输出。
**若 `exit_verification` 不是 `OK`**：即使写入本身校验通过，也按失败处理（清理/退出异常不可忽略），不要断电重启，先发我输出。
**耗时**：两次全读（写前比对 + 写后回读），最长约 1 小时。

---

## S5 · 物理断电（你来操作）

1. 等窗口显示 `service_returned=true`；
2. **拔 USB 线**；
3. 相机拨到 OFF，等存取灯熄灭；
4. **关机后屏幕可能再次点亮（无 UI）——这时不要拔电池**。这是关机过程中的显示/余电释放，等约 **30 秒**，确认相机完全无反应（屏幕不再亮、无声音、无镜头动作）；
5. 再取出电池，等约 30 秒；
6. 装回电池，开机。

> 这是唯一的真冷启动入口。**不要** `fastboot reboot`，不要软件重启，不要在会话还活着时拔电池；也不要在屏幕刚重新亮起时拔电池。

---

## S6 · 冷启动验收

相机开机、重新连上 USB 后：

```powershell
uv run python tools\probe_zve10_runtime.py --backend jiritsu --wbi-verify
```

**判据（缺一不可）**：

| 项 | 期望 | 含义 |
|---|---|---|
| `__WARMBOOT__` | `0` | 这次是冷启动（唯一可靠判据） |
| `__STATE__` | `idle` | 不是快照恢复 |
| `__MAPS__` | `≥ 1` | `im.elf` 里出现了 `libzve10plugin` |
| `__DMODE__` | `3p` | 工厂 preload 开关已生效 |
| `__PRELOAD__` | 门禁装饰串 | 配置还在 |
| WBI 头 | 全零 | 镜像确实失效了 |

再看插件日志：

```powershell
uv run python tools\probe_zve10_runtime.py --backend jiritsu --logs
```

**期望**：出现 `画布初始化完成` 和 `UI主线程激活成功: … canvas=1`。然后在相机上按 **C1** 打开插件菜单。

**如果 `__WARMBOOT__` = 1**：说明这次仍是暖恢复（镜像没失效，或电池没取干净/取的时间太短）。不要重跑 S4，先发我 `--wbi-status` 的输出。

---

## 中止条件（命中任意一条就停）

- S1 的版本不是 `WBI1`（也不是全零）；
- S3 的 fresh read 与 baseline 不一致，或保存失败；
- S4 的输出不是那行原文；
- S4 的回读校验报"512 字节以外被改"；
- 会话状态不是 `closed` / `service_returned=true`。

**永远不要连续重跑 S4。** 镜像已经失效后再清一次没有任何意义。

---

## 恢复路径

- 写入前的完整 60 MiB 镜像：`<备份目录>\wbi-before-*.bin`。
- **恢复不走手写**：官方 2.03 固件更新会重写整个 `nflasha5`（升级器内部同样调用 `ud_wbi_drop.elf` 并写入完整 WBI 载荷），这是唯一被验证过的恢复路径。
- 只想临时停用插件：SD 卡根目录放 `ZVP_OFF`，或执行 `--disable-preload`，下次冷启动即回原厂引导。

---

## 之后会发生什么

- 这次冷启动之后，**日常关机/开机是 STR**（写 `"mem"`，挂起到 RAM），插件一直在内存里，不需要重新加载；
- 拔电池/断电再上电 = 又一次冷启动 → `bootin` preload 重新加载插件；
- 若相机将来在某条 `0x99` 关机路径里重建了 WBI，头会变回 `prof/WBI0/WBI1`（`--wbi-status` 能看到）。那时升级插件需要重新做一次 S4；详见本地逆向记录 §7。

---

## 附：S4 擦除范围审计（为什么它不会多擦一个字节）

对 `tools/payload/ud_wbi_drop.elf`（6200 B，`f2a5b27a…`）做了完整反汇编审计：

**全程序只有一个写调用。** 外部调用清单（`objdump -d` 统计，去重）：

```
printf ×11   puts ×3   memset ×2   feof ×2   fclose ×2   __errno_location ×2
strlen ×1    snprintf ×1   fseek ×1   fopen ×1   fgets ×1   ferror ×1
open ×1      write ×1      close ×1    exit ×1    abort ×1
（没有 fsync / ioctl / lseek / ftruncate / mmap）
```

**那唯一的写，长这样**（`main`，Thumb）：

```
886c  movw r1, #0x1002        ; O_RDWR|O_SYNC
8870  blx  open               ; 打开 "/dev/nflasha%d"
8890  movs r1, #0
8892  add  r0, r7, #4
8894  mov.w r2, #512
8898  blx  memset             ; 把栈上 512 B 缓冲清零
889c  add  r1, r7, #4
889e  mov  r0, r6             ; fd
88a0  mov.w r2, #512
88a4  blx  write              ; write(fd, zeros, 512)  ← 全部写入就这一次
88c6  mov  r0, r6
88c8  blx  close
```

关键点：

1. **写长度是硬编码的 `#512`（0x200）**，不是分区表里的 `size` 字段。
   `size`（`0x03C00000`）只被用来做一次 `size >= 512` 的合理性检查（`8836`–`883c`）；`size` 为 0 时打印 "wbi size is zero. skip" 直接跳过。
2. **没有任何 `lseek`**，打开后文件偏移就是 0，所以写的位置固定是**偏移 0**。
3. 写完立刻 `close`，**没有 fsync/ioctl**，也不碰分区其它区域。
4. 程序按分区表逐行扫描 `type==2` 的条目（`ldr r3,[r5,#40]; cmp r3,#2`），我们的 `partinf.conf` 里**只有一行 type=2**（哈希被钉死为 `9bb1cba1…`），所以只命中一个设备。
5. 设备号来自**行号**（`/dev/nflasha%d` 的 `%d` 是第几行），不是 `start address` 字段——这正是必须用官方五行顺序的原因，而我们的配置是哈希锁定的。

主循环之后最多再扫 30 个空条目（`cmp r4,#31`），没有其它副作用。

**我们这层的额外保证**（`tools/wbi_coldboot.py`）：

| 保证 | 实现 |
|---|---|
| 工具不可替换 | 长度 6200 + SHA-256 `f2a5b27a…` 不匹配就拒绝运行 |
| 分区配置不可改 | `CONFIG_SHA256` 不匹配就拒绝 |
| 写前状态正确 | 整分区与基准逐字节相同，且头必须是 `WBI1` |
| 写后边界正确 | 整分区回读：前 512 B 必须全零，**其余 62,914,048 B 必须逐字节不变** |
| 失败留证 | 回读镜像与清单在抛错前先落盘 |

**会话期间还有哪些 eMMC 写入？**（与本擦除无关，但应一并说明）

- Jiritsu 加载器文件 `/tmp/jiritsu/{run.sh, libJit.so, out.txt}`：这是工具包每次连接都会做的标准动作，位于 eMMC 上的 `/tmp`，会话结束 `backend.close()` 时删除并校验（`loader_files_removed=true`）。
- 其余全部落在 `/tmp_network/zve10-<hex>`（1 MiB tmpfs，纯 RAM）：上传的工具、`partinf.conf`、512 B 头读取缓冲。

**结论**：S4 对闪存的改动 = 加载器文件的建/删（标准流程）＋ `/dev/nflasha5` 偏移 0 的 512 字节。分区其余 60 MiB 原厂载荷保持原样，只是头部归零后 Loader2 不再认它。

---

## 按顺序汇总（复制用）

在仓库根目录（`ZVE10-Plugin`）下，逐条执行：

```powershell
# S0
uv run python tools\wbi_coldboot.py --selftest --tool tools\payload\ud_wbi_drop.elf
# S1
uv run python tools\probe_zve10_runtime.py --backend jiritsu --wbi-status
# S2
uv run python tools\probe_zve10_runtime.py --backend jiritsu --deploy --so prebuilt\libzve10plugin.so
# S2b（可选）
uv run python tools\probe_zve10_runtime.py --backend jiritsu --reset-boot-guard
# S3
uv run python tools\probe_zve10_runtime.py --backend jiritsu --wbi-prepare --wbi-dir <备份目录> --baseline <baseline.bin>
# S4
uv run python tools\probe_zve10_runtime.py --backend jiritsu --wbi-drop --wbi-dir <备份目录> --i-authorize-emmc-write
# —— 拔 USB、关机、取电池 30 秒、装回开机、等重新连上 ——
# S6
uv run python tools\probe_zve10_runtime.py --backend jiritsu --wbi-verify
# S7
uv run python tools\probe_zve10_runtime.py --backend jiritsu --logs
```