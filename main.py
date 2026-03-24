from __future__ import annotations

import argparse

from tampermonkey_bridge import run_bridge_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Screen casting tools")
    parser.add_argument(
        "--bridge",
        action="store_true",
        help="Start the local Tampermonkey bridge instead of the desktop GUI",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bridge bind host when --bridge is used, default is 127.0.0.1",
    )
    parser.add_argument(
        "--port",
        default=9527,
        type=int,
        help="Bridge bind port when --bridge is used, default is 9527",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.bridge:
        run_bridge_server(host=args.host, port=args.port)
        return
    from app.gui import run as run_gui

    run_gui()


if __name__ == "__main__":
    main()
