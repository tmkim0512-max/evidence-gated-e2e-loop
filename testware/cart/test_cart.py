def add_two(page, app):
    page.goto(app + "/cart")
    page.click("#add-A")
    page.click("#add-B")


def test_CART_0001(page, app, obs):
    add_two(page, app)
    obs.step(1, page.inner_text("#count"))
    obs.step(2, page.inner_text("#total"))


def test_CART_0002(page, app, obs):
    add_two(page, app)
    obs.step(1, page.inner_text("#count"))
    page.fill("#coupon", "SAVE10")
    page.click("#apply")
    obs.step(2, page.inner_text("#total"))


def test_CART_0003(page, app, obs):
    page.goto(app + "/cart")
    obs.step(1, page.inner_text("#count"))
