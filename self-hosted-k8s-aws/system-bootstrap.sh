#!/bin/bash
set +x
set -euo pipefail
umask 077
trap 'printf "%s\n" "System bootstrap failed; inspect configuration and service status without exporting credentials." >&2' ERR
if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' 'Run system-bootstrap.sh as root.' >&2
  exit 1
fi
printf '%s\n' 'Installing system prerequisites.'
export DEBIAN_FRONTEND=noninteractive
apt-get -o DPkg::Lock::Timeout=300 update -qq >/dev/null 2>&1
apt-get -o DPkg::Lock::Timeout=300 install -y -qq ca-certificates python3-boto3 python3-yaml curl jq unzip kmod conntrack socat iptables >/dev/null 2>&1
/usr/bin/python3 - <<'PY'
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

import boto3
import yaml
from botocore.config import Config

BASE = Path('/opt/cosmos-bootstrap')


def run(args, timeout=900, **kwargs):
    return subprocess.run(args, check=True, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=timeout, **kwargs)


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.cosmos-')
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data if isinstance(data, bytes) else data.encode())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def download(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def install_k3s(version):
    if not re.fullmatch(r'v\d+\.\d+\.\d+\+k3s\d+', version):
        raise ValueError('invalid version')
    binary = Path('/usr/local/bin/k3s')
    if binary.exists():
        result = subprocess.run([str(binary), '--version'], check=True,
                                capture_output=True, timeout=30)
        if version not in result.stdout.decode().split():
            raise ValueError('existing k3s version differs; explicit upgrade required')
        return
    url = 'https://github.com/k3s-io/k3s/releases/download/' + urllib.parse.quote(version, safe='')
    checksums = download(url + '/sha256sum-amd64.txt').decode().splitlines()
    expected = next(line.split()[0] for line in checksums
                    if line.split()[-1].lstrip('*./') == 'k3s')
    binary_data = download(url + '/k3s')
    if not re.fullmatch(r'[0-9a-f]{64}', expected) or hashlib.sha256(binary_data).hexdigest() != expected:
        raise ValueError('k3s checksum mismatch')
    atomic_write(binary, binary_data, 0o755)


def install_helm(version):
    if not re.fullmatch(r'v\d+\.\d+\.\d+', version):
        raise ValueError('invalid version')
    url = f'https://get.helm.sh/helm-{version}-linux-amd64.tar.gz'
    archive = download(url)
    expected = download(url + '.sha256sum').decode().split()[0]
    if not re.fullmatch(r'[0-9a-f]{64}', expected) or hashlib.sha256(archive).hexdigest() != expected:
        raise ValueError('helm checksum mismatch')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        entry = tar.getmember('linux-amd64/helm')
        if not entry.isfile():
            raise ValueError('invalid helm artifact')
        with tar.extractfile(entry) as stream:
            atomic_write('/usr/local/bin/helm', stream.read(), 0o755)


def validate_agent_token(token, server_token):
    if (not token.startswith('K10') or '::node:' not in token
            or token == server_token
            or token.rsplit(':', 1)[-1] == server_token.rsplit(':', 1)[-1]):
        raise ValueError('agent token is not distinct from server token')


def main():
    os.umask(0o077)
    os.chmod(BASE, 0o700)
    cfg = json.loads((BASE / 'config.json').read_text())
    os.chmod(BASE / 'config.json', 0o600)
    run(['/usr/bin/python3', str(BASE / 'configure-nat.py'), '--install'])
    install_k3s(cfg['k3s_version'])
    install_helm(cfg['helm_version'])
    config_path = Path('/etc/rancher/k3s/config.yaml')
    unit_path = Path('/etc/systemd/system/k3s.service')
    agent_seed = Path('/etc/rancher/k3s/bootstrap-agent-token')
    expected = {
        'node-name': 'cosmos-system',
        'node-ip': cfg['system_private_ip'],
        'advertise-address': cfg['system_private_ip'],
        'tls-san': [cfg['system_private_ip']],
        'node-label': ['cosmos.augmentcode.com/role=system'],
        'disable': ['traefik', 'servicelb', 'metrics-server'],
        'secrets-encryption': True,
        'write-kubeconfig-mode': '0600',
        'agent-token-file': str(agent_seed),
    }
    marker = '# Managed by cosmos-bootstrap'
    if config_path.exists():
        if (marker not in config_path.read_text()
                or (unit_path.exists() and marker not in unit_path.read_text())
                or yaml.safe_load(config_path.read_text()) != expected
                or not agent_seed.exists()):
            raise ValueError('existing cluster configuration differs; refusing overwrite')
    else:
        if unit_path.exists() or Path('/var/lib/rancher/k3s/server').exists():
            raise ValueError('existing cluster identity found; refusing overwrite')
        if not agent_seed.exists():
            atomic_write(agent_seed, secrets.token_hex(32) + '\n')
        atomic_write(config_path, marker + '\n' + yaml.safe_dump(expected))
    os.chmod(agent_seed, 0o600)
    os.chmod(config_path, 0o600)
    if not unit_path.exists():
        atomic_write(unit_path, marker + '\n' + '''[Unit]
Description=Cosmos k3s system server
Wants=network-online.target
Requires=cosmos-nat.service
After=network-online.target cosmos-nat.service
[Service]
Type=notify
ExecStart=/usr/local/bin/k3s server
KillMode=process
Delegate=yes
LimitNOFILE=1048576
LimitNPROC=infinity
LimitCORE=0
TasksMax=infinity
Restart=always
RestartSec=5
TimeoutStartSec=300
StandardOutput=null
StandardError=null
[Install]
WantedBy=multi-user.target
''', 0o644)
    run(['modprobe', 'br_netfilter'])
    run(['modprobe', 'overlay'])
    atomic_write('/etc/modules-load.d/cosmos-k3s.conf', 'overlay\nbr_netfilter\n', 0o644)
    atomic_write('/etc/sysctl.d/90-cosmos-k3s.conf',
                 'net.ipv4.ip_forward=1\nnet.bridge.bridge-nf-call-iptables=1\n', 0o644)
    run(['sysctl', '--system'])
    run(['swapoff', '-a'])
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'k3s.service'])
    for _ in range(120):
        result = subprocess.run(['/usr/local/bin/k3s', 'kubectl', '--kubeconfig',
                                 '/etc/rancher/k3s/k3s.yaml', 'wait',
                                 '--for=condition=Ready', 'node/cosmos-system', '--timeout=5s'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if result.returncode == 0:
            break
        time.sleep(5)
    else:
        raise TimeoutError('server readiness')
    token = Path('/var/lib/rancher/k3s/server/agent-token').read_text().strip()
    server_token = Path('/var/lib/rancher/k3s/server/token').read_text().strip()
    validate_agent_token(token, server_token)
    ssm = boto3.client('ssm', region_name=cfg['region'],
                       config=Config(retries={'mode': 'standard', 'max_attempts': 8}))
    ssm.put_parameter(Name=cfg['agent_parameter'], Type='SecureString',
                      Value=token, Overwrite=True)
    run(['/usr/bin/python3', str(BASE / 'configure-cluster.py')], timeout=2400)
    print('System bootstrap complete; NAT is configured and credentials remain on the system node.')


try:
    main()
except Exception as error:
    print('System bootstrap failed (' + type(error).__name__ + ').', file=sys.stderr)
    sys.exit(1)
PY
