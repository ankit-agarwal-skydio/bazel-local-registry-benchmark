# Local registry invalidation benchmark (bazelbuild/bazel#31424)

[bazelbuild/bazel#31424](https://github.com/bazelbuild/bazel/pull/31424) makes `RegistryFunction`
depend on the `DirectoryTreeDigestValue` of a `file://` registry root so that edits to a local
registry are picked up without `bazel shutdown`. This puts every file in the registry into
Skyframe: a fresh server digests them all, and each later command dirty-checks them
(`--experimental_check_external_other_files` is on by default).

This repo measures that overhead with a local registry that is a full clone of the
[Bazel Central Registry](https://github.com/bazelbuild/bazel-central-registry).

## Results

| | |
|---|---|
| Bazel base (merge base with `master`) | `e86822b8cfe8dacc721b79509efb40b8ea486981` |
| Bazel PR head | `0e53cbe89a4d358086acfa236a5c5386597bab21` |
| Registry | BCR `9a46e25ff8551c5dd764a02d65cdde852feba569`: 49,018 files, 132 MB, `.git` kept |
| Workspace | Bazel's own source at the base commit (416-line `bazel mod graph`) |
| Command | `bazel mod graph --registry=file:///…/bcr` |
| Samples | 10 per scenario, base and PR interleaved, 3 warmup runs |
| Machine | Linux x86_64 (AWS), 8 CPUs, 61 GB RAM |

Median ± stdev:

| Scenario | base | PR | Δ median |
|---|---:|---:|---:|
| Cold: new server, first resolution | 2.160 ± 0.061 s | 3.514 ± 0.188 s | **+1.353 s** (+63%) |
| Warm: no-op re-resolution | 0.052 ± 0.006 s | 0.312 ± 0.016 s | **+0.260 s** (+498%) |
| Warm: after editing an unrelated registry file | 0.046 ± 0.006 s | 0.434 ± 0.048 s | +0.388 s |
| Used heap after GC | 40 MB | 86 MB | +46 MB |

What the numbers mean:

- **Cold** is a one-time cost per server start: digesting all ~49k registry files.
- **Warm no-op** is paid on every command. In the JSON profiles the whole difference is in
  `fsvc.getDirtyKeys` (24 ms → 325 ms), the stat of every registry file to check whether it
  changed.
- **Warm registry edit** isn't overhead. Base finishes quickly because it ignores the edit (the
  bug the PR fixes); the PR invalidates the registry and resolves the graph again.
- Both costs scale roughly linearly with the number of registry files. That works out to about
  27 µs per file per server start and about 5 µs per file per command. A full BCR clone is close
  to the worst case; a private registry with a few thousand files would cost tens of milliseconds.

## Reproducing

Requirements: Linux or macOS, Python 3.12+, git, and `bazel` on `PATH` as
[Bazelisk](https://github.com/bazelbuild/bazelisk), which builds Bazel at the version in its
`.bazelversion`. The first run needs about 15 GB of disk space and spends most of its time
building Bazel twice (around 30 minutes on 8 CPUs).

```bash
git clone https://github.com/ankit-agarwal-skydio/bazel-local-registry-benchmark.git
cd bazel-local-registry-benchmark

# Same commits as the results above:
./run_benchmark.py --workdir work \
  --base-ref e86822b8cfe8dacc721b79509efb40b8ea486981 \
  --pr-ref 0e53cbe89a4d358086acfa236a5c5386597bab21 \
  --bcr-commit 9a46e25ff8551c5dd764a02d65cdde852feba569

# Or against the current PR head, its merge base and the latest BCR:
./run_benchmark.py --workdir work
```

The summary table is printed at the end. Reruns reuse the clones and binaries under
`--workdir`, and each run writes a new timestamped directory under `work/results/` containing:

- `results.md`: summary table
- `results.json`: metadata (all commit SHAs, registry size), summaries and raw samples
- `{base,pr}-{cold,warm}.profile.gz`: JSON trace profiles of one cold and one warm run per
  variant, which you can open in `chrome://tracing` or https://ui.perfetto.dev

### What the script does

1. Clones Bazel, fetches the PR head and builds `//src:bazel` (`-c opt`) twice: once at the
   base commit and once at the PR head. Both builds run in the same checkout, so the second one
   is incremental.
2. Clones the BCR and uses it as the only registry: `--registry=file:///…/bcr`.
3. Checks out Bazel's own source at the base commit as the workspace to resolve. It's a real
   module graph (40 direct `bazel_dep`s, single-version and local-path overrides only), so
   resolution needs nothing from the network apart from the registry.
4. Times `bazel mod graph` for both binaries, alternating between them on every iteration:
   - **cold**: `bazel shutdown`, then resolve.
   - **warm no-op**: server running, nothing changed.
   - **warm registry edit**: `modules/zstd/metadata.json` (not in the graph) is rewritten before
     each command, then restored at the end.
   - **heap**: `bazel info used-heap-size-after-gc` after the warm runs.

Every Bazel invocation passes `--ignore_all_rc_files` (benchmark) or
`--nohome_rc --nosystem_rc` (building Bazel), so machine-specific rc files can't affect the
results.

### Flags

| Flag | Purpose |
|---|---|
| `--bcr-commit SHA` | Pin the registry to a specific BCR commit |
| `--strip-git` | Remove the BCR's `.git` directory, which is also digested otherwise |
| `--pr-ref` / `--base-ref` | Compare other Bazel commits |
| `--workspace-repo URL --workspace-commit SHA` | Resolve a different project |
| `--command "build --nobuild //..."` | Time a different command (`--registry` is appended) |
| `--edit-file PATH` | Registry file to edit in the warm edit scenario |
| `--iterations N` / `--warmup N` | Sample count |

### Caveats

- "Cold" means a new Bazel server with a warm OS page cache. Dropping the page cache needs
  root, so the script doesn't do it.
- `--lockfile_mode` is left at its default (`update`), and the workspace starts without a
  lockfile. The warmup runs settle the lockfile before warm timings start.
