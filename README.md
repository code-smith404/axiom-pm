# axiom

[![tests](https://github.com/code-smith404/axiom-pm/actions/workflows/tests.yml/badge.svg)](https://github.com/code-smith404/axiom-pm/actions/workflows/tests.yml)

**An immutable, atomic, user-space package manager driven by declarative
constraints.**

`axiom` installs packages into isolated profiles without touching the host's
package manager. Every install produces an immutable snapshot with a content
digest; activation is a single atomic symlink swap; rollback is a single
atomic symlink swap in the other direction. If an install is interrupted at
any point, the active profile is exactly what it was before.

```console
$ axiom install examples/environments/dev.toml -p dev --lock
activated profile 'dev' generation 000001-4a1c8fa92b13
  snapshot: sha256:4a1c8fa92b13...
  packages: 3, files: 4
$ axiom -p dev run -- app axiom
hello, axiom
clamped: 10
$ axiom -p dev history
SEQ  GENERATION           ACTION   ENVIRONMENT  SNAPSHOT
1    000001-4a1c8fa92b13  install  dev          4a1c8fa92b13
```

---

## Contents

- [Why](#why)
- [Guarantees](#guarantees)
- [Install](#install)
- [Quickstart](#quickstart)
- [Concepts](#concepts)
- [Command overview](#command-overview)
- [How atomicity works](#how-atomicity-works)
- [Reproducibility and lockfiles](#reproducibility-and-lockfiles)
- [Security model](#security-model)
- [Limitations](#limitations)
- [Development](#development)
- [Documentation](#documentation)
- [License](#license)

---

## Why

Traditional package managers combine three separate concerns:

1. deciding which versions satisfy a set of constraints,
2. mutating a shared filesystem, and
3. remembering what was installed.

Combining them makes failures hard to reason about and rollback hard to trust.
`axiom` separates the three:

- **Resolution** is a pure function from declared constraints to an immutable
  snapshot. The same inputs always produce the same digest.
- **Materialisation** copies content-addressed blobs into a new generation.
  Existing generations are never modified.
- **Activation** replaces one symlink. Readers see the old generation or the
  new one, never a mixture.

The package format, the solver, the store and the profile layout are all
ordinary files in a directory you own. Nothing requires root, nothing edits
`/usr`, and nothing depends on a daemon.

## Guarantees

| Property | How it is achieved |
| --- | --- |
| **Immutable content** | Every file is stored under the SHA-256 of its bytes and opened read-only. Package versions cannot be republished with different contents. |
| **Reproducible environments** | Snapshot digests are computed over canonical JSON with no timestamps, hostnames or absolute paths. Artifact builds are byte-for-byte reproducible. |
| **Atomic activation** | A new generation is fully written and fsynced before `current` is replaced with `os.replace` on a symlink. |
| **Crash safety** | A crash before the symlink swap leaves the previous generation active; staging directories are invisible and cleaned up later. |
| **Instant rollback** | Old generations are immutable and still on disk, so rollback is one symlink swap. |
| **Verifiability** | `axiom verify` re-hashes installed files against the snapshot (ignoring runtime bytecode caches); `axiom verify --store` checks the content store. |
| **No privilege escalation** | Everything lives in `~/.axiom`; setuid/setgid modes are refused; nothing is executed during install. |
| **Declarative** | Desired state lives in TOML. There is no imperative install history to reconstruct. |

## Install

Requires **Python 3.11+** on a POSIX system (Linux, macOS, BSD). There are no
runtime dependencies outside the standard library.

```sh
git clone <this repository> axiom-pm
cd axiom-pm
python3 -m pip install --user -e .
# or, in an isolated environment:
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
```

Without installing the package, run it from the checkout:

```sh
PYTHONPATH=src python3 -m axiom --help
```

## Quickstart

The repository ships four example packages and three environments. The
following builds them, publishes them to a local store, installs the `dev`
environment into an isolated profile and runs its application:

```sh
export AXIOM_ROOT="$PWD/.axiom-demo"

for pkg in hello libmath app legacy; do
  axiom build "examples/packages/$pkg" --publish
done

axiom resolve examples/environments/dev.toml
axiom install examples/environments/dev.toml -p dev --lock
axiom -p dev list
axiom -p dev run -- app axiom
axiom -p dev verify
```

Expected output includes `hello, axiom` and `clamped: 10`, because `app`
depends on both `hello` and `libmath`, and the solver installed them into the
same profile.

Try a conflict: add `legacy = "*"` to `examples/environments/dev.toml` and run
`axiom resolve` again. The resolver refuses with an explanation instead of
producing a broken profile.

```sh
axiom -p dev history
axiom -p dev rollback
axiom -p dev diff examples/environments/dev.toml
```

## Concepts

### Store

The store is a user-owned directory, by default `~/.axiom`, overridden with
`--root` or `$AXIOM_ROOT`:

```
~/.axiom/
├── blobs/ab/<sha256>            file contents, mode 0444
├── packages/<name>/<version>/entry.json
├── artifacts/<sha256>.axp       verified artifact cache
├── profiles/<name>/             isolated installed trees
└── tmp/                         staging for atomic writes
```

Registering a package copies its files into `blobs/` and records the mapping in
`entry.json`. Registering the same version with different content is refused:
published versions are immutable.

### Package artifact (`.axp`)

A package is a directory with a `package.toml` and a `files/` payload. Building
it produces a deterministic tar archive:

```
axiom-artifact.json
files/<install-target>
```

The artifact digest identifies the bytes; the content digest identifies the
package definition plus every file digest. Rebuilding the same directory
produces identical bytes, so the digest can be used as a registry key or a
download pin.

### Environment

An environment is TOML describing what must be true: package constraints,
host assertions and variables. Environments may include other environments;
duplicate requirements are intersected.

```toml
name = "dev"
includes = ["base.toml"]

[packages]
app = "^2.0.0"
hello = ">=1.0.0,<2.0.0"

[variables]
APP_ENV = "development"

[[environment_constraints]]
kind = "os"
name = "system"
operator = "=="
value = "linux"
```

See [`docs/format.md`](docs/format.md) for the complete schema.

### Snapshot

Resolving an environment produces a snapshot: exact versions, content digests,
file modes and sizes, variables and host assertions. Its digest is the identity
of that state. Two machines that resolve the same environment against the same
package content produce the same digest.

### Profile and generation

A profile is an isolated installed tree. Each install creates a generation
directory under `profiles/<name>/generations/` and points `current` at it.
Generations are never modified after activation, which is what makes rollback
free.

```
profiles/dev/
├── current -> generations/000003-4a1c8fa92b13
├── generations/000003-4a1c8fa92b13/
│   ├── GENERATION.json
│   └── files/bin/app
└── history.json
```

## Command overview

| Command | Purpose |
| --- | --- |
| `axiom build DIR [-o FILE] [--publish]` | Build a reproducible `.axp` artifact. |
| `axiom publish PATH...` | Register an artifact or package directory. |
| `axiom search [QUERY]` | Search registered package names and descriptions. |
| `axiom info NAME[@RANGE]` | Show package metadata and files. |
| `axiom resolve ENV` | Resolve and print a plan without changing anything. |
| `axiom lock ENV` | Resolve and write a lockfile. |
| `axiom install ENV [--lock] [--locked]` | Resolve, materialise and atomically activate. |
| `axiom diff ENV` / `axiom diff A B` | Compare the active profile or two snapshots. |
| `axiom rollback [REF]` | Atomically activate an earlier generation. |
| `axiom history` | Show generation history. |
| `axiom list [--store]` | List packages in a profile or the store. |
| `axiom status` | Show the active generation and snapshot. |
| `axiom verify [REF] [--store]` | Verify installed files or the store. |
| `axiom gc [--dry-run] [--prune-generations N]` | Remove unreferenced blobs and old generations. |
| `axiom profiles` | List profiles and their active snapshots. |
| `axiom run -- CMD...` | Run a command with the profile's `PATH` and variables. |
| `axiom shell` | Print shell exports for the active profile. |
| `axiom export [-o FILE]` | Export the active snapshot as a lockfile. |
| `axiom env init/validate/show` | Scaffold, validate and inspect environments. |

Global options: `--root PATH`, `--profile/-p NAME`, `--json`, `-v/--verbose`.
Every read command supports `--json`.

Full reference: [`docs/cli.md`](docs/cli.md).

## How atomicity works

`axiom install` performs these steps:

1. **Check** host constraints and resolve the environment to a snapshot.
2. **Stage** a new generation directory under `generations/.staging-<pid>-...`.
   Files are copied from the store, flushed and fsynced. `current` is not
   touched, so readers keep using the old generation.
3. **Commit data** by renaming the staging directory to its final generation
   name. The generation is now complete and visible, but inactive.
4. **Commit activation** by creating a temporary symlink and calling
   `os.replace(temp, current)`. This is the single atomic commit point.
5. **Record** a history entry (best effort; it is an audit log and can be
   rebuilt from the generation directories).

A crash before step 4 leaves `current` pointing at the previous generation and
the half-written staging directory is ignored and cleaned up later. A crash
after step 4 leaves the new generation active and intact. There is no window in
which `current` is missing or points at partial content.

Rollback goes through the same activation step without materialising anything.
The profile lock (`flock`) serialises writers; readers take no lock and rely on
the atomicity of the symlink swap.

## Reproducibility and lockfiles

```sh
axiom lock examples/environments/dev.toml      # writes axiom.lock.json
axiom install examples/environments/dev.toml --locked
```

`axiom.lock.json` contains the full snapshot and its digest. `--locked` is
strict: the environment is resolved again and compared with the lockfile. If
the environment changed without the lockfile being updated, installation fails
instead of silently installing something different.

Because snapshot digests exclude timestamps, hostnames and paths, the same
environment and package content produce the same lockfile digest on different
machines. `axiom diff` uses that property to answer "what would change?".

## Security model

`axiom` assumes artifacts may be malicious and treats them as untrusted input:

- absolute paths, `..`, backslashes, duplicate entries, symlinks, hard links and
  device nodes are rejected while reading archives;
- every payload file is checked against its declared SHA-256 and size before it
  enters the store;
- install targets are confined to the generation tree;
- setuid, setgid and sticky bits are refused;
- package scripts are metadata only — `axiom` **never executes** them;
- nothing runs as root and nothing writes outside the store root.

The artifact digest is the trust anchor. When downloading an artifact with
another tool, pass the pinned digest:

```sh
axiom publish ./hello-1.0.0.axp --digest <expected-sha256>
```

`axiom` is a package manager, not a sandbox. It does not restrict what an
installed program does when you choose to run it. It also does not sign
artifacts; pair it with your own signature or TLS verification if you need
authenticity beyond digest pinning.

## Limitations

- POSIX only. The atomic activation strategy relies on atomic symlink
  replacement.
- Package content is copied into each generation rather than hardlinked, so
  updates use more disk space in exchange for strict immutability.
- The resolver keeps **one version per package name**. It is a deterministic
  backtracking search, not a full SAT optimiser; it finds a solution according
  to a fixed preference order, not the globally "best" one.
- `provides` names are plain virtual names, not versioned virtual packages.
- There is no remote registry client. Artifacts can be produced or downloaded
  by any tool and registered with `axiom publish`, optionally pinning the
  digest.
- Optional dependencies are installed only when something else requires them.
- No delta updates, binary signing, or per-user daemon.

## Development

```sh
git clone <this repository> axiom-pm
cd axiom-pm
python3 -m unittest discover -s tests -t .   # stdlib only
# or: python3 -m pytest
```

The test suite covers the version algebra, archive hardening, content store,
solver backtracking and conflicts, snapshots, atomic activation and rollback,
crash/failure safety, concurrent readers, the CLI and an end-to-end reproducible
install into two independent stores.

Project layout:

```
src/axiom/
├── version.py      versions, ranges, prerelease rules
├── model.py        immutable packages, requirements, environments
├── manifest.py     TOML loading, includes, normalisation
├── artifact.py     deterministic .axp build/read/verify
├── store.py        content-addressed blobs and package registry
├── solver.py       deterministic backtracking resolver
├── snapshot.py     snapshot identity, lockfiles, diffs
├── profile.py      generations, atomic activation, rollback, verify
├── installer.py    orchestration and host checks
├── host.py         host facts and environment constraints
└── cli.py          command-line interface
```

## Documentation

- [`docs/design.md`](docs/design.md) — invariants, transaction protocol,
  resolver design, failure analysis.
- [`docs/format.md`](docs/format.md) — package, environment, artifact and
  lockfile formats, store layout.
- [`docs/cli.md`](docs/cli.md) — command reference and recipes.
- [`examples/`](examples/) — runnable packages and environments.

## License

MIT. See [LICENSE](LICENSE).
