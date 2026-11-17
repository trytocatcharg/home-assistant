#!/bin/bash
# Genera la linea DEVICES_JSON lista para pegar en el .env de Docker,
# a partir del devices.json generado por el wizard de tinytuya.
# Solo incluye los campos que necesita el bot (name, id, key, ip, version).
cd "$(dirname "$0")"
./venv/bin/python - <<'EOF'
import json

raw = json.load(open("devices.json"))
slim = [
    {
        "name": d["name"],
        "id": d["id"],
        "key": d["key"],
        "ip": d.get("ip") or d.get("address"),
        "version": d.get("version", "3.3"),
    }
    for d in raw
]
print("DEVICES_JSON=" + repr(json.dumps(slim, ensure_ascii=False)))
EOF
