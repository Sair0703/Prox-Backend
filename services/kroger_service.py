# services/kroger_service.py
#
# Kroger cart-fill workflow (3-legged user OAuth + product lookup + cart add).
#
# Flow this module supports:
#   1. get_authorize_url(state)            -> URL we send the user to so they log
#                                            into their own Kroger account.
#   2. exchange_code_for_token(code)       -> swap the returned ?code= for a
#                                            user-authorized access + refresh token.
#   3. refresh_access_token(refresh_token) -> keep the user connected without a
#                                            second login.
#   4. resolve_upc(term, location_id)      -> turn a Prox basket item name into a
#                                            real Kroger UPC (uses product.compact;
#                                            works with our existing app creds).
#   5. add_to_cart(user_access_token, items) -> PUT /v1/cart/add into the user's
#                                            real Kroger cart.
#
# Notes / external dependencies (config you control in the Kroger dev portal):
#   - The Kroger app must have scope `cart.basic:write` enabled for cart add.
#   - The redirect URI below must be registered exactly in the Kroger app.
#   - Product search works today with our existing client_credentials creds.

import os
import re
import time
import base64
import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = "https://api.kroger.com/v1"

# Reuse the existing Prox Kroger app credentials (same ones the store-location
# scripts use). For the user cart flow the app additionally needs cart scope.
CLIENT_ID = os.getenv("KROGER_CLIENT_ID", "proxgrocerysavings-bbcdkn5r")
CLIENT_SECRET = os.getenv("KROGER_CLIENT_SECRET", "")

# Must match EXACTLY what is registered in the Kroger developer portal.
REDIRECT_URI = os.getenv(
    "KROGER_REDIRECT_URI", "http://localhost:8000/kroger/auth/callback"
)

# Scopes: product.compact (read products) + cart.basic:write (add to cart).
USER_SCOPES = os.getenv("KROGER_USER_SCOPES", "product.compact cart.basic:write")

AUTHORIZE_URL = f"{API_BASE}/connect/oauth2/authorize"
TOKEN_URL = f"{API_BASE}/connect/oauth2/token"

DEFAULT_MODALITY = os.getenv("KROGER_DEFAULT_MODALITY", "PICKUP")  # PICKUP | DELIVERY


