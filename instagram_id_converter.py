#!/usr/bin/env python3
"""Convert an Instagram username/handle into its numeric user ID."""

import argparse
import json
import sys
import urllib.error
import urllib.request

API_URL = "https://www.instagram.com/api/v1/users/web_profile_info/?username={}"
IG_APP_ID = "936619743392459"


def get_user_id(username):
    username = username.strip().lstrip("@")
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
        if exc.code == 404:
            raise ValueError(f"No Instagram account found for username '{username}'") from exc
        if exc.code in (401, 403, 429):
            raise RuntimeError(
                "Instagram blocked or rate-limited this request "
                f"(HTTP {exc.code}). This happens when too many requests come "
                "from the same IP/network in a short time, or from datacenter "
                "IPs Instagram flags as bots. Wait a few minutes and try again "
                "from a regular home/mobile network."
            ) from exc
        raise RuntimeError(f"Instagram returned HTTP {exc.code}") from exc
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
    }


def main():
    parser = argparse.ArgumentParser(
        description="Convert an Instagram username to its numeric user ID."
    )
    parser.add_argument("username", help="Instagram username/handle (with or without @)")
    parser.add_argument(
        "--json", action="store_true", help="Print full result as JSON instead of just the ID"
    )
    args = parser.parse_args()

    try:
        result = get_user_id(args.username)
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result["user_id"])


if __name__ == "__main__":
    main()
