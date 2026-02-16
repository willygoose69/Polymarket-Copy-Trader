import os
import json
import requests
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any, Optional, Tuple

from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.exceptions import PolyApiException
from py_clob_client.clob_types import (
    OrderArgs,
    MarketOrderArgs,
    OrderType,
    BalanceAllowanceParams,
    AssetType,
)

from simulator import TradingSimluator  # keep your original spelling

load_dotenv()

MIN_P = Decimal("0.001")
MAX_P = Decimal("0.999")

USDC_STEP = Decimal("0.01")     # BUY market amount precision: 2dp
SHARES_STEP = Decimal("0.0001") # SELL shares precision: 4dp (and taker constraint)
SHARES_BASE = Decimal("1000000")
USDC_BASE = Decimal("1000000")

MAX_LIQ_RETRIES = 6

def q_step(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step


def clamp(x: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return max(lo, min(hi, x))


class TradingModule:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.copy_percentage = Decimal(str(config.get("copy_percentage", 0)))
        self.trading_enabled = bool(config.get("trading_enabled", False))

        if self.trading_enabled:
            print("Connecting to Polymarket...")
            self.client = ClobClient(
                "https://clob.polymarket.com",
                key=os.getenv("POLYMARKET_PRIVATE_KEY"),
                funder=os.getenv("POLYMARKET_FUNDER_ADDRESS"),
                chain_id=137,
                signature_type=1,
            )
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            print("Connected.")
        else:
            self.simulator = TradingSimluator(config)

    # ---------- CLOB helpers ----------
    def get_tick_size(self, token_id: str) -> Decimal:
        r = requests.get(
            "https://clob.polymarket.com/tick-size",
            params={"token_id": token_id},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        tick = data.get("minimum_tick_size")
        if tick is None:
            raise ValueError(f"Could not read tick size from response: {data}")
        return Decimal(str(tick))

    def get_available_balance(self, token_id: str, side: str, *, raw_units: bool = False) -> Decimal:
        side = side.upper()
        if side == "BUY":
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        elif side == "SELL":
            params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        else:
            raise ValueError("side must be BUY or SELL")

        self.client.update_balance_allowance(params)
        ba = self.client.get_balance_allowance(params)

        bal = Decimal(str(ba.get("balance", "0")))

        # try common allowance keys; if none exist, fall back to balance
        allow_raw = (
            ba.get("allowance")
            or ba.get("approved")
            or ba.get("spendAllowance")
            or ba.get("available")
        )
        if allow_raw is None:
            usable = bal
        else:
            allow = Decimal(str(allow_raw))
            usable = min(bal, allow)
        if side == "BUY" and not raw_units:
            return usable / USDC_BASE
        if side == "SELL" and not raw_units:
            return usable / SHARES_BASE
        return usable    
    
    def get_orderbook(self, slug: str, outcome: str, condition_id: str) -> Tuple[Optional[str], Optional[Decimal]]:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug, "conditionId": condition_id},
            timeout=10,
        )
        r.raise_for_status()
        markets = r.json()
        if not markets:
            raise Exception("No market found")

        market = None
        for m in markets:
            if str(m.get("conditionId", "")).lower() == condition_id.lower():
                market = m
                if market.get("acceptingOrders") is False or market.get("closed") is True:
                    return None, None
                break
        if market is None:
            return None, None

        outcomes = market["outcomes"]
        clob_ids = market["clobTokenIds"]
        prices = market["outcomePrices"]

        if isinstance(outcomes, str): outcomes = json.loads(outcomes)
        if isinstance(clob_ids, str): clob_ids = json.loads(clob_ids)
        if isinstance(prices, str): prices = json.loads(prices)

        idx = [o.lower() for o in outcomes].index(outcome.lower())
        token_id = str(clob_ids[idx])
        selected_price = Decimal(str(prices[idx]))
        return token_id, selected_price

    # ---------- Main ----------
    def execute_copy_trade(self, trade_change: Dict[str, Any], multiplier: float, wallet: str, _retried: bool = False):
        """
        - We treat sizing internally as SHARES.
        - For market orders:
            BUY  -> amount is USDC (2dp)
            SELL -> amount is SHARES (4dp)
        """
        try:
            side = trade_change["type"].upper()
            if side not in ("BUY", "SELL"):
                print(f"Skipping trade: unsupported side '{side}'")
                return None, None

            original_size = Decimal(str(trade_change["size"]))
            our_size = Decimal(str(trade_change.get("actual_size", "0")))  # SHARES
            slug = trade_change.get("slug")
            outcome = trade_change["outcome"]
            condition_id = trade_change["conditionId"]

            # determine desired shares
            if our_size <= 0:
                if self.copy_percentage and self.copy_percentage > 0:
                    our_size = original_size * self.copy_percentage
                elif multiplier:
                    our_size = original_size * Decimal(str(multiplier))
                else:
                    return None, None

            desired_shares = q_step(our_size, SHARES_STEP)
            if desired_shares <= 0:
                print(f"Skipping trade: calculated size {desired_shares} is too small.")
                return None, None

            # simulator
            if not self.trading_enabled:
                price = Decimal(str(trade_change["price"]))
                self.simulator.create_order(
                    slug=slug,
                    outcome=outcome,
                    side=side,
                    amount=desired_shares,
                    wallet=wallet,
                    price=price,
                )
                return None, None

            token_id, ref_price = self.get_orderbook(slug, outcome, condition_id)
            if not token_id or ref_price is None:
                print(f"Skipping trade: {slug} not open.")
                return False, slug

            tick = self.get_tick_size(token_id)

            # marketable price limit
            if "price_limit" in trade_change:
                price_limit = q_step(clamp(Decimal(str(trade_change["price_limit"])), MIN_P, MAX_P), tick)
            else:
                raw_price = (ref_price + Decimal("0.01")) if side == "BUY" else (ref_price - Decimal("0.01"))
                price_limit = q_step(clamp(raw_price, MIN_P, MAX_P), tick)

            # --- MARKET ORDER (FAK) ---
            # FAK is defined as: BUY in dollars, SELL in shares. :contentReference[oaicite:2]{index=2}
            if side == "BUY":
                usdc_amount = q_step(desired_shares * price_limit, USDC_STEP)
                print("BUY price:", price_limit, "usdc(2dp):", usdc_amount, "shares(4dp):", desired_shares)
                print("tokenid, price, size, side", token_id, price_limit, usdc_amount, side)
                mo = MarketOrderArgs(
                    token_id=token_id,
                    amount=float(usdc_amount),
                    side="BUY",
                    price=float(price_limit),
                )
            else:
                print("SELL price:", price_limit, "shares(4dp):", desired_shares)
                print("tokenid, price, size, side", token_id, price_limit, desired_shares, side)
                mo = MarketOrderArgs(
                    token_id=token_id,
                    amount=float(desired_shares),
                    side="SELL",
                    price=float(price_limit),
                )

            signed = self.client.create_market_order(mo)
            try:
                resp = self.client.post_order(signed, OrderType.FAK)
            except PolyApiException as e:
                msg = ""
                try:
                    if getattr(e, "error_msg", None):
                        msg = str(e.error_msg.get("error", "")).lower()
                    elif getattr(e, "error_message", None):
                        msg = str(e.error_message.get("error", "")).lower()
                except Exception:
                    msg = str(e).lower()

                if ("not enough balance" in msg or "insufficient balance" in msg):
                    print(f"Retrying trade: insufficient balance for {slug}, computing max affordable...")
                    bal = self.get_available_balance(token_id, side)
                    print(f"Available balance for {side}: {bal} {'shares' if side == 'SELL' else 'USDC'}")

                    if side == "BUY":
                        affordable_usdc = q_step(bal, USDC_STEP)
                        affordable_shares = q_step(affordable_usdc / price_limit, SHARES_STEP)
                        if affordable_shares <= 0:
                            print("Skipping trade: affordable shares too small.")
                            return None, None
                        trade_change["actual_size"] = str(affordable_shares)
                    else:
                        affordable_shares = q_step(bal, SHARES_STEP)
                        if affordable_shares <= 0:
                            print("Skipping trade: share balance too small.")
                            return None, None
                        trade_change["actual_size"] = str(affordable_shares)

                    # retry once (balance)
                    trade_change.pop("price_limit", None)
                    return self.execute_copy_trade(trade_change, multiplier=1.0, wallet=wallet)

                if ("no orders found to match with fak order" in msg):
                    # ---- cap retries + widen progressively ----
                    n = int(trade_change.get("liq_retries", 0))
                    if n >= MAX_LIQ_RETRIES:
                        print(f"Skipping trade: still no liquidity after {n} retries for {slug}.")
                        return None, None

                    trade_change["liq_retries"] = n + 1

                    # widen more each time (BUY: higher cap, SELL: lower min)
                    step = Decimal("0.02")  # 2c per attempt
                    relax_by = step * Decimal(n + 1)

                    new_limit = self.relaxed_price_limit(ref_price, side, tick, relax_by)
                    trade_change["price_limit"] = str(new_limit)

                    print(f"Retrying trade: no liquidity at price. Attempt {n+1}/{MAX_LIQ_RETRIES}, "
                        f"new price_limit={new_limit} (relax_by={relax_by}).")

                    return self.execute_copy_trade(trade_change, multiplier=1.0, wallet=wallet)

                raise

            order_id = resp.get("orderID") or resp.get("orderId")
            return True, order_id

        except Exception as e:
            print(f"Failed to execute copy trade: {e}")
            return None, None

    def check_orders(self):
        return self.client.get_trades()

    def relaxed_price_limit(self, ref_price: Decimal, side: str, tick: Decimal, relax_by: Decimal) -> Decimal:
        if side == "BUY":
            raw = ref_price + relax_by
        else:
            raw = ref_price - relax_by
        return q_step(clamp(raw, MIN_P, MAX_P), tick)