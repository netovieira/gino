#!/usr/bin/env bash
# Local privacy check. Read-only; never prints secret values.
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0

for path in \
  "config/api_keys.json" \
  "memory/continuity.json" \
  "memory/chat/placeholder" \
  "memory/voices/placeholder" \
  "models/placeholder" \
  ".qoder/placeholder" \
  "logs/placeholder" \
  "qoder_live/node_modules/placeholder"
do
  if git check-ignore -q -- "$path"; then
    echo "OK   ignore: $path"
  else
    echo "FAIL ignore: $path"
    fail=1
  fi
done

if [ -d memory/voices ]; then
  mode=$(stat -c '%a' memory/voices 2>/dev/null || echo '?')
  if [ "$mode" = "700" ]; then echo "OK   memory/voices mode 700"; else
    echo "FAIL memory/voices mode $mode (expected 700)"; fail=1
  fi
  if [ -f memory/voices/index.json ]; then
    mode=$(stat -c '%a' memory/voices/index.json 2>/dev/null || echo '?')
    if [ "$mode" = "600" ]; then echo "OK   memory/voices/index.json mode 600"; else
      echo "FAIL memory/voices/index.json mode $mode (expected 600)"; fail=1
    fi
  fi
fi

tmp=$(mktemp)
if git grep -I -l -E 'sk-fish-|gsk_[A-Za-z0-9]|PROXY_API_KEY=|BEGIN (OPENSSH|RSA|EC) PRIVATE KEY' -- . ':(exclude)config/api_keys.json' >"$tmp" 2>/dev/null; then
  echo "FAIL possible secret patterns in tracked files:"
  sed 's/^/  /' "$tmp"
  fail=1
else
  echo "OK   no obvious secret patterns in tracked files"
fi
rm -f "$tmp"

if [ "$fail" -eq 0 ]; then
  echo "PRIVACY_OK"
else
  echo "PRIVACY_FAIL"
fi
exit "$fail"
