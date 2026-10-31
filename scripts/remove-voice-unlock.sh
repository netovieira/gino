#!/usr/bin/env bash
# Remove the credential stored for voice unlock.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m core.unlock delete
