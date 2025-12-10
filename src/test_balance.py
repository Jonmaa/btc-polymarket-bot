"""
Simple script to test Polymarket balance retrieval.
"""
import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

load_dotenv()

def main():
    # Load configuration
    host = "https://clob.polymarket.com"
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    api_key = os.getenv("POLYMARKET_API_KEY")
    api_secret = os.getenv("POLYMARKET_API_SECRET")
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE")
    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
    funder = os.getenv("POLYMARKET_FUNDER", "")
    
    print("=" * 70)
    print("POLYMARKET BALANCE TEST")
    print("=" * 70)
    print(f"Host: {host}")
    print(f"Signature Type: {signature_type}")
    print(f"Private Key: {'✓' if private_key else '✗'}")
    print(f"API Key: {'✓' if api_key else '✗'}")
    print(f"API Secret: {'✓' if api_secret else '✗'}")
    print(f"API Passphrase: {'✓' if api_passphrase else '✗'}")
    print("=" * 70)
    
    try:
        # Create client
        print("\n1. Creating ClobClient...")
        client = ClobClient(
            host,
            key=private_key,
            chain_id=137,
            signature_type=signature_type,
            funder=funder or None
        )
        print("   ✓ Client created")
        
        # Derive credentials from private key
        print("\n2. Deriving API credentials from private key...")
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        print(f"   ✓ API Key: {creds.api_key}")
        print(f"   ✓ Credentials configured")
        
        # Get wallet address
        print("\n3. Getting wallet address...")
        address = client.get_address()
        print(f"   ✓ Address: {address}")
        
        # Get balance - Method 1: COLLATERAL (USDC)
        print("\n4. Getting USDC balance (COLLATERAL)...")
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
            
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=signature_type
            )
            result = client.get_balance_allowance(params)
            print(f"   Response type: {type(result)}")
            print(f"   Response: {result}")
            
            if isinstance(result, dict):
                # Response should have 'balance' and 'allowance'
                balance_raw = result.get("balance", "0")
                balance_wei = float(balance_raw)
                # USDC has 6 decimals
                balance_usdc = balance_wei / 1_000_000
                
                print(f"\n   Balance raw: {balance_raw}")
                print(f"   Balance in wei: {balance_wei}")
                print(f"   💰 BALANCE USDC: ${balance_usdc:.6f}")
                
                # Verify balance directly on blockchain
                print("\n5. Verifying balance directly on Polygon...")
                try:
                    from web3 import Web3
                    # Connect to Polygon
                    w3 = Web3(Web3.HTTPProvider('https://polygon-rpc.com'))
                    
                    # USDC contract address on Polygon
                    usdc_address = Web3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
                    
                    # Minimal ABI for balanceOf
                    usdc_abi = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]
                    
                    usdc_contract = w3.eth.contract(address=usdc_address, abi=usdc_abi)
                    wallet_address = Web3.to_checksum_address(address)
                    
                    # Get real balance
                    balance_onchain = usdc_contract.functions.balanceOf(wallet_address).call()
                    balance_onchain_usdc = balance_onchain / 1_000_000
                    
                    print(f"   🔗 Balance on-chain: ${balance_onchain_usdc:.6f}")
                    
                except Exception as e:
                    print(f"   ⚠️ Could not verify on-chain: {e}")
            else:
                print(f"\n   ⚠️ Unexpected response: {result}")
        except Exception as e:
            print(f"   ✗ Error: {e}")
            import traceback
            traceback.print_exc()
        
        print("\n" + "=" * 70)
        print("TEST COMPLETED")
        print("=" * 70)
        
    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
