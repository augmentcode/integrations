#!/usr/bin/python3
"""Install the pinned charts and KVM capacity on the private k3s cluster."""

import base64
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

BASE = Path("/opt/cosmos-bootstrap")
KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"
KUBE = ["/usr/local/bin/k3s", "kubectl", "--kubeconfig", KUBECONFIG]
ROLE_KEY = "cosmos.augmentcode.com/role"
POOL_KEY = "cosmos.augmentcode.com/pool-type"
# The chart version is not a published container tag; pin the verified image manifest separately.
COSMOS_IMAGE_REPOSITORY = "dist.augmentcode.com/cosmos/cosmos-sh"
COSMOS_IMAGE_DIGEST = (
    "sha256:d912bdfc72b9c4b6b73f814a0d171f6612211f54df1c5a8746a0a6945ea8ff14"
)


def run(args, data=None, capture=False):
    result = subprocess.run(
        args,
        input=data,
        check=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=900,
    )
    return result.stdout if capture else None


def apply(objects):
    run(
        KUBE
        + ["apply", "--server-side", "--field-manager=cosmos-bootstrap", "-f", "-"],
        json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}).encode(),
    )


def validate_config(cfg):
    required = (
        "region",
        "account_id",
        "stack_name",
        "provider",
        "portal_address",
        "system_private_ip",
        "security_group_id",
        "worker_subnet_ids",
        "worker_instance_profile_name",
        "ami_id",
        "worker_instance_types",
        "worker_max_vcpu",
        "worker_disk_gb",
        "portal_parameter",
        "agent_parameter",
        "ecr_repository",
        "k3s_version",
        "karpenter_version",
        "cosmos_chart_version",
        "helm_version",
        "networking_mode",
        "worker_subnet_cidr",
        "vpc_cidrs",
        "session_profile",
    )
    if any(key not in cfg for key in required):
        raise ValueError("missing configuration")
    patterns = {
        "region": r"[a-z]{2}(?:-[a-z]+)+-\d+",
        "account_id": r"\d{12}",
        "stack_name": r"[A-Za-z][A-Za-z0-9-]{0,127}",
        "security_group_id": r"sg-[0-9a-f]+",
        "ami_id": r"ami-[0-9a-f]+",
        "worker_instance_profile_name": r"[\w+=,.@-]{1,128}",
        "k3s_version": r"v\d+\.\d+\.\d+\+k3s\d+",
        "helm_version": r"v\d+\.\d+\.\d+",
        "karpenter_version": r"\d+\.\d+\.\d+",
        "cosmos_chart_version": r"\d+\.\d+\.\d+",
    }
    for key, pattern in patterns.items():
        if not isinstance(cfg[key], str) or not re.fullmatch(pattern, cfg[key]):
            raise ValueError("invalid configuration field")
    if cfg["networking_mode"] != "system-nat-v1":
        raise ValueError("unsupported networking mode")
    if cfg["session_profile"] not in ("default", "eight"):
        raise ValueError("unsupported session profile")
    if not ipaddress.IPv4Address(cfg["system_private_ip"]).is_private:
        raise ValueError("system endpoint must be private")
    for key in ("provider", "portal_address"):
        if (
            not isinstance(cfg[key], str)
            or not cfg[key]
            or any(c.isspace() for c in cfg[key])
        ):
            raise ValueError("invalid endpoint or provider")
    if cfg["portal_parameter"] != "/cosmos/" + cfg["stack_name"] + "/portal-token":
        raise ValueError("unexpected portal parameter")
    if cfg["agent_parameter"] != "/cosmos/" + cfg["stack_name"] + "/agent-token":
        raise ValueError("unexpected agent parameter")
    repository = (
        cfg["account_id"]
        + ".dkr.ecr."
        + cfg["region"]
        + ".amazonaws.com/"
        + cfg["stack_name"]
        + "-snapshots"
    )
    if cfg["ecr_repository"] != repository:
        raise ValueError("unexpected snapshot repository")
    for key, pattern in (
        ("worker_subnet_ids", r"subnet-[0-9a-f]+"),
        ("worker_instance_types", r"[a-z0-9-]+\.[a-z0-9]+"),
    ):
        if (
            not isinstance(cfg[key], list)
            or not cfg[key]
            or any(
                not isinstance(item, str) or not re.fullmatch(pattern, item)
                for item in cfg[key]
            )
        ):
            raise ValueError("invalid worker selectors")
    if type(cfg["worker_max_vcpu"]) is not int or cfg["worker_max_vcpu"] < 4:
        raise ValueError("worker CPU limit too small")
    if (
        type(cfg["worker_disk_gb"]) is not int
        or not 100 <= cfg["worker_disk_gb"] <= 16384
    ):
        raise ValueError("worker disk must be at least 100 GiB")
    if cfg["session_profile"] == "eight" and cfg["worker_disk_gb"] < 200:
        raise ValueError("eight session profile requires at least 200 GiB worker disk")


