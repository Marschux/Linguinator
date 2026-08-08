import os

import uvicorn


def enabled(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


def main():
    options = {
        "app": "app.main:app",
        "host": os.getenv("LINGUINATOR_HOST", "0.0.0.0"),
        "port": int(os.getenv("LINGUINATOR_PORT", "5051")),
        "proxy_headers": enabled("LINGUINATOR_TRUST_PROXY_HEADERS", "true"),
        "forwarded_allow_ips": os.getenv("LINGUINATOR_FORWARDED_ALLOW_IPS", "*"),
    }
    certfile = os.getenv("LINGUINATOR_SSL_CERTFILE", "").strip()
    keyfile = os.getenv("LINGUINATOR_SSL_KEYFILE", "").strip()
    if certfile and keyfile:
        options["ssl_certfile"] = certfile
        options["ssl_keyfile"] = keyfile
    uvicorn.run(**options)


if __name__ == "__main__":
    main()
