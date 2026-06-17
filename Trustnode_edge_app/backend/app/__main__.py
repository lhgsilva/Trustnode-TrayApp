import uvicorn

from app.config import settings


def main() -> None:
    # The primary server always binds to 127.0.0.1 (or whatever the
    # operator/env overrides). LAN sharing is provided by a *second*
    # uvicorn server started in-process (see services/lan_socket.py)
    # so toggling LAN never requires a restart.
    uvicorn.run(
        "app.main:app",
        host=settings.trustnode_host,
        port=settings.trustnode_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
