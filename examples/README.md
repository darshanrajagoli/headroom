# Checkpointed SHA256 manifests

`checkpointed_hash.py` hashes only the regular files you explicitly name. It
uses the Python standard library, reads at most 1 MiB per chunk, and writes a
deterministic `manifest.json` containing absolute input paths, sizes and SHA256
checksums. Input order does not affect the manifest. No directory traversal,
shell commands, network access or credentials are involved.

From the project directory (replace the sample input paths):

```powershell
python examples/checkpointed_hash.py --state C:\work\hash-first -- C:\data\one.bin C:\data\two.bin
python examples/checkpointed_hash.py --state C:\work\hash-next --resume C:\work\hash-first -- C:\data\one.bin C:\data\two.bin
```

Every invocation requires a **new** output directory with an existing parent.
`--resume OLD_STATE` reads its checkpoint without modifying that directory.
Use the identical input set; arguments may be reordered. Existing output
directories, duplicate inputs (including hard-link aliases), nonregular files,
symlink/reparse paths, and ambiguous Windows device/alternate-stream paths are
refused. There are at most 1,024 input files and 2 MiB of checkpoint metadata.
`--chunk-bytes` accepts 1,024 through 1,048,576 bytes.

Each completed file's digest is durably checkpointed. Resume checks the exact
input paths, file identities, sizes and timestamps, validates checkpoint
structure and its integrity checksum, and reuses the completed prefix without
opening completed files for content reads. An interrupted file restarts from
byte zero: Python's SHA256 internal state is deliberately not serialized.
Metadata validation assumes trusted local files/storage; it does not detect
deliberately restored metadata or maliciously rewritten, re-signed checkpoints.
This is an application-specific recipe, not arbitrary-program recovery.

When Headroom supplies `HEADROOM_CONTROL_DIR`, the program polls its
`stop.request` between chunks. Only exact `headroom-stop-v1\nUUID\n` bytes with a
lowercase UUID are accepted. It flushes and atomically publishes a checkpoint
bound to that nonce **before** flushing and atomically publishing the exact
request bytes as `stop.ack`. Malformed requests or existing acknowledgements
cause refusal. The acknowledgement is the sole protocol write outside the
explicit `--state` directory. No input is overwritten. A completed manifest is
published only after every file finishes and metadata is rechecked.

Recognized Windows request-file access/sharing errors get at most200ms of
application-side retry. Permanent/unclassified errors still fail and never
produce an ack. This does not extend the native guardian's grace deadline.

Exit codes: `0` completed, `75` checkpointed/stopped (no manifest), `73` refusal
or I/O failure, `2` invalid command-line syntax. Standard output is a small JSON
verdict. A crash can leave a temporary file or the most recent completed-file
checkpoint; it cannot promise a checkpoint for a chunk interrupted by a crash.
Durability depends on the local filesystem honoring flush/atomic rename.

For a fleet plan, explicitly provide an original argv with `--state FIRST`,
and a resume argv with `--state NEXT --resume FIRST`, plus the same input files.
Use `allow_stop: true` and `max_resumes: 1` for this fixed two-directory recipe.
The application does not launch recovery, discover inputs, or choose another
directory automatically. No native workload is needed to run its unit tests:

```powershell
python -m unittest discover -s tests -p test_checkpointed_hash.py
```
