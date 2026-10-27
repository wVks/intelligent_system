import os

import uvicorn


def main():
    uvicorn.run("studynavigator.main:app", host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8000")), workers=1, proxy_headers=True,
                forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1,::1"))


if __name__ == "__main__":
    main()
