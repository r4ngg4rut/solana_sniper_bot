import os
import re
import requests
import time
import sqlite3
import ssl
import json
import threading
import snscrape.modules.twitter as sntwitter
from base58 import b58decode
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from solana.rpc.api import Client
from solders.keypair import Keypair
from solana.rpc.types import TxOpts
from solana.transaction import Transaction
from dotenv import load_dotenv
import websocket

# Disable SSL verification globally (temporary fix for Railway SSL issues)
ssl._create_default_https_context = ssl._create_unverified_context

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
PRIVATE_KEY = os.getenv("SOL_PRIVATE_KEY")
client = Client(SOLANA_RPC_URL)

# Ensure the private key is present
if PRIVATE_KEY is None:
    print("Error: PRIVATE_KEY not found in environment variables.")
else:
    try:
        # Decode the Base58 private key into bytes
        decoded_key = b58decode(PRIVATE_KEY)
        
        # Create Keypair using the solders package (adjusted for 32-byte private keys)
        wallet = Keypair.from_secret_key(decoded_key)
        
        # Print wallet public key
        print(f"Wallet Address: {wallet.pubkey()}")
    except Exception as e:
        print(f"Error decoding private key: {e}")

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

# Monkey patch requests for snscrape to add headers and bypass SSL verification
original_request = requests.Session.request

def patched_request(self, method, url, *args, **kwargs):
    headers = kwargs.get('headers', {})
    headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    kwargs['headers'] = headers
    kwargs['verify'] = False  # Disable SSL verification for snscrape requests
    return original_request(self, method, url, *args, **kwargs)

requests.Session.request = patched_request

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
        try:
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
        except Exception as e:
            print(f"Error scraping tweets from {username}: {e}")

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

    if "swapTransaction" in swap_data:
        try:
            txn = Transaction.deserialize(bytes.fromhex(swap_data["swapTransaction"]))
            txn.sign(wallet)
            result = client.send_transaction(txn, wallet, opts=TxOpts(skip_confirmation=False))
            print(f"✅ Sniped {token_address} successfully! Transaction ID: {result}")
        except Exception as e:
            print(f"❌ Error sending transaction for {token_address}: {e}")
    else:
        print(f"❌ Sniping failed for {token_address}. No transaction data.")

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

# Run the bot
auto_snipe()
