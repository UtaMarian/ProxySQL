"""Cross-platform entry point using waitress (used on Windows, where gunicorn does not run).

    set PROXYSQL_WEBUI_CONFIG=C:\\ProxySQL-WebUI\\config.ini
    python serve.py --host 127.0.0.1 --port 8080
"""
import argparse
import os

from waitress import serve

from app import create_app


def main():
    p = argparse.ArgumentParser(description="ProxySQL Web UI (waitress server)")
    p.add_argument("--host", default=os.environ.get("WEBUI_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("WEBUI_PORT", "8080")))
    p.add_argument("--threads", type=int, default=8)
    a = p.parse_args()
    print("ProxySQL Web UI listening on http://%s:%d/" % (a.host, a.port))
    serve(create_app(), host=a.host, port=a.port, threads=a.threads, ident="proxysql-webui")


if __name__ == "__main__":
    main()
