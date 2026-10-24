# polygonal-zones-addon local recipes.
#
# `just ci` mirrors the static gates in .github/workflows/{lint,test}.yml, including the
# same dependency install. NOT covered locally, and remaining remote-only gates: the HA
# add-on linter, lint.yml's apparmor job (it needs apparmor_parser, which is Linux-only),
# the multi-arch build, the per-arch smoke boot and the Playwright UI tests, and lint.yml's
# ha-floor job, which reads version.home-assistant.io and so is network-dependent:
# `just ha-floor` runs it on demand, `just ci` never does.

# Local CI gate - the same commands remote CI runs, for the checks it covers.
ci: venv lint test comments

# One rule, measured: no code comment block over 50 words. Docstrings are docs.
comments:
    python3 scripts/check_comment_length.py polygonal_zones_editor scripts

# The declared HA floor against the bound from current stable Core (network; not in `ci`).
ha-floor:
    scripts/ha-floor-check.sh

venv:
    #!/usr/bin/env bash
    set -euo pipefail
    uv venv --python 3.14 --quiet --allow-existing .venv
    uv pip install --python .venv --quiet --require-hashes -r polygonal_zones_editor/requirements-dev-lock.txt
    uv pip install --python .venv --quiet --require-hashes -r polygonal_zones_editor/requirements-lock.txt
    uv pip check --python .venv

lint:
    # Same scope as lint.yml's ruff-action: the repo root, so scripts/ is covered.
    # The venv's ruff is the version lint.yml pins; run `just venv` first.
    .venv/bin/ruff check
    .venv/bin/ruff format --check
    # Same files as lint.yml's shellcheck job; keep the two lists identical.
    shellcheck -x scripts/release-merge.sh scripts/supervisor-pilot.sh scripts/ha-floor-check.sh scripts/test-release-merge.sh scripts/check-published-revision.sh scripts/test-check-published-revision.sh scripts/test-ha-floor-check.sh scripts/check-version-order.sh scripts/test-check-version-order.sh scripts/changelog-notes.sh scripts/test-changelog-notes.sh scripts/check-lock-pins.sh scripts/test-check-lock-pins.sh
    shellcheck -x polygonal_zones_editor/rootfs/etc/cont-init.d/00-fix-perms polygonal_zones_editor/rootfs/etc/services.d/web/run polygonal_zones_editor/rootfs/etc/services.d/web/finish

test: venv
    cd polygonal_zones_editor && ../.venv/bin/python -m pytest -v
    scripts/test-release-merge.sh
    scripts/test-check-published-revision.sh
    scripts/test-ha-floor-check.sh
    scripts/test-check-version-order.sh
    scripts/test-changelog-notes.sh
    scripts/test-check-lock-pins.sh
    scripts/check-lock-pins.sh polygonal_zones_editor/requirements-dev.txt polygonal_zones_editor/requirements-dev-lock.txt
    scripts/changelog-notes.sh "$(awk -F'"' '/^version:/{print $2}' polygonal_zones_editor/config.yaml)" >/dev/null
    cd polygonal_zones_editor && node --test tests/test_trackers.js tests/test_map.js
