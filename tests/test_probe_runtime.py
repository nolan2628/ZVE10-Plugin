"""Offline regression checks for deployment guards and Jiritsu staging."""
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import probe_zve10_runtime as probe
from zve10_jiritsu_runtime import Runtime, find_device, read_plugin_log


class DeploymentTests(unittest.TestCase):
    def test_missing_log_is_a_failed_startup_check(self):
        reader = Mock(side_effect=RuntimeError("GET_FILE status 0x10000006"))
        self.assertEqual(read_plugin_log(Mock(), reader), 1)

    def test_empty_log_is_a_failed_startup_check(self):
        self.assertEqual(read_plugin_log(Mock(), Mock(return_value=b"")), 1)

    def test_usb_error_is_not_reported_as_missing_log(self):
        with self.assertRaisesRegex(RuntimeError, "USB stalled"):
            read_plugin_log(Mock(), Mock(side_effect=RuntimeError("USB stalled")))

    def test_failed_canvas_is_not_successful_initialization(self):
        data = "zve10plugin UI主线程激活成功: tid=157 c1_key=0x150102a0 canvas=0 sounds=2".encode()
        self.assertEqual(read_plugin_log(Mock(), Mock(return_value=data)), 1)

    def test_initialized_canvas_and_ui_pass(self):
        data = ("GEN_CacheInsImg 640x480 RGBA4444 画布初始化完成\n"
                "zve10plugin UI主线程激活成功: tid=157 c1_key=0x150102a0 canvas=1 sounds=2\n").encode()
        self.assertEqual(read_plugin_log(Mock(), Mock(return_value=data)), 0)

    def check_result(self, output):
        with patch.object(probe, "run_cam", side_effect=[
            subprocess.CompletedProcess([], 0, "connected", ""),
            subprocess.CompletedProcess([], 0, "IM_PID=123", ""),
            subprocess.CompletedProcess([], 0, output, ""),
        ]):
            return probe.cmd_check()

    def test_baseline_requires_both_exact_values(self):
        self.assertEqual(self.check_result("NETSERVICE_BYTES= 90 b5 95 b0\nCAUTION_BYTES=20\n"), 0)
        self.assertEqual(self.check_result("NETSERVICE_BYTES=90 b5 95 b0\nCAUTION_BYTES=1b\n"), 1)
        self.assertEqual(self.check_result("NETSERVICE_BYTES=90 b5 95 b0\n"), 1)

    def test_failed_unlink_stops_upload(self):
        with tempfile.NamedTemporaryFile() as source:
            with patch.object(probe, "run_cam", return_value=
                              subprocess.CompletedProcess([], 1, "unlink failed", "")) as command:
                self.assertEqual(probe.cmd_stage_tmp(source.name), 1)
                self.assertEqual(command.call_count, 1)

    def test_jiritsu_upload_verifies_every_chunk_and_whole_file(self):
        data = b"A" * 65536 + b"tail"
        backend = Mock(ram="/tmp_network/zve10-test")
        backend.run.return_value = hashlib.sha256(data).hexdigest() + "  remote\n"
        upload = Mock()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "plugin.so"
            source.write_bytes(data)
            self.assertEqual(Runtime(backend, upload).put(str(source), "/tmp/libzve10plugin.so").returncode, 0)
        self.assertEqual([call.args[2] for call in upload.call_args_list], [data[:65536], data[65536:]])
        self.assertTrue(any("&& mv " in call.args[0] for call in backend.run.call_args_list))

    def test_hash_mismatch_never_publishes_plugin(self):
        backend = Mock(ram="/tmp_network/zve10-test")
        backend.run.return_value = "wrong-hash remote"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "plugin.so"
            source.write_bytes(b"plugin")
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                Runtime(backend, Mock()).put(str(source), "/tmp/libzve10plugin.so")
        self.assertFalse(any("&& mv " in call.args[0] for call in backend.run.call_args_list))
        self.assertTrue(backend.run.call_args.args[0].startswith("rm -f "))

    def test_jiritsu_baseline_uses_binary_reads(self):
        backend = Mock(ram="/tmp_network/zve10-test")
        backend.read_path.side_effect = [(0, bytes.fromhex("90 b5 95 b0")), (0, b"\x20")]
        result = Runtime(backend, Mock()).read_firmware_bytes()
        self.assertEqual(result.stdout, "NETSERVICE_BYTES=90 b5 95 b0\nCAUTION_BYTES=20\n")

    def test_short_baseline_read_is_rejected(self):
        backend = Mock(ram="/tmp_network/zve10-test")
        backend.read_path.return_value = (0, b"")
        with self.assertRaisesRegex(RuntimeError, "Short firmware baseline"):
            Runtime(backend, Mock()).read_firmware_bytes()

    def test_software_reboot_follows_loader_cleanup(self):
        backend = Mock()
        Runtime(backend, Mock()).reboot()
        self.assertEqual(backend.method_calls[0][0], "close")
        self.assertIn("reboot", backend.method_calls[1].args[0])
        self.assertFalse(backend.started)

    def test_cleanup_failure_prevents_reboot(self):
        backend = Mock()
        backend.close.side_effect = RuntimeError("cleanup failed")
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            Runtime(backend, Mock()).reboot()
        backend.run.assert_not_called()


@unittest.skipUnless(os.name == "nt" and importlib.util.find_spec("usb") and
                     importlib.util.find_spec("libusb_package"), "Windows USB dependencies required")
class DriverTests(unittest.TestCase):
    def test_service_device_can_fall_back_to_libusb_win32(self):
        unsupported = Mock()
        unsupported.ctrl_transfer.side_effect = NotImplementedError()
        supported = Mock()
        with patch("usb.backend.libusb1.get_backend", return_value=Mock()), \
             patch("usb.backend.libusb0.get_backend", return_value=Mock()), \
             patch("usb.core.find", side_effect=[unsupported, supported]), \
             patch("usb.util.dispose_resources") as dispose, \
             patch("zve10_jiritsu_runtime.Path.is_file", return_value=True):
            self.assertIs(find_device(0x0336), supported)
            dispose.assert_called_once_with(unsupported)

    def test_transport_timeout_is_not_retried_with_another_driver(self):
        device = Mock()
        device.ctrl_transfer.side_effect = TimeoutError("USB stalled")
        with patch("usb.backend.libusb1.get_backend", return_value=Mock()), \
             patch("usb.backend.libusb0.get_backend", return_value=Mock()), \
             patch("usb.core.find", return_value=device) as find, \
             patch("zve10_jiritsu_runtime.Path.is_file", return_value=True):
            with self.assertRaises(TimeoutError):
                find_device(0x0336)
            self.assertEqual(find.call_count, 1)


if __name__ == "__main__":
    unittest.main()
