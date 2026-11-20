# Formats

Everything `axiom` touches is a file with a documented shape: TOML for humans,
canonical JSON for hashes and machine state, tar for artifacts. This document is
the reference for all of them.

- [Package definition](#package-definition)
- [Environment definition](#environment-definition)
- [Version ranges](#version-ranges)
- [Artifact format](#artifact-format)
- [Lockfile](#lockfile)
- [Store layout](#store-layout)
- [Generation metadata](#generation-metadata)
- [Paths, modes and names](#paths-modes-and-names)
- [Host facts](#host-facts)

---

## Package definition

A package directory contains `package.toml` and a payload directory. By default
the payload is `files/` (the fallback is `payload/`).

```toml
name = "hello"
version = "1.0.0"
description = "A tiny greeting tool"
license = "MIT"
homepage = "https://example.invalid/hello"

# Virtual names this package satisfies.
provides = ["greeting"]

# Conflicts may be a list of "name range" strings...
conflicts = ["legacy <1.0.0"]

# ...or an array of tables:
# [[conflicts]]
# name = "legacy"
# constraint = "<1.0.0"

# Dependencies: array-of-tables form.
[[dependencies]]
name = "libmath"
constraint = ">=1.0.0,<2.0.0"
optional = false
features = []

# Or table form:
# [dependencies]
# libmath = ">=1.0.0,<2.0.0"

# Files. Without any [[files]] entries, every regular file under files/ is
# installed at the same relative path and its mode is inferred from the
# executable bit (0755 if any execute bit is set, otherwise 0644).
[[files]]
source = "bin/hello"
target = "bin/hello"
mode = "0755"

[[environment_constraints]]
kind = "os"
name = "system"
operator = "=="
value = "linux"
message = "requires POSIX shell"

[scripts]
# Metadata for other tools. axiom never executes these.
post-install = "echo this is not run"
```

### Fields

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | string | yes | Package name. No `/`, `\`, whitespace or newline. |
| `version` | string | yes | Semantic version. |
| `description` | string | no | Human description. |
| `license` | string | no | SPDX identifier or free text. |
| `homepage` | string | no | Informational URL. |
| `provides` | array of strings | no | Virtual package names. |
| `conflicts` | array of strings or tables | no | Packages/ranges that cannot coexist. |
| `dependencies` | array of tables or table | no | Requirements on other packages. |
| `files` | array of tables | no | Payload-to-target mapping. |
| `environment_constraints` | array of tables | no | Host assertions checked at install. |
| `scripts` | table of strings | no | Metadata only; never executed. |

### Dependencies

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | required | Dependency name. |
| `constraint` | string | `"*"` | Version range. |
| `optional` | bool | `false` | Install only if something else needs it. |
| `features` | array of strings | `[]` | Recorded in the snapshot; does not create a second slot. |

The solver keeps one version per package name. Features are advisory metadata
for consumers, not a mechanism for installing the same package twice.

### Files

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `source` | string | required | Path under the payload directory. |
| `target` | string | same as `source` | Path inside the profile. |
| `mode` | string | `"0644"` | Four-digit octal permission, `0000`–`0777`. |

Declared sources must exist, must be regular files, and may not be symlinks.
Targets must be unique within the package. Setuid, setgid and sticky bits are
refused.

### Environment constraints on a package

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `kind` | string | `"host"` | Fact namespace. |
| `name` | string | required | Key inside the namespace. |
| `operator` | string | `"=="` | See [operators](#operators). |
| `value` | string | `""` | Expected value. |
| `message` | string | `""` | Extra text in failure messages. |

Package constraints are checked when the package is selected for an install.
They do not affect which version the solver picks (the current implementation
checks them after resolution).

## Environment definition

```toml
name = "dev"
description = "Development profile"
includes = ["base.toml"]

[packages]
app = "^2.0.0"
hello = ">=1.0.0,<2.0.0"

# Optional/annotated form:
# [[packages]]
# name = "app"
# constraint = "^2.0.0"
# optional = false
# reason = "the main application"

[variables]
APP_ENV = "development"
EDITOR = "vi"

[[environment_constraints]]
kind = "os"
name = "system"
operator = "=="
value = "linux"
message = "example packages use POSIX shell scripts"
```

### Fields

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | string | yes (defaults to file stem when loaded from a file) | Environment name. Part of the snapshot digest. |
| `description` | string | no | Human description; not hashed. |
| `includes` | array of strings | no | Other environment files, relative to this file. |
| `packages` | table or array of tables | no | Root requirements. |
| `variables` | table of strings | no | Exported by `axiom run` and `axiom shell`; part of the snapshot digest. |
| `environment_constraints` | array of tables | no | Host assertions checked before install. |

### Include semantics

Includes are resolved recursively, relative to the including file:

- packages from every included file are appended, then duplicate names are
  merged by intersecting their ranges (order of first appearance is kept);
- environment constraints are appended;
- variables are merged; an including file overrides values from its includes;
- `name` and `description` come from the including file;
- cycles are detected and rejected; nesting deeper than 16 levels is rejected.

Includes are flattened before resolution, so the snapshot does not depend on
file paths.

## Version ranges

A version is `major.minor.patch[-prerelease][+build]`; `1` and `1.2` are
normalised to `1.0.0` and `1.2.0` by the version parser, but in a **range** a
partial version means a wildcard: `1.2` is `1.2.x`, `1` is `1.x`.

| Expression | Meaning |
| --- | --- |
| `*`, `x`, `""` | any version |
| `1.2.3`, `=1.2.3`, `==1.2.3` | exactly `1.2.3` |
| `!=1.2.3` | any version except `1.2.3` |
| `>`, `>=`, `<`, `<=` | comparison |
| `^1.2.3` | `>=1.2.3,<2.0.0` |
| `^0.2.3` | `>=0.2.3,<0.3.0` |
| `^0.0.3` | `>=0.0.3,<0.0.4` |
| `^0`, `^0.0` | width-aware caret (`^0` is `<1.0.0`) |
| `~1.2.3` | `>=1.2.3,<1.3.0` |
| `~1.2` | `>=1.2.0,<1.3.0` |
| `~1` | `>=1.0.0,<2.0.0` |
| `1.2.x`, `1.2.*`, `1.x` | wildcard |
| `1.2.3 - 2.0.0` | inclusive range; partial right side widens (`1.2 - 2` is `<3.0.0`) |
| `>=1,<2,!=1.5` | comma-separated conjunction |

OR (`||`) is not supported. Caret and tilde are expanded to explicit `>=`/`<`
bounds during parsing, so `str(parse_constraint("^1.2"))` is
`">=1.2.0,<2.0.0"`.

### Pre-releases

A candidate pre-release matches only if the range explicitly contains a
pre-release with the same `major.minor.patch`. Thus:

- `^1.2.0` does **not** match `1.3.0-rc.1`;
- `>=1.3.0-rc.1` matches `1.3.0-rc.2`;
- `*` does not match `2.0.0-beta.1`.

`axiom --prerelease` (on `resolve`, `lock`, `install`) disables the gate for
that operation.

### Ordering

Semver 2.0.0 precedence: numeric identifiers compare numerically and sort below
alphanumeric identifiers; a shorter pre-release list sorts below a longer one
with the same prefix; a release sorts above its pre-releases. Build metadata is
ignored for precedence but retained for equality and used as a deterministic
tie-breaker when sorting.

## Artifact format

An `.axp` file is an uncompressed POSIX tar archive produced by `axiom build`.

```
axiom-artifact.json
files/bin/hello
files/share/hello/message.txt
```

Rules:

- exactly one `axiom-artifact.json` at the root;
- payload entries live under `files/` and name their **install target**, not
  their source path;
- only regular files carry payload; directory entries are ignored, and symlinks,
  hard links, device nodes and duplicate normalized names are rejected;
- names are relative, POSIX, and may not contain `..`;
- entries are sorted by name; `mtime = 0`; `uid = gid = 0`; `uname = gname = ""`;
- the archive uses PAX format so long names are representable, but the writer
  never emits PAX headers for ordinary files.

### `axiom-artifact.json`

Canonical JSON with sorted keys and compact separators:

```json
{
  "format": 1,
  "package": {
    "name": "hello",
    "version": "1.0.0",
    "dependencies": [],
    "provides": [],
    "conflicts": [],
    "files": [
      {"source": "bin/hello", "target": "bin/hello", "mode": "0755"}
    ],
    "environment_constraints": [],
    "description": "",
    "homepage": "",
    "license": "",
    "scripts": {}
  },
  "files": [
    {
      "target": "bin/hello",
      "digest": "3f...",
      "mode": "0755",
      "size": 18
    }
  ]
}
```

`files[].digest` is the bare lowercase hex SHA-256 of the payload bytes;
`size` is the byte length. `package.files` preserves the authoring source/target
mapping, while `files` is the authoritative install manifest.

### Digests

| Name | Definition |
| --- | --- |
| file digest | `sha256` of the file's bytes (hex, unprefixed inside metadata) |
| content digest | `sha256` of canonical JSON `{"format":1,"package":…,"files":[sorted records]}` |
| artifact digest | `sha256` of the archive's bytes |

The **artifact digest** names the archive bytes and is what `publish --digest`
checks. The **content digest** names the package definition plus its files; the
store records it and refuses to republish a version with a different value.
Rebuilding the same directory gives identical bytes, so both digests are stable.

## Lockfile

`axiom.lock.json` (or the path passed with `--lockfile`):

```json
{
  "lockfile_version": 1,
  "digest": "sha256:...",
  "snapshot": {
    "format": 1,
    "environment": "dev",
    "packages": [
      {
        "name": "hello",
        "version": "1.0.0",
        "digest": "sha256:...",
        "artifact": "…",
        "features": [],
        "files": [
          {
            "target": "bin/hello",
            "digest": "3f...",
            "mode": "0755",
            "size": 18,
            "package": "hello@1.0.0"
          }
        ]
      }
    ],
    "variables": {},
    "environment_constraints": []
  }
}
```

`digest` is computed over the `snapshot` object only. Reading a lockfile
verifies that the recorded digest matches the recomputed one, so hand edits are
detected.

`axiom install --locked` reads the lockfile and then **re-resolves the
environment**. If the fresh resolution has a different snapshot digest, the
install fails with "lockfile is out of date" rather than installing a mixture of
old and new state. This catches added, removed and changed requirements.

## Store layout

```
$AXIOM_ROOT/                     default ~/.axiom
├── format.json                  {"format": 1}
├── blobs/
│   └── ab/
│       └── 0123...ef             file bytes, mode 0444
├── packages/
│   └── hello/
│       └── 1.0.0/
│           └── entry.json
├── artifacts/
│   └── <artifact-sha256>.axp    verified archive cache, mode 0444
├── profiles/
│   └── dev/                     see below
├── snapshots/                   reserved for recorded snapshots
├── tmp/                         staging for atomic writes
└── store.lock                   advisory writer lock
```

### `entry.json`

```json
{
  "format": 1,
  "package": { "...": "Package.to_dict()" },
  "files": [
    {"target": "bin/hello", "digest": "3f...", "mode": "0755", "size": 18}
  ],
  "content_digest": "sha256:...",
  "artifact": "…",
  "artifact_size": 1024,
  "imported_at": "2026-01-01T00:00:00Z"
}
```

`imported_at` is audit data; it is not part of any digest. `verify` recomputes
`content_digest` from the package and file records and reports mismatches.

### Profile layout

```
profiles/dev/
├── current -> generations/000003-1a2b3c4d5e6f
├── previous -> generations/000002-…
├── profile.lock
├── history.json
└── generations/
    ├── 000001-…
    ├── 000002-…
    └── 000003-1a2b3c4d5e6f/
        ├── GENERATION.json
        └── files/
            ├── bin/app
            └── lib/libmath.py
```

Generation names are `%06d-<first 12 hex of snapshot digest>`. Staging
directories are named `.staging-<pid>-<random>` and never appear in
`generations()`.

## Generation metadata

`GENERATION.json`:

```json
{
  "format": 1,
  "sequence": 3,
  "name": "000003-1a2b3c4d5e6f",
  "snapshot": { "...": "snapshot object" },
  "snapshot_digest": "sha256:...",
  "action": "install",
  "created_at": "2026-01-01T00:00:00Z",
  "parent": "sha256:...",
  "facts": {"os": {"system": "linux"}},
  "axiom_version": "0.1.0"
}
```

`action` is `install` or `rollback` (the latter only in history entries; rollback
does not create a generation). `parent` is the snapshot that was active before
this generation. `facts` records only the host fact values referenced by
constraints, never the whole environment.

Loading a generation verifies that `snapshot_digest` matches the recomputed
digest; a mismatch is reported as corruption.

## Paths, modes and names

- **Install targets** are relative POSIX paths. Absolute paths, `..` components,
  empty components and backslashes are rejected. Two packages may not install
  the same target; a file/directory collision is caught during materialisation
  and aborts the transaction.
- **Modes** are four-digit octal strings (`0644`, `0755`). Values above `0777`
  are rejected, which rules out setuid, setgid and sticky bits. The declared
  mode is applied exactly, regardless of umask.
- **Package names** may not contain `/`, `\`, whitespace or newlines. Profile
  names additionally may not start with `.`.
- **Blob files** are written read-only (`0444`). Installed copies use the
  snapshot's mode.

## Host facts

`environment_constraints` are evaluated against namespaces collected from the
running machine:

| Namespace | Keys (examples) |
| --- | --- |
| `host` | `name`, `user`, `uid`, `container`, `python`, `os`, `arch`, `kernel` |
| `os` | `system`, `id`, `version`, `codename`, `family`, `name` |
| `arch` | `machine`, `bits`, `endianness` |
| `kernel` | `name`, `release`, `version` |
| `python` | `version`, `major`, `minor`, `implementation` |
| `env` | every environment variable of the invoking process |
| `axiom` | `version` |

### Operators

| Operator | Behaviour |
| --- | --- |
| `==`, `=` | string equality |
| `!=` | string inequality |
| `>`, `>=`, `<`, `<=` | numeric comparison; both sides must parse as numbers |
| `in`, `not-in` | membership in a comma-separated list |
| `contains` | substring |
| `startswith` | prefix |

A missing fact fails the constraint with a message naming the namespace and key.
Host checks happen before anything is written; `--ignore-host` is available for
deliberate overrides and records the check as bypassed only by omission from the
generation facts.
