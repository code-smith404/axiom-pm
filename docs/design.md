# Design

This document explains the invariants `axiom` maintains, the transaction it
performs on every install, why each step is ordered the way it is, and where
the current implementation deliberately stops.

## 1. Invariants

These are the properties the rest of the system relies on. Tests assert them
directly; changes that weaken one need a very good reason.

1. **Content is addressed by hash.** A blob is named by the SHA-256 of its
   bytes and is never written twice. Package metadata names content by digest.
2. **Package versions are immutable.** Once `name@version` is registered, its
   content digest cannot change. Publishing different bytes under the same
   version is an error; the fix is a new version.
3. **Snapshots are pure.** A snapshot is a function of the environment, the
   selected versions and their file digests. It contains no clock, hostname,
   absolute path or random value.
4. **Generations are immutable.** After a generation directory leaves staging,
   no code path modifies its tree. `verify` detects external modification.
5. **Activation is one atomic operation.** The active generation is identified
   by the `current` symlink; changing it is a single `os.replace`.
6. **Readers are lock-free.** A process reading through `current` never needs
   to coordinate with an install, and always sees a complete generation.
7. **The resolver is deterministic.** Given the same candidates, the same
   requirements produce the same assignment.

## 2. Data flow

```
package.toml + files/ ──build──> .axp ──publish──> store entry + blobs
                                                       │
environment.toml ──load──> requirements ──solve────────┤
                                                       ▼
                                                   snapshot (digest)
                                                       │
                                         materialise   ▼
                              generation directory + symlink
```

No step mutates an earlier step's output. An environment can be resolved
without publishing, a snapshot can be written as a lockfile without
installing, and an artifact can be built without touching a store.

## 3. The install transaction

### 3.1 Steps

```
resolve ──> stage ──> fsync ──> rename ──> symlink swap ──> history
            (invisible)        (data commit) (activation)   (audit)
```

1. **Resolve.** Host constraints are checked first, before any write. The
   solver produces a solution; `snapshot_from_solution` turns it into a
   snapshot and rejects duplicate install targets. Every referenced blob must
   exist.
2. **Stage.** A directory `generations/.staging-<pid>-<random>` is created.
   Files are copied from blobs (never hardlinked, see §3.3), fsynced, and the
   mode from the snapshot is applied. A failed copy removes the staging
   directory.
3. **Write metadata.** `GENERATION.json` records the snapshot, its digest, the
   parent snapshot, the action, a timestamp and the host facts used by
   constraints. Timestamps live here, not in the snapshot digest.
4. **Commit data.** `os.rename(staging, generations/<seq>-<digest12>)` is
   atomic within the profile filesystem. The generation is now complete and
   eligible for rollback, but not active.
5. **Commit activation.** A temporary symlink is created in the profile
   directory and `os.replace`d over `current`. This is the transaction's commit
   point. The directory is fsynced so the replacement survives a crash.
6. **Audit.** A history entry is appended atomically. History is derived data:
   if it is lost or truncated, `Profile.generations()` still reconstructs the
   truth from generation metadata.

### 3.2 Failure analysis

| Failure point | State after crash | Recovery |
| --- | --- | --- |
| During resolve | Nothing written | Retry |
| During staging | `current` unchanged; invisible staging directory | Next install under the lock removes stale staging from dead processes |
| Between rename and symlink swap | A complete, inactive generation exists; `current` points at the old one | Install again (new sequence) or activate the generation explicitly; prune if unwanted |
| During symlink swap | Either old or new generation is active; `os.replace` cannot leave a third state | Retry or roll back |
| After activation, before history append | New generation active; history entry missing | `status`/`generations` remain correct; history can be rebuilt |
| During rollback | Same as activation: old or target generation | Retry |

The only multi-file operation that is not atomic is history append, and history
is not consulted for correctness. `current` plus the content of generation
directories are the source of truth.

### 3.3 Why copy instead of hardlink

Hardlinking a store blob into a generation would share an inode. A user (or a
program) could `chmod` or write to the installed file and silently change the
store blob, breaking content addressing and `verify` for every profile. Copying
costs disk space but preserves the invariant that a blob is immutable. Blobs
are still deduplicated *within* the store; generations are independent.

### 3.4 Locking

Writers take an exclusive `flock` on `profiles/<name>/profile.lock` for the
whole transaction, and the store takes `store.lock` while registering content
or collecting garbage. The lock is advisory but universal within `axiom`, and
the kernel releases it if a process dies. Readers never lock.

Two concurrent installs into the same profile therefore serialise. Two installs
into different profiles may proceed in parallel except while touching the
store.

### 3.5 Garbage collection

Two kinds of garbage exist:

- **Blobs** not referenced by any registered package entry or by a generation
  supplied via `--keep`. Because every registered package keeps its blobs, GC
  reclaims only content that was never registered (for example a manually
  inserted blob or an interrupted ingest).
- **Generations** beyond a retention count. `prune` never removes the active
  generation. It rewrites history to drop references to removed generations.

GC is conservative by design. Reclaiming more would require analysing
lockfiles outside the store and deciding which uninstalled packages are
disposable; that is a separate policy, not an invariant.

## 4. Version algebra

Versions follow semver 2.0.0 precedence: numeric components, then pre-release
identifiers (numeric below alphanumeric, shorter prefix below longer list),
then release above pre-release. Build metadata is excluded from precedence but
included in equality and used as a deterministic tie-breaker in the total order.

