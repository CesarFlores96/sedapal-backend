import os
import httpx
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

API_KEY = os.getenv("API_KEY", "sedapalgis_api_key_unified_2026") # default key if any
PORT = 8000

def test_search(query: str):
    url = f"http://127.0.0.1:{PORT}/api/customer-supplies/search-vector"
    headers = {
        "x-api-key": API_KEY
    }
    params = {
        "query": query,
        "limit": 5
    }
    
    print(f"Testing vector search for: '{query}'...")
    try:
        response = httpx.get(url, headers=headers, params=params, timeout=10.0)
        print("Response Status Code:", response.status_code)
        if response.status_code == 200:
            results = response.json()
            print(f"Found {len(results)} matches:")
            for idx, item in enumerate(results):
                print(f"  {idx + 1}. NIS: {item.get('supply_code')} | Cliente: {item.get('customer_name')} | Direccion: {item.get('service_address')}")
        else:
            print("Response Error:", response.text)
    except Exception as e:
        print("Exception occurred:", e)

if __name__ == "__main__":
    # Test with a name likely to exist in the database (or a generic address)
    test_search("Gloria")
    print("-" * 50)
    test_search("Javier Prado")
