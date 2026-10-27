"""Bounded, local-only reporting and selective event metadata collection (stdlib)."""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import html
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field

MIB = 1024 * 1024
MAX_DATA = 64 * MIB
MAX_NATIVE_LINE = 128 * 1024
MAX_EVENT_LINE = 512 * 1024
MAX_HISTORY = 16 * MIB
MAX_EVENTS = 100_000
MAX_SAMPLES = 100_000
MAX_GROUPS = 16
MAX_SCAN = 1000
MAX_FILES = 64
PER_FILE = 2 * MIB
INITIAL_TAIL = MIB
COLLECT_BUDGET = 16 * MIB
MAX_STATE = MIB
MAX_TS = 253402300799999
NUMBER = re.compile(rb'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')
ISO = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z')
EVENT_KINDS = {"human_prompt_candidate": "project_session",
               "history_prompt_timestamp": "claude_code_history"}
GAP_KEYS = ("oversized_lines", "invalid_records", "rotations", "initial_tail",
            "lagged_files", "scan_limit", "selection_limit", "feed_limit", "time_limit")
BLOCKED_STEMS = {"auth", "config", "configuration", "credential", "credentials", "settings",
                 "secrets", "tokens", "token", "account", "accounts", ".credentials"}


def safe_info(path, directory=False):
    """Reject links/reparse points in every path component, before resolving."""
    absolute = Path(os.path.abspath(path))
    for part in reversed((absolute, *absolute.parents)):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked or reparse input is unsupported")
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("expected directory")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("expected single-link regular file")
    return absolute, info


def safe_open(path, append=False):
    """Validate before and after open; never follow a final symlink on POSIX."""
    absolute = Path(os.path.abspath(path))
    try:
        _, previous = safe_info(absolute)
    except FileNotFoundError:
        if not append:
            raise
        safe_info(absolute.parent, directory=True)
        previous = None
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT if append else os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(absolute, flags, 0o600)
    try:
        actual = os.fstat(fd)
        _, current = safe_info(absolute)
        if (not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1
                or getattr(actual, "st_file_attributes", 0) & 0x400
                or (actual.st_dev, actual.st_ino) != (current.st_dev, current.st_ino)
                or (previous is not None and (actual.st_dev, actual.st_ino) != (previous.st_dev, previous.st_ino))):
            raise ValueError("input changed while opening")
        handle = os.fdopen(fd, "ab" if append else "rb")
        fd = None
        return handle
    finally:
        if fd is not None:
            os.close(fd)


def source_name(path):
    path = Path(path)
    if path.suffix.lower() != ".jsonl" or path.stem.lower() in BLOCKED_STEMS:
        raise ValueError("source must be an explicit event log")
    return path


def integer(value, maximum=2**64 - 1):
    return type(value) is int and 0 <= value <= maximum


def timestamp(value):
    """Numeric values are milliseconds, never guessed to be seconds."""
    if type(value) in (int, float):
        if math.isfinite(value) and 0 <= value <= MAX_TS:
            return int(value)
    elif isinstance(value, str) and ISO.fullmatch(value):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
            delta = parsed.astimezone(dt.timezone.utc) - epoch
            result = delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
            return result if integer(result, MAX_TS) else None
        except (ValueError, OverflowError):
            pass
    return None


