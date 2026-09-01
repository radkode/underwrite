#!/usr/bin/env python3
"""Live Linux conformance checks for the privileged execution boundary."""

import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gateway import artifacts
from gateway.broker import GatewayPolicy
from gateway.docker_runner import DockerRunner, runtime_container_name
from gateway.signing import OpenSSLSigner
from skills.underwrite.scripts.execution_receipt import verify_execution_receipt


IMAGE_ENV = "UNDERWRITE_LIVE_GATEWAY_IMAGE"
PLATFORM_ENV = "UNDERWRITE_LIVE_GATEWAY_PLATFORM"
DOCKER_HOST_ENV = "UNDERWRITE_LIVE_GATEWAY_DOCKER_HOST"
RUNNER_PATH = Path(__file__).parents[1] / "gateway" / "sandbox_runner.py"
ADAPTER_PATH = (Path(__file__).parents[1] / "gateway" / "adapter.py").resolve()
RUNTIME_DOMAIN_ID = "urn:underwrite:runtime-domain:live:local-docker"
DIRECT_DEPLOYMENT_ID = "urn:underwrite:gateway:live:direct-conformance"


@unittest.skipUnless(os.environ.get(IMAGE_ENV), f"set {IMAGE_ENV} for live Docker checks")
class LiveGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ("docker", "git", "openssl"):
            candidate = shutil.which(tool)
            if candidate is None:
                raise RuntimeError("required live gateway tool is unavailable: %s" % tool)
            setattr(cls, tool, str(Path(candidate).resolve()))
        cls.image = os.environ[IMAGE_ENV]
        cls.platform = os.environ.get(PLATFORM_ENV, "linux/amd64")
        cls.docker_host = os.environ.get(
            DOCKER_HOST_ENV, "unix:///var/run/docker.sock"
        )
        probe = (
            "import hashlib,os;"
            "p=os.path.realpath('/usr/local/bin/python3');"
            "b=open(p,'rb').read();"
            "print(p);print(hashlib.sha256(b).hexdigest());print(len(b))"
        )
        completed = subprocess.run(
            [
                cls.docker,
                "--host",
                cls.docker_host,
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "/usr/local/bin/python3",
                cls.image,
                "-I",
                "-S",
                "-c",
                probe,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=True,
        )
        path, digest, size = completed.stdout.splitlines()
        cls.executable = {"path": path, "sha256": digest, "bytes": int(size)}
        cls.runner_sha256 = hashlib.sha256(RUNNER_PATH.read_bytes()).hexdigest()

    def limits(self, **updates):
        value = {
            "wallSeconds": 5,
            "cpuSeconds": 3,
            "memoryBytes": 128 * 1024 * 1024,
            "processes": 1,
            "workspaceBytes": 4 * 1024 * 1024,
            "outputBytes": 64 * 1024,
        }
        value.update(updates)
        return value

    def sandbox(self, limits):
        return {
            "policy": "https://github.com/radkode/underwrite/sandbox-policy/v1",
            "credentials": "absent",
            "network": "denied",
            "hostWrites": "denied",
            "gitHooks": "disabled",
            "gitFilters": "disabled",
            "timeout": "enforced",
            "limits": limits,
        }

    def execute(self, code, *, limits=None, environment=None):
        limits = limits or self.limits()
        environment = environment or {
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
        }
        with tempfile.TemporaryDirectory() as name:
            workspace = Path(name) / "workspace"
            workspace.mkdir()
            (workspace / "seed.txt").write_bytes(b"trusted input\n")
            input_tree = artifacts.synthetic_git_tree(
                workspace, maximum_bytes=limits["workspaceBytes"]
            )
            archive = artifacts.pack_workspace(
                workspace, maximum_bytes=limits["workspaceBytes"]
            )
            request = {
                "version": 1,
                "job": {
                    "argv": [self.executable["path"], "-I", "-S", "-c", code],
                    "cwd": ".",
                    "environment": environment,
                    "executable": self.executable,
                    "stdin": "closed",
                },
                "sandbox": self.sandbox(limits),
                "inputTree": input_tree,
                "targetUid": 65532,
                "targetGid": 65532,
                "runnerSha256": self.runner_sha256,
            }
            runner = DockerRunner(
                self.image,
                self.platform,
                self.docker_host,
                DIRECT_DEPLOYMENT_ID,
                RUNTIME_DOMAIN_ID,
            )
            runner.reconcile()
            try:
                prepared = runner.prepare(request, archive)
                try:
                    self.assertEqual(prepared.ready.input_tree, input_tree)
                    issued_at = datetime.now(timezone.utc)
                    return prepared.run(
                        "a" * 64,
                        issued_at,
                        issued_at + timedelta(minutes=5),
                    ), limits
                finally:
                    prepared.close()
            finally:
                runner.close()

    def command(self, arguments, *, cwd=None):
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return completed.stdout.strip()

    def remove_container(self, name):
        subprocess.run(
            [self.docker, "--host", self.docker_host, "rm", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def source_bundle(self, root):
        repo = root / "source"
        self.command([self.git, "init", repo])
        for name, value in (
            ("user.name", "Underwrite Live Test"),
            ("user.email", "live@underwrite.invalid"),
            ("commit.gpgsign", "false"),
        ):
            self.command([self.git, "config", name, value], cwd=repo)
        (repo / "seed.txt").write_bytes(b"base input\n")
        self.command([self.git, "add", "."], cwd=repo)
        self.command([self.git, "commit", "-m", "base"], cwd=repo)
        base = self.command([self.git, "rev-parse", "HEAD"], cwd=repo).decode("ascii")
        (repo / "seed.txt").write_bytes(b"frozen head input\n")
        self.command([self.git, "add", "."], cwd=repo)
        self.command([self.git, "commit", "-m", "head"], cwd=repo)
        head = self.command([self.git, "rev-parse", "HEAD"], cwd=repo).decode("ascii")
        self.command(
            [self.git, "update-ref", artifacts.SOURCE_REFS[0], base], cwd=repo
        )
        self.command(
            [self.git, "update-ref", artifacts.SOURCE_REFS[1], head], cwd=repo
        )
        bundle_path = root / "source.bundle"
        self.command(
            [
                self.git,
                "bundle",
                "create",
                bundle_path,
                *artifacts.SOURCE_REFS,
            ],
            cwd=repo,
        )
        bundle = bundle_path.read_bytes()
        difference = self.command([self.git, "diff", base, head], cwd=repo)
        trusted_context = b"trusted base instructions\n"
        target = {
            "version": 1,
            "kind": "github_pr",
            "repo": "acme/widget",
            "number": 17,
            "state": "open",
            "merged_at": None,
            "base_sha": base,
            "head_sha": head,
            "head_repo_id": 1234,
            "head_repo": "acme/widget",
            "head_ref": "gateway-live-test",
            "merge_base_sha": base,
            "changed_files": 1,
            "diff_sha256": hashlib.sha256(difference).hexdigest(),
            "diff_bytes": len(difference),
            "trusted_context_sha256": hashlib.sha256(trusted_context).hexdigest(),
            "trusted_context_bytes": len(trusted_context),
            "object_bundle_sha256": hashlib.sha256(bundle).hexdigest(),
            "object_bundle_bytes": len(bundle),
        }
        return bundle_path, target

    def test_adapter_signs_and_publishes_one_verified_end_to_end_execution(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as name:
            root = Path(name).resolve()
            os.chown(root, -1, os.getegid())
            root.chmod(0o710)
            container_name = runtime_container_name(RUNTIME_DOMAIN_ID)
            self.remove_container(container_name)
            self.addCleanup(self.remove_container, container_name)
            self.command(
                [
                    self.docker,
                    "--host",
                    self.docker_host,
                    "run",
                    "--detach",
                    "--name",
                    container_name,
                    "--platform",
                    self.platform,
                    "--network",
                    "none",
                    "--entrypoint",
                    "/usr/local/bin/python3",
                    self.image,
                    "-I",
                    "-S",
                    "-c",
                    "import time;time.sleep(300)",
                ]
            )
            running = self.command(
                [
                    self.docker,
                    "--host",
                    self.docker_host,
                    "container",
                    "inspect",
                    "--format={{.State.Running}}",
                    container_name,
                ]
            )
            self.assertEqual(running, b"true")
            bundle_path, target = self.source_bundle(root)
            private_key = root / "private.pem"
            public_key = root / "public.pem"
            self.command(
                [
                    self.openssl,
                    "genpkey",
                    "-algorithm",
                    "EC",
                    "-pkeyopt",
                    "ec_paramgen_curve:P-256",
                    "-pkeyopt",
                    "ec_param_enc:named_curve",
                    "-out",
                    private_key,
                ]
            )
            private_key.chmod(0o600)
            public_key.write_bytes(
                self.command(
                    [
                        self.openssl,
                        "pkey",
                        "-in",
                        private_key,
                        "-pubout",
                    ]
                )
                + b"\n"
            )
            signer_id = "https://runner.example/hosts/live-test"
            signer = OpenSSLSigner(
                private_key, public_key, signer_id, openssl=self.openssl
            )
            limits = self.limits()
            environment = {
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUTF8": "1",
            }
            sandbox = self.sandbox(limits)
            code = (
                "from pathlib import Path;"
                "assert Path('seed.txt').read_bytes()==b'frozen head input\\n';"
                "Path('executed.txt').write_bytes(b'gateway output\\n');"
                "print('broker completed')"
            )
            job = {
                "argv": [self.executable["path"], "-I", "-S", "-c", code],
                "cwd": ".",
                "environment": environment,
                "executable": self.executable,
                "stdin": "closed",
            }
            policy = GatewayPolicy(
                signer_id=signer_id,
                deployment_id="urn:underwrite:gateway:live-conformance",
                runtime_domain_id=RUNTIME_DOMAIN_ID,
                docker_host=self.docker_host,
                image=self.image,
                platform=self.platform,
                runner_sha256=self.runner_sha256,
                executable=self.executable,
                environment=environment,
                sandbox=sandbox,
                target_uid=65532,
                target_gid=65532,
            )
            request = {
                "version": 1,
                "sessionId": str(uuid.uuid4()),
                "challenge": os.urandom(32).hex(),
                "target": target,
                "action": {"seq": 1, "beat": 1, "attempt": 1},
                "job": job,
                "sandbox": sandbox,
                "exitCode": 0,
            }
            profile = {
                "version": 1,
                "keyId": signer.key_id,
                "signerId": signer_id,
                "executorId": policy.executor_id,
                "job": job,
                "sandbox": sandbox,
                "exitCode": 0,
            }
            store = root / "store"
            outbox = root / "outbox"
            store.mkdir(mode=0o700)
            outbox.mkdir(mode=0o710)
            evidence = outbox / "attempt-1"
            configuration = {
                "version": 1,
                "privateKey": str(private_key),
                "publicKey": str(public_key),
                "storeRoot": str(store),
                "docker": self.docker,
                "openssl": self.openssl,
                "git": self.git,
                "deploymentId": policy.deployment_id,
                "runtimeDomainId": policy.runtime_domain_id,
                "dockerHost": self.docker_host,
                "image": self.image,
                "platform": self.platform,
                "runnerSha256": self.runner_sha256,
                "targetUid": 65532,
                "targetGid": 65532,
                "consumerGid": os.getegid(),
                "capabilitySeconds": 300,
                "maxSourceBundleBytes": 64 * 1024 * 1024,
                "profile": profile,
            }
            def canonical(value):
                return json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            config_path = root / "adapter.json"
            config_path.write_bytes(canonical(configuration))
            config_path.chmod(0o600)
            request_path = root / "request.json"
            request_bytes = canonical(request)
            request_path.write_bytes(request_bytes)
            command = [
                sys.executable,
                "-I",
                "-S",
                str(ADAPTER_PATH),
                "--config",
                str(config_path),
                "--request",
                str(request_path),
                "--source-bundle",
                str(bundle_path),
                "--evidence-dir",
                str(evidence),
            ]

            first = json.loads(self.command(command))

            self.assertEqual(
                first,
                {
                    "version": 1,
                    "status": "complete",
                    "evidenceDir": str(evidence),
                },
            )
            self.assertEqual(
                {path.name for path in evidence.iterdir()},
                {
                    "request.json",
                    "capability.dsse.json",
                    "receipt.dsse.json",
                    "output.bundle",
                    "stdout",
                    "stderr",
                },
            )
            for path in evidence.iterdir():
                details = path.stat()
                self.assertTrue(path.is_file())
                self.assertEqual(details.st_nlink, 1)
            self.assertEqual((evidence / "request.json").read_bytes(), request_bytes)
            stdout = (evidence / "stdout").read_bytes()
            stderr = (evidence / "stderr").read_bytes()
            self.assertEqual(stdout, b"broker completed\n")
            self.assertEqual(stderr, b"")
            output_bundle = (evidence / "output.bundle").read_bytes()
            descriptor = artifacts.verify_output_bundle(
                output_bundle,
                maximum_workspace_bytes=limits["workspaceBytes"],
                maximum_bundle_bytes=limits["workspaceBytes"] * 2,
                git=self.git,
            )
            input_workspace = root / "expected-input"
            input_workspace.mkdir()
            (input_workspace / "seed.txt").write_bytes(b"frozen head input\n")
            input_tree = artifacts.synthetic_git_tree(
                input_workspace,
                maximum_bytes=limits["workspaceBytes"],
            )
            def stream(value):
                return {
                    "sha256": hashlib.sha256(value).hexdigest(),
                    "bytes": len(value),
                    "truncated": False,
                }
            verified = verify_execution_receipt(
                (evidence / "capability.dsse.json").read_bytes(),
                (evidence / "receipt.dsse.json").read_bytes(),
                {
                    "signerId": signer_id,
                    "executorId": policy.executor_id,
                    "sessionId": request["sessionId"],
                    "challenge": request["challenge"],
                    "target": target,
                    "action": request["action"],
                    "job": job,
                    "inputTree": input_tree,
                    "sandbox": sandbox,
                    "outputTree": descriptor.git_tree,
                    "outputBundle": {
                        "sha256": hashlib.sha256(output_bundle).hexdigest(),
                        "bytes": len(output_bundle),
                    },
                    "stdout": stream(stdout),
                    "stderr": stream(stderr),
                    "exitCode": 0,
                },
                signer.verify,
                datetime.now(timezone.utc),
            )
            self.assertEqual(verified.signer_id, signer_id)

            self.assertEqual(json.loads(self.command(command)), first)
            remaining = self.command(
                [
                    self.docker,
                    "--host",
                    self.docker_host,
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"name=^/{container_name}$",
                ]
            )
            self.assertEqual(remaining, b"")

    def test_clean_job_has_only_the_declared_surface_and_round_trips_output(self):
        environment = {
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
        }
        code = "\n".join(
            (
                "import errno,json,os,pathlib,shutil,socket,sys,threading",
                f"expected_environment={environment!r}",
                "assert dict(os.environ) == expected_environment",
                "assert os.getuid() == 65532 and os.getgid() == 65532",
                "status={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line}",
                "assert all(int(status[name],16)==0 for name in ('CapInh','CapPrm','CapEff','CapAmb'))",
                "try:",
                " os.fstat(0); raise AssertionError('stdin is open')",
                "except OSError as error:",
                " assert error.errno == errno.EBADF",
                "denied = {}",
                "denied['capabilities']='cleared'",
                "for label,family in (('inet',socket.AF_INET),('unix',socket.AF_UNIX)):",
                " try:",
                "  socket.socket(family,socket.SOCK_STREAM); raise AssertionError(label)",
                " except OSError as error:",
                "  assert error.errno == errno.EPERM; denied[label]=error.errno",
                "try:",
                " os.fork(); raise AssertionError('fork succeeded')",
                "except OSError as error:",
                " assert error.errno == errno.EPERM; denied['fork']=error.errno",
                "try:",
                " thread=threading.Thread(target=lambda: None); thread.start(); raise AssertionError('thread succeeded')",
                "except RuntimeError:",
                " denied['thread']='blocked'",
                "try:",
                " os.kill(1,0); raise AssertionError('PID 1 is signalable')",
                "except PermissionError:",
                " denied['pid1']='blocked'",
                "try:",
                " pathlib.Path('/host-write').write_bytes(b'bad'); raise AssertionError('root write succeeded')",
                "except OSError as error:",
                " assert error.errno in (errno.EROFS,errno.EACCES,errno.EPERM); denied['hostWrite']=error.errno",
                "try:",
                " pathlib.Path('/workspace/hidden').write_bytes(b'bad'); raise AssertionError('unmeasured workspace write succeeded')",
                "except OSError as error:",
                " assert error.errno in (errno.EACCES,errno.EPERM); denied['workspaceSibling']=error.errno",
                "tool=pathlib.Path('local-tool')",
                "tool.write_text('#!/bin/sh\\nexit 0\\n',encoding='utf-8'); tool.chmod(0o755)",
                "try:",
                " os.execve('./local-tool',['./local-tool'],expected_environment); raise AssertionError('workspace exec succeeded')",
                "except OSError as error:",
                " assert error.errno in (errno.EACCES,errno.EPERM); denied['workspaceExec']=error.errno",
                "assert shutil.which('git') is None",
                "pathlib.Path('result.json').write_text(json.dumps(denied,sort_keys=True),encoding='utf-8')",
                "sys.stdout.buffer.write(b'complete stdout\\n')",
                "sys.stderr.buffer.write(b'complete stderr\\n')",
            )
        )
        result, limits = self.execute(code, environment=environment)

        self.assertEqual(result.status, "exited")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, b"complete stdout\n")
        self.assertEqual(result.stderr, b"complete stderr\n")
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / "output"
            output_tree = artifacts.unpack_workspace(
                result.workspace,
                output,
                maximum_bytes=limits["workspaceBytes"],
            )
            self.assertEqual(output_tree, result.output_tree)
            evidence = json.loads((output / "result.json").read_text("utf-8"))
        self.assertEqual(
            set(evidence),
            {
                "capabilities",
                "fork",
                "hostWrite",
                "inet",
                "pid1",
                "thread",
                "unix",
                "workspaceExec",
                "workspaceSibling",
            },
        )

    def test_wall_and_output_limits_return_no_execution_evidence(self):
        cases = (
            (
                "wall",
                "import time; time.sleep(3)",
                self.limits(wallSeconds=1, cpuSeconds=1),
                "wall limit exceeded",
            ),
            (
                "output",
                "import os; os.write(1,b'x'*65536)",
                self.limits(outputBytes=1024),
                "output limit exceeded",
            ),
        )
        for label, code, limits, failure in cases:
            with self.subTest(label=label):
                result, _limits = self.execute(code, limits=limits)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.failure, failure)
                self.assertIsNone(result.exit_code)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b"")
                self.assertEqual(result.workspace, b"")


if __name__ == "__main__":
    unittest.main()