class KrogerError(Exception):
    """Raised when a Kroger API call fails. Carries status + detail for the API layer."""

    def __init__(self, message: str, status: int = 502, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail


def _basic_auth_header() -> dict:
    if not CLIENT_SECRET:
        raise KrogerError(
            "KROGER_CLIENT_SECRET is not set. Add it to your .env before running "
            "the Kroger workflow.",
            status=500,
        )
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


# Step 1 + 2 + 3: User OAuth (Authorization Code flow)

def get_authorize_url(state: str) -> str:
    """Build the Kroger login URL. `state` carries our Prox user_id round-trip."""
    from urllib.parse import urlencode

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": USER_SCOPES,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code_for_token(code: str) -> dict:
    """Exchange the authorization code for a user access + refresh token."""
    resp = requests.post(
        TOKEN_URL,
        headers=_basic_auth_header(),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise KrogerError(
            "Failed to exchange authorization code for a token.",
            status=502,
            detail=_safe_json(resp),
        )
    return resp.json()


def refresh_access_token(refresh_token: str) -> dict:
    """Use a refresh token to get a fresh access token (no second login)."""
    resp = requests.post(
        TOKEN_URL,
        headers=_basic_auth_header(),
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise KrogerError(
            "Failed to refresh Kroger access token. The user may need to reconnect.",
            status=401,
            detail=_safe_json(resp),
        )
    return resp.json()


# Step 4: Product search -> UPC (client_credentials, product.compact)

_cc_token_cache = {"token": None, "expires_at": 0.0}


def _client_credentials_token() -> str:
    """App-level token for reading products. Cached until ~expiry."""
    if _cc_token_cache["token"] and time.time() < _cc_token_cache["expires_at"]:
        return _cc_token_cache["token"]

    resp = requests.post(
        TOKEN_URL,
        headers=_basic_auth_header(),
        data={"grant_type": "client_credentials", "scope": "product.compact"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise KrogerError(
            "Failed to obtain Kroger client-credentials token for product search.",
            status=502,
            detail=_safe_json(resp),
        )
    data = resp.json()
    _cc_token_cache["token"] = data["access_token"]
    _cc_token_cache["expires_at"] = time.time() + int(data.get("expires_in", 1800)) - 60
    return _cc_token_cache["token"]


def search_products(term: str, location_id: str | None = None, limit: int = 5) -> list[dict]:
    """Search Kroger products by term. Returns simplified candidates with UPCs.

    location_id (a Kroger store id) is optional but lets Kroger return
    store-specific price/availability.
    """
    token = _client_credentials_token()
    params = {"filter.term": term, "filter.limit": max(1, min(limit, 50))}
    if location_id:
        params["filter.locationId"] = location_id

    resp = requests.get(
        f"{API_BASE}/products",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params=params,
        timeout=20,
    )
    if resp.status_code != 200:
        raise KrogerError(
            f"Kroger product search failed for '{term}'.",
            status=502,
            detail=_safe_json(resp),
        )

    out: list[dict] = []
    for p in resp.json().get("data", []):
        items = p.get("items") or [{}]
        first = items[0]
        price = (first.get("price") or {})
        out.append({
            "upc": p.get("upc"),
            "description": p.get("description"),
            "brand": p.get("brand"),
            "size": first.get("size"),
            "price": price.get("promo") or price.get("regular"),
            "stock_level": (first.get("inventory") or {}).get("stockLevel"),
        })
    return out


_IN_STOCK_LEVELS = ("HIGH", "LOW")


def _parse_size(s: str | None) -> tuple[float, str] | None:
    """Normalize a size string to (value, family) so sizes are comparable.
    weight -> ('weight', oz), volume -> ('vol', fl oz), count -> ('count').
    Returns None when nothing parseable is found.
    """
    if not s:
        return None
    sl = s.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*fl\.?\s*oz", sl)              # fluid ounces
    if m:
        return (float(m.group(1)), "vol")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(gallon|gal|qt|pt|ml|l)\b", sl)  # other volume
    if m:
        v, u = float(m.group(1)), m.group(2)
        floz = {"gallon": 128, "gal": 128, "qt": 32, "pt": 16, "l": 33.814, "ml": 0.033814}[u]
        return (v * floz, "vol")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(lbs?|oz)\b", sl)             # weight
    if m:
        v, u = float(m.group(1)), m.group(2)
        return (v * (16 if u.startswith("lb") else 1), "weight")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(ct|count|pk|pack|ea)\b", sl)  # count
    if m:
        return (float(m.group(1)), "count")
    return None


def resolve_upc(term: str, location_id: str | None = None,
                size: str | None = None) -> dict | None:
    """Return the single best product candidate (with UPC) for a basket item.

    When a `size` hint is given (the flyer's product_size), pick the candidate
    whose size is CLOSEST to it — so we match the variant the user saw, not just
    the first result Kroger returns — with in-stock as a tie-breaker. Without a
    size hint, prefer the first in-stock candidate. Returns None when Kroger has
    no match.
    """
    candidates = [c for c in search_products(term, location_id=location_id, limit=10)
                  if c.get("upc")]
    if not candidates:
        return None

    def in_stock(c) -> bool:
        return c.get("stock_level") in _IN_STOCK_LEVELS

    desired = _parse_size(size)
    if desired:
        def size_distance(c) -> float:
            cs = _parse_size(c.get("size"))
            if cs is None or cs[1] != desired[1]:   # unparseable or different family
                return float("inf")
            return abs(cs[0] - desired[0])
        # Closest size first; among equal sizes, prefer in-stock.
        candidates.sort(key=lambda c: (size_distance(c), 0 if in_stock(c) else 1))
        return candidates[0]

    stocked = [c for c in candidates if in_stock(c)]
    return (stocked[0] if stocked else candidates[0])


# Locations: resolve a real Kroger store id (locationId) from a zip + banner.
#
# The Products API only returns price/stock — and a store-specific assortment —
# when you pass a real locationId. Our flyer data has a zip + banner but no
# Kroger locationId, so we look one up here. Cached per (zip, chain).

_location_cache: dict[tuple, str | None] = {}


def find_location_id(zip_code: str, chain: str | None = None) -> str | None:
    """Return the nearest Kroger-family store's official locationId for a zip,
    optionally restricted to a banner via `chain` (e.g. 'RALPHS', 'FRYS').

    Falls back to the nearest Kroger-family store of any banner when the chain
    filter yields nothing. Returns None when no store is found.
    """
    zip_code = (zip_code or "").strip()
    if not zip_code:
        return None
    chain_u = (chain or "").strip().upper() or None
    key = (zip_code, chain_u)
    if key in _location_cache:
        return _location_cache[key]

    headers = {"Authorization": f"Bearer {_client_credentials_token()}",
               "Accept": "application/json"}

    def _query(params: dict) -> list[dict]:
        try:
            resp = requests.get(f"{API_BASE}/locations", headers=headers,
                                params=params, timeout=15)
        except requests.RequestException:
            return []
        return resp.json().get("data", []) if resp.status_code == 200 else []

    base = {"filter.zipCode.near": zip_code, "filter.limit": 10}
    data = _query({**base, "filter.chain": chain_u}) if chain_u else []
    if not data:
        near = _query(base)
        if chain_u:
            data = [l for l in near if (l.get("chain") or "").upper() == chain_u] or near
        else:
            data = near

    location_id = data[0].get("locationId") if data else None
    _location_cache[key] = location_id
    return location_id


# Step 5: Add to the user's real Kroger cart

def add_to_cart(user_access_token: str, items: list[dict]) -> None:
    """PUT /v1/cart/add. items = [{"upc": str, "quantity": int, "modality": str}].

    Returns None on success (Kroger replies 204 No Content).
    Raises KrogerError(status=401) if the token is invalid/expired so the caller
    can refresh and retry.
    """
    if not items:
        raise KrogerError("No items to add to cart.", status=400)

    payload = {"items": [
        {
            "upc": it["upc"],
            "quantity": int(it.get("quantity", 1)),
            "modality": it.get("modality", DEFAULT_MODALITY),
        }
        for it in items
    ]}

    resp = requests.put(
        f"{API_BASE}/cart/add",
        headers={
            "Authorization": f"Bearer {user_access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=payload,
        timeout=20,
    )

    if resp.status_code in (200, 204):
        return None
    if resp.status_code == 401:
        raise KrogerError("Kroger access token expired or invalid.", status=401,
                          detail=_safe_json(resp))
    if resp.status_code == 403:
        raise KrogerError("Kroger cart permission denied.", status=403,
                          detail=_safe_json(resp))
    raise KrogerError(
        "Kroger cart add failed.",
        status=502,
        detail=_safe_json(resp),
    )


def _safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return {"status_code": resp.status_code, "text": resp.text[:500]}