class SelectiveJSON:
    """Walk bytes without decoding/copying unknown string values.

    Only bounded metadata keys/values are decoded. In particular, display,
    pastedContents, text, sessionId, project and unknown nested values are skipped.
    The input record itself is a bounded byte buffer; no prompt string is built.
    """
    TOP = {"timestamp", "type", "isMeta", "isSidechain", "message"}

    def __init__(self, raw):
        self.raw, self.i, self.meta = raw, 0, {}

    def ws(self):
        while self.i < len(self.raw) and self.raw[self.i] in b' \t\r\n':
            self.i += 1

    def take(self, char):
        self.ws()
        if self.i >= len(self.raw) or self.raw[self.i] != char:
            raise ValueError("invalid JSON")
        self.i += 1

    def string(self, capture=False):
        self.take(34)
        saved = bytearray(b'"') if capture else None
        while self.i < len(self.raw):
            begin = self.i
            c = self.raw[self.i]
            self.i += 1
            if c == 34:
                if saved is not None:
                    saved.append(34)
                    return json.loads(saved.decode("utf-8"))
                return None
            if c < 32:
                raise ValueError("invalid JSON string")
            if c == 92:
                if self.i >= len(self.raw):
                    raise ValueError("invalid escape")
                esc = self.raw[self.i]
                self.i += 1
                if esc == 117:
                    for _ in range(4):
                        if self.i >= len(self.raw) or self.raw[self.i] not in b'0123456789abcdefABCDEF':
                            raise ValueError("invalid unicode escape")
                        self.i += 1
                elif esc not in b'"\\/bfnrt':
                    raise ValueError("invalid escape")
            elif c >= 128:
                # Validate UTF-8 in place, including overlong/surrogate exclusions.
                n = 1 if 194 <= c <= 223 else 2 if 224 <= c <= 239 else 3 if 240 <= c <= 244 else -1
                if n < 0 or self.i + n > len(self.raw):
                    raise ValueError("invalid UTF-8")
                first = self.raw[self.i]
                if ((c == 224 and first < 160) or (c == 237 and first > 159)
                        or (c == 240 and first < 144) or (c == 244 and first > 143)):
                    raise ValueError("invalid UTF-8")
                for j in range(n):
                    if not 128 <= self.raw[self.i + j] <= 191:
                        raise ValueError("invalid UTF-8")
                self.i += n
            if saved is not None:
                if len(saved) + self.i - begin > 256:
                    saved = None
                else:
                    for j in range(begin, self.i):
                        saved.append(self.raw[j])
        raise ValueError("unfinished string")

    def value(self, path=None, depth=0):
        if depth > 32:
            raise ValueError("nesting limit")
        self.ws()
        if self.i >= len(self.raw):
            raise ValueError("missing value")
        c = self.raw[self.i]
        if c == 123:
            self.i += 1
            seen = set()
            self.ws()
            if self.i < len(self.raw) and self.raw[self.i] == 125:
                self.i += 1
                return "object"
            while True:
                key = self.string(path in ((), ("message",), ("block",)))
                relevant = ((path == () and key in self.TOP)
                            or (path == ("message",) and key in {"role", "content"})
                            or (path == ("block",) and key == "type"))
                if relevant:
                    if key in seen:
                        raise ValueError("duplicate metadata key")
                    seen.add(key)
                child = path + (key,) if relevant else None
                self.take(58)
                result = self.value(child, depth + 1)
                if relevant:
                    if path == ("block",):
                        self.meta.setdefault("blocks", []).append(result)
                    else:
                        self.meta[child] = result
                self.ws()
                if self.i < len(self.raw) and self.raw[self.i] == 125:
                    self.i += 1
                    return "object"
                self.take(44)
        if c == 91:
            self.i += 1
            self.ws()
            if path == ("message", "content"):
                self.meta["blocks"] = []
                self.meta["all_blocks_objects"] = True
            if self.i < len(self.raw) and self.raw[self.i] == 93:
                self.i += 1
                return "array"
            count = 0
            while True:
                block = path == ("message", "content")
                previous = len(self.meta.get("blocks", []))
                result = self.value(("block",) if block else None, depth + 1)
                if block and (result != "object" or len(self.meta["blocks"]) != previous + 1):
                    self.meta["all_blocks_objects"] = False
                count += 1
                if count > 65536:
                    raise ValueError("array limit")
                self.ws()
                if self.i < len(self.raw) and self.raw[self.i] == 93:
                    self.i += 1
                    return "array"
                self.take(44)
        if c == 34:
            capture = path in {("timestamp",), ("type",), ("message", "role"), ("block", "type")}
            result = self.string(capture)
            return "string" if path == ("message", "content") else result
        for literal, result in ((b'true', True), (b'false', False), (b'null', None)):
            if self.raw.startswith(literal, self.i):
                self.i += len(literal)
                return result
        match = NUMBER.match(self.raw, self.i)
        if not match:
            raise ValueError("invalid value")
        self.i = match.end()
        if path == ("timestamp",) and match.end() - match.start() <= 32:
            return json.loads(match.group())
        return None

    def parse(self):
        if self.value(()) != "object":
            raise ValueError("metadata record must be an object")
        self.ws()
        if self.i != len(self.raw):
            raise ValueError("trailing JSON")
        return self.meta


def event_metadata(raw, provenance):
    meta = SelectiveJSON(raw).parse()
    ts = timestamp(meta.get(("timestamp",)))
    if ts is None:
        return None
    if provenance == "claude_code_history":
        kind = "history_prompt_timestamp"
    else:
        content = meta.get(("message", "content"))
        blocks = meta.get("blocks", [])
        if (meta.get(("type",)) != "user" or meta.get(("isMeta",)) is True
                or meta.get(("isSidechain",)) is True or meta.get(("message", "role")) != "user"
                or not (content == "string" or (content == "array" and blocks
                        and meta.get("all_blocks_objects") and all(b == "text" for b in blocks)))):
            return None
        kind = "human_prompt_candidate"
    return {"ts_ms": ts, "kind": kind, "provenance": provenance}


@dataclass
class Scan:
    offset: int
    discarding: bool = False
    oversized_lines: int = 0
    incomplete: bool = False
    read_bytes: int = 0


