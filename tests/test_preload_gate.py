"""Offline regression checks for the bootin.elf LD_PRELOAD gate and deployment.

逆向依据与门禁复现见 tools/zvp_preload_gate.py。
本文件不接触任何相机设备。
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import probe_zve10_runtime as probe
import zvp_preload_gate as gate


class GateModelTests(unittest.TestCase):
    def test_recommended_content_passes_and_matches_rule_three(self):
        content = gate.build_preload_value("/tmp/libzve10plugin.so")
        result = gate.simulate(content.encode())
        self.assertTrue(result.accepted)
        self.assertEqual(result.rule, 3)
        self.assertEqual(result.matched_prefix, "/usr/tool/libmcheckb.so")
        self.assertEqual(result.ld_preload, "LD_PRELOAD=" + content)

    def test_plain_plugin_path_is_rejected(self):
        # 这正是修复前工具写入的内容，也是 2.03 上 preload 失效的直接原因。
        result = gate.simulate(b"/tmp/libzve10plugin.so")
        self.assertFalse(result.accepted)
        self.assertIsNone(result.ld_preload)

    def test_trailing_newline_after_whitelisted_prefix_passes_gate_but_poisons_value(self):
        # strncmp 只比较白名单串的 23 字节，因此换行不会被门禁拦下，
        # 但换行会进入 LD_PRELOAD 值，使加载器打不开该文件而忽略它。
        result = gate.simulate(b"/usr/tool/libmcheckb.so:payload\n")
        self.assertTrue(result.accepted)
        self.assertTrue(result.ld_preload.endswith("payload\n"))
        # 没有白名单前缀时则直接被门禁拒绝
        self.assertFalse(gate.simulate(b"/tmp/libzve10plugin.so\n").accepted)

    def test_every_whitelisted_prefix_is_accepted(self):
        for known in gate.WHITELIST:
            with self.subTest(known=known):
                self.assertTrue(gate.simulate(known.encode()).accepted)

    def test_prefix_longer_than_known_string_is_fine(self):
        # strncmp 只比较白名单串长度，因此 .so.1 这类版本化文件名同样命中。
        self.assertTrue(gate.simulate(b"/usr/tool/libjemalloc_tsh.so.1").accepted)

    def test_build_rejects_content_over_read_cap(self):
        with self.assertRaises(ValueError):
            gate.build_preload_value("/tmp/" + "a" * 600)

    def test_simulate_truncates_at_498_bytes(self):
        # bootin 只 read() 0x1f2 = 498 字节；超长内容会被截断而不是被拒。
        long_body = b"/usr/tool/libmcheckb.so:" + b"x" * (gate.READ_MAX + 50)
        result = gate.simulate(long_body)
        self.assertTrue(result.accepted)
        self.assertTrue(result.overlong)
        self.assertEqual(len(result.content), gate.READ_MAX)

    def test_build_rejects_separators_in_plugin_path(self):
        for bad in ("/tmp/a:b.so", "/tmp/a b.so", "/tmp/a\n.so", "relative.so", ""):
            with self.subTest(path=bad):
                with self.assertRaises(ValueError):
                    gate.build_preload_value(bad)

    def test_build_rejects_decoy_outside_whitelist(self):
        with self.assertRaises(ValueError):
            gate.build_preload_value("/tmp/libzve10plugin.so", decoy="/tmp/decoy.so")


class DeploymentCommandTests(unittest.TestCase):
    # jiritsu backend: command() 只接受 ASCII，且拒绝含 exit/exec/kill/trap/return 的命令
    JIRITSU_FORBIDDEN = re.compile(r"\b(exit|exec|kill|trap|return)\b")

    def assert_jiritsu_safe(self, shell):
        self.assertTrue(shell.isascii(), f"command must be ASCII: {shell!r}")
        self.assertIsNone(self.JIRITSU_FORBIDDEN.search(shell),
                          f"forbidden word in command: {shell!r}")

    def test_generated_shell_commands_are_jiritsu_safe(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        shells = []
        with patch.object(probe, "run_cam", return_value=completed) as command:
            probe.cmd_enable_preload()
            probe.cmd_preload_status()
            probe.cmd_reset_boot_guard()
            probe.cmd_set_mode("10")
            probe.cmd_set_mode("p", "/usr/tool/libmcheckb.so")
            probe.cmd_write_dmode("3pd", "/usr/tool/libmcheckb.so")
            probe.cmd_write_env_txt("ZVP_BOOT_TEST=1")
            probe.cmd_check()
            shells = [call.args[0][1] for call in command.call_args_list
                      if call.args[0][0] == "sh"]
        self.assertGreaterEqual(len(shells), 4)
        for shell in shells:
            self.assert_jiritsu_safe(shell)

    def run_enable(self, returncode=0):
        completed = subprocess.CompletedProcess([], returncode, "dmode=3p\npreload=...", "")
        with patch.object(probe, "run_cam", return_value=completed) as command:
            code = probe.cmd_enable_preload()
        return code, command

    def test_enable_preload_writes_gate_compliant_content(self):
        code, command = self.run_enable()
        self.assertEqual(code, 0)
        shell = command.call_args.args[0][1]
        expected = gate.build_preload_value(probe.PLUGIN_PRELOAD_PATH)
        self.assertIn(expected, shell)
        self.assertIn("change_mode.sh p", shell)
        # dmode 与 preload 都必须回读核对
        self.assertIn('echo "dmode=$D"', shell)
        self.assertIn('[ "$P" = "$PRELOAD" ]', shell)

    def test_enable_preload_never_prepends_a_real_factory_library(self):
        # 只允许使用保证不存在的占位项；否则会额外加载索尼调试库。
        _, command = self.run_enable()
        shell = command.call_args.args[0][1]
        self.assertNotIn("/usr/tool/LeakTracer.so", shell)
        self.assertNotIn("libjemalloc_tsh.so.1", shell)
        self.assertNotIn("if [ -r /usr/tool/libmcheckb.so ]", shell)

    def test_enable_preload_propagates_remote_failure(self):
        code, _ = self.run_enable(returncode=3)
        self.assertEqual(code, 3)

    def test_preload_status_is_read_only(self):
        completed = subprocess.CompletedProcess([], 0, "IM_PID=149", "")
        with patch.object(probe, "run_cam", return_value=completed) as command:
            code = probe.cmd_preload_status()
        self.assertEqual(code, 0)
        shell = command.call_args.args[0][1]
        for forbidden in ("change_mode.sh", "rm ", "mount ", "> /", "echo 3p"):
            self.assertNotIn(forbidden, shell)
        for required in ("/setting/mode/dmode", "/setting/mode/preload",
                         "/setting/ulogio.bin", "/proc/$PID/maps", "/usr/tool",
                         "/tmp/zvp_boot_guard"):
            self.assertIn(required, shell)

    def test_preload_status_decodes_boot_guard_state(self):
        hexline = ("GUARD_HEX_BEGIN\n"
                   "4742565a010000000100000002000000\n"
                   "GUARD_HEX_END\n")
        completed = subprocess.CompletedProcess([], 0, f"IM_PID=1\n{hexline}", "")
        with patch.object(probe, "run_cam", return_value=completed):
            with patch("builtins.print") as output:
                code = probe.cmd_preload_status()
        self.assertEqual(code, 0)
        text = "\n".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("fail_count=1", text)
        self.assertIn("trip_count=2", text)
        self.assertIn("--reset-boot-guard", text)

    def test_preload_status_decodes_ld_preload_from_environ_hex(self):
        raw = b"HOME=/\0LD_PRELOAD=/usr/tool/libmcheckb.so.zvp-gate-decoy:/tmp/libzve10plugin.so\0PATH=/bin"
        block = "ENVIRON_HEX_BEGIN\n" + raw.hex() + "\nENVIRON_HEX_END\n"
        completed = subprocess.CompletedProcess([], 0, f"IM_PID=1\n{block}", "")
        with patch.object(probe, "run_cam", return_value=completed):
            with patch("builtins.print") as output:
                probe.cmd_preload_status()
        text = "\n".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("LD_PRELOAD=/usr/tool/libmcheckb.so.zvp-gate-decoy:/tmp/libzve10plugin.so", text)

    def test_preload_status_scans_raw_ulogio_for_fingerprint(self):
        raw = (b"\x00\x01bootin: LD_PRELOAD=/usr/tool/libmcheckb.so.zvp-gate-decoy:"
               b"/tmp/libzve10plugin.so\x00tail")
        block = "ULOGIO_HEX_BEGIN\n" + raw.hex() + "\nULOGIO_HEX_END\n"
        completed = subprocess.CompletedProcess([], 0, f"IM_PID=1\n{block}", "")
        with patch.object(probe, "run_cam", return_value=completed):
            with patch("builtins.print") as output:
                probe.cmd_preload_status()
        text = "\n".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("[ulogio] 相关字符串", text)
        self.assertIn("LD_PRELOAD=/usr/tool/libmcheckb.so.zvp-gate-decoy:/tmp/libzve10plugin.so", text)

    def test_preload_status_uses_only_camera_busybox_applets(self):
        completed = subprocess.CompletedProcess([], 0, "IM_PID=1\n", "")
        with patch.object(probe, "run_cam", return_value=completed) as command:
            probe.cmd_preload_status()
        shell = command.call_args.args[0][1]
        # 相机没有 tr/od，也没有 PATH 符号链接；二进制字段必须走 busybox xxd。
        self.assertNotIn(" tr ", shell)
        self.assertNotIn(" od ", shell)
        self.assertIn("busybox xxd -p", shell)
        self.assertIn("busybox tail", shell)

    def test_reset_boot_guard_removes_only_runtime_state(self):
        completed = subprocess.CompletedProcess([], 0, "GUARD_RESET_OK", "")
        with patch.object(probe, "run_cam", return_value=completed) as command:
            code = probe.cmd_reset_boot_guard()
        self.assertEqual(code, 0)
        shell = command.call_args.args[0][1]
        self.assertIn("rm -f /tmp/zvp_boot_guard", shell)
        self.assertNotIn("/setting", shell)


if __name__ == "__main__":
    unittest.main()