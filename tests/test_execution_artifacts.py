#!/usr/bin/env python3
"""Host-neutral artifact integrity and quarantine behavior."""

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from gateway import artifacts
from gateway import git_limiter


WORKSPACE_LIMIT = 2 * 1024 * 1024
BUNDLE_LIMIT = 8 * 1024 * 1024


def object_id(kind, body):
    value = kind + b" " + str(len(body)).encode("ascii") + b"\0" + body
    return hashlib.sha256(value).digest()


class ArtifactCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="underwrite-artifact-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def git(self, *arguments, input_bytes=None):
        completed = subprocess.run(
            ["git", *map(str, arguments)],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", "replace"),
        )
        return completed.stdout.strip()

    def init_repo(self, name="source", object_format="sha1"):
        repo = self.root / name
        self.git("init", f"--object-format={object_format}", repo)
        self.git("-C", repo, "config", "user.name", "Underwrite Test")
        self.git("-C", repo, "config", "user.email", "test@underwrite.invalid")
        self.git("-C", repo, "config", "commit.gpgsign", "false")
        self.git("-C", repo, "config", "tag.gpgsign", "false")
        return repo


class GitResourceBoundTests(ArtifactCase):
    def test_limiter_lowers_unlimited_and_finite_hard_limits(self):
        with mock.patch.object(
            git_limiter.resource,
            "RLIM_INFINITY",
            -1,
        ), mock.patch.object(
            git_limiter.resource,
            "getrlimit",
            return_value=(-1, -1),
        ), mock.patch.object(git_limiter.resource, "setrlimit") as set_limit:
            git_limiter._limit(7, 1024)
        set_limit.assert_called_once_with(7, (1024, 1024))

        with mock.patch.object(
            git_limiter.resource,
            "getrlimit",
            return_value=(512, 512),
        ), mock.patch.object(git_limiter.resource, "setrlimit") as set_limit:
            git_limiter._limit(7, 1024)
        set_limit.assert_called_once_with(7, (512, 512))

    def test_git_invocation_uses_limiter_and_forces_packed_fetches(self):
        process = mock.Mock(pid=123, returncode=0)
        process.communicate.return_value = (None, None)
        with mock.patch.object(
            artifacts.subprocess,
            "Popen",
            return_value=process,
        ) as popen:
            output = artifacts._run_git(
                "/usr/bin/git",
                {},
                None,
                ["version"],
                deadline=time.monotonic() + 10,
            )

        self.assertEqual(output, b"")
        command = popen.call_args.args[0]
        self.assertEqual(
            command[:5],
            [
                sys.executable,
                "-I",
                "-S",
                str(artifacts._GIT_LIMITER),
                "/usr/bin/git",
            ],
        )
        self.assertIn("transfer.unpackLimit=1", command)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_git_timeout_kills_the_isolated_process_group(self):
        process = mock.Mock(pid=456, returncode=-9)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 1),
            (None, None),
        ]
        with mock.patch.object(
            artifacts.subprocess,
            "Popen",
            return_value=process,
        ), mock.patch.object(artifacts.os, "killpg") as kill_group:
            with self.assertRaisesRegex(artifacts.ArtifactError, "timed out"):
                artifacts._run_git(
                    "/usr/bin/git",
                    {},
                    None,
                    ["version"],
                    deadline=time.monotonic() + 10,
                )

        kill_group.assert_called_once_with(456, artifacts.signal.SIGKILL)

    def test_quarantine_measurement_rejects_aggregate_growth(self):
        quarantine = self.root / "quarantine"
        quarantine.mkdir()
        (quarantine / "one").write_bytes(b"1234")
        (quarantine / "two").write_bytes(b"5678")

        with self.assertRaisesRegex(artifacts.ArtifactError, "byte limit"):
            artifacts._quarantine_size(
                quarantine,
                7,
                time.monotonic() + 10,
            )


