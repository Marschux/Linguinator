import os

import uvicorn


def main():
    uvicorn.run(
        app="app.main:app",
        host=os.getenv("LINGUINATOR_HOST", "0.0.0.0"),
        port=int(os.getenv("LINGUINATOR_PORT", "5051")),
        # Always on, from anyone. Without a proxy nobody sends these headers, so there is nothing
        # to pick up; with one they are needed. A client that forges them can claim another
        # address or scheme, which changes nothing while no code here reads either. Should that
        # change - an access log, rate limiting, an IP block - both the switch and a narrower
        # allow list have to come back.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
