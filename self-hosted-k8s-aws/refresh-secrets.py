#!/usr/bin/python3
"""Refresh mounted credentials without logging or passing secrets in argv."""

import base64
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import boto3
from botocore.config import Config

BASE = Path("/opt/cosmos-bootstrap")
KUBE = ["/usr/local/bin/k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/k3s.yaml"]


def apply_secret(namespace, name, secret_type, values):
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": secret_type,
        "data": {
            key: base64.b64encode(value.encode()).decode()
            for key, value in values.items()
        },
    }
    # Server-side apply avoids storing a second credential copy in an annotation.
    subprocess.run(
        KUBE
        + [
            "apply",
            "--server-side",
            "--field-manager=cosmos-secret-refresh",
            "-f",
            "-",
        ],
        input=json.dumps(manifest).encode(),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )


def main():
    if os.geteuid() != 0:
        raise PermissionError("root required")
    os.umask(0o077)
    lock_path = Path("/run/cosmos-secret-refresh.lock")
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = json.loads((BASE / "config.json").read_text())
        retry = Config(
            retries={"mode": "standard", "max_attempts": 8},
            connect_timeout=10,
            read_timeout=30,
        )
        ssm = boto3.client("ssm", region_name=cfg["region"], config=retry)
        portal_token = ssm.get_parameter(
            Name=cfg["portal_parameter"], WithDecryption=True
        )["Parameter"]["Value"]
        if not portal_token.strip():
            raise ValueError("empty portal credential")
        ecr = boto3.client("ecr", region_name=cfg["region"], config=retry)
        authorization = ecr.get_authorization_token(registryIds=[cfg["account_id"]])[
            "authorizationData"
        ]
        registry_host = cfg["ecr_repository"].split("/", 1)[0]
        entry = next(
            item
            for item in authorization
            if item["proxyEndpoint"].removeprefix("https://").rstrip("/")
            == registry_host
        )
        auth = entry["authorizationToken"]
        if not base64.b64decode(auth, validate=True).startswith(b"AWS:"):
            raise ValueError("invalid registry authorization")
        docker_config = json.dumps({"auths": {registry_host: {"auth": auth}}})
        apply_secret("cosmos-ctrl", "portal-token", "Opaque", {"token": portal_token})
        for namespace in ("cosmos-ctrl", "cosmos-sb"):
            apply_secret(
                namespace,
                "cosmos-registry-auth",
                "kubernetes.io/dockerconfigjson",
                {".dockerconfigjson": docker_config},
            )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            "Credential refresh failed (" + type(error).__name__ + ").", file=sys.stderr
        )
        sys.exit(1)
