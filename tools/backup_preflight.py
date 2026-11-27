#!/usr/bin/env python3
"""Pre-experiment backup preflight for the ZV-E10 WBI cold-boot experiment.

Subcommands
-----------
  --audit          Host-only. Inventory ZVE10-Backups, verify every existing
                   manifest / SHA256SUMS / hash, and say what is already covered
                   so nothing is backed up twice.
  --collect        Camera, one Jiritsu session. Reads the whole Linux-visible
                   eMMC user area plus Boot0/Boot1 twice (toolkit raw-backup,
                   2 passes, 4 MiB chunks), then does dedicated double reads of
                   nflasha2 / nflasha5 / nflasha10, captures the configuration
                   files, and records firmware/geometry/device identity.
  --offline        Host-only. Verifies the collected set, slices the target
                   partitions, writes offline-verification.json + layout.tsv,
                   builds the Pico/fastboot sparse images, and runs the official
                   restore_rescue.py --prepare-only as a cross-check.
  --selftest       Host-only. Exercises the parsers, hash gates and slice math
                   with synthetic data; never touches a camera.

Safety
------
* Read-only on the camera: FileControl READ only (plus the toolkit's own
  temporary RAM loader, removed and GET_FILE-verified at close).
* No WBI drop, no partition write, no restore, no Pico hardware.
* Refuses to continue when the firmware string is not the pinned 2.03 build.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
TOOLKIT = ROOT / "third_party" / "zve10-toolkit" / "linux"
FASTBOOT = ROOT / "third_party" / "zve10-toolkit" / "fastboot"
DEFAULT_BACKUP_ROOT = ROOT.parent / "ZVE10-Backups"

EXPECTED_VERSION = b"30110_06.2026060501"
TARGET_PARTITIONS = ("nflasha2", "nflasha5", "nflasha10")
RAW_CHUNK_KIB = 4096

CONFIG_FILES = (
    ("/setting/Backup.bin", "Backup.bin"),
    ("/setting/Backup.bak", "Backup.bak"),
    ("/setting/kemco.txt", "kemco.txt"),
    ("/setting/mode/dmode", "dmode"),
    ("/setting/mode/preload", "preload"),
    ("/setting/env.txt", "env.txt"),
    ("/tmp/libzve10plugin.so", "libzve10plugin.so"),
    ("/tmp/zvp_boot_guard", "zvp_boot_guard"),
)

META_COMMANDS = (
    ("partitions", "busybox cat /proc/partitions"),
    ("mounts", "busybox cat /proc/mounts"),
    ("cpuinfo", "busybox cat /proc/cpuinfo"),
    ("kernel_version", "busybox cat /proc/version"),
    ("uptime", "busybox cat /proc/uptime"),
    ("boot_id", "busybox cat /proc/sys/kernel/random/boot_id"),
    ("meminfo", "busybox cat /proc/meminfo"),
)
LAYOUT_COMMAND = (
    "for p in /sys/class/block/nflasha*; do echo DEVICE:$p; "
    "for f in dev size start ro; do echo FIELD:$f; "
    'busybox cat "$p/$f" 2>/dev/null; done; done'
)
IDENTITY_COMMAND = (
    "for p in /sys/class/block/nflasha/device /sys/class/mmc_host/mmc*/* "
    "/sys/bus/mmc/devices/*; do "
    "for f in cid csd manfid oemid name date hwrev fwrev; do "
    'if [ -f "$p/$f" ]; then echo FILE:$p/$f; busybox cat "$p/$f"; fi; '
    "done; done; true"
)


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #

def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_proc_partitions(text: str) -> dict:
    """name -> {'major','minor','bytes'} for every nflasha* row."""
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 4 or not fields[3].startswith("nflasha"):
            continue
        major, minor, blocks, name = fields[0], fields[1], fields[2], fields[3]
        if not (major.isdecimal() and minor.isdecimal() and blocks.isdecimal()):
            raise ValueError(f"malformed /proc/partitions line: {line!r}")
        if name in result:
            raise ValueError(f"duplicate partition {name}")
        result[name] = {"major": int(major), "minor": int(minor), "bytes": int(blocks) * 1024}
    if not result:
        raise ValueError("no nflasha partitions found")
    return result


def parse_sysfs_layout(text: str) -> dict:
    """name -> {'dev','size','start','ro'} from the LAYOUT_COMMAND output."""
    result = {}
    device = field = None
    for line in text.splitlines():
        if line.startswith("DEVICE:"):
            device = line[7:].rsplit("/", 1)[-1]
            if device in result:
                raise ValueError(f"duplicate sysfs device {device}")
            result[device] = {}
            field = None
        elif line.startswith("FIELD:"):
            field = line[6:]
            if device is None or field not in ("dev", "size", "start", "ro"):
                raise ValueError("unexpected sysfs field")
            result[device][field] = ""
        elif line.strip():
            if device is None or field is None:
                raise ValueError("sysfs value without field")
            result[device][field] = line.strip()
    for name, fields in result.items():
        if not fields.get("dev") or not fields.get("size"):
            raise ValueError(f"incomplete sysfs identity for {name}: {sorted(fields)}")
        if fields.get("ro", "") not in ("0", "1"):
            raise ValueError(f"missing sysfs ro flag for {name}")
        if name.startswith("nflasha") and name[7:].isdigit() and not fields.get("start", "").isdecimal():
            raise ValueError(f"missing start sector for partition {name}")
    return result


def layout_rows(partitions: dict, layout: dict) -> list:
    """Build (name, offset, size) rows for the 16 user partitions.

    Offsets come from the live sysfs ``start`` in 512-byte sectors; sizes come
    from /proc/partitions and must agree with sysfs.  Whole-area devices are
    excluded (they are not partitions of the user-area image).
    """
    rows = []
    for name, info in sorted(partitions.items()):
        if name not in layout:
            raise ValueError(f"{name} missing from sysfs layout")
        fields = layout[name]
        dev = fields["dev"]
        if not re.fullmatch(r"\d+:\d+", dev):
            raise ValueError(f"bad dev for {name}")
        major, minor = map(int, dev.split(":"))
        if (major, minor) != (info["major"], info["minor"]):
            raise ValueError(f"proc/sysfs identity mismatch for {name}")
        size = int(fields["size"]) * 512
        if size != info["bytes"]:
            raise ValueError(f"proc/sysfs capacity mismatch for {name}")
        if name.startswith("nflasha") and name[7:].isdigit():
            if not fields.get("start", "").isdecimal():
                raise ValueError(f"missing start for {name}")
            rows.append({"name": name, "offset": int(fields["start"]) * 512, "bytes": size})
    if not rows:
        raise ValueError("no user partitions in layout")
    rows.sort(key=lambda row: row["offset"])
    return rows


def write_layout_tsv(rows: list, path: Path) -> None:
    lines = ["# name offset size", "# generated from live /proc/partitions + sysfs start"]
    lines += [f"{row['name']} {row['offset']} {row['bytes']}" for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------- #
# audit (host only)
# --------------------------------------------------------------------------- #

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def audit(root: Path) -> int:
    print(f"=== ZVE10-Backups 审计: {root} ===")
    if not root.is_dir():
        print("目录不存在")
        return 1
    findings = []
    covered = {"wbi_full": False, "user_area": False, "boot_areas": False,
               "nflasha2": False, "nflasha10": False, "config": False}

    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        files = list(folder.rglob("*"))
        total = sum(f.stat().st_size for f in files if f.is_file())
        print(f"\n--- {folder.name}  ({total/1024/1024:.1f} MiB, {sum(1 for f in files if f.is_file())} files) ---")

        wbi_manifest = folder / "wbi-manifest.json"
        raw_manifest = folder / "manifest.json"
        sums = folder / "SHA256SUMS"

        if wbi_manifest.is_file():
            manifest = _read_json(wbi_manifest) or {}
            print("类型: WBI 双读备份（自定义清单）")
            ok = True
            for row in manifest.get("reads", []):
                basename = str(row.get("path", "")).replace("\\", "/").rsplit("/", 1)[-1]
                local = folder / basename
                if not local.is_file():
                    print(f"  MISSING {basename}")
                    ok = False
                    continue
                actual = sha_file(local)
                match = actual == row.get("sha256") and local.stat().st_size == row.get("bytes")
                print(f"  {'OK ' if match else 'FAIL'} {local.name}  {local.stat().st_size} B  {actual}")
                ok = ok and match
            header = manifest.get("header", "")
            print(f"  头部: {header[:8]}  match={manifest.get('match')}")
            if ok and header.startswith("57424931"):
                covered["wbi_full"] = True
            findings.append((folder.name, "WBI", ok))

        elif raw_manifest.is_file():
            manifest = _read_json(raw_manifest) or {}
            print("类型: 原始 eMMC 备份（toolkit manifest）")
            print(f"  completed={manifest.get('completed')} passes={manifest.get('passes')} "
                  f"backend={manifest.get('backend')} system={manifest.get('system_version')}")
            reads = manifest.get("reads", [])
            ok = bool(reads) and bool(manifest.get("completed"))
            for row in reads:
                target = folder / row.get("file", "")
                if not target.is_file():
                    print(f"  MISSING {row.get('file')}")
                    ok = False
                    continue
                actual = sha_file(target)
                match = actual == row.get("sha256") and target.stat().st_size == row.get("expected_bytes")
                print(f"  {'OK ' if match else 'FAIL'} {row.get('file')}  {target.stat().st_size} B")
                ok = ok and match
            if sums.is_file():
                proc = subprocess.run([sys.executable, str(TOOLKIT / "host" / "zve10_verify.py"),
                                       "sums", "--sums", str(sums)], capture_output=True, text=True)
                print("  SHA256SUMS:", "PASS" if proc.returncode == 0 else "FAIL")
                ok = ok and proc.returncode == 0
            names = {row.get("device") for row in reads}
            if ok:
                covered["user_area"] = covered["user_area"] or "/dev/nflasha" in names
                covered["boot_areas"] = covered["boot_areas"] or {"/dev/nflashaB0", "/dev/nflashaB1"} <= names
            findings.append((folder.name, "RAW", ok))
        elif (folder / "backup-manifest.json").is_file():
            manifest = _read_json(folder / "backup-manifest.json") or {}
            print("类型: preflight 备份（zvp-backup-v1）")
            print(f"  firmware={manifest.get('firmware')} targeted={list(manifest.get('targeted', {}))}")
            ok = manifest.get("firmware") == EXPECTED_VERSION.decode()
            sums = folder / "SHA256SUMS"
            if sums.is_file():
                proc = subprocess.run([sys.executable, str(TOOLKIT / "host" / "zve10_verify.py"),
                                       "sums", "--sums", str(sums)], capture_output=True, text=True)
                print("  SHA256SUMS:", "PASS" if proc.returncode == 0 else "FAIL")
                ok = ok and proc.returncode == 0
            if ok:
                covered["user_area"] = True
                covered["boot_areas"] = True
                covered["nflasha2"] = "nflasha2" in manifest.get("targeted", {})
                covered["nflasha10"] = "nflasha10" in manifest.get("targeted", {})
                covered["config"] = bool(manifest.get("config"))
            findings.append((folder.name, "PREFLIGHT", ok))
        else:
            print("类型: 非镜像备份（代码/构建产物），跳过哈希验证")
            findings.append((folder.name, "OTHER", None))

    print("\n=== 覆盖情况 ===")
    labels = {
        "wbi_full": "WBI 全区双读（60 MiB ×2）",
        "user_area": "eMMC User Area 全区双读",
        "boot_areas": "Boot0 / Boot1 双读",
        "nflasha2": "nflasha2 独立镜像（/setting）",
        "nflasha10": "nflasha10 独立镜像（/tmp）",
        "config": "Backup.bin / Backup.bak / kemco.txt / dmode / preload",
    }
    for key, label in labels.items():
        print(f"  {'[已有]' if covered[key] else '[缺]  '} {label}")
    print("\n结论: " + ("已有 WBI 备份可复用，仍需补齐 User Area/B0/B1、nflasha2/10 与配置。"
                       if covered["wbi_full"] and not covered["user_area"]
                       else "按 --collect 补齐缺口。"))
    return 0


# --------------------------------------------------------------------------- #
# collect (camera, one Jiritsu session)
# --------------------------------------------------------------------------- #

class _Sink:
    def __init__(self, expected: int):
        self.buffer = bytearray()
        self.expected = expected

    def write(self, data: bytes) -> None:
        if len(self.buffer) + len(data) > self.expected:
            raise RuntimeError("read exceeds expected size")
        self.buffer.extend(data)


def _capture_file(camera, read_file, remote: str, local: Path) -> dict:
    try:
        data = read_file(camera, remote, 64 * 1024 * 1024)
    except RuntimeError as error:
        if "0x10000006" in str(error):
            return {"remote": remote, "present": False}
        raise
    local.write_bytes(data)
    return {"remote": remote, "present": True, "bytes": len(data), "sha256": sha_bytes(data)}


def collect_session(backend, camera, read_file, out: Path, status: dict) -> int:
    out.mkdir(parents=True, exist_ok=True)
    metadata_dir = out / "metadata"
    metadata_dir.mkdir(exist_ok=True)
    config_dir = out / "config"
    config_dir.mkdir(exist_ok=True)

    print("[1/6] 采集运行态与几何信息…", flush=True)
    metadata = {}
    for name, command in META_COMMANDS + (("layout", LAYOUT_COMMAND), ("identity", IDENTITY_COMMAND)):
        metadata[name] = backend.run(command)
        (metadata_dir / f"{name}.txt").write_text(metadata[name], encoding="utf-8")
    partitions = parse_proc_partitions(metadata["partitions"])
    layout = parse_sysfs_layout(metadata["layout"])
    rows = layout_rows(partitions, layout)
    write_layout_tsv(rows, out / "layout.tsv")
    print(f"  {len(rows)} 个用户分区，nflasha={partitions.get('nflasha', {}).get('bytes')} B")

    print("[2/6] 双次固件校验…", flush=True)
    versions = [read_file(camera, "/version.txt", 64) for _ in range(2)]
    if versions != [EXPECTED_VERSION, EXPECTED_VERSION]:
        raise RuntimeError(f"Unexpected firmware versions: {versions!r}")
    version_sha = sha_bytes(versions[0])
    (metadata_dir / "version.txt").write_bytes(versions[0])

    print(f"[3/6] 全盘双读：user area + B0 + B1（passes=2, chunk={RAW_CHUNK_KIB} KiB）…", flush=True)
    from zve10_backup_session import dispatch as raw_dispatch
    import zve10_backup_raw as raw
    raw_dispatch(camera, {"passes": 2, "chunk_kib": RAW_CHUNK_KIB}, str(out), False)
    raw_dir = Path(raw.OUT)
    if not (raw_dir / "manifest.json").is_file():
        raise RuntimeError("raw backup did not produce manifest.json")
    if raw.RESULT.get("buffer", {}).get("dedicated_tmpfs_bytes"):
        if backend.status.get("backup_buffer_removed") is not True:
            raise RuntimeError("dedicated backup tmpfs not confirmed removed")
    print(f"  原始备份: {raw_dir}")

    print("[4/6] nflasha2/5/10 独立双读…", flush=True)
    import zve10_jiritsu as zj
    targeted_dir = out / "targeted"
    targeted_dir.mkdir(exist_ok=True)
    targeted = {}
    for name in TARGET_PARTITIONS:
        if name not in partitions:
            raise RuntimeError(f"missing partition {name}")
        size = partitions[name]["bytes"]
        zj.REGIONS.setdefault(f"/dev/{name}", size)
        hashes = []
        for iteration in (1, 2):
            print(f"  {name} read{iteration} ({size} B)…", flush=True)
            sink = _Sink(size)
            result, _ = backend.read_path(f"/dev/{name}", sink)
            if result not in (0, 1) or len(sink.buffer) != size:
                raise RuntimeError(f"{name} read{iteration} failed or short")
            data = bytes(sink.buffer)
            (targeted_dir / f"{name}.read{iteration}.bin").write_bytes(data)
            hashes.append(sha_bytes(data))
        targeted[name] = {
            "bytes": size,
            "read1_sha256": hashes[0],
            "read2_sha256": hashes[1],
            "identical": hashes[0] == hashes[1],
        }
        if name == "nflasha5" and hashes[0] != hashes[1]:
            raise RuntimeError("WBI double read differs; STOP and analyse before any write")

    print("[5/6] 配置文件备份…", flush=True)
    config = {}
    for remote, local_name in CONFIG_FILES:
        info = _capture_file(camera, read_file, remote, config_dir / local_name)
        config[local_name] = info
        print(f"  {local_name}: {'present' if info['present'] else 'ABSENT'}"
              + (f" ({info['bytes']} B)" if info["present"] else ""))

    print("[6/6] 写入主清单…", flush=True)
    manifest = {
        "schema": "zvp-backup-v1",
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": platform.node(),
        "purpose": "pre-flight backup before WBI cold-boot experiment (read-only collection)",
        "firmware": versions[0].decode("ascii"),
        "version_txt_sha256": version_sha,
        "version_txt_reads": 2,
        "geometry": {
            "partitions": partitions,
            "layout": layout,
            "rows": rows,
        },
        "raw_backup": {
            "dir": str(raw_dir),
            "manifest_sha256": sha_file(raw_dir / "manifest.json"),
            "sha256sums_sha256": sha_file(raw_dir / "SHA256SUMS")
            if (raw_dir / "SHA256SUMS").is_file() else None,
        },
        "targeted": targeted,
        "config": config,
        "metadata": {name: sha_bytes(metadata[name].encode("utf-8")) for name in metadata},
    }
    manifest_path = out / "backup-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sums = []
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.name != "backup-manifest.json":
            sums.append(f"{sha_file(path)}  {path.relative_to(out).as_posix()}")
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8", newline="\n")
    print(f"  主清单: {manifest_path}")
    return 0


def run_collect(args) -> int:
    from zve10_jiritsu_runtime import print_session_exit, usb_pids_snapshot

    root = ROOT
    toolkit = TOOLKIT
    if not (toolkit / "host" / "zve10_jiritsu.py").is_file():
        print("请先 git submodule update --init third_party/zve10-toolkit")
        return 1
    sys.path.insert(0, str(root / "third_party" / "camtool"))
    sys.path.insert(0, str(toolkit / "host"))
    os.environ.setdefault("PMCA_ROOT", str(root / "third_party" / "pmca"))
    import jiritsu_lib
    from zve10_jiritsu_runtime import find_device
    jiritsu_lib.find = find_device
    from pmca.usb.sony import SonySenserCamera
    from zve10_senser import LengthDelimitedSenserDevice, choice
    from zve10_read_file_jiritsu import read_file
    from zve10_jiritsu import FIRMWARES, JiritsuBackend, jcmd
    from zve10_jiritsu_runtime import auto_clean_known_loader

    out = Path(args.out).resolve()
    if out.exists():
        entries = list(out.iterdir())
        reusable = (not entries
                    or (not (out / "backup-manifest.json").exists()
                        and all(entry.is_dir() and entry.name in ("metadata", "config")
                                for entry in entries)))
        if not reusable:
            print(f"[错误] 输出目录已存在且非空: {out}")
            print("       请换一个新目录（推荐），或手动清空后重试；避免与上次失败/半成品混在一起。")
            return 1
        if entries:
            print(f"[提示] 复用上次失败留下的 metadata/（其余内容会重新采集）: {out}")
    connection = None
    backend = None
    session = Path(tempfile.mkdtemp(prefix="zvp-bak-"))
    status = {"session": str(session), "state": "connecting", "action": "backup-collect"}
    result_code = 1
    exit_ok = False
    print(f"Jiritsu 会话记录 {session}")
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
        print(f"固件双次读取通过 {status['firmware']}")
        if not auto_clean_known_loader(camera, read_file, jcmd):
            raise RuntimeError("loader workspace not clean")
        backend = JiritsuBackend(camera, session, "2.03")
        backend.start()
        result_code = collect_session(backend, camera, read_file, out, status)
    except Exception as error:
        status.update(state="failed", error=f"{type(error).__name__}: {error}")
        print(f"[错误] {status['error']}", file=sys.stderr)
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
            status.update(state="cleanup-failed", cleanup_error=f"{type(error).__name__}: {error}")
            raise
        finally:
            if backend is not None:
                status.update(backend.status)
            if status["state"] not in ("failed", "cleanup-failed"):
                status["state"] = "closed"
            (session / "state.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
            exit_ok = print_session_exit(status)
            try:
                out_dir = Path(args.out).resolve()
                if out_dir.is_dir():
                    (out_dir / "session-exit.json").write_text(
                        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except Exception as error:
                print(f"[警告] session-exit.json 写入失败: {error!r}")
    if not exit_ok:
        print("[错误] 会话退出验证失败；备份不可视为完成。", file=sys.stderr)
        return 1
    if result_code != 0:
        print("[错误] 采集未完成；已保留部分文件，勿视为有效备份。", file=sys.stderr)
        return result_code
    print(f"\n备份完成: {out}")
    print("下一步（离线，不连相机）: --offline --out <同一目录>")
    return 0


# --------------------------------------------------------------------------- #
# offline (host only)
# --------------------------------------------------------------------------- #

def _run(cmd, label):
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    if output:
        print(output)
    print(f"{label}: {'PASS' if proc.returncode == 0 else 'FAIL'}")
    return proc.returncode == 0


def stage_pico_kit(out: Path) -> Path:
    """Copy the fastboot/Pico recovery toolchain into <out>/pico-kit."""
    kit = out / "pico-kit"
    wanted = [
        "host/restore_rescue.py",
        "host/bootstrap.py",
        "tools/make_sparse.py",
        "tools/check_abi.py",
        "prebuilt/stage1.bin",
        "prebuilt/stage2-flash.bin",
        "prebuilt/manifest.json",
        "prebuilt/SHA256SUMS",
        "pico/inject_payload.py",
        "pico/pico_link.py",
        "pico/inspect_pico.py",
        "pico/reset_only.py",
        "pico/README.zh-CN.md",
        "pico/PROTOCOL.md",
        "docs/recovery.zh-CN.md",
        "docs/hardware.zh-CN.md",
        "requirements.txt",
    ]
    entries = []
    for rel in wanted:
        source = FASTBOOT / rel
        if not source.is_file():
            print(f"[警告] Pico kit 缺少 {rel}")
            continue
        target = kit / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        entries.append({"path": rel, "bytes": target.stat().st_size, "sha256": sha_file(target)})
    (kit / "KIT-MANIFEST.json").write_text(
        json.dumps({"source": "third_party/zve10-toolkit/fastboot", "files": entries},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (kit / "README.md").write_text(
        """# Pico / fastboot 恢复包

