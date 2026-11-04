"""Read-only live market-data access (Upstox Analytics token) used ONLY to record snapshots to files.

Kept outside nifty_core on purpose: the core stays offline and importable without any network code. Nothing here can
place, change or cancel anything: the HTTP layer sends GET requests only, to one host, for two allow-listed paths.
"""
