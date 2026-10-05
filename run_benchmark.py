#!/usr/bin/env python3
"""Benchmark the overhead of bazelbuild/bazel#31424 against a local clone of the BCR.

The PR makes RegistryFunction depend on the DirectoryTreeDigestValue of a file:// registry root,
so every file in the registry becomes a Skyframe node that is digested on the first command and
dirty-checked on every later one. This script builds Bazel with and without the PR, points both
at the same full BCR clone, and times module resolution of a real open source workspace.

Everything lives under --workdir; rerunning skips steps whose outputs already exist.
"""

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

BAZEL_REPO = "https://github.com/bazelbuild/bazel.git"
BCR_REPO = "https://github.com/bazelbuild/bazel-central-registry.git"

# Never read ~/.bazelrc or /etc/bazel.bazelrc so that machine-specific settings (remote caches,
# resource limits) cannot leak into either the Bazel build or the benchmark.
NO_USER_RC = ["--nohome_rc", "--nosystem_rc"]


def log(msg: str) -> None:
    print(f"[bench] {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str], cwd: Path | None = None, capture: bool = False, **kwargs) -> str:
    log(f"$ {' '.join(map(str, cmd))}" + (f"  (in {cwd})" if cwd else ""))
    result = subprocess.run(
        cmd, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE if capture else None, **kwargs
    )
    return result.stdout.strip() if capture else ""


def git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args], capture=True)


def clone(url: str, dest: Path) -> None:
    if not (dest / ".git").exists():
        run(["git", "clone", "--quiet", url, str(dest)])


# --------------------------------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------------------------------


def resolve_refs(src: Path, pr: int, pr_ref: str | None, base_ref: str | None) -> tuple[str, str]:
    """Returns (base_sha, pr_sha). The base defaults to the merge base of the PR with master."""
    git(src, "fetch", "--quiet", "origin", "master", f"pull/{pr}/head:refs/remotes/origin/pr-{pr}")
    pr_sha = git(src, "rev-parse", pr_ref or f"origin/pr-{pr}")
    base_sha = git(src, "rev-parse", base_ref or git(src, "merge-base", "origin/master", pr_sha))
    return base_sha, pr_sha


def build_bazel(src: Path, sha: str, out: Path, bazel: str) -> None:
    if out.exists():
        log(f"{out} already built, skipping")
        return
    git(src, "checkout", "--quiet", "--detach", sha)
    # Build both variants in the same checkout so the second build is incremental.
    run(
        [bazel, *NO_USER_RC, "build", "--compilation_mode=opt", "//src:bazel"],
        cwd=src,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / "bazel-bin" / "src" / "bazel", out)
    out.chmod(0o755)


def setup_registry(dest: Path, commit: str | None, strip_git: bool) -> str:
    clone(BCR_REPO, dest)
    if commit:
        git(dest, "fetch", "--quiet", "origin", commit)
        git(dest, "checkout", "--quiet", "--detach", commit)
    sha = git(dest, "rev-parse", "HEAD")
    if strip_git and (dest / ".git").exists():
        shutil.rmtree(dest / ".git")
    return sha


def setup_workspace(src: Path, dest: Path, repo: str | None, commit: str) -> None:
    """Checks out the workspace whose module graph is resolved. Defaults to Bazel's own source."""
    if not dest.exists():
        if repo:
            clone(repo, dest)
        else:
            run(["git", "clone", "--quiet", "--shared", str(src), str(dest)])
    git(dest, "fetch", "--quiet", "origin", commit)
    git(dest, "checkout", "--quiet", "--detach", "--force", commit)
    # Start from no lockfile so recorded remote registry hashes do not interfere.
    (dest / "MODULE.bazel.lock").unlink(missing_ok=True)


def count_files(root: Path) -> tuple[int, int]:
    files = 0
    size = 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            files += 1
            size += os.lstat(os.path.join(dirpath, name)).st_size
    return files, size


# --------------------------------------------------------------------------------------------------
# Benchmark
# --------------------------------------------------------------------------------------------------


