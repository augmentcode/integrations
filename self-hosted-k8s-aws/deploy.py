#!/usr/bin/env python3
"""CloudShell entrypoint. Read-only plan by default; --apply requires confirmation."""

import argparse
import base64
import getpass
import gzip
import ipaddress
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
PINS = {
    "k3s_version": "v1.37.1+k3s1",
    "karpenter_version": "1.15.1",
    "cosmos_chart_version": "0.20261024.12931",
    "helm_version": "v3.22.0",
}
NETWORKING_MODE = "system-nat-v1"
ASSETS = (
    "system-bootstrap.sh",
    "worker-bootstrap.sh",
    "refresh-secrets.py",
    "configure-cluster.py",
    "configure-nat.py",
)


def args_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--region", default="us-east-1", help="AWS region (default: us-east-1)"
    )
    parser.add_argument(
        "--availability-zone",
        help="Discovery AZ (default: REGIONa, normally us-east-1a)",
    )
    parser.add_argument(
        "--vpc-id", help="Override automatic selection of the default VPC"
    )
    parser.add_argument(
        "--system-subnet-id", help="Override public-subnet discovery in the selected AZ"
    )
    parser.add_argument(
        "--worker-subnet-cidr",
        help="Override automatic worker CIDR selection (/24 preferred, down to /28)",
    )
    parser.add_argument(
        "--provider", required=True, help="Existing Cosmos sandbox provider slug"
    )
    parser.add_argument("--stack-name", default="cosmos-private")
    parser.add_argument("--portal-address", default="gw.cosmos.augmentcode.com:443")
    parser.add_argument(
        "--worker-instance-types",
        default="m8i-flex.xlarge",
        help="Comma-separated, nested-virtualization capable",
    )
    parser.add_argument("--worker-max-vcpu", type=int, default=16)
    parser.add_argument(
        "--worker-disk-gb", type=int, default=100, help="Worker gp3 volume size in GiB"
    )
    parser.add_argument(
        "--session-profile",
        choices=("default", "eight"),
        default="default",
        help="Scheduler reservations: tested default or untested eight-session packing target",
    )
    parser.add_argument(
        "--ami-id", help="Optional pinned Canonical Ubuntu 24.04 amd64 AMI"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create paid resources after interactive confirmation",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Repeat bootstrap on an existing COMPLETE stack; requires --apply",
    )
    return parser


def validate_network_arguments(args):
    for field, pattern in (
        ("vpc_id", r"vpc-[0-9a-f]+"),
        ("system_subnet_id", r"subnet-[0-9a-f]+"),
    ):
        value = getattr(args, field)
        if value is not None and (
            not isinstance(value, str) or not re.fullmatch(pattern, value)
        ):
            raise ValueError("Invalid --" + field.replace("_", "-"))
    if args.availability_zone is not None and not re.fullmatch(
        re.escape(args.region) + r"[a-z]", args.availability_zone
    ):
        raise ValueError("--availability-zone must be a standard AZ in --region")
    if args.worker_subnet_cidr is not None:
        worker = ipaddress.ip_network(args.worker_subnet_cidr, strict=True)
        private_ranges = [
            ipaddress.ip_network(cidr)
            for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        ]
        if (
            worker.version != 4
            or not 16 <= worker.prefixlen <= 28
            or not any(worker.subnet_of(network) for network in private_ranges)
        ):
            raise ValueError(
                "--worker-subnet-cidr must be an RFC1918 IPv4 subnet, /16 through /28"
            )
        args.worker_subnet_cidr = str(worker)


def validate_arguments(args):
    patterns = {
        "region": r"[a-z]{2}(?:-[a-z]+)+-\d+",
        "provider": r"[a-z]([a-z0-9-]{0,28}[a-z0-9])?",
        "stack_name": r"[a-z][a-z0-9-]{2,29}",
        "portal_address": r"[a-z0-9.-]+:443",
    }
    for field, pattern in patterns.items():
        if not re.fullmatch(pattern, getattr(args, field)):
            raise ValueError("Invalid --" + field.replace("_", "-"))
    validate_network_arguments(args)
    args.worker_instance_types = list(
        dict.fromkeys(args.worker_instance_types.split(","))
    )
    if not all(
        re.fullmatch(r"[a-z0-9-]+\.[a-z0-9]+", item)
        for item in args.worker_instance_types
    ):
        raise ValueError("Invalid worker instance list")
    if args.ami_id and not re.fullmatch(r"ami-[0-9a-f]+", args.ami_id):
        raise ValueError("Invalid AMI ID")
    if not 4 <= args.worker_max_vcpu <= 128 or not 100 <= args.worker_disk_gb <= 16384:
        raise ValueError("Worker limits must be 4–128 vCPUs and 100–16384 GiB disk")
    if args.session_profile == "eight" and args.worker_disk_gb < 200:
        raise ValueError(
            "--session-profile eight requires --worker-disk-gb of at least 200"
        )
    if args.resume and not args.apply:
        raise ValueError("--resume also requires --apply")
    for filename in ASSETS:
        if not (BASE / filename).is_file():
            raise ValueError("Missing bundle asset: " + filename)


def ref(name):
    return {"Ref": name}


def sub(text):
    return {"Fn::Sub": text}


def getatt(name, attr):
    return {"Fn::GetAtt": [name, attr]}


def policy(actions, resources, condition=None):
    result = {"Effect": "Allow", "Action": actions, "Resource": resources}
    if condition:
        result["Condition"] = condition
    return result


