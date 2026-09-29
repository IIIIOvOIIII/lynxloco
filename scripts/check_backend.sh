#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../backend"
export MILOCO_CONFIG_SEARCH_PATH=/tmp/miloco-nonexistent-ci
# Synthetic TestClient credential; never loaded from a deployed service.
export MILOCO_SERVER__TOKEN=local-ci-service-token
# Timing contracts run without instrumentation: coverage overhead changes their clocks.
uv run --frozen pytest miloco/tests \
  --ignore=miloco/tests/e2e --ignore=miloco/tests/agent \
  --ignore=miloco/tests/perception/test_window_concurrency.py \
  --deselect=miloco/tests/perception/engine/identity/test_deep_sort_v12.py::TestV2ReIDModel::test_load_under_600ms \
  --cov=miloco/src/miloco --cov-branch \
  --cov-report=xml:../.tmp/coverage/backend.xml -q --tb=short
uv run --frozen pytest miloco/tests/perception/test_window_concurrency.py \
  miloco/tests/perception/engine/identity/test_deep_sort_v12.py::TestV2ReIDModel::test_load_under_600ms \
  -q --tb=short