def records(handle, scan, end, budget, line_limit):
    """Yield complete bounded lines, retaining a short incomplete line for retry."""
    handle.seek(scan.offset)
    remaining = min(budget, max(0, end - scan.offset))
    while remaining:
        begin = scan.offset
        raw = handle.readline(min(line_limit + 1, remaining))
        if not raw:
            break
        used = len(raw)
        remaining -= used
        scan.read_bytes += used
        scan.offset += used
        complete = raw.endswith(b'\n')
        if scan.discarding:
            scan.discarding = not complete
            continue
        if used > line_limit:
            scan.oversized_lines += 1
            scan.discarding = not complete
            continue
        if not complete:
            scan.offset = begin
            scan.incomplete = True
            break
        yield raw


def atomic_write(path, content):
    """Replace one derived file atomically; never target samples.jsonl."""
    fd, temporary = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def checked_data(path):
    data, _ = safe_info(path, directory=True)
    if not data.is_dir() or any((p / ".git").exists() for p in (data, *data.parents)):
        raise ValueError("data directory must be an existing private directory outside Git")
    return data


def load_start(data):
    with safe_open(data / "samples.jsonl") as handle:
        raw = handle.readline(MAX_NATIVE_LINE + 1)
    if len(raw) > MAX_NATIVE_LINE or not raw.endswith(b'\n'):
        raise ValueError("missing or oversized native start record")
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("type") != "start" or value.get("version") != 1
            or value.get("aggregation") != "root_groups"
            or value.get("cpu_scope") != "observed_member_deltas"
            or not integer(value.get("interval_ms"), 86400000) or value["interval_ms"] == 0
            or not integer(value.get("started_unix_ms"), MAX_TS)
            or not integer(value.get("deadline_unix_ms"), MAX_TS)
            or not 0 <= value["deadline_unix_ms"] - value["started_unix_ms"] <= 86400000):
        raise ValueError("unsupported native start contract")
    return {key: value[key] for key in ("interval_ms", "started_unix_ms", "deadline_unix_ms")}


@dataclass
class Sample:
    ts: int
    private: int
    working: int
    members: int
    groups: dict
    incomplete: bool
    collector_private: int
    collector_working: int
    collector_cpu: int


def sample_record(value):
    fields = ("ts_ms", "host_total_bytes", "host_available_bytes", "collector_private_bytes",
              "collector_working_set_bytes", "collector_cpu_100ns")
    if (not isinstance(value, dict) or any(not integer(value.get(k)) for k in fields)
            or not integer(value["ts_ms"], MAX_TS)
            or value["host_available_bytes"] > value["host_total_bytes"]
            or type(value.get("snapshot_incomplete")) is not bool
            or type(value.get("claude_foreground")) is not bool
            or not isinstance(value.get("processes"), list) or len(value["processes"]) > MAX_GROUPS):
        raise ValueError("invalid sample")
    groups = {}
    private = working = members = 0
    for group in value["processes"]:
        keys = ("pid", "ppid", "created_100ns", "root_pid", "root_created_100ns",
                "private_bytes", "working_set_bytes", "cpu_100ns", "member_count")
        if (not isinstance(group, dict) or any(not integer(group.get(k)) for k in keys)
                or not 1 <= group["member_count"] <= 4096 or group["pid"] == 0
                or group["pid"] != group["root_pid"] or group["created_100ns"] != group["root_created_100ns"]
                or group.get("name", "").lower() != "claude.exe"):
            raise ValueError("invalid root group")
        identity = group["pid"], group["created_100ns"]
        if identity in groups:
            raise ValueError("duplicate root group")
        groups[identity] = group["cpu_100ns"], group["member_count"]
        private += group["private_bytes"]
        working += group["working_set_bytes"]
        members += group["member_count"]
    if members > 4096:
        raise ValueError("member cap exceeded")
    return Sample(value["ts_ms"], private, working, members, groups, value["snapshot_incomplete"],
                  value["collector_private_bytes"], value["collector_working_set_bytes"],
                  value["collector_cpu_100ns"])


@dataclass
class Run:
    start: dict
    samples: list = field(default_factory=list)
    end: dict | None = None
    invalid: int = 0
    partial_tail: bool = False
    helper_failures: int = 0


