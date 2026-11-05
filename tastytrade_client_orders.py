"""tastytrade Open API client with explicit order preparation and submission guards.

Supports the official OAuth2 refresh-token flow and single-leg equity orders,
including notional market orders for fractional-eligible equities.
Always use dry-run first. Never blindly retry a submission after a timeout/5xx.
"""
from __future__ import annotations

import time
import uuid
import math
import requests

SANDBOX_URL = "https://api.cert.tastyworks.com"
PRODUCTION_URL = "https://api.tastyworks.com"


class TastytradeAPIError(RuntimeError):
    pass


class TastytradeClient:
    def __init__(self, client_secret, refresh_token, client_id="", base_url=SANDBOX_URL,
                 user_agent="StockTrader/1.0", timeout=20):
        if not client_secret or not refresh_token:
            raise ValueError("client_secret and refresh_token are required")
        if base_url not in (SANDBOX_URL, PRODUCTION_URL):
            raise ValueError("Unrecognized tastytrade API base URL.")
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.timeout = timeout
        self._access_token = None
        self._expires_at = 0

    @property
    def is_production(self):
        return self.base_url == PRODUCTION_URL

    def _refresh_access_token(self):
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_secret": self.client_secret,
        }
        if self.client_id:
            payload["client_id"] = self.client_id
        response = requests.post(
            f"{self.base_url}/oauth/token", json=payload,
            headers={"User-Agent": self.user_agent, "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        if not response.ok:
            raise TastytradeAPIError(f"OAuth token refresh failed ({response.status_code}): {response.text[:500]}")
        data = response.json()
        token = data.get("access_token")
        if not token:
            raise TastytradeAPIError("OAuth response did not contain access_token.")
        self._access_token = token
        self._expires_at = time.time() + max(30, int(data.get("expires_in", 900)) - 45)

    def _request(self, method, path, *, params=None, json_body=None, retry_auth=True):
        if not self._access_token or time.time() >= self._expires_at:
            self._refresh_access_token()
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        response = requests.request(method, self.base_url + path, headers=headers,
                                    params=params, json=json_body, timeout=self.timeout)
        # Retry once only after 401; the request was rejected as unauthenticated.
        if response.status_code == 401 and retry_auth:
            self._access_token = None
            self._refresh_access_token()
            return self._request(method, path, params=params, json_body=json_body, retry_auth=False)
        if not response.ok:
            raise TastytradeAPIError(f"{method} {path} failed ({response.status_code}): {response.text[:1000]}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise TastytradeAPIError(f"API returned non-JSON response for {path}") from exc

    @staticmethod
    def _items(response):
        data = response.get("data", response)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "accounts", "orders", "positions"):
                if isinstance(data.get(key), list):
                    return data[key]
            return [data]
        return []

    def get_accounts(self):
        return self._items(self._request("GET", "/customers/me/accounts"))

    def get_balances(self, account_number):
        return self._request("GET", f"/accounts/{account_number}/balances")

    def get_positions(self, account_number):
        return self._items(self._request("GET", f"/accounts/{account_number}/positions"))

    def get_equity(self, symbol):
        """Return the broker's current tradability and fractional-share flags."""
        symbol = str(symbol).strip().upper()
        return self._request("GET", f"/instruments/equities/{symbol}")

    def get_orders(self, account_number):
        return self._items(self._request("GET", f"/accounts/{account_number}/orders"))

    def cancel_order(self, account_number, order_id):
        """Request cancellation of a live order; broker status must be refreshed afterward."""
        if not str(order_id).isdigit():
            raise ValueError("A numeric tastytrade order id is required for cancellation.")
        return self._request("DELETE", f"/accounts/{account_number}/orders/{order_id}")

    @staticmethod
    def build_equity_order(symbol, action, quantity, limit_price, source="StockTrader",
                           allow_fractional=False):
        symbol = str(symbol).strip().upper()
        if not symbol.isalnum() or not (1 <= len(symbol) <= 6):
            raise ValueError("Ticker must be 1–6 alphanumeric characters.")
        if action not in ("Buy to Open", "Sell to Close", "Sell to Open", "Buy to Close"):
            raise ValueError("Action must be Buy to Open, Sell to Close, Sell to Open, or Buy to Close.")
        quantity = float(quantity)
        if not quantity > 0 or not math.isfinite(quantity):
            raise ValueError("Quantity must be positive and finite.")
        if not allow_fractional and not quantity.is_integer():
            raise ValueError("Quantity must be a positive whole number of shares.")
        if allow_fractional and (action not in ("Buy to Open", "Sell to Close") or
                                 round(quantity, 4) != quantity):
            raise ValueError("Fractional quantities are limited to 4 decimals for long entries and exits.")
        if float(limit_price) <= 0:
            raise ValueError("Limit price must be positive.")
        order_id = f"stocktrader-{int(time.time())}-{uuid.uuid4().hex[:12]}"
        # tastytrade uses Notional Market orders for fractional eligible equities.
        # Use that order type for every eligible long entry/exit, even if the
        # calculated amount happens to equal a whole-share quantity.
        fractional_notional = allow_fractional
        order = {
            "time-in-force": "Day",
            "order-type": "Notional Market" if fractional_notional else "Limit",
            "source": source,
            "external-identifier": order_id,
            "legs": [{
                "instrument-type": "Equity",
                "symbol": symbol,
                "action": action,
            }],
        }
        if fractional_notional:
            order["value"] = f"{quantity * float(limit_price):.2f}"
            order["value-effect"] = "Credit" if action in ("Sell to Close", "Sell to Open") else "Debit"
        else:
            order["price"] = f"{float(limit_price):.2f}"
            order["price-effect"] = "Credit" if action in ("Sell to Close", "Sell to Open") else "Debit"
            order["legs"][0]["quantity"] = int(quantity)
        return order

    @staticmethod
    def build_equity_market_close(symbol, action, quantity, source="StockTrader/1.0.0"):
        """Build a single-leg market order that only closes an existing equity position."""
        if action not in ("Sell to Close", "Buy to Close"):
            raise ValueError("Market close action must be Sell to Close or Buy to Close.")
        order = TastytradeClient.build_equity_order(symbol, action, quantity, 1.0, source=source)
        order["order-type"] = "Market"
        order.pop("price", None)
        order.pop("price-effect", None)
        return order

    def dry_run_order(self, account_number, order_payload):
        """Preflight the exact payload that may later be submitted."""
        return self._request("POST", f"/accounts/{account_number}/orders/dry-run", json_body=order_payload)

    def submit_order(self, account_number, order_payload, *, confirm_live=False,
                     max_order_notional=1000.0, risk_reference_price=None):
        """Submit an already-previewed order. Production requires explicit confirmation.

        No automatic retry is performed on submission errors. If a timeout or 5xx occurs,
        inspect broker order history for the external-identifier before considering retry.
        """
        if not confirm_live:
            raise PermissionError("Order submission requires explicit confirmation.")
        if not self.is_production:
            # Sandbox orders can be used for end-to-end testing but are still explicitly gated.
            pass
        legs = order_payload.get("legs") or []
        if len(legs) != 1 or legs[0].get("instrument-type") != "Equity":
            raise ValueError("Only single-leg equity orders are supported.")
        order_type = order_payload.get("order-type")
        if order_type == "Notional Market":
            if not self.is_production:
                raise ValueError("Fractional equity notional orders are restricted to Production mode.")
            notional = float(order_payload.get("value", 0))
            price = 1.0
            quantity = None
        elif order_type == "Market":
            quantity = float(legs[0].get("quantity", 0))
            price = float(risk_reference_price or 0)
            notional = quantity * price
        else:
            quantity = float(legs[0].get("quantity", 0))
            price = float(order_payload.get("price", 0))
            notional = quantity * price
        if (quantity is not None and quantity <= 0) or notional <= 0 or price <= 0:
            raise ValueError("Order payload has invalid quantity or risk reference price.")
        actions = {"Buy to Open", "Sell to Close", "Sell to Open", "Buy to Close"}
        if legs[0].get("action") not in actions:
            raise ValueError("Order payload has an unsupported equity action.")
        if order_type == "Market" and legs[0].get("action") not in {"Sell to Close", "Buy to Close"}:
            raise ValueError("Market orders are restricted to closing equity positions.")
        if notional > float(max_order_notional):
            raise ValueError(f"Order notional ${notional:,.2f} exceeds configured cap ${max_order_notional:,.2f}.")
        external_id = order_payload.get("external-identifier")
        if not external_id:
            raise ValueError("Order must include a unique external-identifier.")
        # Never resubmit if this identifier is already present in broker order history.
        existing = self.get_orders(account_number)
        for item in existing:
            order = item.get("order", item) if isinstance(item, dict) else {}
            if order.get("external-identifier") == external_id:
                raise TastytradeAPIError(
                    f"Order identifier {external_id} already exists in broker history; refusing duplicate submission."
                )
        # Intentionally no retry on timeout/5xx: query broker history before any manual retry.
        return self._request("POST", f"/accounts/{account_number}/orders", json_body=order_payload)