def stack_template(cfg):
    """No tokens, passwords, kubeconfig or credentials occur in this template."""
    cluster = cfg["stack_name"]
    parameter_arns = [
        sub(
            "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter"
            + cfg[key]
        )
        for key in ("portal_parameter", "agent_parameter")
    ]
    assume = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    managed_ssm = sub(
        "arn:${AWS::Partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
    )
    region_condition = {"StringEquals": {"aws:RequestedRegion": ref("AWS::Region")}}
    tagged = {
        "StringEquals": {"ec2:ResourceTag/kubernetes.io/cluster/" + cluster: "owned"}
    }
    system_statements = [
        policy(
            [
                "ec2:DescribeImages",
                "ec2:DescribeInstances",
                "ec2:DescribeInstanceTypes",
                "ec2:DescribeInstanceTypeOfferings",
                "ec2:DescribeSubnets",
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeLaunchTemplates",
                "ec2:DescribeLaunchTemplateVersions",
                "ec2:DescribeAvailabilityZones",
                "ec2:DescribeSpotPriceHistory",
                "ec2:DescribeCapacityReservations",
                "ec2:DescribePlacementGroups",
            ],
            "*",
            region_condition,
        ),
        # The starter role is regional, not a production multi-tenant IAM boundary.
        policy(
            ["ec2:RunInstances", "ec2:CreateFleet", "ec2:CreateLaunchTemplate"],
            "*",
            region_condition,
        ),
        policy(
            ["ec2:CreateTags"],
            "*",
            {
                "StringEquals": {
                    "aws:RequestedRegion": ref("AWS::Region"),
                    "ec2:CreateAction": [
                        "RunInstances",
                        "CreateFleet",
                        "CreateLaunchTemplate",
                    ],
                }
            },
        ),
        policy(
            ["ec2:CreateTags", "ec2:TerminateInstances", "ec2:DeleteLaunchTemplate"],
            "*",
            tagged,
        ),
        policy(
            ["iam:PassRole"],
            getatt("WorkerRole", "Arn"),
            {"StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}},
        ),
        policy(["iam:GetInstanceProfile"], getatt("WorkerProfile", "Arn")),
        policy(["pricing:GetProducts"], "*"),
        policy(["ssm:GetParameter"], parameter_arns),
        policy(["ssm:PutParameter"], parameter_arns[1]),
        policy(["ecr:GetAuthorizationToken"], "*"),
        policy(
            [
                "ecr:BatchGetImage",
                "ecr:BatchCheckLayerAvailability",
                "ecr:GetDownloadUrlForLayer",
                "ecr:InitiateLayerUpload",
                "ecr:UploadLayerPart",
                "ecr:CompleteLayerUpload",
                "ecr:PutImage",
                "ecr:DescribeImages",
                "ecr:ListImages",
                "ecr:BatchDeleteImage",
            ],
            getatt("Snapshots", "Arn"),
        ),
        policy(
            ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"],
            "*",
            {
                "StringEquals": {
                    "kms:ViaService": sub("ssm.${AWS::Region}.amazonaws.com"),
                    "kms:EncryptionContext:PARAMETER_ARN": parameter_arns,
                }
            },
        ),
    ]
    resources = {
        "Snapshots": {
            "Type": "AWS::ECR::Repository",
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
            "Properties": {
                "RepositoryName": cluster + "-snapshots",
                "EncryptionConfiguration": {"EncryptionType": "AES256"},
                "ImageTagMutability": "MUTABLE",
                "ImageScanningConfiguration": {"ScanOnPush": True},
            },
        },
        "WorkerRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": assume,
                "ManagedPolicyArns": [managed_ssm],
                "Policies": [
                    {
                        "PolicyName": "ReadOnlyAgentJoin",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [
                                policy(["ssm:GetParameter"], parameter_arns[1]),
                                policy(
                                    ["kms:Decrypt"],
                                    "*",
                                    {
                                        "StringEquals": {
                                            "kms:ViaService": sub(
                                                "ssm.${AWS::Region}.amazonaws.com"
                                            ),
                                            "kms:EncryptionContext:PARAMETER_ARN": parameter_arns[
                                                1
                                            ],
                                        }
                                    },
                                ),
                            ],
                        },
                    }
                ],
            },
        },
        "WorkerProfile": {
            "Type": "AWS::IAM::InstanceProfile",
            "Properties": {"Roles": [ref("WorkerRole")]},
        },
        "SystemRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": assume,
                "ManagedPolicyArns": [managed_ssm],
                "Policies": [
                    {
                        "PolicyName": "CosmosKarpenterStarter",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": system_statements,
                        },
                    }
                ],
            },
        },
        "SystemProfile": {
            "Type": "AWS::IAM::InstanceProfile",
            "Properties": {"Roles": [ref("SystemRole")]},
        },
        "ClusterSecurityGroup": {
            "Type": "AWS::EC2::SecurityGroup",
            "Properties": {
                "GroupDescription": "Private k3s API, kubelet and VXLAN; no SSH or public ingress",
                "VpcId": cfg["vpc_id"],
                "SecurityGroupEgress": [{"IpProtocol": "-1", "CidrIp": "0.0.0.0/0"}],
                "Tags": [{"Key": "Name", "Value": cluster + "-private"}],
            },
        },
        "NatSecurityGroup": {
            "Type": "AWS::EC2::SecurityGroup",
            "Properties": {
                "GroupDescription": "Worker HTTP/HTTPS forwarding only; no public inbound proxy",
                "VpcId": cfg["vpc_id"],
                "SecurityGroupIngress": [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": port,
                        "ToPort": port,
                        "CidrIp": cfg["worker_subnet_cidr"],
                    }
                    for port in (80, 443)
                ],
                "SecurityGroupEgress": [{"IpProtocol": "-1", "CidrIp": "0.0.0.0/0"}],
            },
        },
        "WorkerSubnet": {
            "Type": "AWS::EC2::Subnet",
            "Properties": {
                "VpcId": cfg["vpc_id"],
                "CidrBlock": cfg["worker_subnet_cidr"],
                "AvailabilityZone": cfg["worker_availability_zone"],
                "MapPublicIpOnLaunch": False,
                "Tags": [{"Key": "Name", "Value": cluster + "-workers"}],
            },
        },
        "WorkerRouteTable": {
            "Type": "AWS::EC2::RouteTable",
            "Properties": {
                "VpcId": cfg["vpc_id"],
                "Tags": [{"Key": "Name", "Value": cluster + "-workers"}],
            },
        },
        "WorkerSubnetAssociation": {
            "Type": "AWS::EC2::SubnetRouteTableAssociation",
            "Properties": {
                "SubnetId": ref("WorkerSubnet"),
                "RouteTableId": ref("WorkerRouteTable"),
            },
        },
        "WorkerDefaultRoute": {
            "Type": "AWS::EC2::Route",
            "Properties": {
                "RouteTableId": ref("WorkerRouteTable"),
                "DestinationCidrBlock": "0.0.0.0/0",
                "InstanceId": ref("SystemInstance"),
            },
        },
        "SystemInstance": {
            "Type": "AWS::EC2::Instance",
            "Properties": {
                "ImageId": cfg["ami_id"],
                "InstanceType": "t3.medium",
                "SourceDestCheck": False,
                "IamInstanceProfile": ref("SystemProfile"),
                "NetworkInterfaces": [
                    {
                        "DeviceIndex": "0",
                        "SubnetId": cfg["system_subnet_id"],
                        "AssociatePublicIpAddress": True,
                        "GroupSet": [
                            ref("ClusterSecurityGroup"),
                            ref("NatSecurityGroup"),
                        ],
                    }
                ],
                "MetadataOptions": {
                    "HttpEndpoint": "enabled",
                    "HttpTokens": "required",
                    "HttpPutResponseHopLimit": 1,
                },
                "CreditSpecification": {"CPUCredits": "standard"},
                "BlockDeviceMappings": [
                    {
                        "DeviceName": "/dev/sda1",
                        "Ebs": {
                            "VolumeSize": 30,
                            "VolumeType": "gp3",
                            "Encrypted": True,
                            "DeleteOnTermination": True,
                        },
                    }
                ],
                "Tags": [{"Key": "Name", "Value": cluster + "-system"}],
                "UserData": {
                    "Fn::Base64": "#!/bin/bash\nset -eu\nsystemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service\n"
                },
            },
        },
    }
    for label, protocol, port in [
        ("Api", "tcp", 6443),
        ("Kubelet", "tcp", 10250),
        ("Vxlan", "udp", 8472),
    ]:
        resources[label + "Ingress"] = {
            "Type": "AWS::EC2::SecurityGroupIngress",
            "Properties": {
                "GroupId": ref("ClusterSecurityGroup"),
                "SourceSecurityGroupId": ref("ClusterSecurityGroup"),
                "IpProtocol": protocol,
                "FromPort": port,
                "ToPort": port,
            },
        }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Experimental Cosmos k3s + shared system-node NAT + private Karpenter workers (no EKS)",
        "Resources": resources,
        "Outputs": {
            "SystemInstanceId": {"Value": ref("SystemInstance")},
            "SystemPrivateIp": {"Value": getatt("SystemInstance", "PrivateIp")},
            "SystemPublicIp": {"Value": getatt("SystemInstance", "PublicIp")},
            "WorkerSubnetId": {"Value": ref("WorkerSubnet")},
            "WorkerRouteTableId": {"Value": ref("WorkerRouteTable")},
            "NetworkingMode": {"Value": NETWORKING_MODE},
            "SecurityGroupId": {"Value": ref("ClusterSecurityGroup")},
            "WorkerInstanceProfileName": {"Value": ref("WorkerProfile")},
            "SnapshotRepository": {"Value": getatt("Snapshots", "RepositoryUri")},
        },
    }


