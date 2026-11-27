"""Firmware 2.03 deployment using the pinned zv-e10-toolkit backend."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import probe_zve10_runtime as probe


def find_device(pid):
    """Allow the two camera identities to use different Windows drivers."""
    import usb.backend.libusb0
    import usb.backend.libusb1
    import usb.core
    import usb.util
    import libusb_package

    backends = [usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)]
    if os.name == "nt":
        library = os.environ.get("LIBUSB0_PATH") or str(Path(os.environ["WINDIR"]) / "System32" / "libusb0.dll")
        if Path(library).is_file():
            backends.append(usb.backend.libusb0.get_backend(find_library=lambda name: library))
    last_error = None
    for backend in backends:
        if backend is None:
            continue
        device = usb.core.find(idVendor=0x054c, idProduct=pid, backend=backend)
        if device is None:
            continue
        try:
            device.ctrl_transfer(0x80, 0x06, 0x0100, 0, 18, timeout=2000)
        except NotImplementedError as error:
            last_error = error
            usb.util.dispose_resources(device)
            continue
        return device
    if last_error is not None:
        raise RuntimeError(f"USB {pid:04X} requires an accessible libusbK, WinUSB or libusb-win32 driver") from last_error
    return None


class Runtime:
    def __init__(self, backend, upload):
        self.backend = backend
        self.upload = upload

    def shell(self, command):
        result = self.backend.command("zve10-plugin", command)
        return subprocess.CompletedProcess(command, result["exit_code"], result["output"], "")

    def read_firmware_bytes(self):
        target = self.backend.ram + "/baseline"
        lines = []
        try:
            for name, path, offset, count in (
                ("NETSERVICE_BYTES", "/usr/lib/libNetService.so", 416440, 4),
                ("CAUTION_BYTES", "/usr/lib/CautionConfig.so", 14121188, 1),
            ):
                self.backend.run(f"dd if='{path}' of='{target}' bs=1 skip={offset} count={count} 2>/dev/null")
                _, data = self.backend.read_path(target)
                if len(data) != count:
                    raise RuntimeError(f"Short firmware baseline read: {name}")
                lines.append(f"{name}={data.hex(' ')}")
        finally:
            self.backend.run(f"rm -f '{target}'")
        return subprocess.CompletedProcess(["baseline"], 0, "\n".join(lines) + "\n", "")

    def put(self, source, destination):
        if destination != "/tmp/libzve10plugin.so":
            raise ValueError("Unexpected plugin destination")
        blob = Path(source).read_bytes()
        if not blob or len(blob) > 8 * 1024 * 1024:
            raise ValueError("Invalid plugin size")
        temporary = destination + "." + uuid.uuid4().hex
        chunk = self.backend.ram + "/upload-chunk"
        self.backend.run(f": > '{temporary}'")
        try:
            for offset in range(0, len(blob), 65536):
                self.upload(self.backend.camera, chunk, blob[offset:offset + 65536])
                self.backend.run(f"cat '{chunk}' >> '{temporary}'")
            digest = self.backend.run(f"busybox sha256sum '{temporary}'").split()[0]
            if digest != hashlib.sha256(blob).hexdigest():
                raise RuntimeError("Plugin camera/host SHA-256 mismatch")
            self.backend.run(f"chmod 755 '{temporary}' && mv '{temporary}' '{destination}'")
        finally:
            self.backend.run(f"rm -f '{chunk}' '{temporary}'")
        return subprocess.CompletedProcess(["put", source, destination], 0,
                                           f"SHA-256 verified: {digest}\n", "")

    def run_cam(self, arguments, capture=True):
        if arguments == ["doctor"]:
            return subprocess.CompletedProcess(arguments, 0,
                                               "Jiritsu 2.03 双次固件校验、UID=0 与持久会话已就绪\n", "")
        if arguments[0] == "sh":
            return self.shell(arguments[1])
        if arguments[0] == "put":
            return self.put(arguments[1], arguments[2])
        raise ValueError(f"Unsupported Jiritsu operation: {arguments[0]}")

    def reboot(self):
        # close() removes the loader files while leaving the RAM shell alive.
        # Reuse that shell for the final reboot request after cleanup succeeds.
        # 显式用 busybox：相机 /sbin 没有 reboot 独立文件，PATH 里也不保证有符号链接，
        # 裸 `reboot` 会静默失败（命令被后台化，退出码仍是 0）。
        self.backend.close()
        self.backend.started = True
        try:
            self.backend.run("(sleep 10; sync; busybox reboot) </dev/null >/dev/null 2>&1 &")
        finally:
            self.backend.started = False


def verify_plugin(path):
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("verify_elf_abi.py")), path])
    if result.returncode:
        raise RuntimeError("Plugin ABI verification failed")


def read_plugin_log(camera, read_file):
    try:
        data = read_file(camera, "/tmp/zve10plugin.log", 2 * 1024 * 1024)
    except RuntimeError as error:
        if "0x10000006" not in str(error):
            raise
        print("NO_LOG：插件启动日志不存在，初始化尚未确认。")
        return 1
    text = data.decode("utf-8", "replace")
    print(text)
    if not data.strip():
        print("插件日志为空，初始化尚未确认。")
        return 1
    if "画布初始化完成" not in text or not re.search(r"UI主线程激活成功:[^\r\n]*\bcanvas=1\b", text):
        print("尚未找到画布初始化和 canvas=1 的 UI 激活记录。")
        return 1
    return 0


def clean_loader_workspace(camera, read_file, jcmd):
    """检查并删除上次异常会话残留的 jiritsu loader 文件（不碰相机其它数据）。"""
    import hashlib

    paths = ("/tmp/jiritsu/run.sh", "/tmp/jiritsu/libJit.so", "/tmp/jiritsu/out.txt")
    present = []
    print("[检查] 现有 loader 工作区文件：")
    for path in paths:
        try:
            data = read_file(camera, path, 65536)
        except RuntimeError as exc:
            if "0x10000006" in str(exc):
                print(f"  {path}: 不存在")
                continue
            raise
        present.append(path)
        print(f"  {path}: {len(data)} 字节 sha256={hashlib.sha256(data).hexdigest()}")
        if path.endswith("run.sh"):
            preview = data[:120].decode("ascii", "replace").replace("\n", " | ")
            print(f"    run.sh 预览: {preview}")

    if not present:
        print("[清理] 无需清理。")
        return 0

    print("[清理] 使用 SET_FILE_PATH(0x0F) + FILE_DELETE2(0x12) 删除：")
    failed = 0
    for path in paths:
        try:
            jcmd(camera, 15, b"\0" + path.encode("ascii") + b"\0")
            jcmd(camera, 18, b"\0\xff")
        except Exception as exc:  # noqa: BLE001
            print(f"  {path}: 删除命令失败: {exc}")
            failed += 1
            continue
        try:
            read_file(camera, path, 65536)
        except RuntimeError as exc:
            if "0x10000006" in str(exc):
                print(f"  {path}: 已删除")
            else:
                print(f"  {path}: 回读错误: {exc}")
                failed += 1
        else:
            print(f"  {path}: 仍存在（删除未生效）")
            failed += 1
    return 1 if failed else 0


def auto_clean_known_loader(camera, read_file, jcmd):
    """会话启动前，若残留文件确证为本工具的 loader，则自动清理。"""
    import hashlib
    from zve10_jiritsu import PAYLOAD_SHA

    paths = ("/tmp/jiritsu/run.sh", "/tmp/jiritsu/libJit.so", "/tmp/jiritsu/out.txt")
    found = {}
    for path in paths:
        try:
            found[path] = read_file(camera, path, 65536)
        except RuntimeError as exc:
            if "0x10000006" in str(exc):
                continue
            raise
    if not found:
        return True
    lib = found.get("/tmp/jiritsu/libJit.so")
    run = found.get("/tmp/jiritsu/run.sh")
    ours = (lib is not None and hashlib.sha256(lib).hexdigest() == PAYLOAD_SHA
            and run is not None and b"/tmp_network/zve10-" in run)
    if not ours:
        print("[警告] 发现无法确认来源的 loader 文件，拒绝自动清理；请用 --clean-loader 人工检查。")
        return False
    print("[自动清理] 上次异常会话残留的 loader 文件：")
    for path in paths:
        if path in found:
            print(f"  {path}: {len(found[path])} 字节")
    for path in found:
        jcmd(camera, 15, b"\0" + path.encode("ascii") + b"\0")
        jcmd(camera, 18, b"\0\xff")
        try:
            read_file(camera, path, 65536)
        except RuntimeError as exc:
            if "0x10000006" in str(exc):
                print(f"  {path}: 已删除")
                continue
            print(f"  {path}: 回读错误: {exc}")
            return False
        print(f"  {path}: 仍存在（删除未生效）")
        return False
    return True


def run_wbi(args):
    """Minimal Jiritsu bootstrap for the WBI actions (no plugin staging checks)."""
    import wbi_coldboot

    root = Path(probe.REPO_ROOT)
    toolkit = root / "third_party" / "zve10-toolkit" / "linux"
    if not (toolkit / "host" / "zve10_jiritsu.py").is_file():
        print("请先运行 git submodule update --init third_party/zve10-toolkit")
        return 1
    os.environ.setdefault("PMCA_ROOT", probe.resolve_pmca_root() or "")
    sys.path.insert(0, str(root / "third_party" / "camtool"))
    sys.path.insert(0, str(toolkit / "host"))
    import jiritsu_lib
    jiritsu_lib.find = find_device
    from pmca.usb.sony import SonySenserCamera
    from zve10_senser import LengthDelimitedSenserDevice, choice
    from zve10_read_file_jiritsu import read_file
    from zve10_jiritsu import FIRMWARES, JiritsuBackend, jcmd

    connection = None
    backend = None
    session = Path(tempfile.mkdtemp(prefix="zvp-wbi-"))
    status = {"session": str(session), "state": "connecting", "action": args.wbi_action}
    print(f"Jiritsu 会话记录 {session}", flush=True)
    result_code = 1
    exit_ok = False
    try:
        device = jiritsu_lib.find(jiritsu_lib.MSC_PID)
        if device is not None and device.product != "ZV-E10":
            raise RuntimeError(f"Unsupported camera: {device.product}")
        connection = jiritsu_lib.connect(timeout=30, verbose=True)
        if connection is None:
            raise RuntimeError("Camera service device did not appear")
        if choice() == "length":
            connection.ch = LengthDelimitedSenserDevice(connection.auth.driver)
        camera = SonySenserCamera(connection.ch)
        camera.readHasp()
        versions = [read_file(camera, "/version.txt", 64) for _ in range(2)]
        if versions != [FIRMWARES["2.03"], FIRMWARES["2.03"]]:
            raise RuntimeError(f"Unexpected firmware versions: {versions!r}")
        status["firmware"] = versions[0].decode("ascii")
        print(f"固件双次读取通过 {status['firmware']}", flush=True)
        if not auto_clean_known_loader(camera, read_file, jcmd):
            print("[错误] loader 工作区未清理，停止。")
            result_code = 1
        else:
            backend = JiritsuBackend(camera, session, "2.03")
            backend.start()
            result_code = wbi_coldboot.ACTIONS[args.wbi_action](backend, args)
            status["result"] = result_code
    except Exception as error:
        status.update(state="failed", error=f"{type(error).__name__}: {error}")
        print(f"[错误] {status['error']}", file=sys.stderr, flush=True)
        print("证据与部分文件已保留；不要盲目重试。若已执行 --wbi-drop，先看回读清单再决定。", flush=True)
        result_code = 1
    finally:
        try:
            try:
                if backend is not None:
                    backend.close()
            finally:
                if connection is not None:
                    connection.auth.stop()
                    status["service_returned"] = True
                    status["usb_service_stopped"] = True
                    status["usb_pids_after_stop"] = usb_pids_snapshot()
        except Exception as error:
            status.update(state="cleanup-failed",
                          cleanup_error=f"{type(error).__name__}: {error}")
            raise
        finally:
            if backend is not None:
                status.update(backend.status)
            if status["state"] not in ("failed", "cleanup-failed"):
                status["state"] = "closed"
            (session / "state.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
            exit_ok = print_session_exit(status)
            if args.wbi_action == "drop" and status["state"] == "closed" and exit_ok:
                print("现在可以：拔 USB → 关机 → 取电池约 30 秒 → 装回开机（这一次才是真冷启动）。", flush=True)
    if not exit_ok:
        print("[错误] 会话退出验证失败；本次结果不可视为成功。", file=sys.stderr, flush=True)
        return 1
    return result_code


def usb_pids_snapshot(attempts=4, delay=0.5):
    """Best-effort USB enumeration after the service stop (never opens a session)."""
    import time as _time
    last = None
    for _ in range(attempts):
        try:
            import jiritsu_lib
            last = [f"0x{pid:04x}" for pid in jiritsu_lib.present()]
        except Exception as error:
            return [f"error: {error!r}"]
        if "0x0336" not in last:
            break
        _time.sleep(delay)
    return last


def print_session_exit(status):
    """Explicit exit verification: same session, no reconnection.

    Mirrors the reference JiritsuBackend.close() guarantees:
    loader files are removed *and* GET_FILE confirms absence before the
    service is stopped; the service stop itself is recorded separately.
    """
    print("=== 会话退出验证（同一会话内完成，不重开会话） ===", flush=True)
    print(f"  loader_upload_attempted = {status.get('loader_upload_attempted')}", flush=True)
    print(f"  loader_files_removed    = {status.get('loader_files_removed')}", flush=True)
    print(f"  service_returned        = {status.get('service_returned')}", flush=True)
    print(f"  usb_service_stopped     = {status.get('usb_service_stopped')}", flush=True)
    pids = status.get("usb_pids_after_stop")
    if pids is not None:
        print(f"  usb_pids_after_stop     = {pids}", flush=True)
        if any(isinstance(item, str) and item.startswith("error:") for item in pids):
            print("  usb_service_gone        = unknown（枚举失败）", flush=True)
        else:
            print(f"  usb_service_gone        = {'0x0336' not in pids}", flush=True)
    print(f"  session_state           = {status.get('state')}", flush=True)
    if status.get("cleanup_error"):
        print(f"  cleanup_error           = {status['cleanup_error']}", flush=True)
    # Exit verification describes the *session exit* only. Task success/failure
    # is reported separately by the caller's return code (state=failed with a
    # clean close must not be misreported as a cleanup failure).
    ok = (status.get("service_returned") is True
          and status.get("usb_service_stopped") is True
          and (status.get("loader_files_removed") is True
               or status.get("loader_upload_attempted") is not True))
    print(f"  exit_verification       = {'OK' if ok else 'FAILED'}", flush=True)
    return ok


def run(args):
    if getattr(args, "wbi_action", None):
        return run_wbi(args)
    if args.reboot and not (args.deploy or args.enable_preload or args.disable_preload
                            or args.set_mode is not None or args.write_dmode is not None
                            or args.write_env_txt is not None):
        print("Jiritsu 软件重启请与 --deploy、--enable-preload、--disable-preload、--set-mode、--write-dmode 或 --write-env-txt 在同一命令中使用。")
        return 1
    actions = [args.check, args.stage_tmp, args.enable_preload, args.disable_preload,
               args.logs, args.deploy, args.preload_status, args.reset_boot_guard,
               args.set_mode is not None, args.write_dmode is not None,
               args.write_env_txt is not None, args.rm_env_txt, args.kallsyms,
               args.clean_loader]
    if sum(actions) != 1:
        print("Jiritsu 每次请选择一个操作，完整部署使用 --deploy。")
        return 1
    if args.stage_tmp or args.deploy:
        verify_plugin(args.so)

    root = Path(probe.REPO_ROOT)
    toolkit = root / "third_party" / "zve10-toolkit" / "linux"
    if not (toolkit / "host" / "zve10_jiritsu.py").is_file():
        print("请先运行 git submodule update --init third_party/zve10-toolkit")
        return 1
    os.environ.setdefault("PMCA_ROOT", probe.resolve_pmca_root() or "")
    sys.path.insert(0, str(root / "third_party" / "camtool"))
    sys.path.insert(0, str(toolkit / "host"))
    import jiritsu_lib
    jiritsu_lib.find = find_device
    from pmca.usb.sony import SonySenserCamera
    from zve10_senser import LengthDelimitedSenserDevice, choice
    from zve10_read_file_jiritsu import read_file
    from zve10_jiritsu import FIRMWARES, JiritsuBackend, upload_verified, jcmd

    connection = None
    backend = None
    session = Path(tempfile.mkdtemp(prefix="zvp-jiritsu-"))
    status = {"session": str(session), "state": "connecting"}
    print(f"Jiritsu 会话记录 {session}", flush=True)
    try:
        device = jiritsu_lib.find(jiritsu_lib.MSC_PID)
        if device is not None and device.product != "ZV-E10":
            raise RuntimeError(f"Unsupported camera: {device.product}")
        connection = jiritsu_lib.connect(timeout=30, verbose=True)
        if connection is None:
            raise RuntimeError("Camera service device did not appear")
        if choice() == "length":
            connection.ch = LengthDelimitedSenserDevice(connection.auth.driver)
        camera = SonySenserCamera(connection.ch)
        camera.readHasp()
        versions = [read_file(camera, "/version.txt", 64) for _ in range(2)]
        if versions != [FIRMWARES["2.03"], FIRMWARES["2.03"]]:
            raise RuntimeError(f"Unexpected firmware versions: {versions!r}")
        status["firmware"] = versions[0].decode("ascii")
        print(f"固件双次读取通过 {status['firmware']}", flush=True)
        if args.logs:
            return read_plugin_log(camera, read_file)
        if args.clean_loader:
            return clean_loader_workspace(camera, read_file, jcmd)

        if not auto_clean_known_loader(camera, read_file, jcmd):
            print("[错误] loader 工作区未清理，停止。")
            return 1
        backend = JiritsuBackend(camera, session, "2.03")
        backend.start()
        applets = set(backend.run("busybox --list").split())
        (session / "busybox-applets.txt").write_text("\n".join(sorted(applets)), encoding="utf-8")
        required = {"cat", "chmod", "dd", "grep", "ls", "mv", "pidof", "rm", "sha256sum"}
        if not required <= applets:
            raise RuntimeError(f"Missing camera applets: {sorted(required - applets)}")
        status["state"] = "ready"
        runtime = Runtime(backend, upload_verified)
        probe.run_cam = runtime.run_cam
        probe.read_firmware_bytes = runtime.read_firmware_bytes
        if args.preload_status:
            return probe.cmd_preload_status()
        if args.reset_boot_guard:
            return probe.cmd_reset_boot_guard()
        if args.set_mode is not None:
            result = probe.cmd_set_mode(args.set_mode, args.preload_value)
            if result == 0 and args.reboot:
                runtime.reboot()
                status["software_reboot_scheduled"] = True
            return result
        if args.write_dmode is not None:
            result = probe.cmd_write_dmode(args.write_dmode, args.write_preload)
            if result == 0 and args.reboot:
                runtime.reboot()
                status["software_reboot_scheduled"] = True
            return result
        if args.write_env_txt is not None:
            result = probe.cmd_write_env_txt(args.write_env_txt)
            if result == 0 and args.reboot:
                runtime.reboot()
                status["software_reboot_scheduled"] = True
            return result
        if args.rm_env_txt:
            return probe.cmd_rm_env_txt()
        if args.kallsyms:
            return probe.cmd_kallsyms()
        if args.check or args.deploy or args.enable_preload:
            result = probe.cmd_check()
            if result or args.check:
                return result
        if args.stage_tmp or args.deploy:
            result = probe.cmd_stage_tmp(args.so)
            if result or args.stage_tmp:
                return result
        if args.enable_preload or args.deploy:
            result = probe.cmd_enable_preload()
        else:
            result = probe.cmd_disable_preload()
        if result == 0 and args.reboot:
            runtime.reboot()
            status["software_reboot_scheduled"] = True
        return result
    except Exception as error:
        status.update(state="failed", error=f"{type(error).__name__}: {error}")
        print(f"[错误] {status['error']}", file=sys.stderr, flush=True)
        return 1
    finally:
        try:
            try:
                if backend is not None:
                    backend.close()
            finally:
                if connection is not None:
                    connection.auth.stop()
                    status["service_returned"] = True
                    status["usb_service_stopped"] = True
                    status["usb_pids_after_stop"] = usb_pids_snapshot()
        except Exception as error:
            status.update(state="cleanup-failed", cleanup_error=repr(error))
            raise
        finally:
            if backend is not None:
                status.update(backend.status)
            if status["state"] != "failed" and status["state"] != "cleanup-failed":
                status["state"] = "closed"
            (session / "state.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
            print_session_exit(status)
            if status.get("software_reboot_scheduled"):
                print("加载器已清理，已安排软件重启。请保持 USB 连接，等待相机重新连线后读取日志。", flush=True)
            elif status.get("requires_power_cycle"):
                print("（RAM 子 shell 会在相机关机断电后消失；本步不需要现在断电，按清单执行下一步即可。）", flush=True)


if __name__ == "__main__":
    raise SystemExit("Use probe_zve10_runtime.py --backend jiritsu")
