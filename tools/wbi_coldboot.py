"""Guarded WBI invalidation for ZV-E10 firmware 2.03 (cold-boot enablement).

Purpose
-------
The camera normally powers off into STR (``/sys/power/state`` = ``"mem"``), so the
factory fast-start image in ``/dev/nflasha5`` is only used after a real power loss.
To make the ``bootin`` preload path run, that image must be invalidated once, with
Sony's own ``ud_wbi_drop.elf`` (the same utility the official updater runs).  This
module implements exactly that recipe and nothing else:

* double-read the whole 60 MiB partition and prove the two reads match,
* require a ``WBI1`` header (refuse unknown headers and already-invalid images),
* upload the hash-pinned Sony utility plus its five-entry partition file,
* execute it from the running system (never from fastboot),
* read the whole partition back and prove *only* the first 512 bytes changed,
* remove the temporary files and record a manifest.

There is deliberately no block writer here: recovery is the official updater
(which rewrites ``nflasha5``) or the owner's own full backup, never a hand-rolled
write.  All device access goes through the Jiritsu backend used by the rest of
the toolkit suite.

Testability
-----------
Nothing in this module imports USB libraries; the camera side arrives as a
``backend`` object with ``read_path``, ``run``, ``ram``, ``camera`` and
``camera``-scoped ``upload_verified``.  The pure helpers can therefore be tested
on the host with synthetic buffers.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import time
import uuid
from pathlib import Path

DEVICE = "/dev/nflasha5"
SIZE = 60 * 1024 * 1024
SECTOR = 512

TOOL_NAME = "ud_wbi_drop.elf"
TOOL_BYTES = 6200
TOOL_SHA256 = "f2a5b27a0c11d55a612d61e61e82c55278efb744052ecb8ec2cbbd8851d0ea6e"

CONFIG = (
    b"# start address,size,type,flag\n"
    b"0x00010000,0x007F0000,0x00000001,0xFFFFFFFF\n"
    b"0x00800000,0x01400000,0x00000001,0xFFFFFFFF\n"
    b"0x01C00000,0x03000000,0x00000001,0xFFFFFFFF\n"
    b"0x04C00000,0x00C00000,0x00000000,0xFFFFFFFF\n"
    b"0x05800000,0x03C00000,0x00000002,0xFFFFFFFF\n"
)
CONFIG_SHA256 = "9bb1cba13c10ce88bd88d054dc49a823b3b1409ce6dfbeaeaa180f31d22744a8"

EXPECTED_OUTPUT = "[UD_WBI_DROP] /dev/nflasha5 wbi_drop"

HEADER_VERSIONS = {
    b"WBI1": "valid (steady-state fast-start image)",
    b"WBI0": "optimized image, rebuild not finished",
    b"prof": "profile image, rebuild stage 1",
    b"invl": "write-in-progress marker, rebuild restarts at stage 0",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# pure helpers (offline-testable)
# --------------------------------------------------------------------------- #

def parse_header(first: bytes) -> dict:
    """Decode the proven header fields.  Only the first 28 bytes are interpreted;
    the remaining 16 bytes of the 44-byte header are device-specific and kept raw."""
    if len(first) < 28:
        raise ValueError("header too short")
    version, sections, flag, resume, dversion, sector, rsize = struct.unpack(
        "<4s6I", first[:28]
    )
    return {
        "version": version.decode("ascii", "replace"),
        "version_hex": version.hex(),
        "sections": sections,
        "flag": flag,
        "resume_vector": resume,
        "dversion": dversion,
        "sector_size": sector,
        "r_data_size": rsize,
        "raw": first[:44].hex(),
    }


def classify(data: bytes) -> str:
    """'valid' | 'already-invalid' | 'rebuild-stage' | raises for unknown images."""
    if len(data) != SIZE:
        raise ValueError(f"wrong WBI length {len(data)} != {SIZE}")
    if data[:SECTOR] == bytes(SECTOR):
        return "already-invalid"
    version = data[:4]
    if version == b"WBI1":
        header = parse_header(data)
        if header["sections"] not in range(1, 4096):
            raise ValueError("implausible section count in WBI1 header")
        if header["sector_size"] != SECTOR:
            raise ValueError("unexpected sector size in WBI1 header")
        return "valid"
    if version in (b"WBI0", b"prof", b"invl"):
        return "rebuild-stage"
    raise ValueError(f"unknown WBI header {version!r}; refuse to write")


def validate_before(before: bytes, reference: bytes | None = None) -> dict:
    """Pre-write gate: size, header, and equality with the trusted reference."""
    state = classify(before)
    if state != "valid":
        raise ValueError(f"WBI is not a valid image ({state}); nothing to invalidate")
    if reference is not None:
        if len(reference) != SIZE:
            raise ValueError("reference backup has the wrong size")
        if reference != before:
            raise ValueError("current partition differs from the trusted backup; STOP")
    return {"state": state, "header": parse_header(before), "sha256": sha(before)}


def validate_after(before: bytes, after: bytes) -> dict:
    """Post-write gate: first 512 bytes zeroed, every other byte identical."""
    if len(before) != SIZE or len(after) != SIZE:
        raise ValueError("readback size mismatch; keep evidence, do not retry")
    if after[:SECTOR] != bytes(SECTOR):
        raise ValueError("first sector is not zero after the write; STOP")
    if after[SECTOR:] != before[SECTOR:]:
        offset = next(
            i for i in range(SECTOR, SIZE) if after[i] != before[i]
        )
        raise ValueError(f"write touched byte {offset}, outside the allowed sector; STOP")
    return {"changed_bytes": SECTOR, "sha256_before": sha(before), "sha256_after": sha(after)}


def tail_hash(data: bytes) -> str:
    """Hash of everything after the header sector (cheap identity check)."""
    return hashlib.sha256(data[SECTOR:]).hexdigest()


# --------------------------------------------------------------------------- #
# camera side
# --------------------------------------------------------------------------- #

def _read_partition(backend, label: str, progress=None) -> bytes:
    """Stream the whole partition through the backend's verified chunk reader."""
    buffer = bytearray()

    class Sink:
        def write(self, chunk: bytes) -> None:
            if len(buffer) + len(chunk) > SIZE:
                raise RuntimeError("read exceeded partition capacity")
            buffer.extend(chunk)
            if progress is not None:
                progress(len(buffer), SIZE)

    status, _ = backend.read_path(DEVICE, Sink())
    if status not in (0, 1):
        raise RuntimeError(f"partition read failed (status={status})")
    if len(buffer) != SIZE:
        raise RuntimeError(f"short partition read: {len(buffer)} of {SIZE} bytes")
    return bytes(buffer)


