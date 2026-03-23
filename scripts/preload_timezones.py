#!/usr/bin/env python3
import asyncio
import sqlite3
import json
import os
from pathlib import Path
import httpx
from datetime import datetime
from tenacity import retry, stop_after_attempt, wait_exponential
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
TOP_TIMEZONES = [
    tz.strip() for tz in 
    os.getenv(
        "TOP_TIMEZONES",
        "America/New_York,America/Chicago,America/Denver,America/Los_Angeles,"
        "America/Toronto,Europe/London,Europe/Paris,Europe/Berlin,Asia/Tokyo,"
        "Asia/Shanghai,Asia/Kolkata,America/Sao_Paulo,Australia/Sydney,"
        "Pacific/Auckland,Asia/Dubai,Asia/Singapore,America/Mexico_City,"
        "Europe/Moscow,Africa/Johannesburg,Asia/Jakarta,Asia/Bangkok,"
        "America/Argentina/Buenos_Aires,Asia/Seoul,Asia/Taipei,America/Vancouver,"
        "Europe/Madrid,Europe/Rome,Europe/Amsterdam,Europe/Stockholm,Asia/Hong_Kong,"
        "Asia/Kuala_Lumpur,Asia/Manila,Asia/Ho_Chi_Minh,Asia/Riyadh,Asia/Karachi,"
        "Asia/Dhaka,Africa/Cairo,Africa/Lagos,Africa/Nairobi,Australia/Melbourne,"
        "Australia/Perth,America/Phoenix,America/Bogota,America/Lima,"
        "America/Santiago,America/Caracas,Europe/Lisbon,Europe/Zurich,"
        "Europe/Athens,Europe/Brussels"
    ).split(",")
    if tz.strip()  # Skip empty entries
]

CACHE_DB = Path("/var/cache/timezone_proxy/timezone_cache.db")
API_BASE = "https://timeapi.io/api/timezone/zone"
REQUEST_DELAY = int(os.getenv("TIMEZONE_PRELOAD_DELAY", "32"))  # seconds
MAX_RETRIES = int(os.getenv("TIMEZONE_MAX_RETRIES", "3"))
TIMEOUT = 30  # seconds

@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    reraise=True
)
async def fetch_timezone(client: httpx.AsyncClient, timezone: str):
    """Fetch timezone data with robust error handling"""
    url = f"{API_BASE}?timeZone={timezone}&futureChanges=true"
    try:
        response = await client.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP {e.response.status_code} for {timezone}"
        if e.response.status_code == 429:
            retry_after = int(e.response.headers.get('Retry-After', 60))
            await asyncio.sleep(retry_after)
        raise Exception(error_msg) from e
    except httpx.RequestError as e:
        raise Exception(f"Network error for {timezone}: {str(e)}") from e

def save_to_cache(timezone: str, data: dict):
    """Atomic cache update with error handling"""
    try:
        with sqlite3.connect(str(CACHE_DB)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO timezone_cache (timezone, data) VALUES (?, ?)",
                (timezone, json.dumps({
                    **data,
                    "_cached_at": datetime.utcnow().isoformat(),
                    "_source": "batch_preload"
                }))
            )
            conn.commit()
    except sqlite3.Error as e:
        raise Exception(f"DB save failed for {timezone}: {str(e)}")

async def process_timezone(client: httpx.AsyncClient, timezone: str, attempt: int = 1):
    """Process a single timezone with retry logic"""
    try:
        print(f"⏳ Attempt {attempt} for {timezone}")
        data = await fetch_timezone(client, timezone)
        save_to_cache(timezone, data)
        print(f"✅ Saved {timezone}")
        return True
    except Exception as e:
        print(f"⚠️ Error: {str(e)}")
        if attempt < MAX_RETRIES:
            await asyncio.sleep(2 ** attempt)  # Exponential backoff
        return False

async def main():
    print(f"\n🚀 Starting preload for {len(TOP_TIMEZONES)} timezones")
    print(f"⏱  Delay: {REQUEST_DELAY}s | Retries: {MAX_RETRIES} | Timeout: {TIMEOUT}s")
    
    success_count = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(TIMEOUT),
        limits=httpx.Limits(max_connections=1)  # Avoid overwhelming API
    ) as client:
        for i, tz in enumerate(TOP_TIMEZONES, 1):
            print(f"\n📍 [{i}/{len(TOP_TIMEZONES)}] Processing {tz}")
            
            for attempt in range(1, MAX_RETRIES + 1):
                if await process_timezone(client, tz, attempt):
                    success_count += 1
                    break
            else:
                print(f"❌ Failed all attempts for {tz}")
            
            # Dynamic delay with occasional longer pause
            delay = REQUEST_DELAY * 2 if i % 10 == 0 else REQUEST_DELAY
            if i < len(TOP_TIMEZONES):  # No delay after last item
                print(f"⏸  Waiting {delay}s...")
                await asyncio.sleep(delay)
    
    success_rate = (success_count / len(TOP_TIMEZONES)) * 100
    print(f"\n🎉 Completed! Success rate: {success_count}/{len(TOP_TIMEZONES)} ({success_rate:.1f}%)")

if __name__ == "__main__":
    asyncio.run(main())
