"""Tiny shop used as the system under test. stdlib only.

BUG=cart_total  makes the cart total wrong (for the drift demo).
Known defect (always on): coupon codes are accepted but never applied.
"""
import argparse
import html
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

PRODUCTS = {"A": ("Notebook", 10_000), "B": ("Pen set", 20_000)}
STATE = {"cart": [], "orders": 0}
PAGE = """<!doctype html><meta charset="utf-8"><title>{title}</title>
<nav><a href="/login">login</a> | <a href="/cart">cart</a> | <a href="/checkout">checkout</a></nav>
<h1>{title}</h1>{body}"""


def cart_total() -> int:
    total = sum(PRODUCTS[sku][1] for sku in STATE["cart"])
    if os.environ.get("BUG") == "cart_total" and len(STATE["cart"]) >= 2:
        total -= 5_000
    return total


def login_page(msg="") -> str:
    return (f'<form method="post" action="/login"><input id="user" name="user"> '
            f'<input id="password" name="password" type="password"> <button id="submit">Sign in</button></form>'
            f'<p id="msg">{html.escape(msg)}</p>')


def cart_page() -> str:
    buttons = "".join(f'<form method="post" action="/cart/add"><button id="add-{sku}" name="sku" value="{sku}">'
                      f'Add {name}</button></form>' for sku, (name, _) in PRODUCTS.items())
    if not STATE["cart"]:
        return buttons + '<p id="count">Your cart is empty</p>'
    rows = "".join(f"<li>{PRODUCTS[s][0]}</li>" for s in STATE["cart"])
    return (buttons + f'<ul>{rows}</ul><p id="count">Items: {len(STATE["cart"])}</p>'
            f'<p id="total">Total: {cart_total():,}</p>'
            '<form method="post" action="/cart/coupon"><input id="coupon" name="code"> '
            '<button id="apply">Apply</button></form>')


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, status=200, title="Shop") -> None:
        data = PAGE.format(title=title, body=body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to: str) -> None:
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def _form(self) -> dict:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def do_GET(self):
        routes = {"/__health": lambda: "ok", "/login": login_page, "/cart": cart_page,
                  "/checkout": lambda: '<form method="post" action="/checkout"><button id="place">Place order</button></form>'}
        if self.path not in routes:
            return self._send("not found", 404)
        self._send(routes[self.path](), title=self.path.strip("/").title())

    def do_POST(self):
        form = self._form()
        if self.path == "/__reset":
            STATE.update(cart=[], orders=0)
            return self._send("reset")
        if self.path == "/login":
            if not form.get("user"):
                return self._send(login_page("Username required"), title="Login")
            ok = form["user"] == "demo" and form.get("password") == "demo123"
            return self._send(login_page("Welcome, demo" if ok else "Invalid credentials"), title="Login")
        if self.path == "/cart/add" and form.get("sku") in PRODUCTS:
            STATE["cart"].append(form["sku"])
            return self._redirect("/cart")
        if self.path == "/cart/coupon":  # known defect: the code is accepted but never applied
            return self._redirect("/cart")
        if self.path == "/checkout":
            if not STATE["cart"]:
                return self._send('<p id="msg">Cart is empty</p>', title="Checkout")
            STATE["orders"] += 1
            STATE["cart"] = []
            return self._send(f'<p id="msg">Order confirmed: #{STATE["orders"]}</p>', title="Checkout")
        self._send("not found", 404)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ThreadingHTTPServer(("127.0.0.1", ap.parse_args().port), Handler).serve_forever()
