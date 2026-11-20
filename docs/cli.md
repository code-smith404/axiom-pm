# Command reference

```
axiom [GLOBAL OPTIONS] COMMAND [COMMAND OPTIONS] [ARGS]
```

Global options may appear before or after the command name:

| Option | Meaning |
| --- | --- |
| `--root PATH` | Store root. Defaults to `$AXIOM_ROOT`, then `~/.axiom`. |
| `--profile NAME`, `-p NAME` | Profile to operate on. Defaults to `$AXIOM_PROFILE`, then `default`. |
| `--json` | Emit machine-readable JSON on stdout. Errors are emitted as a JSON object on stdout and a human message on stderr. |
| `-v`, `--verbose` | Accepted for compatibility; reserved for future logging. |
| `--version` | Print the version and exit. |

Commands that read state support `--json`; `install`, `lock`, `build`,
`publish`, `resolve`, `diff`, `rollback`, `gc`, `prune`, `status`, `history`,
`list`, `verify`, `profiles`, `export`, `env show` and `env validate` are the
main consumers.

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Invalid input, missing data, refused operation or host mismatch |
| `2` | Command-line usage error |
| `3` | Dependency constraints are unsatisfiable |
| `4` | Verification found drift |
| `130` | Interrupted (Ctrl-C) |

---

## Store and packages

### `axiom init`

Create the store layout. Idempotent.

```sh
axiom init
axiom --root /tmp/demo-store init
```

### `axiom build DIR [-o FILE] [--publish]`

Build a deterministic `.axp` artifact from a package directory. The default
output is `DIR/dist/<name>-<version>.axp`. With `--publish`, the artifact is
registered in the store immediately.

```sh
axiom build examples/packages/hello
axiom build examples/packages/hello -o /tmp/hello.axp
axiom build examples/packages/hello --publish
```

JSON output includes `identity`, `path`, `artifact_digest`, `content_digest`,
`files` and `size`.

### `axiom publish TARGET... [--digest SHA256]`

Register one or more `.axp` artifacts or package directories. `--digest` pins
the expected artifact digest (useful after downloading); a mismatch aborts
before anything is stored.

```sh
axiom publish /tmp/hello.axp
axiom publish ./downloaded.axp --digest 9f2c...
axiom publish examples/packages/hello examples/packages/libmath
```

Publishing a version that already exists with the same content is a no-op.
Publishing the same version with different content is refused: package versions
are immutable.

### `axiom search [QUERY]`

Search package names and descriptions in the store (case-insensitive substring).

```sh
axiom search
axiom search math --json
```

### `axiom info PACKAGE[@RANGE]`

Show metadata and the file manifest. Without a range, the highest registered
version is shown.

```sh
axiom info hello
axiom info 'hello@^1.0.0'
axiom info libmath@2.0.0 --json
```

---

## Environments

### `axiom env init PATH [--force]`

Write a commented environment scaffold. Refuses to overwrite without `--force`.

### `axiom env validate PATH [--host]`

Parse an environment, resolve includes and report counts. `--host` also checks
its `environment_constraints` against the current machine and exits `1` on
mismatch.

### `axiom env show PATH`

Print the merged environment (after includes). JSON with `--json`.

### `axiom resolve ENV [--prerelease]`

Resolve an environment and print the plan. Nothing is written. Host constraint
failures are printed as warnings, but resolution itself still succeeds; use
`install` to enforce them.

```sh
axiom resolve examples/environments/dev.toml
axiom resolve examples/environments/dev.toml --json
```

Unsatisfiable constraints exit `3` and print the conflicting requirements.

### `axiom lock ENV [-o FILE] [--prerelease]`

Resolve an environment and write a lockfile. The default path is
`axiom.lock.json` next to the environment file.

```sh
axiom lock examples/environments/dev.toml
axiom lock examples/environments/dev.toml -o lock/dev.lock.json
```

### `axiom install ENV` / `apply` / `sync`

Resolve, materialise and atomically activate an environment.

| Option | Meaning |
| --- | --- |
| `-p, --profile NAME` | Target profile. Precedence: `--profile`, then `$AXIOM_PROFILE`, then the environment's `name`. |
| `--lock` | Write/update the lockfile while installing. |
| `--locked` | Install exactly the lockfile, after verifying it still matches the environment. |
| `--lockfile PATH` | Lockfile location. Default: `axiom.lock.json` next to the environment. |
| `--dry-run` | Print the plan without writing anything. |
| `--force` | Create a generation even when the snapshot is already active. |
| `--verify-blobs` | Re-hash every blob while copying (slower, paranoid). |
| `--ignore-host` | Skip host constraint checks. |
| `--prerelease` | Allow pre-release versions for this resolution. |

```sh
axiom install examples/environments/dev.toml -p dev
axiom install examples/environments/dev.toml -p dev --lock
axiom install examples/environments/dev.toml -p dev --locked
axiom install examples/environments/dev.toml -p dev --dry-run
```

