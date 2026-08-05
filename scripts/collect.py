#!/usr/bin/env python3
"""Aggregate language usage across every repo I actually work in.

Sources, unioned and de-duplicated:
  1. Repos I own (public + private, forks excluded).
  2. Repos I'm an outside collaborator on.
  3. Repos in any org where I have commit contributions -- walked year by
     year since the account was created, so org repos I've never touched
     don't get counted just because I'm a member.

Needs a PAT with `repo` + `read:org`. The default Actions GITHUB_TOKEN
cannot see private or org repos and will silently return a public-only
answer, which is the whole thing this script exists to avoid.

Writes data/languages.json. Rendering lives in scripts/render.py.
"""

import datetime
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com/graphql"

# Languages to drop. GitHub measures raw file bytes, which makes markup and
# generated output wildly outweigh real source: one sandbox repo here holds
# 56 MB of HTML and 25 MB of .ipynb (notebooks store base64 plot output
# inline). Ranking by breadth already defuses most of that, but these still
# aren't things worth claiming as languages.
#
# Jupyter Notebook is the judgement call: it's 10 repos of genuine ML work,
# but engineers tend to read notebooks as exploratory rather than shipped.
# Drop it from this set if you'd rather show it.
EXCLUDE_LANGS = {
    "Jupyter Notebook",
    "HTML",
    "CSS",
    "SCSS",
    "Roff",
    "TeX",
    "Makefile",
    "CMake",
    "Dockerfile",
    "Batchfile",
    "Mako",
}

# Repos to drop, as "owner/name".
EXCLUDE_REPOS = set()

# A repo only counts toward a language if it holds at least this many bytes of
# it. Without a floor, a 40-byte stub counts the same as a real codebase, and
# breadth ranking becomes trivially gameable by accident.
MIN_BYTES_PER_REPO = 2048

REPO_FIELDS = """
    nameWithOwner
    isPrivate
    isFork
    isArchived
    pushedAt
    languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
      edges { size node { name color } }
    }
"""


def gql(query, variables, token):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "roest1-language-stats",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {e.reason}: {e.read().decode()[:400]}") from None
    if "errors" in data:
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return data["data"]


def viewer_repos(token):
    """Every repo I own or collaborate on, including private ones."""
    query = """
    query($cursor: String) {
      viewer {
        repositories(
          first: 50
          after: $cursor
          ownerAffiliations: [OWNER, COLLABORATOR]
          isFork: false
          orderBy: {field: PUSHED_AT, direction: DESC}
        ) {
          pageInfo { hasNextPage endCursor }
          nodes { %s }
        }
      }
    }
    """ % REPO_FIELDS

    repos, cursor = {}, None
    while True:
        page = gql(query, {"cursor": cursor}, token)["viewer"]["repositories"]
        for node in page["nodes"]:
            repos[node["nameWithOwner"]] = node
        if not page["pageInfo"]["hasNextPage"]:
            return repos
        cursor = page["pageInfo"]["endCursor"]


def contributed_repo_names(login, token, since_year):
    """Repos I've actually committed to, walked one year at a time.

    contributionsCollection only spans a year per query, so we page by year
    rather than trusting ORGANIZATION_MEMBER affiliation (which would sweep in
    every repo of every org I've ever joined).
    """
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          commitContributionsByRepository(maxRepositories: 100) {
            repository { nameWithOwner isFork }
          }
          pullRequestContributionsByRepository(maxRepositories: 100) {
            repository { nameWithOwner isFork }
          }
        }
      }
    }
    """
    names, this_year = set(), datetime.date.today().year
    for year in range(since_year, this_year + 1):
        data = gql(
            query,
            {
                "login": login,
                "from": f"{year}-01-01T00:00:00Z",
                "to": f"{year}-12-31T23:59:59Z",
            },
            token,
        )["user"]["contributionsCollection"]
        for key in ("commitContributionsByRepository", "pullRequestContributionsByRepository"):
            for entry in data[key]:
                repo = entry["repository"]
                if not repo["isFork"]:
                    names.add(repo["nameWithOwner"])
    return names


def fetch_repos(name_with_owners, token):
    """Look up repos by name in batches of 20 using GraphQL aliases."""
    out, names = {}, sorted(name_with_owners)
    for start in range(0, len(names), 20):
        batch = names[start : start + 20]
        parts = []
        for i, full in enumerate(batch):
            owner, _, name = full.partition("/")
            parts.append(
                f'r{i}: repository(owner: "{owner}", name: "{name}") {{ {REPO_FIELDS} }}'
            )
        data = gql("query { " + " ".join(parts) + " }", {}, token)
        for node in data.values():
            if node:
                out[node["nameWithOwner"]] = node
    return out


def aggregate(repos, login):
    langs = {}
    counted = []

    for full, repo in sorted(repos.items()):
        if full in EXCLUDE_REPOS:
            continue
        edges = repo["languages"]["edges"]
        if not edges:
            continue
        counted.append(
            {
                "repo": full,
                "private": repo["isPrivate"],
                "org": not full.startswith(f"{login}/"),
                "pushed": repo["pushedAt"],
            }
        )
        for edge in edges:
            name = edge["node"]["name"]
            if name in EXCLUDE_LANGS or edge["size"] < MIN_BYTES_PER_REPO:
                continue
            entry = langs.setdefault(
                name,
                {
                    "name": name,
                    "color": edge["node"]["color"] or "#8b949e",
                    "bytes": 0,
                    "repos": 0,
                    "private_repos": 0,
                    "org_repos": 0,
                    "last_push": "",
                },
            )
            entry["bytes"] += edge["size"]
            entry["repos"] += 1
            if repo["isPrivate"]:
                entry["private_repos"] += 1
            if not full.startswith(f"{login}/"):
                entry["org_repos"] += 1
            entry["last_push"] = max(entry["last_push"], repo["pushedAt"])

    # Breadth first, size only as a tie-breaker. Bytes alone would crown C#
    # (6.3 MB, one repo) over Python (2.9 MB, twenty repos) -- a ranking that
    # describes the largest repo rather than the person who wrote it.
    ranked = sorted(langs.values(), key=lambda lang: (-lang["repos"], -lang["bytes"]))
    return {
        "login": login,
        "languages": ranked,
        "repos_counted": len(counted),
        "sources": counted,
    }


def main():
    token = os.environ.get("GH_STATS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("set GH_STATS_TOKEN (needs `repo` + `read:org`)")

    login = gql("query { viewer { login createdAt } }", {}, token)["viewer"]
    created_year = int(login["createdAt"][:4])
    login = login["login"]

    owned = viewer_repos(token)
    print(f"owned/collaborator repos: {len(owned)}", file=sys.stderr)

    contributed = contributed_repo_names(login, token, created_year)
    missing = contributed - set(owned)
    print(f"contributed repos not already covered: {len(missing)}", file=sys.stderr)

    repos = dict(owned)
    repos.update(fetch_repos(missing, token))

    result = aggregate(repos, login)
    os.makedirs("data", exist_ok=True)
    with open("data/languages.json", "w") as f:
        json.dump(result, f, indent=2)

    print(
        f"{result['repos_counted']} repos "
        f"-> {len(result['languages'])} languages",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
