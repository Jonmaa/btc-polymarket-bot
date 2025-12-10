# Bitcoin 15min Arbitrage Bot - Polymarket

Simple arbitrage bot implementing **Jeremy Whittaker's strategy** for Bitcoin 15-minute markets on Polymarket.

## 🎯 Strategy

**Pure arbitrage**: Buy both sides (UP + DOWN) when total cost < $1.00 to guarantee profit regardless of outcome.

### Example:
```
BTC goes up (UP):     $0.48
BTC goes down (DOWN): $0.51
─────────────────────────
Total:                $0.99  ✅ < $1.00
Profit:               $0.01 per share (1.01%)
```

**Why does it work?**
- At close, ONE of the two sides pays $1.00 per share
- If you paid $0.99 total, you earn $0.01 no matter which side wins
- It's **guaranteed profit** (pure arbitrage)

---

## 🚀 Installation

### 1. Clone the repository:
```bash
git clone https://github.com/Jonmaa/btc-polymarket-bot.git
cd btc-polymarket-bot
```

### 2. Create virtual environment and install dependencies:
```bash
python -m venv .venv
.\.venv\Scripts\activate  # Windows
# or: source .venv/bin/activate  # Linux/Mac
pip install -r requirements.txt
```

### 3. Configure environment variables:

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Then configure each variable (see detailed explanation below).

---

## 🔐 Environment Variables (.env)

### Required Variables

| Variable | Description | How to Get It |
|----------|-------------|---------------|
| `POLYMARKET_PRIVATE_KEY` | Your wallet's private key (starts with `0x`) | Export from your wallet (MetaMask, etc.) or use the one linked to your Polymarket account |
| `POLYMARKET_API_KEY` | API key for Polymarket CLOB | Run `python -m src.generate_api_key` |
| `POLYMARKET_API_SECRET` | API secret for Polymarket CLOB | Run `python -m src.generate_api_key` |
| `POLYMARKET_API_PASSPHRASE` | API passphrase for Polymarket CLOB | Run `python -m src.generate_api_key` |

### Wallet Configuration

| Variable | Description | Value |
|----------|-------------|-------|
| `POLYMARKET_SIGNATURE_TYPE` | Type of wallet signature | `0` = EOA (MetaMask, hardware wallet)<br>`1` = Magic.link (email login on Polymarket)<br>`2` = Gnosis Safe |
| `POLYMARKET_FUNDER` | Proxy wallet address (only for Magic.link users) | Leave **empty** for EOA wallets. For Magic.link, go to your profile https://polymarket.com/@{}, and click 'Copy address' |

### Trading Configuration

| Variable | Description | Default | Recommended |
|----------|-------------|---------|-------------|
| `TARGET_PAIR_COST` | Maximum combined cost to trigger arbitrage | `0.991` | `0.99` - `0.995` |
| `ORDER_SIZE` | Number of shares per trade (minimum is 5) | `5` | Start with `5`, increase after testing |
| `DRY_RUN` | Simulation mode | `true` | Start with `true`, change to `false` for live trading |

### Optional

| Variable | Description |
|----------|-------------|
| `POLYMARKET_MARKET_SLUG` | Force a specific market slug (leave empty for auto-discovery) |

---

## 🔑 Generating API Keys

Before running the bot, you need to generate your Polymarket API credentials.

### Step 1: Set your private key

Edit `.env` and add your private key:
```env
POLYMARKET_PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
```

### Step 2: Run the API key generator

```bash
python -m src.generate_api_key
```

This will output something like:
```
API Key: abc123...
Secret: xyz789...
Passphrase: mypassphrase
```

### Step 3: Add the credentials to `.env`

```env
POLYMARKET_API_KEY=abc123...
POLYMARKET_API_SECRET=xyz789...
POLYMARKET_API_PASSPHRASE=mypassphrase
```

> ⚠️ **Important**: The API credentials are derived from your private key. If you change the private key, you'll need to regenerate the API credentials.

---

## 💰 Checking Your Balance

Before trading, verify that your wallet is configured correctly and has funds:

```bash
python -m src.test_balance
```

This will show:
```
======================================================================
POLYMARKET BALANCE TEST
======================================================================
Host: https://clob.polymarket.com
Signature Type: 1
Private Key: ✓
API Key: ✓
API Secret: ✓
API Passphrase: ✓
======================================================================

1. Creating ClobClient...
   ✓ Client created

2. Deriving API credentials from private key...
   ✓ Credentials configured

3. Getting wallet address...
   ✓ Address: 0x52e78F6071719C...

4. Getting USDC balance (COLLATERAL)...
   💰 BALANCE USDC: $25.123456

5. Verifying balance directly on Polygon...
   🔗 Balance on-chain: $25.123456

======================================================================
TEST COMPLETED
======================================================================
```

