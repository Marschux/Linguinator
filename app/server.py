import os

import uvicorn


def enabled(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


def main():
    options = {
        "app": "app.main:app",
        "host": os.getenv("LINGUMACHINA_HOST", "0.0.0.0"),
        "port": int(os.getenv("LINGUMACHINA_PORT", "5051")),
        "proxy_headers": enabled("LINGUMACHINA_TRUST_PROXY_HEADERS", "true"),
        "forwarded_allow_ips": os.getenv("LINGUMACHINA_FORWARDED_ALLOW_IPS", "*"),
    }
    certfile = os.getenv("LINGUMACHINA_SSL_CERTFILE", "").strip()
    keyfile = os.getenv("LINGUMACHINA_SSL_KEYFILE", "").strip()
    if certfile and keyfile:
        options["ssl_certfile"] = certfile
        options["ssl_keyfile"] = keyfile
    uvicorn.run(**options)


if __name__ == "__main__":
    main()
