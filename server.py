import os
import sqlite3
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="GRAM Genesis Vault API")

# Разрешаем запросы с GitHub Pages
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "vault.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            shards INTEGER DEFAULT 0,
            energy INTEGER DEFAULT 2000,
            tier TEXT DEFAULT 'MODERN',
            multiplier REAL DEFAULT 1.0,
            last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    cur.execute("SELECT shards, energy, tier, multiplier FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        tier, mult = calc_tier(user_id)
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (user_id, shards, energy, tier, multiplier) VALUES (?, 0, 2000, ?, ?)",
            (user_id, tier, mult)
        )
        conn.commit()
        conn.close()
        return {"user_id": user_id, "shards": 0, "energy": 2000, "tier": tier, "multiplier": mult}

    return {"user_id": user_id, "shards": row[0], "energy": row[1], "tier": row[2], "multiplier": row[3]}

@app.post("/api/sync")
def sync_clicks(data: SyncPayload):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT shards FROM users WHERE user_id = ?", (data.user_id,))
    row = cur.fetchone()

    if not row:
        tier, mult = calc_tier(data.user_id)
        cur.execute(
            "INSERT INTO users (user_id, username, shards, energy, tier, multiplier) VALUES (?, ?, ?, ?, ?, ?)",
            (data.user_id, data.username, data.added_shards, data.current_energy, tier, mult)
        )
    else:
        cur.execute(
            "UPDATE users SET shards = shards + ?, energy = ?, last_sync = CURRENT_TIMESTAMP WHERE user_id = ?",
            (data.added_shards, data.current_energy, data.user_id)
        )
    conn.commit()
    conn.close()
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
