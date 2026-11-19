#!/usr/bin/env python3
"""Validate coworld_manifest_template.json against compose.yaml and coworld's upload + game-config validators.

Phase 40's `coworld build` rejects a template repo CI happily parsed
(collab-cooking, 2026-08-25): 0.1.42 wants `game.replay_viewer` rather than a
top-level one, no top-level `version`, no `game.display_name`, `game.owner`
present, and NO runner-managed `tokens` in the certification fixture. Running
the real validator here fails in repo CI instead of two phases later.

Usage: python3 tools/ci/check_manifest_loads.py [manifest]
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from coworld.manifest import validate_upload_manifest  # type: ignore
from coworld.manifest_validation import validate_coworld_manifest_game_configs  # type: ignore
from coworld.schema_validation import load_json_object  # type: ignore

ROLE_SECTIONS = ("player", "commissioner", "grader", "diagnoser", "optimizer")
REPO_COMPOSE = Path(__file__).resolve().parents[2] / "compose.yaml"


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "coworld_manifest_template.json")
    if not path.is_file():
        raise SystemExit(f"manifest not found: {path}")
    sibling_compose = path.resolve().parent / "compose.yaml"
    compose_path = sibling_compose if sibling_compose.is_file() else REPO_COMPOSE
    services = (yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}).get("services") or {}
    placeholders = {
        f"{{{{{name.upper().replace('-', '_')}_IMAGE}}}}": spec["image"]
        for name, spec in services.items()
        if isinstance(spec, dict) and isinstance(spec.get("image"), str)
    }
    document = load_json_object(path)
    game = document.get("game")
    if not isinstance(game, dict) or "version" in game:
        raise SystemExit(f"Coworld manifest templates must define `game` without `game.version`: {path}")
    game["version"] = "0.0.0"
    runnables = [
        game.get("runnable"),
        *(r for s in ROLE_SECTIONS for r in (document[s] if isinstance(document.get(s), list) else ())),
    ]
    for runnable in runnables:
        image = runnable.get("image") if isinstance(runnable, dict) else None
        if image in placeholders:
            runnable["image"] = placeholders[image]
        elif isinstance(image, str) and image.startswith("{{") and image.endswith("}}"):
            raise SystemExit(f"Coworld image placeholder does not match a Compose service: {image}")
    validated = validate_upload_manifest(document)
    if validated.runtime_manifest.tags is None:
        raise SystemExit("Coworld certification requires at least three manifest tags")
    validate_coworld_manifest_game_configs(validated.runtime_manifest)
    print(f"coworld validated the upload document: {type(validated).__name__}")


if __name__ == "__main__":
    main()
