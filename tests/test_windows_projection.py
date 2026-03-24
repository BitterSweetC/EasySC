import unittest
from unittest import mock

from app.windows_projection import (
    DISPLAY_SWITCH_ARGS,
    collect_projection_diagnostics,
    get_display_switch_arg,
    parse_dxdiag_output,
    parse_netsh_output,
)


NETSH_SAMPLE = """
Interface name: WLAN

    Driver information for this interface is not available.
    Wireless Display Supported: Yes (Graphics Driver: Yes, Wi-Fi Driver: Yes)
"""

DXDIAG_SAMPLE = """
------------------
Display Devices
------------------
           Miracast: Available, with HDCP
           Miracast: Not Supported by Graphics driver
"""


class WindowsProjectionTests(unittest.TestCase):
    def test_parse_netsh_output(self) -> None:
        info = parse_netsh_output(NETSH_SAMPLE)
        self.assertEqual(info["wlan_interface"], "WLAN")
        self.assertTrue(info["wireless_display_supported"])
        self.assertTrue(info["graphics_driver_supported"])
        self.assertTrue(info["wifi_driver_supported"])

    def test_parse_dxdiag_output(self) -> None:
        info = parse_dxdiag_output(DXDIAG_SAMPLE)
        self.assertTrue(info["miracast_available"])
        self.assertTrue(info["hdcp_supported"])
        self.assertIn("Available", info["miracast_summary"])

    def test_get_display_switch_arg(self) -> None:
        self.assertEqual(get_display_switch_arg("clone"), DISPLAY_SWITCH_ARGS["clone"])
        with self.assertRaises(ValueError):
            get_display_switch_arg("invalid")

    @mock.patch("app.windows_projection._run_command")
    def test_collect_projection_diagnostics(self, run_command: mock.Mock) -> None:
        def fake_run(command: list[str], encoding=None) -> str:
            if command[:3] == ["netsh", "wlan", "show"]:
                return NETSH_SAMPLE
            if command and command[0] == "dxdiag":
                from pathlib import Path
                path = Path(command[-1])
                path.write_text(DXDIAG_SAMPLE, encoding="utf-8")
                return ""
            return ""

        run_command.side_effect = fake_run
        diagnostics = collect_projection_diagnostics()
        self.assertTrue(diagnostics.wireless_display_supported)
        self.assertTrue(diagnostics.miracast_available)
        self.assertTrue(any("Win+K" in item for item in diagnostics.warnings))


if __name__ == "__main__":
    unittest.main()
