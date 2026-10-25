#!/bin/bash
set +x
set -euo pipefail
umask 077
trap 'printf "%s\n" "Worker bootstrap failed; no credentials were emitted." >&2' ERR
if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' 'Run worker-bootstrap.sh as root.' >&2
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get -o DPkg::Lock::Timeout=300 update -qq >/dev/null 2>&1
apt-get -o DPkg::Lock::Timeout=300 install -y -qq ca-certificates curl python3-boto3 kmod udev conntrack socat iptables >/dev/null 2>&1
/usr/bin/python3 - <<'PY'
import base64
import grp
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import boto3
from botocore.config import Config

# Replaced with an allowlisted, nonsecret configuration by configure-cluster.py.
CONFIG_B64 = '__COSMOS_CONFIG_B64__'
SANDBOX_APPARMOR_PROFILE = '''abi <abi/4.0>,
include <tunables/global>
profile cosmos-sandbox /usr/local/opt/cosmos/bin/sb flags=(unconfined) {
  userns,
}
'''


def run(args):
    return subprocess.run(args, check=True, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=300)


def write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.cosmos-')
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content if isinstance(content, bytes) else content.encode())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def install_sandbox_apparmor():
    path = Path('/etc/apparmor.d/cosmos-sandbox')
    if path.exists() and path.read_text() != SANDBOX_APPARMOR_PROFILE:
        raise ValueError('existing sandbox AppArmor profile differs; refusing overwrite')
    write(path, SANDBOX_APPARMOR_PROFILE, 0o644)
    run(['apparmor_parser', '--replace', str(path)])


def kubelet_args(cfg, provider_id):
    profile = cfg['session_profile']
    if profile not in ('default', 'eight'):
        raise ValueError('unsupported session profile')
    max_pods = 8 if profile == 'eight' else 110
    return [f'provider-id={provider_id}', f'max-pods={max_pods}',
            'system-reserved=cpu=200m,memory=512Mi,ephemeral-storage=2Gi',
            'kube-reserved=cpu=200m,memory=512Mi,ephemeral-storage=2Gi']


def get(url, headers=None, method='GET', data=None):
    request = urllib.request.Request(url, headers=headers or {}, method=method, data=data)
    with urllib.request.urlopen(request, timeout=120 if url.startswith('https:') else 10) as response:
        return response.read()


def validate_agent_token(token):
    if not token.startswith('K10') or '::node:' not in token:
        raise ValueError('not a secure agent token')