def ec2_pages(ec2, operation, key, **kwargs):
    return [
        item
        for page in ec2.get_paginator(operation).paginate(**kwargs)
        for item in page[key]
    ]


def gateway_for_subnet(tables, subnet_id):
    explicit = [
        table
        for table in tables
        if any(a.get("SubnetId") == subnet_id for a in table.get("Associations", []))
    ]
    candidates = explicit or [
        table
        for table in tables
        if any(a.get("Main") for a in table.get("Associations", []))
    ]
    defaults = [
        route
        for table in candidates
        for route in table.get("Routes", [])
        if route.get("DestinationCidrBlock") == "0.0.0.0/0"
    ]
    if (
        len(candidates) == 1
        and len(defaults) == 1
        and defaults[0].get("State") == "active"
        and defaults[0].get("GatewayId", "").startswith("igw-")
    ):
        return defaults[0]["GatewayId"]
    return None


def peer_networks(ec2, vpc_id):
    peerings = {}
    for side in ("requester", "accepter"):
        for peer in ec2_pages(
            ec2,
            "describe_vpc_peering_connections",
            "VpcPeeringConnections",
            Filters=[{"Name": side + "-vpc-info.vpc-id", "Values": [vpc_id]}],
        ):
            peerings[peer["VpcPeeringConnectionId"]] = peer
    networks = []
    for peer in peerings.values():
        if peer.get("Status", {}).get("Code") not in (
            "active",
            "pending-acceptance",
            "provisioning",
        ):
            continue
        for side in ("RequesterVpcInfo", "AccepterVpcInfo"):
            remote = peer.get(side, {})
            if remote.get("VpcId") == vpc_id:
                continue
            for item in remote.get(
                "CidrBlockSet", [{"CidrBlock": remote.get("CidrBlock", "0.0.0.0/0")}]
            ):
                networks.append(ipaddress.ip_network(item["CidrBlock"]))
    return networks


def pick_worker_cidr(vpc, subnets, tables, peers):
    cluster = [ipaddress.ip_network(cidr) for cidr in ("10.42.0.0/16", "10.43.0.0/16")]
    ranges = sorted(
        [
            ipaddress.ip_network(item["CidrBlock"])
            for item in vpc.get("CidrBlockAssociationSet", [])
            if item.get("CidrBlockState", {}).get("State") == "associated"
        ]
    )
    if any(network.overlaps(reserved) for network in ranges for reserved in cluster):
        raise ValueError("VPC overlaps k3s pod/service CIDRs; use another VPC")
    blocked = (
        [ipaddress.ip_network(item["CidrBlock"]) for item in subnets] + cluster + peers
    )
    blocked.extend(
        ipaddress.ip_network(route["DestinationCidrBlock"])
        for table in tables
        for route in table.get("Routes", [])
        if route.get("DestinationCidrBlock") not in (None, "0.0.0.0/0")
        and route.get("GatewayId") != "local"
    )
    private = [
        ipaddress.ip_network(cidr)
        for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ]
    ranges = [
        network
        for network in ranges
        if any(network.subnet_of(item) for item in private)
    ]
    for prefix in range(24, 29):
        for network in ranges:
            if network.prefixlen > prefix:
                continue
            for candidate in network.subnets(new_prefix=prefix):
                if not any(candidate.overlaps(item) for item in blocked):
                    return str(candidate)
    raise ValueError(
        "No unused /24 through /28 worker CIDR found in this VPC; no subnet or VPC was created"
    )


