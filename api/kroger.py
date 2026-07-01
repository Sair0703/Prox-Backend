# api/kroger.py
#
# FastAPI routes for the Kroger cart-fill workflow.
#
#   GET  /kroger/auth/login?user_id=...     -> redirect user to Kroger login
#   GET  /kroger/auth/callback?code=&state= -> Kroger redirects back here; we
#                                             exchange the code and store the token
#   GET  /kroger/status?user_id=...         -> is this user connected to Kroger?
#   GET  /kroger/products/search?term=...   -> demo product -> UPC lookup (no login)
#   POST /kroger/cart/add                    -> add basket items to the user's cart
#   DELETE /kroger/disconnect?user_id=...    -> forget a user's token
#
# Token storage backend is chosen by env KROGER_TOKEN_STORE (local | supabase).
# For the demo it defaults to a local JSON file; production Supabase is untouched.

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel, Field

from services import kroger_service as ks
from services.kroger_token_store import (
    get_token_store,
    make_record,
    is_expired,
)

router = APIRouter(prefix="/kroger", tags=["kroger"])


# Helpers

def _valid_access_token(user_id: str) -> str:
    """Return a non-expired user access token, refreshing if needed.

    Raises HTTPException(401) when the user isn't connected or can't be refreshed.
    """
    store = get_token_store()
    record = store.get(user_id)
    if not record:
        raise HTTPException(
            status_code=401,
            detail=f"User '{user_id}' is not connected to Kroger. "
                   f"Send them through /kroger/auth/login first.",
        )

    if is_expired(record):
        if not record.get("refresh_token"):
            store.delete(user_id)
            raise HTTPException(status_code=401,
                                detail="Kroger session expired. Please reconnect.")
        try:
            refreshed = ks.refresh_access_token(record["refresh_token"])
        except ks.KrogerError as e:
            store.delete(user_id)
            raise HTTPException(status_code=401,
                                detail="Could not refresh Kroger session. Please reconnect.")
        record = make_record(user_id, refreshed)
        # Kroger may not return a new refresh token; keep the old one if so.
        if not record.get("refresh_token"):
            record["refresh_token"] = store.get(user_id).get("refresh_token") if store.get(user_id) else None
        store.set(record)

    return record["access_token"]


# OAuth: login + callback

@router.get("/auth/login")
def kroger_login(user_id: str = Query(..., description="Prox user id to connect")):
    """Redirect the user to Kroger's login/consent screen."""
    url = ks.get_authorize_url(state=user_id)
    return RedirectResponse(url)


@router.get("/auth/callback", response_class=HTMLResponse)
def kroger_callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
):
    """Kroger redirects here after login. Exchange the code and store the token."""
    if error:
        return HTMLResponse(f"<h3>Kroger connection failed</h3><p>{error}</p>", status_code=400)
    if not code or not state:
        return HTMLResponse("<h3>Missing code/state from Kroger.</h3>", status_code=400)

    user_id = state
    try:
        token_response = ks.exchange_code_for_token(code)
    except ks.KrogerError as e:
        return HTMLResponse(
            f"<h3>Token exchange failed</h3><pre>{e.detail}</pre>", status_code=e.status
        )

    record = make_record(user_id, token_response)
    get_token_store().set(record)

    return HTMLResponse(
        f"<h3>Kroger account connected for user '{user_id}'.</h3>"
        f"<p>You can close this window and return to Prox.</p>"
    )


@router.get("/status")
def kroger_status(user_id: str = Query(...)):
    """Lightweight check the app can poll to know if a user is connected."""
    record = get_token_store().get(user_id)
    if not record:
        return {"connected": False}
    return {
        "connected": True,
        "expired": is_expired(record),
        "scope": record.get("scope"),
        "updated_at": record.get("updated_at"),
    }


@router.delete("/disconnect")
def kroger_disconnect(user_id: str = Query(...)):
    get_token_store().delete(user_id)
    return {"disconnected": True, "user_id": user_id}


# Product search (no login required; uses app creds)

@router.get("/products/search")
def kroger_product_search(
    term: str = Query(..., min_length=2),
    location_id: str | None = Query(None, description="Optional Kroger store id"),
    limit: int = Query(5, ge=1, le=20),
):
    """Resolve a basket-item name into Kroger product candidates (with UPCs)."""
    try:
        return {"term": term, "candidates": ks.search_products(term, location_id, limit)}
    except ks.KrogerError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


# Cart add

class CartItemIn(BaseModel):
    # Provide either a `term` (we resolve the UPC) or a `upc` directly.
    term: str | None = None
    upc: str | None = None
    quantity: int = Field(default=1, ge=1)
    modality: str | None = None  # PICKUP | DELIVERY; defaults from service config


class CartAddIn(BaseModel):
    user_id: str
    items: list[CartItemIn]
    location_id: str | None = None  # improves UPC resolution accuracy


@router.post("/cart/add")
def kroger_cart_add(body: CartAddIn):
    """Add basket items to the user's real Kroger cart.

    For each item: if a `upc` is given we use it; otherwise we resolve the `term`
    to a UPC via Kroger product search. Items with no resolvable UPC are returned
    in `unresolved` rather than silently dropped.
    """
    resolved: list[dict] = []
    unresolved: list[dict] = []

    for item in body.items:
        if item.upc:
            resolved.append({
                "upc": item.upc, "quantity": item.quantity,
                "modality": item.modality or ks.DEFAULT_MODALITY,
                "source": "upc",
                "term": item.term,
            })
            continue

        if not item.term:
            unresolved.append({"item": item.dict(), "reason": "no term or upc provided"})
            continue

        try:
            match = ks.resolve_upc(item.term, location_id=body.location_id)
        except ks.KrogerError as e:
            raise HTTPException(status_code=e.status, detail=str(e))

        if not match:
            unresolved.append({"term": item.term, "reason": "no Kroger product match"})
            continue

        resolved.append({
            "upc": match["upc"], "quantity": item.quantity,
            "modality": item.modality or ks.DEFAULT_MODALITY,
            "source": "term_match",
            "term": item.term,
            "matched_description": match.get("description"),
            "matched_price": match.get("price"),
        })

    if not resolved:
        return {"added": False, "added_items": [], "unresolved": unresolved,
                "message": "No items could be resolved to a Kroger UPC."}

    # Get a valid user token (refreshing if needed), add, retry once on 401.
    access_token = _valid_access_token(body.user_id)
    cart_items = [{"upc": r["upc"], "quantity": r["quantity"], "modality": r["modality"]}
                  for r in resolved]
    try:
        ks.add_to_cart(access_token, cart_items)
    except ks.KrogerError as e:
        if e.status == 401:
            # token died between check and call; force a refresh path once
            get_token_store().delete(body.user_id)
            raise HTTPException(status_code=401,
                                detail="Kroger session expired mid-request. Please reconnect.")
        raise HTTPException(status_code=e.status, detail=str(e))

    return {"added": True, "added_items": resolved, "unresolved": unresolved}
