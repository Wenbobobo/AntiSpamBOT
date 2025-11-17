from __future__ import annotations

import argparse
import asyncio
import logging

from jurybot.app import run_bot


def main() -> None:
    parser = argparse.ArgumentParser(description="Run JuryBot anti-spam bot.")
    parser.add_argument(
        "--config",
        type=str,
        default="config.toml",
        help="Path to config.toml file.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    asyncio.run(run_bot(args.config))


if __name__ == "__main__":
    main()
