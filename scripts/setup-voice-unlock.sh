#!/usr/bin/env bash
# Opt-in voice unlock setup. The password is read with no echo and stored in
# the desktop keyring (Secret Service). It is never written to this repo.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m core.unlock store
