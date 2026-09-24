"""Preconfigured, bounded subprocess fixture; never accepts task-supplied code."""
import json
import os
import signal
import sys
import time
from pathlib import Path


def publish(path, value):
    path = Path(path)
    staging = path.with_suffix(".writing")
    with staging.open("x", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    staging.replace(path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    mode = sys.argv[1]
    with open(os.environ["MPTASK_INPUT_PATH"], encoding="utf-8") as stream:
        task = json.load(stream)
    identity = {"schema_version": 1, "task_id": task["id"],
                "attempt_id": task["attempt"]["id"],
                "claim_generation": task["claim_generation"]}
    if mode == "crash":
        sys.exit(3)
    if mode in {"success", "remote", "stale", "empty"}:
        if mode == "remote":
            Path("remote-job-pending").write_text("not reconciled", encoding="utf-8")
        if mode == "stale":
            identity["claim_generation"] -= 1
        if mode != "empty":
            publish(os.environ["MPTASK_RESULT_PATH"], {
                **identity, "summary": "Fixture acceptance passed",
                "evidence": ["artifact:fixture-acceptance"]})
        sys.exit(0)
    if mode == "checkpoint":
        old = task["attempt"]["checkpoint"]
        publish(os.environ["MPTASK_CHECKPOINT_PATH"], {
            **identity, "sequence": old["sequence"] + 1 if old else 1,
            "reference": "artifact:checkpoint-" + task["attempt"]["id"]})
    if mode == "descendant":
        if os.fork():
            sys.exit(0)
    if mode in {"stubborn", "descendant", "unicode-stubborn"}:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode == "unicode-stubborn":
        Path("/proc/self/comm").write_bytes("wörker) name\n".encode("utf-8"))
    Path("worker-ready").write_text("ready", encoding="utf-8")
    for _ in range(600):
        if mode == "spam":
            print("still running", flush=True)
        time.sleep(0.05)