def _read_header(backend) -> bytes:
    target = backend.ram + "/wbi-head"
    try:
        backend.run(
            f"busybox dd if={DEVICE} of={target} bs=512 count=1 2>/dev/null"
        )
        _, data = backend.read_path(target)
    finally:
        backend.run(f"busybox rm -f {target}")
    if len(data) != SECTOR:
        raise RuntimeError(f"header read returned {len(data)} bytes")
    return data


def _load_tool(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) != TOOL_BYTES or sha(data) != TOOL_SHA256:
        raise ValueError("ud_wbi_drop.elf size/hash mismatch; refusing to run it")
    return data


def _check_config() -> bytes:
    if sha(CONFIG) != CONFIG_SHA256:
        raise RuntimeError("partition config hash mismatch; refusing to run the tool")
    return CONFIG


def _manifest_path(folder: Path, name: str) -> Path:
    return folder / name


def _write_manifest(folder: Path, name: str, payload: dict) -> Path:
    path = _manifest_path(folder, name)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #

def action_status(backend, args) -> int:
    header = _read_header(backend)
    print("=== WBI 头（前 512 字节） ===")
    print(f"  sha256(512B) = {sha(header)}")
    print(f"  raw[0:44]    = {header[:44].hex()}")
    try:
        parsed = parse_header(header)
        print(f"  version      = {parsed['version']!r}  -> {HEADER_VERSIONS.get(header[:4], 'unknown')}")
        print(f"  sections     = {parsed['sections']}")
        print(f"  sector_size  = {parsed['sector_size']}")
        print(f"  r_data_size  = {parsed['r_data_size']}")
        print(f"  resume_vector= 0x{parsed['resume_vector']:08x}")
    except ValueError as error:
        print(f"  （无法按 WBI 头解析：{error}）")
    script = (
        "echo __STATE__; cat /proc/ssboot/state 2>/dev/null; "
        "echo __BOOTMODE__; cat /proc/udm/upm_bootmode 2>/dev/null; "
        "echo __UPTIME__; busybox cat /proc/uptime; "
        "echo __WARMBOOT__; busybox dmesg 2>/dev/null | busybox grep -c WARMBOOT; "
        "echo __EVOFF__; busybox dmesg 2>/dev/null | busybox grep -c 'EV OFF Warm boot'; "
        "echo __POWERSTATE__; cat /sys/power/state 2>/dev/null; "
        "echo __IMAGE_MODE__; cat /sys/power/image_mode 2>/dev/null"
    )
    print("=== 运行态 ===")
    print(backend.run(script).rstrip())
    if args.wbi_dir:
        folder = Path(args.wbi_dir)
        print(f"备份目录: {folder} ({'存在' if folder.is_dir() else '不存在'})")
    return 0


