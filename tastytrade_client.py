"""Minimal tastytrade Open API client using official OAuth2 refresh-token flow."""
from __future__ import annotations
import time
import requests


class TastytradeAPIError(RuntimeError):
    pass


class TastytradeClient:
    def __init__(self, client_secret, refresh_token, client_id="", base_url="https://api.cert.tastyworks.com",
                 user_agent="TastyTraderBot/3.0", timeout=20):
        if not client_secret or not refresh_token:
            raise ValueError("client_secret and refresh_token are required")
        if base_url not in ("https://api.cert.tastyworks.com", "https://api.tastyworks.com"):
            raise ValueError("Unrecognized tastytrade API base URL.")
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.timeout = timeout
        self._access_token = None
        self._expires_at = 0

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
            # Some endpoints return one object rather than a collection.
            return [data]
        return []

    def get_accounts(self):
        return self._items(self._request("GET", "/customers/me/accounts"))

    def get_balances(self, account_number):
        return self._request("GET", f"/accounts/{account_number}/balances")

    def get_positions(self, account_number):
        return self._items(self._request("GET", f"/accounts/{account_number}/positions"))

    def get_orders(self, account_number):
        return self._items(self._request("GET", f"/accounts/{account_number}/orders"))

    def dry_run_equity_order(self, account_number, symbol, action, quantity, limit_price):
        symbol = symbol.strip().upper()
        if not symbol.isalnum() or not (1 <= len(symbol) <= 6):
            raise ValueError("Ticker must be 1–6 alphanumeric characters.")
        if action not in ("Buy to Open", "Sell to Close"):
            raise ValueError("Only Buy to Open or Sell to Close are supported.")
        if int(quantity) < 1 or float(limit_price) <= 0:
            raise ValueError("Quantity and limit price must be positive.")
        # Validation endpoint only. Deliberately no method that submits an order.
        order = {
            "time-in-force": "Day",
            "order-type": "Limit",
            "price": f"{float(limit_price):.2f}",
            "price-effect": "Debit" if action == "Buy to Open" else "Credit",
            "source": "TastyTraderBot-v3-dry-run",
            "external-identifier": f"ttbot-dryrun-{int(time.time())}-{symbol}",
            "legs": [{
                "instrument-type": "Equity",
                "symbol": symbol,
                "quantity": int(quantity),
                "action": action,
            }],
        }
        return self._request("POST", f"/accounts/{account_number}/orders/dry-run", json_body=order)