def load_run(data):
    run = Run(load_start(data))
    with safe_open(data / "samples.jsonl") as handle:
        size = os.fstat(handle.fileno()).st_size
        if size > MAX_DATA:
            raise ValueError("native data exceeds 64 MiB")
        scan = Scan(0)
        first = True
        ended = False
        for raw in records(handle, scan, size, MAX_DATA, MAX_NATIVE_LINE):
            if first:
                first = False
                continue
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("invalid record")
                # Native helper diagnostics may follow end. They never establish
                # completion and contain no Claude activity evidence.
                if value.get("type") == "helper":
                    if not integer(value.get("exit_code"), 2**32 - 1) or type(value.get("final")) is not bool:
                        raise ValueError("invalid helper diagnostic")
                    run.helper_failures += int(value["exit_code"] != 0 or value.get("start_failed") is True)
                    continue
                if ended:
                    run.end = None
                    raise ValueError("records after end")
                lower = run.samples[-1].ts if run.samples else run.start["started_unix_ms"]
                if value.get("type") == "sample":
                    sample = sample_record(value)
                    if sample.ts < run.start["started_unix_ms"] or (run.samples and sample.ts <= lower):
                        raise ValueError("out of order sample")
                    if len(run.samples) >= MAX_SAMPLES:
                        raise ValueError("sample count limit")
                    run.samples.append(sample)
                elif (value.get("type") == "end" and integer(value.get("ts_ms"), MAX_TS)
                      and value["ts_ms"] >= lower and value.get("reason") in {"deadline", "requested", "limit", "error"}):
                    run.end = {"ts_ms": value["ts_ms"], "reason": value["reason"]}
                    ended = True
                else:
                    raise ValueError("unsupported record")
            except (ValueError, TypeError, AttributeError, RecursionError):
                run.invalid += 1
        run.invalid += scan.oversized_lines
        run.partial_tail = scan.incomplete or scan.discarding
    return run


def discover_projects(root, start, counts, expires=None):
    """At most 1000 entries, exactly project/*.jsonl; no subagents/symlinks."""
    candidates = []
    scanned = 0
    root, _ = safe_info(root, directory=True)
    with os.scandir(root) as projects:
        for project in projects:
            if expires is not None and time.monotonic() >= expires:
                counts["time_limit"] += 1
                break
            scanned += 1
            if scanned >= MAX_SCAN:
                counts["scan_limit"] += 1
                break
            if not project.is_dir(follow_symlinks=False) or getattr(project.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
                continue
            with os.scandir(project.path) as entries:
                for entry in entries:
                    if expires is not None and time.monotonic() >= expires:
                        counts["time_limit"] += 1
                        break
                    scanned += 1
                    if scanned >= MAX_SCAN:
                        counts["scan_limit"] += 1
                        break
                    if not entry.name.endswith(".jsonl") or not entry.is_file(follow_symlinks=False):
                        continue
                    try:
                        # Windows DirEntry.stat can return zero for st_nlink/
                        # st_ino; use a fresh path stat for link/identity checks.
                        _, info = safe_info(entry.path)
                    except (OSError, ValueError):
                        counts["invalid_records"] += 1
                        continue
                    if Path(entry.name).stem.lower() in BLOCKED_STEMS:
                        counts["invalid_records"] += 1
                        continue
                    if info.st_mtime_ns // 1000000 >= start and len(entry.path) <= 4096:
                        candidates.append((info.st_mtime_ns, entry.path))
            if scanned >= MAX_SCAN:
                break
    candidates.sort(reverse=True)
    if len(candidates) > MAX_FILES:
        counts["selection_limit"] += len(candidates) - MAX_FILES
    return [path for _, path in candidates[:MAX_FILES]]


def load_state(path, started):
    if not path.exists():
        return {"version": 2, "salt": os.urandom(32).hex(), "started_unix_ms": started, "cursors": {}}
    if path.stat().st_size > MAX_STATE:
        raise ValueError("private cursor state exceeds bound")
    with safe_open(path) as handle:
        raw = handle.read(MAX_STATE + 1)
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("version") != 2
            or not isinstance(value.get("salt"), str) or re.fullmatch(r"[0-9a-f]{64}", value["salt"]) is None
            or value.get("started_unix_ms") != started or not isinstance(value.get("cursors"), dict)
            or len(value["cursors"]) > MAX_FILES + 1):
        raise ValueError("invalid private cursor state")
    for path_key, cursor in value["cursors"].items():
        if (not isinstance(path_key, str) or re.fullmatch(r"[0-9a-f]{64}", path_key) is None or not isinstance(cursor, dict)
                or any(not integer(cursor.get(k)) for k in ("offset", "dev", "ino", "mtime_ns"))
                or not integer(cursor.get("prefix_len"), 256)
                or any(not isinstance(cursor.get(k), str) or re.fullmatch(r"[0-9a-f]{64}", cursor[k]) is None
                       for k in ("prefix_mac", "boundary_mac"))
                or type(cursor.get("discarding")) is not bool):
            raise ValueError("invalid private cursor")
    return value


def cursor_id(state, path):
    return hmac.new(bytes.fromhex(state["salt"]), os.fsencode(os.path.normcase(os.path.abspath(path))), hashlib.sha256).hexdigest()


def continuity(handle, state, offset, prefix_len):
    """Bounded keyed fingerprints; never persist source bytes or unkeyed hashes."""
    key = bytes.fromhex(state["salt"])
    handle.seek(0)
    prefix = handle.read(prefix_len)
    handle.seek(max(0, offset - 256))
    boundary = handle.read(min(256, offset))
    return {"prefix_len": prefix_len,
            "prefix_mac": hmac.new(key, b"prefix:" + prefix, hashlib.sha256).hexdigest(),
            "boundary_mac": hmac.new(key, b"boundary:" + boundary, hashlib.sha256).hexdigest()}


