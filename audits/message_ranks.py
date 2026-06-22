#!/usr/bin/env python3

import argparse
import json
from collections import Counter
from pathlib import Path


def iter_jsonl_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []

    for path in paths:
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.glob("*.jsonl")))
            files.extend(sorted(path.glob("*.json")))
        else:
            print(f"Warning: skipping missing path: {path}")

    return files


def analyse_chat_logs(files: list[Path]) -> Counter[str]:
    counts: Counter[str] = Counter()

    for file_path in files:
        with file_path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                line = line.strip()

                if not line:
                    continue

                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"Warning: invalid JSON in {file_path}:{line_number}: {e}")
                    continue

                user_display_name = entry.get("user_display_name")
                user_name = entry.get("user_name")
                user_id = entry.get("user_id")

                name = user_display_name or user_name or user_id or "<unknown user>"
                counts[name] += 1

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyse JSONL Twitch/chat logs and show who sent the most messages."
    )

    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="One or more JSONL files or directories containing JSONL files.",
    )

    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Only show the top N users.",
    )

    args = parser.parse_args()

    files = iter_jsonl_files(args.paths)

    if not files:
        print("No JSONL files found.")
        return

    counts = analyse_chat_logs(files)

    if not counts:
        print("No chat messages found.")
        return

    print("Messages by user:")
    print("-" * 40)

    results = counts.most_common(args.top)

    for index, (name, count) in enumerate(results, start=1):
        print(f"{index:>3}. {name:<25} {count:>6}")


if __name__ == "__main__":
    main()
