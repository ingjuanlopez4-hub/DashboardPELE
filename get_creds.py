import os
from py_clob_client_v2 import ClobClient

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

def main():
    private_key = os.environ.get("PRIVATE_KEY")
    if not private_key:
        print("PRIVATE_KEY no configurada")
        return

    client = ClobClient(host=HOST, chain_id=CHAIN_ID, key=private_key)

    print("Derivando credenciales CLOB...")
    try:
        creds = client.create_or_derive_api_key()
        print(f"POLYMARKET_API_KEY={creds.api_key}")
        print(f"POLYMARKET_SECRET={creds.api_secret}")
        print(f"POLYMARKET_PASSPHRASE={creds.api_passphrase}")
    except Exception as e:
        print(f"Error: {e}")
        try:
            creds = client.derive_api_key()
            print(f"POLYMARKET_API_KEY={creds.api_key}")
            print(f"POLYMARKET_SECRET={creds.api_secret}")
            print(f"POLYMARKET_PASSPHRASE={creds.api_passphrase}")
        except Exception as e2:
            print(f"Error derive: {e2}")

if __name__ == "__main__":
    main()
