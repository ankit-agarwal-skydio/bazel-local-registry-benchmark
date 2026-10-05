# Local registry invalidation benchmark (bazelbuild/bazel#31424)

[bazelbuild/bazel#31424](https://github.com/bazelbuild/bazel/pull/31424) makes `RegistryFunction`
depend on the `DirectoryTreeDigestValue` of a `file://` registry root so that edits to a local
registry are picked up without `bazel shutdown`. This puts every file in the registry into
Skyframe: a fresh server digests them all, and each later command dirty-checks them
(`--experimental_check_external_other_files` is on by default).

This repo measures that overhead with a local registry that is a full clone of the
[Bazel Central Registry](https://github.com/bazelbuild/bazel-central-registry).

## What it does

1. Clones Bazel, fetches the PR head and builds `//src:bazel` (`-c opt`) twice: once at the
   merge base with `master` (**base**) and once at the PR head (**pr**). Both builds run in the
   same checkout, so the second one is incremental.
2. Clones the BCR. It is used as the only registry: `--registry=file:///…/bcr`.
3. Checks out Bazel's own source at the base commit as the workspace to resolve. It's a real
   module graph (40 direct `bazel_dep`s, single-version and local-path overrides only), so
   resolution needs nothing from the network apart from the registry.
4. Times `bazel mod graph` for both binaries, interleaving them on every iteration:
   - **cold**: `bazel shutdown`, then resolve. With the PR, this digests the whole registry.
   - **warm no-op**: server running, nothing changed. With the PR, this dirty-checks every
     registry file.
   - **warm registry edit**: an unrelated registry file (`modules/zstd/metadata.json`) changes
     before each command. With the PR, this invalidates the registry and re-resolves; **base**
     does nothing because it ignores the change (that's the bug being fixed). This row shows the
     cost of a correct re-resolution, not overhead.
   - **heap**: `bazel info used-heap-size-after-gc` after the warm runs.

Every Bazel invocation passes `--ignore_all_rc_files` (benchmark) or
`--nohome_rc --nosystem_rc` (building Bazel), so machine-specific rc files can't affect the
results.

## Running it

Requirements: Linux or macOS, Python 3.12+, git, and `bazel` on `PATH` as
[Bazelisk](https://github.com/bazelbuild/bazelisk), which builds Bazel at the version in its
`.bazelversion`. The first run needs about 15 GB of disk space and spends most of its time
building Bazel twice.

```bash
./run_benchmark.py --workdir work
```

Reruns reuse the clones and binaries under `--workdir` and write a new timestamped directory
under `work/results/` containing:

- `results.md`: summary table
- `results.json`: metadata (all commit SHAs, registry size), summaries and raw samples
- `{base,pr}-{cold,warm}.profile.gz`: JSON trace profiles of one cold and one warm run per
  variant, which you can open in `chrome://tracing` or https://ui.perfetto.dev

Useful flags (see `--help`):

| Flag | Purpose |
|---|---|
| `--bcr-commit SHA` | Pin the registry to a specific BCR commit |
| `--strip-git` | Remove the BCR's `.git` directory, which is also digested otherwise |
| `--pr-ref` / `--base-ref` | Compare other Bazel commits |
| `--workspace-repo URL --workspace-commit SHA` | Resolve a different project |
| `--command "build --nobuild //..."` | Time a different command (`--registry` is appended) |
| `--iterations N` / `--warmup N` | Sample count |

## Caveats

- "Cold" means a new Bazel server with a warm OS page cache. Dropping the page cache needs
  root, so the script doesn't do it.
- `--lockfile_mode` is left at its default (`update`), and the workspace starts without a
  lockfile. The warmup runs settle the lockfile before warm timings start.
