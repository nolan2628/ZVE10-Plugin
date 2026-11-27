#!/usr/bin/env python3
"""
Sony ZV-E10 运行期探针与 Phase 1 部署验证工具 (基于 camtool)
功能：
  --check         检查相机连接、关键库加载基址与固件基线字节
  --stage-tmp     将编译好的 libzve10plugin.so 推送到 /tmp/libzve10plugin.so（不改 /setting）
  --logs          拉取相机端 /tmp/zve10plugin.log 诊断日志
  --preload-status 只读核对 preload 配置、开机日志指纹与 im.elf 映射（不写任何文件）
  --enable-preload 写入合规的 dmode=3p + preload 内容（见 tools/zvp_preload_gate.py）
  --disable-preload 恢复原厂 mode 3

关于 preload 内容：bootin.elf 只接受以 4 条 /usr/tool/* 路径为前缀的内容，
详见 tools/zvp_preload_gate.py。
"""
from __future__ import annotations

import argparse
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

import zvp_preload_gate as preload_gate

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, ".."))
BUILD_SO = os.path.join(REPO_ROOT, "build-arm", "libzve10plugin.so")
PREBUILT_SO = os.path.join(REPO_ROOT, "prebuilt", "libzve10plugin.so")
DEFAULT_SO = BUILD_SO if os.path.isfile(BUILD_SO) else PREBUILT_SO
# 相机上插件常驻路径；也是 preload 内容的第二段（第一段见 zvp_preload_gate.GATE_DECOY）
PLUGIN_PRELOAD_PATH = "/tmp/libzve10plugin.so"
# 2.03 原厂 /usr/bin/im.elf 的哈希，用于确认真机跑的就是分析样本
IM_ELF_SHA256_FW203 = "3fd399b62b468c0302b370362378a3719df6b7d68ff9f6e1c09312a84beeabbf"


def resolve_cam_py(explicit_path: str | None = None) -> str:
    candidates = []
    if explicit_path:
        candidates.append(explicit_path)
    env_cam = os.environ.get("ZVP_CAM_PY")
    if env_cam:
        candidates.append(env_cam)
    candidates.extend([
        os.path.join(REPO_ROOT, "third_party", "camtool", "cam.py"),
        os.path.join(TOOLS_DIR, "camtool", "cam.py"),
        os.path.join(REPO_ROOT, "camtool", "camtool", "cam.py"),
        os.path.join(REPO_ROOT, "..", "camtool", "camtool", "cam.py"),
        os.path.join(REPO_ROOT, "..", "zve10", "camtool", "camtool", "cam.py"),
        os.path.join(REPO_ROOT, "..", "..", "camtool", "camtool", "cam.py"),
    ])
    for cand in candidates:
        p = os.path.abspath(cand)
        if os.path.isfile(p):
            return p
    return os.path.abspath(candidates[0])


CAM_PY = resolve_cam_py()


def resolve_pmca_root() -> str | None:
    candidates = []
    env_pmca = os.environ.get("PMCA_ROOT")
    if env_pmca:
        candidates.append(env_pmca)
    candidates.extend([
        os.path.join(REPO_ROOT, "third_party", "pmca"),
        os.path.join(REPO_ROOT, "..", "Sony-PMCA-RE"),
        os.path.join(os.path.dirname(CAM_PY), "vendor"),
    ])
    for cand in candidates:
        p = os.path.abspath(cand)
        if os.path.isdir(os.path.join(p, "pmca")):
            return p
    return None


