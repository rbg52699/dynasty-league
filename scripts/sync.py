"""Pull lock-in matchup data from Sleeper (needs a league member's login token)
and save it under data/ so the website can read it.

Runs in GitHub Actions on a schedule. Finished weeks are fetched once and kept;
the current and previous week are refreshed every run."""
import json, os, sys, time, urllib.request

LEAGUE_ID = "1342346419675078656"
REST = "https://api.sleeper.app/v1"
GQL = "https://sleeper.com/graphql"
TOKEN = os.environ.get("SLEEPER_TOKEN", "").strip()
FIELDS = "roster_id matchup_id round leg points custom_points proj_points max_points starters starters_games player_map players subs"

def get(path):
    with urllib.request.urlopen(REST + path, timeout=30) as r:
        return json.load(r)

def gql(query):
    req = urllib.request.Request(GQL, data=json.dumps({"query": query}).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": TOKEN,
                 "Origin": "https://sleeper.com", "Referer": "https://sleeper.com/",
                 "User-Agent": "Mozilla/5.0 (league site sync)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    if out.get("errors"):
        raise RuntimeError(json.dumps(out["errors"])[:500])
    return out["data"]

def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    if os.path.exists(path) and open(path).read() == new:
        return False
    open(path, "w").write(new)
    return True

def main():
    if not TOKEN:
        sys.exit("SLEEPER_TOKEN secret is not set; nothing to do.")
    state = get("/state/nba")
    changed, leagues, lid = 0, [], LEAGUE_ID
    while lid and lid != "0" and len(leagues) < 20:
        L = get(f"/league/{lid}"); leagues.append(L); lid = L.get("previous_league_id")
    for L in leagues:
        lid, done = L["league_id"], L.get("status") == "complete"
        live = 0
        if not done and str(state.get("season")) == str(L.get("season")) and L.get("status") == "in_season":
            live = state.get("display_week") or state.get("week") or 0
        last = (L.get("settings") or {}).get("last_scored_leg") or 25
        top = min(30, last) if done else live
        for rnd in range(1, top + 1):
            path = f"data/legs/{lid}/{rnd}.json"
            final = done or rnd < live - 1
            if final and os.path.exists(path):
                continue
            try:
                legs = gql(f'query q {{ matchup_legs(league_id: "{lid}", round: {rnd}) {{ {FIELDS} }} }}')["matchup_legs"]
            except Exception as e:
                print(f"{lid} round {rnd}: {e}"); continue
            if legs:
                changed += write(path, {"league_id": lid, "round": rnd, "final": final, "legs": legs})
            time.sleep(0.3)
    index = {"updated": int(time.time() * 1000), "seasons": {
        L["league_id"]: sorted(int(f[:-5]) for f in os.listdir(f"data/legs/{L['league_id']}")) if os.path.isdir(f"data/legs/{L['league_id']}") else []
        for L in leagues}}
    write("data/index.json", index)
    print(f"done, {changed} file(s) changed")

main()
