import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs
from py_clob_client.order_builder.constants import BUY, SELL
from dotenv import load_dotenv
from typing import Dict, Any
import requests
import json
from simulator import TradingSimluator


load_dotenv()

class TradingModule:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.copy_percentage = config.get("copy_percentage", 0)
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
        """
        Executes a copy trade based on a detected change in someone else's positions.
        """
        try:
            side = trade_change['type'].upper() # 'buy' or 'sell'
            original_size = float(trade_change['size'])
            slug = trade_change.get('slug')
            price = trade_change['price']
            outcome = trade_change["outcome"]
            asset = trade_change["asset"]
            conditionId = trade_change["conditionId"]
            print("slug outcome asset conditionId", slug, outcome, asset, conditionId)

            if self.copy_percentage:
                # Calculate our size based on the percentage config
                our_size = round(original_size * self.copy_percentage, 2)
            elif multiplier:
                our_size = round(original_size*multiplier)
            
            if our_size <= 0 or not trade_change['price']:
                print(f"Skipping trade: calculated size {our_size} is too small.")
                return


            print(f"Copying {side} for {slug}: {trade_change['type']} {our_size} shares @ ${trade_change['price']}")
            # Add this data to CSV
            if not self.trading_enabled:
                self.simulator.create_order(
                    slug=slug,
                    outcome=outcome,
                    side=side,
                    amount=our_size,
                    wallet=wallet,
                    price=price
                )
                return
            token_id = self.get_orderbook(slug, outcome, conditionId)
            if token_id:
                print("token, price, amount, side", token_id, float(price), float(our_size), side)
                order = MarketOrderArgs(
                    token_id=token_id,
                    price=float(price),
                    amount=float(our_size),
                    side=side
                )
                signed = self.client.create_market_order(order)
                resp = self.client.post_order(signed)
                order_id = resp["orderID"]
                return order_id
            print(f"Skipping trade: {slug} not open.")
            return (False, slug)

        except Exception as e:
            print(f"Failed to execute copy trade: {e}")

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
                    return None
                break
        if market is None:
            return None


        outcomes = market["outcomes"]
        clob_ids = market["clobTokenIds"]
        if isinstance(outcomes, str): outcomes = json.loads(outcomes)
        if isinstance(clob_ids, str): clob_ids = json.loads(clob_ids)

        idx = [o.lower() for o in outcomes].index(outcome.lower())
        token_id = clob_ids[idx]

        outcomes = market["outcomes"]
        clob_ids = market["clobTokenIds"]
        # sometimes Gamma returns strings instead of lists
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)

        # 2. map outcome → token id
        idx = [o.lower() for o in outcomes].index(outcome.lower())
        token_id = clob_ids[idx]
        return token_id