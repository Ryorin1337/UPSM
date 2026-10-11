"""
The Universal Phigros Save Manager (UPSM) Launcher
Starts the Starlette + Uvicorn server and automatically opens the browser.
Usage:
    python run_webui.py [--port 8000]
"""
import os
import sys
import socket
import webbrowser
import threading
import time
import argparse
import uvicorn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from server import app


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) == 0


def find_available_port(start_port: int = 8000, max_attempts: int = 20) -> int:
    for port in range(start_port, start_port + max_attempts):
        if not is_port_in_use(port):
            return port
    return start_port


def open_browser_delayed(url: str, delay: float = 1.0):
    time.sleep(delay)
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"[Launcher] Failed to auto-open browser: {e}")


def main():
    parser = argparse.ArgumentParser(description="The Universal Phigros Save Manager (UPSM)")
    parser.add_argument("--port", type=int, default=None, help="Server port (default: auto 8000)")
    args = parser.parse_args()

    port = args.port if args.port else find_available_port(8000)
    url = f"http://127.0.0.1:{port}"

    print("==================================================================")
    print("   The Universal Phigros Save Manager (UPSM)")
    print("==================================================================")
    print(f" 服务地址: {url}")
    print(" 正在自动启动浏览器打开控制面板...")
    print(" 按 Ctrl + C 可停止服务。")
    print("==================================================================")

    # Launch browser after 1 second
    threading.Thread(target=open_browser_delayed, args=(url, 1.2), daemon=True).start()

    # Start server
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
