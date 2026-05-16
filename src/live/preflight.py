import logging
import os
from decimal import Decimal
from typing import Dict, List

from dotenv import load_dotenv
from eth_account import Account

logger = logging.getLogger("preflight")

REQUIRED_ENV_VARS: Dict[str, str] = {
    "PRIVATE_KEY": "Clave privada de la wallet (hex, 32 bytes)",
    "POLYMARKET_API_KEY": "API Key de Polymarket",
    "POLYMARKET_SECRET": "Secret de API (base64)",
    "POLYMARKET_PASSPHRASE": "Passphrase de API",
}


class PreflightResult:
    def __init__(self) -> None:
        self.passed: bool = True
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.env_status: Dict[str, str] = {}
        self.wallet_address: str = ""
        self.balance: Decimal = Decimal("0")


async def run_preflight() -> PreflightResult:
    result = PreflightResult()
    load_dotenv()

    for var, description in REQUIRED_ENV_VARS.items():
        value = os.getenv(var)
        if not value:
            result.errors.append(f"{var}: {description} — NO CONFIGURADO")
            result.passed = False
        else:
            result.env_status[var] = "OK"

    pk = os.getenv("PRIVATE_KEY", "")
    try:
        clean_pk = pk[2:] if pk.startswith("0x") else pk
        key_bytes = bytes.fromhex(clean_pk)
        if len(key_bytes) != 32:
            result.errors.append(f"PRIVATE_KEY: debe ser 32 bytes, recibidos {len(key_bytes)}")
            result.passed = False
        else:
            result.wallet_address = Account.from_key(clean_pk).address
    except (ValueError, AttributeError) as e:
        result.errors.append(f"PRIVATE_KEY: inválida — {e}")
        result.passed = False

    return result


def print_preflight_summary(result: PreflightResult) -> str:
    lines = [
        "=" * 50,
        "PRE-FLIGHT CHECKLIST",
        "=" * 50,
    ]
    for var, status in result.env_status.items():
        lines.append(f"  [OK] {var}")
    if result.wallet_address:
        lines.append(f"  [OK] Wallet: {result.wallet_address}")
    for err in result.errors:
        lines.append(f"  [FAIL] {err}")
    for warn in result.warnings:
        lines.append(f"  [WARN] {warn}")
    lines.append(f"\n  Resultado: {'PASSED' if result.passed else 'FAILED'}")
    lines.append(f"  Errores: {len(result.errors)}, Warnings: {len(result.warnings)}")
    lines.append("=" * 50)
    return "\n".join(lines)
