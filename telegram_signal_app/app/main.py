import logging


def main():
    print("[1/3] Signal Lab: loading settings...", flush=True)

    from .config import load_settings
    settings = load_settings()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    # Provider URLs may contain credentials.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)

    print("[2/3] Loading web server...", flush=True)

    import uvicorn
    from .web import create_app

    # Railway передаёт порт через переменную окружения PORT
    port = settings.port

    # Railway задаёт HOST=0.0.0.0; локальный просмотр остаётся на loopback.
    host = settings.host

    print(f"[3/3] Starting http://{host}:{port}", flush=True)

    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        proxy_headers=False,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStartup stopped with Ctrl+C.", flush=True)
