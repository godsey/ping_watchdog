import os
import sys
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_DIR = os.path.join(REPO_ROOT, "usr", "local", "bin")
sys.path.insert(0, SCRIPT_DIR)

import ping_watchdog

class TestPingWatchdog(unittest.TestCase):
    def test_parse_bool(self):
        self.assertTrue(ping_watchdog.parse_bool("1"))
        self.assertTrue(ping_watchdog.parse_bool("true"))
        self.assertTrue(ping_watchdog.parse_bool("YES"))
        self.assertTrue(ping_watchdog.parse_bool("on"))
        self.assertFalse(ping_watchdog.parse_bool("0"))
        self.assertFalse(ping_watchdog.parse_bool("false"))
        self.assertFalse(ping_watchdog.parse_bool("no"))
        self.assertFalse(ping_watchdog.parse_bool("off"))
        self.assertFalse(ping_watchdog.parse_bool(""))

    def test_vm_config_from_lines(self):
        lines = [
            "agent: 1",
            "onboot: 1",
            "startup: order=10,up=60",
            "lock: backup"
        ]
        config = ping_watchdog.VMConfig.from_lines(lines)
        self.assertTrue(config.agent_enabled)
        self.assertTrue(config.onboot)
        self.assertEqual(config.startup_order, 10)
        self.assertEqual(config.lock_value, "backup")

    def test_vm_config_agent_variants(self):
        self.assertTrue(ping_watchdog.VMConfig.from_lines(["agent: enabled=1"]).agent_enabled)
        self.assertTrue(ping_watchdog.VMConfig.from_lines(["agent: 1,fstrim_cloned_disks=1"]).agent_enabled)
        self.assertFalse(ping_watchdog.VMConfig.from_lines(["agent: 0"]).agent_enabled)
        self.assertFalse(ping_watchdog.VMConfig.from_lines(["agent: enabled=0"]).agent_enabled)

    def test_parse_qm_list(self):
        mock_stdout = "      VMID NAME                 STATUS            MEM(MB)    BOOTDISK(GB) PID       \n       100 vm1                  running           2048              32.00 1234      \n       101 vm2                  stopped           1024              16.00 0         "
        watchdog = ping_watchdog.Watchdog({})
        with patch.object(watchdog, 'run_cmd') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_stdout)
            vms = watchdog.parse_qm_list()
            self.assertEqual(len(vms), 2)
            self.assertEqual(vms[0].vmid, "100")
            self.assertEqual(vms[0].status, "running")
            self.assertEqual(vms[1].vmid, "101")
            self.assertEqual(vms[1].status, "stopped")

if __name__ == '__main__':
    unittest.main()
