"""
FairSeat - sealed-bid, uniform-price ticket auction with a verifiable ledger.
NEOMA MSc Fintech & DeFi - Final group project (MVP)
Team: Charles Bories, Gabriel Rodrigues, Thijmen Delwel, Julie Ceccaldi

Run:   streamlit run fairseat_app.py
Demo:  open the app in several browser tabs; each tab can act as a different fan.
       All tabs share the same ledger file (fairseat_ledger.json, next to this script).
"""

import hashlib
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

LEDGER_PATH = Path(__file__).with_name("fairseat_ledger.json")
GENESIS_HASH = "0" * 64
PHASES = ["SETUP", "COMMIT", "REVEAL", "CLOSED"]


# --------------------------------------------------------------------------
# 1. Crypto helpers
# --------------------------------------------------------------------------
def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def commit_hash(event_id: str, fan_id: str, bid: float, nonce: str) -> str:
    """The commitment a fan publishes. Bid is normalised to 2 decimals."""
    return sha256(f"{event_id}|{fan_id}|{bid:.2f}|{nonce}")


def block_hash(block: dict) -> str:
    """Hash of a block's content + the previous block's hash (the chain link)."""
    body = {k: block[k] for k in ("index", "timestamp", "type", "data", "prev_hash")}
    return sha256(json.dumps(body, sort_keys=True, separators=(",", ":")))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------
# 2. Append-only, hash-chained ledger (stored as a JSON file)
# --------------------------------------------------------------------------
def load_ledger() -> list:
    if not LEDGER_PATH.exists():
        return []
    try:
        return json.loads(LEDGER_PATH.read_text())
    except json.JSONDecodeError:
        return []


def append_block(block_type: str, data: dict) -> dict:
    """The ONLY write operation: add a block at the end. No edit, no delete."""
    ledger = load_ledger()
    block = {
        "index": len(ledger),
        "timestamp": now_iso(),
        "type": block_type,
        "data": data,
        "prev_hash": ledger[-1]["hash"] if ledger else GENESIS_HASH,
    }
    block["hash"] = block_hash(block)
    ledger.append(block)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=1))
    return block


def verify_chain(ledger: list) -> tuple:
    """Recompute every hash. Returns (ok, index_of_first_broken_block or None)."""
    prev = GENESIS_HASH
    for i, b in enumerate(ledger):
        if b["index"] != i or b["prev_hash"] != prev or block_hash(b) != b["hash"]:
            return False, i
        prev = b["hash"]
    return True, None


# --------------------------------------------------------------------------
# 3. Deterministic state + auction engine (anyone can re-run it)
# --------------------------------------------------------------------------
def current_event(ledger: list):
    events = [b for b in ledger if b["type"] == "EVENT_CREATED"]
    return events[-1] if events else None


def event_blocks(ledger: list, event_id: str) -> list:
    return [b for b in ledger if b["data"].get("event_id") == event_id]


def current_phase(blocks: list) -> str:
    phases = [b["data"]["phase"] for b in blocks if b["type"] == "PHASE"]
    return phases[-1] if phases else "SETUP"


def check_reveals(event: dict, blocks: list) -> pd.DataFrame:
    """Rule set (public, deterministic):
    1) for each fan, take the LATEST reveal made during the REVEAL phase;
    2) match it with the fan's latest commit made BEFORE that reveal;
    3) accept if the hash matches and the bid is >= floor price."""
    cfg = event["data"]
    commits = [b for b in blocks if b["type"] == "COMMIT"]
    reveals = [b for b in blocks if b["type"] == "REVEAL"]
    latest_reveal = {}
    for r in reveals:
        latest_reveal[r["data"]["fan_id"]] = r  # ledger order = time order
    rows = []
    for fan, r in latest_reveal.items():
        prior = [c for c in commits if c["data"]["fan_id"] == fan and c["index"] < r["index"]]
        bid = r["data"]["bid"]
        status, reason, commit_ts = "rejected", "", None
        if not prior:
            reason = "no prior commit"
        else:
            c = prior[-1]
            commit_ts = c["timestamp"]
            recomputed = commit_hash(cfg["event_id"], fan, bid, r["data"]["nonce"])
            if recomputed != c["data"]["commit"]:
                reason = "hash mismatch (wrong bid or nonce)"
            elif bid < cfg["floor_price"]:
                reason = f"bid below floor ({cfg['floor_price']:.2f} EUR)"
            else:
                status, reason = "valid", "hash matches commit"
        rows.append({"fan_id": fan, "bid_eur": bid, "commit_time": commit_ts,
                     "status": status, "reason": reason})
    return pd.DataFrame(rows, columns=["fan_id", "bid_eur", "commit_time", "status", "reason"])


