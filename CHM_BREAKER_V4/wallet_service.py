"""
wallet_service.py — кастодиальные кошельки Polygon для Polymarket.

Каждый пользователь получает уникальный Polygon-кошелёк.
Приватные ключи хранятся в БД в зашифрованном виде (Fernet AES-128-CBC).
Ключ НИКОГДА не логируется и не остаётся в памяти дольше чем нужно для подписи.

Env:
  WALLET_ENCRYPTION_KEY — Fernet-ключ (base64, 32 байта)
    Сгенерировать: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  POLYGON_RPC — HTTP JSON-RPC Polygon (по умолчанию: https://polygon-rpc.com)
"""

import logging
import os
import secrets

import aiohttp

log = logging.getLogger("CHM.Wallet")

# USDC (native) на Polygon Mainnet
_USDC_CONTRACT = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
_POLYGON_RPC   = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")


def _fernet():
    from cryptography.fernet import Fernet
    key = os.getenv("WALLET_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError(
            "WALLET_ENCRYPTION_KEY не задан. "
            "Сгенерируй: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


def generate_wallet() -> dict:
    """Генерирует новый Polygon-кошелёк. Возвращает {address, private_key}."""
    from eth_account import Account
    acc = Account.create(secrets.token_hex(32))
    return {"address": acc.address, "private_key": acc.key.hex()}


def encrypt_key(private_key: str) -> str:
    """Шифрует приватный ключ для хранения в БД."""
    return _fernet().encrypt(private_key.encode()).decode()


def decrypt_key(encrypted_key: str) -> str:
    """
    Расшифровывает приватный ключ.
    ВАЖНО: использовать только в момент подписи транзакции,
    результат немедленно выбрасывать после использования.
    """
    return _fernet().decrypt(encrypted_key.encode()).decode()


async def get_usdc_balance(address: str) -> float:
    """
    Читает баланс USDC (Polygon native USDC) через JSON-RPC.
    Не требует web3 — использует прямой JSON-RPC вызов.
    """
    # balanceOf(address) selector = 0x70a08231
    addr_hex = address.lower().removeprefix("0x").zfill(64)
    data = "0x70a08231" + addr_hex
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": _USDC_CONTRACT, "data": data}, "latest"],
        "id": 1,
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as s:
            async with s.post(_POLYGON_RPC, json=payload) as r:
                resp = await r.json(content_type=None)
        result = resp.get("result", "0x0") or "0x0"
        return int(result, 16) / 1_000_000  # USDC = 6 decimals
    except Exception as exc:
        log.warning(f"get_usdc_balance({address[:10]}…): {exc}")
        return 0.0


def is_configured() -> bool:
    """Проверяет, задан ли WALLET_ENCRYPTION_KEY."""
    return bool(os.getenv("WALLET_ENCRYPTION_KEY"))