def post_render():
    documents = list(yaml.safe_load_all(sys.stdin.read()))
    matched = 0
    for document in documents:
        if not document or document.get("kind") != "Deployment":
            continue
        if document.get("metadata", {}).get("namespace") != "cosmos-ctrl":
            continue
        pod = document["spec"]["template"]["spec"]
        if not any(
            container.get("command") == ["cosmos", "k8s"]
            for container in pod["containers"]
        ):
            continue
        pod.setdefault("nodeSelector", {})[ROLE_KEY] = "system"
        matched += 1
    if matched != 1:
        raise ValueError("expected exactly one Cosmos controller deployment")
    yaml.safe_dump_all([doc for doc in documents if doc], sys.stdout, sort_keys=False)


def worker_user_data(cfg):
    fields = (
        "region",
        "agent_parameter",
        "system_private_ip",
        "k3s_version",
        "session_profile",
    )
    payload = base64.b64encode(
        json.dumps({key: cfg[key] for key in fields}).encode()
    ).decode()
    template = (BASE / "worker-bootstrap.sh").read_text()
    marker = "__COSMOS_CONFIG_B64__"
    if template.count(marker) != 1:
        raise ValueError("worker bootstrap configuration marker missing or repeated")
    rendered = template.replace(marker, payload)
    if len(rendered.encode()) > 16384:
        raise ValueError("worker user data exceeds EC2 limit")
    return rendered


def karpenter_values(cfg, ca_bundle):
    return {
        "replicas": 1,
        "hostNetwork": True,
        "dnsPolicy": "ClusterFirstWithHostNet",
        "nodeSelector": {ROLE_KEY: "system", "kubernetes.io/os": "linux"},
        "topologySpreadConstraints": [],
        "controller": {
            "env": [
                {"name": "AWS_REGION", "value": cfg["region"]},
                {"name": "AWS_DEFAULT_REGION", "value": cfg["region"]},
                {"name": "EKS_CONTROL_PLANE", "value": "false"},
            ],
            "resources": {
                "requests": {"cpu": "500m", "memory": "1Gi"},
                "limits": {"memory": "1Gi"},
            },
        },
        "settings": {
            "clusterName": cfg["stack_name"],
            "clusterEndpoint": "https://" + cfg["system_private_ip"] + ":6443",
            "clusterCABundle": ca_bundle,
            "eksControlPlane": False,
        },
    }


def cosmos_values(cfg, api_service_ip):
    deny = [cfg["system_private_ip"] + "/32"]
    if cfg.get("system_public_ip"):
        deny.append(str(ipaddress.IPv4Address(cfg["system_public_ip"])) + "/32")
    if api_service_ip:
        deny.append(str(ipaddress.IPv4Address(api_service_ip)) + "/32")
    existing_auth = {
        "enabled": True,
        "source": "existing",
        "name": "cosmos-registry-auth",
        "key": ".dockerconfigjson",
    }
    return {
        "installationName": "cosmos",
        "providerName": cfg["provider"],
        "image": {
            "repository": COSMOS_IMAGE_REPOSITORY,
            "tag": "latest",
            "digest": COSMOS_IMAGE_DIGEST,
        },
        "registry": {
            "repository": cfg["ecr_repository"],
            "controllerAuthSecret": existing_auth.copy(),
            "sandboxAuthSecret": existing_auth.copy(),
        },
        "portal": {
            "address": cfg["portal_address"],
            "tokenSecret": {
                "source": "existing",
                "name": "portal-token",
                "key": "token",
            },
        },
        "controller": {
            "replicas": 1,
            "resources": {
                "requests": {"cpu": "200m", "memory": "512Mi"},
                "limits": {"memory": "1Gi"},
            },
            "args": [
                "--pool-selector=" + POOL_KEY + "=kvm",
                "--pod-image-cache=/var/lib/cosmos-sh/cache",
                "--image.cache-max-size=8Gi",
                "--pod-disk-reserve=6Gi",
                "--image.max-size=4Gi",
                "--snapshot.max-size=3Gi",
            ],
        },
        "sandbox": {
            "namespace": "cosmos-sb",
            "namespaceResource": {"create": False},
            "kvmHostPath": True,
            "supplementalGroups": [1080],
            "resources": {"cpu": "2", "ram": "4Gi", "disk": "16Gi"},
            "network": {"access": "host", "deny": deny},
        },
    }


