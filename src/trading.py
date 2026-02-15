import os
from py_clob_client.client import ClobClient
from py_clob_client.exceptions import PolyApiException
from py_clob_client.clob_types import OrderArgs
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
from typing import Dict, Any
import requests
import json
from simulator import TradingSimluator

MIN_P = 0.001
MAX_P = 0.999
load_dotenv()

class TradingModule:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.copy_percentage = Decimal(str(config.get("copy_percentage", 0)))
        self.trading_enabled = config.get("trading_enabled", False)
        
        if self.trading_enabled:
            print("Connecting to Polymarket...")
            self.client = ClobClient(
                "https://clob.polymarket.com",
                key=os.getenv("POLYMARKET_PRIVATE_KEY"),
                funder=os.getenv("POLYMARKET_FUNDER_ADDRESS"),
                chain_id=137,
                signature_type=1
            )
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            print("Connected.")
        else:
            self.simulator = TradingSimluator(config)

    def execute_copy_trade(self, trade_change: Dict[str, Any], multiplier: float, wallet: str):
        try:
            side = trade_change['type'].upper()
            original_size = Decimal(str(trade_change['size']))
            slug = trade_change.get('slug')
            outcome = trade_change["outcome"]
            conditionId = trade_change["conditionId"]

            if self.copy_percentage:
                our_size = original_size * self.copy_percentage
            elif multiplier:
                our_size = original_size * Decimal(str(multiplier))
            else:
                return None, None

            if our_size <= 0:
                print(f"Skipping trade: calculated size {our_size} is too small.")
                return None, None

            if not self.trading_enabled:
                price = Decimal(str(trade_change['price']))
                self.simulator.create_order(
                    slug=slug,
                    outcome=outcome,
                    side=side,
                    amount=our_size,
                    wallet=wallet,
                    price=price
                )
                return None, None

            token_id, price = self.get_orderbook(slug, outcome, conditionId)
            if not token_id:
                print(f"Skipping trade: {slug} not open.")
                return False, slug

            price = Decimal(str(price))

            # quantize helpers
            def q(x, step):
                return (x / step).to_integral_value(rounding=ROUND_DOWN) * step


            # shares must always be 4dp
            maker_shares = q(our_size, Decimal("0.0001"))

            # make price aggressively marketable
            if side == "BUY":
                market_price = q(price + Decimal("0.01"), Decimal("0.001"))
                desired_shares = q(our_size, Decimal("0.0001"))
                usdc = q(desired_shares * market_price, Decimal("0.01"))
                shares = q(usdc / market_price, Decimal("0.00001"))

                print("BUY price:", market_price, "usdc(2dp):", usdc, "shares(5dp):", shares)

                order_args = OrderArgs(
                    token_id=token_id,
                    side="BUY",
                    price=float(market_price),
                    size=float(usdc),   # <-- USDC (2 decimals)
                )
            else:
                market_price = q(price - Decimal("0.01"), Decimal("0.001"))
                shares = q(our_size, Decimal("0.0001"))

                print("SELL price:", market_price, "shares(4dp):", shares)

                order_args = OrderArgs(
                    token_id=token_id,
                    side="SELL",
                    price=float(market_price),
                    size=float(shares),  # <-- shares for SELL
                )

            print("tokenid, price, size, side", token_id, market_price, maker_shares, side)

            order_args = OrderArgs(
                token_id=token_id,
                side=side,
                price=float(market_price),
                size=float(maker_shares),
            )

            signed = self.client.create_order(order_args)
            try:
                resp = self.client.post_order(signed, "FAK")
            except PolyApiException as e:
                print(f"Order failed: {e}")
                return None, None   

            order_id = resp["orderID"]
            return True, order_id

        except Exception as e:
            print(f"Failed to execute copy trade: {e}")
            return None, None


    def check_orders(self):
        return self.client.get_trades()

    def get_orderbook(self, slug, outcome, condition_id):
        # 1. fetch market from Gamma
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug, "conditionId": condition_id},
            timeout=10
        )
        r.raise_for_status()
        markets = r.json()
        if not markets:
            raise Exception("No market found")

        # prefer exact conditionId match
        market = None
        for m in markets:
            if str(m.get("conditionId", "")).lower() == condition_id.lower():
                market = m
                # Skip markets that cannot accept orders
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

        #price_map = {o.lower(): float(p) for o, p in zip(outcomes, prices)}

        idx = [o.lower() for o in outcomes].index(outcome.lower())
        token_id = clob_ids[idx]
        selected_price = Decimal(str(prices[idx]))
        return token_id, selected_price
