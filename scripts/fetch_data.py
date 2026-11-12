#!/usr/bin/env python3
"""Refresh data/contributions.json (run daily by the GitHub Action).

Needs GH_TOKEN (or GITHUB_TOKEN). The default Actions token sees public
contributions only; set a PROFILE_TOKEN secret (read:user) to include private ones.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "cards.config.json").read_text(encoding="utf-8"))
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def gql(query, variables):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {TOKEN}", "Content-Type": "application/json",
                 "User-Agent": "profile-cards"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    if out.get("errors"):
        sys.exit(f"GraphQL error: {out['errors']}")
    return out["data"]


def window_start(now):
    """First day of the month 11 months ago: 12 calendar months, under GitHub's 1-year cap."""
    k = now.year * 12 + now.month - 1 - 11
    return datetime(k // 12, k % 12 + 1, 1, tzinfo=timezone.utc)


def main():
    if not TOKEN:
        sys.exit("Set GH_TOKEN or GITHUB_TOKEN.")
    login = CFG["username"]
    now = datetime.now(timezone.utc)
    names = [f if isinstance(f, str) else f["name"] for f in CFG["featured"]]
    repo_q = "\n".join(
        f'r{i}: repository(owner: $login, name: "{n}") '
        "{ name description url stargazerCount forkCount primaryLanguage { name color } }"
        for i, n in enumerate(names)
    )
    query = f"""query($login:String!,$from:DateTime!,$to:DateTime!){{
      user(login:$login){{ contributionsCollection(from:$from,to:$to){{
        contributionCalendar{{ weeks{{ contributionDays{{ date contributionCount }} }} }} }} }}
      {repo_q}
    }}"""
    d = gql(query, {"login": login, "from": window_start(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
    weeks = d["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
    days = [{"date": x["date"], "count": x["contributionCount"]}
            for w in weeks for x in w["contributionDays"]]
    days.sort(key=lambda x: x["date"])
    repos = []
    for i in range(len(names)):
        r = d.get(f"r{i}")
        if r:
            lang = r.get("primaryLanguage") or {}
            repos.append({"name": r["name"], "description": r.get("description") or "",
                          "url": r["url"], "stars": r["stargazerCount"], "forks": r["forkCount"],
                          "language": lang.get("name", ""), "languageColor": lang.get("color", "")})
    out = ROOT / CFG.get("data_file", "data/contributions.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated": now.strftime("%Y-%m-%d"), "username": login,
                               "days": days, "repos": repos}, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(days)} days, {len(repos)} repos)")


if __name__ == "__main__":
    main()
