"""Always-on host process for cloud deploys (Render etc.).

Keeps the read-only dashboard up 24/7 and starts `live_run` (without --dashboard) only while the NSE session is open.
live_run exits outside market hours or on a non-trading day; this supervisor just waits and retries, so the host never
sees a crash loop. PAPER only; same guards as live_run.
"""
import os
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from nifty_live import live_run, recorder
from tools import dashboard

RETRY_SECONDS = 60


def main(argv=None):
    extra = list(sys.argv[1:] if argv is None else argv)
    port = int(os.environ.get("PORT", 8765))
    server = ThreadingHTTPServer((dashboard.bind_host(), port), dashboard.make_handler(
        os.path.abspath(live_run.JOURNAL_DIR), live_run.STATE_PATH))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("dashboard: http://%s:%d/live (read-only)" % (dashboard.bind_host(), port), flush=True)
    cmd = [sys.executable, "-m", "nifty_live.live_run"] + extra
    while True:
        if recorder.in_session(recorder.now_ist()):
            print("market open: starting live_run", flush=True)
            subprocess.call(cmd)
            print("live_run exited; retrying in %ds" % RETRY_SECONDS, flush=True)
        time.sleep(RETRY_SECONDS)


if __name__ == "__main__":
    main()
