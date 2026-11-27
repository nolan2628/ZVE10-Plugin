# WBI 冷启动实验前的完整备份 —— 执行清单

目标：在做任何 WBI 实验之前，把「Pico / BootROM / Fastboot 救援」需要的一切都落到本机磁盘上，并证明它们来自这台 2.03 固件的相机。

本阶段**只备份、只读采集、离线校验、源码审计**；不执行 WBI drop、不写任何分区、不做恢复、不接 Pico。

---

## 0. 前置条件

| 项 | 要求 |
|---|---|
| 磁盘 | 输出目录所在盘 **≥ 6 GiB 可用**（用户区约 1.9 GiB ×2 遍 + B0/B1 + 1 GiB 余量） |
| 相机 | ZV-E10 Gen1，固件 **2.03 / `30110_06.2026060501`**，电池满电，USB 数据线 |
| 电脑 | Windows，已装 `uv`；相机未被其他程序占用（不要同时开 cam.py / probe） |
| 目录 | 建议输出到仓库外的独立目录，例如 `ZVE10-Backups\PRE-WBI` |
| 禁止 | 本阶段不要运行 `--wbi-drop`、不要写 `--i-authorize-emmc-write`、不要动 Pico |

---

## 1. 命令（逐条手动执行，工作目录 `ZVE10-Plugin`）

```powershell
# B0 自检（不连相机，约 1 秒）
uv run python tools\backup_preflight.py --selftest

# B1 审计已有备份（不连相机，约 1 分钟）
uv run python tools\backup_preflight.py --audit

# B2 采集（连相机，一个 Jiritsu 会话，约 35–45 分钟）
uv run python tools\backup_preflight.py --collect --out ..\ZVE10-Backups\PRE-WBI

# B3 离线校验 + 切片 + Pico 准备（不连相机，约 3–5 分钟）
uv run python tools\backup_preflight.py --offline --out ..\ZVE10-Backups\PRE-WBI
```

`--collect` 会自己用 `uv run --with pyusb --with libusb-package --with pycryptodomex --with pyelftools` 重启解释器，不需要手工装依赖。

---

## 2. 每一步的判读

### B0 自检

最后一行必须是 `SELFTEST OK`。

### B1 审计（检查已有备份）

| 目录 | 内容 | 结论 |
|---|---|---|
| `已有 WBI 备份` | `wbi-read-1.bin` / `wbi-read-2.bin` 各 62,914,560 B，SHA-256 均为 `aa584ec9…32e5`，头部 `WBI1` | **有效，可复用**；本次全盘读取的 nflasha5 切片应得到同一哈希 |
| `audio-before-rollback-20261126-178e7c22` | 82.2 MiB / 372 个文件，插件源码与构建产物 | 不是闪存备份，不参与恢复 |

审计输出应为「[已有] WBI 全区双读」+ 其余全部「[缺]」，然后按 B2 补齐。

### B2 采集（关键，一个会话内完成）

脚本按顺序做：

1. 采集 `/proc/partitions`、`/proc/mounts`、`/proc/cpuinfo`、`/proc/version`、`/proc/meminfo`、sysfs 每个 `nflasha*` 的 `dev/size/start/ro`、eMMC CID/CSD（`metadata\`）；
2. `/version.txt` **连续读两次**，必须都等于 `30110_06.2026060501`，否则立即中止；
3. 调 toolkit 官方 `raw-backup`（`passes=2`, `chunk_kib=4096`）读 **user area + B0 + B1**，每区两遍，逐 1 MiB 块哈希比对；B0/B1 不一致会直接抛错；
4. 对 **nflasha2 / nflasha5 / nflasha10** 各做两次独立 `dd` 读取（512 KiB 块，逐块 sha256 校验），写 `targeted\`；nflasha5 两遍不一致会中止；
5. 读取配置文件（GET_FILE）：`/setting/Backup.bin`、`/setting/Backup.bak`、`/setting/kemco.txt`、`/setting/mode/dmode`、`/setting/mode/preload`、`/setting/env.txt`、`/tmp/libzve10plugin.so`、`/tmp/zvp_boot_guard`——**不存在的记录 `present=false`，不算失败**；
6. 写 `backup-manifest.json` + 全套 `SHA256SUMS`，然后正常关闭 Jiritsu 会话。

**结束判读**（必须原样出现）：

```
=== 会话退出验证（同一会话内完成，不重开会话） ===
  loader_upload_attempted = True
  loader_files_removed    = True
  service_returned        = True
  usb_service_stopped     = True
  session_state           = closed
  exit_verification       = OK