def action_prepare(backend, args) -> int:
    folder = Path(args.wbi_dir)
    folder.mkdir(parents=True, exist_ok=True)
    reference_path = Path(args.baseline) if args.baseline else None
    reference = reference_path.read_bytes() if reference_path else None
    if reference is not None and len(reference) != SIZE:
        raise ValueError(f"baseline {reference_path} is not {SIZE} bytes")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    first_path = folder / f"wbi-before-{stamp}.bin"
    second_path = folder / f"wbi-before2-{stamp}.bin"

    print(f"[1/3] 读取分区（{SIZE} 字节）…")
    first = _read_partition(backend, "first")
    first_path.write_bytes(first)
    print(f"  已保存 {first_path}  sha256={sha(first)}")

    if reference is not None:
        if reference != first:
            raise ValueError("fresh read differs from --baseline; refusing to continue")
        print("  与给定 baseline 逐字节一致（跳过第二次全读）")
        second = None
        second_path = None
    else:
        print(f"[2/3] 第二次读取并比对…")
        second = _read_partition(backend, "second")
        second_path.write_bytes(second)
        if second != first:
            raise ValueError("double reads differ; refuse to write anything")

    info = validate_before(first, reference)
    print(f"[3/3] 头部 {info['header']['version']}，sections={info['header']['sections']}，指纹通过")

    manifest = {
        "action": "wbi-prepare",
        "write_attempted": False,
        "stamp": stamp,
        "read": {"path": str(first_path), "bytes": len(first), "sha256": sha(first)},
        "second_read": ({"path": str(second_path), "bytes": len(second), "sha256": sha(second)}
                        if second is not None else None),
        "reference": ({"path": str(reference_path), "bytes": len(reference), "sha256": sha(reference)}
                      if reference is not None else None),
        "match": second is None or second == first,
        "header": first[:44].hex(),
        "tail_sha256": tail_hash(first),
    }
    path = _write_manifest(folder, f"wbi-manifest-{stamp}.json", manifest)
    print(f"准备完成: {path}")
    print("下一步（需机主明确授权）: --backend jiritsu --wbi-drop --wbi-dir <同目录>")
    return 0