def run_cam(args: list[str], capture: bool = True) -> subprocess.CompletedProcess:
    if not os.path.isfile(CAM_PY):
        raise FileNotFoundError(
            f"未找到 cam.py: {CAM_PY}。请运行 `git submodule update --init --recursive` 拉取 third_party/camtool，或通过 --cam-py / ZVP_CAM_PY 指定路径"
        )
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    pmca_root = resolve_pmca_root()
    if pmca_root and not env.get("PMCA_ROOT"):
        env["PMCA_ROOT"] = pmca_root
    cmd = ["uv", "run", "--with", "pyusb", "--with", "libusb-package", "--with", "pycryptodomex", "python", CAM_PY] + args
    return subprocess.run(
        cmd,
        env=env,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def read_firmware_bytes() -> subprocess.CompletedProcess:
    return run_cam(["sh", (
        "echo NETSERVICE_BYTES=$(dd if=/usr/lib/libNetService.so bs=1 skip=416440 count=4 2>/dev/null | od -tx1 -An); "
        "echo CAUTION_BYTES=$(dd if=/usr/lib/CautionConfig.so bs=1 skip=14121188 count=1 2>/dev/null | od -tx1 -An)"
    )])


def cmd_check() -> int:
    print("[1/3] 检查相机连接状态...")
    res = run_cam(["doctor"])
    print(res.stdout)
    if res.returncode != 0:
        print(res.stderr)
        print("[提示] 请确认 ZV-E10 已开机、USB 模式设为大容量存储并连线至电脑。")
        return res.returncode

    print("[2/3] 读取运行期进程映射与关键目录状态 (只读)...")
    probe_sh = (
        "PID=$(pidof im.elf); "
        "echo IM_PID=$PID; "
        "if [ -n \"$PID\" ]; then "
        "  grep -E 'libObj\\.so|libNetService\\.so|CautionConfig\\.so|libbackup\\.so|libInfraFsys\\.so' /proc/$PID/maps; "
        "fi; "
        "echo '--- mode files ---'; "
        "ls -la /setting/mode/ 2>/dev/null || true; "
        "echo dmode=$(cat /setting/mode/dmode 2>/dev/null || echo NONE); "
        "echo preload=$(cat /setting/mode/preload 2>/dev/null || echo NONE); "
        "df -h /tmp /setting /usr 2>/dev/null || true"
    )
    res2 = run_cam(["sh", probe_sh])
    print(res2.stdout)
    if res2.stderr:
        print(res2.stderr)
    if res2.returncode != 0:
        return res2.returncode
    print("[3/3] 校验磁盘上的固件基线字节...")
    baseline = read_firmware_bytes()
    print(baseline.stdout)
    if baseline.returncode != 0:
        print(baseline.stderr)
        return baseline.returncode
    for name, expected in (("NETSERVICE_BYTES", "90 b5 95 b0"), ("CAUTION_BYTES", "20")):
        match = re.search(rf"^{name}=(.*)$", baseline.stdout, re.MULTILINE)
        actual = " ".join(match.group(1).split()) if match else "MISSING"
        if actual != expected:
            print(f"[错误] {name}={actual}，期望 {expected}，停止部署")
            return 1
    return 0


def cmd_stage_tmp(so_path: str) -> int:
    if not os.path.exists(so_path):
        print(f"[错误] 找不到共享库文件: {so_path}")
        return 1
    print("[1/3] 解除旧目标文件链接（避免覆写已映射只读页）...")
    removed = run_cam(["sh", "rm -f /tmp/libzve10plugin.so /tmp/zve10plugin.log"])
    if removed.returncode != 0:
        print(removed.stdout)
        print(removed.stderr)
        return removed.returncode
    print(f"[2/3] 推送 {so_path} -> /tmp/libzve10plugin.so ...")
    res = run_cam(["put", so_path, "/tmp/libzve10plugin.so"])
    print(res.stdout)
    if res.returncode != 0:
        print(res.stderr)
        return res.returncode

    print("[3/3] 验证文件权限与大小...")
    res2 = run_cam(["sh", "chmod 755 /tmp/libzve10plugin.so && ls -lh /tmp/libzve10plugin.so"])
    print(res2.stdout)
    return res2.returncode


def cmd_logs() -> int:
    res = run_cam(["sh", "cat /tmp/zve10plugin.log 2>/dev/null || echo NO_LOG"])
    print(res.stdout)
    return res.returncode


def cmd_enable_preload() -> int:
    """写入 bootin.elf 能接受的 dmode/preload 组合。

    关键点（逆向结论）：
      * `p` 置位 bootin 的 preload 位，`3` 选择 app 阶段 —— 两者都要写进 dmode；
      * preload 文件内容必须以 4 条 /usr/tool/* 之一为前缀，否则 bootin 静默拒绝；
      * 因此首段放一个"前缀命中但保证不存在"的占位项，第二段才是插件路径。
        设备加载器会打印一则 `cannot be preloaded: ignored` 并继续，程序正常启动。
    """
    print("[1/4] 离线复现 bootin.elf 的 preload 门禁...")
    print(f"      基线 bootin.elf sha256 = {preload_gate.BOOTIN_ELF_SHA256_FW203}")
    try:
        content = preload_gate.build_preload_value(PLUGIN_PRELOAD_PATH)
    except ValueError as exc:
        print(f"[错误] 无法构造合规的 preload 内容: {exc}")
        return 1
    gate = preload_gate.simulate(content.encode("ascii"))
    print(f"      写入内容 = {content}")
    print(f"      门禁     = 通过（规则 #{gate.rule}: {gate.matched_prefix}）")
    print(f"      LD_PRELOAD => {gate.ld_preload}")

    print("[2/4] 写入 dmode=3p 与 preload（change_mode.sh p）...")
    command = (
        f'PRELOAD="{content}"; '
        'RC=0; change_mode.sh p "$PRELOAD" || RC=1; '
        'D=$(cat /setting/mode/dmode 2>/dev/null); '
        'P=$(cat /setting/mode/preload 2>/dev/null); '
        'echo "dmode=$D"; echo "preload=$P"; '
        '[ "$RC" = "0" ] || echo "[ERR] change_mode.sh failed"; '
        f'[ "$D" = "{preload_gate.DMODE_PRELOAD_VALUE}" ] || '
        f'echo "[ERR] dmode mismatch (expect {preload_gate.DMODE_PRELOAD_VALUE})"; '
        '[ "$P" = "$PRELOAD" ] || echo "[ERR] preload readback mismatch"; '
        f'[ "$RC" = "0" ] && [ "$D" = "{preload_gate.DMODE_PRELOAD_VALUE}" ] '
        '&& [ "$P" = "$PRELOAD" ] && echo "[3/4] readback OK"'
    )
    res = run_cam(["sh", command])
    print(res.stdout)
    if res.returncode != 0:
        if res.stderr:
            print(res.stderr)
        return res.returncode

    print("[4/4] 重启后的判读依据（用 --preload-status 只读核对）:")
    print(f"      ulogio 应出现 : LD_PRELOAD={content}")
    print(f"      加载器警告    : object '{preload_gate.GATE_DECOY}' from LD_PRELOAD cannot be preloaded: ignored.")
    print("      im.elf maps   : /tmp/libzve10plugin.so")
    return 0


def _hex_block(text: str, name: str) -> bytes | None:
    """解析 `xxd -p` 输出块；块内容被清洗为纯十六进制后再还原字节。"""
    match = re.search(rf"{re.escape(name)}_BEGIN\r?\n(.*?){re.escape(name)}_END",
                      text, re.DOTALL)
    if not match:
        return None
    payload = re.sub(r"[^0-9a-fA-F]", "", match.group(1))
    if not payload or len(payload) % 2:
        return None
    return bytes.fromhex(payload)


def cmd_preload_status() -> int:
    """只读采集：配置、白名单库、开机日志指纹、im.elf 映射与初始环境。

    只使用相机 busybox 确实具备的 applet（cat/grep/ls/pidof/xxd/tail），
    且一律用 `busybox <applet>` 调用——相机的 PATH 里没有这些符号链接，
    也没有 tr/od，因此二进制字段统一用 `xxd -p` 交给主机侧解码。
    """
    print("[只读] 采集 preload 相关证据（不写入任何文件）...")
    script = (
        "echo '=== system ==='; "
        "echo uptime=$(cat /proc/uptime 2>/dev/null || echo NONE); "
        "cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo NO_BOOT_ID; "
        "cat /proc/cmdline 2>/dev/null || echo NO_CMDLINE; "
        "grep -E ' /setting | /usr ' /proc/mounts 2>/dev/null || true; "
        "ls -l /lib/ld-linux.so.3 /lib/libc.so.6 /lib/libdl.so.2 2>/dev/null || true; "
        "echo '=== binaries ==='; "
        "busybox sha256sum /usr/bin/bootin.elf /usr/bin/im.elf 2>/dev/null || true; "
        "echo '=== init namespace view ==='; "
        "ls -l /proc/1/ns/mnt /proc/self/ns/mnt 2>/dev/null || true; "
        "ls -l /proc/1/root /proc/self/root 2>/dev/null || true; "
        "echo 'init dmode:' $(cat /proc/1/root/setting/mode/dmode 2>/dev/null || echo UNREADABLE); "
        "echo 'init preload:' $(cat /proc/1/root/setting/mode/preload 2>/dev/null || echo UNREADABLE); "
        "grep -E ' /setting ' /proc/1/mounts 2>/dev/null || true; "
        "echo '=== dmesg tail ==='; busybox dmesg 2>/dev/null | busybox tail -n 15 || true; "
        "echo '=== dmesg boot ==='; busybox dmesg 2>/dev/null | busybox grep -iE 'setting|mount|bootin|init' | busybox tail -n 25 || true; "
        "echo '=== setting files ==='; ls -la /setting 2>/dev/null || true; "
        "echo '--- env.txt ---'; cat /setting/env.txt 2>/dev/null || echo NO_ENV_TXT; "
        "echo '=== ssboot ==='; ls -la /proc/ssboot 2>/dev/null || echo NO_SSBOOT_PROC; "
        "for f in mode operation resmode state section; do echo \"ssboot.$f=$(busybox timeout 1 cat /proc/ssboot/$f 2>/dev/null || echo TIMEOUT_OR_EMPTY)\"; done; "
        "ls -la /proc/ssboot/optimizer /proc/ssboot/region 2>/dev/null || true; "
        "ls -la /system/sabin/ssboot*.bin 2>/dev/null || true; "
        "echo '=== mode files ==='; "
        "ls -la /setting/mode/ 2>/dev/null || true; "
        "echo dmode=$(cat /setting/mode/dmode 2>/dev/null || echo NONE); "
        "echo preload=$(cat /setting/mode/preload 2>/dev/null || echo NONE); "
        "echo -n 'dmode hex: '; busybox xxd -p /setting/mode/dmode 2>/dev/null || true; "
        "echo -n 'preload hex: '; busybox xxd -p /setting/mode/preload 2>/dev/null || true; "
        "echo '=== /usr/tool ==='; "
        "ls -la /usr/tool 2>/dev/null || true; "
        "echo '=== boot log (ulogio) ==='; "
        "ls -l /setting/ulogio.bin 2>/dev/null || echo NO_ULOGIO_BIN; "
        "grep -aE 'LD_PRELOAD|libzve10plugin|mcheckb' /setting/ulogio.bin 2>/dev/null "
        "| busybox tail -n 20 || true; "
        "echo ULOGIO_HEX_BEGIN; busybox xxd -p -l 16384 /setting/ulogio.bin 2>/dev/null || echo NO_XXD; "
        "echo ULOGIO_HEX_END; "
        "echo '=== plugin file ==='; "
        "ls -l /tmp/libzve10plugin.so 2>/dev/null || echo NO_PLUGIN_FILE; "
        "echo '=== im.elf ==='; "
        "PID=$(pidof im.elf); echo IM_PID=$PID; "
        'if [ -n "$PID" ]; then '
        "  echo '-- LD_PRELOAD in initial environment --'; "
        "  echo ENVIRON_HEX_BEGIN; busybox xxd -p /proc/$PID/environ 2>/dev/null || echo NO_XXD; "
        "  echo ENVIRON_HEX_END; "
        "  echo '-- plugin mappings (count) --'; "
        "  grep -c libzve10plugin /proc/$PID/maps 2>/dev/null || true; "
        "  echo '-- proc stat --'; "
        "  cat /proc/$PID/stat 2>/dev/null || true; "
        "  echo '-- cmdline --'; "
        "  echo CMDLINE_BEGIN; busybox xxd -p /proc/$PID/cmdline 2>/dev/null || true; echo CMDLINE_END; "
        "fi; "
        "echo '=== plugin log tail ==='; "
        "busybox tail -n 20 /tmp/zve10plugin.log 2>/dev/null || echo NO_PLUGIN_LOG; "
        "echo '=== boot guard ==='; "
        "if [ -f /tmp/zvp_boot_guard ]; then "
        "  echo GUARD_HEX_BEGIN; busybox xxd -p /tmp/zvp_boot_guard 2>/dev/null || echo NO_XXD; "
        "  echo GUARD_HEX_END; "
        "else echo NO_BOOT_GUARD_STATE; fi"
    )
    res = run_cam(["sh", script])
    visible = re.sub(r"ENVIRON_HEX_BEGIN\r?\n.*?ENVIRON_HEX_END\r?\n?", "",
                     res.stdout, flags=re.DOTALL)
    visible = re.sub(r"GUARD_HEX_BEGIN\r?\n.*?GUARD_HEX_END\r?\n?", "",
                     visible, flags=re.DOTALL)
    visible = re.sub(r"ULOGIO_HEX_BEGIN\r?\n.*?ULOGIO_HEX_END\r?\n?", "",
                     visible, flags=re.DOTALL)
    visible = re.sub(r"CMDLINE_BEGIN\r?\n.*?CMDLINE_END\r?\n?", "",
                     visible, flags=re.DOTALL)
    print(visible.rstrip())
    if res.stderr:
        print(res.stderr)

    ulogio = _hex_block(res.stdout, "ULOGIO_HEX")
    if ulogio:
        strings = [s.decode("ascii") for s in re.findall(rb"[ -~]{4,}", ulogio)]
        keywords = ("LD_PRELOAD", "libzve10plugin", "mcheckb", "bootin", "preload", "im.elf")
        hits = [s for s in strings if any(k.lower() in s.lower() for k in keywords)]
        if hits:
            print("[ulogio] 相关字符串:")
            for line in hits[-20:]:
                print(f"  {line}")
        else:
            print(f"[ulogio] 未发现 preload 相关字符串（共 {len(strings)} 个可读串，{len(ulogio)} 字节）")
            for line in strings[:15]:
                print(f"  | {line}")
    elif "NO_XXD" in res.stdout:
        print("[ulogio] 相机 busybox 缺少 xxd，无法扫描原始日志")

    environ = _hex_block(res.stdout, "ENVIRON_HEX")
    if environ is not None:
        entries = [item.decode("ascii", "replace") for item in environ.split(b"\0") if item]
        entry = next((item for item in entries if item.startswith("LD_PRELOAD=")), None)
        if entry:
            print(f"[环境] {entry}")
        else:
            shown = ", ".join(entries[:20]) if entries else "(空)"
            print(f"[环境] exec 时无 LD_PRELOAD（共 {len(entries)} 项: {shown}）")
    elif "NO_XXD" in res.stdout:
        print("[环境] 相机 busybox 缺少 xxd，无法解析 environ")

    hashes = dict(re.findall(r"^([0-9a-f]{64})\s+(\S+)$", res.stdout, re.MULTILINE))
    for path, expected in (("/usr/bin/bootin.elf", preload_gate.BOOTIN_ELF_SHA256_FW203),
                           ("/usr/bin/im.elf", IM_ELF_SHA256_FW203)):
        actual = hashes.get(path)
        if actual:
            verdict = "MATCH" if actual == expected else "MISMATCH"
            print(f"[校验] {path}: {verdict} ({actual[:16]}…)")

    guard = _hex_block(res.stdout, "GUARD_HEX")
    if guard is not None and len(guard) >= 16:
        magic, version, fails, trips = struct.unpack("<IIII", guard[:16])
        print(f"[看门狗] magic=0x{magic:08x} version={version} "
              f"fail_count={fails} trip_count={trips}")
        if magic != 0x5A564247:
            print("  [注意] magic 不匹配，状态文件不是本插件写的")
        elif fails >= 1:
            print("  [注意] fail_count >= 1：上次插件会话未正常收尾；"
                  "累计到 2 会自熔断并清空 preload。")
            print("         需要干净验证时，先执行: --reset-boot-guard")
    elif "NO_BOOT_GUARD_STATE" in res.stdout:
        print("[看门狗] 尚无状态文件（首次加载创建，fail_count 从 0 开始）")

    uptime_match = re.search(r"^uptime=([0-9.]+)", res.stdout, re.MULTILINE)
    stat_match = re.search(r"^\d+ \(.*\) (.*)$", res.stdout, re.MULTILINE)
    if uptime_match and stat_match:
        fields = stat_match.group(1).split()
        if len(fields) > 19:
            try:
                start_seconds = int(fields[19]) / 100.0
                print(f"[进程] im.elf 启动于开机后 {start_seconds:.1f}s（当前 uptime={uptime_match.group(1)}s）")
            except ValueError:
                pass

    cmdline_hex = _hex_block(res.stdout, "CMDLINE")
    if cmdline_hex:
        parts = [item.decode("ascii", "replace") for item in cmdline_hex.split(b"\0") if item]
        print(f"[进程] im.elf argv = {parts}")
    return res.returncode


def cmd_reset_boot_guard() -> int:
    """删除插件自己的运行期看门狗状态（/tmp，非原厂配置）。

    该文件记录 fail_count/trip_count；累计 >=2 会让插件在下次加载时
    自行写回 dmode=3 并清空 preload，干扰真机验证。
    """
    print("[写入] 重置插件运行期看门狗状态 /tmp/zvp_boot_guard ...")
    command = (
        "rm -f /tmp/zvp_boot_guard; "
        "if [ -f /tmp/zvp_boot_guard ]; then echo STILL_EXISTS; false; "
        "else echo GUARD_RESET_OK; fi"
    )
    res = run_cam(["sh", command])
    print(res.stdout)
    if res.stderr:
        print(res.stderr)
    return res.returncode


def cmd_disable_preload() -> int:
    print("恢复原厂默认启动模式 (change_mode.sh 3)...")
    res = run_cam(["sh", "change_mode.sh 3"])
    print(res.stdout)
    return res.returncode


def cmd_reboot(wait_sec: int = 30) -> int:
    print("[1/2] 重启相机 (cam.py reboot reboot --confirm)...")
    res = run_cam(["reboot", "reboot", "--confirm"])
    print(res.stdout)
    if res.returncode != 0:
        if res.stderr:
            print(res.stderr)
        return res.returncode
    print(f"[2/2] 等待相机重启重新连线 (最多 {wait_sec}s)...")
    res2 = run_cam(["wait", str(wait_sec)])
    print(res2.stdout)
    if res2.stderr:
        print(res2.stderr)
    return res2.returncode


def cmd_set_mode(mode: str, value: str | None = None) -> int:
    """诊断用：直接调用原厂 change_mode.sh（只写 /setting/mode，不碰固件）。"""
    if not re.fullmatch(r"[0-9a-fp]{1,2}", mode):
        print(f"[错误] 非法模式参数: {mode!r}")
        return 2
    if mode == "p" and not value:
        print("[错误] 模式 p 需要 --preload-value")
        return 2
    if mode == "p":
        command = (
            f'change_mode.sh p "{value}"; D=$(cat /setting/mode/dmode 2>/dev/null); '
            'P=$(cat /setting/mode/preload 2>/dev/null); echo "dmode=$D"; echo "preload=$P"'
        )
    else:
        command = (
            f'change_mode.sh {mode}; D=$(cat /setting/mode/dmode 2>/dev/null); '
            'P=$(cat /setting/mode/preload 2>/dev/null); echo "dmode=$D"; echo "preload=$P"'
        )
    print(f"[写入] change_mode.sh {mode} ...")
    res = run_cam(["sh", command])
    print(res.stdout.rstrip())
    if res.stderr:
        print(res.stderr)
    if res.returncode == 0 and "Please reboot" in res.stdout:
        print("[提示] 配置已写入，需要重启后生效。")
    return res.returncode


def cmd_write_dmode(dmode: str, preload: str | None = None) -> int:
    """诊断用：直接写 /setting/mode/dmode（可选 preload），仅用于真机差分实验。"""
    if not re.fullmatch(r"[0-9A-Za-z]{1,10}", dmode or ""):
        print(f"[错误] 非法 dmode: {dmode!r}")
        return 2
    if preload is not None and not re.fullmatch(r"[0-9A-Za-z._:/+-]{0,400}", preload):
        print(f"[错误] 非法 preload: {preload!r}")
        return 2
    if preload is None:
        command = (
            f'echo {dmode} > /setting/mode/dmode; '
            'D=$(cat /setting/mode/dmode 2>/dev/null); P=$(cat /setting/mode/preload 2>/dev/null); '
            'echo "dmode=$D"; echo "preload=$P"'
        )
    else:
        command = (
            f'echo {dmode} > /setting/mode/dmode; '
            f'echo -n "{preload}" > /setting/mode/preload; '
            'D=$(cat /setting/mode/dmode 2>/dev/null); P=$(cat /setting/mode/preload 2>/dev/null); '
            'echo "dmode=$D"; echo "preload=$P"'
        )
    print(f"[写入] dmode={dmode} preload={preload!r} ...")
    res = run_cam(["sh", command])
    print(res.stdout.rstrip())
    if res.stderr:
        print(res.stderr)
    return res.returncode


def cmd_write_env_txt(value: str) -> int:
    """诊断用：写 /setting/env.txt（init 启动时逐行 putenv 的文件）。"""
    if not re.fullmatch(r"[0-9A-Za-z_./:=+-]{1,200}", value or ""):
        print(f"[错误] 非法 env 行: {value!r}")
        return 2
    print(f"[写入] /setting/env.txt <- {value}")
    command = (
        f'echo "{value}" > /setting/env.txt; '
        'echo "env.txt=$(cat /setting/env.txt 2>/dev/null)"; '
        'ls -la /setting/env.txt 2>/dev/null || true'
    )
    res = run_cam(["sh", command])
    print(res.stdout.rstrip())
    if res.stderr:
        print(res.stderr)
    return res.returncode


def cmd_rm_env_txt() -> int:
    """诊断收尾：删除哨兵测试写入的 /setting/env.txt。"""
    print("[删除] /setting/env.txt")
    res = run_cam(["sh", "rm -f /setting/env.txt; ls -la /setting/env.txt 2>/dev/null || echo ENV_TXT_GONE"])
    print(res.stdout.rstrip())
    if res.stderr:
        print(res.stderr)
    return res.returncode


def cmd_kallsyms() -> int:
    """只读：导出内核 ssboot/upm 符号地址，用于静态映射。"""
    script = (
        "echo '=== kallsyms head ==='; sed -n '1,3p' /proc/kallsyms; "
        "echo '=== ssboot/upm symbols ==='; "
        "grep -E ' (ssboot|upm)' /proc/kallsyms | sed -n '1,120p'; "
    )
    res = run_cam(["sh", script])
    print(res.stdout.rstrip())
    if res.stderr:
        print(res.stderr)
    return res.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Sony ZV-E10 运行期探针与部署助手")
    parser.add_argument("--check", action="store_true", help="检查相机连接与 im.elf 映射，Jiritsu 会临时上传加载器")
    parser.add_argument("--stage-tmp", action="store_true", help="上传 libzve10plugin.so 到相机 /tmp")
    parser.add_argument("--logs", action="store_true", help="读取 /tmp/zve10plugin.log")
    parser.add_argument("--preload-status", action="store_true",
                        help="只读核对 preload 配置、开机日志指纹与 im.elf 映射")
    parser.add_argument("--clean-loader", action="store_true",
                        help="检查并删除上次异常会话残留的 /tmp/jiritsu loader 文件（Jiritsu）")
    parser.add_argument("--reset-boot-guard", action="store_true",
                        help="删除 /tmp/zvp_boot_guard（插件运行期看门狗状态，非原厂配置）")
    parser.add_argument("--enable-preload", action="store_true", help="设置 /setting/mode/preload (重启后生效)")
    parser.add_argument("--disable-preload", action="store_true", help="恢复默认 mode3 无注入")
    parser.add_argument("--set-mode", metavar="MODE", default=None,
                        help="诊断：直接调用 change_mode.sh <MODE>（如 10=mcheckb, 7=LeakTracer）")
    parser.add_argument("--write-dmode", metavar="TEXT", default=None,
                        help="诊断：直接写 /setting/mode/dmode（如 3pd，配合 --write-preload）")
    parser.add_argument("--write-preload", metavar="TEXT", default=None, help="配合 --write-dmode 写 preload")
    parser.add_argument("--write-env-txt", metavar="TEXT", default=None,
                        help="诊断：写 /setting/env.txt（init 启动时 putenv 的哨兵测试）")
    parser.add_argument("--rm-env-txt", action="store_true", help="删除哨兵测试写入的 /setting/env.txt")
    parser.add_argument("--kallsyms", action="store_true", help="只读：导出内核 ssboot/upm 符号地址")
    parser.add_argument("--preload-value", default=None, help="配合 --set-mode p 使用")
    parser.add_argument("--reboot", action="store_true", help="重启相机并等待 30 秒重新连线")
    parser.add_argument("--backend", choices=("camtool", "jiritsu"), default="camtool",
                        help="2.03 使用 jiritsu，默认保留 camtool 路径")
    parser.add_argument("--deploy", action="store_true", help="Jiritsu 单会话检查、上传并启用插件")
    parser.add_argument("--jiritsu-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--wbi-status", action="store_true",
                        help="Jiritsu 只读：WBI 头 + ssboot/UPM 运行态（不改任何东西）")
    parser.add_argument("--wbi-prepare", action="store_true",
                        help="Jiritsu：整分区双读并比对，生成写入前的备份与清单")
    parser.add_argument("--wbi-drop", action="store_true",
                        help="Jiritsu：用原厂 ud_wbi_drop.elf 清零 WBI 头 512 B（需 --i-authorize-emmc-write）")
    parser.add_argument("--wbi-verify", action="store_true",
                        help="Jiritsu 只读：冷启动验收（无 WARMBOOT / im.elf 映射 / 插件日志）")
    parser.add_argument("--wbi-dir", default=None, help="WBI 备份与清单目录（prepare/drop 必填）")
    parser.add_argument("--wbi-tool", default=str(Path(__file__).with_name("payload") / "ud_wbi_drop.elf"),
                        help="原厂 ud_wbi_drop.elf 路径（默认 tools/payload）")
    parser.add_argument("--baseline", default=None,
                        help="已知可信的整分区备份；提供时 prepare 只做一次全读并与它逐字节比对")
    parser.add_argument("--i-authorize-emmc-write", action="store_true",
                        help="机主对 eMMC 写入的显式授权；--wbi-drop 缺少它时拒绝执行")
    parser.add_argument("--so", default=DEFAULT_SO, help="libzve10plugin.so 路径")
    parser.add_argument("--cam-py", default=None, help="指定 camtool cam.py 路径（也可设 ZVP_CAM_PY）")
    args = parser.parse_args()

    args.wbi_action = None
    for flag, name in (("wbi_status", "status"), ("wbi_prepare", "prepare"),
                       ("wbi_drop", "drop"), ("wbi_verify", "verify")):
        if getattr(args, flag):
            if args.wbi_action:
                parser.error("每次只执行一个 WBI 动作")
            args.wbi_action = name
    if args.wbi_action:
        if args.backend != "jiritsu":
            parser.error("WBI 动作只支持 --backend jiritsu（2.03）")
        if args.wbi_action in ("prepare", "drop") and not args.wbi_dir:
            parser.error("--wbi-prepare/--wbi-drop 需要 --wbi-dir <备份目录>")
        if args.wbi_action == "drop" and not args.i_authorize_emmc_write:
            parser.error("--wbi-drop 会写 eMMC。确认后加 --i-authorize-emmc-write 再执行")
        tool = Path(args.wbi_tool)
        if args.wbi_action == "drop" and not tool.is_file():
            parser.error(f"找不到原厂工具 {tool}；先用 extract 步骤放入 tools/payload/")

    if args.backend == "jiritsu":
        if args.cam_py:
            parser.error("--cam-py 仅适用于 camtool 后端")
        if not args.jiritsu_worker:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            command = ["uv", "run", "--with", "pyusb", "--with", "libusb-package",
                       "--with", "pycryptodomex", "--with", "pyelftools", "python", os.path.abspath(__file__),
                       *sys.argv[1:], "--jiritsu-worker"]
            return subprocess.run(command, env=env).returncode
        from zve10_jiritsu_runtime import run
        return run(args)
    if args.deploy:
        parser.error("--deploy 需要 --backend jiritsu")
    if args.clean_loader:
        parser.error("--clean-loader 需要 --backend jiritsu")

    global CAM_PY
    if args.cam_py:
        CAM_PY = resolve_cam_py(args.cam_py)

    if args.check:
        return cmd_check()
    if args.stage_tmp:
        return cmd_stage_tmp(args.so)
    if args.logs:
        return cmd_logs()
    if args.preload_status:
        return cmd_preload_status()
    if args.reset_boot_guard:
        return cmd_reset_boot_guard()
    if args.enable_preload:
        return cmd_enable_preload()
    if args.disable_preload:
        return cmd_disable_preload()
    if args.set_mode is not None:
        return cmd_set_mode(args.set_mode, args.preload_value)
    if args.write_dmode is not None:
        return cmd_write_dmode(args.write_dmode, args.write_preload)
    if args.write_env_txt is not None:
        return cmd_write_env_txt(args.write_env_txt)
    if args.rm_env_txt:
        return cmd_rm_env_txt()
    if args.kallsyms:
        return cmd_kallsyms()
    if args.reboot:
        return cmd_reboot(30)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
