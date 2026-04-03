#!/usr/bin/env python3
import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

FIELDS = ["input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "total_tokens"]
BOLD = "[1m"
RESET = "[0m"


def parse_last(value):
    suffix = value[-1].lower()
    amount = int(value[:-1])
    if suffix == "h":
        return timedelta(hours=amount)
    if suffix == "d":
        return timedelta(days=amount)
    if suffix == "w":
        return timedelta(weeks=amount)
    raise argparse.ArgumentTypeError(f"Invalid suffix, use h/d/w")


def get_usage_stats(log_file="usage-log.jsonl", start_date=None, end_date=None):
    """Return usage stats keyed by model name.

    Each value is a dict with the five token fields plus ``request_count``.
    Date filtering: inclusive of *start_date*, exclusive of *end_date*.
    """
    totals = defaultdict(lambda: defaultdict(int))
    counts = defaultdict(int)
    with open(log_file) as f:
        for line in f:
            record = json.loads(line)
            if "model" not in record:
                continue
            if start_date or end_date:
                ts = datetime.fromisoformat(record["timestamp"])
                if start_date and ts < start_date:
                    continue
                if end_date and ts >= end_date:
                    continue
            model = record["model"]
            counts[model] += 1
            for field in FIELDS:
                totals[model][field] += record.get(field, 0)
    return {
        model: {**dict(totals[model]), "request_count": counts[model]}
        for model in totals
    }


def main():
    parser = argparse.ArgumentParser(description="Summarize usage-log.jsonl by model")
    parser.add_argument("--log", default="usage-log.jsonl", help="Path to JSONL log file")
    parser.add_argument("--last", type=parse_last, default=None, help="Time window, e.g. 24h, 7d, 2w")
    parser.add_argument("--no-comma", action="store_true", help="Disable thousand separator")
    args = parser.parse_args()
    start_date = datetime.now(timezone.utc) - args.last if args.last else None
    stats = get_usage_stats(log_file=args.log, start_date=start_date)
    for model in sorted(stats):
        print(f"{BOLD}{model}{RESET}  ({stats[model]['request_count']} requests)")
        for field in FIELDS:
            num = stats[model][field] if args.no_comma else f"{stats[model][field]:,}"
            print(f"  {field}: {num}")


if __name__ == "__main__":
    main()