```

判读规则：`exit_verification = OK` 只表示**会话干净关闭**（loader 已删且回读确认、服务调用返回、USB 服务停止调用返回）；采集是否成功看脚本最后一行/退出码。若采集动作失败但会话干净关闭，会看到 `session_state = failed` + `exit_verification = OK`——此时备份无效，按错误信息处理后重跑。

任何一项不是 `True`/`OK` → **停**，把 `state.json` 和 `session-exit.json` 发我。**不要为确认关闭状态再开一次 Jiritsu 会话。**

**如果 `--collect` 中途失败**：只有 `metadata\` 的失败残留会自动复用并重新采集；一旦已生成 `raw-backup-…\`，脚本会拒绝写入非空目录，请换新目录（或手动清空）后再跑，防止半成品混入正式备份。

### B3 离线校验

脚本按顺序做：

1. 复核 `SHA256SUMS` 全部文件；
2. `zve10_verify.py manifest` + `sums` 验原始备份；
3. `zve10_verify.py slices` 用 `layout.tsv` 逐分区比对两遍全盘读取，输出 `offline-verification.json`；
4. 从 `nflasha.read1.bin` 切出 `partitions\nflasha2.bin`、`nflasha5.bin`、`nflasha10.bin`，哈希必须等于 `read1_sha256`；同时生成 `.simg`；
5. `restore_rescue.py --partitions nflasha2,nflasha5,nflasha10 --prepare-only` 官方交叉验证（不接相机）；
6. 生成 `pico-kit\`（17 个文件，见 §4）。

**汇总必须是全 `true`**：

```json
{ "files": true, "raw_manifest": true, "raw_sums": true,
  "slices": true, "targets": true, "restore_prepare": true }
