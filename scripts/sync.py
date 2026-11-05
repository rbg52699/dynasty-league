"""Sync lock-in scoring data from Sleeper into data/ for the league website.

Needs a league member's Sleeper login token in the SLEEPER_TOKEN env var.
Runs in GitHub Actions on a schedule. For every season (following
previous_league_id) and every week it writes data/w/<league_id>/<week>.json with:
  - each team's starters, bench, locked games and Sleeper's official score
  - every NBA game that week (start time, status, teams)
  - fantasy points for each rostered player in each game, using that
    season's league scoring settings
Finished weeks are written once; the live week (and the one before it)
are refreshed each run.
"""
import json, os, sys, time, urllib.request, urllib.error

LEAGUE_ID = "1342346419675078656"
REST = "https://api.sleeper.app/v1"
GQL = "https://sleeper.com/graphql"
TOKEN = os.environ.get("SLEEPER_TOKEN", "").strip()
V = 3  # bump to force every week to be rebuilt


def _req(req, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError) as e:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def rest(path):
    return _req(urllib.request.Request(REST + path, headers={"User-Agent": "league-site-sync"}))


def gql(query):
    req = urllib.request.Request(
        GQL, data=json.dumps({"query": query}).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": TOKEN,
                 "Origin": "https://sleeper.com", "Referer": "https://sleeper.com/",
                 "User-Agent": "Mozilla/5.0 (league site sync)"})
    out = _req(req)
    if out.get("errors"):
        raise RuntimeError(json.dumps(out["errors"])[:400])
    return out["data"]


def load(path):
    try:
        return json.load(open(path))
    except Exception:
        return None


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    if os.path.exists(path) and open(path).read() == new:
        return False
    open(path, "w").write(new)
    return True


def fpts(stats, scoring):
    return round(sum((stats.get(k) or 0) * v for k, v in scoring.items()), 2)


def legs_for(lid, rnd):
    q = f'query q {{ matchup_legs(league_id: "{lid}", round: {rnd}) {{ roster_id matchup_id points proj_points max_points starters starters_games players }} }}'
    return gql(q)["matchup_legs"] or []


def games_for(season, week):
    q = f'query q {{ scores(sport: "nba", season_type: "regular", season: "{season}", week: {week}) {{ game_id date status start_time metadata }} }}'
    out = []
    for g in gql(q)["scores"] or []:
        md = g.get("metadata") or {}
        out.append([g["game_id"], g.get("start_time") or 0, g.get("status") or "",
                    (md.get("home_team") or {}).get("team") or "", (md.get("away_team") or {}).get("team") or "", g.get("date") or ""])
    return sorted(out, key=lambda x: (x[1], x[0]))


def game_points(season, gid, scoring, keep):
    q = f'query q {{ game_stats(sport: "nba", season_type: "regular", season: "{season}", game_id: "{gid}", category: "stat", order_by: "pts") {{ player_id stats }} }}'
    rows = gql(q)["game_stats"] or []
    return {r["player_id"]: fpts(r.get("stats") or {}, scoring) for r in rows if r["player_id"] in keep}


def sync_week(L, w, live, done):
    lid, season = L["league_id"], str(L["season"])
    path = f"data/w/{lid}/{w}.json"
    prev = load(path) or {}
    final = done or (live and w < live)
    if final and prev.get("final") and prev.get("v") == V:
        return 0, None
    legs = legs_for(lid, w)
    if not legs:
        return 0, None
    keep = set()
    for t in legs:
        keep.update(t.get("players") or [])
        keep.update(t.get("starters") or [])
    games = games_for(season, w)
    # a round can span more than one NBA week (e.g. the championship): pull in
    # following weeks until every locked game is covered
    locked = {g for t in legs for g in (t.get("starters_games") or {}).values()}
    for extra in (1, 2):
        have = {g[0] for g in games}
        if not (locked - have):
            break
        more = [g for g in games_for(season, w + extra) if g[0] not in have]
        if not more:
            break
        games = sorted(games + more, key=lambda x: (x[1], x[0]))
    old_status = {g[0]: g[2] for g in prev.get("games", [])}
    pts = {pid: list(v) for pid, v in (prev.get("pts") or {}).items()} if prev.get("v") == V else {}
    scoring = L.get("scoring_settings") or {}
    for gid, start, status, *_ in games:
        if status not in ("complete", "in_progress"):
            continue
        if status == "complete" and old_status.get(gid) == "complete" and prev.get("v") == V:
            continue
        try:
            gp = game_points(season, gid, scoring, keep)
        except Exception as e:
            print(f"  game {gid}: {e}")
            continue
        for pid, p in gp.items():
            lst = [x for x in pts.get(pid, []) if x[0] != gid]
            lst.append([gid, p])
            pts[pid] = lst
        time.sleep(0.08)
    start_of = {g[0]: g[1] for g in games}
    for pid in pts:
        pts[pid].sort(key=lambda x: start_of.get(x[0], 0))
    pts = {pid: v for pid, v in pts.items() if pid in keep}
    teams = [{"r": t["roster_id"], "m": t.get("matchup_id"), "off": t.get("points"),
              "proj": t.get("proj_points"), "max": t.get("max_points"),
              "s": t.get("starters") or [], "lk": t.get("starters_games") or {}, "pl": t.get("players") or []}
             for t in legs]
    # check our lock-in math against Sleeper's official score for finished weeks
    check = None
    if final:
        gpts = lambda pid, gid: next((p for g, p in pts.get(pid, []) if g == gid), 0)
        diffs = 0
        for t in teams:
            calc = 0
            for pid in t["s"]:
                if not pid or pid == "0":
                    continue
                if pid in t["lk"]:
                    calc += gpts(pid, t["lk"][pid])
                elif pts.get(pid):
                    calc += pts[pid][-1][1]
            if t["off"] is not None and abs(calc - t["off"]) > 0.01:
                diffs += 1
        check = diffs
    changed = write(path, {"v": V, "league_id": lid, "season": season, "week": w, "final": bool(final),
                           "live": bool(live and w == live), "teams": teams, "games": games, "pts": pts})
    return int(changed), check


