#!/usr/bin/env bash
# Quick rollback: turn the Qoder Live stack and the new voice actions off.
# The local brain continues to answer after the app is restarted.
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python - <<'PY'
import json
from pathlib import Path
path = Path("config/api_keys.json")
data = json.loads(path.read_text(encoding="utf-8"))
for key in ("qoder_live_enabled", "voice_id_enabled", "restart_enabled",
            "power_enabled", "memory_librarian_enabled"):
    data[key] = False
path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
print("Flags desligadas: qoder_live, voice_id, restart, power, memory_librarian.")
print("Reinicie o app para voltar ao cérebro local. Nenhum arquivo de memória foi apagado.")
PY