def main():
    os.umask(0o077)
    cfg = json.loads(base64.b64decode(CONFIG_B64, validate=True))
    metadata = 'http://169.254.169.254/latest/'
    imds_token = get(metadata + 'api/token', {'X-aws-ec2-metadata-token-ttl-seconds': '60'},
                     method='PUT', data=b'').decode()
    headers = {'X-aws-ec2-metadata-token': imds_token}
    instance_id = get(metadata + 'meta-data/instance-id', headers).decode().strip()
    az = get(metadata + 'meta-data/placement/availability-zone', headers).decode().strip()
    private_ip = get(metadata + 'meta-data/local-ipv4', headers).decode().strip()
    if not re.fullmatch(r'i-[0-9a-f]+', instance_id) or not re.fullmatch(r'[a-z0-9-]+', az):
        raise ValueError('invalid instance metadata')
    cpuinfo = Path('/proc/cpuinfo').read_text()
    module = 'kvm_intel' if 'GenuineIntel' in cpuinfo else 'kvm_amd' if 'AuthenticAMD' in cpuinfo else None
    if module is None:
        raise ValueError('unsupported CPU vendor')
    run(['modprobe', 'kvm'])
    run(['modprobe', module])
    run(['modprobe', 'overlay'])
    run(['modprobe', 'br_netfilter'])
    try:
        if grp.getgrgid(1080).gr_name != 'cosmos-kvm':
            raise ValueError('KVM group ID collision')
    except KeyError:
        try:
            grp.getgrnam('cosmos-kvm')
        except KeyError:
            run(['groupadd', '--gid', '1080', 'cosmos-kvm'])
        else:
            raise ValueError('KVM group name collision')
    if grp.getgrnam('cosmos-kvm').gr_gid != 1080:
        raise ValueError('KVM group ID collision')
    write('/etc/udev/rules.d/99-cosmos-kvm.rules',
          'KERNEL=="kvm", GROUP="cosmos-kvm", MODE="0660"\n', 0o644)
    run(['udevadm', 'control', '--reload-rules'])
    run(['udevadm', 'trigger', '--subsystem-match=misc', '--sysname-match=kvm'])
    run(['udevadm', 'settle'])
    if not Path('/dev/kvm').exists() or not stat.S_ISCHR(os.stat('/dev/kvm').st_mode):
        raise ValueError('nested virtualization unavailable')
    os.chown('/dev/kvm', 0, 1080)
    os.chmod('/dev/kvm', 0o660)
    if os.stat('/dev/kvm').st_gid != 1080:
        raise ValueError('KVM device group mismatch')
    fd = os.open('/dev/kvm', os.O_RDWR | os.O_CLOEXEC)
    os.close(fd)
    write('/etc/modules-load.d/cosmos-k3s.conf', f'{module}\noverlay\nbr_netfilter\n', 0o644)
    write('/etc/sysctl.d/90-cosmos-k3s.conf',
          'net.ipv4.ip_forward=1\nnet.bridge.bridge-nf-call-iptables=1\n', 0o644)
    run(['sysctl', '--system'])
    run(['swapoff', '-a'])
    install_sandbox_apparmor()
    version = cfg['k3s_version']
    if not re.fullmatch(r'v\d+\.\d+\.\d+\+k3s\d+', version):
        raise ValueError('invalid k3s version')
    binary = Path('/usr/local/bin/k3s')
    if binary.exists():
        result = subprocess.run([str(binary), '--version'], check=True,
                                capture_output=True, timeout=30)
        if version not in result.stdout.decode().split():
            raise ValueError('existing k3s version differs')
    else:
        release = 'https://github.com/k3s-io/k3s/releases/download/' + urllib.parse.quote(version, safe='')
        checksums = get(release + '/sha256sum-amd64.txt').decode().splitlines()
        expected = next(line.split()[0] for line in checksums
                        if line.split()[-1].lstrip('*./') == 'k3s')
        payload = get(release + '/k3s')
        if not re.fullmatch(r'[0-9a-f]{64}', expected) or hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError('k3s checksum mismatch')
        write(binary, payload, 0o755)
    ssm = boto3.client('ssm', region_name=cfg['region'],
                       config=Config(retries={'mode': 'standard', 'max_attempts': 8}))
    for attempt in range(30):
        try:
            token = ssm.get_parameter(Name=cfg['agent_parameter'], WithDecryption=True)['Parameter']['Value'].strip()
            break
        except Exception:
            if attempt == 29:
                raise
            time.sleep(10)
    validate_agent_token(token)
    write('/etc/rancher/k3s/agent-token', token + '\n')
    agent_config = {
        'server': 'https://' + cfg['system_private_ip'] + ':6443',
        'token-file': '/etc/rancher/k3s/agent-token',
        'node-name': instance_id,
        'node-ip': private_ip,
        'node-label': ['cosmos.augmentcode.com/pool-type=kvm'],
        'node-taint': ['karpenter.sh/unregistered:NoExecute',
                       'cosmos.augmentcode.com/pool-type=kvm:NoSchedule'],
        'kubelet-arg': kubelet_args(cfg, f'aws:///{az}/{instance_id}'),
    }
    config_path = Path('/etc/rancher/k3s/config.yaml')
    if config_path.exists() and json.loads(config_path.read_text()) != agent_config:
        raise ValueError('existing worker configuration differs; refusing overwrite')
    if not config_path.exists():
        write(config_path, json.dumps(agent_config))
    unit = Path('/etc/systemd/system/k3s-agent.service')
    marker = '# Managed by cosmos-bootstrap'
    if unit.exists() and marker not in unit.read_text():
        raise ValueError('unmanaged agent service; refusing overwrite')
    if not unit.exists():
        write(unit, marker + '\n' + '''[Unit]
Description=Cosmos k3s KVM worker
Wants=network-online.target
After=network-online.target
[Service]
Type=notify
ExecStart=/usr/local/bin/k3s agent
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
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'k3s-agent.service'])
    print('KVM worker bootstrap complete.')


try:
    main()
except Exception as error:
    print('Worker bootstrap failed (' + type(error).__name__ + ').', file=sys.stderr)
    sys.exit(1)
PY
