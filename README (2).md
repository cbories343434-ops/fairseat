# FairSeat — Streamlit MVP

Sealed-bid, uniform-price concert ticket auction with a hash-chained public ledger.
NEOMA MSc Fintech & DeFi — Charles Bories, Gabriel Rodrigues, Thijmen Delwel, Julie Ceccaldi.

## Run

```
pip install streamlit pandas
streamlit run fairseat_app.py
```

The ledger is stored in `fairseat_ledger.json` next to the script (created automatically).

## Demo script (≈ 3 minutes)

1. **Organizer tab** → Create event (10 tickets, floor 45 €, cap 150 €) → *Open COMMIT phase*.
2. *Add simulated commits* (25 fans) so there is a crowd.
3. Sidebar: enter a Fan ID → **Commit tab** → bid + nonce → the app shows the hash stored on the ledger.
   Open a second browser tab with another Fan ID to show two fans bidding.
4. Organizer → *Open REVEAL phase* → *Reveal simulated fans*.
5. **Reveal tab** → re-enter your bid + nonce → accepted. Try a wrong nonce → rejected with a reason.
6. Organizer → *Open CLOSED phase* → **Results tab**: one clearing price for all winners.
7. **Ledger & Verify** → *Run verification* (chain intact) → *Tamper with a copy* (the broken block is detected).
8. **Resale tab**: a winner lists a ticket — the price cannot exceed the clearing price.

## Rules (deterministic — anyone gets the same result)

- `commit = sha256(event_id|fan_id|bid|nonce)`, bid with 2 decimals.
- Latest reveal per fan, matched to the fan's latest commit made before it; valid if hash matches and bid ≥ floor.
- Top N valid bids win (1 ticket per fan), ties → earliest commit.
- Everyone pays the lowest winning bid (floor if undersubscribed), capped by the organizer.
- Each ledger block stores the previous block's hash; editing any block breaks the chain.