> ⚠️ If balance shows `$0.00` but you have funds on Polymarket, check your `POLYMARKET_SIGNATURE_TYPE` and `POLYMARKET_FUNDER` settings.

---

## 💻 Usage

### Simulation mode (recommended first):

Make sure `DRY_RUN=true` in `.env`, then:
```bash
python -m src.simple_arb_bot
```

The bot will scan for opportunities but won't place real orders.

### Live trading mode:

1. Change `DRY_RUN=false` in `.env`
2. Ensure you have USDC in your Polymarket wallet
3. Run:
```bash
python -m src.simple_arb_bot
```

---

## 📊 Features

✅ **Auto-discovers** active BTC 15min market  
✅ **Detects opportunities** when price_up + price_down < threshold  
✅ **Continuous scanning** with no delays (maximum speed)  
✅ **Auto-switches** to next market when current one closes  
✅ **Final summary** with total investment, profit and market result  
✅ **Simulation mode** for risk-free testing  
✅ **Balance verification** before executing trades  

---

## 📈 Example Output

```
🚀 BITCOIN 15MIN ARBITRAGE BOT STARTED
======================================================================
Market: btc-updown-15m-1765301400
Time remaining: 12m 34s
Mode: 🔸 SIMULATION
Cost threshold: $0.99
Order size: 5 shares
======================================================================

[Scan #1] 12:34:56
No arbitrage: UP=$0.48 + DOWN=$0.52 = $1.00 (needs < $0.99)

🎯 ARBITRAGE OPPORTUNITY DETECTED
======================================================================
UP price (goes up):   $0.4800
DOWN price (goes down): $0.5100
Total cost:           $0.9900
Profit per share:     $0.0100
Profit %:             1.01%
----------------------------------------------------------------------
Order size:           5 shares each side
Total investment:     $4.95
Expected payout:      $5.00
EXPECTED PROFIT:      $0.05
======================================================================
✅ ARBITRAGE EXECUTED SUCCESSFULLY

🏁 MARKET CLOSED - FINAL SUMMARY
======================================================================
Market: btc-updown-15m-1765301400
Result: UP (goes up) 📈
Mode: 🔴 REAL TRADING
----------------------------------------------------------------------
Total opportunities detected:  3
Total trades executed:         3
Total shares bought:           30
----------------------------------------------------------------------
Total invested:                $14.85
Expected payout at close:      $15.00
Expected profit:               $0.15 (1.01%)
======================================================================
```

---

## 📁 Project Structure

```
Bot/
├── src/
│   ├── simple_arb_bot.py    # Main arbitrage bot
│   ├── config.py            # Configuration loader
│   ├── lookup.py            # Market ID fetcher
│   ├── trading.py           # Order execution
│   ├── generate_api_key.py  # API key generator utility
│   └── test_balance.py      # Balance verification utility
├── tests/
│   └── test_state.py        # Unit tests
├── .env                     # Environment variables (create from .env.example)
├── .env.example             # Environment template
├── requirements.txt         # Dependencies
└── README.md                # This file
```

---

## ⚠️ Warnings

- ⚠️ **DO NOT use `DRY_RUN=false` without funds** in your Polymarket wallet
- ⚠️ **Spreads** can eliminate profit (verify liquidity)
- ⚠️ Markets close every **15 minutes** (don't accumulate positions)
- ⚠️ Start with **small orders** (ORDER_SIZE=5)
- ⚠️ This software is **educational only** - use at your own risk
- ⚠️ **Never share your private key** with anyone

---

## 🔧 Troubleshooting

### "Invalid signature" error
- Verify `POLYMARKET_SIGNATURE_TYPE` matches your wallet type
- For Magic.link users: set `POLYMARKET_FUNDER=0x9D80964BDB2eB1D7106c2D2E8eAffB9F3e5D6Fb1`
- Regenerate API credentials with `python -m src.generate_api_key`

### Balance shows $0 but I have funds
- Check that your private key corresponds to the wallet with funds
- For Magic.link: the private key is for your EOA, not the proxy wallet
- Run `python -m src.test_balance` to see your wallet address

### "No active BTC 15min market found"
- Markets open every 15 minutes; wait for the next one
- Check your internet connection
- Try visiting https://polymarket.com/crypto/15M manually

---

## 📚 Resources

- [Jeremy Whittaker's original article](https://jeremywhittaker.com/index.php/2024/09/24/arbitrage-in-polymarket-com/)
- [Polymarket](https://polymarket.com/)
- [BTC 15min Markets](https://polymarket.com/crypto/15M)
- [py-clob-client documentation](https://github.com/Polymarket/py-clob-client)

---

## ⚖️ Disclaimer

This software is for educational purposes only. Trading involves risk. I am not responsible for financial losses. Always do your own research and never invest more than you can afford to lose.