def reservation_objects(cfg):
    name = "cosmos-sandbox-small-reservations"
    api = "admissionregistration.k8s.io/v1"
    memory = "1500M" if cfg["session_profile"] == "eight" else "2G"
    match = {
        "namespaceSelector": {
            "matchLabels": {"kubernetes.io/metadata.name": "cosmos-sb"}
        },
        "objectSelector": {
            "matchLabels": {
                "cosmos.augmentcode.com/installation": "cosmos",
                "cosmos.augmentcode.com/provider": cfg["provider"],
                "cosmos.augmentcode.com/sandbox-backend": "true",
            }
        },
        "resourceRules": [
            {
                "apiGroups": [""],
                "apiVersions": ["v1"],
                "operations": ["CREATE"],
                "resources": ["pods"],
                "scope": "Namespaced",
            }
        ],
    }
    expression = (
        """Object{spec: Object.spec{containers: [Object.spec.containers{
  name: 'sandbox', resources: Object.spec.containers.resources{
    requests: {'cpu': '125m', 'memory': '%s'}
  }
}]}}"""
        % memory
    )
    policy = {
        "apiVersion": api,
        "kind": "MutatingAdmissionPolicy",
        "metadata": {"name": name},
        "spec": {
            "failurePolicy": "Fail",
            "reinvocationPolicy": "IfNeeded",
            "matchConstraints": match,
            "matchConditions": [
                {
                    "name": "sandbox-container-present",
                    "expression": "object.spec.containers.exists(c, c.name == 'sandbox')",
                }
            ],
            "mutations": [
                {
                    "patchType": "ApplyConfiguration",
                    "applyConfiguration": {"expression": expression},
                }
            ],
        },
    }
    binding = {
        "apiVersion": api,
        "kind": "MutatingAdmissionPolicyBinding",
        "metadata": {"name": name},
        "spec": {"policyName": name, "matchResources": match},
    }
    return [policy, binding]


def capacity_objects(cfg):
    reservations = {"cpu": "200m", "memory": "512Mi", "ephemeral-storage": "2Gi"}
    node_class = {
        "apiVersion": "karpenter.k8s.aws/v1",
        "kind": "EC2NodeClass",
        "metadata": {"name": "cosmos-kvm"},
        "spec": {
            "amiFamily": "Custom",
            "amiSelectorTerms": [{"id": cfg["ami_id"]}],
            "subnetSelectorTerms": [
                {"id": subnet} for subnet in cfg["worker_subnet_ids"]
            ],
            "securityGroupSelectorTerms": [{"id": cfg["security_group_id"]}],
            "instanceProfile": cfg["worker_instance_profile_name"],
            "associatePublicIPAddress": False,
            "cpuOptions": {"nestedVirtualization": "enabled"},
            "metadataOptions": {
                "httpEndpoint": "enabled",
                "httpTokens": "required",
                "httpPutResponseHopLimit": 1,
                "httpProtocolIPv6": "disabled",
            },
            "blockDeviceMappings": [
                {
                    "deviceName": "/dev/sda1",
                    "rootVolume": True,
                    "ebs": {
                        "volumeType": "gp3",
                        "volumeSize": str(cfg["worker_disk_gb"]) + "Gi",
                        "encrypted": True,
                        "deleteOnTermination": True,
                    },
                }
            ],
            "kubelet": {
                "maxPods": 8 if cfg["session_profile"] == "eight" else 110,
                "systemReserved": reservations.copy(),
                "kubeReserved": reservations.copy(),
            },
            "tags": {
                "Name": cfg["stack_name"] + "-kvm",
                "karpenter.sh/discovery": cfg["stack_name"],
            },
            "userData": worker_user_data(cfg),
        },
    }
    node_pool = {
        "apiVersion": "karpenter.sh/v1",
        "kind": "NodePool",
        "metadata": {"name": "sessions"},
        "spec": {
            "template": {
                "metadata": {"labels": {POOL_KEY: "kvm"}},
                "spec": {
                    "nodeClassRef": {
                        "group": "karpenter.k8s.aws",
                        "kind": "EC2NodeClass",
                        "name": "cosmos-kvm",
                    },
                    "taints": [
                        {"key": POOL_KEY, "value": "kvm", "effect": "NoSchedule"}
                    ],
                    "requirements": [
                        {
                            "key": "node.kubernetes.io/instance-type",
                            "operator": "In",
                            "values": cfg["worker_instance_types"],
                        },
                        {
                            "key": "kubernetes.io/arch",
                            "operator": "In",
                            "values": ["amd64"],
                        },
                        {
                            "key": "kubernetes.io/os",
                            "operator": "In",
                            "values": ["linux"],
                        },
                        {
                            "key": "karpenter.sh/capacity-type",
                            "operator": "In",
                            "values": ["on-demand"],
                        },
                    ],
                    "expireAfter": "Never",
                },
            },
            "limits": {"cpu": str(cfg["worker_max_vcpu"])},
            "disruption": {
                "consolidationPolicy": "WhenEmpty",
                "consolidateAfter": "120s",
                "budgets": [{"nodes": "0", "reasons": ["Drifted"]}],
            },
        },
    }
    return [node_class, node_pool]


