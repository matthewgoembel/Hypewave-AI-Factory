"""
One-time script: approve Polymarket's CLOB contracts to spend USDC
from the replayer wallet. Run this once before going live.
"""

import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY    = os.getenv("POLYMARKET_WALLET_PRIVATE_KEY")
WALLET         = Web3.to_checksum_address(os.getenv("POLYMARKET_REPLAYER_ADDRESS"))

# Polygon RPC — tries multiple public endpoints
RPCS = [
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon.llamarpc.com",
]
w3 = None
for rpc in RPCS:
    try:
        _w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
        if _w3.is_connected():
            w3 = _w3
            print(f"Connected via {rpc}")
            break
    except Exception:
        continue

if w3 is None:
    print("ERROR: Could not connect to any Polygon RPC. Make sure NordVPN is on.")
    sys.exit(1)

# Native USDC on Polygon (the correct one — not USDC.e)
USDC_ADDRESS = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")

# Polymarket CLOB contracts that need USDC approval
CONTRACTS = {
    "CTF Exchange":          "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "Neg Risk CTF Exchange": "0xC5d563A36AE78145C45a50134d48A1a16f5853c6",
}

ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

MAX_UINT = 2**256 - 1
usdc = w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)

# --- Status check ---
usdc_balance = usdc.functions.balanceOf(WALLET).call()
pol_balance  = w3.eth.get_balance(WALLET)
print(f"\nWallet:       {WALLET}")
print(f"USDC balance: ${usdc_balance / 1e6:.4f}")
print(f"POL balance:  {w3.from_wei(pol_balance, 'ether'):.4f} POL")

if pol_balance == 0:
    print("\nERROR: No POL for gas. Send at least 0.5 POL to this wallet first.")
    sys.exit(1)

if usdc_balance == 0:
    print("\nWARNING: USDC balance is 0. Approvals will still run but nothing to trade.")

# --- Approve each contract ---
print()
nonce = w3.eth.get_transaction_count(WALLET)

for name, addr in CONTRACTS.items():
    spender = Web3.to_checksum_address(addr)

    # Check if already approved
    existing = usdc.functions.allowance(WALLET, spender).call()
    if existing > 10**24:
        print(f"[{name}] Already approved (allowance: {existing:.2e}) — skipping.")
        continue

    print(f"[{name}] Approving...")
    txn = usdc.functions.approve(spender, MAX_UINT).build_transaction({
        "from":     WALLET,
        "nonce":    nonce,
        "gas":      100_000,
        "gasPrice": w3.eth.gas_price,
    })
    signed   = w3.eth.account.sign_transaction(txn, PRIVATE_KEY)
    tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  TX sent: {tx_hash.hex()}")
    receipt  = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    status   = "OK" if receipt.status == 1 else "FAILED"
    print(f"  Status: {status}")
    nonce += 1

print("\nDone. Now run: python engine/executor.py")
print("You should see your USDC balance > $0.\n")
