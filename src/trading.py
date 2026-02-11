import os
import pmxt
from dotenv import load_dotenv
from typing import Dict, Any
import math
from simulator import TradingSimluator


load_dotenv()

class TradingModule:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.copy_percentage = config.get("copy_percentage", 0)
        self.trading_enabled = config.get("trading_enabled", False)
        
        if self.trading_enabled:
            print("Connecting to Polymarket...")
            self.poly = pmxt.Polymarket(
                private_key=os.getenv("POLYMARKET_PRIVATE_KEY"),
                proxy_address=os.getenv("POLYMARKET_PROXY_ADDRESS"),
                signature_type=2
            )
        else:
            self.simulator = TradingSimluator(config)
        print("Connected.", self.poly.fetch_balance())

    def execute_copy_trade(self, trade_change: Dict[str, Any], multiplier: float, wallet: str):
        """
        Executes a copy trade based on a detected change in someone else's positions.
        """
        try:
            print('trade change:',trade_change)
            side = trade_change['type'].lower() # 'buy' or 'sell'
            asset_id = trade_change['asset']
            original_size = float(trade_change['size'])
            slug = trade_change.get('slug')
            
            if self.copy_percentage:
                # Calculate our size based on the percentage config
                our_size = round(original_size * self.copy_percentage, 2)
            elif multiplier:
                our_size = round(original_size*multiplier)
            
            if our_size <= 0 or not trade_change['price']:
                print(f"Skipping trade: calculated size {our_size} is too small.")
                return


            print(f"Copying {side} for {slug}: {trade_change['type']} {our_size} shares @ ${trade_change['price']} ({'-' if side == 'buy' else '+'}${math.floor(trade_change['price']*our_size*100)/100})")
            # Add this data to CSV
            
            if not self.trading_enabled:
                self.simulator.create_order(
                    slug=slug,
                    outcome=trade_change["outcome"],
                    side=side,
                    amount=our_size,
                    wallet=wallet,
                    price=trade_change['price']
                )
                return
            
            # Find market ID from slug if not provided (though asset_id/outcome_id is what's needed for order)
            # The positions API gives us the asset (outcome_id). 
            # We still need the market_id for the pmxt create_order call.
            
            markets = self.poly.fetch_markets(slug=slug)
            if not markets:
                print(f"Market not found for slug: {slug}")
                return
            market_id = markets[0].market_id

            order = self.poly.create_order(
                market_id=market_id,
                outcome_id=asset_id,
                side=side,
                type="market",
                amount=our_size,
                fee=1000
            )
            
            print(f"Success! Order ID: {order.order_id}")
            return order

        except Exception as e:
            print(f"Failed to execute copy trade: {e}")
