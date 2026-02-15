#!/usr/bin/env python3
import time
import json
import logging
import sys
import os
import asyncio
from tqdm.contrib.logging import logging_redirect_tqdm
from tqdm import tqdm

# Ensure we can import from src
sys.path.append(os.getcwd())

from src.positions import get_user_positions, detect_order_changes
from src.trading import TradingModule

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"

def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

async def main():
    config = load_config()
    wallets = config.get("wallets_to_track", [])
    rate_limit = config.get("rate_limit", 25)
    account_budget = config.get("account_budget_dollars", 1)
    semaphore = asyncio.Semaphore(rate_limit)
    
    if not wallets:
        logger.error("No wallets to track in config.")
        return

    trading_module = TradingModule(config)
    
    async def fetch_positions_safe(wallet_address, initialize=False):
        async with semaphore:
            start = time.perf_counter()
            positions = await asyncio.to_thread(get_user_positions, wallet_address)
            fetch_time = time.perf_counter() - start
            if positions and initialize:
                logger.info(f"Initialized {wallet_address[:8]}... with {len(positions)} positions")
            logger.debug(f"Fetched {wallet_address[:8]} positions={len(positions) if positions else 0} in {fetch_time:.2f}s")
            return positions
    
    # Initialize state
    logger.info(f"Initializing state for {len(wallets)} wallets...")
    wallet_tasks = {wallet: asyncio.create_task(fetch_positions_safe(wallet, True)) for wallet in wallets}
    results = []
    try:
        with logging_redirect_tqdm():
            with tqdm(total=len(wallet_tasks), desc="Initializing wallets") as pbar:
                for coro in asyncio.as_completed(wallet_tasks.values()):
                    res = await coro
                    results.append(res)
                    pbar.update(1)
        wallet_states = dict(zip(wallet_tasks.keys(), results))
        wallet_states = {k: v for k, v in wallet_states.items() if v}
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Initialization interrupted...")
        # Cancel all remaining initialization tasks
        for task in wallet_tasks.values():
            if not task.done():
                task.cancel()
        # Wait for all tasks to finish cancelling
        if wallet_tasks.values():
            await asyncio.gather(*wallet_tasks.values(), return_exceptions=True)
        return

    logger.info("Starting copy trader loop...")

    async def check_on(wallet):
        async with semaphore:
            try:
                current_positions = await fetch_positions_safe(wallet)
                if current_positions:
                    previous_positions = wallet_states.get(wallet, [])
                    logger.debug(f"{wallet[:8]} prev={len(previous_positions)} curr={len(current_positions)}")
                    changes = detect_order_changes(previous_positions, current_positions)
                    if changes:
                        logger.info(f"Detected {len(changes)} changes for {wallet[:8]}")
                        total_initial_value = sum(pos.get('initialValue', 0) for pos in current_positions) or 0
                        if total_initial_value == 0:
                            logger.warning("Total initial value is 0, skipping multiplier-based sizing")
                        else:
                            multiplier = account_budget / total_initial_value
                            for change in changes:
                                success, order_id = trading_module.execute_copy_trade(change, multiplier, wallet)
                                if success: logger.info(f"Success! Order ID: {order_id}")
                    else:
                        logger.debug("No changes found")
                    wallet_states[wallet] = current_positions
                else:
                    logger.error(f"No current positions for wallet {wallet}")
            except Exception as e:
                logger.error(f"Error tracking {wallet}: {e}")
            await asyncio.sleep(1)
    
    wallet_tasks = {}
    try:
        while True:
            # Create tasks for any wallets not currently being monitored
            for wallet in wallets:
                if wallet not in wallet_tasks or wallet_tasks[wallet].done():
                    wallet_tasks[wallet] = asyncio.create_task(check_on(wallet))
            
            # Wait a bit before checking again
            await asyncio.sleep(5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Stopping...")
        # Cancel all remaining tasks
        for task in wallet_tasks.values():
            if not task.done():
                task.cancel()
        # Wait for all tasks to finish cancelling
        if wallet_tasks.values():
            await asyncio.gather(*wallet_tasks.values(), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
