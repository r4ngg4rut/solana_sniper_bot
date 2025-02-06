import os
import re
import requests
import time
import sqlite3
import snscrape.modules.twitter as sntwitter
import pandas as pd
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from solana.system_program import CreateAccountParams, create_account
from solana.publickey import PublicKey
from solana.transaction import Transaction
from solana.rpc.api import Client
from dotenv import load_dotenv
import websocket
import json
import threading

# Load environment variables
load_dotenv()

# Configurations
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v4/swap"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/search?q="
SOLSNIFFER_API = "https://solsniffer.com/api/score/"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Solana Wallet Setup
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
client = Client(SOLANA_RPC_URL)
wallet = Account(bytes.fromhex(PRIVATE_KEY))

# Create SQLite Database
conn = sqlite3.connect("solana_memecoins.db")
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS memecoins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    symbol TEXT,
    contract_address TEXT,
    dex_url TEXT,
    price REAL,
    volume REAL,
    liquidity REAL,
    market_cap REAL,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()

# Telegram Alert Function
def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    requests.post(url, data=data)

# Extract Tickers & Contract Addresses from Tweets
def extract_tickers_and_contracts(text):
    tickers = re.findall(r'\$[A-Z]{2,5}', text)
    contracts = re.findall(r'[1-9A-HJ-NP-Za-km-z]{44}', text)  # Solana contract pattern
    return tickers, contracts

# Scrape Twitter for Memecoin Mentions
def scrape_twitter_for_memecoins(kol_usernames):
    tweets = []
    analyzer = SentimentIntensityAnalyzer()

    for username in kol_usernames:
        for tweet in sntwitter.TwitterSearchScraper(f'from:{username}').get_items():
            tickers, contracts = extract_tickers_and_contracts(tweet.content)
            sentiment = analyzer.polarity_scores(tweet.content)['compound']
            
            if tickers or contracts:
                tweets.append({
                    "username": username,
                    "content": tweet.content,
                    "tickers": tickers,
                    "contracts": contracts,
                    "sentiment": sentiment
                })
    return tweets

# Fetch DexScreener Data
def fetch_dexscreener_data(contract_address):
    response = requests.get(DEXSCREENER_API + contract_address)
    if response.status_code == 200:
        return response.json().get("pairs", [])[0]
    return None

# Store Memecoin Data in Database
def store_memecoin(data):
    conn = sqlite3.connect("solana_memecoins.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO memecoins (name, symbol, contract_address, dex_url, price, volume, liquidity, market_cap)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["baseToken"]["name"],
        data["baseToken"]["symbol"],
        data["baseToken"]["address"],
        data["url"],
        float(data["priceUsd"]) if data["priceUsd"] else None,
        float(data["volume"]["h24"]) if data["volume"]["h24"] else None,
        float(data["liquidity"]["usd"]) if data["liquidity"]["usd"] else None,
        float(data["fdv"]) if data["fdv"] else None
    ))
    conn.commit()
    conn.close()

# Get SolSniffer Contract Score
def get_sol_sniffer_score(contract_address):
    response = requests.get(SOLSNIFFER_API + contract_address)
    if response.status_code == 200:
        return response.json().get("score", 0)
    return None

# Buy Tokens (Sniping)
def snipe_token(token_address):
    payload = {
        "inputMint": "So11111111111111111111111111111111111111112",  # SOL Mint Address
        "outputMint": token_address,
        "amount": int(0.01 * 1e9),  # 0.01 SOL in lamports
        "slippageBps": 1500,  # 15% slippage
    }

    response = requests.post(JUPITER_SWAP_API, json=payload)
    swap_data = response.json()

    if "data" in swap_data:
        txn = Transaction.deserialize(bytes.fromhex(swap_data["data"]["swapTransaction"]))
        txn.sign(wallet)
        client.send_transaction(txn, wallet)
        print(f"✅ Sniped {token_address} successfully!")
    else:
        print(f"❌ Sniping failed for {token_address}.")

# Sell Tokens (80% Sell, 20% Moonbag)
def take_profit(token_address, balance):
    amount_to_sell = int(balance * 0.8 * 1e9)
    payload = {
        "inputMint": token_address,
        "outputMint": "So11111111111111111111111111111111111111112",
        "amount": amount_to_sell,
        "slippageBps": 1500,
    }

    response = requests.post(JUPITER_SWAP_API, json=payload)
    swap_data = response.json()

    if "data" in swap_data:
        txn = Transaction.deserialize(bytes.fromhex(swap_data["data"]["swapTransaction"]))
        txn.sign(wallet)
        client.send_transaction(txn, wallet)
        print(f"✅ Sold 80% of {token_address}, keeping 20% moonbag!")
    else:
        print(f"❌ Sell failed for {token_address}.")

# WebSocket for DexScreener real-time price updates
def on_message(ws, message):
    data = json.loads(message)
    try:
        pair_data = data["data"]
        contract_address = pair_data["pair"]["baseToken"]["address"]
        price = float(pair_data["priceUsd"])
        print(f"Real-time price for {contract_address}: ${price}")
        monitor_price(contract_address, price)  # Call function to monitor price
    except KeyError:
        print("Error parsing real-time data.")

def on_error(ws, error):
    print("Error:", error)

def on_close(ws, close_status_code, close_msg):
    print("WebSocket closed.")

def on_open(ws):
    print("WebSocket connected. Subscribing to price updates...")
    subscription_message = {
        "type": "subscribe",
        "channel": "pairs",  # Subscribe to pair price updates
        "symbols": ["SOL-USDT", "SOL-USD"]  # Example pair (can add more)
    }
    ws.send(json.dumps(subscription_message))

def run_websocket():
    ws = websocket.WebSocketApp("wss://api.dexscreener.com/ws", 
                                on_message=on_message,
                                on_error=on_error,
                                on_close=on_close)
    ws.on_open = on_open
    ws.run_forever()

# Run WebSocket in a separate thread
def start_real_time_monitoring():
    websocket_thread = threading.Thread(target=run_websocket)
    websocket_thread.daemon = True
    websocket_thread.start()

# Target price for token (example: sell if price >= 2x initial price)
target_price_multiplier = 2.0
initial_price = None

# Monitor price
def monitor_price(contract_address, current_price):
    global initial_price

    if not initial_price:
        initial_price = current_price  # Set initial price when first seen
    
    print(f"Monitoring {contract_address} | Current Price: ${current_price}")

    if current_price >= initial_price * target_price_multiplier:
        print(f"🚀 {contract_address} price target reached! Taking profit...")
        balance = get_wallet_balance(contract_address)
        take_profit(contract_address, balance)
        initial_price = None  # Reset initial price after selling

# Get wallet balance for a specific token (assumed)
def get_wallet_balance(contract_address):
    # Placeholder: Assume fetching wallet balance for token
    return 100  # Example value: 100 tokens

# Main Function
def auto_snipe():
    kol_usernames = ["CryptoNobler", "0xChiefy", "Danny_Crypton", "DefiWimar"]
    tweets = scrape_twitter_for_memecoins(kol_usernames)

    for tweet in tweets:
        for contract in tweet["contracts"]:
            score = get_sol_sniffer_score(contract)
            if score and score < 85:
                send_telegram_alert(f"⚠️ Warning! Contract {contract} has a low SolSniffer score: {score}.")
                continue
            
            # Snipe token
            snipe_token(contract)
            start_real_time_monitoring()  # Start real-time price monitoring

# Run the bot
auto_snipe()
