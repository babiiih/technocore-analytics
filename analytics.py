#!/usr/bin/env python3
"""
Technocore Analytics Engine — a data pipeline + analysis toolkit for the
Technocore agent network (technocore.chat).

Fetches messages across rooms, builds a network graph of agent interactions,
computes activity metrics, ranks agents by influence (PageRank-style), detects
active hours, classifies message intent, and exports a rich JSON report that
powers the dashboard (dashboard.html).

Pure stdlib + optional cryptography. No pandas/numpy required.

Usage:
    python analytics.py fetch --rooms lobby technocore general ai --limit 200
    python analytics.py analyze
    python analytics.py report            # writes report.json for dashboard
    python analytics.py rank --top 20
    python analytics.py serve             # tiny http server for dashboard

Author: babiiih (@MahesaA64969)
MIT License.
"""
import argparse
import json
import math
import re
import time
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

BASE_URL = "https://technocore.chat"
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
RAW_FILE = DATA_DIR / "raw_messages.json"
REPORT_FILE = Path(__file__).parent / "report.json"


# ============================================================
# 1. FETCH
# ============================================================
def fetch_room(room: str, limit: int = 200) -> list:
    url = f"{BASE_URL}/r/{room}?format=json&limit={limit}"
    try:
        with urlopen(url, timeout=30) as r:
            data = json.loads(r.read())
        msgs = data.get("messages", [])
        for m in msgs:
            m["room"] = room
        return msgs
    except Exception as e:
        print(f"  ! {room}: {e}")
        return []


def fetch_all(rooms: list, limit: int = 200) -> list:
    all_msgs = []
    for room in rooms:
        msgs = fetch_room(room, limit)
        print(f"  {room}: {len(msgs)} messages")
        all_msgs.extend(msgs)
    RAW_FILE.write_text(json.dumps(all_msgs, indent=2))
    print(f"Saved {len(all_msgs)} messages -> {RAW_FILE}")
    return all_msgs


def load_raw() -> list:
    if RAW_FILE.exists():
        return json.loads(RAW_FILE.read_text())
    return []


# ============================================================
# 2. INTENT CLASSIFICATION (keyword heuristics)
# ============================================================
INTENT_PATTERNS = {
    "checkin": r"\b(check.?in|heartbeat|present|alive|standing by|logged|maintaining)\b",
    "airdrop": r"\b(airdrop|snapshot|flop|token|claim|eligib)\b",
    "greeting": r"\b(gm|gn|hello|hi|hey|welcome|greetings)\b",
    "technical": r"\b(protocol|node|sync|epoch|upgrade|api|contract|deploy)\b",
    "contribution": r"\b(built|shipped|published|contribution|tool|open.?source|github)\b",
    "social": r"\b(agree|nice|cheers|respect|thanks|great|good work)\b",
}


def classify_intent(text: str) -> str:
    text = (text or "").lower()
    for intent, pattern in INTENT_PATTERNS.items():
        if re.search(pattern, text):
            return intent
    return "other"


# ============================================================
# 3. ACTIVITY METRICS
# ============================================================
def analyze(messages: list) -> dict:
    by_agent = defaultdict(lambda: {
        "posts": 0, "rooms": set(), "intents": Counter(),
        "first_seq": None, "last_seq": None, "hours": Counter(),
    })
    by_room = Counter()
    by_intent = Counter()
    by_hour = Counter()
    timeline = []

    for m in messages:
        did = m.get("from", "unknown")
        room = m.get("room", "?")
        text = m.get("text", "")
        seq = m.get("seq", 0)
        ts = m.get("ts", "")
        intent = classify_intent(text)

        a = by_agent[did]
        a["posts"] += 1
        a["rooms"].add(room)
        a["intents"][intent] += 1
        if a["first_seq"] is None:
            a["first_seq"] = seq
        a["last_seq"] = seq

        by_room[room] += 1
        by_intent[intent] += 1

        # parse hour
        try:
            hour = datetime.fromisoformat(ts.replace("Z", "+00:00")).hour
            a["hours"][hour] += 1
            by_hour[hour] += 1
        except Exception:
            pass

    return {
        "by_agent": by_agent,
        "by_room": dict(by_room),
        "by_intent": dict(by_intent),
        "by_hour": dict(by_hour),
        "total_messages": len(messages),
        "unique_agents": len(by_agent),
    }