def collect_events(data, projects=None, history=None, now_ms=None):
    start = load_start(data)
    since = start["started_unix_ms"]
    now = int(time.time() * 1000) if now_ms is None else now_ms
    counts = dict.fromkeys(GAP_KEYS, 0)
    if now >= start["deadline_unix_ms"]:
        return {"written": 0, "coverage": counts, "deadline_reached": True}
    expires = time.monotonic() + min(22, (start["deadline_unix_ms"] - now) / 1000)
    until = min(now, start["deadline_unix_ms"])
    state_path = data / ".event-cursors.json"
    state = load_state(state_path, since)
    files = []
    if history is not None:
        files.append((os.path.abspath(source_name(history)), "claude_code_history"))
    if projects is not None:
        try:
            files.extend((path, "project_session") for path in discover_projects(projects, since, counts, expires))
        except (OSError, ValueError):
            counts["invalid_records"] += 1
    budget = COLLECT_BUDGET
    feed_path = data / "events.jsonl"
    written = 0
    next_cursors = dict(state["cursors"])
    with safe_open(feed_path, append=True) as feed:
        feed_size = os.fstat(feed.fileno()).st_size
        if feed_size > MAX_HISTORY:
            raise ValueError("event feed exceeds 16 MiB")
        for path, provenance in files:
            if time.monotonic() >= expires:
                counts["time_limit"] += 1
                break
            if budget <= 0:
                counts["lagged_files"] += 1
                continue
            if len(path) > 4096:
                counts["invalid_records"] += 1
                continue
            try:
                with safe_open(path) as handle:
                    info = os.fstat(handle.fileno())
                    key = cursor_id(state, path)
                    old = state["cursors"].get(key)
                    rotated = old is not None and (old["dev"] != info.st_dev or old["ino"] != info.st_ino
                                or info.st_size < old["offset"] or info.st_mtime_ns < old["mtime_ns"])
                    if old is not None and not rotated:
                        check = continuity(handle, state, old["offset"], old["prefix_len"])
                        rotated = any(check[k] != old[k] for k in ("prefix_mac", "boundary_mac"))
                    if old is None or rotated:
                        offset = max(0, info.st_size - INITIAL_TAIL)
                        scan = Scan(offset, discarding=offset > 0)
                        counts["rotations"] += int(rotated)
                        counts["initial_tail"] += int(offset > 0)
                    else:
                        scan = Scan(old["offset"], old["discarding"])
                    for raw in records(handle, scan, info.st_size, min(PER_FILE, budget), MAX_EVENT_LINE):
                        if time.monotonic() >= expires:
                            scan.offset -= len(raw)
                            counts["time_limit"] += 1
                            break
                        try:
                            event = event_metadata(raw, provenance)
                        except (ValueError, TypeError, RecursionError):
                            counts["invalid_records"] += 1
                            continue
                        if event is None or not since <= event["ts_ms"] <= until:
                            continue
                        encoded = (json.dumps(event, separators=(",", ":")) + "\n").encode("ascii")
                        if feed_size + len(encoded) > MAX_HISTORY - 2048:
                            counts["feed_limit"] += 1
                            # Do not advance past an event that could not be retained.
                            scan.offset -= len(raw)
                            break
                        feed.write(encoded)
                        feed_size += len(encoded)
                        written += 1
                    budget -= scan.read_bytes
                    counts["oversized_lines"] += scan.oversized_lines
                    if scan.offset < info.st_size:
                        counts["lagged_files"] += 1
                    next_cursors[key] = {"offset": scan.offset, "discarding": scan.discarding,
                                         "dev": info.st_dev, "ino": info.st_ino, "mtime_ns": info.st_mtime_ns,
                                         **continuity(handle, state, scan.offset, min(256, scan.offset))}
            except (OSError, ValueError):
                counts["invalid_records"] += 1
        # Keep cursor count bounded; evicted files may be tailed again and duplicate events.
        selected = {cursor_id(state, path) for path, _ in files}
        ordered = sorted(next_cursors, key=lambda p: (p not in selected, -next_cursors[p]["mtime_ns"]))
        state["cursors"] = {p: next_cursors[p] for p in ordered[:MAX_FILES + 1]}
        while len(json.dumps(state, separators=(",", ":")).encode("utf-8")) > MAX_STATE:
            state["cursors"].pop(next(reversed(state["cursors"])))
            counts["selection_limit"] += 1
        if any(counts.values()):
            gap = {"kind": "coverage_gap", "ts_ms": now, "counts": counts}
            encoded = (json.dumps(gap, separators=(",", ":")) + "\n").encode("ascii")
            if feed_size + len(encoded) <= MAX_HISTORY:
                feed.write(encoded)
        feed.flush()
        os.fsync(feed.fileno())
    atomic_write(state_path, json.dumps(state, separators=(",", ":")) + "\n")
    return {"written": written, "coverage": counts}


