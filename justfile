# polygonal-zones-addon governance recipes (the cross-repo `just` convention).
#
# `just ci` mirrors the static gates in .github/workflows/{lint,test}.yml, including the
# same dependency install. NOT covered locally, and remaining remote-only gates: the HA
# add-on linter, the multi-arch build, the amd64 smoke boot and the Playwright UI tests.

# Local CI gate - the same commands remote CI runs, for the checks it covers.
ci: lint test

venv:
    #!/usr/bin/env bash
    set -euo pipefail
    uv venv --python 3.12 --quiet --allow-existing .venv
    uv pip install --python .venv --quiet -r polygonal_zones_editor/requirements.txt
    uv pip install --python .venv --quiet -r polygonal_zones_editor/requirements-dev.txt

lint:
    ruff check polygonal_zones_editor
    shellcheck -x scripts/release-merge.sh

test: venv
    cd polygonal_zones_editor && ../.venv/bin/python -m pytest -v
