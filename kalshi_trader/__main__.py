"""
Entry point: python -m kalshi_trader

Runs the multi-platform paper trading simulator (Kalshi + Polymarket).
"""

import logging
import sys

from .orchestrator import Orchestrator


def main():
    # Configure logging — file + stderr
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("kalshi_trader.log", encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )

    # Suppress noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    orchestrator = Orchestrator()
    orchestrator.run()


if __name__ == "__main__":
    main()
    main()
