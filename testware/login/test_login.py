def sign_in(page, app, user, password):
    page.goto(app + "/login")
    page.fill("#user", user)
    page.fill("#password", password)
    page.click("#submit")
    return page.inner_text("#msg")


def test_LOGIN_0001(page, app, obs):
    obs.step(1, sign_in(page, app, "demo", "demo123"))


def test_LOGIN_0002(page, app, obs):
    obs.step(1, sign_in(page, app, "demo", "wrong"))


def test_LOGIN_0003(page, app, obs):
    obs.step(1, sign_in(page, app, "", "demo123"))
