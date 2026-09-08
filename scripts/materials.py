"""Private, immutable application materials. No model calls.

Import: python scripts/materials.py import FILE --organisation NAME --kind cv
Search: python scripts/materials.py search WORDS
Submission status requires explicit evidence; imports default to unknown.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

ROOT = (
    Path(os.environ.get("JOBSEARCH_PRIVATE_DIR", Path(__file__).resolve().parents[1] / "private"))
    / "materials"
)


def load(root=ROOT):
    path = root / "index.json"
    return json.loads(path.read_text()) if path.exists() else []


def save(rows, root=ROOT):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False) as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
        name = f.name
    os.replace(name, root / "index.json")


def add(
    data, filename, organisation, kind, source, *, status="unknown", evidence="", date="", root=ROOT
):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if status not in {"unknown", "draft", "sent"}:
            raise ValueError("Invalid submission status")
        if status == "sent" and not evidence:
            raise ValueError("Sent requires submission evidence")
        digest = hashlib.sha256(data).hexdigest()
        identity = hashlib.sha256((source + digest).encode()).hexdigest()
        objects = root / "objects"
        objects.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = objects / digest
        if not target.exists():
            with target.open("xb") as f:
                f.write(data)
            target.chmod(0o600)
        if target.read_bytes() != data:
            raise ValueError(f"Stored original failed integrity check: {digest}")
        rows = load(root)
        previous = next((r for r in rows if r["id"] == identity), None)
        if previous:
            if status != "unknown":
                previous.update(status=status, evidence=evidence)
                save(rows, root)
            return previous
        text = (
            data.decode("utf-8", errors="replace")
            if Path(filename).suffix.lower() in {".md", ".txt", ".typ"}
            else ""
        )
        row = dict(
            id=identity,
            sha256=digest,
            filename=Path(filename).name,
            organisation=organisation,
            kind=kind,
            source=source,
            status=status,
            evidence=evidence,
            date=date,
            text=text,
        )
        rows.append(row)
        save(rows, root)
        return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("file", type=Path)
    imp.add_argument("--organisation", required=True)
    imp.add_argument(
        "--kind",
        choices=["cv", "cover_letter", "answers", "test", "evidence", "notes"],
        required=True,
    )
    imp.add_argument("--source")
    imp.add_argument("--status", choices=["unknown", "draft", "sent"], default="unknown")
    imp.add_argument("--evidence", default="")
    imp.add_argument("--date", default="")
    search = sub.add_parser("search")
    search.add_argument("query")
    args = parser.parse_args()
    if args.command == "import":
        row = add(
            args.file.read_bytes(),
            args.file.name,
            args.organisation,
            args.kind,
            args.source or str(args.file.resolve()),
            status=args.status,
            evidence=args.evidence,
            date=args.date,
        )
        print(row["id"])
    else:
        words = args.query.casefold().split()
        print(
            json.dumps(
                [
                    r
                    for r in load()
                    if all(w in json.dumps(r, ensure_ascii=False).casefold() for w in words)
                ],
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