class SyntheticTreeTests(ArtifactCase):
    def make_workspace(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "a.c").write_bytes(b"root file\n")
        (workspace / "a").mkdir()
        (workspace / "a" / "child").write_bytes(b"nested\n")
        (workspace / "run").write_bytes(b"#!/bin/sh\n")
        os.chmod(workspace / "run", 0o751)
        os.symlink(b"a/child", os.fsencode(workspace / "link"))
        return workspace

    def test_tree_uses_git_modes_object_headers_and_canonical_order(self):
        workspace = self.make_workspace()

        tree = artifacts.synthetic_git_tree(
            workspace,
            maximum_bytes=WORKSPACE_LIMIT,
        )

        child = object_id(b"blob", b"nested\n")
        directory_body = b"100644 child\0" + child
        directory = object_id(b"tree", directory_body)
        root_file = object_id(b"blob", b"root file\n")
        executable = object_id(b"blob", b"#!/bin/sh\n")
        link = object_id(b"blob", b"a/child")
        root_body = b"".join((
            b"100644 a.c\0" + root_file,
            b"40000 a\0" + directory,
            b"120000 link\0" + link,
            b"100755 run\0" + executable,
        ))
        self.assertEqual(tree, object_id(b"tree", root_body).hex())

    def test_symlink_body_is_measured_without_following_its_target(self):
        workspace = self.root / "workspace"
        outside = self.root / "secret"
        workspace.mkdir()
        outside.write_bytes(b"secret bytes")
        os.symlink(b"../secret", os.fsencode(workspace / "link"))

        first = artifacts.synthetic_git_tree(workspace, maximum_bytes=100)
        outside.write_bytes(b"different secret bytes")
        second = artifacts.synthetic_git_tree(workspace, maximum_bytes=100)

        self.assertEqual(first, second)
        link = object_id(b"blob", b"../secret")
        self.assertEqual(
            first,
            object_id(b"tree", b"120000 link\0" + link).hex(),
        )

    def test_rejects_metadata_empty_directories_hardlinks_and_special_files(self):
        cases = []

        metadata = self.root / "metadata"
        metadata.mkdir()
        (metadata / ".GIT").mkdir()
        (metadata / ".GIT" / "config").write_text("unsafe", encoding="utf-8")
        cases.append((metadata, r"\.git metadata"))

        empty = self.root / "empty"
        empty.mkdir()
        (empty / "file").write_text("x", encoding="utf-8")
        (empty / "directory").mkdir()
        cases.append((empty, "must not be empty"))

        linked = self.root / "linked"
        linked.mkdir()
        (linked / "one").write_text("same inode", encoding="utf-8")
        os.link(linked / "one", linked / "two")
        cases.append((linked, "hard-linked"))

        if hasattr(os, "mkfifo"):
            special = self.root / "special"
            special.mkdir()
            os.mkfifo(special / "pipe")
            cases.append((special, "special or unsupported"))

        for workspace, message in cases:
            with self.subTest(workspace=workspace.name):
                with self.assertRaisesRegex(artifacts.ArtifactError, message):
                    artifacts.synthetic_git_tree(
                        workspace,
                        maximum_bytes=WORKSPACE_LIMIT,
                    )

    def test_rejects_a_symlink_workspace_root(self):
        target = self.root / "target"
        target.mkdir()
        (target / "file").write_text("x", encoding="utf-8")
        os.symlink(target.name, self.root / "workspace")

        with self.assertRaisesRegex(artifacts.ArtifactError, "safely open"):
            artifacts.synthetic_git_tree(
                self.root / "workspace",
                maximum_bytes=100,
            )

    def test_rejects_fstat_changes_during_measurement(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        file = workspace / "file"
        file.write_bytes(b"content")
        inode = file.stat().st_ino
        real_fstat = os.fstat
        seen = 0

        def changing_fstat(fd):
            nonlocal seen
            value = real_fstat(fd)
            if value.st_ino == inode:
                seen += 1
                if seen == 2:
                    fields = {
                        name: getattr(value, name)
                        for name in (
                            "st_dev",
                            "st_ino",
                            "st_mode",
                            "st_nlink",
                            "st_size",
                            "st_mtime_ns",
                            "st_ctime_ns",
                        )
                    }
                    fields["st_size"] += 1
                    return types.SimpleNamespace(**fields)
            return value

        with mock.patch.object(artifacts.os, "fstat", side_effect=changing_fstat):
            with self.assertRaisesRegex(artifacts.ArtifactError, "changed"):
                artifacts.synthetic_git_tree(workspace, maximum_bytes=100)

    def test_workspace_content_limit_is_checked_before_reading_past_it(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "file").write_bytes(b"12345")

        with self.assertRaisesRegex(artifacts.ArtifactError, "byte limit"):
            artifacts.synthetic_git_tree(workspace, maximum_bytes=4)


class WorkspaceArchiveTests(ArtifactCase):
    def make_workspace(self):
        return SyntheticTreeTests.make_workspace(self)

    def test_archive_round_trips_paths_modes_links_and_bodies(self):
        workspace = self.make_workspace()
        before = artifacts.synthetic_git_tree(workspace, maximum_bytes=WORKSPACE_LIMIT)

        archive = artifacts.pack_workspace(workspace, maximum_bytes=WORKSPACE_LIMIT)
        destination = self.root / "unpacked"
        after = artifacts.unpack_workspace(
            archive,
            destination,
            maximum_bytes=WORKSPACE_LIMIT,
        )

        self.assertEqual(after, before)
        self.assertEqual(
            artifacts.pack_workspace(destination, maximum_bytes=WORKSPACE_LIMIT),
            archive,
        )
        self.assertTrue((destination / "run").stat().st_mode & stat.S_IXUSR)
        self.assertTrue((destination / "link").is_symlink())
        self.assertEqual(os.readlink(destination / "link"), "a/child")

    @unittest.skipIf(sys.platform == "darwin", "APFS rejects non-UTF-8 names")
    def test_archive_round_trips_non_utf8_path_bytes(self):
        workspace = self.root / "raw"
        workspace.mkdir()
        root_fd = os.open(os.fsencode(workspace), os.O_RDONLY | os.O_DIRECTORY)
        try:
            file_fd = os.open(b"\xff", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=root_fd)
            os.write(file_fd, b"raw")
            os.close(file_fd)
        finally:
            os.close(root_fd)
        archive = artifacts.pack_workspace(workspace, maximum_bytes=1000)

        destination = self.root / "raw-copy"
        artifacts.unpack_workspace(archive, destination, maximum_bytes=1000)

        copied_fd = os.open(os.fsencode(destination), os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.assertIn(b"\xff", [os.fsencode(name) for name in os.listdir(copied_fd)])
        finally:
            os.close(copied_fd)

    def test_archive_is_canonical_and_bounded(self):
        workspace = self.make_workspace()
        archive = artifacts.pack_workspace(workspace, maximum_bytes=WORKSPACE_LIMIT)

        with self.assertRaisesRegex(artifacts.ArtifactError, "byte limit"):
            artifacts.pack_workspace(workspace, maximum_bytes=len(archive) - 1)
        with self.assertRaisesRegex(artifacts.ArtifactError, "byte limit"):
            artifacts.unpack_workspace(
                archive,
                self.root / "too-small",
                maximum_bytes=len(archive) - 1,
            )
        with self.assertRaisesRegex(artifacts.ArtifactError, "trailing bytes"):
            artifacts.unpack_workspace(
                archive + b"x",
                self.root / "trailing",
                maximum_bytes=WORKSPACE_LIMIT,
            )

    def test_archive_rejects_path_traversal_before_creating_destination(self):
        body = b"x"
        path = b"../outside"
        archive = b"".join((
            artifacts._ARCHIVE_MAGIC,
            artifacts._ARCHIVE_COUNT.pack(1),
            artifacts._ARCHIVE_RECORD.pack(b"f", len(path), len(body)),
            path,
            body,
        ))
        destination = self.root / "destination"

        with self.assertRaisesRegex(artifacts.ArtifactError, "invalid path component"):
            artifacts.unpack_workspace(
                archive,
                destination,
                maximum_bytes=1000,
            )

        self.assertFalse(destination.exists())
        self.assertFalse((self.root.parent / "outside").exists())


class SourceBundleTests(ArtifactCase):
    def source_bundle(self, *, submodule=False, prerequisite=False):
        repo = self.init_repo()
        (repo / "common").write_text("common\n", encoding="utf-8")
        self.git("-C", repo, "add", ".")
        self.git("-C", repo, "commit", "-m", "common")
        common = self.git("-C", repo, "rev-parse", "HEAD").decode("ascii")
        (repo / "base").write_text("base\n", encoding="utf-8")
        self.git("-C", repo, "add", ".")
        self.git("-C", repo, "commit", "-m", "base")
        base = self.git("-C", repo, "rev-parse", "HEAD").decode("ascii")
        (repo / "nested").mkdir()
        (repo / "nested" / "run").write_bytes(b"#!/bin/sh\n")
        os.chmod(repo / "nested" / "run", 0o755)
        os.symlink(b"base", os.fsencode(repo / "link"))
        self.git("-C", repo, "add", ".")
        if submodule:
            self.git(
                "-C",
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{base},submodule",
            )
        self.git("-C", repo, "commit", "-m", "head")
        head = self.git("-C", repo, "rev-parse", "HEAD").decode("ascii")
        self.git("-C", repo, "update-ref", artifacts.SOURCE_REFS[0], base)
        self.git("-C", repo, "update-ref", artifacts.SOURCE_REFS[1], head)
        bundle_path = self.root / ("prerequisite.bundle" if prerequisite else "source.bundle")
        arguments = [
            "-C", repo,
            "bundle", "create", bundle_path,
            *artifacts.SOURCE_REFS,
        ]
        if prerequisite:
            arguments.append(f"^{common}")
        self.git(*arguments)
        bundle = bundle_path.read_bytes()
        target = {
            "base_sha": base,
            "head_sha": head,
            "object_bundle_sha256": hashlib.sha256(bundle).hexdigest(),
            "object_bundle_bytes": len(bundle),
        }
        return bundle, target

    def test_source_bundle_is_hash_bound_quarantined_and_manually_materialized(self):
        bundle, target = self.source_bundle()
        workspace = self.root / "workspace"

        verified = artifacts.verify_source_bundle(
            bundle,
            target,
            workspace,
            maximum_workspace_bytes=WORKSPACE_LIMIT,
        )

        self.assertEqual(verified.workspace, workspace)
        self.assertEqual(verified.input_tree, verified.descriptor.git_tree)
        self.assertEqual(verified.descriptor.sha256, target["object_bundle_sha256"])
        self.assertEqual(verified.descriptor.size, target["object_bundle_bytes"])
        self.assertEqual(
            verified.descriptor.git_tree,
            artifacts.synthetic_git_tree(
                workspace,
                maximum_bytes=WORKSPACE_LIMIT,
            ),
        )
        self.assertFalse((workspace / ".git").exists())
        self.assertEqual((workspace / "base").read_text(encoding="utf-8"), "base\n")
        self.assertTrue((workspace / "nested" / "run").stat().st_mode & stat.S_IXUSR)
        self.assertEqual(os.readlink(workspace / "link"), "base")

    def test_source_bundle_stays_packed_in_quarantine(self):
        bundle, _target = self.source_bundle()
        executable = artifacts._git_path("git")
        temporary, repo, _environment, _refs = artifacts._quarantine_bundle(
            bundle,
            "sha1",
            artifacts.SOURCE_REFS,
            executable,
            artifacts._git_deadline(),
        )
        try:
            self.assertTrue(list((repo / "objects" / "pack").glob("*.pack")))
            loose = [
                path
                for path in (repo / "objects").iterdir()
                if path.is_dir()
                and len(path.name) == 2
                and all(character in "0123456789abcdef" for character in path.name)
            ]
            self.assertEqual(loose, [])
        finally:
            temporary.cleanup()

    def test_source_bundle_rejects_transport_or_frozen_ref_mismatch(self):
        bundle, target = self.source_bundle()

        for change, message in (
            ({"object_bundle_sha256": "0" * 64}, "bytes do not match"),
            ({"object_bundle_bytes": len(bundle) + 1}, "bytes do not match"),
            ({"head_sha": target["base_sha"]}, "refs do not match"),
        ):
            with self.subTest(change=change):
                changed = dict(target)
                changed.update(change)
                with self.assertRaisesRegex(artifacts.ArtifactError, message):
                    artifacts.verify_source_bundle(
                        bundle,
                        changed,
                        self.root / ("workspace-" + next(iter(change))),
                        maximum_workspace_bytes=WORKSPACE_LIMIT,
                    )

    def test_source_bundle_rejects_prerequisites(self):
        bundle, target = self.source_bundle(prerequisite=True)

        with self.assertRaisesRegex(artifacts.ArtifactError, "prerequisites"):
            artifacts.verify_source_bundle(
                bundle,
                target,
                self.root / "workspace",
                maximum_workspace_bytes=WORKSPACE_LIMIT,
            )

    def test_source_bundle_rejects_submodules(self):
        bundle, target = self.source_bundle(submodule=True)

        with self.assertRaisesRegex(artifacts.ArtifactError, "submodules"):
            artifacts.verify_source_bundle(
                bundle,
                target,
                self.root / "workspace",
                maximum_workspace_bytes=WORKSPACE_LIMIT,
            )

    def test_source_bundle_rejects_missing_pack_data(self):
        bundle, target = self.source_bundle()
        truncated = bundle[:-20]
        target["object_bundle_sha256"] = hashlib.sha256(truncated).hexdigest()
        target["object_bundle_bytes"] = len(truncated)

        with self.assertRaisesRegex(artifacts.ArtifactError, "Git artifact verification"):
            artifacts.verify_source_bundle(
                truncated,
                target,
                self.root / "workspace",
                maximum_workspace_bytes=WORKSPACE_LIMIT,
            )


class OutputBundleTests(ArtifactCase):
    def workspace(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "result").write_bytes(b"review result\n")
        (workspace / "bin").mkdir()
        (workspace / "bin" / "tool").write_bytes(b"#!/bin/sh\n")
        os.chmod(workspace / "bin" / "tool", 0o755)
        os.symlink(b"result", os.fsencode(workspace / "latest"))
        return workspace

    def test_output_bundle_uses_fixed_ref_and_round_trips_synthetic_tree(self):
        workspace = self.workspace()
        tree = artifacts.synthetic_git_tree(workspace, maximum_bytes=WORKSPACE_LIMIT)

        built = artifacts.build_output_bundle(
            workspace,
            maximum_workspace_bytes=WORKSPACE_LIMIT,
            maximum_bundle_bytes=BUNDLE_LIMIT,
        )
        verified = artifacts.verify_output_bundle(
            built.bundle,
            maximum_workspace_bytes=WORKSPACE_LIMIT,
            maximum_bundle_bytes=BUNDLE_LIMIT,
        )

        self.assertEqual(built.descriptor, verified)
        self.assertEqual(verified.git_tree, tree)
        self.assertEqual(verified.sha256, hashlib.sha256(built.bundle).hexdigest())
        self.assertEqual(verified.size, len(built.bundle))
        self.assertEqual(verified.bytes, len(built.bundle))
        self.assertEqual(verified.gitTree, tree)
        self.assertEqual(
            verified.protocol_value(),
            {
                "gitTree": tree,
                "sha256": verified.sha256,
                "bytes": len(built.bundle),
            },
        )
        bundle_path = self.root / "output.bundle"
        bundle_path.write_bytes(built.bundle)
        heads = self.git("bundle", "list-heads", bundle_path).decode("ascii")
        self.assertEqual(heads.split(" ", 1)[1], artifacts.OUTPUT_REF)

    def test_output_bundle_expected_descriptor_is_exact(self):
        built = artifacts.build_output_bundle(
            self.workspace(),
            maximum_workspace_bytes=WORKSPACE_LIMIT,
            maximum_bundle_bytes=BUNDLE_LIMIT,
        )
        wrong = replace(built.descriptor, git_tree="0" * 64)

        with self.assertRaisesRegex(artifacts.ArtifactError, "expected descriptor"):
            artifacts.verify_output_bundle(
                built.bundle,
                maximum_workspace_bytes=WORKSPACE_LIMIT,
                maximum_bundle_bytes=BUNDLE_LIMIT,
                expected=wrong,
            )

    def test_expected_output_bundle_materializes_exact_tree(self):
        workspace = self.workspace()
        built = artifacts.build_output_bundle(
            workspace,
            maximum_workspace_bytes=WORKSPACE_LIMIT,
            maximum_bundle_bytes=BUNDLE_LIMIT,
        )
        destination = self.root / "verified-output"

        descriptor = artifacts.materialize_output_bundle(
            built.bundle,
            destination,
            maximum_workspace_bytes=WORKSPACE_LIMIT,
            maximum_bundle_bytes=BUNDLE_LIMIT,
            expected=built.descriptor,
        )

        self.assertEqual(descriptor, built.descriptor)
        self.assertEqual(
            artifacts.synthetic_git_tree(
                destination,
                maximum_bytes=WORKSPACE_LIMIT,
            ),
            built.descriptor.git_tree,
        )
        self.assertEqual((destination / "result").read_bytes(), b"review result\n")
        self.assertTrue((destination / "bin" / "tool").stat().st_mode & stat.S_IXUSR)
        self.assertEqual(os.readlink(destination / "latest"), "result")

    def test_output_materialization_fails_closed_before_destination_creation(self):
        built = artifacts.build_output_bundle(
            self.workspace(),
            maximum_workspace_bytes=WORKSPACE_LIMIT,
            maximum_bundle_bytes=BUNDLE_LIMIT,
        )
        destination = self.root / "untrusted-output"
        wrong = replace(built.descriptor, git_tree="0" * 64)

        with self.assertRaisesRegex(artifacts.ArtifactError, "expected descriptor"):
            artifacts.materialize_output_bundle(
                built.bundle,
                destination,
                maximum_workspace_bytes=WORKSPACE_LIMIT,
                maximum_bundle_bytes=BUNDLE_LIMIT,
                expected=wrong,
            )
        self.assertFalse(destination.exists())

        with self.assertRaisesRegex(artifacts.ArtifactError, "expected descriptor"):
            artifacts.materialize_output_bundle(
                built.bundle[:-16],
                destination,
                maximum_workspace_bytes=WORKSPACE_LIMIT,
                maximum_bundle_bytes=BUNDLE_LIMIT,
                expected=built.descriptor,
            )
        self.assertFalse(destination.exists())

    def test_output_materialization_never_reuses_an_existing_destination(self):
        built = artifacts.build_output_bundle(
            self.workspace(),
            maximum_workspace_bytes=WORKSPACE_LIMIT,
            maximum_bundle_bytes=BUNDLE_LIMIT,
        )
        destination = self.root / "existing"
        destination.mkdir()
        sentinel = destination / "sentinel"
        sentinel.write_bytes(b"keep")

        with self.assertRaisesRegex(artifacts.ArtifactError, "must not already exist"):
            artifacts.materialize_output_bundle(
                built.bundle,
                destination,
                maximum_workspace_bytes=WORKSPACE_LIMIT,
                maximum_bundle_bytes=BUNDLE_LIMIT,
                expected=built.descriptor,
            )

        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_output_bundle_is_bounded_and_integrity_checked(self):
        built = artifacts.build_output_bundle(
            self.workspace(),
            maximum_workspace_bytes=WORKSPACE_LIMIT,
            maximum_bundle_bytes=BUNDLE_LIMIT,
        )

        with self.assertRaisesRegex(artifacts.ArtifactError, "byte limit"):
            artifacts.verify_output_bundle(
                built.bundle,
                maximum_workspace_bytes=WORKSPACE_LIMIT,
                maximum_bundle_bytes=len(built.bundle) - 1,
            )
        with self.assertRaisesRegex(artifacts.ArtifactError, "verification"):
            artifacts.verify_output_bundle(
                built.bundle[:-16],
                maximum_workspace_bytes=WORKSPACE_LIMIT,
                maximum_bundle_bytes=BUNDLE_LIMIT,
            )

    def test_output_bundle_rejects_submodules(self):
        repo = self.init_repo("sha256", object_format="sha256")
        (repo / "file").write_text("x", encoding="utf-8")
        self.git("-C", repo, "add", ".")
        self.git("-C", repo, "commit", "-m", "commit object")
        commit = self.git("-C", repo, "rev-parse", "HEAD").decode("ascii")
        self.git(
            "-C",
            repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{commit},submodule",
        )
        self.git("-C", repo, "commit", "-m", "output")
        self.git("-C", repo, "update-ref", artifacts.OUTPUT_REF, "HEAD")
        bundle_path = self.root / "submodule.bundle"
        self.git("-C", repo, "bundle", "create", bundle_path, artifacts.OUTPUT_REF)

        with self.assertRaisesRegex(artifacts.ArtifactError, "submodules"):
            artifacts.verify_output_bundle(
                bundle_path.read_bytes(),
                maximum_workspace_bytes=WORKSPACE_LIMIT,
                maximum_bundle_bytes=BUNDLE_LIMIT,
            )

    def test_output_ref_must_point_directly_to_a_commit(self):
        repo = self.init_repo("tagged", object_format="sha256")
        (repo / "file").write_text("x", encoding="utf-8")
        self.git("-C", repo, "add", ".")
        self.git("-C", repo, "commit", "-m", "output")
        self.git("-C", repo, "tag", "-a", "tagged-output", "-m", "tag")
        tag = self.git("-C", repo, "rev-parse", "tagged-output").decode("ascii")
        self.git("-C", repo, "update-ref", artifacts.OUTPUT_REF, tag)
        bundle_path = self.root / "tagged.bundle"
        self.git("-C", repo, "bundle", "create", bundle_path, artifacts.OUTPUT_REF)

        with self.assertRaisesRegex(artifacts.ArtifactError, "directly"):
            artifacts.verify_output_bundle(
                bundle_path.read_bytes(),
                maximum_workspace_bytes=WORKSPACE_LIMIT,
                maximum_bundle_bytes=BUNDLE_LIMIT,
            )


class CommitTreeMeasurementTests(ArtifactCase):
    def make_commit(self):
        repo = self.init_repo("candidate")
        (repo / "payload").write_bytes(b"committed\n")
        (repo / "bin").mkdir()
        (repo / "bin" / "tool").write_bytes(b"#!/bin/sh\n")
        os.chmod(repo / "bin" / "tool", 0o755)
        os.symlink(b"payload", os.fsencode(repo / "latest"))
        self.git("-C", repo, "add", ".")
        self.git("-C", repo, "commit", "-m", "candidate")
        commit = self.git("-C", repo, "rev-parse", "HEAD").decode("ascii")
        expected = self.root / "expected"
        expected.mkdir()
        (expected / "payload").write_bytes(b"committed\n")
        (expected / "bin").mkdir()
        (expected / "bin" / "tool").write_bytes(b"#!/bin/sh\n")
        os.chmod(expected / "bin" / "tool", 0o755)
        os.symlink(b"payload", os.fsencode(expected / "latest"))
        return repo, commit, expected

    def test_measures_one_exact_commit_independent_of_worktree_state(self):
        repo, commit, expected = self.make_commit()
        (repo / "payload").write_bytes(b"uncommitted replacement\n")

        tree = artifacts.measure_commit_tree(
            repo,
            commit,
            maximum_workspace_bytes=WORKSPACE_LIMIT,
        )

        self.assertEqual(
            tree,
            artifacts.synthetic_git_tree(
                expected,
                maximum_bytes=WORKSPACE_LIMIT,
            ),
        )

    def test_commit_measurement_rejects_refs_abbreviations_and_sha256_repos(self):
        repo, commit, _expected = self.make_commit()
        for candidate in ("HEAD", commit[:12], commit.upper()):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(artifacts.ArtifactError, "full lowercase"):
                    artifacts.measure_commit_tree(
                        repo,
                        candidate,
                        maximum_workspace_bytes=WORKSPACE_LIMIT,
                    )

        sha256 = self.init_repo("sha256-candidate", object_format="sha256")
        with self.assertRaisesRegex(artifacts.ArtifactError, "SHA-1 objects"):
            artifacts.measure_commit_tree(
                sha256,
                "0" * 40,
                maximum_workspace_bytes=WORKSPACE_LIMIT,
            )

    def test_commit_measurement_does_not_run_repository_filters(self):
        repo = self.init_repo("filtered")
        marker = self.root / "filter-ran"
        (repo / ".gitattributes").write_text(
            "payload filter=hostile\n",
            encoding="utf-8",
        )
        (repo / "payload").write_bytes(b"raw committed bytes\n")
        self.git("-C", repo, "add", ".")
        self.git("-C", repo, "commit", "-m", "filtered candidate")
        commit = self.git("-C", repo, "rev-parse", "HEAD").decode("ascii")
        self.git(
            "-C",
            repo,
            "config",
            "filter.hostile.smudge",
            f"touch {marker}",
        )

        tree = artifacts.measure_commit_tree(
            repo,
            commit,
            maximum_workspace_bytes=WORKSPACE_LIMIT,
        )

        self.assertRegex(tree, r"\A[0-9a-f]{64}\Z")
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
