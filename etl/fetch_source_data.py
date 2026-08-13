"""
Fetch site/data.json and site/history.json from the private
inospace-za/sitelink-analytics-dashboard repo via the GitHub Contents API.

This deliberately does NOT touch Cloudflare Access or the published Pages site -
it reads the committed files straight out of the git repo, authenticated with a
fine-grained, read-only PAT (see README.md "Auth").

Usage:
    export SOURCE_REPO_PAT=ghp_xxx
    python etl/fetch_source_data.py
"""
import base64
import json
import os
import sys
import urllib.request
import urllib.error

SOURCE_OWNER = "inospace-za"
SOURCE_REPO = "sitelink-analytics-dashboard"
SOURCE_BRANCH = os.environ.get("SOURCE_REPO_BRANCH", "main")
FILES_TO_FETCH = ["site/data.json", "site/history.json"]
OUT_DIR = "_incoming"


def fetch_file(path: str, token: str) -> dict:
    url = (
        f"https://api.github.com/repos/{SOURCE_OWNER}/{SOURCE_REPO}/contents/"
        f"{path}?ref={SOURCE_BRANCH}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "inospace-leads-dashboard-etl",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"GitHub Contents API error fetching {path}: {e.code} {e.reason}\n{body}\n"
            "Check SOURCE_REPO_PAT is set, valid, not expired, and has Contents:Read "
            f"on {SOURCE_OWNER}/{SOURCE_REPO}."
        )
    if payload.get("encoding") != "base64":
        raise SystemExit(f"Unexpected encoding for {path}: {payload.get('encoding')}")
    content = base64.b64decode(payload["content"]).decode("utf-8")
    return json.loads(content)


def main():
    token = os.environ.get("SOURCE_REPO_PAT")
    if not token:
        print("SOURCE_REPO_PAT is not set - see README.md 'Auth'.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(OUT_DIR, exist_ok=True)
    for path in FILES_TO_FETCH:
        data = fetch_file(path, token)
        out_name = os.path.basename(path)
        out_path = os.path.join(OUT_DIR, out_name)
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Fetched {path} -> {out_path} ({len(json.dumps(data))} bytes)")


if __name__ == "__main__":
    main()