Supported range syntax is a deliberate subset of npm ranges:

| Form | Meaning |
| --- | --- |
| `1.2.3`, `=1.2.3` | exact |
| `>`, `>=`, `<`, `<=`, `!=` | comparison |
| `^1.2.3` | `>=1.2.3 <2.0.0`; `^0.2.3` is `<0.3.0`; `^0.0.3` is `<0.0.4` |
| `~1.2.3` | `>=1.2.3 <1.3.0`; `~1` is `<2.0.0` |
| `1.2.x`, `1.x`, `1.2.*` | wildcard |
| `1.2`, `1` | partial version, wildcard semantics |
| `1.2.3 - 2.0.0` | inclusive hyphen range |
| `A, B` | conjunction |

Caret and tilde are expanded to explicit bounds at parse time so that
intersection and error reporting only deal with simple constraints. OR
expressions are rejected: a dependency is either compatible or it is not, and
disjunctions make unsatisfiability explanations much less useful.

Pre-releases follow npm's rule: a pre-release candidate matches only ranges
that explicitly mention a pre-release with the same `major.minor.patch`. `*` and
`>=1.0.0` do not silently select `2.0.0-rc.1`. `--prerelease` lifts the gate for
an explicit request.

## 5. Resolver

The resolver is a depth-first backtracking search over requirement names.

State:

- `selected`: real package name → chosen package (one version per name),
- `claims`: requirement name → real package name (for `provides`),
- `pending`: requirement name → intersected range,
- `provenance`: requirement name → list of (requirer, range) for explanations.

At each step:

1. For every pending name, compute viable candidates under all accumulated
   constraints. If any name has none, record a failure and return.
2. Choose the name with the fewest candidates, breaking ties alphabetically.
3. Try candidates in preference order: highest version first, stable before
   pre-release, direct name before virtual provider.
4. Reject candidates that conflict with an already selected package.
5. Add the candidate's non-optional dependencies to the pending set, intersect
   ranges for names that are already pending, and prune immediately if the
   intersection has no viable candidate.
6. Recurse. On failure, restore the previous state and try the next candidate.

The search has a step budget (200,000 by default) so a pathological input fails
with an explanation instead of hanging. Failures are collected with the
constraint and the packages that requested it, which is what makes messages
like this possible:

```
axiom: cannot resolve environment 'dev': no assignment satisfies 'lib'
  - lib >=2.0.0,<3.0.0 (available: 1.0.0, 1.5.0; app@2.0.0 wants >=2.0.0,<3.0.0)
  - lib <2.0.0 (available: 1.0.0, 1.5.0; environment 'dev' wants <2.0.0)
```

Optional dependencies are activated only when another requirement (or an
already selected package) needs the same name. `provides` lets a package
satisfy a virtual requirement; once a provider is selected, further
requirements for the virtual name are pinned to it. Features are recorded in
the snapshot but do not create multiple slots for the same package name.

The resolver is not an optimiser. It returns the first solution in preference
order, not a minimum-size or maximum-version global solution. Determinism is
the design goal: identical inputs give identical snapshots.

## 6. Snapshot identity

`Snapshot.digest` is `sha256:` over canonical JSON (sorted keys, compact
separators, UTF-8) of:

- format version,
- environment name,
- packages sorted by name: name, version, store content digest, artifact
  digest, features, and file records (target, digest, mode, size, owner),
- variables sorted by key,
- environment constraints in declared order.

Excluded on purpose: wall-clock time, hostname, store path, profile name,
generation number, and the host facts used to validate constraints. This is
what makes a lockfile portable and lets two stores holding the same package
content agree on the identity of an environment.

Package-level metadata (description, homepage, scripts) is not in the snapshot;
two packages with identical files and dependencies but different descriptions
install identically, and changing a description does not change a profile's
identity. The artifact digest still changes.

## 7. Trust boundaries

`axiom` protects against *malicious archives*, not against a malicious local
user with write access to your store or profile. Concretely:

- Archive reading rejects traversal, absolute paths, duplicates, links and
  special files, then verifies every payload digest and size.
- The store refuses to change an existing package version.
- Install targets are lexically confined to the generation tree.
- Modes are limited to `0777`; setuid/setgid/sticky are refused.
- No package script is executed at any point. `scripts` is metadata for other
  tools.
- Installed files are ordinary files owned by the invoking user.

Artifact authenticity comes from the digest. `axiom` does not sign or verify
signatures; pass `--digest` to `publish` when the expected digest comes from a
trusted channel. TLS is the caller's responsibility because there is no network
client in this version.

## 8. Deliberate non-goals

- **One version per package name.** Side-by-side versions and feature-split
  slots would complicate the model; the current snapshot format has no place
  for them.
- **Global optimum resolution.** Backtracking with a fixed preference order is
  predictable and adequate for small user-space environments.
- **Hardlink farm / deduplicating generations.** Immutability wins over disk
  usage; a future `--hardlink` would need a copy-on-write filesystem or
  read-only bind semantics to stay safe.
- **Automatic garbage collection of uninstalled packages.** GC is explicit and
  conservative.
- **Running package build scripts.** Packages are data; building is done by
  their authors before `axiom build`.
- **Windows.** Atomic symlink replacement and POSIX modes are load-bearing.
