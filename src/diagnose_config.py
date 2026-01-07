"""
Diagnose wallet and API configuration issues.

Run with: python -m src.diagnose_config
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv(override=False)


def main():
    print("=" * 70)
    print("POLYMARKET BOT - CONFIGURATION DIAGNOSIS")
    print("=" * 70)
    print()

    # 1. Check environment variables
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
    funder = os.getenv("POLYMARKET_FUNDER", "")
    api_key = os.getenv("POLYMARKET_API_KEY", "")

    print("1. Environment Variables:")
    print(f"   POLYMARKET_PRIVATE_KEY: {'✓ Set' if private_key else '✗ Missing'}")
    print(f"   POLYMARKET_SIGNATURE_TYPE: {signature_type}")
    print(f"   POLYMARKET_FUNDER: {funder if funder else '(empty)'}")
    print(f"   POLYMARKET_API_KEY: {'✓ Set' if api_key else '✗ Missing'}")
    print()

    if not private_key:
        print("❌ ERROR: POLYMARKET_PRIVATE_KEY is required")
        sys.exit(1)

    # 2. Derive addresses
    print("2. Deriving addresses from private key...")
    try:
        from py_clob_client.client import ClobClient

        client = ClobClient(
            "https://clob.polymarket.com",
            key=private_key.strip(),
            chain_id=137,
            signature_type=signature_type,
            funder=funder.strip() if funder else None,
        )

        signer_address = client.get_address()
        print(f"   Signer address (from private key): {signer_address}")
        print(f"   Funder address (POLYMARKET_FUNDER): {funder if funder else '(same as signer)'}")
        print()

        # 3. Check for Magic.link configuration issues
        if signature_type == 1:
            print("3. Magic.link (signature_type=1) Check:")
            if not funder:
                print("   ⚠️  WARNING: POLYMARKET_FUNDER is empty!")
                print("      For Magic.link accounts, you MUST set POLYMARKET_FUNDER to your")
                print("      Polymarket proxy wallet address (found on your profile page).")
                print()
            elif funder.lower() == signer_address.lower():
                print("   ⚠️  WARNING: POLYMARKET_FUNDER equals your signer address!")
                print("      For Magic.link, the funder should be your Polymarket PROXY wallet,")
                print("      which is DIFFERENT from the signer address.")
                print("      Go to https://polymarket.com/@YOUR_USERNAME and copy the address there.")
                print()
            else:
                print("   ✓ POLYMARKET_FUNDER is set to a different address (good)")
                print()

        # 4. Get balance through API
        print("4. Checking USDC balance via Polymarket API...")
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            derived_creds = client.create_or_derive_api_creds()
            client.set_api_creds(derived_creds)

            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=signature_type,
            )
            result = client.get_balance_allowance(params)
            balance_raw = result.get("balance", "0") if isinstance(result, dict) else "0"
            balance_usdc = float(balance_raw) / 1_000_000
            print(f"   💰 Polymarket API Balance: ${balance_usdc:.6f}")
        except Exception as e:
            print(f"   ❌ Error getting balance: {e}")
        print()

        # 5. Try to check neg_risk detection for a sample token
        print("5. Testing neg_risk detection (for BTC 15min markets)...")
        try:
            # Use a known BTC 15min token pattern - we'll try to fetch one
            import httpx
            import re

            resp = httpx.get(
                "https://polymarket.com/crypto/15M",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            pattern = r'btc-updown-15m-(\d+)'
            matches = re.findall(pattern, resp.text)
            if matches:
                from .lookup import fetch_market_from_slug

                slug = f"btc-updown-15m-{matches[0]}"
                market_info = fetch_market_from_slug(slug)
                token_id = market_info.get("yes_token_id")
                if token_id:
                    neg_risk = client.get_neg_risk(token_id)
                    print(f"   Token: {token_id[:20]}...")
                    print(f"   neg_risk: {neg_risk}")
                    if neg_risk:
                        print("   ✓ BTC 15min markets are neg_risk=True (expected)")
                    else:
                        print("   ⚠️  neg_risk=False is unexpected for BTC 15min")
            else:
                print("   Could not find a sample BTC 15min market to test")
        except Exception as e:
            print(f"   Could not test neg_risk: {e}")
        print()

        # 6. Summary
        print("=" * 70)
        print("DIAGNOSIS SUMMARY")
        print("=" * 70)

        issues = []

        if signature_type == 1 and not funder:
            issues.append("POLYMARKET_FUNDER must be set for Magic.link accounts")

        if signature_type == 1 and funder and funder.lower() == signer_address.lower():
            issues.append(
                "POLYMARKET_FUNDER should be your Polymarket PROXY wallet, "
                "not your signer address"
            )

        if issues:
            print("❌ Issues found:")
            for issue in issues:
                print(f"   - {issue}")
            print()
            print("The 'invalid signature' error is likely caused by incorrect")
            print("POLYMARKET_FUNDER configuration.")
        else:
            print("✓ No obvious configuration issues detected.")
            print()
            print("If you still get 'invalid signature' errors:")
            print("  1. Regenerate API credentials: python -m src.generate_api_key")
            print("  2. Verify POLYMARKET_FUNDER is your Polymarket proxy wallet address")
            print("  3. Check that your account has trading enabled on Polymarket")

    except Exception as e:
        print(f"❌ Error during diagnosis: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