def action_drop(backend, args) -> int:
    folder = Path(args.wbi_dir)
    reference_path = Path(args.baseline) if args.baseline else None
    if not reference_path:
        candidates = sorted(folder.glob("wbi-before-*.bin")) if folder.is_dir() else []
        if not candidates:
            raise ValueError("no prepared backup found; run --wbi-prepare first")
        reference_path = candidates[-1]
    reference = reference_path.read_bytes()
    if len(reference) != SIZE:
        raise ValueError("prepared backup has the wrong size")

    print("[1/7] 写前重新读取整个分区…")
    before = _read_partition(backend, "pre-write")
    info = validate_before(before, reference)
    print(f"  指纹通过，头部 {info['header']['version']}，sha256={info['sha256']}")

    tool = _load_tool(Path(args.wbi_tool))
    config = _check_config()

    token = uuid.uuid4().hex
    remote = backend.ram + "/wbi-" + token
    after = None
    output = None
    validation_error = None
    execution_error = None
    filesystem_sync = None
    print(f"[2/7] 上传工具与分区配置 -> {remote}")
    backend.run(f"busybox mkdir -p '{remote}'")
    try:
        from zve10_jiritsu import upload_verified  # type: ignore

        upload_verified(backend.camera, remote + "/" + TOOL_NAME, tool)
        upload_verified(backend.camera, remote + "/partinf.conf", config)
        backend.run(f"busybox chmod 700 '{remote}/{TOOL_NAME}'")
        print("[3/7] 执行（写头 512 B）…")
        output = backend.run(f"cd '{remote}' && ./{TOOL_NAME} partinf.conf")
        print(f"  输出: {output.strip()}")
        if output.strip() != EXPECTED_OUTPUT:
            print("  [警告] 输出与预期不符；继续整分区回读以确认实际状态")
        print("[4/7] 文件同步…")
        sync_output = backend.run("busybox sync; echo __SYNC_RC__:$?")
        if "__SYNC_RC__:0" not in sync_output:
            raise RuntimeError(f"filesystem sync did not complete: {sync_output.strip()!r}")
        filesystem_sync = "ok"
        print("  sync 完成（工具本身以 O_RDWR|O_SYNC 打开分区，写入为同步写）")
        print("[5/7] 整分区回读校验…")
        after = _read_partition(backend, "post-write")
        try:
            result = validate_after(before, after)
            print(f"  仅前 {result['changed_bytes']} 字节变化，其余 {SIZE - SECTOR} 字节不变")
        except ValueError as error:
            validation_error = str(error)
    except Exception as error:  # keep evidence, then re-raise after recording
        execution_error = repr(error)
        raise
    finally:
        print("[6/7] 清理临时文件…")
        remote_cleanup = None
        try:
            backend.run(f"busybox rm -f '{remote}/{TOOL_NAME}' '{remote}/partinf.conf'")
            backend.run(f"busybox rmdir '{remote}'")
            remote_cleanup = "ok"
            print("  相机 RAM 临时文件已清理")
        except Exception as error:
            remote_cleanup = f"error: {error!r}"
            print(f"  [警告] RAM 临时文件清理失败: {error!r}")
        after_path = None
        after_saved = False
        if after is not None:
            after_path = folder / f"wbi-after-{token}.bin"
            try:
                after_path.write_bytes(after)
                after_saved = True
            except Exception as error:
                print(f"  [警告] 回读文件保存失败: {error!r}")
                after_path = None
        manifest = {
            "action": "wbi-drop",
            "write_attempted": True,
            "token": token,
            "reference": str(reference_path),
            "after": str(after_path) if after_path else None,
            "after_saved": after_saved,
            "before_sha256": sha(before),
            "after_sha256": sha(after) if after is not None else None,
            "changed_bytes": SECTOR if validation_error is None and after is not None else None,
            "write_verified": validation_error is None and after is not None,
            "readback_bytes": len(after) if after is not None else 0,
            "filesystem_sync": filesystem_sync,
            "remote_cleanup": remote_cleanup,
            "validation_error": validation_error,
            "execution_error": execution_error,
            "tool_sha256": TOOL_SHA256,
            "config_sha256": CONFIG_SHA256,
            "sony_output": (output or "").strip(),
        }
        try:
            path = _write_manifest(folder, f"wbi-drop-{token}.json", manifest)
            print(f"[7/7] 记录: {path}")
        except Exception as error:
            print(f"  [警告] 清单写入失败: {error!r}")

    if validation_error:
        raise ValueError(validation_error)
    print("=== WBI 写入验证 ===")
    print(f"  sony_output     = {(output or '').strip()}")
    print(f"  filesystem_sync = {filesystem_sync}")
    print(f"  readback_bytes  = {len(after)}")
    print(f"  changed_bytes   = {SECTOR}（仅头 512 B）")
    print(f"  write_verified  = True")
    print("写入完成且校验通过。下一步：正常关会话 -> 拔 USB -> 关机 -> 取电池 30 s -> 开机。")
    return 0