def discover_network(ec2, args, previous=None):
    if previous is not None:
        for field in ("vpc_id", "system_subnet_id", "worker_subnet_cidr"):
            saved = previous.get(field)
            if not isinstance(saved, str) or (
                getattr(args, field) is not None and getattr(args, field) != saved
            ):
                raise ValueError(
                    "Resume network inputs differ from saved deployment-state.json"
                )
            setattr(args, field, saved)
        if args.availability_zone and args.availability_zone != previous.get(
            "worker_availability_zone"
        ):
            raise ValueError(
                "Resume availability zone differs from saved deployment-state.json"
            )
        validate_network_arguments(args)
        return
    complete = all(
        getattr(args, field) is not None
        for field in ("vpc_id", "system_subnet_id", "worker_subnet_cidr")
    )
    if complete and not args.availability_zone:
        return
    if args.vpc_id is None and args.system_subnet_id is not None:
        selected = ec2.describe_subnets(SubnetIds=[args.system_subnet_id])["Subnets"]
        if len(selected) != 1:
            raise ValueError("System subnet was not found")
        args.vpc_id = selected[0]["VpcId"]
    if args.vpc_id is None:
        defaults = ec2_pages(
            ec2,
            "describe_vpcs",
            "Vpcs",
            Filters=[{"Name": "is-default", "Values": ["true"]}],
        )
        defaults = [
            vpc
            for vpc in defaults
            if vpc.get("IsDefault") is True and vpc.get("State") == "available"
        ]
        if len(defaults) != 1:
            raise ValueError(
                "No unique available default VPC; pass --vpc-id or --system-subnet-id"
            )
        args.vpc_id = defaults[0]["VpcId"]
    vpcs = ec2.describe_vpcs(VpcIds=[args.vpc_id])["Vpcs"]
    if len(vpcs) != 1:
        raise ValueError("Selected VPC was not found")
    subnets = ec2_pages(
        ec2,
        "describe_subnets",
        "Subnets",
        Filters=[{"Name": "vpc-id", "Values": [args.vpc_id]}],
    )
    tables = ec2_pages(
        ec2,
        "describe_route_tables",
        "RouteTables",
        Filters=[{"Name": "vpc-id", "Values": [args.vpc_id]}],
    )
    if args.system_subnet_id is None:
        zone = args.availability_zone or args.region + "a"
        candidates = [
            item
            for item in subnets
            if item.get("State") == "available"
            and item.get("AvailableIpAddressCount", 0) > 0
            and item.get("AvailabilityZone") == zone
            and gateway_for_subnet(tables, item["SubnetId"])
        ]
        defaults = [item for item in candidates if item.get("DefaultForAz") is True]
        choices = defaults or candidates
        if len(choices) != 1:
            raise ValueError(
                "No unique public system subnet in "
                + zone
                + "; pass --system-subnet-id"
            )
        args.system_subnet_id = choices[0]["SubnetId"]
    system = [item for item in subnets if item.get("SubnetId") == args.system_subnet_id]
    if len(system) != 1 or (
        args.availability_zone
        and system[0].get("AvailabilityZone") != args.availability_zone
    ):
        raise ValueError(
            "System subnet does not match the selected VPC/availability zone"
        )
    if args.worker_subnet_cidr is None:
        args.worker_subnet_cidr = pick_worker_cidr(
            vpcs[0], subnets, tables, peer_networks(ec2, args.vpc_id)
        )
    validate_network_arguments(args)