def main():
    if not TOKEN:
        sys.exit("SLEEPER_TOKEN secret is not set; nothing to do.")
    state = rest("/state/nba")
    leagues, lid = [], LEAGUE_ID
    while lid and lid != "0" and len(leagues) < 20:
        L = rest(f"/league/{lid}")
        leagues.append(L)
        lid = L.get("previous_league_id")
    changed, index = 0, {}
    for L in leagues:
        lid, done = L["league_id"], L.get("status") == "complete"
        live = 0
        if not done and str(state.get("season")) == str(L.get("season")) and L.get("status") == "in_season":
            live = state.get("display_week") or state.get("week") or 0
        top = min(30, (L.get("settings") or {}).get("last_scored_leg") or 25) if done else live
        mism = 0
        for w in range(1, top + 1):
            try:
                c, chk = sync_week(L, w, live, done)
            except Exception as e:
                print(f"{L['season']} week {w}: {e}")
                continue
            changed += c
            if chk:
                mism += chk
                print(f"{L['season']} week {w}: {chk} team score(s) differ from Sleeper's official total")
        weeks = sorted(int(f[:-5]) for f in os.listdir(f"data/w/{lid}")) if os.path.isdir(f"data/w/{lid}") else []
        index[lid] = {"season": str(L["season"]), "weeks": weeks, "live": live}
        print(f"{L['season']}: {len(weeks)} weeks, {mism} mismatched team-weeks")
    # league details + player names/teams for the lock advisor skill
    cur = leagues[0]
    try:
        users = {u["user_id"]: u for u in rest(f"/league/{cur['league_id']}/users")}
        rosters = rest(f"/league/{cur['league_id']}/rosters")
        teams = {}
        for r in rosters:
            u = users.get(r.get("owner_id")) or {}
            teams[str(r["roster_id"])] = {"name": (u.get("metadata") or {}).get("team_name") or u.get("display_name") or f"Team {r['roster_id']}",
                                          "owner": u.get("display_name") or "", "players": r.get("players") or [],
                                          "reserve": r.get("reserve") or [], "taxi": r.get("taxi") or []}
        changed += write("data/league.json", {"league_id": cur["league_id"], "season": str(cur["season"]),
                                              "roster_positions": cur.get("roster_positions") or [],
                                              "live_week": index.get(cur["league_id"], {}).get("live", 0), "teams": teams})
        pl = load("data/players.json") or {}
        if time.time() - (pl.get("_t") or 0) > 3 * 3600:
            raw = rest("/players/nba")
            keep = {p for t in teams.values() for p in t["players"]}
            out = {"_t": int(time.time())}
            for pid, x in raw.items():
                if pid in keep or x.get("team"):
                    out[pid] = {"n": x.get("full_name") or f"{x.get('first_name','')} {x.get('last_name','')}".strip(),
                                "pos": x.get("fantasy_positions") or ([x["position"]] if x.get("position") else []),
                                "tm": x.get("team") or "FA", "inj": x.get("injury_status") or ""}
            changed += write("data/players.json", out)
    except Exception as e:
        print(f"league/players: {e}")
    # record when the login token expires (the expiry date only, never the token)
    exp = None
    try:
        import base64
        part = TOKEN.split(".")[1]; part += "=" * (-len(part) % 4)
        exp = json.loads(base64.urlsafe_b64decode(part)).get("exp")
    except Exception:
        pass
    index["_token_exp"] = exp
    old = load("data/index.json") or {}
    if changed or old.get("leagues") != index or old.get("v") != V:
        changed += write("data/index.json", {"v": V, "leagues": index, "updated": int(time.time() * 1000)})
    print(f"done, {changed} file(s) changed")


main()