def action_verify(backend, args) -> int:
    """Post-cold-boot read-only acceptance: no WARMBOOT, plugin in im.elf."""
    script = (
        "echo __WARMBOOT__; busybox dmesg 2>/dev/null | busybox grep -c WARMBOOT; "
        "echo __STATE__; cat /proc/ssboot/state 2>/dev/null; "
        "echo __BOOTMODE__; cat /proc/udm/upm_bootmode 2>/dev/null; "
        "echo __UPTIME__; busybox cat /proc/uptime; "
        "echo __DMODE__; busybox cat /setting/mode/dmode 2>/dev/null; "
        "echo __PRELOAD__; busybox cat /setting/mode/preload 2>/dev/null; "
        "echo __PID__; busybox pidof im.elf; "
        "echo __MAPS__; busybox grep -c libzve10plugin /proc/$(busybox pidof im.elf)/maps 2>/dev/null; "
        "echo __LOG__; busybox tail -n 6 /tmp/zve10plugin.log 2>/dev/null; "
        "echo __END__"
    )
    print(backend.run(script).rstrip())
    header = _read_header(backend)
    print("=== WBI 头 ===")
    print(f"  raw[0:16] = {header[:16].hex()}  ({HEADER_VERSIONS.get(header[:4], 'all-zero / invalidated')})")
    return 0


ACTIONS = {
    "status": action_status,
    "prepare": action_prepare,
    "drop": action_drop,
    "verify": action_verify,
}


# --------------------------------------------------------------------------- #
# host-only self test (no camera, no USB)
# --------------------------------------------------------------------------- #

def selftest(tool_path: Path) -> int:
    real_header = bytes.fromhex(
        "5742493198020000090000001c4e120024020620000200000098210200000000"
        "00000000000f0f0f0f00402308000000000000000000000000"
    )[:44]
    before = real_header + bytes(SIZE - len(real_header))
    print("selftest: header ->", parse_header(before)["version"], classify(before))
    assert classify(before) == "valid"
    validate_before(before, before)

    after = bytes(SECTOR) + before[SECTOR:]
    result = validate_after(before, after)
    assert result["changed_bytes"] == SECTOR and result["sha256_after"] != result["sha256_before"]
    assert classify(after) == "already-invalid"

    for mutation, label in (
        (bytes(SECTOR) + b"\x01" + before[SECTOR + 1:], "byte 512 changed"),
        (bytes(SECTOR - 1) + b"\x01" + before[SECTOR:], "last header byte changed"),
    ):
        try:
            validate_after(before, mutation)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted a forbidden write: {label}")

    for bad, label in (
        (b"XXXX" + before[4:], "unknown version"),
        (b"WBI0" + before[4:], "rebuild-stage image"),
        (before[:-1], "short read"),
    ):
        try:
            validate_before(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted a bad pre-write state: {label}")

    try:
        validate_before(before, bytes(SIZE))
    except ValueError:
        pass
    else:
        raise AssertionError("accepted a partition that differs from the reference")

    if not tool_path.is_file():
        raise AssertionError(f"missing Sony tool: {tool_path}")
    _load_tool(tool_path)
    _check_config()
    print(f"selftest: tool {tool_path.name} size/hash OK")
    print("selftest: config hash OK")
    print("SELFTEST OK")
    return 0


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Guarded WBI invalidation (host self test only)")
    parser.add_argument("--selftest", action="store_true", required=True,
                        help="离线自检：验证校验逻辑与工具哈希，不连接相机")
    parser.add_argument("--tool", default=str(Path(__file__).with_name("payload") / TOOL_NAME))
    args = parser.parse_args(argv)
    return selftest(Path(args.tool))


if __name__ == "__main__":
    raise SystemExit(main())