import os
from datetime import datetime
from typing import Dict, Any
import csv
import json
import math

class TradingSimluator:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.balance = config.get('simulator_balance',
            config.get('account_budget_dollars') * len(config.get('wallets_to_track')))
        self.holdings = config.get('simulator_holdings', {})
        self.fee = config.get('simulator_fee_percentage', 0.0156)
        self.csv_file = "trades.csv"
        self.csv_writer = None
        self.csv_file_handle = None
        self.state_file = config.get('simulator_state_file', 'state.json')
        self._init_csv()

    def _init_csv(self):
        """Initialize CSV file and writer, creating headers if needed."""
        file_exists = os.path.isfile(self.csv_file)
        self.csv_file_handle = open(self.csv_file, mode='a', newline='')
        self.csv_writer = csv.writer(self.csv_file_handle)
        if not file_exists:
            self.csv_writer.writerow(['timestamp', 'side', 'slug', 'outcome', 'wallet_id', 'amount', 'price', 'total_cost', 'cash_balance', 'notes'])
            self.csv_file_handle.flush()

    def write_to_csv(self, slug, outcome, side, amount, wallet, price, notes):
        total_cost = math.floor(price * amount * 100) / 100
        self.csv_writer.writerow([
            datetime.now().isoformat(),
            side,
            slug,
            outcome,
            wallet,
            amount,
            price,
            total_cost,
            self.balance,
            notes
        ])
        self.csv_file_handle.flush()

    def _write_state_json(self):
        """Write current balance and holdings to the configured JSON state file."""
        state = {
            'balance': self.balance,
            'holdings': {}
        }
        for key, amt in self.holdings.items():
            if isinstance(key, tuple) and len(key) == 2:
                slug, outcome = key
            else:
                # if key isn't a tuple, try to parse or store as-is
                slug = str(key)
                outcome = ""
            state['holdings'].setdefault(slug, {})[str(outcome)] = amt
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception:
            # best-effort write; don't crash simulator on I/O errors
            pass

    def create_order(self, slug, outcome, side, amount, wallet, price):
        notes = []
        self.holdings.setdefault((slug, outcome), 0)
        if side == "SELL":
            if self.holdings.get((slug, outcome)) < amount:
                new_amount = self.holdings.get((slug,outcome), 0)
                notes.append(f"Tried to sell {amount} shares, selling all ({new_amount}) shares instead.")
            else:
                new_amount = amount
            self.holdings[(slug, outcome)] -= new_amount
            self.balance += (1-self.fee) * new_amount * price
        if side == "BUY":
            total_cost = amount * price * (1+self.fee)
            if self.balance < total_cost:
                notes.append(f"Tried to buy {amount} shares but we can't afford it.")
            else: 
                self.holdings[(slug,outcome)] += amount
                self.balance -= total_cost
        self.write_to_csv(slug, outcome, side, amount, wallet, price, notes)
        # persist state (balance + holdings) to JSON after each order
        self._write_state_json()

    def __del__(self):
        """Close file handle on cleanup."""
        if self.csv_file_handle:
            self.csv_file_handle.close()
