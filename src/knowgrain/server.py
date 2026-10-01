import uvicorn

from knowgrain.config import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "knowgrain.api:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        workers=1,
    )


if __name__ == "__main__":
    main()