用途：当相机无法正常启动、但 BootROM 串口注入与 USB 仍可用时，用 Pico 注入 stage1，
再由本目录的 `host/restore_rescue.py` 通过 fastboot 恢复**用户分区**。

## 前提与限制

- 只支持用户分区（nflasha1/2/3/4/5/6/7/10/11/12/13/15/16/17/18/23）；
  **不支持 Boot0/Boot1、分区表、RPMB/OTP 或物理硬件故障**。
- 上游实机记录只覆盖固件 2.02 / macOS 主机；2.03 与 Windows 走这条路线**未经验证**，
  只能作为最后手段。
- 需要：RP2040 Pico + 24pin USB-C 转接板（按 `docs/hardware.zh-CN.md` 接线）、
  Python 3.10+、libusb、Android platform-tools 的 `fastboot`。

## 文件

| 文件 | 用途 |
|---|---|
| `prebuilt/stage1.bin` | Pico 串口注入的第一级（7636 B） |
| `prebuilt/stage2-flash.bin` | fastboot stage2，支持 raw/sparse（10228 B） |
| `host/restore_rescue.py` | 切片 + sparse + 写入 + 回读校验 |
| `tools/make_sparse.py` | 独立 sparse 生成器（离线准备用） |
| `pico/inject_payload.py` | 串口注入 stage1 |
| `docs/recovery.zh-CN.md` | 官方恢复流程 |

