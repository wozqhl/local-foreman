#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export LOCAL_FOREMAN_WORKER=mock
export LOCAL_FOREMAN_COACH=mock
export LOCAL_FOREMAN_CONFIRM=0
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export PYTHONPATH="src:${PYTHONPATH:-}"
python3 -m local_foreman --smoke