def load_events(data, history, lower, upper):
    events, info = [], {"source": "none", "invalid": 0, "oversized": 0,
                       "partial_tail": False, "tail_limited": False, "gaps": 0, "limited": False}
    if lower is None:
        return events, info
    feed = data / "events.jsonl"
    path = feed if feed.exists() else Path(history) if history is not None else None
    if path is None:
        return events, info
    info["source"] = "collected metadata" if feed.exists() else "Claude Code history timestamps only"
    try:
        if not feed.exists():
            source_name(path)
        with safe_open(path) as handle:
            size = os.fstat(handle.fileno()).st_size
            if feed.exists() and size > MAX_HISTORY:
                raise ValueError("event feed exceeds 16 MiB")
            offset = max(0, size - MAX_HISTORY)
            info["tail_limited"] = offset > 0
            scan = Scan(offset, discarding=offset > 0)
            for raw in records(handle, scan, size, MAX_HISTORY, MAX_EVENT_LINE):
                try:
                    if feed.exists():
                        value = json.loads(raw)
                        if not isinstance(value, dict):
                            raise ValueError("invalid event")
                        if value.get("kind") == "coverage_gap":
                            info["gaps"] += 1
                            continue
                        kind = value.get("kind")
                        if (kind not in EVENT_KINDS or value.get("provenance") != EVENT_KINDS[kind]
                                or not integer(value.get("ts_ms"), MAX_TS)):
                            raise ValueError("invalid event")
                        event = {"ts_ms": value["ts_ms"], "kind": kind, "provenance": EVENT_KINDS[kind]}
                    else:
                        event = event_metadata(raw, "claude_code_history")
                    if event and lower <= event["ts_ms"] <= upper:
                        events.append(event)
                        if len(events) >= MAX_EVENTS:
                            info["limited"] = True
                            break
                except (ValueError, TypeError, RecursionError):
                    info["invalid"] += 1
            info["oversized"] = scan.oversized_lines
            info["partial_tail"] = scan.incomplete or scan.discarding
    except (OSError, ValueError):
        info["source"] = "unavailable"
    events.sort(key=lambda e: e["ts_ms"])
    return events, info


def metrics(run):
    result = {"observed_ms": 0, "covered_ms": 0, "gap_ms": 0, "gap_count": 0,
              "cpu_100ns": 0, "collector_cpu_100ns": 0, "cpu_resets": 0, "group_changes": 0}
    for before, after in zip(run.samples, run.samples[1:]):
        elapsed = after.ts - before.ts
        result["group_changes"] += int(before.groups.keys() != after.groups.keys())
        if elapsed > 2 * run.start["interval_ms"]:
            result["gap_ms"] += elapsed
            result["gap_count"] += 1
            continue
        result["covered_ms"] += elapsed
        if before.groups.keys() & after.groups.keys() and not (before.incomplete or after.incomplete):
            result["observed_ms"] += elapsed
        for identity in before.groups.keys() & after.groups.keys():
            delta = after.groups[identity][0] - before.groups[identity][0]
            if delta >= 0:
                result["cpu_100ns"] += delta
            else:
                result["cpu_resets"] += 1
        delta = after.collector_cpu - before.collector_cpu
        if delta >= 0:
            result["collector_cpu_100ns"] += delta
    for name, attr in (("peak_private_bytes", "private"), ("peak_working_set_bytes", "working"),
                       ("collector_peak_private_bytes", "collector_private"),
                       ("collector_peak_working_set_bytes", "collector_working")):
        result[name] = max((getattr(s, attr) for s in run.samples), default=None)
    return result


def correlate(samples, events):
    times = [s.ts for s in samples]
    rows = []
    for event in events:
        t = event["ts_ms"]
        left = bisect.bisect_right(times, t) - 1
        right = bisect.bisect_left(times, t)
        before = samples[left] if left >= 0 and t - times[left] <= 120000 else None
        after = samples[right] if right < len(times) and times[right] - t <= 120000 else None
        rows.append((event, before, after))
    return rows


