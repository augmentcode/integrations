"""Offline NAT tests: no AWS, root access, or live firewall operations."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "configure_nat", Path(__file__).with_name("configure-nat.py")
)
nat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nat)


def fixture():
    return {
        "networking_mode": "system-nat-v1",
        "worker_subnet_cidr": "172.31.128.0/24",
        "vpc_cidrs": ["172.31.0.0/16"],
        "system_private_ip": "172.31.1.10",
    }


class NatTests(unittest.TestCase):
    def test_invalid_mode_network_and_placement_fail_before_commands(self):
        for change in (
            {"networking_mode": "public-ip"},
            {"worker_subnet_cidr": "0.0.0.0/0"},
            {"worker_subnet_cidr": "172.31.128.1/24"},
            {"worker_subnet_cidr": "fd00::/64"},
            {"worker_subnet_cidr": "192.168.1.0/24"},
            {"vpc_cidrs": []},
            {"system_private_ip": "172.31.128.5"},
            {"system_private_ip": "192.168.1.5"},
        ):
            with self.subTest(change=change), patch.object(nat, "command") as command:
                cfg = {**fixture(), **change}
                with self.assertRaises(ValueError):
                    nat.apply_rules(cfg, "ens5")
                with self.assertRaises(ValueError):
                    nat.install(cfg)
                command.assert_not_called()

    def test_interface_discovery_requires_single_route_and_matching_private_ip(self):
        route = [{"dev": "ens5"}]
        addresses = [{"addr_info": [{"local": "172.31.1.10"}]}]
        with patch.object(
            nat,
            "command",
            side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(route).encode()),
                subprocess.CompletedProcess([], 0, json.dumps(addresses).encode()),
            ],
        ):
            self.assertEqual(nat.default_interface(fixture()), "ens5")
        for invalid in (
            [],
            [{"dev": "ens5"}, {"dev": "ens6"}],
            [{"dev": "lo"}],
            [{"dev": "ens5;bad"}],
            [{"dev": "ens5", "multipath": []}],
        ):
            with (
                self.subTest(route=invalid),
                patch.object(
                    nat,
                    "command",
                    return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps(invalid).encode()
                    ),
                ),
            ):
                with self.assertRaises(ValueError):
                    nat.default_interface(fixture())
        with patch.object(
            nat,
            "command",
            side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(route).encode()),
                subprocess.CompletedProcess([], 0, b"[]"),
            ],
        ):
            with self.assertRaisesRegex(ValueError, "own system IP"):
                nat.default_interface(fixture())

    def test_forwarding_is_worker_web_egress_and_established_return_only(self):
        forward, masquerade = nat.rules(fixture(), "ens5")
        self.assertEqual(
            forward[:4],
            ("filter", "COSMOS_FWD", "FORWARD", ["-i", "ens5", "-o", "ens5"]),
        )
        entries = forward[4]
        self.assertEqual(
            entries[0],
            [
                "-d",
                "172.31.128.0/24",
                "-m",
                "conntrack",
                "--ctstate",
                "ESTABLISHED,RELATED",
                "-j",
                "ACCEPT",
            ],
        )
        accept = entries[-2]
        self.assertIn("172.31.128.0/24", accept)
        self.assertIn("80,443", accept)
        self.assertIn("NEW,ESTABLISHED", accept)
        self.assertEqual(entries[-1], ["-j", "DROP"])
        for cidr in (
            "169.254.0.0/16",
            "127.0.0.0/8",
            "224.0.0.0/4",
            "10.43.0.0/16",
            "172.31.0.0/16",
        ):
            self.assertIn(["-d", cidr, "-j", "DROP"], entries[1:-2])
        self.assertEqual(masquerade[3], ["-s", "172.31.128.0/24", "-o", "ens5"])
        for cidr in ("172.31.0.0/16", "10.42.0.0/16", "169.254.0.0/16"):
            self.assertIn(["-d", cidr, "-j", "RETURN"], masquerade[4][:-1])
        self.assertEqual(masquerade[4][-1][-2:], ["-j", "MASQUERADE"])
        self.assertIn("80,443", masquerade[4][-1])

    def test_repeated_apply_keeps_single_jumps_and_never_flushes_parent_chains(self):
        hooks = set()
        calls = []

        def command(args, **kwargs):
            calls.append((args, kwargs))
            code = 0
            if args[0] == "iptables" and "-C" in args:
                code = 0 if tuple(args[args.index("-C") + 1 :]) in hooks else 1
            if args[0] == "iptables" and "-I" in args:
                offset = args.index("-I")
                hooks.add(tuple([args[offset + 1]] + args[offset + 3 :]))
            return subprocess.CompletedProcess(args, code)

        with patch.object(nat, "command", side_effect=command):
            nat.apply_rules(fixture(), "ens5")
            nat.apply_rules(fixture(), "ens5")
        self.assertEqual(len(hooks), 2)
        self.assertEqual(sum("-I" in args for args, _ in calls), 2)
        restores = [
            (args, kwargs) for args, kwargs in calls if args[0] == "iptables-restore"
        ]
        self.assertEqual(len(restores), 4)
        for args, kwargs in restores:
            self.assertIn("--noflush", args)
            self.assertIn("--wait", args)
            text = kwargs["data"].decode()
            self.assertNotIn("-F FORWARD", text)
            self.assertNotIn("-F POSTROUTING", text)
            self.assertNotIn("KUBE", text)
            self.assertTrue(text.endswith("COMMIT\n"))
            self.assertEqual(
                len([line for line in text.splitlines() if line.startswith("-F ")]), 1
            )
            self.assertTrue(
                next(
                    line for line in text.splitlines() if line.startswith("-F ")
                ).startswith("-F COSMOS_")
            )

    def test_only_expected_missing_rule_status_is_tolerated(self):
        with patch.object(
            nat.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)
        ):
            self.assertEqual(
                nat.command(["iptables", "-C"], missing_ok=True).returncode, 1
            )
            with self.assertRaises(RuntimeError):
                nat.command(["iptables-restore"])
        with patch.object(
            nat.subprocess, "run", return_value=subprocess.CompletedProcess([], 2)
        ):
            with self.assertRaises(RuntimeError):
                nat.command(["iptables", "-C"], missing_ok=True)

    def test_install_persists_unit_and_sysctl_and_rejects_unmanaged_files(self):
        with tempfile.TemporaryDirectory() as directory:
            unit, sysctl = Path(directory) / "nat.service", Path(directory) / "nat.conf"
            with (
                patch.object(nat, "UNIT", unit),
                patch.object(nat, "SYSCTL", sysctl),
                patch.object(nat, "default_interface", return_value="ens5"),
                patch.object(nat, "command") as command,
            ):
                unit.write_text("unmanaged\n")
                with self.assertRaisesRegex(ValueError, "unmanaged"):
                    nat.install(fixture())
                self.assertFalse(sysctl.exists())
                command.assert_not_called()
                unit.write_text(nat.MARKER)
                nat.install(fixture())
                nat.install(fixture())
                self.assertIn("Before=k3s.service", unit.read_text())
                self.assertIn("After=network-online.target", unit.read_text())
                self.assertIn(
                    "/usr/bin/python3 /opt/cosmos-bootstrap/configure-nat.py --apply",
                    unit.read_text(),
                )
                self.assertIn("WantedBy=multi-user.target", unit.read_text())
                self.assertIn("net.ipv4.ip_forward=1", sysctl.read_text())
                self.assertIn("net.ipv4.conf.ens5.send_redirects=0", sysctl.read_text())
                self.assertEqual(unit.stat().st_mode & 0o777, 0o644)
                self.assertTrue(
                    any(
                        call.args[0] == ["systemctl", "restart", "cosmos-nat.service"]
                        for call in command.call_args_list
                    )
                )


if __name__ == "__main__":
    unittest.main()
