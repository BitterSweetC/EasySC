from __future__ import annotations

import argparse

from tampermonkey_bridge import run_bridge_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the local Tampermonkey casting bridge.")
    parser.add_argument("--host", default="127.0.0.1", help="Bridge bind host, default is 127.0.0.1")
    parser.add_argument("--port", default=9527, type=int, help="Bridge bind port, default is 9527")
    args = parser.parse_args()
    run_bridge_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
