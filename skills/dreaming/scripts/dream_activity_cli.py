#!/usr/bin/env python3
"""Read existing tool history into an offline, nonpublishing intent report."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from dream_activity import apply_reviews, artifact_review, build_activities, encode_json
from dream_activity_copilot import load_evidence


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True, help="Explicit local events.jsonl input")
    parser.add_argument("--session-id", required=True, help="Source session identifier")
    parser.add_argument("--out", required=True, help="New private JSON output file")
    parser.add_argument("--view", choices=("activities", "artifacts"), default="activities")
    parser.add_argument("--reviews", help="Explicit snapshot-bound inference reviews")
    parser.add_argument("--max-bytes", type=_positive, default=16 * 1024 * 1024)
    parser.add_argument("--max-events", type=_positive, default=20_000)
    parser.add_argument("--max-calls", type=_positive, default=2_000)
    parser.add_argument("--max-text-chars", type=_positive, default=2_000)
    parser.add_argument("--max-output-bytes", type=_positive, default=4 * 1024 * 1024)
    return parser.parse_args(argv)


def _read_reviews(path: str, max_bytes: int) -> list[dict]:
    with open(path, "rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("review input exceeds byte limit")
    try:
        reviews = json.loads(data)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("invalid review JSON") from exc
    if not isinstance(reviews, list):
        raise ValueError("review input must be a list")
    return reviews


def _publish(path: Path, encoded: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    identity = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # Remove only our incomplete file, never a concurrently replaced path.
        try:
            current = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                path.unlink()
        raise


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    stage = "output destination"
    try:
        output = Path(args.out)
        if output.exists() or output.is_symlink():
            raise FileExistsError("output already exists")
        stage = "source evidence"
        packet = load_evidence(
            args.events,
            args.session_id,
            max_bytes=args.max_bytes,
            max_events=args.max_events,
            max_calls=args.max_calls,
            max_text_chars=args.max_text_chars,
        )
        stage = "activity evidence"
        report = build_activities(packet)
        if args.reviews:
            stage = "inference review"
            report = apply_reviews(report, _read_reviews(args.reviews, args.max_bytes))
        if args.view == "artifacts":
            stage = "artifact review"
            report = artifact_review(report, max_output_bytes=args.max_output_bytes)
        stage = "output"
        encoded = encode_json(report, max_output_bytes=args.max_output_bytes)
        _publish(output, encoded)
    except FileExistsError:
        print("activity evidence: output already exists; choose a new --out path", file=sys.stderr)
        return 1
    except OSError as exc:
        detail = os.strerror(exc.errno) if exc.errno is not None else "IO error"
        print(f"activity evidence: {stage}: {detail}", file=sys.stderr)
        return 1
    except (ValueError, UnicodeError) as exc:
        print(f"activity evidence: {stage}: {str(exc)[:300]}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
