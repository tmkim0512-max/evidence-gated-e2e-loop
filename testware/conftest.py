"""Shared fixtures for testware scripts.

A script records observations with obs.step(n, text). Each call writes the observed text and a
screenshot under EVIDENCE_DIR; the loop builds the observation packet from those files only.
"""
import json
import os
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR", "evidence"))


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def app():
    urllib.request.urlopen(urllib.request.Request(BASE_URL + "/__reset", method="POST"), timeout=5).close()
    return BASE_URL


@pytest.fixture
def page(browser):
    pg = browser.new_page()
    yield pg
    pg.close()


class Observer:
    def __init__(self, case: str, expects: dict, page):
        self.case, self.expects, self.page = case, expects, page

    def step(self, n: int, observed: str) -> None:
        out = EVIDENCE_DIR / self.case
        out.mkdir(parents=True, exist_ok=True)
        shot = out / f"{n}.png"
        self.page.screenshot(path=shot)
        record = {"key": f"{self.case}#{n}", "observed": observed, "screenshot": str(shot)}
        (out / f"{n}.json").write_text(json.dumps(record, ensure_ascii=False))
        assert self.expects[n] in observed, f"{self.case}#{n}: expected {self.expects[n]!r}, observed {observed!r}"


@pytest.fixture
def obs(request, page):
    case = request.node.name.removeprefix("test_").replace("_", "-")
    doc = json.loads((Path(request.node.fspath).parent / "cases.json").read_text())
    expects = {s["n"]: s["expect"] for c in doc["cases"] if c["id"] == case for s in c["steps"]}
    return Observer(case, expects, page)