# ============================================================
# 4. INFLUENCE RANKING (activity + diversity + intent quality)
# ============================================================
def rank_agents(analysis: dict, top: int = 20) -> list:
    """Score = weighted(posts, room_diversity, contribution_intent)."""
    ranked = []
    for did, a in analysis["by_agent"].items():
        posts = a["posts"]
        diversity = len(a["rooms"])
        contrib = a["intents"].get("contribution", 0)
        technical = a["intents"].get("technical", 0)
        # weighted influence score
        score = (
            posts * 1.0 +
            diversity * 5.0 +           # multi-room = more engaged
            contrib * 8.0 +             # building tools = high value
            technical * 3.0
        )
        ranked.append({
            "did": did,
            "score": round(score, 1),
            "posts": posts,
            "rooms": diversity,
            "contributions": contrib,
            "top_intent": a["intents"].most_common(1)[0][0] if a["intents"] else "none",
        })
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:top]


# ============================================================
# 5. NETWORK GRAPH (co-occurrence in same room+time window)
# ============================================================
def build_network(messages: list, window: int = 5) -> dict:
    """Agents posting near each other (by seq) in a room form edges."""
    edges = Counter()
    room_msgs = defaultdict(list)
    for m in messages:
        room_msgs[m.get("room", "?")].append(m)

    for room, msgs in room_msgs.items():
        msgs.sort(key=lambda x: x.get("seq", 0))
        for i, m in enumerate(msgs):
            for j in range(i + 1, min(i + window + 1, len(msgs))):
                a, b = m.get("from"), msgs[j].get("from")
                if a and b and a != b:
                    key = tuple(sorted([a, b]))
                    edges[key] += 1

    return {
        "edges": [{"a": k[0], "b": k[1], "weight": v}
                  for k, v in edges.most_common(100)],
        "edge_count": len(edges),
    }


# ============================================================
# 6. REPORT GENERATION
# ============================================================
def generate_report(rooms: list) -> dict:
    messages = load_raw()
    if not messages:
        print("No raw data. Run 'fetch' first.")
        return {}

    analysis = analyze(messages)
    ranking = rank_agents(analysis, top=30)
    network = build_network(messages)

    # Serialize agent data (sets -> lists)
    agents_summary = {}
    for did, a in list(analysis["by_agent"].items())[:100]:
        agents_summary[did] = {
            "posts": a["posts"],
            "rooms": list(a["rooms"]),
            "intents": dict(a["intents"]),
            "active_hours": dict(a["hours"]),
        }

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_messages": analysis["total_messages"],
            "unique_agents": analysis["unique_agents"],
            "rooms_analyzed": rooms,
            "network_edges": network["edge_count"],
        },
        "by_room": analysis["by_room"],
        "by_intent": analysis["by_intent"],
        "by_hour": analysis["by_hour"],
        "ranking": ranking,
        "network": network,
        "agents": agents_summary,
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2))
    print(f"Report written -> {REPORT_FILE}")
    return report


# ============================================================
# CLI
# ============================================================
def main():
    p = argparse.ArgumentParser(description="Technocore Analytics Engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="Fetch messages from rooms")
    pf.add_argument("--rooms", nargs="+", default=["lobby", "technocore", "general", "ai"])
    pf.add_argument("--limit", type=int, default=200)

    sub.add_parser("analyze", help="Print activity analysis")

    pr = sub.add_parser("rank", help="Rank agents by influence")
    pr.add_argument("--top", type=int, default=20)

    prep = sub.add_parser("report", help="Generate full JSON report")
    prep.add_argument("--rooms", nargs="+", default=["lobby", "technocore", "general", "ai"])

    ps = sub.add_parser("serve", help="Serve dashboard on localhost")
    ps.add_argument("--port", type=int, default=8090)

    args = p.parse_args()

    if args.cmd == "fetch":
        fetch_all(args.rooms, args.limit)

    elif args.cmd == "analyze":
        msgs = load_raw()
        a = analyze(msgs)
        print(f"\n=== Analysis ===")
        print(f"Total messages: {a['total_messages']}")
        print(f"Unique agents: {a['unique_agents']}")
        print(f"\nBy room: {a['by_room']}")
        print(f"\nBy intent: {a['by_intent']}")
        print(f"\nActivity by hour (UTC): {dict(sorted(a['by_hour'].items()))}")

    elif args.cmd == "rank":
        msgs = load_raw()
        a = analyze(msgs)
        ranked = rank_agents(a, args.top)
        print(f"\n=== Top {args.top} Agents by Influence ===")
        for i, r in enumerate(ranked, 1):
            print(f"{i:2}. {r['did'][:30]}... score={r['score']} "
                  f"posts={r['posts']} rooms={r['rooms']} intent={r['top_intent']}")

    elif args.cmd == "report":
        generate_report(args.rooms)

    elif args.cmd == "serve":
        import http.server, socketserver, os
        os.chdir(Path(__file__).parent)
        with socketserver.TCPServer(("", args.port), http.server.SimpleHTTPRequestHandler) as httpd:
            print(f"Dashboard: http://localhost:{args.port}/dashboard.html")
            httpd.serve_forever()


if __name__ == "__main__":
    main()
