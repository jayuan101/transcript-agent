#!/usr/bin/env python3
"""
ProxyScrape proxy manager.
Fetches live HTTP proxies from the ProxyScrape API, tests them concurrently,
and returns the fastest working one.

Usage:
  python proxy_manager.py            # prints best proxy URL to stdout
  import proxy_manager; p = proxy_manager.get_best_proxy(api_key)
"""

import urllib.request
import os
import time
import random
import sys
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


PROXYSCRAPE_API = (
    "https://api.proxyscrape.com/v3/free-proxy-list/get"
    "?request=displayproxies&proxy_type=http&country=all"
    "&ssl=all&anonymity=elite&limit=50&apikey={api_key}"
)

TEST_URL      = "http://api.ipify.org"
CACHE_FILE    = Path(__file__).parent / ".proxy_cache.txt"
CACHE_TTL     = 300   # seconds — reuse cached proxy for 5 minutes


def _fetch_proxy_list(api_key: str) -> list[str]:
    """Fetch list of candidate proxies from ProxyScrape API."""
    try:
        handler = urllib.request.ProxyHandler({})
        opener  = urllib.request.build_opener(handler)
        url     = PROXYSCRAPE_API.format(api_key=api_key)
        resp    = opener.open(url, timeout=12)
        lines   = resp.read().decode().splitlines()
        return [l.strip() for l in lines if l.strip() and ":" in l]
    except Exception as e:
        print(f"[proxy_manager] API fetch failed: {e}", file=sys.stderr)
        return []


def _test_proxy(proxy: str, timeout: int = 6) -> tuple[str, float] | None:
    """Return (proxy, latency_ms) if working, else None."""
    try:
        t0 = time.time()
        ph = urllib.request.ProxyHandler({
            "http":  f"http://{proxy}",
            "https": f"http://{proxy}",
        })
        op  = urllib.request.build_opener(ph)
        ip  = op.open(TEST_URL, timeout=timeout).read().decode().strip()
        ms  = (time.time() - t0) * 1000
        return (proxy, ms)
    except Exception:
        return None


def get_best_proxy(api_key: str, workers: int = 10, top_n: int = 3,
                   verbose: bool = False) -> str | None:
    """
    Fetch proxy list, test concurrently, return the fastest working proxy
    formatted as 'http://HOST:PORT', or None if none found.
    """
    # ── check cache ───────────────────────────────────────────────────────────
    if CACHE_FILE.exists():
        age = time.time() - CACHE_FILE.stat().st_mtime
        if age < CACHE_TTL:
            cached = CACHE_FILE.read_text().strip()
            if cached:
                if verbose:
                    print(f"[proxy_manager] Using cached proxy ({int(age)}s old): {cached}",
                          file=sys.stderr)
                return cached

    if verbose:
        print("[proxy_manager] Fetching proxy list from ProxyScrape API…", file=sys.stderr)

    candidates = _fetch_proxy_list(api_key)
    if not candidates:
        return None

    random.shuffle(candidates)
    test_batch = candidates[:min(30, len(candidates))]

    if verbose:
        print(f"[proxy_manager] Testing {len(test_batch)} proxies ({workers} parallel)…",
              file=sys.stderr)

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_test_proxy, p): p for p in test_batch}
        for fut in as_completed(futures):
            r = fut.result()
            if r:
                results.append(r)
                if verbose:
                    print(f"[proxy_manager]   ✓ {r[0]}  {r[1]:.0f}ms", file=sys.stderr)
                if len(results) >= top_n:
                    # cancel remaining futures
                    for f in futures:
                        f.cancel()
                    break

    if not results:
        if verbose:
            print("[proxy_manager] No working proxies found.", file=sys.stderr)
        return None

    best_proxy, best_ms = min(results, key=lambda x: x[1])
    proxy_url = f"http://{best_proxy}"

    if verbose:
        print(f"[proxy_manager] Best: {best_proxy}  ({best_ms:.0f}ms)", file=sys.stderr)

    # ── cache it ──────────────────────────────────────────────────────────────
    CACHE_FILE.write_text(proxy_url)
    return proxy_url


def set_env_proxy(api_key: str, verbose: bool = True) -> str | None:
    """
    Find a working proxy and set HTTP_PROXY / HTTPS_PROXY env vars.
    Returns the proxy URL or None.
    """
    proxy = get_best_proxy(api_key, verbose=verbose)
    if proxy:
        os.environ["HTTP_PROXY"]  = proxy
        os.environ["HTTPS_PROXY"] = proxy
        os.environ["http_proxy"]  = proxy
        os.environ["https_proxy"] = proxy
        if verbose:
            print(f"[proxy_manager] Proxy active: {proxy}", file=sys.stderr)
    return proxy


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    api_key = os.environ.get("PROXYSCRAPE_API_KEY", "")
    if not api_key:
        print("ERROR: PROXYSCRAPE_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    proxy = get_best_proxy(api_key, verbose=True)
    if proxy:
        print(proxy)   # stdout — used by entrypoint scripts
    else:
        sys.exit(1)
