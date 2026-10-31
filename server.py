import os
import sqlite3
import uvicorn
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="GRAM Genesis Vault API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "vault.db"
MY_WALLET = "UQALP8hAmq3qvZfEwHfgSoJWAZhM57MKos4icLJ4YaqrmEgU"

UPGRADE_PRICES = {
    "charge": 0.2,
    "battery": 0.3,
    "overclock": 0.5,
    "autoclicker": 1.0
}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            shards INTEGER DEFAULT 0,
            energy INTEGER DEFAULT 2000,
            max_energy INTEGER DEFAULT 2000,
            regen_speed INTEGER DEFAULT 2,
            tier TEXT DEFAULT 'MODERN',
            multiplier REAL DEFAULT 1.0,
            has_autoclicker INTEGER DEFAULT 0,
            referred_by INTEGER DEFAULT NULL,
            referrals_count INTEGER DEFAULT 0,
            last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_txs (
            tx_hash TEXT PRIMARY KEY,
            user_id INTEGER,
            upgrade_type TEXT,
            amount REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

class SyncPayload(BaseModel):
    user_id: int
    username: str = ""
    added_shards: int
    current_energy: int

class VerifyPayload(BaseModel):
    user_id: int
    upgrade_type: str

def calc_tier(user_id: int):
    if user_id < 200_000_000:
        return "GENESIS NODE (Tier 1)", 4.0
    elif user_id < 800_000_000:
        return "VINTAGE NODE (Tier 2)", 2.5
    return "MODERN NODE", 1.0

@app.get("/api/user/{user_id}")
def get_user(user_id: int, ref: int = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT shards, energy, max_energy, regen_speed, tier, multiplier, has_autoclicker, referrals_count 
        FROM users WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()

    if not row:
        tier, mult = calc_tier(user_id)
        start_shards = 0
        ref_by = None

        if ref and ref != user_id:
            cur.execute("SELECT user_id FROM users WHERE user_id = ?", (ref,))
            if cur.fetchone():
                ref_by = ref
                start_shards = 5000
                cur.execute("UPDATE users SET shards = shards + 5000, referrals_count = referrals_count + 1 WHERE user_id = ?", (ref,))

        cur.execute("""
            INSERT INTO users (user_id, shards, energy, max_energy, regen_speed, tier, multiplier, has_autoclicker, referred_by, referrals_count) 
            VALUES (?, ?, 2000, 2000, 2, ?, ?, 0, ?, 0)
        """, (user_id, start_shards, tier, mult, ref_by))
        conn.commit()
        
        cur.execute("""
            SELECT shards, energy, max_energy, regen_speed, tier, multiplier, has_autoclicker, referrals_count 
            FROM users WHERE user_id = ?
        """, (user_id,))
        row = cur.fetchone()

    conn.close()
    return {
        "user_id": user_id, "shards": row[0], "energy": row[1], "max_energy": row[2],
        "regen_speed": row[3], "tier": row[4], "multiplier": row[5], "has_autoclicker": row[6],
        "referrals_count": row[7]
    }

@app.post("/api/sync")
def sync_clicks(data: SyncPayload):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET shards = shards + ?, energy = ?, last_sync = CURRENT_TIMESTAMP WHERE user_id = ?",
        (data.added_shards, data.current_energy, data.user_id)
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/verify-ton-payment")
async def verify_ton_payment(data: VerifyPayload):
    expected_price = UPGRADE_PRICES.get(data.upgrade_type)
    if not expected_price:
        raise HTTPException(status_code=400, detail="Invalid upgrade type")

    expected_comment = f"upg_{data.upgrade_type}_{data.user_id}"

    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(
                "https://toncenter.com/api/v2/getTransactions",
                params={"address": MY_WALLET, "limit": 15},
                timeout=10.0
            )
            res_data = res.json()
        except Exception:
            raise HTTPException(status_code=502, detail="Blockchain query failed")

    if not res_data.get("ok"):
        raise HTTPException(status_code=502, detail="TON API error")

    matched_tx = None
    for tx in res_data.get("result", []):
        in_msg = tx.get("in_msg", {})
        msg_data = in_msg.get("message", "")
        value_ton = int(in_msg.get("value", 0)) / 1e9

        if expected_comment in msg_data and value_ton >= (expected_price * 0.98):
            matched_tx = (tx.get("transaction_id", {}).get("hash"), value_ton)
            break

    if not matched_tx:
        raise HTTPException(status_code=404, detail="Транзакция еще не подтверждена сетью.")

    tx_hash, confirmed_amount = matched_tx
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT tx_hash FROM processed_txs WHERE tx_hash = ?", (tx_hash,))
    if cur.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Транзакция уже использована!")

    if data.upgrade_type == "battery":
        cur.execute("UPDATE users SET max_energy = max_energy + 2000 WHERE user_id = ?", (data.user_id,))
    elif data.upgrade_type == "charge":
        cur.execute("UPDATE users SET regen_speed = regen_speed + 4 WHERE user_id = ?", (data.user_id,))
    elif data.upgrade_type == "overclock":
        cur.execute("UPDATE users SET multiplier = multiplier + 2.0 WHERE user_id = ?", (data.user_id,))
    elif data.upgrade_type == "autoclicker":
        cur.execute("UPDATE users SET has_autoclicker = 1 WHERE user_id = ?", (data.user_id,))

    cur.execute(
        "INSERT INTO processed_txs (tx_hash, user_id, upgrade_type, amount) VALUES (?, ?, ?, ?)",
        (tx_hash, data.user_id, data.upgrade_type, confirmed_amount)
    )
    conn.commit()
    conn.close()
    return {"status": "success", "upgrade": data.upgrade_type}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
