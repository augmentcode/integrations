# Profile regression tests.
import ast
import base64
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_deploy import BASE, configure, deploy, fixture


def worker_tree(script):
    return ast.parse(script.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0])


class ProfileTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(configure, "BASE", BASE)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.script = (BASE / "worker-bootstrap.sh").read_text()
        self.tree = worker_tree(self.script)
        selected = [
            node
            for node in self.tree.body
            if (
                isinstance(node, ast.FunctionDef)
                and node.name in ("install_sandbox_apparmor", "kubelet_args")
            )
            or (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name)
                    and target.id == "SANDBOX_APPARMOR_PROFILE"
                    for target in node.targets
                )
            )
        ]
        self.assertEqual(len(selected), 3)
        self.worker = {
            "Path": MagicMock(spec=Path),
            "run": MagicMock(),
            "write": MagicMock(),
        }
        exec(
            compile(
                ast.Module(body=selected, type_ignores=[]),
                "worker-bootstrap.sh",
                "exec",
            ),
            self.worker,
        )

    def test_reservations_are_scoped_and_only_override_sandbox_requests(self):
        for profile, memory in (("default", "2G"), ("eight", "1500M")):
            for provider in ("my-k8s-0", "another-provider"):
                with self.subTest(profile=profile, provider=provider):
                    cfg = {
                        **fixture(),
                        "session_profile": profile,
                        "provider": provider,
                    }
                    policy, binding = configure.reservation_objects(cfg)
                    self.assertEqual(policy["kind"], "MutatingAdmissionPolicy")
                    self.assertEqual(binding["kind"], "MutatingAdmissionPolicyBinding")
                    self.assertEqual(
                        binding["spec"]["policyName"], policy["metadata"]["name"]
                    )
                    match = {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "cosmos-sb"}
                        },
                        "objectSelector": {
                            "matchLabels": {
                                "cosmos.augmentcode.com/installation": "cosmos",
                                "cosmos.augmentcode.com/provider": provider,
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
                    self.assertEqual(policy["spec"]["matchConstraints"], match)
                    self.assertEqual(binding["spec"]["matchResources"], match)
                    self.assertEqual(
                        policy["spec"]["matchConditions"],
                        [
                            {
                                "name": "sandbox-container-present",
                                "expression": "object.spec.containers.exists(c, c.name == 'sandbox')",
                            }
                        ],
                    )
                    expected = (
                        "Object{spec: Object.spec{containers: [Object.spec.containers{"
                        "name: 'sandbox', resources: Object.spec.containers.resources{"
                        "requests: {'cpu': '125m', 'memory': '" + memory + "'}}}]}}"
                    )
                    mutations = policy["spec"]["mutations"]
                    self.assertEqual(len(mutations), 1)
                    self.assertEqual(mutations[0]["patchType"], "ApplyConfiguration")
                    expression = mutations[0]["applyConfiguration"]["expression"]
                    self.assertEqual(
                        "".join(expression.split()), "".join(expected.split())
                    )
                    for forbidden in ("limits", "ephemeral-storage", "disk", "sidecar"):
                        self.assertNotIn(forbidden, expression)

    def test_cli_defaults_and_eight_profile_disk_guard(self):
        args = deploy.args_parser().parse_args(["--provider", "my-k8s-0"])
        deploy.validate_arguments(args)
        self.assertEqual(args.worker_instance_types, ["m8i-flex.xlarge"])
        self.assertEqual(args.session_profile, "default")
        self.assertEqual(args.worker_disk_gb, 100)
        for disk in (100, 199, 200):
            with self.subTest(disk=disk):
                args = deploy.args_parser().parse_args(
                    [
                        "--provider",
                        "my-k8s-0",
                        "--session-profile",
                        "eight",
                        "--worker-disk-gb",
                        str(disk),
                    ]
                )
                if disk < 200:
                    with self.assertRaisesRegex(ValueError, "at least 200"):
                        deploy.validate_arguments(args)
                else:
                    deploy.validate_arguments(args)
                    self.assertEqual(args.session_profile, "eight")

    def test_configuration_profile_and_disk_boundaries(self):
        configure.validate_config(fixture())
        for invalid in ("unknown", "EIGHT", "", None, True):
            with (
                self.subTest(profile=invalid),
                self.assertRaisesRegex(ValueError, "session profile"),
            ):
                configure.validate_config({**fixture(), "session_profile": invalid})
        for disk in (100, 199):
            with self.subTest(disk=disk), self.assertRaisesRegex(ValueError, "200 GiB"):
                configure.validate_config(
                    {**fixture(), "session_profile": "eight", "worker_disk_gb": disk}
                )
        configure.validate_config(
            {**fixture(), "session_profile": "eight", "worker_disk_gb": 200}
        )

    def test_worker_payload_and_kubelet_match_karpenter_for_both_profiles(self):
        provider_id = "aws:///us-east-1a/i-example"
        for profile, max_pods in (("default", 110), ("eight", 8)):
            with self.subTest(profile=profile):
                cfg = {
                    **fixture(),
                    "session_profile": profile,
                    "worker_disk_gb": 200 if profile == "eight" else 100,
                }
                node_class, _ = configure.capacity_objects(cfg)
                self.assertEqual(node_class["kind"], "EC2NodeClass")
                self.assertEqual(node_class["spec"]["kubelet"]["maxPods"], max_pods)
                userdata = node_class["spec"]["userData"]
                self.assertLess(len(userdata.encode()), 16384)
                self.assertNotIn("__COSMOS_CONFIG_B64__", userdata)
                (assignment,) = [
                    node
                    for node in worker_tree(userdata).body
                    if isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "CONFIG_B64"
                        for target in node.targets
                    )
                ]
                payload = json.loads(
                    base64.b64decode(ast.literal_eval(assignment.value), validate=True)
                )
                fields = (
                    "region",
                    "agent_parameter",
                    "system_private_ip",
                    "k3s_version",
                    "session_profile",
                )
                self.assertEqual(payload, {key: cfg[key] for key in fields})
                result = subprocess.run(
                    ["bash", "-n"], input=userdata.encode(), capture_output=True
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                args = self.worker["kubelet_args"](payload, provider_id)
                self.assertEqual(
                    args,
                    [
                        f"provider-id={provider_id}",
                        f"max-pods={max_pods}",
                        "system-reserved=cpu=200m,memory=512Mi,ephemeral-storage=2Gi",
                        "kube-reserved=cpu=200m,memory=512Mi,ephemeral-storage=2Gi",
                    ],
                )
                self.assertIn(
                    f"max-pods={node_class['spec']['kubelet']['maxPods']}", args
                )
                self.worker["run"].assert_not_called()
                self.worker["write"].assert_not_called()

    def test_worker_rejects_invalid_profile(self):
        for invalid in ("unknown", "EIGHT", "", None, True):
            with (
                self.subTest(profile=invalid),
                self.assertRaisesRegex(ValueError, "session profile"),
            ):
                self.worker["kubelet_args"](
                    {"session_profile": invalid}, "aws:///us-east-1a/i-example"
                )

    def test_apparmor_writes_and_loads_exact_profile_with_mode_0644(self):
        expected = (
            "abi <abi/4.0>,\ninclude <tunables/global>\n"
            "profile cosmos-sandbox /usr/local/opt/cosmos/bin/sb flags=(unconfined) {\n"
            "  userns,\n}\n"
        )
        self.assertEqual(self.worker["SANDBOX_APPARMOR_PROFILE"], expected)
        path = self.worker["Path"].return_value
        path.__str__.return_value = "/etc/apparmor.d/cosmos-sandbox"
        for exists in (False, True):
            with self.subTest(existing_matching_profile=exists):
                path.exists.return_value = exists
                path.read_text.return_value = expected
                self.worker["Path"].reset_mock()
                self.worker["write"].reset_mock()
                self.worker["run"].reset_mock()
                self.worker["install_sandbox_apparmor"]()
                self.worker["Path"].assert_called_once_with(
                    "/etc/apparmor.d/cosmos-sandbox"
                )
                self.worker["write"].assert_called_once_with(path, expected, 0o644)
                self.worker["run"].assert_called_once_with(
                    ["apparmor_parser", "--replace", "/etc/apparmor.d/cosmos-sandbox"]
                )

    def test_apparmor_refuses_different_existing_profile_without_side_effects(self):
        path = self.worker["Path"].return_value
        path.exists.return_value = True
        path.read_text.return_value = "profile owned-by-someone-else {}\n"
        with self.assertRaisesRegex(ValueError, "refusing overwrite"):
            self.worker["install_sandbox_apparmor"]()
        self.worker["write"].assert_not_called()
        self.worker["run"].assert_not_called()

    def test_bootstrap_installs_apparmor_without_disabling_global_userns_restrictions(
        self,
    ):
        main = next(
            node
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "install_sandbox_apparmor"
                for node in ast.walk(main)
            )
        )
        self.assertNotRegex(
            self.script, r"apparmor_restrict_unprivileged_(?:userns|unconfined)\s*=\s*0"
        )
        self.assertNotRegex(self.script, r"unprivileged_userns_clone\s*=\s*0")

    def test_capacity_arithmetic_is_a_model_not_eight_live_sessions(self):
        memory, disk = 15029848 * 1024, 93448723585
        disk_request = 14776380 * 1024
        self.assertEqual(memory // 2_000_000_000, 7)
        self.assertEqual(disk // disk_request, 6)
        self.assertGreaterEqual(memory, 8 * 1_500_000_000)
        self.assertGreaterEqual(200 * 1024**3, 8 * disk_request)


if __name__ == "__main__":
    unittest.main()
