def test_CHECKOUT_0001(page, app, obs):
    page.goto(app + "/cart")
    page.click("#add-A")
    page.goto(app + "/checkout")
    page.click("#place")
    obs.step(1, page.inner_text("#msg"))
    page.goto(app + "/cart")
    obs.step(2, page.inner_text("#count"))


def test_CHECKOUT_0002(page, app, obs):
    page.goto(app + "/checkout")
    page.click("#place")
    obs.step(1, page.inner_text("#msg"))