```

`difference-analysis.json`：两遍全盘读取有差异的分区会列出 `mount`、`rw`、`different_bytes`。挂在 `rw` 上的分区（`/setting`、`/tmp` 等）在采集期间变化属预期；**只读挂载却出现差异 = 必须停下分析**。

---

## 3. 输出目录结构

```
PRE-WBI\
├─ backup-manifest.json          总清单（固件、几何、哈希、targeted、config）
├─ SHA256SUMS                    全目录文件校验
├─ layout.tsv                    实际分区表（起始扇区 → 偏移 + 容量）
├─ session-exit.json             会话退出状态（loader 清理/服务返回/USB stop）
├─ metadata\                     partitions / mounts / cpuinfo / version.txt / identity …
├─ raw-backup-…\                 官方双遍全盘备份
│   ├─ nflasha.read1.bin / read2.bin
│   ├─ nflashaB0.read1.bin / read2.bin
│   ├─ nflashaB1.read1.bin / read2.bin
│   ├─ manifest.json / SHA256SUMS
├─ targeted\                     nflasha2/5/10 独立双读
├─ config\                       Backup.bin / Backup.bak / kemco.txt / dmode / preload …
├─ partitions\                   nflasha2/5/10 .bin + .simg（Pico 用）
├─ offline-verification.json     逐分区 read1/read2 哈希与差异
├─ difference-analysis.json      差异及原因分析
├─ pico-rescue\                  restore_rescue --prepare-only 产物
└─ pico-kit\                     Pico/fastboot 工具包（见 §4）
```

---

## 4. Pico 恢复所需的文件和工具

`pico-kit\` 内已包含（含 `KIT-MANIFEST.json` 逐个 SHA-256）：

| 文件 | 大小 | 用途 |
|---|---:|---|
| `prebuilt/stage1.bin` | 7,636 B | Pico 串口注入的第一级（USB 传输/内存访问） |
| `prebuilt/stage2-flash.bin` | 10,228 B | fastboot stage2，支持 raw/sparse 写 |
| `prebuilt/manifest.json` + `SHA256SUMS` | — | 官方镜像版本/校验 |
| `host/restore_rescue.py` | — | 切片 + sparse + 写入 + 回读校验 |
| `host/bootstrap.py` | — | stage2 加载（依赖 pyusb） |
| `tools/make_sparse.py` | — | 独立 sparse 生成器 |
| `tools/check_abi.py` | — | stage1/stage2 ABI 配对校验 |
| `pico/inject_payload.py` | — | 串口注入 stage1 |
| `pico/pico_link.py` / `inspect_pico.py` / `reset_only.py` | — | 串口链路/状态/复位 |
| `docs/recovery.zh-CN.md` / `docs/hardware.zh-CN.md` | — | 官方恢复流程与接线 |

**还需自备的硬件/软件**（不在仓库里）：

- RP2040 Raspberry Pi Pico + 24pin USB-C 转接板 + 复位电阻与 CC 下拉（按 `docs/hardware.zh-CN.md` 接线）；
- Pico 发送固件 `zve10_uart_od 15`：用 `arduino-cli`（rp2040 core 6.3.0）编译 `pico/firmware/zve10_pico_od_reset` 得到 UF2，BOOTSEL 拷入；
- 电脑：Python 3.10+、libusb、Android platform-tools 的 `fastboot`（PATH 中或 `--fastboot` 指定）。

**镜像格式与大小要求**（`restore_rescue.py` 源码确认）：

- 输入必须是**整盘用户区镜像** `nflasha.read1.bin` + `offline-verification.json`；
- 每个分区 `offset % 512 == 0`、`size % 4096 == 0`、`offset+size <= 镜像大小`，且 `read1_sha256` 必须匹配；
- 写出格式为 libsparse（`make_sparse.py` 生成，28 B 头 + 12 B 块头，4096 B 块）；
- 支持的分区集合：`nflasha{1,2,3,4,5,6,7,10,11,12,13,15,16,17,18,23}` —— **nflasha2 / nflasha5 / nflasha10 全部支持**；
- **不支持 Boot0/Boot1、分区表、RPMB/OTP、物理硬件故障**——这些只能靠本目录的原始镜像留档，不能由该工具写回。

**重要限制**：上游实机记录只覆盖固件 **2.02 / macOS**；2.03 + Windows 的 Pico 路线**未经验证**，只能作为最后手段。本阶段的 `--prepare-only` 只证明镜像/切片/哈希正确，不证明实机可写。

---

## 5. 安全规则

1. **只用本机备份**：`--collect` 双读 `/version.txt` 并记录 CID；任何其它相机的 `nflasha.read1.bin` 禁止混用。
2. **本阶段绝不写相机**：不运行 `--wbi-drop`，不加 `--i-authorize-emmc-write`。
3. `--collect` 结束前必须看到 `exit_verification = OK`；否则视为失败，不进入后续实验。
4. 备份目录写完后设为只读/另盘复制一份；`SHA256SUMS` 与 `backup-manifest.json` 一起保存。
5. 出现任何 `FAIL`：保留现场（`state.json`、`.part`、日志），不要盲目重试。

---

## 6. 时间预算

| 步骤 | 预计 |
|---|---|
| B0 selftest | < 1 s |
| B1 audit | ~1 min（120 MiB 哈希） |
| B2 collect | 35–45 min（用户区 2 遍 ≈ 31 min + 目标分区 2 遍 ≈ 2 min + 配置/元数据） |
| B3 offline | 3–5 min（~3.8 GiB 哈希 + 切片 + sparse） |
