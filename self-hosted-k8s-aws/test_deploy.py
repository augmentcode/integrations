"""Offline regression checks. No AWS clients, credentials or paid resources."""

import argparse
import ast
import base64
import gzip
import importlib.util
import io
import json
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import deploy

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "configure_cluster", BASE / "configure-cluster.py"
)
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


def fixture():
    return {
        **deploy.PINS,
        "region": "us-east-1",
        "account_id": "123456789012",
        "stack_name": "cosmos-private",
        "provider": "my-k8s-0",
        "portal_address": "gw.cosmos.augmentcode.com:443",
        "vpc_id": "vpc-abcdef",
        "system_subnet_id": "subnet-aaa",
        "worker_subnet_ids": ["subnet-bbb"],
        "networking_mode": deploy.NETWORKING_MODE,
        "worker_subnet_cidr": "172.31.128.0/24",
        "worker_availability_zone": "us-east-1a",
        "vpc_cidrs": ["172.31.0.0/16"],
        "internet_gateway_id": "igw-abc",
        "system_public_ip": "198.51.100.10",
        "worker_instance_types": ["m8i-flex.xlarge"],
        "worker_max_vcpu": 16,
        "worker_disk_gb": 100,
        "session_profile": "default",
        "ami_id": "ami-abcdef",
        "system_private_ip": "172.31.1.10",
        "security_group_id": "sg-abcdef",
        "worker_instance_profile_name": "test-worker-profile",
        "portal_parameter": "/cosmos/cosmos-private/portal-token",
        "agent_parameter": "/cosmos/cosmos-private/agent-token",
        "ecr_repository": "123456789012.dkr.ecr.us-east-1.amazonaws.com/cosmos-private-snapshots",
    }


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.old_base = configure.BASE
        configure.BASE = BASE

    def tearDown(self):
        configure.BASE = self.old_base

    def test_plan_is_default_and_resume_requires_apply(self):
        args = deploy.args_parser().parse_args(
            [
                "--region",
                "us-east-1",
                "--vpc-id",
                "vpc-abcdef",
                "--system-subnet-id",
                "subnet-aaa",
                "--worker-subnet-cidr",
                "172.31.128.0/24",
                "--provider",
                "my-k8s-0",
            ]
        )
        self.assertFalse(args.apply)
        args.resume = True
        with self.assertRaisesRegex(ValueError, "also requires"):
            deploy.validate_arguments(args)

    def test_one_public_system_nat_private_workers_and_retained_snapshots(self):
        template = deploy.stack_template(fixture())
        resources = template["Resources"]
        self.assertFalse(
            any(item["Type"].startswith("AWS::EKS::") for item in resources.values())
        )
        nodes = [
            item["Properties"]
            for item in resources.values()
            if item["Type"] == "AWS::EC2::Instance"
        ]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["InstanceType"], "t3.medium")
        self.assertTrue(nodes[0]["NetworkInterfaces"][0]["AssociatePublicIpAddress"])
        self.assertFalse(nodes[0]["SourceDestCheck"])
        self.assertEqual(len(nodes[0]["NetworkInterfaces"]), 1)
        self.assertIn(
            deploy.ref("NatSecurityGroup"), nodes[0]["NetworkInterfaces"][0]["GroupSet"]
        )
        self.assertFalse(resources["WorkerSubnet"]["Properties"]["MapPublicIpOnLaunch"])
        route = resources["WorkerDefaultRoute"]["Properties"]
        self.assertEqual(route["InstanceId"], deploy.ref("SystemInstance"))
        self.assertEqual(route["RouteTableId"], deploy.ref("WorkerRouteTable"))
        self.assertEqual(
            resources["WorkerSubnetAssociation"]["Properties"]["SubnetId"],
            deploy.ref("WorkerSubnet"),
        )
        self.assertEqual(
            resources["WorkerSubnet"]["Properties"]["AvailabilityZone"], "us-east-1a"
        )
        self.assertFalse(
            any(
                item["Type"]
                in (
                    "AWS::EC2::VPC",
                    "AWS::EC2::NatGateway",
                    "AWS::EC2::InternetGateway",
                )
                for item in resources.values()
            )
        )
        nat_ingress = resources["NatSecurityGroup"]["Properties"][
            "SecurityGroupIngress"
        ]
        self.assertEqual({rule["FromPort"] for rule in nat_ingress}, {80, 443})
        self.assertTrue(
            all(
                rule["CidrIp"] == fixture()["worker_subnet_cidr"]
                for rule in nat_ingress
            )
        )
        self.assertEqual(nodes[0]["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"], 30)
        self.assertEqual(nodes[0]["CreditSpecification"]["CPUCredits"], "standard")
        self.assertEqual(resources["Snapshots"]["DeletionPolicy"], "Retain")
        self.assertLess(len(json.dumps(template).encode()), 51200)
        self.assertNotIn(
            "0.0.0.0/0",
            json.dumps([v for k, v in resources.items() if k.endswith("Ingress")]),
        )

    def test_only_agent_token_is_readable_by_worker_role(self):
        resources = deploy.stack_template(fixture())["Resources"]
        text = json.dumps(resources["WorkerRole"])
        self.assertIn("/agent-token", text)
        self.assertNotIn("/portal-token", text)
        self.assertNotIn("ecr:", text)
        system = resources["SystemRole"]["Properties"]["Policies"][0]["PolicyDocument"][
            "Statement"
        ]
        pass_role = next(item for item in system if item["Action"] == ["iam:PassRole"])
        self.assertEqual(pass_role["Resource"], deploy.getatt("WorkerRole", "Arn"))
        terminate = next(
            item for item in system if "ec2:TerminateInstances" in item["Action"]
        )
        self.assertIn(
            "ec2:ResourceTag/kubernetes.io/cluster/cosmos-private",
            terminate["Condition"]["StringEquals"],
        )

    def test_worker_scale_zero_contract_and_active_session_protection(self):
        configure.validate_config(fixture())
        node_class, pool = configure.capacity_objects(fixture())
        self.assertEqual(node_class["spec"]["amiFamily"], "Custom")
        self.assertFalse(node_class["spec"]["associatePublicIPAddress"])
        self.assertEqual(
            node_class["spec"]["subnetSelectorTerms"], [{"id": "subnet-bbb"}]
        )
        self.assertEqual(
            node_class["spec"]["securityGroupSelectorTerms"], [{"id": "sg-abcdef"}]
        )
        self.assertEqual(
            node_class["spec"]["cpuOptions"]["nestedVirtualization"], "enabled"
        )
        self.assertEqual(
            node_class["spec"]["metadataOptions"]["httpPutResponseHopLimit"], 1
        )
        self.assertNotIn("replicas", pool["spec"])
        self.assertEqual(pool["spec"]["template"]["spec"]["expireAfter"], "Never")
        self.assertEqual(pool["spec"]["disruption"]["consolidationPolicy"], "WhenEmpty")
        self.assertEqual(
            pool["spec"]["disruption"]["budgets"],
            [{"nodes": "0", "reasons": ["Drifted"]}],
        )
        userdata = node_class["spec"]["userData"]
        self.assertLess(len(userdata.encode()), 16384)
        self.assertNotIn("__COSMOS_CONFIG_B64__", userdata)
        self.assertIn("karpenter.sh/unregistered:NoExecute", userdata)
        result = subprocess.run(
            ["bash", "-n"], input=userdata.encode(), capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_management_placement_and_guest_control_plane_deny(self):
        values = configure.karpenter_values(fixture(), "PUBLIC-CA-FIXTURE")
        self.assertEqual(values["replicas"], 1)
        self.assertFalse(values["settings"]["eksControlPlane"])
        self.assertTrue(values["hostNetwork"])
        self.assertEqual(values["nodeSelector"][configure.ROLE_KEY], "system")
        cosmos = configure.cosmos_values(fixture(), "10.43.0.1")
        self.assertEqual(cosmos["controller"]["replicas"], 1)
        self.assertIn("172.31.1.10/32", cosmos["sandbox"]["network"]["deny"])
        self.assertIn("198.51.100.10/32", cosmos["sandbox"]["network"]["deny"])
        self.assertIn("10.43.0.1/32", cosmos["sandbox"]["network"]["deny"])
        for key in ("controllerAuthSecret", "sandboxAuthSecret"):
            self.assertEqual(cosmos["registry"][key]["source"], "existing")

    def test_cosmos_image_is_pinned_independently_of_the_chart_version(self):
        cfg = fixture()
        image = configure.cosmos_values(cfg, "10.43.0.1")["image"]
        self.assertEqual(image["tag"], "latest")
        self.assertEqual(image["repository"], "dist.augmentcode.com/cosmos/cosmos-sh")
        self.assertRegex(image["digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn(cfg["cosmos_chart_version"], json.dumps(image))
        changed = {**cfg, "cosmos_chart_version": "0.20261025.1"}
        self.assertEqual(image, configure.cosmos_values(changed, "10.43.0.1")["image"])

    def test_ssm_payload_contains_only_assets_and_nonsecret_config(self):
        commands = deploy.bootstrap_commands(fixture())
        self.assertEqual(commands[0], "set +x")
        self.assertIn("system-bootstrap.sh", commands[-1])
        self.assertLess(len(json.dumps(commands).encode()), 60000)
        payloads = {
            filename: (BASE / filename).read_bytes() for filename in deploy.ASSETS
        }
        payloads["config.json"] = json.dumps(fixture()).encode()
        writes = [
            shlex.split(command)
            for command in commands
            if command.startswith("printf %s ")
        ]
        self.assertEqual(len(writes), len(payloads))
        for command in writes:
            self.assertEqual(
                command[3:10], ["|", "base64", "-d", "|", "gzip", "-d", ">"]
            )
            filename = Path(command[10]).name
            self.assertEqual(
                gzip.decompress(base64.b64decode(command[2], validate=True)),
                payloads[filename],
            )

    def test_post_renderer_sets_real_deployment_node_selector(self):
        import yaml

        documents = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "cosmos-controller", "namespace": "cosmos-ctrl"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {"name": "controller", "command": ["cosmos", "k8s"]}
                            ]
                        }
                    }
                },
            }
        ]
        result = subprocess.run(
            ["python3", str(BASE / "configure-cluster.py"), "--post-render"],
            input=yaml.safe_dump_all(documents).encode(),
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        rendered = yaml.safe_load(result.stdout)
        self.assertEqual(
            rendered["spec"]["template"]["spec"]["nodeSelector"][configure.ROLE_KEY],
            "system",
        )
        invalid = subprocess.run(
            ["python3", str(BASE / "configure-cluster.py"), "--post-render"],
            input=b"apiVersion: v1\nkind: ConfigMap\n",
            capture_output=True,
        )
        self.assertNotEqual(invalid.returncode, 0)

    def test_secure_node_agent_token_is_accepted_without_allowing_server_token(self):
        agent = "K10" + "a" * 64 + "::node:dummy-agent-password"
        server = "K10" + "a" * 64 + "::server:dummy-server-password"
        for filename in ("system-bootstrap.sh", "worker-bootstrap.sh"):
            script = (BASE / filename).read_text()
            embedded = script.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
            tree = ast.parse(embedded)
            guard = next(
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name == "validate_agent_token"
            )
            namespace = {}
            exec(
                compile(ast.Module(body=[guard], type_ignores=[]), filename, "exec"),
                namespace,
            )
            validate = namespace["validate_agent_token"]
            extra = [server] if filename == "system-bootstrap.sh" else []
            validate(agent, *extra)
            for invalid in (
                server,
                "short-token",
                agent.replace("::node:", "::agent:"),
            ):
                with self.assertRaises(ValueError):
                    validate(invalid, *extra)
            if extra:
                with self.assertRaises(ValueError):
                    validate(agent, agent.replace("::node:", "::server:"))

    def test_resume_rejects_old_topology_changed_inputs_and_unowned_stack(self):
        cfg = fixture()
        stack = {
            "Tags": [{"Key": "cosmos-networking", "Value": deploy.NETWORKING_MODE}]
        }
        deploy.validate_resume(cfg, cfg.copy(), stack)
        for changes in (
            {"networking_mode": "public-ip"},
            {"worker_subnet_cidr": "172.31.129.0/24"},
            {"worker_disk_gb": 200},
            {"session_profile": "eight"},
            {"k3s_version": "v1.36.1+k3s1"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                deploy.validate_resume(cfg, {**cfg, **changes}, stack)
        with self.assertRaises(ValueError):
            deploy.validate_resume(cfg, cfg.copy(), {"Tags": []})

    def test_readonly_plan_never_calls_a_mutating_client(self):
        for automatic in (False, True):
            with (
                self.subTest(automatic=automatic),
                tempfile.TemporaryDirectory() as directory,
            ):
                sdk = types.ModuleType("boto3")
                clients = {
                    name: MagicMock()
                    for name in ("ec2", "sts", "ssm", "cloudformation")
                }
                clients["ec2"] = MagicMock(wraps=NetworkEC2())
                clients["sts"].get_caller_identity.return_value = {
                    "Account": "123456789012",
                    "Arn": "arn:aws:iam::123456789012:user/test",
                }
                sdk.Session = MagicMock()
                sdk.Session.return_value.client.side_effect = lambda name, **kwargs: (
                    clients[name]
                )
                botocore = types.ModuleType("botocore")
                sdk_config = types.ModuleType("botocore.config")
                sdk_config.Config = MagicMock()
                argv = ["deploy.py", "--provider", "my-k8s-0"]
                if not automatic:
                    argv += [
                        "--region",
                        "us-east-1",
                        "--vpc-id",
                        "vpc-abcdef",
                        "--system-subnet-id",
                        "subnet-aaa",
                        "--worker-subnet-cidr",
                        "172.31.128.0/24",
                    ]
                for filename in deploy.ASSETS:
                    (Path(directory) / filename).touch()
                with (
                    patch.dict(
                        sys.modules,
                        {
                            "boto3": sdk,
                            "botocore": botocore,
                            "botocore.config": sdk_config,
                        },
                    ),
                    patch.object(sys, "argv", argv),
                    patch.object(deploy, "BASE", Path(directory)),
                    patch.object(deploy, "find_stack", return_value=None),
                    patch.object(deploy, "inspect_capacity"),
                    patch.object(deploy, "resolve_ami", return_value="ami-abcdef"),
                    patch(
                        "builtins.input", side_effect=AssertionError("plan prompted")
                    ),
                    patch("sys.stdout", io.StringIO()) as output,
                ):
                    deploy.main()
                template = json.loads(
                    (Path(directory) / "infrastructure.generated.json").read_text()
                )
                worker = template["Resources"]["WorkerSubnet"]["Properties"]
                self.assertEqual(worker["VpcId"], "vpc-abcdef")
                self.assertEqual(worker["AvailabilityZone"], "us-east-1a")
                self.assertEqual(
                    worker["CidrBlock"],
                    "172.31.16.0/24" if automatic else "172.31.128.0/24",
                )
                self.assertIn(
                    "Selected VPC: vpc-abcdef; public system subnet: subnet-aaa",
                    output.getvalue(),
                )
                self.assertFalse((Path(directory) / "deployment-state.json").exists())
                for call in clients["ec2"].method_calls:
                    self.assertTrue(
                        call[0].startswith("describe_") or call[0] == "get_paginator",
                        call[0],
                    )
                for name in ("ssm", "cloudformation"):
                    self.assertEqual(clients[name].method_calls, [])

    def test_created_network_must_match_plan_before_bootstrap(self):
        cfg = fixture()
        outputs = {
            "NetworkingMode": deploy.NETWORKING_MODE,
            "SystemInstanceId": "i-test",
            "SystemPrivateIp": cfg["system_private_ip"],
            "WorkerSubnetId": "subnet-bbb",
            "WorkerRouteTableId": "rtb-test",
        }
        ec2 = MagicMock()
        node = {
            "SourceDestCheck": False,
            "PublicIpAddress": cfg["system_public_ip"],
            "SubnetId": cfg["system_subnet_id"],
            "PrivateIpAddress": cfg["system_private_ip"],
            "NetworkInterfaces": [{"NetworkInterfaceId": "eni-test"}],
        }
        worker = {
            "VpcId": cfg["vpc_id"],
            "CidrBlock": cfg["worker_subnet_cidr"],
            "AvailabilityZone": cfg["worker_availability_zone"],
            "MapPublicIpOnLaunch": False,
        }
        route = {
            "DestinationCidrBlock": "0.0.0.0/0",
            "InstanceId": "i-test",
            "State": "active",
        }
        table = {"Associations": [{"SubnetId": "subnet-bbb"}], "Routes": [route]}
        ec2.describe_instances.return_value = {"Reservations": [{"Instances": [node]}]}
        ec2.describe_subnets.return_value = {"Subnets": [worker]}
        ec2.describe_route_tables.return_value = {"RouteTables": [table]}
        deploy.check_created_network(ec2, cfg, outputs)
        for value in (True, None):
            node["SourceDestCheck"] = value
            with self.assertRaisesRegex(ValueError, "source/destination"):
                deploy.check_created_network(ec2, cfg, outputs)
        node["SourceDestCheck"] = False
        worker["MapPublicIpOnLaunch"] = True
        with self.assertRaisesRegex(ValueError, "worker subnet differs"):
            deploy.check_created_network(ec2, cfg, outputs)
        worker["MapPublicIpOnLaunch"] = False
        route["InstanceId"] = "i-unrelated"
        with self.assertRaisesRegex(ValueError, "dedicated route table"):
            deploy.check_created_network(ec2, cfg, outputs)
        route["InstanceId"] = "i-test"
        table["Associations"] = [{"SubnetId": "subnet-unrelated"}]
        with self.assertRaisesRegex(ValueError, "dedicated route table"):
            deploy.check_created_network(ec2, cfg, outputs)

    def test_nat_service_installed_before_cluster_and_required_on_boot(self):
        source = (BASE / "system-bootstrap.sh").read_text()
        self.assertLess(
            source.index("str(BASE / 'configure-nat.py')"),
            source.index("install_k3s(cfg['k3s_version'])"),
        )
        self.assertIn("Requires=cosmos-nat.service", source)
        self.assertIn("After=network-online.target cosmos-nat.service", source)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.ssm = MagicMock()
        self.ssm.exceptions.InvocationDoesNotExist = type(
            "InvocationDoesNotExist", (Exception,), {}
        )
        self.canary = "PRIVATE_RESPONSE_CANARY"

    def resource(self, name, status):
        return {
            "LogicalResourceId": name,
            "ResourceStatus": status,
            "ResourceStatusReason": self.canary,
            "PhysicalResourceId": self.canary,
        }

    def test_stack_bar_counts_all_pages_and_targets_created_stack_id(self):
        cfn = MagicMock()
        finished = {
            "StackStatus": "CREATE_COMPLETE",
            "Outputs": [{"OutputValue": self.canary}],
        }
        cfn.describe_stacks.side_effect = [
            {"Stacks": [{"StackStatus": "CREATE_IN_PROGRESS"}]},
            {"Stacks": [finished]},
        ]
        cfn.get_paginator.return_value.paginate.side_effect = [
            [
                {"StackResourceSummaries": [self.resource("Node", "CREATE_COMPLETE")]},
                {
                    "StackResourceSummaries": [
                        self.resource("Subnet", "CREATE_IN_PROGRESS"),
                        self.resource("Unexpected", "CREATE_COMPLETE"),
                    ]
                },
            ],
            [
                {
                    "StackResourceSummaries": [
                        self.resource("Node", "CREATE_COMPLETE"),
                        self.resource("Subnet", "CREATE_COMPLETE"),
                    ]
                }
            ],
        ]
        with (
            patch.object(deploy.time, "sleep") as sleep,
            patch.object(deploy.time, "monotonic", side_effect=[0, 15, 30]),
            patch("sys.stdout", io.StringIO()) as output,
        ):
            result = deploy.wait_stack(
                cfn, "exact-created-stack-id", ["Node", "Subnet"]
            )
        self.assertIs(result, finished)
        text = output.getvalue()
        self.assertIn("[##########----------] 1/2 resources", text)
        self.assertIn("[####################] 2/2 resources", text)
        self.assertIn("elapsed 00:15", text)
        self.assertNotIn(self.canary, text)
        sleep.assert_called_once_with(15)
        for call in cfn.describe_stacks.call_args_list:
            self.assertEqual(call.kwargs, {"StackName": "exact-created-stack-id"})
        for call in cfn.get_paginator.return_value.paginate.call_args_list:
            self.assertEqual(call.kwargs, {"StackName": "exact-created-stack-id"})

    def test_stack_failure_shows_owned_resource_names_not_reasons_or_outputs(self):
        for status in ("CREATE_FAILED", "ROLLBACK_IN_PROGRESS", "DELETE_COMPLETE"):
            with self.subTest(status=status):
                cfn = MagicMock()
                cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": status}]}
                cfn.get_paginator.return_value.paginate.return_value = [
                    {"StackResourceSummaries": [self.resource("Node", "CREATE_FAILED")]}
                ]
                with (
                    patch("sys.stdout", io.StringIO()) as output,
                    self.assertRaisesRegex(RuntimeError, status) as error,
                ):
                    deploy.wait_stack(cfn, "stack-id", ["Node"])
                self.assertIn("failed resources: Node", output.getvalue())
                self.assertNotIn(self.canary, output.getvalue() + str(error.exception))

    def test_missing_optional_counter_permission_keeps_stack_progress(self):
        cfn = MagicMock()
        denied = RuntimeError(self.canary)
        denied.response = {"Error": {"Code": "AccessDenied", "Message": self.canary}}
        cfn.describe_stacks.side_effect = [
            {"Stacks": [{"StackStatus": "CREATE_IN_PROGRESS"}]},
            {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]},
        ]
        cfn.get_paginator.return_value.paginate.side_effect = denied
        with (
            patch.object(deploy.time, "sleep"),
            patch("sys.stdout", io.StringIO()) as output,
        ):
            deploy.wait_stack(cfn, "stack-id", ["Node"])
        self.assertEqual(cfn.get_paginator.return_value.paginate.call_count, 1)
        self.assertIn(
            "CREATE_COMPLETE; resource counter unavailable", output.getvalue()
        )
        self.assertNotIn(self.canary, output.getvalue())

    def test_stack_polling_timeout_is_bounded_and_does_not_modify_resources(self):
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {
            "Stacks": [{"StackStatus": "CREATE_IN_PROGRESS"}]
        }
        cfn.get_paginator.return_value.paginate.return_value = [
            {"StackResourceSummaries": []}
        ]
        with (
            patch.object(deploy.time, "sleep") as sleep,
            patch("sys.stdout", io.StringIO()),
            self.assertRaisesRegex(TimeoutError, "may still be creating"),
        ):
            deploy.wait_stack(cfn, "stack-id", ["Node"])
        self.assertEqual(cfn.describe_stacks.call_count, 160)
        self.assertEqual(sleep.call_count, 160)
        self.assertTrue(
            all(
                call[0]
                in ("describe_stacks", "get_paginator", "get_paginator().paginate")
                for call in cfn.mock_calls
            )
        )

    def test_ssm_registration_prints_periodic_progress_until_online(self):
        self.ssm.describe_instance_information.side_effect = [
            {"InstanceInformationList": []},
            {
                "InstanceInformationList": [
                    {"PingStatus": "Online", "ComputerName": self.canary}
                ]
            },
        ]
        with (
            patch.object(deploy.time, "sleep") as sleep,
            patch("sys.stdout", io.StringIO()) as output,
        ):
            deploy.wait_ssm(self.ssm, "instance-id")
        self.assertIn(
            "[2/3] Systems Manager: waiting for agent; elapsed", output.getvalue()
        )
        self.assertIn("[2/3] Systems Manager: Online; elapsed", output.getvalue())
        self.assertNotIn(self.canary, output.getvalue())
        sleep.assert_called_once_with(10)

    def test_ssm_registration_timeout_is_bounded(self):
        self.ssm.describe_instance_information.return_value = {
            "InstanceInformationList": []
        }
        with (
            patch.object(deploy.time, "sleep") as sleep,
            patch("sys.stdout", io.StringIO()),
            self.assertRaisesRegex(TimeoutError, "did not register"),
        ):
            deploy.wait_ssm(self.ssm, "instance-id")
        self.assertEqual(self.ssm.describe_instance_information.call_count, 120)
        self.assertEqual(sleep.call_count, 120)

    def test_bootstrap_prints_heartbeat_even_without_status_changes(self):
        self.ssm.get_command_invocation.side_effect = [
            self.ssm.exceptions.InvocationDoesNotExist(),
            *[
                {
                    "Status": status,
                    "StandardOutputContent": self.canary,
                    "StandardErrorContent": self.canary,
                }
                for status in ("Pending", "InProgress", "InProgress", "Success")
            ],
        ]
        with (
            patch.object(deploy.time, "sleep") as sleep,
            patch("sys.stdout", io.StringIO()) as output,
        ):
            deploy.wait_bootstrap(self.ssm, "command-id", "instance-id")
        text = output.getvalue()
        self.assertIn("waiting for command registration; elapsed", text)
        self.assertEqual(text.count("[3/3] Bootstrap: InProgress; elapsed"), 2)
        self.assertIn("[3/3] Bootstrap: Success; elapsed", text)
        self.assertNotIn(self.canary, text)
        self.assertEqual(sleep.call_count, 4)

    def test_bootstrap_terminal_failures_stop_without_exposing_remote_output(self):
        for status in ("Failed", "Cancelled", "TimedOut", "Cancelling"):
            with self.subTest(status=status):
                self.ssm.get_command_invocation.return_value = {
                    "Status": status,
                    "StandardOutputContent": self.canary,
                    "StandardErrorContent": self.canary,
                }
                with (
                    patch.object(deploy.time, "sleep") as sleep,
                    patch("sys.stdout", io.StringIO()) as output,
                    self.assertRaisesRegex(
                        RuntimeError, "resources remain billable"
                    ) as error,
                ):
                    deploy.wait_bootstrap(self.ssm, "command-id", "instance-id")
                self.assertNotIn(self.canary, output.getvalue() + str(error.exception))
                sleep.assert_not_called()

    def test_bootstrap_polling_timeout_is_bounded(self):
        self.ssm.get_command_invocation.return_value = {
            "Status": "InProgress",
            "StandardOutputContent": self.canary,
        }
        with (
            patch.object(deploy.time, "sleep") as sleep,
            patch("sys.stdout", io.StringIO()) as output,
            self.assertRaisesRegex(TimeoutError, "inspect command STATUS"),
        ):
            deploy.wait_bootstrap(self.ssm, "command-id", "instance-id")
        self.assertEqual(self.ssm.get_command_invocation.call_count, 360)
        self.assertEqual(sleep.call_count, 360)
        self.assertNotIn(self.canary, output.getvalue())

    def test_unrecognized_service_status_is_not_echoed(self):
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": self.canary}]}
        self.ssm.get_command_invocation.return_value = {"Status": self.canary}
        with patch("sys.stdout", io.StringIO()) as output:
            for callback in (
                lambda: deploy.wait_stack(cfn, "stack-id", ["Node"]),
                lambda: deploy.safe_ssm_status(self.ssm, "command-id", "instance-id"),
            ):
                with self.assertRaises(ValueError) as error:
                    callback()
                self.assertNotIn(self.canary, str(error.exception))
        self.assertNotIn(self.canary, output.getvalue())


class NetworkEC2:
    def __init__(self):
        self.system = {
            "VpcId": "vpc-abcdef",
            "SubnetId": "subnet-aaa",
            "State": "available",
            "CidrBlock": "172.31.0.0/20",
            "AvailabilityZone": "us-east-1a",
            "MapPublicIpOnLaunch": True,
            "DefaultForAz": True,
            "AvailableIpAddressCount": 1000,
        }
        self.subnets = [[self.system]]
        self.tables = [
            {
                "Associations": [{"Main": True}],
                "Routes": [
                    {
                        "DestinationCidrBlock": "172.31.0.0/16",
                        "GatewayId": "local",
                        "State": "active",
                    },
                    {
                        "DestinationCidrBlock": "0.0.0.0/0",
                        "GatewayId": "igw-abc",
                        "State": "active",
                    },
                ],
            }
        ]
        self.peers = []
        self.vpc_cidr = "172.31.0.0/16"
        self.attached = True
        self.default_vpc = True

    def describe_subnets(self, **kwargs):
        return {
            "Subnets": [
                item
                for page in self.subnets
                for item in page
                if item.get("SubnetId") in kwargs["SubnetIds"]
            ]
        }

    def describe_vpcs(self, **kwargs):
        return {
            "Vpcs": [
                {
                    "VpcId": "vpc-abcdef",
                    "IsDefault": True,
                    "State": "available",
                    "CidrBlockAssociationSet": [
                        {
                            "CidrBlock": self.vpc_cidr,
                            "CidrBlockState": {"State": "associated"},
                        }
                    ],
                }
            ]
        }

    def describe_vpc_attribute(self, **kwargs):
        return {"EnableDnsSupport": {"Value": True}}

    def describe_internet_gateways(self, **kwargs):
        return {
            "InternetGateways": [
                {
                    "Attachments": [
                        {
                            "VpcId": "vpc-abcdef",
                            "State": "available" if self.attached else "detached",
                        }
                    ]
                }
            ]
        }

    def get_paginator(self, name):
        pages = {
            "describe_vpcs": [
                {"Vpcs": self.describe_vpcs()["Vpcs"] if self.default_vpc else []}
            ],
            "describe_subnets": [{"Subnets": page} for page in self.subnets],
            "describe_route_tables": [{"RouteTables": self.tables}],
            "describe_vpc_peering_connections": [{"VpcPeeringConnections": self.peers}],
        }
        return types.SimpleNamespace(paginate=lambda **kwargs: pages[name])


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.ec2 = NetworkEC2()
        self.args = deploy.args_parser().parse_args(["--provider", "my-k8s-0"])
        deploy.validate_arguments(self.args)

    def test_default_vpc_public_system_az_and_unused_worker_range(self):
        for offset in (16, 32, 48, 64, 80):
            self.ec2.subnets.append(
                [
                    {
                        "CidrBlock": "172.31." + str(offset) + ".0/20",
                        "AvailabilityZone": "us-east-1b",
                        "State": "available",
                        "SubnetId": "subnet-" + str(offset),
                    }
                ]
            )
        deploy.discover_network(self.ec2, self.args)
        self.assertEqual(self.args.region, "us-east-1")
        self.assertEqual(self.args.vpc_id, "vpc-abcdef")
        self.assertEqual(self.args.system_subnet_id, "subnet-aaa")
        self.assertEqual(self.args.worker_subnet_cidr, "172.31.96.0/24")
        result = deploy.check_network(self.ec2, self.args)
        self.assertEqual(result["worker_availability_zone"], "us-east-1a")

    def test_later_page_subnets_routes_and_pending_peers_are_excluded(self):
        self.ec2.subnets.append([{"CidrBlock": "172.31.16.0/24"}])
        self.ec2.tables[0]["Routes"].append(
            {"DestinationCidrBlock": "172.31.17.0/24", "TransitGatewayId": "tgw-test"}
        )
        self.ec2.peers = [
            {
                "VpcPeeringConnectionId": "pcx-test",
                "Status": {"Code": "pending-acceptance"},
                "RequesterVpcInfo": {"VpcId": "vpc-abcdef"},
                "AccepterVpcInfo": {
                    "VpcId": "vpc-other",
                    "CidrBlock": "172.31.18.0/24",
                },
            }
        ]
        deploy.discover_network(self.ec2, self.args)
        self.assertEqual(self.args.worker_subnet_cidr, "172.31.19.0/24")

    def test_no_default_vpc_requires_explicit_override(self):
        self.ec2.default_vpc = False
        with self.assertRaisesRegex(ValueError, "default VPC"):
            deploy.discover_network(self.ec2, self.args)
        self.args.vpc_id = "vpc-abcdef"
        deploy.discover_network(self.ec2, self.args)
        self.assertEqual(self.args.system_subnet_id, "subnet-aaa")

    def test_default_for_az_preferred_but_ambiguity_is_not_guessed(self):
        other = {
            **self.ec2.system,
            "SubnetId": "subnet-bbb",
            "CidrBlock": "172.31.32.0/20",
            "DefaultForAz": False,
        }
        self.ec2.subnets.append([other])
        deploy.discover_network(self.ec2, self.args)
        self.assertEqual(self.args.system_subnet_id, "subnet-aaa")
        self.ec2.system["DefaultForAz"] = False
        self.args.system_subnet_id = None
        with self.assertRaisesRegex(ValueError, "unique public system subnet"):
            deploy.discover_network(self.ec2, self.args)

    def test_private_default_route_wrong_az_and_no_free_space_fail_closed(self):
        self.ec2.tables[0]["Routes"][-1] = {
            "DestinationCidrBlock": "0.0.0.0/0",
            "NatGatewayId": "nat-test",
            "State": "active",
        }
        with self.assertRaisesRegex(ValueError, "public system subnet"):
            deploy.discover_network(self.ec2, self.args)
        self.ec2 = NetworkEC2()
        self.ec2.system["AvailabilityZone"] = "us-east-1b"
        with self.assertRaisesRegex(ValueError, "us-east-1a"):
            deploy.discover_network(self.ec2, self.args)
        self.ec2 = NetworkEC2()
        self.ec2.subnets.append([{"CidrBlock": "172.31.0.0/16"}])
        with self.assertRaisesRegex(ValueError, "No unused"):
            deploy.discover_network(self.ec2, self.args)

    def test_exhausted_system_subnet_is_not_selected(self):
        self.ec2.system["AvailableIpAddressCount"] = 0
        with self.assertRaisesRegex(ValueError, "public system subnet"):
            deploy.discover_network(self.ec2, self.args)
        other = {
            **self.ec2.system,
            "SubnetId": "subnet-bbb",
            "CidrBlock": "172.31.32.0/20",
            "DefaultForAz": False,
            "AvailableIpAddressCount": 100,
        }
        self.ec2.subnets.append([other])
        deploy.discover_network(self.ec2, self.args)
        self.assertEqual(self.args.system_subnet_id, "subnet-bbb")
        self.args.system_subnet_id = "subnet-aaa"
        with self.assertRaisesRegex(ValueError, "no available IPv4"):
            deploy.check_network(self.ec2, self.args)
        deploy.check_network(self.ec2, self.args, "owned-stack")

    def test_explicit_subnet_infers_vpc_and_preserves_cidr_override(self):
        self.ec2.default_vpc = False
        self.args.system_subnet_id = "subnet-aaa"
        self.args.worker_subnet_cidr = "172.31.128.0/24"
        deploy.discover_network(self.ec2, self.args)
        self.assertEqual(self.args.vpc_id, "vpc-abcdef")
        self.assertEqual(self.args.worker_subnet_cidr, "172.31.128.0/24")

    def test_az_override_must_match_region_and_explicit_subnet(self):
        self.args.availability_zone = "us-west-2a"
        with self.assertRaisesRegex(ValueError, "standard AZ"):
            deploy.validate_network_arguments(self.args)
        self.args.availability_zone = "us-east-1b"
        self.args.system_subnet_id = "subnet-aaa"
        with self.assertRaisesRegex(ValueError, "does not match"):
            deploy.discover_network(self.ec2, self.args)
        self.args.system_subnet_id = None
        self.ec2.system["AvailabilityZone"] = "us-east-1b"
        deploy.discover_network(self.ec2, self.args)
        self.assertEqual(self.args.system_subnet_id, "subnet-aaa")

    def test_smaller_worker_range_when_no_full_24_is_available(self):
        vpc = {
            "CidrBlockAssociationSet": [
                {
                    "CidrBlock": "172.31.0.0/24",
                    "CidrBlockState": {"State": "associated"},
                }
            ]
        }
        self.assertEqual(
            deploy.pick_worker_cidr(vpc, [{"CidrBlock": "172.31.0.0/25"}], [], []),
            "172.31.0.128/25",
        )
        with self.assertRaisesRegex(ValueError, "k3s"):
            deploy.pick_worker_cidr(
                {
                    "CidrBlockAssociationSet": [
                        {
                            "CidrBlock": "10.42.0.0/16",
                            "CidrBlockState": {"State": "associated"},
                        }
                    ]
                },
                [],
                [],
                [],
            )

    def test_resume_reuses_saved_network_without_rediscovery(self):
        ec2 = MagicMock()
        deploy.discover_network(ec2, self.args, fixture())
        self.assertEqual(self.args.worker_subnet_cidr, "172.31.128.0/24")
        self.assertEqual(self.args.system_subnet_id, "subnet-aaa")
        self.assertEqual(ec2.method_calls, [])
        self.args.worker_subnet_cidr = "172.31.129.0/24"
        with self.assertRaisesRegex(ValueError, "Resume network inputs differ"):
            deploy.discover_network(ec2, self.args, fixture())


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.ec2 = NetworkEC2()
        self.args = argparse.Namespace(
            vpc_id="vpc-abcdef",
            system_subnet_id="subnet-aaa",
            worker_subnet_cidr="172.31.128.0/24",
        )

    def test_public_system_subnet_and_unused_private_cidr_pass(self):
        result = deploy.check_network(self.ec2, self.args)
        self.assertEqual(result["worker_availability_zone"], "us-east-1a")
        self.assertEqual(result["internet_gateway_id"], "igw-abc")

    def test_managed_nat_or_blackhole_system_route_rejected(self):
        for route in (
            {"NatGatewayId": "nat-abc", "State": "active"},
            {"GatewayId": "igw-abc", "State": "blackhole"},
        ):
            self.ec2.tables[0]["Routes"][-1] = {
                "DestinationCidrBlock": "0.0.0.0/0",
                **route,
            }
            with (
                self.subTest(route=route),
                self.assertRaisesRegex(ValueError, "internet gateway"),
            ):
                deploy.check_network(self.ec2, self.args)

    def test_explicit_route_association_takes_precedence_over_main(self):
        self.ec2.tables.append(
            {"Associations": [{"SubnetId": "subnet-aaa"}], "Routes": []}
        )
        with self.assertRaisesRegex(ValueError, "internet gateway"):
            deploy.check_network(self.ec2, self.args)

    def test_worker_collision_on_later_page_is_rejected(self):
        self.ec2.subnets.append([{"CidrBlock": "172.31.128.0/20"}])
        with self.assertRaisesRegex(ValueError, "overlaps an existing subnet"):
            deploy.check_network(self.ec2, self.args)

    def test_only_exact_owned_worker_subnet_can_be_reused_on_resume(self):
        owned = {
            "CidrBlock": self.args.worker_subnet_cidr,
            "AvailabilityZone": "us-east-1a",
            "MapPublicIpOnLaunch": False,
            "Tags": [
                {"Key": "aws:cloudformation:stack-id", "Value": "owned-stack"},
                {"Key": "aws:cloudformation:logical-id", "Value": "WorkerSubnet"},
            ],
        }
        self.ec2.subnets.append([owned])
        deploy.check_network(self.ec2, self.args, "owned-stack")
        with self.assertRaises(ValueError):
            deploy.check_network(self.ec2, self.args, "other-stack")
        owned["MapPublicIpOnLaunch"] = True
        with self.assertRaises(ValueError):
            deploy.check_network(self.ec2, self.args, "owned-stack")

    def test_outside_vpc_cidr_cluster_overlap_and_detached_gateway_rejected(self):
        self.args.worker_subnet_cidr = "192.168.1.0/24"
        with self.assertRaisesRegex(ValueError, "within"):
            deploy.check_network(self.ec2, self.args)
        self.ec2.vpc_cidr = "10.42.0.0/16"
        self.args.worker_subnet_cidr = "10.42.128.0/24"
        with self.assertRaisesRegex(ValueError, "k3s"):
            deploy.check_network(self.ec2, self.args)
        self.ec2.vpc_cidr = "172.31.0.0/16"
        self.args.worker_subnet_cidr = "172.31.128.0/24"
        self.ec2.attached = False
        with self.assertRaisesRegex(ValueError, "attached"):
            deploy.check_network(self.ec2, self.args)

    def test_connected_route_and_peer_cluster_conflicts_rejected(self):
        self.ec2.tables[0]["Routes"].append(
            {"DestinationCidrBlock": "172.31.128.0/24", "TransitGatewayId": "tgw-test"}
        )
        with self.assertRaisesRegex(ValueError, "connected-network"):
            deploy.check_network(self.ec2, self.args)
        self.ec2.tables[0]["Routes"].pop()
        self.ec2.peers = [
            {
                "VpcPeeringConnectionId": "pcx-test",
                "Status": {"Code": "active"},
                "RequesterVpcInfo": {"VpcId": "vpc-abcdef"},
                "AccepterVpcInfo": {
                    "VpcId": "vpc-other",
                    "CidrBlockSet": [{"CidrBlock": "10.42.0.0/16"}],
                },
            }
        ]
        with self.assertRaisesRegex(ValueError, "peered"):
            deploy.check_network(self.ec2, self.args)


if __name__ == "__main__":
    unittest.main()
