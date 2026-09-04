from __future__ import annotations

import argparse
from pathlib import Path


SCRIPT_TAG = '<script src="data.js"></script>'


def inline_report(html: Path, data: Path, output: Path) -> None:
    document = html.read_text()
    if document.count(SCRIPT_TAG) != 1:
        raise RuntimeError(f"Expected exactly one {SCRIPT_TAG!r} in {html}")
    standalone = document.replace(SCRIPT_TAG, f"<script>\n{data.read_text()}</script>")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(standalone)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inline report data into its HTML.")
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inline_report(args.html, args.data, args.output)


if __name__ == "__main__":
    main()