def install_chart(name, chart, namespace, version, values, renderer=False):
    fd, filename = tempfile.mkstemp(prefix=".helm-values-", suffix=".json", dir=BASE)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(values, stream)
        args = [
            "/usr/local/bin/helm",
            "upgrade",
            "--install",
            name,
            chart,
            "--namespace",
            namespace,
            "--version",
            version,
            "--kubeconfig",
            KUBECONFIG,
            "--values",
            filename,
            "--wait",
            "--timeout",
            "10m",
            "--history-max",
            "3",
        ]
        if renderer:
            args.extend(
                [
                    "--post-renderer",
                    str(BASE / "configure-cluster.py"),
                    "--post-renderer-args=--post-render",
                ]
            )
        run(args)
    finally:
        os.unlink(filename)


def install_refresh_timer():
    units = {
        "cosmos-refresh-secrets.service": """[Unit]
Description=Refresh Cosmos mounted credentials
Wants=network-online.target
After=network-online.target k3s.service
[Service]
Type=oneshot
User=root
UMask=0077
LimitCORE=0
ExecStart=/usr/bin/python3 /opt/cosmos-bootstrap/refresh-secrets.py
TimeoutStartSec=600
StandardOutput=null
StandardError=journal
""",
        "cosmos-refresh-secrets.timer": """[Unit]
Description=Refresh Cosmos credentials every four hours
[Timer]
OnBootSec=5min
OnUnitActiveSec=4h
RandomizedDelaySec=2min
Unit=cosmos-refresh-secrets.service
[Install]
WantedBy=timers.target
""",
    }
    for name, contents in units.items():
        path = Path("/etc/systemd/system") / name
        path.write_text(contents)
        path.chmod(0o644)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "cosmos-refresh-secrets.timer"])


def main():
    if os.geteuid() != 0:
        raise PermissionError("root required")
    os.umask(0o077)
    cfg = json.loads((BASE / "config.json").read_text())
    validate_config(cfg)
    os.environ["KUBECONFIG"] = KUBECONFIG
    for asset in ("configure-cluster.py", "refresh-secrets.py", "worker-bootstrap.sh"):
        (BASE / asset).chmod(0o700)
    apply(
        [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}
            for name in ("karpenter", "cosmos-ctrl", "cosmos-sb")
        ]
    )
    apply(reservation_objects(cfg))
    print("Provisioning mounted credentials.", flush=True)
    run(["/usr/bin/python3", str(BASE / "refresh-secrets.py")])
    install_refresh_timer()
    ca = Path("/var/lib/rancher/k3s/server/tls/server-ca.crt").read_bytes()
    if b"-----BEGIN CERTIFICATE-----" not in ca:
        raise ValueError("missing cluster CA")
    print("Installing the pinned Karpenter controller.", flush=True)
    install_chart(
        "karpenter",
        "oci://public.ecr.aws/karpenter/karpenter",
        "karpenter",
        cfg["karpenter_version"],
        karpenter_values(cfg, base64.b64encode(ca).decode()),
    )
    for crd in ("ec2nodeclasses.karpenter.k8s.aws", "nodepools.karpenter.sh"):
        run(
            KUBE
            + ["wait", "--for=condition=Established", "crd/" + crd, "--timeout=120s"]
        )
    apply(capacity_objects(cfg))
    run(
        KUBE
        + ["wait", "--for=condition=Ready", "ec2nodeclass/cosmos-kvm", "--timeout=180s"]
    )
    service = json.loads(
        run(
            KUBE + ["get", "service", "kubernetes", "-n", "default", "-o", "json"],
            capture=True,
        )
    )
    print(
        "Installing the pinned Cosmos controller and waiting for portal readiness.",
        flush=True,
    )
    install_chart(
        "cosmos",
        "oci://dist.augmentcode.com/helm/cosmos",
        "cosmos-ctrl",
        cfg["cosmos_chart_version"],
        cosmos_values(cfg, service["spec"]["clusterIP"]),
        renderer=True,
    )
    print("Cluster configuration complete; workers scale from zero for sandbox demand.")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--post-render"]:
            post_render()
        elif sys.argv[1:]:
            raise ValueError("unexpected arguments")
        else:
            main()
    except Exception as error:
        print(
            "Cluster configuration failed (" + type(error).__name__ + ").",
            file=sys.stderr,
        )
        sys.exit(1)
