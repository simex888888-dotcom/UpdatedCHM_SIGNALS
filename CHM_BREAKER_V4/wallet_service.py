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


async def get_matic_balance(address: str) -> float:
    """Читает баланс MATIC на Polygon через JSON-RPC."""
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": [address, "latest"],
        "id": 1,
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as s:
            async with s.post(_POLYGON_RPC, json=payload) as r:
                resp = await r.json(content_type=None)
        result = resp.get("result", "0x0") or "0x0"
        return int(result, 16) / 1e18
    except Exception as exc:
        log.warning(f"get_matic_balance({address[:10]}…): {exc}")
        return 0.0


async def transfer_usdc(
    from_address: str,
    private_key: str,
    to_address: str,
    amount_usdc: float,
) -> str:
    """
    Переводит USDC (Polygon) с кастодиального кошелька на внешний адрес.
    Возвращает хэш транзакции (0x...).
    Требует наличия MATIC на кошельке для оплаты газа.
    """
    from eth_account import Account

    amount_units = int(round(amount_usdc * 1_000_000))  # 6 decimals

    # transfer(address,uint256) selector = 0xa9059cbb
    to_padded     = to_address.lower().removeprefix("0x").zfill(64)
    amount_hex    = hex(amount_units)[2:].zfill(64)
    calldata      = "0xa9059cbb" + to_padded + amount_hex

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as s:

        async def _rpc(method, params, id_=1):
            async with s.post(_POLYGON_RPC, json={
                "jsonrpc": "2.0", "method": method, "params": params, "id": id_,
            }) as r:
                return await r.json(content_type=None)

        nonce_r  = await _rpc("eth_getTransactionCount", [from_address, "latest"])
        nonce    = int(nonce_r["result"], 16)

        gp_r     = await _rpc("eth_gasPrice", [])
        gas_price = int(int(gp_r["result"], 16) * 1.3)  # +30% буфер

        est_r    = await _rpc("eth_estimateGas", [{
            "from": from_address,
            "to":   _USDC_CONTRACT,
            "data": "0x" + calldata,
        }])
        if "error" in est_r:
            raise RuntimeError(f"Gas estimation error: {est_r['error'].get('message', est_r['error'])}")
        gas_limit = int(int(est_r["result"], 16) * 1.3)

        chain_r  = await _rpc("eth_chainId", [])
        chain_id = int(chain_r["result"], 16)

    tx = {
        "nonce":    nonce,
        "gasPrice": gas_price,
        "gas":      gas_limit,
        "to":       _USDC_CONTRACT,
        "value":    0,
        "data":     "0x" + calldata,
        "chainId":  chain_id,
    }
    account = Account.from_key(private_key)
    signed  = account.sign_transaction(tx)
    raw_hex = "0x" + signed.raw_transaction.hex()

    timeout2 = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout2) as s:
        send_r = await s.post(_POLYGON_RPC, json={
            "jsonrpc": "2.0",
            "method":  "eth_sendRawTransaction",
            "params":  [raw_hex],
            "id": 1,
        })
        resp = await send_r.json(content_type=None)

    if "error" in resp:
        msg = resp["error"].get("message", str(resp["error"]))
        raise RuntimeError(f"Transaction rejected: {msg}")

    return resp["result"]  # tx hash


def is_configured() -> bool:
    """Проверяет, задан ли WALLET_ENCRYPTION_KEY."""
    return bool(os.getenv("WALLET_ENCRYPTION_KEY"))
