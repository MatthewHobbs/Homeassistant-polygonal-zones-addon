# polygonal-zones-addon governance recipes (the cross-repo `just` convention).
#
# `just ci` mirrors the static gates in .github/workflows/{lint,test}.yml, including the
# same dependency install. NOT covered locally, and remaining remote-only gates: the HA
# add-on linter, the multi-arch build, the amd64 smoke boot and the Playwright UI tests,
# and lint.yml's ha-floor job, which reads version.home-assistant.io and so is
# network-dependent: `just ha-floor` runs it on demand, `just ci` never does.

# Local CI gate - the same commands remote CI runs, for the checks it covers.
ci: lint test

# ADR 0001 row 12 - the declared HA floor against the bound from current stable Core (network; not in `ci`).
ha-floor:
    scripts/ha-floor-check.sh

venv:
    #!/usr/bin/env bash
    set -euo pipefail
    uv venv --python 3.12 --quiet --allow-existing .venv
    uv pip install --python .venv --quiet -r polygonal_zones_editor/requirements.txt
    uv pip install --python .venv --quiet -r polygonal_zones_editor/requirements-dev.txt

lint:
    # Same scope as lint.yml's ruff-action: the repo root, so scripts/ is covered.
    ruff check
    ruff format --check
    shellcheck -x scripts/release-merge.sh scripts/supervisor-pilot.sh scripts/ha-floor-check.sh

test: venv
    cd polygonal_zones_editor && ../.venv/bin/python -m pytest -v