@dataclass
class Variant:
    name: str
    binary: Path
    output_base: Path
    registry_url: str
    command: list[str]
    workspace: Path

    def startup(self) -> list[str]:
        return [str(self.binary), "--ignore_all_rc_files", f"--output_base={self.output_base}"]

    def invoke(self, args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*self.startup(), *args],
            cwd=self.workspace,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def resolve(self, extra: list[str] | None = None) -> float:
        start = time.perf_counter()
        self.invoke([*self.command, f"--registry={self.registry_url}", *(extra or [])])
        return time.perf_counter() - start

    def shutdown(self) -> None:
        self.invoke(["shutdown"])

    def heap_mb(self) -> float:
        out = self.invoke(["info", "used-heap-size-after-gc"], capture=True).stdout.strip()
        assert out.endswith("MB"), out
        return float(out.removesuffix("MB"))


@dataclass
class Stats:
    samples: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, float]:
        s = self.samples
        return {
            "n": len(s),
            "median": statistics.median(s),
            "mean": statistics.fmean(s),
            "stdev": statistics.stdev(s) if len(s) > 1 else 0.0,
            "min": min(s),
            "max": max(s),
        }


def touch_registry_file(path: Path, i: int) -> None:
    """Rewrites a registry file outside the resolved graph so its digest changes."""
    original = path.read_text().rstrip("\n")
    path.write_text(original + "\n" * (1 + i % 2))


def benchmark(
    variants: list[Variant], iterations: int, warmup: int, edit_file: Path, profile_dir: Path
) -> dict:
    results: dict[str, dict[str, Stats]] = {v.name: {} for v in variants}

    def record(scenario: str, v: Variant, seconds: float) -> None:
        results[v.name].setdefault(scenario, Stats()).samples.append(seconds)

    log("Scenario cold: fresh server, first module resolution (digests the whole registry)")
    for v in variants:
        v.shutdown()
        v.resolve(["--profile", str(profile_dir / f"{v.name}-cold.profile.gz")])
    for i in range(iterations):
        # Interleave variants so slow drift on the machine affects both equally.
        for v in variants if i % 2 == 0 else reversed(variants):
            v.shutdown()
            record("cold", v, v.resolve())

    log("Scenario warm no-op: server up, nothing changed (dirty-checks every registry file)")
    for v in variants:
        for _ in range(warmup):
            v.resolve()
    for v in variants:
        v.resolve(["--profile", str(profile_dir / f"{v.name}-warm.profile.gz")])
    for i in range(iterations):
        for v in variants if i % 2 == 0 else reversed(variants):
            record("warm_noop", v, v.resolve())

    heap = {v.name: v.heap_mb() for v in variants}

    log("Scenario warm edit: an unrelated registry file changed between commands")
    original = edit_file.read_bytes()
    try:
        for i in range(iterations):
            for v in variants if i % 2 == 0 else reversed(variants):
                touch_registry_file(edit_file, i)
                record("warm_registry_edit", v, v.resolve())
    finally:
        edit_file.write_bytes(original)

    for v in variants:
        v.shutdown()

    return {
        "timings_s": {
            name: {scenario: stats.summary() for scenario, stats in scenarios.items()}
            for name, scenarios in results.items()
        },
        "raw_s": {
            name: {scenario: stats.samples for scenario, stats in scenarios.items()}
            for name, scenarios in results.items()
        },
        "used_heap_after_gc_mb": heap,
    }


