"""
Shopify Admin REST API wrapper.
Loads credentials from .env — never hardcode secrets here.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

STORE_URL = os.getenv("SHOPIFY_STORE_URL")
ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-01")


class ShopifyClient:
    def __init__(self):
        if not STORE_URL or not ACCESS_TOKEN:
            raise ValueError(
                "SHOPIFY_STORE_URL and SHOPIFY_ACCESS_TOKEN must be set in .env"
            )
        self.base_url = f"https://{STORE_URL}/admin/api/{API_VERSION}"
        self.headers = {
            "X-Shopify-Access-Token": ACCESS_TOKEN,
            "Content-Type": "application/json",
        }

    def get(self, path: str, params: dict = None) -> dict:
        response = requests.get(
            f"{self.base_url}{path}",
            headers=self.headers,
            params=params or {},
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def put(self, path: str, payload: dict) -> dict:
        response = requests.put(
            f"{self.base_url}{path}",
            headers=self.headers,
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def post(self, path: str, payload: dict) -> dict:
        response = requests.post(
            f"{self.base_url}{path}",
            headers=self.headers,
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()
