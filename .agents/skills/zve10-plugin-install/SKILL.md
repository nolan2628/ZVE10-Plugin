---
name: zve10-plugin-install
description: >-
  Automated installation, ARM build/ABI verification, USB staging, preload setup,
  log diagnostics, and stock rollback skill for ZVE10-Plugin on Sony ZV-E10 (Gen 1).
  Use when a user asks an AI Agent to install, deploy, build, verify, troubleshoot,
  or uninstall ZVE10-Plugin onto their Sony ZV-E10 camera.
---

# ZVE10-Plugin 自动化安装与运维 Skill

本 Skill 用于指导 AI Agent 安全地将 **ZVE10-Plugin** 编译、校验、推送并启用到一台 **第一代 Sony ZV-E10（`ILCE-ZV-E10`）** 相机上，或在需要时安全恢复至原厂状态。

---

## 1. 前置检查与依赖准备

在向相机执行任何命令前，请先向用户确认以下 3 项条件（缺一则先引导用户准备）：

1. **机型确认**：仅支持第一代 **Sony ZV-E10 (`ILCE-ZV-E10`)**。若用户是 ZV-E10 II 或其他型号，**立即拒绝安装**。
2. **相机连接与 Windows 驱动状态**：
   - 相机已插入 SD 卡并开机；
   - 相机菜单：`设置 (Setup) -> USB 连接模式 -> 大容量存储 (Mass Storage)`；
   - 通过数据线直连电脑（避免使用不稳定扩展坞）；
   - **Windows 首次使用驱动（Zadig `libusbK`）**：如果 `--check` 检测不到设备，请先引导用户下载 [`zadig-2.9.exe`](https://github.com/pbatard/libwdi/releases/download/v1.5.1/zadig-2.9.exe)，连接相机后在 Zadig 中选中相机设备（必要时勾选 `Options -> List All Devices`），**安装 `libusbK` 版本驱动**；
   - **务必提前告知用户**：连接 USB 后相机屏幕出现 **「请检查所连接的设备」** 属于**正常现象，绝对不能拔掉数据线**，保持连接即可。
3. **宿主机工具依赖**：
   - 必须已安装 [`uv`](https://docs.astral.sh/uv/)；
   - 若需要从源码编译 `.so`，还需具备 WSL2 / Linux 环境下的 `cmake` 与 `arm-linux-gnueabi-gcc` / `arm-linux-gnueabi-g++`；
   - **内置调试与推机工具 `third_party/camtool`、认证层 `third_party/pmca` 与 Jiritsu 部署工具 `third_party/zve10-toolkit`**：分别作为 Git Submodule 挂载了 [`Sony-ZVE-10-Camtool`](https://github.com/nolan2628/Sony-ZVE-10-Camtool)、[`ma1co/Sony-PMCA-RE`](https://github.com/ma1co/Sony-PMCA-RE) 与 [`dino2048/zv-e10-toolkit`](https://github.com/dino2048/zv-e10-toolkit)。若对应目录为空，先在仓库根目录执行 `git submodule update --init --recursive` 拉取。`tools/probe_zve10_runtime.py` 会自动注入这些子模块路径（也可用 `--cam-py` / `ZVP_CAM_PY` / `PMCA_ROOT` 覆盖）。

---

## 2. 必须严格遵守的安全红线

1. **严禁并发操作 USB / `camtool`**：同一时刻只能运行一个 `probe_zve10_runtime.py` 或 `cam.py` 进程；推送、启用、重启、等待、读取日志必须**单命令严格串行**执行。
2. **推送前必须校验 ELF ABI**：只要重新编译了 `build-arm/libzve10plugin.so`，推送前必须执行 `tools/verify_elf_abi.py` 并确认输出 `ELF/ABI/GLIBC 约束全部校验通过`（目标板仅支持 ARMv7 soft-float EABI5 与 `<= GLIBC_2.11.2`）。
3. **禁止直接覆写内存已映射文件**：切勿手动 `cam.py put` 覆写 `/tmp/libzve10plugin.so`，必须使用 `probe_zve10_runtime.py --stage-tmp`（内置先 `rm -f` 再上传逻辑）。
4. **先体检再启用**：首次启用前务必运行 `--check`，确认 `cam.py doctor` 正常以及原厂基线字节校验通过（`libNetService.so` 为 `90 b5 95 b0`，`CautionConfig.so` 为 `20`）。

---

## 3. 标准安装流程（按顺序执行）

### 固件 2.03 的安装路径（Jiritsu + 冷启动 preload）

固件 `30110_06.2026060501` 使用 `--backend jiritsu`。该后端通过 `third_party/zve10-toolkit` 子模块复用加密 GET_FILE、持久化命令会话和加载器清理。先初始化子模块：

```powershell
git submodule update --init third_party/camtool third_party/pmca third_party/zve10-toolkit
```

**2.03 的关键限制**：正常开机使用 ssboot/UPM 暖启动快照恢复，不执行 `init`/`bootin`，因此 `preload` 只在真正冷启动时生效。WBI 仍有效的相机，先按 [`docs/WBI_COLDBOOT_RUNBOOK.md`](../../../docs/WBI_COLDBOOT_RUNBOOK.md) 完成一次受控的 WBI 失效和物理冷启动；WBI 已失效的相机，可直接使用下面的部署命令。

```powershell
uv run python tools/probe_zve10_runtime.py --backend jiritsu --deploy --reboot --so prebuilt/libzve10plugin.so
```

`--deploy` 在一个串行 USB 会话中完成 ABI 校验、双次固件版本检查、原厂基线检查、分块上传与 SHA-256 回读校验、preload 设置；`--reboot` 会先清理并核对加载器文件，再通过现有 RAM 会话安排软件重启。检查输出目录内 `state.json` 的 `service_returned`、`loader_files_removed`，等待相机重新连线后执行 `--backend jiritsu --wbi-verify` 和 `--logs`，再按原厂 MENU → 取景 → C1 的顺序验证菜单。`--deploy` 不加 `--reboot` 时只完成上传与配置，适合配合 WBI 冷启动流程。

WBI 分区写入属于破坏性操作，必须先完成独立只读诊断和完整备份，并由机主单独明确授权；不要为了取得日志或应用 preload 直接清除 WBI。Jiritsu 初始化或清理失败时保留会话记录，检查实际状态后处理；基线不一致时停止部署。

默认 Camtool 后端仍使用下面的逐步流程。

### Step 1：确认预编译产物（或按需重新编译）

1. **优先使用仓库内置预编译产物**（推荐，普通用户无需安装交叉编译工具链）：
   检查 `prebuilt/libzve10plugin.so` 或 `build-arm/libzve10plugin.so` 是否存在，并先执行 ABI 校验：
   ```powershell
   uv run --with pyelftools python tools/verify_elf_abi.py prebuilt/libzve10plugin.so
   ```
   > 若通过校验且未修改 C 源码，可直接跳至 **Step 2**（`tools/probe_zve10_runtime.py --stage-tmp` 会自动优先选用 `build-arm/libzve10plugin.so`，不存在时自动回退使用 `prebuilt/libzve10plugin.so`）。

2. **如用户修改了源码或需从零编译**：
   先确认已拉取 LVGL 子模块，再在 Linux / WSL2 中运行主机单测与 ARM 交叉编译：
   ```bash
   git submodule update --init --recursive

   cmake -S . -B build-host -DZVP_BUILD_HOST_TESTS=ON
   cmake --build build-host -j
   ctest --test-dir build-host --output-on-failure

   cmake -S . -B build-arm -DCMAKE_TOOLCHAIN_FILE=cmake/zve10_arm_soft.cmake
   cmake --build build-arm -j
   ```
   然后执行校验：
   ```powershell
   uv run --with pyelftools python tools/verify_elf_abi.py build-arm/libzve10plugin.so
   ```

> 仅在看到 `[通过] libzve10plugin.so ELF/ABI/GLIBC 约束全部校验通过！` 后方可继续下一步。

### Step 2：检查相机 USB 连接与基线状态（只读）

```powershell
uv run python tools/probe_zve10_runtime.py --check
```

若找不到 `cam.py`，请显式传入路径，例如：
```powershell
uv run python tools/probe_zve10_runtime.py --cam-py <path-to-cam.py> --check
```

核对输出：
- `cam.py doctor` 正常连接；
- `preload=` 当前状态（初次为 `NONE` 或已配置值）；
- `verify firmware bytes on disk` 与期望字节一致（`90 b5 95 b0` 与 `20`）。

### Step 3：推送插件至相机临时区

```powershell
uv run python tools/probe_zve10_runtime.py --stage-tmp
```

确认输出末尾显示 `/tmp/libzve10plugin.so` 权限为 `-rwxr-xr-x` 且大小约 900KB 左右。

### Step 4：启用 `preload` 注入引导

> **2026-11-26 真机结论（重要）**：ZV-E10 正常开机使用 ssboot/UPM 暖启动快照恢复
> （`/proc/ssboot/state=snapshot`），**不执行 `init`/`bootin`**，因此本步骤写入的
> preload 在正常重启后不会生效（`/setting/env.txt` 哨兵也不会被读取）。白名单门禁只在
> **真正冷启动**时才是限制。2.03 请按 [`docs/WBI_COLDBOOT_RUNBOOK.md`](../../../docs/WBI_COLDBOOT_RUNBOOK.md)
> 先完成受控的 WBI 失效和物理冷启动；不要因为日志里没有 `LD_PRELOAD` 就反复重试本步骤。

```powershell
uv run python tools/probe_zve10_runtime.py --enable-preload
```

> **机制（已逆向确认，勿改成裸路径）**：`bootin.elf` 只接受**以 4 条
> `/usr/tool/*` 路径之一为前缀** 的 preload 内容；其它内容会被静默拒绝，
> `LD_PRELOAD` 根本不会设置。因此工具写入的是：
>
> ```
> /usr/tool/libmcheckb.so.zvp-gate-decoy:/tmp/libzve10plugin.so
> ```
>
> 第一段仅用于通过前缀比对且**保证不存在**（设备加载器打印
> `cannot be preloaded: ignored` 后继续启动）；第二段才是插件本体。
> 该内容由 `tools/zvp_preload_gate.py` 离线复现门禁并校验。
>
> **不要**改回 `/tmp/libzve10plugin.so` 裸路径；也不要依赖
> `/usr/tool/libmcheckb.so` 是否存在——2.03 原厂镜像里没有这个文件，它只影响
> **冷启动**路径的前缀判定。正常开机不加载的根因是暖启动跳过 `bootin`
> （见 `docs/ZVE10_FW203_RUNTIME_NOTES.md` 的 ssboot 一节），不是本门禁。

命令会打印门禁判定、写入内容，并回读核对 `dmode=3p` 与 `preload` 是否一致；
任何不一致都会返回非 0 并停止后续步骤。

### Step 5：重启相机并等待重连，拉取运行日志验证

使用串行命令执行重启、等待 30 秒设备重连与读取日志（`--reboot` 已内置串行 `reboot` + `wait 30` 与 `PMCA_ROOT` 注入）：

```powershell
uv run python tools/probe_zve10_runtime.py --reboot; if ($LASTEXITCODE -eq 0) { uv run python tools/probe_zve10_runtime.py --logs }
```

重连后先做一次**只读**核对（不写任何文件）：

```powershell
uv run python tools/probe_zve10_runtime.py --backend jiritsu --preload-status
```

> 完整可勾选的真机验证清单见 [`docs/ZVE10_PRELOAD_VERIFY_CHECKLIST.md`](../../../docs/ZVE10_PRELOAD_VERIFY_CHECKLIST.md)。

**preload 是否真的生效（按顺序看这三项）**：
1. `/setting/ulogio.bin` 中出现
   `LD_PRELOAD=/usr/tool/libmcheckb.so.zvp-gate-decoy:/tmp/libzve10plugin.so`
   —— 这行是 bootin 通过门禁后才打印的；若里面只有裸 `/tmp/libzve10plugin.so`
   （等级 `M`），说明内容被拒绝；
2. `im.elf` 的 `/proc/<pid>/environ` 里存在同一个 `LD_PRELOAD=`；
3. `/proc/<pid>/maps` 中 `libzve10plugin` 计数 > 0。

**插件是否工作的判据**：日志末尾应包含类似输出：
- `GEN_CacheInsImg 640x480 RGBA4444 画布初始化完成`
- `zve10plugin UI主线程激活成功: tid=... c1_key=0x150102a0 canvas=1 sounds=...`

> 插件在原厂 UI 的首次动画帧中完成初始化。验证时先拔掉 USB，打开一次原厂 MENU 再退出回取景画面，然后按 **C1**；不要直接在 USB 大容量存储界面按 C1 判定。

### Step 6（可选）：导入示例快门音效与自定义 LUT 滤镜包到 SD 卡

- **自定义快门音**：将 `assets/sounds/` 中的 `.WAV` 文件复制到 SD 卡 `/PRIVATE/M4ROOT/GENERAL/SOUNDS/` 目录下。
- **自定义 LUT 滤镜包**：将 `ZVLOOK01` 格式的 `.bin` 滤镜包复制到 SD 卡 `/ZVC/` 或 `/PRIVATE/SONY/PRO/LUT/` 目录下，在插件「滤镜」页点击顶栏 `SD导入` 图标即可热加载。

---

## 4. 故障排查与彻底恢复原厂（卸载）

- **用户想要恢复原厂引导 / 卸载插件**：
  ```powershell
  uv run python tools/probe_zve10_runtime.py --disable-preload; if ($LASTEXITCODE -eq 0) { uv run python tools/probe_zve10_runtime.py --reboot }
  ```
  执行完成后，相机即重启切回原厂 `mode 3` 无注入状态。
- **日志显示 `卡控门禁拦截启动`**：
  - `DISABLED_NO_CARD`：请插入 SD 卡后再开机；
  - `DISABLED_KILL_FILE`：SD 卡根目录或 `/tmp/` 存在 `ZVP_OFF` 文件，删除后重启即可恢复。
- **日志显示 `开机看门狗自熔断触发` / `preload` 被自动清空**：
  插件连续 `>=2` 次在门禁评估完成前就随进程退出（例如实验期间频繁断电、
  手动杀进程），会自行把 `dmode` 写回 `3` 并清空 `preload`。
  这不是 preload 机制失败，而是自熔断保护；重新执行 `--enable-preload` 即可，
  并尽量用 `--reboot` 让进程正常结束。
  `--preload-status` 会解码 `/tmp/zvp_boot_guard` 的 `fail_count`/`trip_count`；
  若 `fail_count >= 1`，可在**开始验证前**执行 `rm -f /tmp/zvp_boot_guard`
  让计数从 0 开始（该文件是插件自己的运行期状态，不属于原厂配置）。
- **菜单按键无反应或触控错位**：
  - 检查是否在 `设置与安全` 中把 C1 改为了 `长按 >=500ms`（需长按 C1 半秒）；
  - 可在 `设置与安全` 中连续两次点击 `安全复位 (全量恢复默认)` 一键重置。
