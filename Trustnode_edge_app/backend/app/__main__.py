import uvicorn

from app.config import settings


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.trustnode_host,
        port=settings.trustnode_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
