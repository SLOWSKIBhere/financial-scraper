import httpx
import sys

# Test different headers to see what Reddit allows without OAuth
tests = [
    {
        "name": "Standard Chrome Header",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        }
    },
    {
        "name": "Custom Bot Style User-Agent",
        "headers": {
            "User-Agent": "VibeForgeTrendsCollector/1.0 (by /u/kowshik_j)",
            "Accept": "application/json"
        }
    },
    {
        "name": "Firefox Header mimicking browser",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin"
        }
    }
]

for idx, t in enumerate(tests):
    print(f"\n--- Running Test {idx+1}: {t['name']} ---")
    try:
        # We enforce HTTP/1.1 by setting http2=False
        with httpx.Client(http2=False, follow_redirects=True) as client:
            response = client.get("https://www.reddit.com/r/learnpython/hot.json?limit=5", headers=t["headers"])
            print(f"Status Code: {response.status_code}")
            if response.status_code == 200:
                print("Success! First post title:")
                data = response.json()
                children = data.get("data", {}).get("children", [])
                if children:
                    print(f"-> {children[0]['data']['title']}")
                else:
                    print("-> No posts found in response")
            else:
                print(f"Response: {response.text[:200]}")
    except Exception as e:
        print(f"Error: {e}")
