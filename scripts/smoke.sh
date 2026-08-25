#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export LOCAL_FOREMAN_WORKER=mock
export LOCAL_FOREMAN_COACH=mock
export PYTHONPATH="src:${PYTHONPATH:-}"
python3 -m local_foreman --smoke
