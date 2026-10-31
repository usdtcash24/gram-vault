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
def get_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT shards, energy, max_energy, regen_speed, tier, multiplier, has_autoclicker 
        FROM users WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        tier, mult = calc_tier(user_id)
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (user_id, shards, energy, max_energy, regen_speed, tier, multiplier, has_autoclicker) 
            VALUES (?, 0, 2000, 2000, 2, ?, ?, 0)
        """, (user_id, tier, mult))
        conn.commit()
        conn.close()
        return {
            "user_id": user_id, "shards": 0, "energy": 2000, "max_energy": 2000,
            "regen_speed": 2, "tier": tier, "multiplier": mult, "has_autoclicker": 0
        }

    return {
        "user_id": user_id, "shards": row[0], "energy": row[1], "max_energy": row[2],
        "regen_speed": row[3], "tier": row[4], "multiplier": row[5], "has_autoclicker": row[6]
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

    # Запрашиваем последние 15 транзакций кошелька через открытый API toncenter
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(
                "https://toncenter.com/api/v2/getTransactions",
                params={"address": MY_WALLET, "limit": 15},
                timeout=10.0
            )
            res_data = res.json()
        except Exception as e:
            raise HTTPException(status_code=502, detail="Failed to connect to TON network")

    if not res_data.get("ok"):
        raise HTTPException(status_code=502, detail="TON API response error")

    transactions = res_data.get("result", [])
    matched_tx = None

    for tx in transactions:
        in_msg = tx.get("in_msg", {})
        msg_data = in_msg.get("message", "")
        value_nano = int(in_msg.get("value", 0))
        value_ton = value_nano / 1e9

        # Сверяем комментарий и сумму (допускаем погрешность в пределах комиссии)
        if expected_comment in msg_data and value_ton >= (expected_price * 0.98):
            tx_id = tx.get("transaction_id", {}).get("hash")
            matched_tx = (tx_id, value_ton)
            break

    if not matched_tx:
        raise HTTPException(
            status_code=404, 
            detail="Транзакция пока не подтверждена блокчейном. Подождите 10-15 секунд."
        )

    tx_hash, confirmed_amount = matched_tx

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Проверка на повторное использование той же транзакции
    cur.execute("SELECT tx_hash FROM processed_txs WHERE tx_hash = ?", (tx_hash,))
    if cur.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Эта транзакция уже была активирована!")

    # Применяем улучшение
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

    return {"status": "success", "upgrade": data.upgrade_type, "tx_hash": tx_hash}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
