import os
import aiohttp
import asyncio

API_KEY = os.getenv("APISPORTS_KEY")

async def main():
    if not API_KEY:
        print("❌ No se encontró la variable APISPORTS_KEY")
        return

    url = "https://v3.football.api-sports.io/leagues?search=LaLiga"
    headers = {"x-apisports-key": API_KEY}

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                print(f"❌ Error HTTP {resp.status}")
                return
            data = await resp.json()
            print("✅ Respuesta recibida de la API:")
            for league in data.get("response", []):
                print("-", league["league"]["name"], "(", league["country"]["name"], ")")

asyncio.run(main())
