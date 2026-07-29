import logging
import time
from eth_account import Account
from eth_account.messages import encode_defunct

logger = logging.getLogger("auth")


def get_address_from_private_key(private_key: str) -> str:
    account = Account.from_key(private_key)
    return account.address


def create_ws_auth_payload(
    private_key: str,
    chain_id: int = 137,
) -> dict:
    account = Account.from_key(private_key)
    address = account.address
    timestamp = int(time.time())

    # L1 auth message format from py-clob-client-v2
    message = f"polymarket-clob-ws-v2\n{address}\n{timestamp}"

    # EIP-191 standard sign (encode_defunct wraps with \x19Ethereum Signed Message:\n...)
    signable = encode_defunct(text=message)
    signed = Account.sign_message(signable, private_key=private_key)
    signature = "0x" + signed.signature.hex()

    logger.info(
        "L1 WebSocket auth payload: address=%s timestamp=%d chain_id=%d",
        address, timestamp, chain_id,
    )
    logger.debug(
        "L1 auth signed message: %s | signature: %s",
        message, signature,
    )

    return {
        "type": "auth",
        "address": address,
        "signature": signature,
    }
