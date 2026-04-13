#!/usr/bin/env python3
"""
Sidecar Generator - Builds the zero-dependency sidecar.py from registry.json.
"""

import json
import os
from pathlib import Path


def generate():
    # Root relative paths
    root = Path(__file__).parent.parent
    registry_path = root / "app/core/registry.json"
    template_path = root / "scripts/sidecar_template.py"
    output_path = root / "scripts/sidecar.py"

    if not registry_path.exists():
        print(f"Error: {registry_path} not found")
        return

    if not template_path.exists():
        print(f"Error: {template_path} not found")
        return

    with open(registry_path) as f:
        registry = json.load(f)

    with open(template_path) as f:
        template = f.read()

    # Convert registry to a pretty-printed python dict string
    registry_str = json.dumps(registry, indent=4)

    # Inject registry into template
    output = template.replace("__REGISTRY__ = {}", f"__REGISTRY__ = {registry_str}")

    with open(output_path, "w") as f:
        f.write(output)

    os.chmod(output_path, 0o755)
    print(f"Successfully generated {output_path} from registry.")


if __name__ == "__main__":
    generate()