def check_network(ec2, args, stack_id=None):
    selected = ec2.describe_subnets(SubnetIds=[args.system_subnet_id])["Subnets"]
    if (
        len(selected) != 1
        or selected[0]["VpcId"] != args.vpc_id
        or selected[0].get("State") != "available"
    ):
        raise ValueError("System subnet must be available in the selected VPC")
    system = selected[0]
    if not stack_id and system.get("AvailableIpAddressCount", 0) < 1:
        raise ValueError("System subnet has no available IPv4 addresses")
    worker = ipaddress.ip_network(args.worker_subnet_cidr)
    vpc = ec2.describe_vpcs(VpcIds=[args.vpc_id])["Vpcs"][0]
    vpc_cidrs = [
        item["CidrBlock"]
        for item in vpc.get("CidrBlockAssociationSet", [])
        if item.get("CidrBlockState", {}).get("State") == "associated"
    ]
    if not vpc_cidrs or not any(
        worker.subnet_of(ipaddress.ip_network(cidr)) for cidr in vpc_cidrs
    ):
        raise ValueError(
            "Worker CIDR must lie within an associated IPv4 CIDR of the VPC"
        )
    cluster_networks = [
        ipaddress.ip_network(cidr) for cidr in ("10.42.0.0/16", "10.43.0.0/16")
    ]
    if any(
        ipaddress.ip_network(cidr).overlaps(network)
        for cidr in vpc_cidrs
        for network in cluster_networks
    ):
        raise ValueError(
            "VPC overlaps k3s pod/service CIDRs 10.42/16 or 10.43/16; use another VPC"
        )
    dns = ec2.describe_vpc_attribute(VpcId=args.vpc_id, Attribute="enableDnsSupport")
    if not dns["EnableDnsSupport"]["Value"]:
        raise ValueError("The VPC must have DNS support enabled")
    subnets = ec2_pages(
        ec2,
        "describe_subnets",
        "Subnets",
        Filters=[{"Name": "vpc-id", "Values": [args.vpc_id]}],
    )
    for subnet in subnets:
        if not worker.overlaps(ipaddress.ip_network(subnet["CidrBlock"])):
            continue
        tags = {tag["Key"]: tag["Value"] for tag in subnet.get("Tags", [])}
        owned = (
            stack_id
            and tags.get("aws:cloudformation:stack-id") == stack_id
            and tags.get("aws:cloudformation:logical-id") == "WorkerSubnet"
            and subnet["CidrBlock"] == str(worker)
            and subnet["AvailabilityZone"] == system["AvailabilityZone"]
            and subnet.get("MapPublicIpOnLaunch") is False
        )
        if not owned:
            raise ValueError(
                "Worker CIDR overlaps an existing subnet; choose an unused CIDR"
            )
    tables = ec2_pages(
        ec2,
        "describe_route_tables",
        "RouteTables",
        Filters=[{"Name": "vpc-id", "Values": [args.vpc_id]}],
    )
    gateway_id = gateway_for_subnet(tables, args.system_subnet_id)
    if not gateway_id:
        raise ValueError(
            "System subnet needs an active default route directly to an internet gateway"
        )
    gateways = ec2.describe_internet_gateways(InternetGatewayIds=[gateway_id])[
        "InternetGateways"
    ]
    if not any(
        a.get("VpcId") == args.vpc_id and a.get("State") == "available"
        for gateway in gateways
        for a in gateway.get("Attachments", [])
    ):
        raise ValueError("The internet gateway must be attached to the selected VPC")
    for table in tables:
        for route in table.get("Routes", []):
            cidr = route.get("DestinationCidrBlock")
            if cidr and cidr != "0.0.0.0/0" and route.get("GatewayId") != "local":
                network = ipaddress.ip_network(cidr)
                if worker.overlaps(network) or any(
                    network.overlaps(n) for n in cluster_networks
                ):
                    raise ValueError(
                        "Worker or k3s CIDR overlaps a nonlocal route; resolve connected-network conflicts"
                    )
    for network in peer_networks(ec2, args.vpc_id):
        if worker.overlaps(network) or any(
            network.overlaps(n) for n in cluster_networks
        ):
            raise ValueError("Worker or k3s CIDR overlaps a peered VPC")
    return {
        "worker_availability_zone": system["AvailabilityZone"],
        "vpc_cidrs": vpc_cidrs,
        "internet_gateway_id": gateway_id,
    }


def inspect_capacity(ec2, args, network):
    types = ec2.describe_instance_types(
        InstanceTypes=["t3.medium"] + args.worker_instance_types
    )["InstanceTypes"]
    workers = {
        item["InstanceType"]: item
        for item in types
        if item["InstanceType"] in args.worker_instance_types
    }
    for name, item in workers.items():
        if "nested-virtualization" not in item["ProcessorInfo"].get(
            "SupportedFeatures", []
        ):
            raise ValueError(
                name
                + " does not advertise nested virtualization (or the CloudShell AWS SDK is too old)"
            )
        if "x86_64" not in item["ProcessorInfo"]["SupportedArchitectures"]:
            raise ValueError(name + " must support x86_64")
        if item["VCpuInfo"]["DefaultVCpus"] > args.worker_max_vcpu:
            raise ValueError(name + " exceeds --worker-max-vcpu")
        # CPU requests exclude reserved capacity and VM/runtime overhead.
        if (
            item["VCpuInfo"]["DefaultVCpus"] < 4
            or item["MemoryInfo"]["SizeInMiB"] < 8192
        ):
            raise ValueError(
                name + " is too small for this initial 2-vCPU/4-GiB session profile"
            )
        if args.session_profile == "eight" and item["MemoryInfo"]["SizeInMiB"] < 16384:
            raise ValueError(
                name + " needs at least 16 GiB RAM for the eight-session packing target"
            )
    offers = []
    paginator = ec2.get_paginator("describe_instance_type_offerings")
    for page in paginator.paginate(
        LocationType="availability-zone",
        Filters=[
            {
                "Name": "instance-type",
                "Values": ["t3.medium"] + args.worker_instance_types,
            }
        ],
    ):
        offers.extend(page["InstanceTypeOfferings"])
    offered = {(item["InstanceType"], item["Location"]) for item in offers}
    zone = network["worker_availability_zone"]
    if ("t3.medium", zone) not in offered:
        raise ValueError("t3.medium is not offered in the system subnet AZ")
    for name in args.worker_instance_types:
        if (name, zone) not in offered:
            raise ValueError(name + " is not offered in the system/private-worker AZ")
    print(
        "Instance offerings and nested-virtualization capability checked; capacity and quotas are not guaranteed."
    )


def resolve_ami(ec2, requested):
    if requested:
        images = ec2.describe_images(ImageIds=[requested], Owners=["099720109477"])[
            "Images"
        ]
    else:
        images = ec2.describe_images(
            Owners=["099720109477"],
            Filters=[
                {
                    "Name": "name",
                    "Values": [
                        "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
                    ],
                },
                {"Name": "state", "Values": ["available"]},
                {"Name": "architecture", "Values": ["x86_64"]},
            ],
        )["Images"]
    images = [
        item
        for item in images
        if item.get("Architecture") == "x86_64"
        and item.get("State") == "available"
        and item.get("RootDeviceType") == "ebs"
        and item.get("RootDeviceName") == "/dev/sda1"
        and "ubuntu-noble-24.04-amd64-server-" in item.get("Name", "")
    ]
    if not images:
        raise ValueError("No matching official Canonical Ubuntu 24.04 amd64 AMI")
    return max(images, key=lambda item: item["CreationDate"])["ImageId"]


def find_stack(cfn, name):
    from botocore.exceptions import ClientError

    try:
        return cfn.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as error:
        if (
            error.response["Error"]["Code"] == "ValidationError"
            and "does not exist" in error.response["Error"]["Message"]
        ):
            return None
        raise


