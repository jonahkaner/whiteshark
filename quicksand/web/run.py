"""Run the Quicksand web dashboard.

Usage:
    python -m quicksand.web.run
    python -m quicksand.web.run --port 8080
    python -m quicksand.web.run --host 0.0.0.0  # Allow external access
"""

import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Quicksand Web Dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    args = parser.parse_args()

    print(f"\n  Quicksand Dashboard: http://{args.host}:{args.port}\n")

    uvicorn.run(
        "quicksand.web.app:app",
        host=args.host,
        port=args.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
