"""Import a verified local NR runtime, list its manifest, or restore a prior selection."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from component_store import ComponentStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=ROOT / "app")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    add = sub.add_parser("import")
    add.add_argument("path", type=Path)
    add.add_argument("--profile", choices=("rtx30", "rtx40", "rtx50"), required=True)
    add.add_argument("--sha256", required=True)
    add.add_argument("--version", required=True)
    add.add_argument("--source", required=True)
    undo = sub.add_parser("rollback")
    undo.add_argument("revision")
    args = parser.parse_args()
    store = ComponentStore(args.app)
    value = store.read() if args.action == "list" else store.rollback(args.revision) if args.action == "rollback" else store.import_dll(args.path, args.profile, args.sha256, args.version, args.source)
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