def put_portal_if_missing(ssm, name):
    # A metadata-only list avoids reading a pre-existing token into CloudShell.
    metadata = ssm.describe_parameters(
        ParameterFilters=[{"Key": "Name", "Option": "Equals", "Values": [name]}]
    )
    if metadata["Parameters"]:
        print(
            "Using the existing portal-token parameter; no token was read into CloudShell."
        )
        return
    token = getpass.getpass(
        "Cosmos service-account API token (hidden; never stored locally): "
    )
    if not token.strip():
        raise ValueError("Token cannot be empty")
    try:
        ssm.put_parameter(
            Name=name,
            Type="SecureString",
            Value=token,
            Overwrite=False,
            Description="Cosmos portal token; read only by the system instance role",
        )
    finally:
        token = None


def bootstrap_commands(config):
    commands = [
        "set +x",
        "set -eu",
        "umask 077",
        "install -d -m 700 /opt/cosmos-bootstrap",
    ]
    payloads = {filename: (BASE / filename).read_bytes() for filename in ASSETS}
    payloads["config.json"] = json.dumps(config).encode()
    for filename, data in payloads.items():
        encoded = base64.b64encode(gzip.compress(data, mtime=0)).decode()
        path = "/opt/cosmos-bootstrap/" + filename
        commands.append(
            "printf %s "
            + shlex.quote(encoded)
            + " | base64 -d | gzip -d > "
            + shlex.quote(path)
        )
        commands.append("chmod 700 " + shlex.quote(path))
    commands.append("/bin/bash /opt/cosmos-bootstrap/system-bootstrap.sh")
    if len(json.dumps(commands).encode()) > 60000:
        raise ValueError("Bootstrap exceeds the conservative 60-KB SSM request budget")
    return commands


def validate_resume(config, previous, existing):
    keys = (
        "networking_mode",
        "region",
        "account_id",
        "stack_name",
        "provider",
        "portal_address",
        "vpc_id",
        "system_subnet_id",
        "worker_subnet_cidr",
        "worker_availability_zone",
        "worker_instance_types",
        "worker_max_vcpu",
        "worker_disk_gb",
        "session_profile",
        "portal_parameter",
        "agent_parameter",
        "ami_id",
        "internet_gateway_id",
        "vpc_cidrs",
        *PINS,
    )
    tags = {tag["Key"]: tag["Value"] for tag in existing.get("Tags", [])}
    if tags.get("cosmos-networking") != NETWORKING_MODE or any(
        previous.get(key) != config.get(key) for key in keys
    ):
        raise ValueError(
            "Resume inputs/networking mode differ; older bundles cannot be migrated with --resume"
        )


def check_created_network(ec2, config, outputs):
    if outputs.get("NetworkingMode") != NETWORKING_MODE:
        raise ValueError("Stack networking mode differs; refusing bootstrap")
    instances = ec2.describe_instances(InstanceIds=[outputs["SystemInstanceId"]])[
        "Reservations"
    ]
    nodes = [item for reservation in instances for item in reservation["Instances"]]
    if (
        len(nodes) != 1
        or nodes[0].get("SourceDestCheck") is not False
        or not nodes[0].get("PublicIpAddress")
        or nodes[0]["SubnetId"] != config["system_subnet_id"]
        or nodes[0]["PrivateIpAddress"] != outputs["SystemPrivateIp"]
        or len(nodes[0].get("NetworkInterfaces", [])) != 1
    ):
        raise ValueError(
            "System node must have one interface, public IPv4 and source/destination checks disabled"
        )
    worker = ec2.describe_subnets(SubnetIds=[outputs["WorkerSubnetId"]])["Subnets"]
    if (
        len(worker) != 1
        or worker[0]["VpcId"] != config["vpc_id"]
        or worker[0]["CidrBlock"] != config["worker_subnet_cidr"]
        or worker[0]["AvailabilityZone"] != config["worker_availability_zone"]
        or worker[0].get("MapPublicIpOnLaunch") is not False
    ):
        raise ValueError("Created worker subnet differs from the reviewed plan")
    tables = ec2.describe_route_tables(RouteTableIds=[outputs["WorkerRouteTableId"]])[
        "RouteTables"
    ]
    if (
        len(tables) != 1
        or not any(
            a.get("SubnetId") == outputs["WorkerSubnetId"]
            for a in tables[0].get("Associations", [])
        )
        or not any(
            route.get("DestinationCidrBlock") == "0.0.0.0/0"
            and route.get("InstanceId") == outputs["SystemInstanceId"]
            and route.get("State") == "active"
            for route in tables[0]["Routes"]
        )
    ):
        raise ValueError(
            "Worker subnet must use its dedicated route table and system-node default route"
        )


def print_progress(stage, status, started):
    elapsed = max(0, int(time.monotonic() - started))
    print(
        f"{stage}: {status}; elapsed {elapsed // 60:02d}:{elapsed % 60:02d}", flush=True
    )


def wait_stack(cfn, stack_id, resource_names):
    started = time.monotonic()
    expected = set(resource_names)
    if not expected:
        raise ValueError("Expected CloudFormation resource list cannot be empty")
    details_available = True
    print(
        "[1/3] CloudFormation: waiting for resource status (40-minute polling budget).",
        flush=True,
    )
    for _ in range(160):
        stack = cfn.describe_stacks(StackName=stack_id)["Stacks"][0]
        status = stack["StackStatus"]
        if status not in (
            "CREATE_IN_PROGRESS",
            "CREATE_COMPLETE",
            "CREATE_FAILED",
            "REVIEW_IN_PROGRESS",
            "ROLLBACK_IN_PROGRESS",
            "ROLLBACK_FAILED",
            "ROLLBACK_COMPLETE",
            "DELETE_IN_PROGRESS",
            "DELETE_FAILED",
            "DELETE_COMPLETE",
        ):
            raise ValueError(
                "Unexpected CloudFormation status; inspect the stack privately"
            )
        detail = "resource counter unavailable"
        failed = []
        if details_available:
            try:
                resources = [
                    item
                    for page in cfn.get_paginator("list_stack_resources").paginate(
                        StackName=stack_id
                    )
                    for item in page["StackResourceSummaries"]
                    if item["LogicalResourceId"] in expected
                ]
            except Exception as error:
                # Optional resource counts must not block a deployment on missing read permissions.
                if not getattr(error, "response", {}).get("Error", {}).get("Code"):
                    raise
                details_available = False
            else:
                complete = len(
                    {
                        item["LogicalResourceId"]
                        for item in resources
                        if item["ResourceStatus"] == "CREATE_COMPLETE"
                    }
                )
                filled = 20 * complete // len(expected)
                detail = (
                    "["
                    + "#" * filled
                    + "-" * (20 - filled)
                    + f"] {complete}/{len(expected)} resources"
                )
                failed = sorted(
                    {
                        item["LogicalResourceId"]
                        for item in resources
                        if item["ResourceStatus"].endswith("_FAILED")
                    }
                )
        if failed:
            detail += "; failed resources: " + ", ".join(failed)
        print_progress("[1/3] CloudFormation", status + "; " + detail, started)
        if status == "CREATE_COMPLETE":
            return stack
        if status not in ("CREATE_IN_PROGRESS", "REVIEW_IN_PROGRESS"):
            raise RuntimeError(
                "CloudFormation status "
                + status
                + "; resources may remain billable. Inspect stack events privately; do not rerun --apply."
            )
        time.sleep(15)
    raise TimeoutError(
        "CloudFormation polling timed out; AWS may still be creating billable resources. Inspect stack status before retrying."
    )


