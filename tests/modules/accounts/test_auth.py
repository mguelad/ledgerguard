def test_local_login_preserves_skip_link_keyboard_order(client, settings):
    settings.PRODUCTION = False
    response = client.get("/login/")
    assert response.status_code == 200
    html = response.content.decode()
    assert "autofocus" not in html
    assert html.index("Skip to content") < html.index('name="username"')
    assert 'name="password"' in html


def test_deployed_login_still_redirects_to_identity_provider(client, settings):
    settings.PRODUCTION = True
    response = client.get("/login/")
    assert response.status_code == 302
    assert response["Location"] == "/auth/start"