def render_markdown(meta: dict, bench: dict) -> str:
    names = list(bench["timings_s"])
    base, pr = names
    descriptions = {
        "cold": "Cold: new server, first resolution",
        "warm_noop": "Warm: no-op re-resolution",
        "warm_registry_edit": "Warm: after editing an unrelated registry file",
    }
    lines = [
        "| Scenario | " + " | ".join(f"{n} median (s)" for n in names) + " | Δ median | Δ % |",
        "|---|" + "---:|" * (len(names) + 2),
    ]
    for scenario, label in descriptions.items():
        b = bench["timings_s"][base][scenario]
        p = bench["timings_s"][pr][scenario]
        delta = p["median"] - b["median"]
        cells = [f"{s['median']:.3f} ± {s['stdev']:.3f}" for s in (b, p)]
        lines.append(
            f"| {label} | {' | '.join(cells)} | {delta:+.3f} | {100 * delta / b['median']:+.1f}% |"
        )
    hb = bench["used_heap_after_gc_mb"][base]
    hp = bench["used_heap_after_gc_mb"][pr]
    lines.append(f"| Used heap after GC (MB) | {hb:.0f} | {hp:.0f} | {hp - hb:+.0f} | "
                 f"{100 * (hp - hb) / hb:+.1f}% |")
    header = [
        "## Setup",
        "",
        f"- Bazel base: `{meta['bazel_base_sha']}`",
        f"- Bazel PR:   `{meta['bazel_pr_sha']}`",
        f"- BCR: `{meta['bcr_sha']}` ({meta['registry_files']} files, "
        f"{meta['registry_bytes'] / 1e6:.0f} MB, .git {'stripped' if meta['strip_git'] else 'kept'})",
        f"- Workspace: {meta['workspace_repo']} @ `{meta['workspace_sha']}`",
        f"- Command: `bazel {' '.join(meta['command'])} --registry=file://...`",
        f"- Iterations: {meta['iterations']} (variants interleaved), warmup: {meta['warmup']}",
        f"- Machine: {meta['machine']}",
        "",
        "## Results (median ± stdev)",
        "",
    ]
    return "\n".join(header + lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--workdir", type=Path, default=Path("work"))
    parser.add_argument("--pr", type=int, default=31424)
    parser.add_argument("--pr-ref", help="Bazel commit with the change (default: PR head)")
    parser.add_argument("--base-ref", help="Bazel commit without the change (default: merge base)")
    parser.add_argument("--bcr-commit", help="BCR commit to pin (default: current main)")
    parser.add_argument(
        "--strip-git", action="store_true", help="Delete the BCR's .git directory before benchmarking"
    )
    parser.add_argument("--workspace-repo", help="Workspace to resolve (default: Bazel's own source)")
    parser.add_argument("--workspace-commit", help="Workspace commit (default: Bazel base commit)")
    parser.add_argument(
        "--command",
        default="mod graph",
        help="Bazel command to time; --registry is appended (default: %(default)s)",
    )
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument(
        "--edit-file",
        default="modules/zstd/metadata.json",
        help="Registry file outside the graph to edit in the warm edit scenario",
    )
    parser.add_argument(
        "--bazel", default="bazel", help="Bazelisk/bazel used to build Bazel from source"
    )
    args = parser.parse_args()

    workdir = args.workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / "bazel-src"
    registry = workdir / "bcr"
    workspace = workdir / "workspace"
    results_dir = workdir / "results" / time.strftime("%Y%m%d-%H%M%S")
    results_dir.mkdir(parents=True)

    clone(BAZEL_REPO, src)
    base_sha, pr_sha = resolve_refs(src, args.pr, args.pr_ref, args.base_ref)
    log(f"base={base_sha} pr={pr_sha}")
    binaries = {
        "base": workdir / "bin" / f"bazel-{base_sha[:12]}",
        "pr": workdir / "bin" / f"bazel-{pr_sha[:12]}",
    }
    build_bazel(src, base_sha, binaries["base"], args.bazel)
    build_bazel(src, pr_sha, binaries["pr"], args.bazel)

    bcr_sha = setup_registry(registry, args.bcr_commit, args.strip_git)
    registry_files, registry_bytes = count_files(registry)
    workspace_sha = args.workspace_commit or base_sha
    setup_workspace(src, workspace, args.workspace_repo, workspace_sha)

    edit_file = registry / args.edit_file
    if not edit_file.is_file():
        sys.exit(f"--edit-file {edit_file} does not exist in the registry")

    command = args.command.split()
    variants = [
        Variant(
            name=name,
            binary=binary,
            output_base=workdir / "output_bases" / name,
            registry_url=registry.as_uri(),
            command=command,
            workspace=workspace,
        )
        for name, binary in binaries.items()
    ]

    log("Checking that both variants resolve the workspace against the local registry")
    for v in variants:
        v.resolve()

    bench = benchmark(variants, args.iterations, args.warmup, edit_file, results_dir)
    meta = {
        "bazel_base_sha": base_sha,
        "bazel_pr_sha": pr_sha,
        "bcr_sha": bcr_sha,
        "registry_files": registry_files,
        "registry_bytes": registry_bytes,
        "strip_git": args.strip_git,
        "workspace_repo": args.workspace_repo or BAZEL_REPO,
        "workspace_sha": workspace_sha,
        "command": command,
        "iterations": args.iterations,
        "warmup": args.warmup,
        "machine": f"{platform.platform()}, {os.cpu_count()} CPUs",
    }
    (results_dir / "results.json").write_text(json.dumps({"meta": meta, **bench}, indent=2))
    report = render_markdown(meta, bench)
    (results_dir / "results.md").write_text(report)
    print(report)
    log(f"Results and JSON profiles written to {results_dir}")


if __name__ == "__main__":
    main()
