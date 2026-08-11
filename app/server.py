import os

import uvicorn


def enabled(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


def main():
    uvicorn.run(
        app="app.main:app",
        host=os.getenv("LINGUINATOR_HOST", "0.0.0.0"),
        port=int(os.getenv("LINGUINATOR_PORT", "5051")),
        proxy_headers=enabled("LINGUINATOR_TRUST_PROXY_HEADERS", "true"),
        # Anyone reaching the container directly can claim any forwarded address. Nothing here
        # decides by client IP, so this only feeds the scheme the app thinks it was reached over.
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
