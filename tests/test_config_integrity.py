import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("config_integrity", str(ROOT / "config-integrity"))
spec = importlib.util.spec_from_loader(loader.name, loader)
ci = importlib.util.module_from_spec(spec)
loader.exec_module(ci)


def completed(args, rc=0, out="", err=""):
    return subprocess.CompletedProcess(args, rc, out, err)


class Harness:
    def __init__(self, failed=(), rc=2, malformed=None, packages=None):
        self.failed = list(failed)
        self.rc = rc
        self.malformed = malformed
        self.packages = packages or {}

    def __call__(self, args):
        if args[:2] == ["debsums", "-e"]:
            output = "".join(f"{p}   FAILED\n" for p in self.failed)
            if self.malformed:
                output += self.malformed + "\n"
            return completed(args, self.rc, output, "")
        path = args[-1]
        owner = self.packages.get(path)
        return completed(args, 0, f"{owner}: {path}\n", "") if owner else completed(args, 1)


class ConfigIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.baseline = self.root / "state" / "baseline.json"
        self.extras = self.root / "extra-files"

    def tearDown(self):
        self.temp.cleanup()

    def args(self, **values):
        defaults = {"baseline": self.baseline, "extra_files": self.extras,
                    "force": False, "verbose": False, "yes": False}
        defaults.update(values)
        return types.SimpleNamespace(**defaults)

    def write_baseline(self, files):
        ci.write_baseline_atomic(self.baseline, ci.make_baseline(files))

    def entry(self, path, source="debsums", content=None):
        if content is None:
            content = path.read_bytes()
        return {"sha256": hashlib.sha256(content).hexdigest(), "source": source}

    def capture(self, function, *args, **kwargs):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            result = function(*args, **kwargs)
        return result, stream.getvalue()

    def test_empty_initial_state(self):
        rc, output = self.capture(ci.cmd_init, self.args(), Harness([], rc=0))
        self.assertEqual(rc, 0)
        self.assertEqual(ci.load_baseline(self.baseline)["files"], {})
        self.assertIn("Stored 0", output)

    def test_initial_baseline_one_failed_file_and_package(self):
        path = self.root / "one.conf"; path.write_text("one")
        runner = Harness([path], packages={str(path): "sample"})
        ci.cmd_init(self.args(), runner)
        entry = ci.load_baseline(self.baseline)["files"][str(path)]
        self.assertEqual(entry["package"], "sample")
        self.assertEqual(entry["source"], "debsums")

    def test_unchanged_tracked_file_and_clean_exit(self):
        path = self.root / "one"; path.write_text("same")
        self.write_baseline({str(path): self.entry(path)})
        rc, output = self.capture(ci.cmd_check, self.args(), Harness([path]))
        self.assertEqual(rc, ci.EXIT_OK)
        self.assertNotIn("UNCHANGED", output)

    def test_verbose_displays_unchanged(self):
        path = self.root / "one"; path.write_text("same")
        self.write_baseline({str(path): self.entry(path)})
        rc, output = self.capture(ci.cmd_check, self.args(verbose=True), Harness([path]))
        self.assertEqual(rc, 0); self.assertIn("UNCHANGED", output)

    def test_tracked_file_content_changes_and_difference_exit(self):
        path = self.root / "one"; path.write_text("old")
        self.write_baseline({str(path): self.entry(path)})
        path.write_text("new")
        rc, output = self.capture(ci.cmd_check, self.args(), Harness([path]))
        self.assertEqual(rc, ci.EXIT_DIFFERENCES); self.assertIn("CHANGED", output)
        self.assertIn("old:", output); self.assertIn("new:", output)

    def test_new_failed_file(self):
        path = self.root / "new"; path.write_text("new")
        self.write_baseline({})
        rc, output = self.capture(ci.cmd_check, self.args(), Harness([path]))
        self.assertEqual(rc, 1); self.assertIn(f"NEW {path}", output)

    def test_tracked_file_disappears(self):
        path = self.root / "gone"; path.write_text("old")
        self.write_baseline({str(path): self.entry(path)})
        path.unlink()
        rc, output = self.capture(ci.cmd_check, self.args(), Harness([path]))
        self.assertEqual(rc, 1); self.assertIn("REMOVED", output)

    def test_tracked_file_disappears_and_is_not_reported(self):
        path = self.root / "gone"; path.write_text("old")
        self.write_baseline({str(path): self.entry(path)})
        path.unlink()
        rc, output = self.capture(ci.cmd_check, self.args(), Harness([], rc=0))
        self.assertEqual(rc, 1); self.assertIn("REMOVED", output)

    def test_previously_failed_file_is_restored(self):
        path = self.root / "restored"; path.write_text("packaged")
        self.write_baseline({str(path): self.entry(path, content=b"modified")})
        rc, output = self.capture(ci.cmd_check, self.args(), Harness([], rc=0))
        self.assertEqual(rc, 1); self.assertIn("RESTORED", output)

    def test_multiple_simultaneous_changes(self):
        changed = self.root / "changed"; changed.write_text("old")
        removed = self.root / "removed"; removed.write_text("old")
        restored = self.root / "restored"; restored.write_text("old")
        files = {str(p): self.entry(p) for p in (changed, removed, restored)}
        self.write_baseline(files); changed.write_text("new"); removed.unlink()
        new = self.root / "new"; new.write_text("new")
        rc, output = self.capture(ci.cmd_check, self.args(), Harness([changed, removed, new]))
        self.assertEqual(rc, 1)
        for state in ("CHANGED", "REMOVED", "RESTORED", "NEW"):
            self.assertIn(state, output)

    def test_path_containing_spaces(self):
        path = self.root / "a config file"; path.write_text("x")
        parsed = ci.parse_debsums_output(f"{path}     FAILED\n", "")
        self.assertEqual(parsed, {path})

    def test_malformed_debsums_output(self):
        with self.assertRaisesRegex(ci.OperationalError, "unexpected output"):
            ci.run_debsums(Harness([], malformed="debsums: warning nonsense"))

    def test_debsums_operational_failure(self):
        with self.assertRaisesRegex(ci.OperationalError, "exit status 1"):
            ci.run_debsums(Harness([], rc=1))

    def test_debsums_exit_two_is_expected(self):
        self.assertEqual(ci.run_debsums(Harness([], rc=2)), set())

    def test_missing_baseline(self):
        with self.assertRaisesRegex(ci.OperationalError, "does not exist"):
            ci.load_baseline(self.baseline)

    def test_corrupt_json_baseline(self):
        self.baseline.parent.mkdir(); self.baseline.write_text("{")
        with self.assertRaisesRegex(ci.OperationalError, "not valid JSON"):
            ci.load_baseline(self.baseline)

    def test_unsupported_baseline_version(self):
        self.baseline.parent.mkdir(); self.baseline.write_text('{"version": 99, "files": {}}')
        with self.assertRaisesRegex(ci.OperationalError, "unsupported"):
            ci.load_baseline(self.baseline)

    def test_refuses_overwrite_without_force(self):
        self.write_baseline({})
        with self.assertRaisesRegex(ci.OperationalError, "already exists"):
            ci.cmd_init(self.args(), Harness([], rc=0))

    def test_successful_forced_init(self):
        self.write_baseline({})
        path = self.root / "new"; path.write_text("x")
        self.assertEqual(ci.cmd_init(self.args(force=True), Harness([path])), 0)
        self.assertIn(str(path), ci.load_baseline(self.baseline)["files"])

    def test_update_confirmation_rejected(self):
        self.write_baseline({})
        original = self.baseline.read_bytes()
        rc, output = self.capture(ci.cmd_update, self.args(), Harness([], rc=0), lambda _: "no")
        self.assertEqual(rc, 1); self.assertEqual(self.baseline.read_bytes(), original)
        self.assertIn("not changed", output)

    def test_update_confirmation_accepted(self):
        self.write_baseline({})
        path = self.root / "new"; path.write_text("x")
        rc, _ = self.capture(ci.cmd_update, self.args(), Harness([path]), lambda _: "yes")
        self.assertEqual(rc, 0); self.assertIn(str(path), ci.load_baseline(self.baseline)["files"])

    def test_yes_update_does_not_prompt(self):
        self.write_baseline({})
        def fail_prompt(_): self.fail("prompt called")
        rc, _ = self.capture(ci.cmd_update, self.args(yes=True), Harness([], rc=0), fail_prompt)
        self.assertEqual(rc, 0)

    def test_update_can_accept_tracked_removal(self):
        path = self.root / "gone"; path.write_text("old")
        self.write_baseline({str(path): self.entry(path, "extra")})
        path.unlink()
        rc, output = self.capture(ci.cmd_update, self.args(yes=True), Harness([], rc=0))
        self.assertEqual(rc, 0); self.assertIn("REMOVED", output)
        self.assertEqual(ci.load_baseline(self.baseline)["files"], {})

    def test_atomic_baseline_replacement(self):
        self.write_baseline({})
        old_inode = self.baseline.stat().st_ino
        ci.write_baseline_atomic(self.baseline, ci.make_baseline({}))
        self.assertNotEqual(old_inode, self.baseline.stat().st_ino)
        self.assertFalse(list(self.baseline.parent.glob(".baseline.json.*")))

    def test_sha256_known_value(self):
        path = self.root / "hash"; path.write_bytes(b"abc")
        self.assertEqual(ci.hash_regular_file(path),
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_extra_file_unchanged(self):
        path = self.root / "extra"; path.write_text("same")
        self.extras.write_text(f"{path}\n")
        self.write_baseline({str(path): self.entry(path, "extra")})
        rc, _ = self.capture(ci.cmd_check, self.args(), Harness([], rc=0))
        self.assertEqual(rc, 0)

    def test_extra_file_changed(self):
        path = self.root / "extra"; path.write_text("old")
        self.extras.write_text(f"{path}\n"); self.write_baseline({str(path): self.entry(path, "extra")})
        path.write_text("new")
        rc, output = self.capture(ci.cmd_check, self.args(), Harness([], rc=0))
        self.assertEqual(rc, 1); self.assertIn("CHANGED", output)

    def test_extra_file_removed(self):
        path = self.root / "extra"; path.write_text("old")
        self.extras.write_text(f"{path}\n"); self.write_baseline({str(path): self.entry(path, "extra")})
        path.unlink()
        rc, output = self.capture(ci.cmd_check, self.args(), Harness([], rc=0))
        self.assertEqual(rc, 1); self.assertIn("REMOVED", output)

    def test_extra_comments_and_blanks(self):
        path = self.root / "extra"; path.write_text("x")
        self.extras.write_text(f"\n# comment\n  # also comment\n{path}\n")
        self.assertEqual(ci.parse_extra_files(self.extras), {path})

    def test_relative_extra_rejected(self):
        self.extras.write_text("relative/path\n")
        with self.assertRaises(ci.OperationalError): ci.parse_extra_files(self.extras)

    def test_symlink_handling(self):
        target = self.root / "target"; target.write_text("secret")
        link = self.root / "link"; link.symlink_to(target)
        with self.assertRaisesRegex(ci.OperationalError, "symlink"):
            ci.hash_regular_file(link)

    def test_tracked_path_replaced_by_symlink(self):
        path = self.root / "tracked"; path.write_text("old")
        self.write_baseline({str(path): self.entry(path)})
        path.unlink(); path.symlink_to(self.root / "elsewhere")
        with self.assertRaisesRegex(ci.OperationalError, "become a symlink"):
            ci.cmd_check(self.args(), Harness([], rc=0))

    def test_operational_error_exit_code(self):
        with mock.patch.object(ci, "require_root"), mock.patch.object(
                ci, "run_command", side_effect=ci.OperationalError("failure")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(ci.main(["check", "--baseline", str(self.baseline)]), 2)

    def test_baseline_permissions(self):
        self.write_baseline({})
        self.assertEqual(stat.S_IMODE(self.baseline.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.baseline.parent.stat().st_mode), 0o700)

    def test_init_missing_extra_is_error(self):
        path = self.root / "missing"; self.extras.write_text(f"{path}\n")
        with self.assertRaisesRegex(ci.OperationalError, "monitored files are missing"):
            ci.cmd_init(self.args(), Harness([], rc=0))

    def test_package_lookup_failure_does_not_prevent_hashing(self):
        path = self.root / "file"; path.write_text("x")
        files, _ = ci.discover(self.extras, Harness([path]))
        self.assertNotIn("package", files[str(path)])


if __name__ == "__main__":
    unittest.main()
