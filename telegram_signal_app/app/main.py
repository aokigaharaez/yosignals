import logging


def main():
    print("[1/3] Signal Lab: loading settings...", flush=True)
    from .config import load_settings

    settings = load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    # Provider URLs contain credentials; suppress request-level logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    print("[2/3] Loading web server. First startup may take longer...", flush=True)
    import uvicorn
    from .web import create_app

    print(f"[3/3] Starting http://{settings.host}:{settings.port} (keep this window open)", flush=True)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, proxy_headers=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStartup stopped with Ctrl+C.", flush=True)

