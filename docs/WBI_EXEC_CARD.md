# WBI 冷启动 + 插件加载：执行卡（手动）

> 依据 [`WBI_COLDBOOT_RUNBOOK.md`](WBI_COLDBOOT_RUNBOOK.md)，本页只保留“照着敲 + 判读 + 红线”。执行前先按 [`BACKUP_PREFLIGHT_RUNBOOK.md`](BACKUP_PREFLIGHT_RUNBOOK.md) 完成备份。

**执行前基线**：先用 `--backend jiritsu --preload-status` 记录当前 `dmode`、`preload`、看门狗计数和 `/tmp/libzve10plugin.so` 的 SHA-256；用 `--wbi-status` 记录 WBI 头状态。以下判据以 S1 输出为准。

---

## 执行清单

| # | 命令 | 期望 / 判据 | 不满足就停 |
|---|---|---|---|
| **S0** | `uv run python tools\wbi_coldboot.py --selftest --tool tools\payload\ud_wbi_drop.elf` | 最后一行 `SELFTEST OK` | 停止，贴输出 |
| **S1** | `uv run python tools\probe_zve10_runtime.py --backend jiritsu --wbi-status` | `version = 'WBI1'`、`sections = 664`、`__STATE__ … snapshot`、`__WARMBOOT__ … 1`、结尾 `exit_verification = OK` | 版本是 `prof/WBI0/invl` → 停；全零 → 跳过 S3/S4 直接 S2 |
| **S2** | `uv run python tools\probe_zve10_runtime.py --backend jiritsu --deploy --so prebuilt\libzve10plugin.so` | `preload=` 回读与写入一致、`service_returned=true`、`exit_verification=OK` | 任何不一致/非 0 → 停 |
| **S3** | `uv run python tools\probe_zve10_runtime.py --backend jiritsu --wbi-prepare --wbi-dir <备份目录> --baseline <baseline.bin>` | `与给定 baseline 逐字节一致`、`[3/3] 头部 WBI1，sections=664，指纹通过` | `fresh read differs` → 不要写，去掉 `--baseline` 重跑并贴两份哈希 |
| **S4** | `uv run python tools\probe_zve10_runtime.py --backend jiritsu --wbi-drop --wbi-dir <备份目录> --i-authorize-emmc-write` | `changed_bytes = 512`、`write_verified = True`、`filesystem_sync = ok`、`exit_verification = OK`；sony 输出恰好 `[UD_WBI_DROP] /dev/nflasha5 wbi_drop` | 任何其它结果：**不重跑**，保留目录贴输出 |
| **S5** | 物理断电：等 `service_returned=true` → 拔 USB → 拨 OFF 等灯灭。**若屏幕再次点亮（无 UI），不要拔电池**，等约 30 s 确认相机无反应 → 取电池 30 s → 装回 → 开机 | — | 不要在会话还活着或屏幕刚重新亮起时拔电池 |
| **S6** | `uv run python tools\probe_zve10_runtime.py --backend jiritsu --wbi-verify` | `__WARMBOOT__=0`、`__STATE__=idle`、`__MAPS__>=1`、`__DMODE__=3p`、WBI 头全零 | `__WARMBOOT__=1` → 停，发 `--wbi-status`，不要重跑 S4 |
| **S7** | `uv run python tools\probe_zve10_runtime.py --backend jiritsu --logs` | `画布初始化完成`、`UI主线程激活成功: … canvas=1` | 没有 → 贴日志 |

**S0–S7 逐条执行，一条跑完再下一条。** S4 的 `--wbi-dir` 目录会自动创建。
**S2 不加 `--reboot`**；S2 结尾的"RAM 子 shell 会在关机断电后消失"只是提示，本步不需要断电。

---

## 红线（绝对不做）

1. **同一时刻只允许一个** `probe_zve10_runtime.py` / `cam.py` / Jiritsu 进程；所有步骤严格串行，不同时开两个 PowerShell 窗口操作相机。
2. **S5 之前不绝断**：不拔电池、不关机、不拔 USB（S1/S2/S3/S4 都是会话内完成）。S5 才是唯一允许的断电点；S5 关机后若屏幕再次点亮（无 UI），**不要拔电池**，等约 30 s 确认相机无反应再取电池。
3. **S2 不加 `--reboot`**；任何步骤都不使用 `fastboot reboot` 或软件重启代替 S5 的物理断电。
4. **S4 只执行一次**：`write_verified != True` 时也绝不"再试一次"——镜像可能已部分失效，重跑只会扩大未知。
5. **`exit_verification != OK` 一律按失败处理**，即使动作本身看起来成功；不要继续下一步，不要为确认状态重开 Jiritsu 会话。
6. **不直接覆盖相机上的 `/tmp/libzve10plugin.so`**（禁止 `cam.py put`），只走 `--deploy` / `--stage-tmp`。
7. **不使用其他相机的备份**：S3/S4 只用本机备份目录；版本必须是 `30110_06.2026060501`。
8. **不跳过 S3**：没有"指纹通过"就不执行 S4。
9. **不删除/不覆盖任何备份目录**；S3/S4 的新文件只写入当前 `--wbi-dir`。
10. **任何报错就停**：保留整段输出、`state.json`、`wbi-*.json` / `*.bin`，先贴给我再决定。

---

## 失败时怎么办

| 现象 | 处理 |
|---|---|
| S1 版本不是 WBI1（也不是全零） | 停，贴输出；不要继续装插件 |
| S2 `preload` 回读不一致 | 停；重新跑 S2 前先看 `--preload-status` |
| S3 与 baseline 不一致 | 不要写；去掉 `--baseline` 做完整双读，贴两份哈希 |
| S4 输出不是 `[UD_WBI_DROP] /dev/nflasha5 wbi_drop` | 保留 `--wbi-dir` 全部文件，贴输出；不要重跑 |
| S4 回读报"512 字节以外被改" | 同上；**不要**再写任何东西 |
| S6 `__WARMBOOT__=1` | 仍是暖恢复：不要重跑 S4，发 `--wbi-status` 输出 |
| S6 `__MAPS__=0` / S7 无日志 | 插件没加载：贴 `--wbi-verify` + `--logs`，不要反复重启 |

**恢复兜底**：`<备份目录>\raw-backup-…\nflasha.read1.bin` + `offline-verification.json` + `pico-kit\`；Pico 路线只作最后手段（上游仅验证 2.02/macOS）。

---

## 时间

| 步骤 | 预计 |
|---|---|
| S0–S2 | 2–4 分钟（部署上传约 600 KB） |
| S3 | 30–60 秒（60 MiB 全读） |
| S4 | 2–4 分钟（写前比对 + 512 B 写 + 全分区回读） |
| S5 | 约 1 分钟（含取电池 30 s） |
| S6–S7 | 1–2 分钟 |
