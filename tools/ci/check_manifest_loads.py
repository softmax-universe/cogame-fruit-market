#!/usr/bin/env python3
"""Validate coworld_manifest_template.json against compose.yaml and coworld.manifest.validate_upload_manifest.

Phase 40's `coworld build` rejects a template repo CI happily parsed
(collab-cooking, 2026-08-25): 0.1.42 wants `game.replay_viewer` rather than a
top-level one, no top-level `version`, no `game.display_name`, `game.owner`
present, and NO runner-managed `tokens` in the certification fixture. Running
the real validator here fails in repo CI instead of two phases later.

Usage: python3 tools/ci/check_manifest_loads.py [manifest]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from coworld.manifest import validate_upload_manifest  # type: ignore

ROLE_SECTIONS = ("player", "commissioner", "grader", "diagnoser", "optimizer")


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "coworld_manifest_template.json")
    if not path.is_file():
        raise SystemExit(f"manifest not found: {path}")
    compose_path = path.resolve().parent / "compose.yaml"
    services = (yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}).get("services") or {}
    placeholders = {f"{{{{{name.upper().replace('-', '_')}_IMAGE}}}}": spec["image"] for name, spec in services.items()}
    document = json.loads(path.read_text(encoding="utf-8"))
    game = document.get("game")
    if not isinstance(game, dict) or "version" in game:
        raise SystemExit(f"Coworld manifest templates must define `game` without `game.version`: {path}")
    game["version"] = "0.0.0"
    for runnable in [game.get("runnable"), *(r for s in ROLE_SECTIONS for r in document.get(s, ()))]:
        image = runnable.get("image") if isinstance(runnable, dict) else None
        if image in placeholders:
            runnable["image"] = placeholders[image]
        elif isinstance(image, str) and image.startswith("{{") and image.endswith("}}"):
            raise SystemExit(f"Coworld image placeholder does not match a Compose service: {image}")
    validated = validate_upload_manifest(document)
    print(f"coworld validated the upload document: {type(validated).__name__}")


if __name__ == "__main__":
    main()