def utc(ts):
    return (dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(milliseconds=ts)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def svg_plot(run):
    samples = run.samples
    if not samples:
        return '<p>No samples available for a memory plot.</p>'
    lo, hi = samples[0].ts, samples[-1].ts
    maximum = max(1, max(max(s.private, s.working) for s in samples))
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 300" role="img" aria-label="Reported Claude group memory over time">',
             '<rect width="900" height="300" fill="white"/>',
             '<path d="M70 25V250H870" stroke="#555" fill="none"/>']
    def point(s, attr):
        return 70 + 800 * (s.ts - lo) / max(1, hi - lo), 250 - 210 * getattr(s, attr) / maximum
    for attr, color in (("private", "#145ea8"), ("working", "#ae4a00")):
        segment = []
        previous = None
        def flush():
            if segment:
                parts.append(f'<polyline fill="none" stroke="{color}" points="' + ' '.join(f'{x:.2f},{y:.2f}' for x, y in segment) + '"/>')
                if len(segment) == 1:
                    x, y = segment[0]
                    parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2" fill="{color}"/>')
                segment.clear()
        for s in samples:
            if previous and (s.ts - previous.ts > 2 * run.start["interval_ms"] or previous.incomplete or s.incomplete):
                flush()
            if not (s.incomplete and not s.groups):
                segment.append(point(s, attr))
            previous = s
        flush()
    parts.extend([f'<text x="70" y="18">{maximum / MIB:.2f} MiB (scale maximum)</text>',
                  '<text x="16" y="254">0</text>',
                  f'<text x="70" y="278">{utc(lo)}</text>',
                  f'<text x="870" y="278" text-anchor="end">{utc(hi)}</text>', '</svg>'])
    return ''.join(parts)


LIMITATIONS = (
    "Private bytes measure private commit, not physical RAM. Working set is a separate counter; summing members can double-count shared pages.",
    "Groups are best-effort reported Claude roots and descendants, not kernel ownership. Missing members and partial snapshots give lower bounds; membership changes do not reconstruct old memory.",
    "Gaps (including sleep) are unknown, never zero. Plot lines break across intervals over twice the configured cadence and across partial snapshots. An empty partial snapshot is not plotted as zero.",
    "Observed wall-clock hours sum adjacent complete snapshots sharing a root identity, at intervals no longer than twice the cadence. Partial snapshots and gaps are excluded; zero hours do not establish absence. This assumes presence between those snapshots and is not proof of uninterrupted use, model activity, or productivity.",
    "CPU is a lower bound from native accrued observed-member deltas, differenced only for the same root PID and creation time with nonnegative counters across covered intervals. Root changes and PID reuse are never joined.",
    "Correlation is observational, not causal. Before/after snapshots must be within 120 seconds on each side; an exact-time snapshot may serve both sides. No old memory reconstruction or claims of reduction, recovery, or ownership are supported.",
    "History timestamps cover Claude Code only and may omit desktop prompts. Zero events do not prove idle time. Project-session human prompt candidates may be injected or duplicated; they are not proven human actions or counts of model calls.",
    "Foreground/window focus is not proof of model activity and is not used to calculate observed hours. No desktop chat database is read.",
    "Collector overhead is shown separately. Short-lived Python metadata/report helper overhead is not measured by the native collector's own counters.",
    "Reports contain only aggregate counters, times and fixed labels. Private raw samples and opaque cursor state remain local. No uploads, external network resources, scripts, paid calls, or prompts are issued by this reporter.",
)


