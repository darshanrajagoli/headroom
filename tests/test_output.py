"""Serial bounded fleet-output integration tests: python tests/test_output.py -v.

No build step. Wait for the main agent's build notice before running this file.
Only fresh owned fixtures run, under the shared serial_fleet lock. Each native
invocation has a 20-second fleet deadline and a 30-second test deadline. The
existing fleet coordinator-crash test remains the Job-handle inheritance check.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import headroom
from test_fleet import (FLEET, alive, make_plan, pressure_plan,
                        read_events, select, serial_fleet, until)

WORKER = ROOT / "tests" / "output_worker.py"
LIMIT = 1048576
ARTIFACT_ROOT = None


def command(mode, output, *options):
    return [sys.executable, str(WORKER), mode, str(output), *map(str, options)]


def job(identifier, mode, output, *options, claim=48, hard=128):
    return {"id": identifier, "claim_mib": claim, "hard_mib": hard,
            "argv": command(mode, output, *options)}


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@unittest.skipUnless(os.name == "nt" and FLEET.is_file(), "native fleet executable unavailable")
class OutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.serial = serial_fleet()
        cls.serial.__enter__()
        cls.addClassCleanup(cls.serial.__exit__, None, None, None)

    def setUp(self):
        if ARTIFACT_ROOT is None:
            temporary = tempfile.TemporaryDirectory(prefix="headroom-output-")
            self.addCleanup(temporary.cleanup)
            self.base = Path(temporary.name)
        else:
            self.base = ARTIFACT_ROOT / self._testMethodName
            self.base.mkdir()
        self.terminals_seen = set()
        self.terminal_snapshots = {}

    def start(self, plan, *, base=None):
        base = Path(base) if base else self.base
        base.mkdir(parents=True, exist_ok=True)
        self.state = base / "state"
        self.assertFalse(self.state.exists(), "each run must use fresh state")
        self.terminals_seen = set()
        self.terminal_snapshots = {}
        plan = dict(plan, timeout_ms=20000)
        compiled = base / "plan.hrp"
        compiled.write_bytes(headroom.compile_plan(plan))
        (base / "input-plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        self.binary_hash = hashlib.sha256(FLEET.read_bytes()).hexdigest()
        self.run_deadline = time.monotonic() + 30
        process = subprocess.Popen([str(FLEET), "--plan", str(compiled),
                                    "--state", str(self.state)],
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.cleanup_process, process, base)
        return process

    def cleanup_process(self, process, base):
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate(timeout=8)
        (base / "coordinator-stdout.log").write_bytes(stdout)
        (base / "coordinator-stderr.log").write_bytes(stderr)
        (base / "process-outcome.json").write_text(json.dumps({
            "returncode": process.returncode, "native_sha256": self.binary_hash}), encoding="utf-8")

    def attempt_dir(self, task, attempt=1):
        return self.state / task / ("attempt-" + str(attempt))

    def snapshot(self, task, attempt):
        directory = self.attempt_dir(task, attempt)
        metadata = load_json(directory / "output.json")
        self.assertEqual(metadata["version"], 1)
        self.assertEqual(metadata["limit_bytes"], LIMIT)
        streams = {}
        for name in ("stdout", "stderr"):
            data = (directory / (name + ".log")).read_bytes()
            item = metadata[name]
            self.assertIs(type(item["retained_bytes"]), int)
            self.assertIs(type(item["discarded_bytes"]), int)
            self.assertIs(item["eof"], True)
            self.assertEqual(item["retained_bytes"], len(data))
            self.assertLessEqual(len(data), LIMIT)
            self.assertGreaterEqual(item["discarded_bytes"], 0)
            streams[name] = data
        return metadata, streams

    def observe_terminals(self):
        # Read only complete journal lines. A visible terminal record must
        # already have complete, readable output evidence, even while peers run.
        events = read_events(self.state, partial=True)
        for event in events:
            if event["event"] not in ("completed", "stopped"):
                continue
            key = (event["task"], event["attempt"])
            if key not in self.terminals_seen:
                self.terminal_snapshots[key] = self.snapshot(*key)
                self.terminals_seen.add(key)
        return events

    def finish(self, process, *, expected=0):
        while process.poll() is None:
            self.observe_terminals()
            self.assertLess(time.monotonic(), self.run_deadline,
                            "bounded native output fixture deadline expired")
            time.sleep(0.01)
        stdout, stderr = process.communicate(timeout=3)
        events = self.observe_terminals()
        self.assertEqual(process.returncode, expected,
                         stderr.decode(errors="replace") + "\n" + repr(events))
        self.assertFalse(select(events, "internal_error"), events)
        self.assertFalse(select(events, "deadline"), events)
        self.assertEqual(events[-1]["event"], "finished")
        self.assertEqual(hashlib.sha256(FLEET.read_bytes()).hexdigest(), self.binary_hash,
                         "native binary changed during test run")
        for event in select(events, "started"):
            key = (event["task"], event["attempt"])
            self.assertIn(key, self.terminals_seen)
            # Nothing may append or mutate counters after terminal publication.
            self.assertEqual(self.snapshot(*key), self.terminal_snapshots[key])
            local = [json.loads(line) for line in
                     (self.attempt_dir(*key) / "events.jsonl").read_bytes().splitlines()]
            self.assertTrue(any(item["event"] in ("stopped", "completed") for item in local))
        return events, stdout, stderr

    def assert_output(self, task, stdout, stderr, *, attempt=1,
                      discarded_stdout=0, discarded_stderr=0):
        metadata, streams = self.snapshot(task, attempt)
        self.assertEqual(streams, {"stdout": stdout, "stderr": stderr})
        for name, data, discarded in (("stdout", stdout, discarded_stdout),
                                      ("stderr", stderr, discarded_stderr)):
            self.assertEqual(metadata[name], {"retained_bytes": len(data),
                "discarded_bytes": discarded, "eof": True})

    def test_unicode_binary_and_streams_remain_byte_exact(self):
        output = self.base / "नमस्ते output"
        events, parent_out, parent_err = self.finish(self.start(make_plan([
            job("normal", "normal", output)])))
        stdout = "stdout: नमस्ते 🌱\n".encode("utf-8") + bytes(range(256)) + b"\x00\xff\r\n\x1a"
        stderr = "stderr: Καλημέρα 🔥\n".encode("utf-8") + bytes(reversed(range(256))) + b"\x80\x00\n"
        self.assert_output("normal", stdout, stderr)
        self.assertEqual(select(events, "completed", "normal")[0]["code"], 0)
        self.assertNotIn(stdout, parent_out)
        self.assertNotIn(stderr, parent_err)

    def test_empty_streams_still_create_logs_and_eof_metadata(self):
        self.finish(self.start(make_plan([job("empty", "empty", self.base / "empty")])))
        self.assert_output("empty", b"", b"")

    def test_both_streams_flood_truncate_exactly_and_drain_excess(self):
        output = self.base / "flood"
        self.finish(self.start(make_plan([job("flood", "flood", output)])))
        self.assertIs(load_json(output / "writes-finished.json"), True)
        self.assert_output("flood", bytes(range(256)) * 4096,
                           bytes(reversed(range(256))) * 4096,
                           discarded_stdout=65536, discarded_stderr=65536)

    def test_one_busy_stream_cannot_wait_for_empty_peer(self):
        for name in ("stdout", "stderr"):
            with self.subTest(stream=name):
                base = self.base / name
                output = base / "worker"
                options = ("--stderr-bytes", 0) if name == "stdout" else ("--stdout-bytes", 0)
                self.finish(self.start(make_plan([job("flood", "flood", output, *options)]), base=base))
                self.assertIs(load_json(output / "writes-finished.json"), True)
                self.assert_output("flood",
                    bytes(range(256)) * 4096 if name == "stdout" else b"",
                    bytes(reversed(range(256))) * 4096 if name == "stderr" else b"",
                    discarded_stdout=65536 if name == "stdout" else 0,
                    discarded_stderr=65536 if name == "stderr" else 0)

    def wait_for_root_exit(self, output):
        until(lambda: (output / "tree.json").is_file())
        tree = load_json(output / "tree.json")
        until(lambda: not alive(tree["root_pid"]))
        self.assertTrue(alive(tree["child_pid"]))
        return tree

    def assert_not_terminal(self, task, process, duration=0.6):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            events = self.observe_terminals()
            self.assertIsNone(process.poll(), "fleet exited with a live owned descendant")
            self.assertFalse(select(events, "completed", task), events)
            self.assertFalse(select(events, "stopped", task), events)
            self.assertFalse((self.attempt_dir(task) / "output.json").exists(),
                             "final output evidence published before owned group became empty")
            time.sleep(0.01)

    def test_descendant_late_output_survives_root_exit(self):
        output = self.base / "tree"
        process = self.start(make_plan([job("tree", "root-late", output)]))
        tree = self.wait_for_root_exit(output)
        self.assert_not_terminal("tree", process)
        for name in ("stdout", "stderr"):
            self.assertTrue((self.attempt_dir("tree") / (name + ".log")).is_file())
        (output / "release-child").write_bytes(b"release")
        self.finish(process)
        self.assertIs(load_json(output / "child-finished.json"), True)
        self.assertFalse(alive(tree["child_pid"]))
        self.assert_output("tree", b"root stdout\nlate child stdout\x00\xff\n",
                           b"root stderr\nlate child stderr\x00\xfe\n")

    def test_pipe_eof_alone_cannot_complete_live_owned_descendant(self):
        output = self.base / "tree"
        process = self.start(make_plan([job("tree", "root-closed", output)]))
        tree = self.wait_for_root_exit(output)
        self.assert_not_terminal("tree", process)
        (output / "release-child").write_bytes(b"release")
        self.finish(process)
        self.assertFalse(alive(tree["child_pid"]))
        self.assert_output("tree", b"root stdout\n", b"root stderr\n")

    def test_stdin_is_immediate_eof(self):
        output = self.base / "stdin"
        self.finish(self.start(make_plan([job("stdin", "stdin", output)])))
        self.assertEqual(load_json(output / "stdin.json"), {"bytes": []})
        self.assert_output("stdin", b"stdin EOF\n", b"")

    def test_nonzero_root_exit_is_preserved_with_separate_peer_logs(self):
        for code in (17, 259):
            with self.subTest(exit_code=code):
                base = self.base / str(code)
                events, _, _ = self.finish(self.start(make_plan([
                    job("failed", "nonzero", base / "failed", "--exit-code", code),
                    job("peer", "empty", base / "peer")]), base=base), expected=1)
                self.assertEqual(select(events, "completed", "failed")[0]["code"], code)
                self.assertEqual(select(events, "completed", "peer")[0]["code"], 0)
                self.assertFalse(select(events, "resume_queued"))
                self.assert_output("failed", b"stdout before nonzero\x00\xff\n",
                                   b"stderr before nonzero\x00\xfe\n")
                self.assert_output("peer", b"", b"")

    def test_checkpoint_and_resume_keep_output_in_separate_attempts(self):
        plan = pressure_plan(self.base)
        low = next(item for item in plan["jobs"] if item["id"] == "low")
        output = self.base / "outputs" / "low"
        low["argv"] = command("checkpoint", output, "--barrier",
                              self.base / "outputs" / "protected" / "ready.json")
        low["resume_argv"] = command("resume", output)
        events, _, _ = self.finish(self.start(plan))
        starts = select(events, "started", "low")
        self.assertEqual([event["attempt"] for event in starts], [1, 2])
        self.assertEqual(len(select(events, "checkpoint_acknowledged", "low")), 1)
        self.assertEqual(len(select(events, "stopped", "low")), 1)
        self.assertEqual(len(select(events, "resume_queued", "low")), 1)
        self.assertEqual(select(events, "completed", "low")[0]["attempt"], 2)
        first = self.attempt_dir("low", 1)
        self.assertEqual((first / "stop.request").read_bytes(), (first / "stop.ack").read_bytes())
        self.assert_output("low", b"checkpoint stdout begin\ncheckpoint stdout end\n",
                           b"checkpoint stderr begin\ncheckpoint stderr end\n", attempt=1)
        self.assert_output("low", b"resume stdout begin\nresume stdout end\n",
                           b"resume stderr begin\nresume stderr end\n", attempt=2)
        self.assert_output("protected", b"", b"")
        self.assert_output("queued", b"", b"")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--artifacts", type=Path)
    options, remaining = parser.parse_known_args()
    if options.artifacts:
        options.artifacts.mkdir(parents=True, exist_ok=True)
        ARTIFACT_ROOT = Path(tempfile.mkdtemp(prefix="output-tests-", dir=options.artifacts)).resolve()
        print("Retained output test evidence: " + str(ARTIFACT_ROOT), file=sys.stderr)
    program = unittest.main(argv=[sys.argv[0], *remaining], exit=False)
    result = program.result
    if ARTIFACT_ROOT:
        (ARTIFACT_ROOT / "test-result.json").write_text(json.dumps({
            "tests_run": result.testsRun, "successful": result.wasSuccessful(),
            "failures": [test.id() for test, _ in result.failures],
            "errors": [test.id() for test, _ in result.errors],
            "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
        }, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
