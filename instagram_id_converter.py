#!/usr/bin/env python3
"""Convert Instagram usernames/handles/URLs into their numeric user IDs.

Instagram now requires a logged-in session to resolve profile data for most
requests. To supply one:
  1. Log into instagram.com in your browser.
  2. Open DevTools -> Application/Storage -> Cookies -> instagram.com.
  3. Copy the value of the 'sessionid' cookie.
  4. Pass it via --session-id <value>, or set it once with:
       export IG_SESSION_ID=<value>
Treat this value like a password: it grants full access to your account.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API_URL = "https://www.instagram.com/api/v1/users/web_profile_info/?username={}"
IG_APP_ID = "936619743392459"
DEFAULT_LIST_FILE = "instagram_accounts.txt"
REQUEST_DELAY_SECONDS = 2
SESSION_ID_ENV_VAR = "IG_SESSION_ID"


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


def get_user_id(username, session_id=None):
    username = normalize_username(username)
    if not username:
        raise ValueError("Username cannot be empty")

    headers = {
        "x-ig-app-id": IG_APP_ID,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": f"https://www.instagram.com/{username}/",
        "Accept": "*/*",
    }
    if session_id:
        headers["Cookie"] = f"sessionid={session_id}"

    request = urllib.request.Request(API_URL.format(username), headers=headers)

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
        if exc.code in (401, 403, 429) or "laser.provider" in detail:
            hint = (
                "Instagram now requires a logged-in session for this lookup. "
                f"Pass your session cookie via --session-id or the {SESSION_ID_ENV_VAR} "
                "env var (see script header for how to get it)."
                if not session_id
                else "Your session cookie may be expired or invalid — log into "
                "Instagram again in a browser and grab a fresh sessionid."
            )
            raise RuntimeError(
                f"Instagram blocked this request (HTTP {exc.code}: {detail}). {hint}"
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
    parser.add_argument(
        "--session-id",
        default=os.environ.get(SESSION_ID_ENV_VAR),
        help="Your Instagram 'sessionid' cookie value, required now that Instagram "
        f"blocks anonymous lookups. Defaults to the {SESSION_ID_ENV_VAR} env var. "
        "See the top of this script for how to get one.",
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
            results.append(get_user_id(username, session_id=args.session_id))
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
