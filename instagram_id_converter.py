#!/usr/bin/env python3
"""Convert Instagram usernames/handles/URLs into their numeric user IDs."""

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request

API_URL = "https://www.instagram.com/api/v1/users/web_profile_info/?username={}"
IG_APP_ID = "936619743392459"
DEFAULT_LIST_FILE = "instagram_accounts.txt"
REQUEST_DELAY_SECONDS = 2


def normalize_username(value):
    """Accept a bare handle, an @handle, or a full profile URL."""
    value = value.strip()
    if not value or value.startswith("#"):
        return None
    match = re.search(r"instagram\.com/([^/?#]+)", value)
    if match:
        value = match.group(1)
    return value.lstrip("@")


def load_usernames_from_file(path):
    with open(path, encoding="utf-8") as handle:
        return [u for line in handle if (u := normalize_username(line))]


def get_user_id(username):
    username = normalize_username(username)
    if not username:
        raise ValueError("Username cannot be empty")

    request = urllib.request.Request(
        API_URL.format(username),
        headers={
            "x-ig-app-id": IG_APP_ID,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": f"https://www.instagram.com/{username}/",
            "Accept": "*/*",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("message", body)
        except json.JSONDecodeError:
            detail = body
        detail = detail.strip()[:200]

        if exc.code == 404:
            raise ValueError(f"No Instagram account found for username '{username}'") from exc
        if exc.code in (401, 403, 429):
            raise RuntimeError(
                "Instagram blocked or rate-limited this request "
                f"(HTTP {exc.code}: {detail}). This happens when too many requests "
                "come from the same IP/network in a short time, or from datacenter "
                "IPs Instagram flags as bots. Wait a few minutes and try again "
                "from a regular home/mobile network, or increase --delay."
            ) from exc
        raise RuntimeError(f"Instagram returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error while contacting Instagram: {exc.reason}") from exc

    user = data.get("data", {}).get("user")
    if not user:
        raise ValueError(f"No Instagram account found for username '{username}'")

    return {
        "username": user.get("username", username),
        "user_id": user.get("id"),
        "full_name": user.get("full_name") or None,
        "is_private": user.get("is_private"),
        "is_verified": user.get("is_verified"),
        "error": None,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Convert Instagram usernames/URLs to their numeric user IDs."
    )
    parser.add_argument(
        "usernames",
        nargs="*",
        help="Instagram username(s), @handle(s), or profile URL(s). "
        f"If omitted, reads from --file (default: {DEFAULT_LIST_FILE}).",
    )
    parser.add_argument(
        "--file",
        default=None,
        help=f"Path to a file with one username/URL per line (default: {DEFAULT_LIST_FILE} "
        "when no usernames are given on the command line).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print full result(s) as JSON"
    )
    parser.add_argument(
        "--csv", action="store_true", help="Print results as CSV"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY_SECONDS,
        help=f"Seconds to wait between requests when looking up multiple accounts "
        f"(default: {REQUEST_DELAY_SECONDS})",
    )
    args = parser.parse_args()

    if args.usernames:
        targets = [u for raw in args.usernames if (u := normalize_username(raw))]
    else:
        targets = load_usernames_from_file(args.file or DEFAULT_LIST_FILE)

    # de-duplicate while preserving order
    seen = set()
    targets = [t for t in targets if not (t.lower() in seen or seen.add(t.lower()))]

    if not targets:
        print("Error: no usernames provided", file=sys.stderr)
        sys.exit(1)

    results = []
    for i, username in enumerate(targets):
        try:
            results.append(get_user_id(username))
        except (ValueError, RuntimeError) as exc:
            results.append({"username": username, "user_id": None, "error": str(exc)})
        if i < len(targets) - 1:
            time.sleep(args.delay)

    if args.json:
        print(json.dumps(results if len(results) > 1 else results[0], indent=2))
    elif args.csv:
        writer = csv.DictWriter(
            sys.stdout,
            fieldnames=["username", "user_id", "full_name", "is_private", "is_verified", "error"],
        )
        writer.writeheader()
        writer.writerows(results)
    else:
        for result in results:
            if result.get("error"):
                print(f"{result['username']}: ERROR - {result['error']}", file=sys.stderr)
            else:
                print(f"{result['username']}: {result['user_id']}")

    if any(r.get("error") for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
