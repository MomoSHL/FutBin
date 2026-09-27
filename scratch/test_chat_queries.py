import sys
import os
import asyncio
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services.http_client import HttpClient
from services.price_service import PriceService
from services.state_service import StateService
from main import normalize_player_text, matches_player_name

async def main():
    print("Testing chat sales query resolution...")
    http = HttpClient()
    await http.start()
    price_svc = PriceService(http, max_concurrent_requests=3, platform="pc")

    queries = ["stanway", "wirtz", "musiala", "florian wirtz", "georgia stanway"]

    for q in queries:
        print(f"\n--- Testing Query: '{q}' ---")
        results = await price_svc.search_player(q)
        print(f"  Results found: {len(results)}")
        if results:
            match = None
            for res in results:
                if matches_player_name(q, res['name']):
                    match = res
                    break
            if not match:
                match = results[0]
            print(f"  Matched: {match['name']} ({match['url']})")
            
            # Fetch sales for matched player
            sales_res = await price_svc.fetch_player_sales(match['url'], platform="pc")
            print(f"  Sales found: {len(sales_res.get('sales', []))} | Avg: {sales_res.get('avg_price')}")
            if sales_res.get('sales'):
                print(f"  Latest sale: {sales_res['sales'][0]}")

    await http.close()
    print("\n✅ All chat queries tested successfully!")

if __name__ == '__main__':
    asyncio.run(main())
