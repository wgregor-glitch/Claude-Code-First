#!/usr/bin/env python3
"""Convert Instagram usernames/handles/URLs into their numeric user IDs.

Instagram's private web API endpoints break/change frequently, so this uses
instaloader (https://instaloader.github.io/) — an actively maintained
library that tracks those changes — instead of hand-rolled HTTP requests.

One-time setup:
  1. pip3 install -r requirements-instagram.txt
  2. Log in once from your own Instagram account (this saves a reusable
     session file under ~/.config/instaloader/ so you won't need to log in
     again, and handles 2FA/checkpoints interactively):
       instaloader --login=<your_own_instagram_username>
  3. Run this script with --login <your_own_instagram_username>

Logging in is required — Instagram now blocks most anonymous profile
lookups. Treat your Instaloader session file like a password.
"""

import argparse
import csv
import json
import re
import sys
import time

import instaloader

DEFAULT_LIST_FILE = "instagram_accounts.txt"
REQUEST_DELAY_SECONDS = 3


def normalize_username(value):
    """Accept a bare handle, an @handle, or a full profile URL."""
    value = value.strip()
    if not value or value.startswith("#"):
        return None
    match = re.search(r"instagram\.com/([^/?#]+)", value)
    if match:
        value = match.group(1)
    return value.lstrip("@").rstrip("/")


def load_usernames_from_file(path):
    with open(path, encoding="utf-8") as handle:
        return [u for line in handle if (u := normalize_username(line))]


def build_context(login_username):
    context = instaloader.Instaloader(quiet=True)
    if login_username:
        try:
            context.load_session_from_file(login_username)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"No saved Instaloader session for '{login_username}'. Run this once "
                f"first: instaloader --login={login_username}"
            ) from exc
    return context


def get_user_id(context, username):
    username = normalize_username(username)
    if not username:
        raise ValueError("Username cannot be empty")

    try:
        profile = instaloader.Profile.from_username(context.context, username)
    except instaloader.exceptions.ProfileNotExistsException as exc:
        raise ValueError(f"No Instagram account found for username '{username}'") from exc
    except instaloader.exceptions.LoginRequiredException as exc:
        raise RuntimeError(
            "Instagram requires a logged-in session for this lookup. Pass "
            "--login <your_own_instagram_username> (see script header for setup)."
        ) from exc
    except instaloader.exceptions.ConnectionException as exc:
        raise RuntimeError(
            f"Instagram blocked or rate-limited this request: {exc}. Wait a few "
            "minutes and try again, or increase --delay."
        ) from exc

    return {
        "username": profile.username,
        "user_id": profile.userid,
        "full_name": profile.full_name or None,
        "is_private": profile.is_private,
        "is_verified": profile.is_verified,
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
        "--login",
        default=None,
        help="Your own Instagram username, used to load a saved Instaloader session "
        "(create one first with: instaloader --login=<username>).",
    )
    parser.add_argument("--json", action="store_true", help="Print full result(s) as JSON")
    parser.add_argument("--csv", action="store_true", help="Print results as CSV")
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

    seen = set()
    targets = [t for t in targets if not (t.lower() in seen or seen.add(t.lower()))]

    if not targets:
        print("Error: no usernames provided", file=sys.stderr)
        sys.exit(1)

    try:
        context = build_context(args.login)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    results = []
    for i, username in enumerate(targets):
        try:
            results.append(get_user_id(context, username))
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
