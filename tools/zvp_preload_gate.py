"""bootin.elf 的 LD_PRELOAD 门禁模型（离线、可单测）。

逆向依据（ZV-E10 2.03 原厂 `/usr/bin/bootin.elf`,
sha256=eedb96c6d7c8d5859f0c4837925e759e2d2a7ba5439985a72a7afbceffef19cd）:

  * `dmode` 中的 `p`/`P` 置位 g_state.flags 的 bit 0x40
    （FUN_00009448 @ 0x94a0–0x94a8）。
  * 只有该位为 1 才会处理 preload（FUN_0000991c @ 0x9990: `tst r3,#0x40`）。
  * 读取 `/setting/mode/preload`（路径写死，O_RDONLY），最多 0x1f2 = 498 字节。
  * 缓冲区前缀为 `LD_PRELOAD=`（11 字节），NUL 写在 min(11+已读字节, 0x1ff)。
  * 文件内容必须**以 4 条硬编码路径之一为前缀**
    （0x99d0–0x99f0，字符串表 @ 0xa8e0），否则只写日志、**不设置** LD_PRELOAD。

设备加载器行为（glibc 2.11.2，基于固件内 ARM 二进制实测）:
  无法加载的 preload 项（缺失 / 非 ELF）只会打印
  `ERROR: ld.so: object '...' from LD_PRELOAD cannot be preloaded: ignored.`
  **程序继续启动**。

因此本模块提供的构造方式是：用一个"前缀命中白名单、但保证不存在"的占位项
(GATE_DECOY) 作为第一段，再用 `:` 接上真正的插件路径。这样既能通过 bootin 的
字符串前缀门禁，又不会额外加载任何索尼调试库；占位项失败被加载器忽略，
那条 `ignored` 警告同时成为"LD_PRELOAD 确实生效"的正向证据。
"""
from __future__ import annotations

from dataclasses import dataclass

# --- 来自 bootin.elf 的硬事实 -------------------------------------------------
BOOTIN_ELF_SHA256_FW203 = (
    "eedb96c6d7c8d5859f0c4837925e759e2d2a7ba5439985a72a7afbceffef19cd"
)
WHITELIST = (
    "/usr/tool/LeakTracer.so",
    "/usr/tool/libefence.so",
    "/usr/tool/libjemalloc_tsh.so",
    "/usr/tool/libmcheckb.so",
)
DMODE_FILE = "/setting/mode/dmode"
PRELOAD_FILE = "/setting/mode/preload"
DMODE_PRELOAD_VALUE = "3p"          # '3' = app 阶段, 'p' = preload 位
LD_PRELOAD_PREFIX = b"LD_PRELOAD="
READ_MAX = 0x1F2                     # 498
NUL_CAP = 0x1FF
LD_PRELOAD_FLAG = 0x40

# 占位项：以白名单 #3 为前缀（strncmp 只比较 23 字节），但带不可存在的后缀。
GATE_DECOY = "/usr/tool/libmcheckb.so.zvp-gate-decoy"

DEFAULT_PLUGIN_PATH = "/tmp/libzve10plugin.so"

_FORBIDDEN_IN_PATH = set(":\x00 \t\r\n")


@dataclass(frozen=True)
class GateResult:
    accepted: bool
    rule: int | None
    ld_preload: str | None
    content: bytes
    overlong: bool

    @property
    def matched_prefix(self) -> str | None:
        return WHITELIST[self.rule] if self.rule is not None and self.rule < len(WHITELIST) else None


def match_whitelist(content: bytes) -> int | None:
    """完全按 bootin 的方式匹配：content 必须以某条白名单字符串为前缀。"""
    for index, known in enumerate(WHITELIST):
        known_b = known.encode("ascii")
        if content[: len(known_b)] == known_b:
            return index
    return None


def simulate(content: bytes) -> GateResult:
    """逐字节复现 FUN_0000991c 的 preload 分支。"""
    overlong = len(content) > READ_MAX
    body = content[:READ_MAX]
    rule = match_whitelist(body)
    if rule is None:
        return GateResult(False, None, None, body, overlong)
    total = len(body) + len(LD_PRELOAD_PREFIX)
    nul_at = NUL_CAP if total > 0x1FE else total
    value = (LD_PRELOAD_PREFIX + body)[:nul_at].decode("ascii", "backslashreplace")
    return GateResult(True, rule, value, body, overlong)


def _validate_plugin_path(plugin_path: str) -> None:
    if not plugin_path:
        raise ValueError("插件路径不能为空")
    if not plugin_path.startswith("/"):
        raise ValueError(f"插件路径必须是绝对路径: {plugin_path!r}")
    bad = sorted(_FORBIDDEN_IN_PATH.intersection(plugin_path))
    if bad:
        shown = ", ".join(repr(c) for c in bad)
        raise ValueError(
            f"插件路径不能包含 {shown}：ld.so 以 ':' 和空格分隔 LD_PRELOAD，"
            "change_mode.sh 也不做引号保护"
        )


def build_preload_value(
    plugin_path: str = DEFAULT_PLUGIN_PATH,
    decoy: str = GATE_DECOY,
) -> str:
    """构造要写入 /setting/mode/preload 的内容（不含换行）。

    `decoy` 必须是白名单前缀之一，且应指向一个保证不存在的路径。
    """
    _validate_plugin_path(plugin_path)
    if match_whitelist(decoy.encode("ascii")) is None:
        raise ValueError(f"占位项不满足白名单前缀: {decoy!r}")
    content = f"{decoy}:{plugin_path}"
    encoded = content.encode("ascii")
    if len(encoded) > READ_MAX:
        raise ValueError(
            f"preload 内容 {len(encoded)} 字节超过 bootin 的 {READ_MAX} 字节上限"
        )
    result = simulate(encoded)
    if not result.accepted:
        raise ValueError(f"构造出的内容不能通过门禁: {content!r}")
    return content


def is_accepted(content: str | bytes) -> bool:
    data = content.encode("ascii") if isinstance(content, str) else content
    return simulate(data).accepted


def _main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--content", help="检查指定内容能否通过门禁")
    ap.add_argument("--build", nargs="?", const=DEFAULT_PLUGIN_PATH,
                    metavar="PLUGIN_PATH", help="构造推荐的 preload 内容")
    ap.add_argument("--decoy", default=GATE_DECOY, help="占位项路径")
    args = ap.parse_args(argv)

    if args.build is not None:
        value = build_preload_value(args.build, args.decoy)
        res = simulate(value.encode("ascii"))
        print(f"preload 内容 : {value}")
        print(f"门禁         : 通过（规则 #{res.rule} = {res.matched_prefix}）")
        print(f"LD_PRELOAD   : {res.ld_preload}")
        print(f"字节数       : {len(value)} / {READ_MAX}")
        return 0

    if args.content is not None:
        data = args.content.encode("ascii")
        res = simulate(data)
        if res.accepted:
            print(f"通过：规则 #{res.rule} = {res.matched_prefix}")
            print(f"LD_PRELOAD = {res.ld_preload}")
            return 0
        print("拒绝：内容不以任何白名单路径为前缀 → bootin 不会设置 LD_PRELOAD")
        return 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())