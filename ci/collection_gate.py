"""Allow partial provider failures and fresh hourly skips; fail closed otherwise."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import sys


# The enabled A-tier APIs; display-only page successes cannot mask their outage.
A_TIER_APIS = frozenset({"azure", "linode", "scaleway", "verda", "clore"})


def _cached_status(result: dict) -> str:
    """An hourly budget is consumed by failures too, so a skip is not a success."""
    if (result.get("reason") != "page fetched within 60 minutes"
            or type(result.get("pages")) is not int or result["pages"] != 0
            or result["records"] != 0):
        return "invalid"
    previous = result.get("previous_run")
    if previous is None:
        return "failed"  # A request claim alone proves no successful observation.
    if (not isinstance(previous, dict)
            or previous.get("status") not in ("complete", "failed", "limited")
            or type(previous.get("records")) is not int or previous["records"] < 0):
        return "invalid"
    if previous["status"] != "complete":
        return previous["status"]
    try:
        checked = datetime.fromisoformat(result["checked_at"].replace("Z", "+00:00"))
        finished = datetime.fromisoformat(previous["finished_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError):
        return "invalid"
    if checked.utcoffset() != timedelta(0) or finished.utcoffset() != timedelta(0):
        return "invalid"
    if previous["records"] > 0 and timedelta(0) <= checked - finished < timedelta(hours=1):
        return "complete"
    return "failed"


def classify(summary: dict, exit_code: int) -> tuple[bool, str]:
    if not isinstance(summary, dict) or "error" in summary:
        return False, "Collector reported a fatal error or an invalid summary."
    providers = summary.get("collections")
    if not isinstance(providers, dict) or not providers:
        return False, "No per-provider collection results; refusing deployment."
    successful, cached, failed = [], [], []
    for name, result in providers.items():
        if not isinstance(result, dict) or result.get("status") not in ("complete", "failed", "skipped"):
            return False, "Unknown or limited provider status; full collection is required."
        records = result.get("records")
        if type(records) is not int or records < 0:
            return False, "Invalid provider record count."
        if result["status"] == "skipped":
            status = _cached_status(result)
            if status in ("invalid", "limited"):
                return False, "Unknown, invalid or limited cached provider result; full collection is required."
            (cached if status == "complete" else failed).append(name)
        else:
            (successful if result["status"] == "complete" and records > 0 else failed).append(name)
    if not successful and not cached:
        return False, "All providers failed or returned no observations; deployment skipped."
    reported_failures = any(value["status"] == "failed" for value in providers.values())
    if exit_code != (1 if reported_failures else 0):
        return False, "Collector exit code disagrees with its provider results; deployment skipped."
    if failed and not A_TIER_APIS.intersection(successful + cached):
        return False, "No successful A-tier API provider available for a partial collection; deployment skipped."
    retained = (" Hourly throttled providers with fresh successful data retained: " + ", ".join(sorted(cached))) if cached else ""
    if failed:
        return True, "Partial collection: successful providers retained; failed/stale/empty providers: " + ", ".join(sorted(failed)) + retained
    if not successful:
        return True, "All providers hourly throttled with fresh successful data; no new observations. Refresh policy decides whether a build is needed."
    return True, "All reported providers completed with observations." + retained


if __name__ == "__main__":
    try:
        allowed, message = classify(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")), int(sys.argv[2]))
    except (OSError, ValueError) as exc:
        allowed, message = False, f"Cannot validate collection summary: {exc}"
    print(("::notice::" if allowed else "::error::") + message)
    raise SystemExit(0 if allowed else 1)
