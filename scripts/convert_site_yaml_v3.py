#!/usr/bin/env python3
"""Convert a v2 site.yaml to v3 (grouped by program, CONTRACTS 7.1, D-055).

    python3 ~/INSIGHTZZ/code/scripts/convert_site_yaml_v3.py --model-version 2026.10.1
    python3 ~/INSIGHTZZ/code/scripts/convert_site_yaml_v3.py path/to/site.yaml \
        --model-name dpc_maskrcnn --model-version 2026.10.1 --output path/to/site.v3.yaml

What it does:
- reads the v2 file (it must be chmod 600, like any site.yaml),
- moves every key as listed by the loader (secrets included, values are never printed),
- drops the model file keys (dir, weights, class_json, detectron_yaml, classes, detect_labels):
  they come from the model package now (CONTRACTS 12),
- writes a NEW file (default site.v3.yaml next to the input, chmod 600) and checks it.
The original file is not touched. Comments are not carried over. Review the new file, keep the
old one as a backup, then rename:  mv site.yaml site.v2.bak.yaml && mv site.v3.yaml site.yaml
"""

import argparse
import os
import sys

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from common.config import site_config as sc  # noqa: E402

HEADER = (
    "# DPC site config, schema v3 (CONTRACTS 7.1), converted from v2 by scripts/convert_site_yaml_v3.py.\n"
    "# Holds secrets: keep chmod 600, never commit. Comments of the old file were not carried over;\n"
    "# see config/examples/site.example.yaml for what each key means.\n\n"
)


def write_private(path: str, text: str) -> None:
    """Create the file with mode 600 from the start (no moment where others can read it).
    Refuses to overwrite an existing file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="convert_site_yaml_v3.py", description="Convert site.yaml v2 to v3.")
    parser.add_argument("path", nargs="?", help="v2 site.yaml (default: $DPC_SITE_CONFIG or ~/INSIGHTZZ/config/site.yaml)")
    parser.add_argument("--model-version", required=True,
                        help="version of the model package inspection will use, e.g. 2026.10.1 (D-056)")
    parser.add_argument("--model-name", help="model package name (default: the last folder of v2 model.dir)")
    parser.add_argument("--output", help="new file (default: site.v3.yaml next to the input)")
    args = parser.parse_args(argv)

    source = os.path.abspath(os.path.expanduser(args.path)) if args.path else sc.default_config_path()
    output = os.path.abspath(os.path.expanduser(args.output)) if args.output else \
        os.path.join(os.path.dirname(source), "site.v3.yaml")
    try:
        data = sc._read_yaml(source)          # same chmod 600 rule as the loader
    except sc.SiteConfigError as exc:
        print(exc)
        return 1
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        print(f"{source}: not a v2 file (schema_version is {data.get('schema_version') if isinstance(data, dict) else None})")
        return 1

    model_dir = (data.get("model") or {}).get("dir")
    model_name = args.model_name or (os.path.basename(os.path.normpath(model_dir)) if isinstance(model_dir, str) else None)
    if not model_name:
        print("give --model-name: the v2 file has no model.dir to take it from")
        return 1
    if os.path.lexists(output):
        print(f"{output} already exists; remove it or choose another --output")
        return 1

    moves = sc.v2_moves(data)
    converted = sc.convert_v2_to_v3(data, model_name, args.model_version)
    write_private(output, HEADER + yaml.safe_dump(converted, sort_keys=False, allow_unicode=True))

    print(f"Wrote {output} (chmod 600). Moved keys:")
    for old, new in moves:
        print(f"  {old} -> {new if new else 'removed (now in the model package)'}")
    print(f"  added inspection.model = {model_name}/{args.model_version}")
    try:
        sc.load_site_config(output)
    except sc.SiteConfigError as exc:
        print("The new file still has problems; fix them in it, then check again:")
        print(exc)
        return 1
    print("Check: OK. Review the file, then: mv site.yaml site.v2.bak.yaml && mv site.v3.yaml site.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
