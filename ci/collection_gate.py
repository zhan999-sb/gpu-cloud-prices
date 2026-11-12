"""Allow documented partial provider failures, never an unknown/fatal failure."""

import json
from pathlib import Path
import sys


def classify(summary: dict, exit_code: int) -> tuple[bool, str]:
    if not isinstance(summary, dict) or "error" in summary:
        return False, "Collector reported a fatal error or an invalid summary."
    providers = summary.get("collections")
    if not isinstance(providers, dict) or not providers:
        return False, "No per-provider collection results; refusing deployment."
    successful, failed = [], []
    for name, result in providers.items():
        if not isinstance(result, dict) or result.get("status") not in {"complete", "failed"}:
            return False, "Unknown or limited provider status; full collection is required."
        records = result.get("records")
        if type(records) is not int or records < 0:
            return False, "Invalid provider record count."
        (successful if result["status"] == "complete" and records > 0 else failed).append(name)
    if not successful:
        return False, "All providers failed or returned no observations; deployment skipped."
    reported_failures = any(value["status"] == "failed" for value in providers.values())
    if exit_code != (1 if reported_failures else 0):
        return False, "Collector exit code disagrees with its provider results; deployment skipped."
    if failed:
        return True, "Partial collection: successful providers retained; stale/empty providers: " + ", ".join(sorted(failed))
    return True, "All reported providers completed with observations."


if __name__ == "__main__":
    try:
        allowed, message = classify(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")), int(sys.argv[2]))
    except (OSError, ValueError) as exc:
        allowed, message = False, f"Cannot validate collection summary: {exc}"
    print(("::notice::" if allowed else "::error::") + message)
    raise SystemExit(0 if allowed else 1)