def generate_report(data, history=None, since_start=False, refresh=False):
    run = load_run(data)
    if int(time.time() * 1000) >= run.start["deadline_unix_ms"]:
        history = None  # Final reporting never reopens original logs after cutoff.
    lower = run.samples[0].ts if run.samples else None
    upper = run.samples[-1].ts if run.samples else None
    if since_start and lower is not None:
        lower = max(lower, run.start["started_unix_ms"])
    events, event_info = load_events(data, history, lower, upper)
    measured = metrics(run)
    status = ({"deadline": "Completed: deadline", "requested": "Stopped: requested",
               "limit": "Stopped: limit", "error": "Stopped: error"}[run.end["reason"]]
              if run.end else "Live / incomplete: no valid end record" if refresh else "Incomplete: no valid end record")
    partial = sum(s.incomplete for s in run.samples)
    hours = measured["observed_ms"] / 3600000
    bullet = (f"- Built a local read-only Claude observation sidecar; recorded {hours:.4f} sample-supported observed Claude wall-clock hours "
              f"across {len(run.samples)} snapshots. Observational evidence only; not proof of model activity or productivity.\n")
    lines = ["# Local Claude observation report", "", f"Status: {status}.", "",
             f"Samples: {len(run.samples)}; partial snapshots: {partial}; rejected/oversized records: {run.invalid}; incomplete tail: {run.partial_tail}.",
             f"Recorded metadata/report helper failures: {run.helper_failures}. Final report completion is recorded separately in completion.json after the native helper exits.",
             f"Sampling cadence: {run.start['interval_ms']} ms. Planned deadline: {utc(run.start['deadline_unix_ms'])}.",
             f"Sample-supported observed Claude wall-clock hours: {hours:.4f} ({measured['observed_ms']} ms).",
             f"Coverage gaps: {measured['gap_count']} ({measured['gap_ms']} ms unknown); group-membership transitions: {measured['group_changes']}.", ""]
    if run.samples:
        lines.append(f"Sample window: {utc(lower)} to {utc(upper)}. No extrapolation before/after this window.")
    def byte_metric(key):
        value = measured[key]
        return "unavailable" if value is None else f"{value} bytes ({value / MIB:.3f} MiB)"
    lines.extend(["", f"Reported group peak private commit: {byte_metric('peak_private_bytes')}.",
                  f"Reported group peak working set: {byte_metric('peak_working_set_bytes')}.",
                  f"Observed group CPU delta lower bound: {measured['cpu_100ns'] / 10000000:.7f} seconds; negative counter transitions skipped: {measured['cpu_resets']}.",
                  f"Collector measured peak private commit: {byte_metric('collector_peak_private_bytes')}.",
                  f"Collector measured peak working set: {byte_metric('collector_peak_working_set_bytes')}.",
                  f"Collector measured CPU deltas across covered intervals: {measured['collector_cpu_100ns'] / 10000000:.7f} seconds.", "",
                  f"Event source: {event_info['source']}; in-window timestamp/candidate records: {len(events)}.",
                  f"Event coverage: {json.dumps(event_info, sort_keys=True)}.", "",
                  "History is Claude Code only; desktop activity may be absent. Zero events do not prove idle time.", "",
                  "Memory plot: open report.html (private commit and working set drawn separately; gaps left open).", "",
                  "Timestamp correlation (observational; first 100 records, exact reported bytes):", "",
                  "| UTC event | Kind | Before UTC | Before private / working bytes | After UTC | After private / working bytes |",
                  "|---|---|---|---:|---|---:|"])
    for event, before, after in correlate(run.samples, events[:100]):
        def cells(sample):
            if sample is None:
                return "unavailable | unavailable"
            qualifier = " (partial lower bound)" if sample.incomplete else ""
            return f"{utc(sample.ts)} | {sample.private} / {sample.working}{qualifier}"
        lines.append(f"| {utc(event['ts_ms'])} | {event['kind']} | {cells(before)} | {cells(after)} |")
    lines.extend(["", "Interpretation and limitations:", "", *(f"- {s}" for s in LIMITATIONS), "", "Conservative resume bullet:", "", bullet.strip(), ""])
    markdown = '\n'.join(lines)
    document = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src \'none\'; base-uri \'none\'; form-action \'none\'">'
                '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Local Claude observation report</title>'
                '<style>body{font:16px system-ui;margin:2rem auto;max-width:1100px;padding:0 1rem;color:#17212b}svg{width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 system-ui}.private{color:#145ea8}.working{color:#ae4a00}</style></head><body>'
                '<h1>Local Claude observation report</h1><p><span class="private">Blue: private commit</span>; '
                '<span class="working">orange: working set</span>. Independent aggregate counters.</p>'
                + svg_plot(run) + '<pre>' + html.escape(markdown) + '</pre></body></html>\n')
    manifest = {"report_version": 1, "status": status, "sample_count": len(run.samples),
                "partial_samples": partial, "invalid_records": run.invalid, "partial_tail": run.partial_tail,
                "helper_failures": run.helper_failures,
                "sample_start_ms": lower, "sample_end_ms": upper, "metrics": measured,
                "event_records": len(events), "event_coverage": event_info,
                "files": ["report.md", "report.html", "resume.txt"]}
    for name, content in (("report.md", markdown), ("report.html", document), ("resume.txt", bullet),
                          ("manifest.json", json.dumps(manifest, indent=2) + "\n")):
        atomic_write(data / name, content)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, help="private native output directory outside Git")
    parser.add_argument("--history", help="explicit Claude Code history.jsonl; never auto-discovered")
    parser.add_argument("--projects", help="explicit Claude projects root; metadata helper only")
    parser.add_argument("--collect-events", action="store_true", help="one bounded metadata pass; no report")
    parser.add_argument("--since-start", action="store_true", help="explicitly clamp event range to native start")
    parser.add_argument("--refresh", action="store_true", help="allow a live, incomplete report without claiming completion")
    args = parser.parse_args(argv)
    try:
        data = checked_data(args.data)
        if args.collect_events:
            if not args.projects and not args.history:
                raise ValueError("metadata collection requires an explicit projects or history source")
            result = collect_events(data, args.projects, args.history)
            preview = data / "report.html"
            if not preview.exists() or time.time() - preview.stat().st_mtime >= 300:
                generate_report(data, args.history, args.since_start, refresh=True)
            print(json.dumps(result, sort_keys=True))
        else:
            # Final reporting consumes collected metadata only. Source access is
            # restricted to the explicitly requested bounded collection mode.
            result = generate_report(data, None, args.since_start, args.refresh)
            print(f"Local report written; samples={result['sample_count']}; {result['status']}.")
        return 0
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        # Never echo exception text, paths, input records, argparse values or identities.
        print("Local reporting failed: unavailable input, invalid contract, or configured bound exceeded.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
