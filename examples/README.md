# axiom examples

The packages here are deliberately tiny fixtures: a greeting script, a math
library and an app that depends on both.

```sh
# from the repository root, with src/ on PYTHONPATH (or after `pip install -e .`)
export AXIOM_ROOT="$PWD/.axiom-demo"

for pkg in hello libmath app legacy; do
  python -m axiom build "examples/packages/$pkg" --publish
done

python -m axiom resolve examples/environments/dev.toml
python -m axiom install examples/environments/dev.toml --lock -p dev
python -m axiom -p dev list
python -m axiom -p dev run -- app axiom
python -m axiom -p dev verify
```

`legacy` is published but not installed. It conflicts with `app`, so adding it
to `dev.toml` produces an unsatisfiable environment with an explanation rather
than a broken profile.
