import os
from py_clob_client.client import ClobClient
from dotenv import load_dotenv
# Load the environment variables from the .env file
load_dotenv()
def main():
    host = "https://clob.polymarket.com"
    key = os.getenv("POLYMARKET_PRIVATE_KEY")
    chain_id = 137  # Polygon Mainnet chain ID
    # Ensure the private key is loaded correctly
    if not key:
        raise ValueError("Private key not found. Please set POLYMARKET_PRIVATE_KEY in the environment variables.")
    # Initialize the client with your private key
    client = ClobClient(host, key=key, chain_id=chain_id)
    # Create or derive API credentials (this is where the API key, secret, and passphrase are generated)
    try:
        api_creds = client.create_or_derive_api_creds()
        print("API Key:", api_creds.api_key)
        print("Secret:", api_creds.api_secret)
        print("Passphrase:", api_creds.api_passphrase)
        # You should now save these securely (e.g., store them in your .env file)
    except Exception as e:
        print("Error creating or deriving API credentials:", e)
if __name__ == "__main__":
    main()