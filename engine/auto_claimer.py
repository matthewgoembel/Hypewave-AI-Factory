"""
Auto-claimer for Polymarket resolved positions.
Polls every 60s for winning positions, redeems them on-chain via the proxy wallet.
Recycles USDC back so the copy trader can keep buying.
Run: python engine/auto_claimer.py
"""

import sys, os, time, requests, logging
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from web3 import Web3
from eth_abi import encode as abi_encode
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/auto_claimer.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("auto_claimer")

# ── Config ────────────────────────────────────────────────────────────────────
PRIVATE_KEY      = os.getenv("POLYMARKET_WALLET_PRIVATE_KEY")
SIGNER_ADDRESS   = Web3.to_checksum_address(os.getenv("POLYMARKET_REPLAYER_ADDRESS"))
PROXY_WALLET     = Web3.to_checksum_address(os.getenv("POLYMARKET_ACCOUNT_ADRESS"))
USDC_ADDRESS     = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
CTF_ADDRESS      = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
NEG_RISK_ADDRESS = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

CHECK_INTERVAL   = 60    # seconds between scans
MIN_CLAIM_USD    = 0.50  # skip dust positions

RPCS = [
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon.llamarpc.com",
]

# ── ABIs ──────────────────────────────────────────────────────────────────────
PROXY_WALLET_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "typeCode", "type": "uint8"},
                    {"name": "to",       "type": "address"},
                    {"name": "value",    "type": "uint256"},
                    {"name": "data",     "type": "bytes"},
                ],
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "proxy",
        "outputs": [{"name": "returnValues", "type": "bytes[]"}],
        "stateMutability": "payable",
        "type": "function",
    }
]

CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken",    "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",        "type": "bytes32"},
            {"name": "indexSets",          "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id",      "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

PROXY_WALLET_OWNER_ABI = [
    {
        "inputs": [],
        "name": "owner",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]

NEG_RISK_ABI = [
    {
        "inputs": [
            {"name": "_conditionId", "type": "bytes32"},
            {"name": "_amounts",     "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# ── ABI encoding (works across all web3.py versions) ─────────────────────────
def encode_call(fn_signature: str, types: list, args: list) -> bytes:
    """Encode a contract call: 4-byte selector + ABI-encoded args."""
    selector = Web3.keccak(text=fn_signature)[:4]
    return selector + abi_encode(types, args)


# ── Web3 ──────────────────────────────────────────────────────────────────────
def connect_web3():
    for rpc in RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                log.info(f"Connected via {rpc}")
                return w3
        except Exception:
            continue
    return None


# ── Positions API ─────────────────────────────────────────────────────────────
def get_redeemable():
    """Return positions that have resolved as winners (curPrice = 1.0)."""
    try:
        r = requests.get(
            f"https://data-api.polymarket.com/positions?user={PROXY_WALLET}&redeemable=true",
            timeout=10,
        )
        positions = r.json()
        if not isinstance(positions, list):
            return []
        winners = [p for p in positions if p.get("curPrice", 0) >= 0.99]
        return winners
    except Exception as e:
        log.error(f"API error fetching positions: {e}")
        return []


# ── Redemption ────────────────────────────────────────────────────────────────
def redeem(w3, proxy_contract, ctf_contract, neg_risk_contract, position):
    """
    Submit on-chain claim for one resolved winning position.
    Routes to CTF or NegRiskAdapter depending on position type.
    """
    condition_id  = position.get("conditionId", "")
    outcome_index = position.get("outcomeIndex", 0)
    is_neg_risk   = position.get("negativeRisk", False)
    title         = position.get("title", "")[:50]
    value         = position.get("currentValue", 0)
    asset_id      = position.get("asset", "")

    if value < MIN_CLAIM_USD:
        log.info(f"Skipping {title} — ${value:.2f} below threshold")
        return False

    try:
        cid_bytes = bytes.fromhex(condition_id.replace("0x", "").zfill(64))
    except Exception:
        log.error(f"Bad conditionId: {condition_id}")
        return False

    # Verify the condition is actually resolved on-chain before attempting redemption.
    # The API can show redeemable=true before the on-chain report is confirmed.
    try:
        denom = ctf_contract.functions.payoutDenominator(cid_bytes).call()
        if denom == 0:
            log.info(f"Condition not yet resolved on-chain for {title}, waiting...")
            return False
    except Exception as e:
        log.warning(f"Could not check payoutDenominator: {e}")

    try:
        if is_neg_risk:
            # NegRisk market — get exact on-chain balance then call NegRiskAdapter
            token_id = int(asset_id)
            balance  = ctf_contract.functions.balanceOf(PROXY_WALLET, token_id).call()
            if balance == 0:
                log.info(f"NegRisk balance 0 for {title}, skipping")
                return False
            yes_amt  = balance if outcome_index == 0 else 0
            no_amt   = balance if outcome_index == 1 else 0
            call_data = encode_call(
                "redeemPositions(bytes32,uint256[])",
                ["bytes32", "uint256[]"],
                [cid_bytes, [yes_amt, no_amt]],
            )
            target = NEG_RISK_ADDRESS

        else:
            # Standard CTF — redeems all tokens for this indexSet automatically
            index_set = 1 << outcome_index   # outcome 0 → 1, outcome 1 → 2
            call_data = encode_call(
                "redeemPositions(address,bytes32,bytes32,uint256[])",
                ["address", "bytes32", "bytes32", "uint256[]"],
                [USDC_ADDRESS, b"\x00" * 32, cid_bytes, [index_set]],
            )
            target = CTF_ADDRESS

        # Wrap in ProxyWallet.proxy() so it runs as the proxy wallet
        proxy_call = (
            1,                                  # typeCode = CALL
            Web3.to_checksum_address(target),
            0,                                  # value = 0 ETH
            call_data,
        )

        nonce     = w3.eth.get_transaction_count(SIGNER_ADDRESS)
        gas_price = w3.eth.gas_price

        txn = proxy_contract.functions.proxy([proxy_call]).build_transaction({
            "from":     SIGNER_ADDRESS,
            "nonce":    nonce,
            "gas":      300_000,
            "gasPrice": int(gas_price * 1.2),  # 20% tip to land faster
        })

        signed  = w3.eth.account.sign_transaction(txn, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

        print(f"\n[CLAIM] Submitted → {title}")
        print(f"[CLAIM] ${value:.2f} | TX: {tx_hash.hex()}")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status == 1:
            print(f"[CLAIM] ✓ SUCCESS — ${value:.2f} USDC back in wallet!\n")
            log.info(f"Claimed ${value:.2f} for {title}")
            return True
        else:
            print(f"[CLAIM] ✗ TX reverted for {title}")
            log.error(f"TX reverted: {tx_hash.hex()}")
            return False

    except Exception as e:
        log.error(f"Redeem failed for {title}: {e}")
        print(f"[CLAIM] Error on {title}: {e}")
        return False


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    os.makedirs("logs", exist_ok=True)

    w3 = connect_web3()
    if not w3:
        print("ERROR: Could not connect to Polygon RPC. Turn on NordVPN.")
        return

    proxy_contract    = w3.eth.contract(address=PROXY_WALLET,     abi=PROXY_WALLET_ABI)
    ctf_contract      = w3.eth.contract(address=CTF_ADDRESS,      abi=CTF_ABI)
    neg_risk_contract = w3.eth.contract(address=NEG_RISK_ADDRESS, abi=NEG_RISK_ABI)

    # Diagnostic: show who owns the proxy wallet
    try:
        owner_contract = w3.eth.contract(address=PROXY_WALLET, abi=PROXY_WALLET_OWNER_ABI)
        owner = owner_contract.functions.owner().call()
        is_owner = owner.lower() == SIGNER_ADDRESS.lower()
        print(f"[AUTO-CLAIMER] Proxy wallet owner: {owner}")
        print(f"[AUTO-CLAIMER] Signer address:     {SIGNER_ADDRESS}")
        print(f"[AUTO-CLAIMER] Signer is owner:    {is_owner}")
        if not is_owner:
            print("[AUTO-CLAIMER] WARNING: Signer is NOT the proxy wallet owner — claims will fail!")
    except Exception as e:
        print(f"[AUTO-CLAIMER] Could not check owner: {e}")

    print(f"[AUTO-CLAIMER] Watching {PROXY_WALLET[:12]}...")
    print(f"[AUTO-CLAIMER] Checking every {CHECK_INTERVAL}s. Ctrl+C to stop.\n")

    claimed_ids = set()  # conditionIds claimed this session (avoid double-spend)

    while True:
        try:
            # Reconnect if dropped
            if not w3.is_connected():
                w3 = connect_web3()
                if not w3:
                    log.error("RPC lost, retrying next cycle.")
                    time.sleep(CHECK_INTERVAL)
                    continue
                proxy_contract    = w3.eth.contract(address=PROXY_WALLET,     abi=PROXY_WALLET_ABI)
                ctf_contract      = w3.eth.contract(address=CTF_ADDRESS,      abi=CTF_ABI)
                neg_risk_contract = w3.eth.contract(address=NEG_RISK_ADDRESS, abi=NEG_RISK_ABI)

            winners = get_redeemable()

            if not winners:
                log.info("No redeemable positions.")
            else:
                total = sum(p.get("currentValue", 0) for p in winners)
                new   = [p for p in winners if p.get("conditionId") not in claimed_ids]
                print(f"[AUTO-CLAIMER] {len(new)} new positions to claim (${total:.2f})")

                for p in new:
                    cid = p.get("conditionId", "")
                    ok  = redeem(w3, proxy_contract, ctf_contract, neg_risk_contract, p)
                    if ok:
                        claimed_ids.add(cid)
                    time.sleep(3)  # small gap between txs

        except Exception as e:
            log.error(f"Main loop error: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