## 离线准备（不接相机）

```sh
python host/restore_rescue.py --backup-dir <备份目录> \
  --partitions nflasha2,nflasha5,nflasha10 --prepare-only --out-dir <输出目录>
```

## 实机恢复（需要 Pico 硬件）

```sh
python host/restore_rescue.py --backup-dir <备份目录> \
  --partitions nflasha2,nflasha5,nflasha10
```
""", encoding="utf-8")
    print(f"Pico kit: {kit} ({len(entries)} files)")
    return kit


def offline(args) -> int:
    out = Path(args.out).resolve()
    manifest_path = out / "backup-manifest.json"
    if not manifest_path.is_file():
        print(f"missing {manifest_path}")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("firmware") != EXPECTED_VERSION.decode():
        print(f"firmware mismatch: {manifest.get('firmware')!r}")
        return 1

    print("=== 1. 全量文件哈希复核 ===")
    bad = []
    for line in (out / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, name = line.split("  ", 1)
        target = out / name
        actual = sha_file(target) if target.is_file() else "missing"
        if actual != expected:
            bad.append(name)
            print(f"FAIL {name}")
    print(f"文件复核: {'PASS' if not bad else 'FAIL ' + str(bad)}")

    raw_dir = Path(manifest["raw_backup"]["dir"])
    if not raw_dir.is_dir():
        raw_dir = out / raw_dir.name
    print("\n=== 2. raw-backup 清单与 SHA256SUMS ===")
    ok_manifest = _run([sys.executable, TOOLKIT / "host" / "zve10_verify.py", "manifest",
                        "--manifest", raw_dir / "manifest.json"], "manifest")
    ok_sums = _run([sys.executable, TOOLKIT / "host" / "zve10_verify.py", "sums",
                    "--sums", raw_dir / "SHA256SUMS"], "sums")

    print("\n=== 3. 分区切片双读比对 ===")
    verification = out / "offline-verification.json"
    ok_slices = _run([sys.executable, TOOLKIT / "host" / "zve10_verify.py", "slices",
                      "--read1", raw_dir / "nflasha.read1.bin",
                      "--read2", raw_dir / "nflasha.read2.bin",
                      "--layout", out / "layout.tsv",
                      "--json", verification], "slices")
    rows = {}
    difference_analysis = []
    if verification.is_file():
        shutil.copy2(verification, raw_dir / "offline-verification.json")
        report = json.loads(verification.read_text(encoding="utf-8"))
        rows = {row["name"]: row for row in report.get("partitions", [])}
        differing = [name for name, row in rows.items() if not row.get("identical")]
        print(f"切片差异分区: {differing or '无'}")
        mounts_text = ""
        meta_mounts = out / "metadata" / "mounts.txt"
        if meta_mounts.is_file():
            mounts_text = meta_mounts.read_text(encoding="utf-8", errors="replace")
        mount_map = {}
        for line in mounts_text.splitlines():
            fields = line.split()
            if len(fields) >= 4:
                mount_map[fields[0]] = (fields[1], fields[3])
        diff_bytes = {}
        for sector in report.get("different_sectors", []):
            for name in sector.get("partitions", []):
                diff_bytes[name] = diff_bytes.get(name, 0) + int(sector.get("different_bytes", 0))
        for name in differing:
            mount, options = mount_map.get("/dev/" + name, (None, ""))
            rw = "rw" in options.split(",")
            difference_analysis.append({
                "partition": name,
                "mount": mount,
                "options": options,
                "rw": rw,
                "different_bytes": diff_bytes.get(name, 0),
                "expected": rw,
                "reason": ("只读挂载却出现差异，必须人工分析" if not rw
                           else "读写挂载，会话期间可能变化（日志/缓存/配置），属预期"),
            })
        (out / "difference-analysis.json").write_text(
            json.dumps({"partitions": difference_analysis}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        for item in difference_analysis:
            print(f"  {item['partition']}: mount={item['mount']} rw={item['rw']} "
                  f"diff={item['different_bytes']} B -> {'预期' if item['expected'] else '需分析'}")
        wbi = rows.get("nflasha5")
        if wbi is not None and not wbi.get("identical"):
            print("FAIL nflasha5 两次全盘读取不一致：WBI 在采集期间发生变化，停止。")
            ok_slices = False

    print("\n=== 4. 目标分区镜像与 sparse ===")
    part_dir = out / "partitions"
    part_dir.mkdir(exist_ok=True)
    image = raw_dir / "nflasha.read1.bin"
    target_ok = True
    with image.open("rb") as handle:
        for name in TARGET_PARTITIONS:
            row = rows.get(name)
            if not row:
                print(f"FAIL {name}: 不在 offline-verification.json")
                target_ok = False
                continue
            handle.seek(row["offset"])
            data = handle.read(row["bytes"])
            raw_path = part_dir / f"{name}.bin"
            raw_path.write_bytes(data)
            digest = sha_bytes(data)
            match = digest == row["read1_sha256"]
            print(f"{'OK ' if match else 'FAIL'} {name}.bin {len(data)} B {digest[:16]} "
                  f"(read1 {'match' if match else 'MISMATCH'})")
            target_ok = target_ok and match
            simg = part_dir / f"{name}.simg"
            sparse = subprocess.run([sys.executable, FASTBOOT / "tools" / "make_sparse.py",
                                     "--raw", str(raw_path), "--output", str(simg)],
                                    capture_output=True, text=True)
            if sparse.returncode:
                print(f"FAIL {name}.simg: {sparse.stderr.strip()[:200]}")
                target_ok = False
            else:
                print(f"OK  {name}.simg {simg.stat().st_size} B")

    print("\n=== 5. 官方 restore_rescue --prepare-only 交叉验证 ===")
    rescue_dir = out / "pico-rescue"
    ok_rescue = _run([sys.executable, FASTBOOT / "host" / "restore_rescue.py",
                      "--backup-dir", raw_dir,
                      "--partitions", ",".join(TARGET_PARTITIONS),
                      "--prepare-only", "--out-dir", rescue_dir],
                     "restore_rescue prepare")

    print("\n=== 6. 生成 Pico 恢复工具包 ===")
    kit_dir = stage_pico_kit(out)

    print("\n=== 6.5 派生文件校验 ===")
    derived = []
    for path in sorted(out.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(out).as_posix()
        if rel in ("backup-manifest.json", "SHA256SUMS", "offline-SHA256SUMS", "session-exit.json"):
            continue
        if (rel.startswith(("partitions/", "pico-rescue/", "pico-kit/"))
                or rel in ("offline-verification.json", "difference-analysis.json")):
            derived.append(f"{sha_file(path)}  {rel}")
    (out / "offline-SHA256SUMS").write_text("\n".join(derived) + "\n", encoding="utf-8", newline="\n")
    print(f"  {len(derived)} 个派生文件 -> offline-SHA256SUMS")

    print("\n=== 汇总 ===")
    summary = {
        "files": not bad,
        "raw_manifest": ok_manifest,
        "raw_sums": ok_sums,
        "slices": ok_slices,
        "targets": target_ok,
        "restore_prepare": ok_rescue,
        "pico_files": str(rescue_dir),
        "pico_kit": str(kit_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(v is True for k, v in summary.items()
                    if k not in ("pico_files", "pico_kit")) else 1


# --------------------------------------------------------------------------- #
# selftest (host only)
# --------------------------------------------------------------------------- #

def selftest() -> int:
    partitions_text = "\n".join([
        "major minor  #blocks  name",
        " 252     0    4194304 nflasha",
        " 252     1       8128 nflasha1",
        " 252     2      20480 nflasha2",
        " 252     5      61440 nflasha5",
        " 252    10      12288 nflasha10",
        " 252    20       4096 nflashaB0",
        " 252    21       4096 nflashaB1",
    ])
    layout_text = "\n".join([
        "DEVICE:/sys/class/block/nflasha",
        "FIELD:dev", "252:0", "FIELD:size", "8388608", "FIELD:ro", "0",
        "DEVICE:/sys/class/block/nflasha1",
        "FIELD:dev", "252:1", "FIELD:size", "16256", "FIELD:start", "128", "FIELD:ro", "0",
        "DEVICE:/sys/class/block/nflasha2",
        "FIELD:dev", "252:2", "FIELD:size", "40960", "FIELD:start", "16384", "FIELD:ro", "0",
        "DEVICE:/sys/class/block/nflasha5",
        "FIELD:dev", "252:5", "FIELD:size", "122880", "FIELD:start", "180224", "FIELD:ro", "0",
        "DEVICE:/sys/class/block/nflasha10",
        "FIELD:dev", "252:10", "FIELD:size", "24576", "FIELD:start", "352256", "FIELD:ro", "0",
        "DEVICE:/sys/class/block/nflashaB0",
        "FIELD:dev", "252:20", "FIELD:size", "8192", "FIELD:ro", "0",
        "DEVICE:/sys/class/block/nflashaB1",
        "FIELD:dev", "252:21", "FIELD:size", "8192", "FIELD:ro", "0",
    ])
    parts = parse_proc_partitions(partitions_text)
    layout = parse_sysfs_layout(layout_text)
    rows = layout_rows(parts, layout)
    by_name = {row["name"]: row for row in rows}
    assert by_name["nflasha2"]["offset"] == 16384 * 512
    assert by_name["nflasha5"]["offset"] == 180224 * 512
    assert by_name["nflasha10"]["offset"] == 352256 * 512
    assert by_name["nflasha5"]["bytes"] == 61440 * 1024
    assert len(rows) == 4
    print("selftest: layout OK", {name: (row["offset"], row["bytes"]) for name, row in by_name.items()})

    tmp = Path(tempfile.mkdtemp(prefix="zvp-backup-selftest-"))
    tsv = tmp / "layout.tsv"
    write_layout_tsv(rows, tsv)
    assert "nflasha5 92274688 62914560" in tsv.read_text(encoding="utf-8")
    print("selftest: tsv OK")

    blob = bytes(range(256)) * 4096
    p = tmp / "x.bin"
    p.write_bytes(blob)
    assert sha_file(p) == sha_bytes(blob)
    print("selftest: hash OK")
    print("SELFTEST OK")
    return 0


# --------------------------------------------------------------------------- #
# entry
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--audit", action="store_true", help="审计已有备份（不连相机）")
    group.add_argument("--collect", action="store_true", help="连接相机做一次完整备份会话")
    group.add_argument("--offline", action="store_true", help="离线校验/切片/Pico 准备")
    group.add_argument("--selftest", action="store_true", help="离线自检")
    parser.add_argument("--out", default=None, help="备份输出目录（collect/offline 必填）")
    parser.add_argument("--backup-root", default=str(DEFAULT_BACKUP_ROOT), help="已有备份根目录")
    parser.add_argument("--jiritsu-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.audit:
        return audit(Path(args.backup_root))
    if args.selftest:
        return selftest()
    if args.offline:
        if not args.out:
            parser.error("--offline 需要 --out")
    else:
        if not args.out:
            parser.error("--collect 需要 --out")
    if not args.jiritsu_worker:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        command = ["uv", "run", "--with", "pyusb", "--with", "libusb-package",
                   "--with", "pycryptodomex", "--with", "pyelftools", "python",
                   os.path.abspath(__file__), *sys.argv[1:], "--jiritsu-worker"]
        return subprocess.run(command, env=env).returncode
    if args.offline:
        return offline(args)
    return run_collect(args)


if __name__ == "__main__":
    raise SystemExit(main())