If the active snapshot already matches, the command reports "already up to
date" and creates no new generation.

### `axiom diff LEFT [RIGHT]`

- With two arguments, compares two snapshots. Each argument may be a generation
  reference in the active profile (`current`, `previous`, a sequence number, a
  generation name or a snapshot digest prefix), a lockfile/snapshot `.json`, or
  an environment `.toml` (resolved on the fly).
- With one environment `.toml`, compares the active profile with what that
  environment would install.

```sh
axiom -p dev diff examples/environments/dev.toml
axiom -p dev diff 1 2
axiom -p dev diff current previous --json
```

The JSON payload contains `from`, `to`, `identical`, `added_packages`,
`removed_packages`, `changed_packages`, `variables` and `files` (action +
target).

---

## Profiles

### `axiom list [--store]`

List packages in the active profile, or every registered package version with
`--store`.

### `axiom status`

Show the active generation, snapshot digest, environment, package/file counts,
size and number of generations.

### `axiom history`

Show generation history, newest first: sequence, generation, action
(`install`/`rollback`), environment, snapshot, timestamp. History is an audit
log; generation directories are the source of truth.

### `axiom rollback [REF]`

Atomically activate an earlier (or explicitly named) generation. With no `REF`,
the newest generation older than the current one is used. Rollback does not
copy files and does not create a generation.

```sh
axiom -p dev rollback
axiom -p dev rollback 000002-1a2b3c4d5e6f
axiom -p dev rollback previous
axiom -p dev rollback --dry-run
```

Use `axiom install` to go forward to a new snapshot after a rollback.

### `axiom prune [--keep N] [--dry-run]`

Remove the oldest generations beyond `N` (default 5, minimum 2). The active
generation is never removed. History is rewritten to forget pruned generations.

### `axiom profiles`

List profiles with their active generation and snapshot.

### `axiom run -- COMMAND [ARGS...]`

Run a command with `PATH` prefixed by `<profile>/current/files/bin`, the
profile's declared variables, and `AXIOM_PROFILE`, `AXIOM_GENERATION`,
`AXIOM_SNAPSHOT`. The child inherits the current environment otherwise. The
command's exit code is returned.

```sh
axiom -p dev run -- app axiom
axiom -p dev run -- sh -c 'echo $AXIOM_SNAPSHOT'
```

### `axiom shell`

Print `export` lines suitable for `eval`. PATH points through `current`, so it
follows later activations and rollbacks.

```sh
eval "$(axiom -p dev shell)"
```

### `axiom export [-o FILE]`

Write the active snapshot as a lockfile, or print it to stdout.

```sh
axiom -p dev export -o dev.lock.json
axiom -p dev export > dev.lock.json
```

---

## Maintenance

### `axiom verify [REF] [--store] [--shallow]`

Verify the active profile (or a generation reference) against its snapshot:
missing/extra files, sizes, modes and, unless `--shallow`, content digests.
Python bytecode caches (`__pycache__`, `*.pyc`) are ignored because running
Python from a profile creates them; `--strict` reports them too.
`--store` checks package entries, blob presence, sizes and metadata digests
instead.

Exits `4` when problems are found and prints them on stderr.

```sh
axiom -p dev verify
axiom -p dev verify 1 --shallow
axiom -p dev verify --strict
axiom verify --store --json
```

### `axiom gc [--dry-run] [--prune-generations N] [--artifacts]`

Remove blobs that are not referenced by any registered package entry or by a
generation in any profile. Because every registered package keeps its blobs,
this reclaims content that was never registered (for example interrupted
ingests). With `--prune-generations N`, also prune each profile. With
`--artifacts`, remove cached `.axp` files whose digest is not referenced by a
package entry.

```sh
axiom gc --dry-run
axiom gc --prune-generations 5
axiom gc --artifacts
```

---

## Recipes

### Pin an environment for CI

```sh
axiom install env.toml -p ci --lock
# commit axiom.lock.json
# CI, on a clean machine with the same store content:
axiom install env.toml -p ci --locked
axiom -p ci verify
```

### Upgrade a single dependency

Edit the constraint in `env.toml`, then:

```sh
axiom resolve env.toml          # inspect the new plan
axiom install env.toml -p dev --lock
axiom -p dev rollback           # if it was a mistake
```

### Test an upgrade without touching the live profile

```sh
axiom install env.toml -p staging --lock
axiom -p staging run -- smoke-tests
axiom install env.toml -p dev --locked   # promote later
```

### Publish from a build pipeline

```sh
axiom build ./pkg -o dist/pkg.axp
sha256sum dist/pkg.axp > dist/pkg.axp.sha256
# elsewhere:
axiom publish pkg.axp --digest "$(cat pkg.axp.sha256 | cut -d' ' -f1)"
```

### Inspect what changed between two installs

```sh
axiom -p dev diff 3 4
axiom -p dev diff 3 4 --json
```