def clear_auction(event: dict, reveal_table: pd.DataFrame) -> dict:
    """Uniform-price clearing: top-N valid bids win (1 ticket per fan),
    ties broken by earliest commit; everyone pays the lowest winning bid,
    capped by the organizer's price cap."""
    cfg = event["data"]
    supply = cfg["tickets"]
    valid = reveal_table[reveal_table["status"] == "valid"].copy()
    if valid.empty:
        return {"winners": valid, "price": None, "valid_bids": 0, "supply": supply}
    valid = valid.sort_values(["bid_eur", "commit_time"], ascending=[False, True]).reset_index(drop=True)
    valid["rank"] = valid.index + 1
    winners = valid.head(supply).copy()
    if len(valid) <= supply:
        price = cfg["floor_price"]  # undersubscribed: everybody pays the floor
    else:
        price = winners["bid_eur"].min()
    price = min(price, cfg["price_cap"])
    winners["pays_eur"] = price
    return {"winners": winners, "ranked": valid, "price": price,
            "valid_bids": len(valid), "supply": supply}


def resale_state(blocks: list, price_paid: float) -> pd.DataFrame:
    listings = {}
    for b in blocks:
        d = b["data"]
        if b["type"] == "RESALE_LIST":
            listings[d["listing_id"]] = {"listing_id": d["listing_id"], "seller": d["fan_id"],
                                         "price_eur": d["price"], "buyer": None}
        elif b["type"] == "RESALE_BUY" and d["listing_id"] in listings:
            listings[d["listing_id"]]["buyer"] = d["fan_id"]
    return pd.DataFrame(listings.values(), columns=["listing_id", "seller", "price_eur", "buyer"])


def ticket_holders(winners: pd.DataFrame, resale: pd.DataFrame) -> set:
    holders = set(winners["fan_id"]) if not winners.empty else set()
    for _, l in resale.iterrows():
        if l["buyer"]:
            holders.discard(l["seller"])
            holders.add(l["buyer"])
    return holders


# --------------------------------------------------------------------------
# 4. UI
# --------------------------------------------------------------------------
st.set_page_config(page_title="FairSeat", page_icon="🎟️", layout="wide")
st.title("🎟️ FairSeat")
st.caption("Sealed-bid concert tickets. No bots, no scalpers, one fair price - and anyone can verify it.")

ledger = load_ledger()
event = current_event(ledger)
blocks = event_blocks(ledger, event["data"]["event_id"]) if event else []
phase = current_phase(blocks) if event else "SETUP"

with st.sidebar:
    st.header("Who are you?")
    fan_id = st.text_input("Fan ID (e.g. student number)", key="fan_id").strip()
    st.divider()
    if event:
        cfg = event["data"]
        st.subheader(cfg["name"])
        st.write(f"**Phase:** `{phase}`")
        st.write(f"Tickets: **{cfg['tickets']}** - Floor: **{cfg['floor_price']:.2f} EUR** - "
                 f"Cap: **{cfg['price_cap']:.2f} EUR**")
        n_commits = len({b['data']['fan_id'] for b in blocks if b['type'] == 'COMMIT'})
        n_reveals = len({b['data']['fan_id'] for b in blocks if b['type'] == 'REVEAL'})
        st.write(f"Fans committed: **{n_commits}** - revealed: **{n_reveals}**")
    else:
        st.info("No event yet. Create one in the Organizer tab.")
    ok, _ = verify_chain(ledger)
    st.write("Ledger:", "✅ intact" if ok else "❌ TAMPERED", f"({len(ledger)} blocks)")

