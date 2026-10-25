#!/usr/bin/python3
"""Scoped, persistent HTTP/HTTPS NAT for private workers; never flush k3s chains."""

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path("/opt/cosmos-bootstrap")
MARKER = "# Managed by cosmos-bootstrap system-nat-v1\n"
SYSCTL = Path("/etc/sysctl.d/98-cosmos-nat.conf")
UNIT = Path("/etc/systemd/system/cosmos-nat.service")
BLOCKED = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "10.42.0.0/16",
    "10.43.0.0/16",
)


def validate_config(cfg):
    if cfg.get("networking_mode") != "system-nat-v1":
        raise ValueError("unsupported networking mode")
    worker = ipaddress.ip_network(cfg["worker_subnet_cidr"], strict=True)
    private = [
        ipaddress.ip_network(cidr)
        for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ]
    if (
        worker.version != 4
        or not 16 <= worker.prefixlen <= 28
        or not any(worker.subnet_of(network) for network in private)
    ):
        raise ValueError("invalid worker network")
    cidrs = cfg["vpc_cidrs"]
    if not isinstance(cidrs, list) or not cidrs:
        raise ValueError("missing VPC CIDRs")
    networks = [ipaddress.ip_network(cidr, strict=True) for cidr in cidrs]
    address = ipaddress.IPv4Address(cfg["system_private_ip"])
    if (
        any(network.version != 4 for network in networks)
        or not any(worker.subnet_of(network) for network in networks)
        or not any(address in network for network in networks)
        or address in worker
    ):
        raise ValueError("invalid NAT placement")
    return str(worker)


def command(args, missing_ok=False, capture=False, data=None):
    result = subprocess.run(
        args,
        check=False,
        timeout=60,
        input=data,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode and not (missing_ok and result.returncode == 1):
        raise RuntimeError("NAT command failed")
    return result


def default_interface(cfg):
    routes = json.loads(
        command(["ip", "-j", "-4", "route", "show", "default"], capture=True).stdout
    )
    if len(routes) != 1 or "multipath" in routes[0] or "nexthops" in routes[0]:
        raise ValueError("expected one primary default route")
    nic = routes[0].get("dev", "")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,14}", nic) or nic == "lo":
        raise ValueError("invalid default interface")
    addresses = json.loads(
        command(["ip", "-j", "-4", "addr", "show", "dev", nic], capture=True).stdout
    )
    if not any(
        address.get("local") == cfg["system_private_ip"]
        for item in addresses
        for address in item.get("addr_info", [])
    ):
        raise ValueError("default interface does not own system IP")
    return nic


def rules(cfg, nic):
    worker = validate_config(cfg)
    excluded = list(dict.fromkeys(list(BLOCKED) + cfg["vpc_cidrs"]))
    forward = [
        [
            "-d",
            worker,
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED,RELATED",
            "-j",
            "ACCEPT",
        ]
    ]
    forward += [["-d", cidr, "-j", "DROP"] for cidr in excluded]
    forward += [
        [
            "-s",
            worker,
            "-p",
            "tcp",
            "-m",
            "multiport",
            "--dports",
            "80,443",
            "-m",
            "conntrack",
            "--ctstate",
            "NEW,ESTABLISHED",
            "-j",
            "ACCEPT",
        ],
        ["-j", "DROP"],
    ]
    nat = [["-d", cidr, "-j", "RETURN"] for cidr in excluded]
    nat += [["-p", "tcp", "-m", "multiport", "--dports", "80,443", "-j", "MASQUERADE"]]
    return [
        ("filter", "COSMOS_FWD", "FORWARD", ["-i", nic, "-o", nic], forward),
        ("nat", "COSMOS_NAT", "POSTROUTING", ["-s", worker, "-o", nic], nat),
    ]


def apply_rules(cfg, nic):
    plan = rules(cfg, nic)
    command(
        [
            "sysctl",
            "-w",
            "net.ipv4.ip_forward=1",
            "net.ipv4.conf.all.send_redirects=0",
            "net.ipv4.conf.default.send_redirects=0",
            "net.ipv4.conf." + nic + ".send_redirects=0",
        ]
    )
    for table, chain, parent, match, entries in plan:
        # Restore commits each table atomically without clearing any parent/k3s chain.
        lines = ["*" + table, ":" + chain + " - [0:0]", "-F " + chain]
        lines += ["-A " + chain + " " + " ".join(entry) for entry in entries]
        lines += ["COMMIT", ""]
        command(
            ["iptables-restore", "--wait", "30", "--noflush"],
            data="\n".join(lines).encode(),
        )
        prefix = ["iptables", "-w", "30", "-t", table]
        jump = match + ["-j", chain]
        if command(prefix + ["-C", parent] + jump, missing_ok=True).returncode:
            command(prefix + ["-I", parent, "1"] + jump)


def unit_text():
    return (
        MARKER
        + """[Unit]
Description=Cosmos scoped worker HTTP/HTTPS NAT
Wants=network-online.target
After=network-online.target
Before=k3s.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/cosmos-bootstrap/configure-nat.py --apply
RemainAfterExit=yes
StandardOutput=null
StandardError=null
[Install]
WantedBy=multi-user.target
"""
    )


def install(cfg):
    validate_config(cfg)
    nic = default_interface(cfg)
    files = {
        SYSCTL: MARKER
        + "net.ipv4.ip_forward=1\nnet.ipv4.conf.all.send_redirects=0\n"
        + "net.ipv4.conf.default.send_redirects=0\nnet.ipv4.conf."
        + nic
        + ".send_redirects=0\n",
        UNIT: unit_text(),
    }
    for path in files:
        if path.is_symlink() or (
            path.exists() and not path.read_text().startswith(MARKER)
        ):
            raise ValueError("unmanaged NAT service or sysctl file; refusing overwrite")
    for path, content in files.items():
        fd, name = tempfile.mkstemp(prefix=".cosmos-nat-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
            os.chmod(name, 0o644)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    command(["systemctl", "daemon-reload"])
    command(["systemctl", "enable", "cosmos-nat.service"])
    command(["systemctl", "restart", "cosmos-nat.service"])
    command(["systemctl", "is-active", "--quiet", "cosmos-nat.service"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--install", action="store_true")
    modes.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("NAT setup requires root")
    cfg = json.loads((BASE / "config.json").read_text())
    validate_config(cfg)
    if args.install:
        install(cfg)
    else:
        apply_rules(cfg, default_interface(cfg))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            "NAT setup failed ("
            + type(error).__name__
            + "); inspect service status privately.",
            file=sys.stderr,
        )
        sys.exit(1)
