# ZV-E10 2.03 运行注意事项

## 适用范围

仅适用于第一代 Sony ZV-E10、固件 `30110_06.2026060501`。当前结论限于原厂显示正常及 C1 菜单打开；快门音效暂未纳入本验证范围。

## 为什么必须冷启动

ZV-E10 正常开机使用 Sony UPM/ssboot 的用户态快照恢复，内核启动后直接恢复内存镜像，**不执行 `/sbin/init`**，因此 `/usr/bin/bootin.elf` 不会运行，`/setting/mode/dmode` 与 `/setting/mode/preload` 也不会被读取。原生 preload 在正常暖启动上不可能生效。

因此，2.03 的插件加载路径是：

1. 先按 [WBI 冷启动 Runbook](WBI_COLDBOOT_RUNBOOK.md) 完成一次受控的 WBI 失效和物理冷启动；
2. 冷启动时 `bootin.elf` 读取 `dmode=3p` 与 preload 配置，将插件加载进 `im.elf`；
3. 之后日常开关机是 STR（挂起到 RAM），插件保留在内存中；拔电池/断电再上电才会再次冷启动并重新加载插件。

WBI 分区写入是唯一的破坏性步骤，必须先完成独立只读诊断和完整备份，并由机主单独明确授权。不要为了取得日志或应用 preload 直接清除 WBI。

## 部署与更新

在仓库根目录操作。相机开机、插入 SD 卡，USB 设为大容量存储；USB 命令严格串行。

```powershell
git submodule update --init third_party/camtool third_party/pmca third_party/zve10-toolkit
uv run --with pyelftools python tools/verify_elf_abi.py prebuilt/libzve10plugin.so
```

WBI 仍有效时，先按 WBI Runbook 的 S2 完成上传和 preload 配置（**不要加 `--reboot`**，此时重启只是暖恢复）：

```powershell
uv run python tools/probe_zve10_runtime.py --backend jiritsu --deploy --so prebuilt/libzve10plugin.so
```

WBI 已失效的相机，可以在同一条命令后加 `--reboot`，让冷启动加载新插件：

```powershell
uv run python tools/probe_zve10_runtime.py --backend jiritsu --deploy --reboot --so prebuilt/libzve10plugin.so
```

`--deploy` 会依次完成 ABI 校验、双次固件版本检查、原厂基线字节检查、分块上传插件并做 SHA-256 回读、写入 `dmode=3p` 与 preload，最后清理加载器文件。任何一步失败都会停止后续步骤。请保留命令输出的 `Session:` 目录及其中 `state.json`，并确认 `loader_files_removed=true`、`service_returned=true`。

## 冷启动后的验证

相机重新枚举 USB 后，先执行只读核对：

```powershell
uv run python tools/probe_zve10_runtime.py --backend jiritsu --wbi-verify
```

应看到：

- `__WARMBOOT__ = 0`
- `__STATE__ = idle`
- `__DMODE__ = 3p`
- `__MAPS__ >= 1`（`im.elf` 中出现 `libzve10plugin`）
- WBI 头全零

再读取插件日志：

```powershell
uv run python tools/probe_zve10_runtime.py --backend jiritsu --logs
```

应出现：

- `GEN_CacheInsImg 640x480 RGBA4444 画布初始化完成`
- `zve10plugin UI主线程激活成功: tid=... c1_key=0x150102a0 canvas=1 sounds=...`

插件在原厂 UI 的首次动画帧中完成初始化。日志出现上述内容后，拔掉 USB，打开一次原厂 MENU 再退出回取景画面，随后按 **C1**；不要直接在 USB 大容量存储界面按 C1 判定菜单。

完整的逐项打勾清单见 [ZV-E10 原生 preload 真机验证清单](ZVE10_PRELOAD_VERIFY_CHECKLIST.md)。

## 原生 preload 机制（2.03 逆向结论）

> **适用范围：仅冷启动路径。** 正常开机走 ssboot/UPM 暖启动，`init`/`bootin` 不执行，本节机制不会被触发。

`/usr/bin/bootin.elf` 的 preload 实现有一个**内容白名单**：只有以
`/usr/tool/LeakTracer.so`、`/usr/tool/libefence.so`、`/usr/tool/libjemalloc_tsh.so`、
`/usr/tool/libmcheckb.so` 之一为**前缀**的内容才会被 `putenv("LD_PRELOAD=…")`；
其它内容只写一条等级 `M` 的 ulogio 日志，**完全不设置 `LD_PRELOAD`**。
该路径上限 498 字节，路径写死为 `/setting/mode/preload`，并且换行会进入变量值。

2.03 原厂镜像的 `/usr/tool` 只有 `LeakTracer.so` 与 `libjemalloc_tsh.so.1`，
**没有 `libmcheckb.so`**。此前 `--enable-preload` 只在
`[ -r /usr/tool/libmcheckb.so ]` 成立时才加前缀，因此在真机上始终写入裸
`/tmp/libzve10plugin.so`，被上述门禁静默拒绝——这是"配置写好了、开机却不加载"的根因。

现在的写入内容固定为：

```text
/usr/tool/libmcheckb.so.zvp-gate-decoy:/tmp/libzve10plugin.so
```

首段只用于通过前缀比对，`zvp-gate-decoy` 后缀保证它永远不存在；设备加载器
（glibc 2.11.2）对无法加载的 preload 项只打印
`ERROR: ld.so: object '…' from LD_PRELOAD cannot be preloaded: ignored.` 并继续启动。
门禁逻辑由 `tools/zvp_preload_gate.py` 离线复现，写入内容由 `--enable-preload` 回读核对。

## 看门狗与故障判读

插件自带开机看门狗（`ZVP_BOOT_GUARD_MAX_FAILS=2`）：连续两次在门禁评估完成前随进程退出，就会自行写回 `dmode=3` 并清空 `preload`。

`--preload-status` 会解码 `/tmp/zvp_boot_guard`（magic `0x5A564247`）中的 `fail_count` / `trip_count`。若开始验证前 `fail_count >= 1`，执行：

```powershell
uv run python tools/probe_zve10_runtime.py --backend jiritsu --reset-boot-guard
```

让计数从 0 开始；该文件是插件自己的运行期状态，不属于原厂配置。

日志显示 `开机看门狗自熔断触发` 时，重新执行 `--enable-preload` 或重新部署，不要判定 preload 机制失效。

## 回退与诊断

停止 preload 引导：

```powershell
uv run python tools/probe_zve10_runtime.py --backend jiritsu --disable-preload --reboot
```

重启后确认 `dmode=3`、`preload=` 为空、`__MAPS__ = 0`。相机 `/tmp` 是持久文件系统，`/tmp_network` 才是 tmpfs；普通插件文件和 preload 配置不会因为一次重启自动消失。

日志读取使用 Jiritsu GET_FILE，无需上传加载器。Jiritsu 初始化或清理失败时保留会话记录、`state.json` 和完整输出，检查实际状态后处理；不要盲目重试，也不要直接覆盖相机上的 `/tmp/libzve10plugin.so`。

## 开发与验证

运行相关离线回归测试：

```powershell
uv run --with pyusb --with libusb-package python -m unittest discover -s tests -p "test_*.py"
```

任何新编译的插件上传前都应再次执行 ELF/ABI 校验。静态校验只覆盖部分 ABI 约束，不能代替真实加载测试。