def wait_ssm(ssm, instance_id):
    started = time.monotonic()
    print(
        "[2/3] Systems Manager: waiting for agent registration (20-minute polling budget).",
        flush=True,
    )
    for _ in range(120):
        result = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )
        online = any(
            item["PingStatus"] == "Online" for item in result["InstanceInformationList"]
        )
        print_progress(
            "[2/3] Systems Manager",
            "Online" if online else "waiting for agent",
            started,
        )
        if online:
            return
        time.sleep(10)
    raise TimeoutError(
        "System instance did not register with SSM; check public egress, IAM and Ubuntu SSM agent"
    )


def safe_ssm_status(ssm, command_id, instance_id):
    # Never display StandardOutputContent or StandardErrorContent: remote diagnostics can contain credentials.
    result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
    status = result["Status"]
    if status not in (
        "Pending",
        "InProgress",
        "Delayed",
        "Success",
        "Cancelled",
        "TimedOut",
        "Failed",
        "Cancelling",
    ):
        raise ValueError("Unexpected Systems Manager status; inspect it privately")
    return status


def wait_bootstrap(ssm, command_id, instance_id):
    started = time.monotonic()
    print(
        "[3/3] Bootstrap: waiting for remote command (60-minute polling budget).",
        flush=True,
    )
    for _ in range(360):
        try:
            status = safe_ssm_status(ssm, command_id, instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            print_progress(
                "[3/3] Bootstrap", "waiting for command registration", started
            )
        else:
            print_progress("[3/3] Bootstrap", status, started)
            if status == "Success":
                return
            if status in ("Failed", "Cancelled", "TimedOut", "Cancelling"):
                raise RuntimeError(
                    "Bootstrap status "
                    + status
                    + "; resources remain billable. Inspect service STATUS privately and use --resume after fixing."
                )
        time.sleep(10)
    raise TimeoutError(
        "Bootstrap is still running; inspect command STATUS in Systems Manager before resuming"
    )


def main():
    args = args_parser().parse_args()
    validate_arguments(args)
    os.umask(0o077)
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise RuntimeError(
            "Run in AWS CloudShell with its supplied Python boto3 package"
        ) from None
    session = boto3.Session(region_name=args.region)
    retry = Config(
        retries={"mode": "standard", "max_attempts": 8},
        connect_timeout=10,
        read_timeout=60,
    )
    clients = {
        name: session.client(name, config=retry)
        for name in ("ec2", "sts", "ssm", "cloudformation")
    }
    ec2, ssm, cfn = clients["ec2"], clients["ssm"], clients["cloudformation"]
    identity = clients["sts"].get_caller_identity()
    account = identity["Account"]
    if not identity["Arn"].startswith("arn:aws:"):
        raise ValueError("This starter currently targets AWS commercial regions only")
    existing = find_stack(cfn, args.stack_name)
    state = BASE / "deployment-state.json"
    previous = None
    if existing and not args.resume:
        raise ValueError(
            "Stack exists; inspect it, then use --apply --resume with the SAME bundle and inputs"
        )
    if args.resume:
        if not existing or existing["StackStatus"] not in (
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
        ):
            raise ValueError(
                "--resume requires an existing CREATE_COMPLETE or UPDATE_COMPLETE stack"
            )
        if not state.exists():
            raise ValueError("Resume requires this bundle's deployment-state.json")
        previous = json.loads(state.read_text())
        if previous.get("networking_mode") != NETWORKING_MODE:
            raise ValueError(
                "Older deployment state cannot be resumed as system-node NAT"
            )
    discover_network(ec2, args, previous)
    network = check_network(ec2, args, existing["StackId"] if existing else None)
    inspect_capacity(ec2, args, network)
    ami_id = resolve_ami(ec2, args.ami_id or (previous["ami_id"] if previous else None))
    config = {
        **PINS,
        "region": args.region,
        "account_id": account,
        "stack_name": args.stack_name,
        "provider": args.provider,
        "portal_address": args.portal_address,
        "vpc_id": args.vpc_id,
        "system_subnet_id": args.system_subnet_id,
        "worker_subnet_cidr": args.worker_subnet_cidr,
        "networking_mode": NETWORKING_MODE,
        **network,
        "worker_instance_types": args.worker_instance_types,
        "worker_max_vcpu": args.worker_max_vcpu,
        "worker_disk_gb": args.worker_disk_gb,
        "session_profile": args.session_profile,
        "ami_id": ami_id,
        "portal_parameter": "/cosmos/" + args.stack_name + "/portal-token",
        "agent_parameter": "/cosmos/" + args.stack_name + "/agent-token",
    }
    if previous is not None:
        validate_resume(config, previous, existing)
        if previous.get("ssm_command_id"):
            status = safe_ssm_status(
                ssm, previous["ssm_command_id"], previous["system_instance_id"]
            )
            if status not in ("Success", "Failed", "Cancelled", "TimedOut"):
                raise ValueError(
                    "Previous bootstrap is still active; inspect its STATUS before resuming"
                )
    template = stack_template(config)
    template_text = json.dumps(template, indent=2)
    if len(template_text.encode()) > 51200:
        raise ValueError("CloudFormation inline template exceeds 51,200 bytes")
    (BASE / "infrastructure.generated.json").write_text(template_text)
    print(
        f"Account {account}; region {args.region}; stack {args.stack_name}; AMI {ami_id}"
    )
    print(
        "Create: one public-IPv4 t3.medium / 30-GiB gp3, private worker subnet/route table, ECR, IAM, security groups, two SecureStrings."
    )
    print(
        "Workers stay private. System node forwards worker HTTP/HTTPS; no public SSH or Kubernetes API ingress."
    )
    print(
        "No EKS, separate NAT instance, managed NAT gateway, load balancer, VPC or internet gateway is created."
    )
    print(
        "Selected VPC: "
        + config["vpc_id"]
        + "; public system subnet: "
        + config["system_subnet_id"]
    )
    print(
        "New worker subnet: "
        + config["worker_subnet_cidr"]
        + " in "
        + config["worker_availability_zone"]
    )
    print(
        "CIDR selection checks visible AWS inventory, not unpublished organizational/IPAM reservations."
    )
    print(
        "Existing public subnet, main route table and internet-gateway route are NOT modified."
    )
    print(
        "Session workers: On-Demand, "
        + ", ".join(args.worker_instance_types)
        + "; eventual vCPU limit "
        + str(args.worker_max_vcpu)
    )
    print(
        "Session profile: "
        + args.session_profile
        + "; worker disk: "
        + str(args.worker_disk_gb)
        + " GiB."
    )
    print(
        "Reduced scheduler requests do not reduce VM limits; simultaneous heavy workloads can exhaust the host."
    )
    if args.session_profile == "eight":
        print(
            "Eight-session packing is an untested target for light workloads, not a concurrency guarantee."
        )
    print(
        "The worker limit is eventually consistent, not a strict cost cap; use AWS Budgets and service quotas."
    )
    print(
        "The system public IPv4, data transfer, workers/disks, ECR growth and Cosmos usage incur additional charges."
    )
    if not args.apply:
        print(
            "READ-ONLY PLAN COMPLETE. Review infrastructure.generated.json; rerun with --apply to create resources."
        )
        return
    print(
        "This is an experimental single-node deployment, NOT a tested or highly available production blueprint."
    )
    if (
        input("Type DEPLOY " + account + " to authorize paid AWS resource creation: ")
        != "DEPLOY " + account
    ):
        raise ValueError("Confirmation did not match; no resources were created")
    if not existing:
        # Save nonsecret recovery state before making any AWS writes.
        state.write_text(json.dumps(config, indent=2))
    put_portal_if_missing(ssm, config["portal_parameter"])
    if not existing:
        print(
            "Creating CloudFormation stack. Failed resources are preserved for diagnosis and may incur charges.",
            flush=True,
        )
        created = cfn.create_stack(
            StackName=args.stack_name,
            TemplateBody=template_text,
            Capabilities=["CAPABILITY_IAM"],
            OnFailure="DO_NOTHING",
            Tags=[
                {"Key": "cosmos-bootstrap", "Value": args.stack_name},
                {"Key": "cosmos-networking", "Value": NETWORKING_MODE},
            ],
        )
        stack = wait_stack(cfn, created["StackId"], template["Resources"])
    else:
        print("[1/3] CloudFormation: reusing the existing COMPLETE stack.", flush=True)
        stack = cfn.describe_stacks(StackName=existing["StackId"])["Stacks"][0]
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack["Outputs"]}
    check_created_network(ec2, config, outputs)
    config.update(
        {
            "system_private_ip": outputs["SystemPrivateIp"],
            "system_public_ip": outputs["SystemPublicIp"],
            "worker_subnet_ids": [outputs["WorkerSubnetId"]],
            "worker_route_table_id": outputs["WorkerRouteTableId"],
            "security_group_id": outputs["SecurityGroupId"],
            "worker_instance_profile_name": outputs["WorkerInstanceProfileName"],
            "ecr_repository": outputs["SnapshotRepository"],
        }
    )
    state.write_text(json.dumps(config, indent=2))
    instance_id = outputs["SystemInstanceId"]
    print("Waiting for outbound Systems Manager connectivity; instance " + instance_id)
    wait_ssm(ssm, instance_id)
    result = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Comment="Install shared system-node NAT and private Cosmos k3s bootstrap",
        TimeoutSeconds=600,
        Parameters={
            "commands": bootstrap_commands(config),
            "executionTimeout": ["3600"],
        },
    )
    command_id = result["Command"]["CommandId"]
    config["system_instance_id"] = instance_id
    config["ssm_command_id"] = command_id
    state.write_text(json.dumps(config, indent=2))
    print(
        "Bootstrap command "
        + command_id
        + ". Waiting up to 60 minutes; closing CloudShell does not stop AWS resources."
    )
    wait_bootstrap(ssm, command_id, instance_id)
    print(
        "BOOTSTRAP SUCCEEDED. Infrastructure and controllers are installed; no session lifecycle has been verified."
    )
    print(
        "Finish the Cosmos environment setup and perform the acceptance checks in the guide."
    )
    print(
        "Recovery metadata: deployment-state.json. Never export kubeconfig, agent tokens, or raw SSM output to chat."
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nInterrupted. Any created AWS resources and remote bootstrap remain active.",
            file=sys.stderr,
        )
        sys.exit(130)
    except Exception as error:
        # Raw AWS errors and remote output are intentionally not printed.
        if isinstance(error, (ValueError, RuntimeError, TimeoutError)):
            print(str(error), file=sys.stderr)
        else:
            code = (
                getattr(error, "response", {})
                .get("Error", {})
                .get("Code", type(error).__name__)
            )
            print(
                "Deployment failed ("
                + code
                + "); inspect AWS status privately. Existing resources are not deleted.",
                file=sys.stderr,
            )
        sys.exit(1)