tab_org, tab_commit, tab_reveal, tab_results, tab_resale, tab_ledger = st.tabs(
    ["🛠 Organizer", "🔒 1. Commit", "🔓 2. Reveal", "🏆 3. Results", "🔁 Resale", "⛓ Ledger & Verify"])

# ---------------- Organizer ----------------
with tab_org:
    st.subheader("Create an event")
    with st.form("create_event"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Event name", "Concert - Zénith Paris, 14 Feb 2027")
        tickets = c2.number_input("Tickets for sale", 1, 100000, 10)
        floor_price = c1.number_input("Floor price = face value (EUR)", 0.0, 10000.0, 45.0, step=5.0)
        price_cap = c2.number_input("Price cap (EUR)", 0.0, 10000.0, 150.0, step=5.0)
        if st.form_submit_button("Create event (starts in SETUP)"):
            if price_cap < floor_price:
                st.error("The cap must be at least the floor price.")
            else:
                event_id = f"EVT-{int(time.time())}"
                append_block("EVENT_CREATED", {"event_id": event_id, "name": name, "tickets": int(tickets),
                                               "floor_price": float(floor_price), "price_cap": float(price_cap)})
                append_block("PHASE", {"event_id": event_id, "phase": "SETUP"})
                st.success(f"Event {event_id} created.")
                st.rerun()

    if event:
        st.subheader("Move to the next phase")
        st.write(f"Current phase: **{phase}**. Phases only move forward: "
                 + " → ".join(f"**{p}**" if p == phase else p for p in PHASES))
        if phase != "CLOSED":
            nxt = PHASES[PHASES.index(phase) + 1]
            if st.button(f"Open {nxt} phase"):
                append_block("PHASE", {"event_id": event["data"]["event_id"], "phase": nxt})
                st.rerun()

        st.subheader("Demo helper")
        st.caption("Adds simulated fans so the class demo has a real crowd. Their nonces are random.")
        n_bots = st.number_input("Number of simulated fans", 1, 500, 25)
        if phase == "COMMIT" and st.button("Add simulated commits"):
            sim = st.session_state.setdefault("sim_secrets", {})
            for _ in range(int(n_bots)):
                fid = f"SIM-{random.randint(100000, 999999)}"
                bid = round(random.triangular(event["data"]["floor_price"] * 0.8,
                                              event["data"]["price_cap"] * 1.2,
                                              event["data"]["floor_price"] * 1.6), 2)
                nonce = sha256(str(random.random()))[:12]
                sim[fid] = (bid, nonce)
                append_block("COMMIT", {"event_id": event["data"]["event_id"], "fan_id": fid,
                                        "commit": commit_hash(event["data"]["event_id"], fid, bid, nonce)})
            st.success(f"{int(n_bots)} simulated commits added.")
            st.rerun()
        if phase == "REVEAL" and st.button("Reveal simulated fans"):
            sim = st.session_state.get("sim_secrets", {})
            for fid, (bid, nonce) in sim.items():
                append_block("REVEAL", {"event_id": event["data"]["event_id"], "fan_id": fid,
                                        "bid": bid, "nonce": nonce})
            st.success(f"{len(sim)} simulated reveals added.")
            st.rerun()

        st.subheader("Reset")
        if st.checkbox("I want to wipe the whole ledger (demo only)"):
            if st.button("Delete ledger file", type="primary"):
                LEDGER_PATH.unlink(missing_ok=True)
                st.session_state.pop("sim_secrets", None)
                st.rerun()

# ---------------- Commit ----------------
with tab_commit:
    st.subheader("Step 1 - Commit a sealed bid")
    st.markdown("Your bid stays **secret**: only its fingerprint (a SHA-256 hash) goes on the ledger. "
                "You will reveal it after the deadline. **Bid what the ticket is really worth to you** - "
                "everyone pays the same clearing price, never more than their own bid.")
    if not event or phase != "COMMIT":
        st.warning(f"Commits are closed (phase: {phase}).")
    elif not fan_id:
        st.info("Enter your Fan ID in the sidebar first.")
    else:
        with st.form("commit"):
            bid = st.number_input("Your maximum price (EUR)", 0.0, 10000.0, 60.0, step=1.0)
            nonce = st.text_input("Secret nonce (like a password - write it down!)", type="password")
            if st.form_submit_button("Commit my sealed bid"):
                if not nonce:
                    st.error("A nonce is required, otherwise your bid could be guessed from its hash.")
                else:
                    h = commit_hash(event["data"]["event_id"], fan_id, bid, nonce)
                    append_block("COMMIT", {"event_id": event["data"]["event_id"], "fan_id": fan_id,
                                            "commit": h})
                    st.success("Commit recorded on the ledger.")
                    st.code(f"preimage : {event['data']['event_id']}|{fan_id}|{bid:.2f}|{nonce}\ncommit   : {h}")
                    st.warning("Save the preimage above. You need the exact same bid and nonce to reveal.")

# ---------------- Reveal ----------------
with tab_reveal:
    st.subheader("Step 2 - Reveal your bid")
    if not event or phase != "REVEAL":
        st.warning(f"Reveals are not open (phase: {phase}).")
    elif not fan_id:
        st.info("Enter your Fan ID in the sidebar first.")
    else:
        with st.form("reveal"):
            bid = st.number_input("The bid you committed (EUR)", 0.0, 10000.0, 60.0, step=1.0)
            nonce = st.text_input("Your secret nonce", type="password")
            if st.form_submit_button("Reveal"):
                append_block("REVEAL", {"event_id": event["data"]["event_id"], "fan_id": fan_id,
                                        "bid": float(bid), "nonce": nonce})
                blocks = event_blocks(load_ledger(), event["data"]["event_id"])
                mine = check_reveals(event, blocks)
                mine = mine[mine["fan_id"] == fan_id]
                if not mine.empty and mine.iloc[0]["status"] == "valid":
                    st.success("✅ Reveal accepted: the hash matches your commit.")
                else:
                    reason = mine.iloc[0]["reason"] if not mine.empty else "unknown"
                    st.error(f"❌ Reveal rejected: {reason}. You can try again; the latest reveal counts.")

# ---------------- Results ----------------
with tab_results:
    st.subheader("Step 3 - Results (computed live from the public ledger)")
    if not event:
        st.info("No event yet.")
    elif phase not in ("REVEAL", "CLOSED"):
        st.info("Results appear once the reveal phase has started.")
    else:
        table = check_reveals(event, blocks)
        res = clear_auction(event, table)
        if res["price"] is None:
            st.info("No valid bids yet.")
        else:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Clearing price", f"{res['price']:.2f} EUR")
            c2.metric("Tickets", res["supply"])
            c3.metric("Valid bids", res["valid_bids"])
            c4.metric("Demand / supply", f"{res['valid_bids'] / res['supply']:.1f}x")
            if phase == "REVEAL":
                st.caption("Provisional - final when the organizer closes the reveal phase.")
            if fan_id:
                w = res["winners"]
                if fan_id in set(w["fan_id"]):
                    st.success(f"🎉 You won a ticket. You pay {res['price']:.2f} EUR.")
                elif fan_id in set(table["fan_id"]):
                    st.info("No ticket this time: your bid was below the clearing price.")
            ranked = res["ranked"].copy()
            ranked["winner"] = ranked["rank"] <= res["supply"]
            st.bar_chart(ranked.set_index("rank")[["bid_eur"]], height=260)
            st.caption("Bids ranked high to low. The top N win and all pay the N-th highest bid.")
            st.dataframe(ranked[["rank", "fan_id", "bid_eur", "commit_time", "winner"]], hide_index=True)
            scalper_gap = (res["price"] - event["data"]["floor_price"]) * min(res["supply"], res["valid_bids"])
            st.markdown(f"**Value kept from scalpers:** {scalper_gap:,.2f} EUR - the gap between face value "
                        f"and the market price, now paid to the organizer instead of a reseller.")
        with st.expander("Rejected reveals"):
            st.dataframe(table[table["status"] != "valid"], hide_index=True)

# ---------------- Resale ----------------
with tab_resale:
    st.subheader("Capped resale (organizer-authorized)")
    st.markdown("A winner who can no longer attend can resell **only here**, at **no more than the clearing "
                "price**. No markup is possible, so buying to resell makes no sense.")
    if not event or phase != "CLOSED":
        st.info("Resale opens once the auction is closed.")
    else:
        res = clear_auction(event, check_reveals(event, blocks))
        price = res["price"] or event["data"]["floor_price"]
        resale = resale_state(blocks, price)
        holders = ticket_holders(res["winners"], resale)
        open_listings = resale[resale["buyer"].isna()]
        st.dataframe(resale, hide_index=True)
        if fan_id in holders and fan_id not in set(open_listings["seller"]):
            ask = st.number_input("Your asking price (EUR)", 0.0, float(price), float(price))
            if st.button("List my ticket"):
                append_block("RESALE_LIST", {"event_id": event["data"]["event_id"], "fan_id": fan_id,
                                             "listing_id": f"L{len(resale) + 1}", "price": float(ask)})
                st.rerun()
        elif fan_id and fan_id not in holders and not open_listings.empty:
            lid = st.selectbox("Buy listing", open_listings["listing_id"])
            if st.button("Buy this ticket"):
                append_block("RESALE_BUY", {"event_id": event["data"]["event_id"], "fan_id": fan_id,
                                            "listing_id": lid})
                st.rerun()

# ---------------- Ledger & Verify ----------------
with tab_ledger:
    st.subheader("The public ledger")
    st.markdown("Every action is a block that stores the hash of the previous block. "
                "Changing any past block breaks every hash after it.")
    if ledger:
        df = pd.DataFrame([{"#": b["index"], "time (UTC)": b["timestamp"], "type": b["type"],
                            "data": json.dumps(b["data"]), "prev_hash": b["prev_hash"][:12] + "…",
                            "hash": b["hash"][:12] + "…"} for b in ledger])
        st.dataframe(df, hide_index=True, height=300)
        st.download_button("Download full ledger (JSON)", json.dumps(ledger, indent=1),
                           file_name="fairseat_ledger.json")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Verify the real ledger**")
            if st.button("Run verification"):
                ok, bad = verify_chain(ledger)
                if ok:
                    st.success(f"✅ All {len(ledger)} blocks verified. Chain intact.")
                    if event and phase in ("REVEAL", "CLOSED"):
                        r = clear_auction(event, check_reveals(event, blocks))
                        if r["price"] is not None:
                            st.write(f"Recomputed clearing price: **{r['price']:.2f} EUR**, "
                                     f"{len(r['winners'])} winners - same result for anyone who runs it.")
                else:
                    st.error(f"❌ Chain broken at block #{bad}.")
        with c2:
            st.markdown("**Tamper test (on a copy)**")
            st.caption("Simulates a dishonest organizer raising one fan's revealed bid.")
            target = [b for b in ledger if b["type"] == "REVEAL"]
            if target and st.button("Tamper with a copy and verify"):
                fake = json.loads(json.dumps(ledger))
                victim = fake[target[0]["index"]]
                victim["data"]["bid"] = victim["data"]["bid"] + 100
                ok, bad = verify_chain(fake)
                st.error(f"❌ Detected: block #{bad} no longer matches its hash. "
                         f"Every block after it is invalid too.")
    else:
        st.info("Ledger is empty.")
