import socket
import threading
import time
from pathlib import Path
import pytest
import uvicorn
from anki.collection import Collection
from ankiweb.config import Settings
from ankiweb.app import create_app

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seed_collection(col_path: Path):
    col = Collection(str(col_path))
    try:
        did = col.decks.id("Default")
        col.decks.set_current(did)
        for i in range(1, 5):
            n = col.new_note(col.models.by_name("Basic"))
            n["Front"] = f"Front question {i}"
            n["Back"] = f"Back answer {i}"
            col.add_note(n, did)
    finally:
        col.close()


@pytest.fixture(scope="module")
def live_server_layout(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("layout_e2e")
    col_path = tmp_path / "layout.anki2"
    seed_collection(col_path)

    port = get_free_port()
    settings = Settings(collection_path=col_path, port=port)
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("live server did not start")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


@pytest.fixture
def live_server_flow(tmp_path: Path):
    col_path = tmp_path / "flow.anki2"
    seed_collection(col_path)

    port = get_free_port()
    settings = Settings(collection_path=col_path, port=port)
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("live server did not start")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


VIEWPORT_DEVICES = [
    ("iPhone 13 portrait", "iPhone 13", False),
    ("iPhone 13 landscape", "iPhone 13", True),
    ("Pixel 7 portrait", "Pixel 7", False),
    ("iPad (gen 7)", "iPad (gen 7)", False),
    ("desktop 1280x800", None, False),
]


def _setup_context(playwright, browser, device_key: str, is_landscape_swap: bool):
    if device_key:
        dev = playwright.devices[device_key]
        if is_landscape_swap:
            vp = dev["viewport"]
            dev_config = dict(dev)
            dev_config["viewport"] = {"width": vp["height"], "height": vp["width"]}
            return browser.new_context(**dev_config)
        return browser.new_context(**dev)
    return browser.new_context(viewport={"width": 1280, "height": 800})


@pytest.mark.parametrize("name,device_key,swap", VIEWPORT_DEVICES)
def test_viewports_no_horizontal_scroll_and_nav(live_server_layout, name, device_key, swap):
    base = live_server_layout
    screens = ["/deckbrowser", "/overview", "/add", "/browse", "/reviewer"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = _setup_context(p, browser, device_key, swap)
        page = context.new_page()
        vp_width = page.viewport_size["width"]
        is_compact = vp_width < 640

        for path in screens:
            page.goto(f"{base}{path}")
            page.wait_for_load_state("domcontentloaded")

            # 1. No horizontal page scroll at any width
            page.wait_for_function(
                "() => document.documentElement.scrollWidth <= window.innerWidth + 1",
                timeout=5000,
            )
            scroll_w = page.evaluate("document.documentElement.scrollWidth")
            inner_w = page.evaluate("window.innerWidth")
            assert scroll_w <= inner_w + 1, (
                f"{path} on {name}: scrollWidth ({scroll_w}) > innerWidth ({inner_w})"
            )

            # 2. Navigation assertions
            if path in ("/deckbrowser", "/overview", "/add", "/browse"):
                if is_compact:
                    # Compact: #ankiweb-bottomnav visible with exactly 5 tabs, each >= 44x44
                    page.wait_for_selector("#ankiweb-bottomnav", state="visible", timeout=5000)
                    assert not page.is_visible("#ankiweb-toolbar"), (
                        f"{path} on {name}: top toolbar should be hidden on compact"
                    )
                    tabs = page.locator("#ankiweb-bottomnav .tab-item")
                    assert tabs.count() == 5, f"{path} on {name}: expected exactly 5 tabs"
                    for idx in range(5):
                        box = tabs.nth(idx).bounding_box()
                        assert box is not None, f"tab {idx} bounding box is None"
                        assert box["width"] >= 44, (
                            f"{path} on {name} tab {idx} width {box['width']} < 44"
                        )
                        assert box["height"] >= 44, (
                            f"{path} on {name} tab {idx} height {box['height']} < 44"
                        )
                    # Verify active tab is indicated
                    active_tab = page.locator("#ankiweb-bottomnav .tab-item.active")
                    assert active_tab.count() >= 1, f"no active tab indicated on {path}"
                    assert path in active_tab.first.get_attribute("href"), (
                        f"active tab href {active_tab.first.get_attribute('href')} does not match {path}"
                    )
                else:
                    # Wide/medium: #ankiweb-bottomnav not visible and #ankiweb-toolbar visible
                    page.wait_for_selector("#ankiweb-toolbar", state="visible", timeout=5000)
                    assert not page.is_visible("#ankiweb-bottomnav"), (
                        f"{path} on {name}: bottom nav should be hidden on wide/medium"
                    )

            elif path == "/reviewer":
                # Reviewer screen:
                if is_compact:
                    # Tab bar hidden so answer buttons have room; compact top bar has way back to Decks
                    assert not page.is_visible("#ankiweb-bottomnav"), (
                        f"reviewer on {name}: bottomnav should be hidden on compact reviewer"
                    )
                    assert page.is_visible("#ankiweb-toolbar a[href='/deckbrowser']"), (
                        f"reviewer on {name}: way back to Decks must be visible"
                    )
                else:
                    assert page.is_visible("#ankiweb-toolbar"), (
                        f"reviewer on {name}: top toolbar should be visible on wide/medium"
                    )
                    assert not page.is_visible("#ankiweb-bottomnav"), (
                        f"reviewer on {name}: bottom nav should be hidden on wide/medium"
                    )

                # Show Answer & Rating buttons verification
                page.wait_for_selector("#ansbut", timeout=6000)
                ans_box = page.locator("#ansbut").bounding_box()
                assert ans_box is not None and ans_box["height"] >= 44 and ans_box["width"] >= 44

                page.click("#ansbut")
                page.wait_for_selector(".ease-row button.ease", timeout=6000)
                ease_btns = page.locator(".ease-row button.ease")
                assert ease_btns.count() == 4
                vp = page.viewport_size
                for i in range(4):
                    box = ease_btns.nth(i).bounding_box()
                    assert box is not None, f"ease {i} bounding box is None"
                    assert box["width"] >= 44, f"ease {i} width {box['width']} < 44 on {name}"
                    assert box["height"] >= 44, f"ease {i} height {box['height']} < 44 on {name}"
                    assert box["x"] >= 0, f"ease {i} x {box['x']} < 0 on {name}"
                    assert box["y"] >= 0, f"ease {i} y {box['y']} < 0 on {name}"
                    assert box["x"] + box["width"] <= vp["width"] + 1, (
                        f"ease {i} overflows x ({box['x'] + box['width']} > {vp['width']}) on {name}"
                    )
                    assert box["y"] + box["height"] <= vp["height"] + 1, (
                        f"ease {i} overflows y ({box['y'] + box['height']} > {vp['height']}) on {name}"
                    )

        browser.close()


def test_card_tap_does_not_answer(live_server_layout):
    base = live_server_layout
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(**p.devices["iPhone 13"])
        page = context.new_page()
        page.goto(f"{base}/reviewer")
        page.wait_for_selector("#ansbut", timeout=6000)

        # Tapping card body does not answer
        page.click("#qa")
        assert page.is_visible("#ansbut")
        assert not page.is_visible(".ease-row")

        browser.close()


def test_more_bottom_sheet_keyboard_reachable_and_closable(live_server_layout):
    base = live_server_layout
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(**p.devices["iPhone 13"])
        page = context.new_page()
        page.goto(f"{base}/deckbrowser")
        page.wait_for_selector("#ankiweb-more-btn", timeout=6000)

        # Open bottom sheet via More button
        page.click("#ankiweb-more-btn")
        page.wait_for_selector("#ankiweb-more-sheet:not([hidden])", timeout=4000)

        # Check required entries in the More sheet
        assert page.is_visible("#ankiweb-more-sheet a[href='/graphs']")
        assert page.is_visible("#ankiweb-more-sheet a[href='/preferences']")
        assert page.is_visible("#ankiweb-more-sheet a[href='/tools']")
        assert page.is_visible("#ankiweb-more-sheet a[href='/notify']")
        assert page.is_visible("#ankiweb-more-sheet a[href='/about']")
        assert page.is_visible("#ankiweb-more-sheet button[onclick*='ankiwebToggleNight']")

        # Sheet is keyboard reachable and closable via Escape key
        page.keyboard.press("Escape")
        page.wait_for_function(
            "() => document.getElementById('ankiweb-more-sheet').hasAttribute('hidden')",
            timeout=4000,
        )
        assert not page.is_visible("#ankiweb-more-sheet .panel")

        # Can also open and close via close button
        page.click("#ankiweb-more-btn")
        page.wait_for_selector("#ankiweb-more-sheet:not([hidden])", timeout=4000)
        page.click("#ankiweb-more-sheet .close")
        page.wait_for_function(
            "() => document.getElementById('ankiweb-more-sheet').hasAttribute('hidden')",
            timeout=4000,
        )

        browser.close()


def test_critical_review_flow_iphone13(live_server_flow):
    base = live_server_flow
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(**p.devices["iPhone 13"])
        page = context.new_page()

        # Critical flow on iPhone 13 portrait:
        # open deck -> study -> show answer -> click Good -> next card or congrats screen appears;
        # undo works if an undo control is present.
        page.goto(f"{base}/deckbrowser")
        page.wait_for_selector("a.deck", timeout=6000)
        page.click("a.deck")

        # Navigated to /overview
        page.wait_for_selector("#study", timeout=6000)
        page.click("#study")

        # Navigated to /reviewer
        page.wait_for_selector("#ansbut", timeout=6000)
        page.click("#ansbut")

        # Ease buttons displayed, click Good (data-ease='3')
        page.wait_for_selector("button.ease[data-ease='3']", timeout=6000)
        page.click("button.ease[data-ease='3']")

        # Next card or congrats screen appears
        page.wait_for_function(
            "() => !!document.getElementById('ansbut') || !!document.querySelector('.congrats')",
            timeout=6000,
        )

        # Undo works if an undo control is present
        undo_btn = page.locator("#rev-actions button:has-text('Undo')")
        if undo_btn.is_visible():
            undo_btn.click()
            # Wait for card to be restored (Show Answer button appears again)
            page.wait_for_selector("#ansbut", timeout=6000)

        browser.close()


def test_dark_mode_and_prefers_color_scheme(live_server_layout):
    base = live_server_layout
    with sync_playwright() as p:
        browser = p.chromium.launch()
        # 1. Respect prefers-color-scheme: dark
        context = browser.new_context(**p.devices["iPhone 13"], color_scheme="dark")
        page = context.new_page()
        page.goto(f"{base}/deckbrowser")
        page.wait_for_selector("#ankiweb-bottomnav", timeout=5000)
        nav_bg = page.evaluate("() => getComputedStyle(document.getElementById('ankiweb-bottomnav')).backgroundColor")
        # In dark mode, background should not be white rgb(255, 255, 255)
        assert nav_bg not in ("rgb(255, 255, 255)", "#ffffff")
        contrast = page.locator("a.deck").first.evaluate(
            """el => {
              const rgb = s => (s.match(/[0-9.]+/g) || []).slice(0,3).map(Number);
              const lum = c => {
                const v = c.map(x => x / 255).map(x => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4);
                return .2126 * v[0] + .7152 * v[1] + .0722 * v[2];
              };
              const fg = lum(rgb(getComputedStyle(el).color));
              const bg = lum(rgb(getComputedStyle(document.body).backgroundColor));
              return (Math.max(fg,bg)+.05)/(Math.min(fg,bg)+.05);
            }"""
        )
        assert contrast >= 4.5

        # 2. Night-mode class toggle
        context_light = browser.new_context(**p.devices["iPhone 13"], color_scheme="light")
        page_light = context_light.new_page()
        page_light.goto(f"{base}/deckbrowser")
        page_light.wait_for_selector("#ankiweb-more-btn", timeout=5000)
        page_light.click("#ankiweb-more-btn")
        page_light.wait_for_selector("#ankiweb-more-sheet:not([hidden])", timeout=4000)
        # Click toggle night mode button
        page_light.click("#ankiweb-more-sheet button[onclick*='ankiwebToggleNight']")
        page_light.wait_for_load_state("domcontentloaded")
        page_light.wait_for_function("() => document.documentElement.classList.contains('night-mode')", timeout=5000)
        assert page_light.evaluate("() => document.documentElement.classList.contains('night-mode')")

        browser.close()


def test_add_chrome_does_not_cover_editor_on_phone(live_server_layout):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(**p.devices["iPhone 13"]).new_page()
        page.goto(f"{live_server_layout}/add")
        page.wait_for_selector(".note-editor", timeout=10000)
        chrome = page.locator("#add-chrome").bounding_box()
        editor = page.locator(".note-editor").bounding_box()
        assert chrome and editor
        assert editor["y"] >= chrome["y"] + chrome["height"] - 1
        browser.close()


def test_text_zoom_200_percent_actions_reachable(live_server_layout):
    base = live_server_layout
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(**p.devices["iPhone 13"])
        page = context.new_page()
        page.goto(f"{base}/deckbrowser")
        page.wait_for_selector("#ankiweb-bottomnav", timeout=5000)

        # Scale font size to 200%
        page.evaluate("() => { document.documentElement.style.fontSize = '200%'; }")

        # Verify all 5 tabs in bottomnav remain clickable and reachable
        tabs = page.locator("#ankiweb-bottomnav .tab-item")
        assert tabs.count() == 5
        for i in range(5):
            box = tabs.nth(i).bounding_box()
            assert box is not None
            assert box["width"] >= 44
            assert box["height"] >= 44

        # Test on /reviewer with 200% font zoom
        page.goto(f"{base}/reviewer")
        page.wait_for_selector("#ansbut", timeout=6000)
        page.evaluate("() => { document.documentElement.style.fontSize = '200%'; }")
        ans_box = page.locator("#ansbut").bounding_box()
        assert ans_box is not None and ans_box["height"] >= 44

        page.click("#ansbut")
        page.wait_for_selector(".ease-row button.ease", timeout=6000)
        ease_btns = page.locator(".ease-row button.ease")
        for i in range(4):
            box = ease_btns.nth(i).bounding_box()
            assert box is not None
            assert box["height"] >= 44
            assert box["width"] >= 44

        browser.close()


def test_browse_compact_drilldown_iphone13(live_server_layout):
    base = live_server_layout
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(**p.devices["iPhone 13"])
        page = context.new_page()

        page.goto(f"{base}/browse")
        page.wait_for_selector("#search", timeout=6000)

        # 1. Search
        page.fill("#search", "Front")
        page.keyboard.press("Enter")
        page.wait_for_selector("#results-body tr.browser-row", timeout=6000)

        # Initial compact browse: list visible, detail hidden
        assert page.is_visible("#results-wrap")
        assert not page.is_visible("#detail")

        # 2. Tap row
        page.click("#results-body tr.browser-row")

        # 3. Detail visible and list hidden
        page.wait_for_selector("#detail", state="visible", timeout=6000)
        assert page.is_visible("#detail")
        assert not page.is_visible("#results-wrap")

        # 4. Back
        page.wait_for_selector("#browser-back-bar button", timeout=6000)
        page.click("#browser-back-bar button")

        # 5. List visible
        page.wait_for_selector("#results-wrap", state="visible", timeout=6000)
        assert page.is_visible("#results-wrap")
        assert not page.is_visible("#detail")

        browser.close()


def test_add_screen_compact_stacked_fields_and_sticky_button(live_server_layout):
    base = live_server_layout
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(**p.devices["iPhone 13"])
        page = context.new_page()

        page.goto(f"{base}/add")
        page.wait_for_selector("#add-btn", timeout=6000)
        page.wait_for_selector(".field-container", timeout=6000)

        # Fields stack full width
        fields = page.locator(".field-container")
        assert fields.count() >= 2
        vp = page.viewport_size
        for i in range(fields.count()):
            f_box = fields.nth(i).bounding_box()
            assert f_box is not None
            assert f_box["width"] >= vp["width"] - 20
            if i > 0:
                prev_box = fields.nth(i - 1).bounding_box()
                assert f_box["y"] >= prev_box["y"] + prev_box["height"] - 1

        # Scroll page to its end
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        # 1. No horizontal scroll
        scroll_w = page.evaluate("document.documentElement.scrollWidth")
        inner_w = page.evaluate("window.innerWidth")
        assert scroll_w <= inner_w + 1, f"horizontal scroll: scrollWidth ({scroll_w}) > innerWidth ({inner_w})"

        # 2. Add button fully inside the viewport after scrolling
        btn_box = page.locator("#add-btn").bounding_box()
        assert btn_box is not None, "add button bounding box is None"
        assert btn_box["x"] >= 0
        assert btn_box["y"] >= 0
        assert btn_box["x"] + btn_box["width"] <= vp["width"] + 1, (
            f"add button overflows viewport width: {btn_box['x'] + btn_box['width']} > {vp['width']}"
        )
        assert btn_box["y"] + btn_box["height"] <= vp["height"] + 1, (
            f"add button overflows viewport height: {btn_box['y'] + btn_box['height']} > {vp['height']}"
        )

        # 3. Not overlapped by bottom nav
        nav_box = page.locator("#ankiweb-bottomnav").bounding_box()
        assert nav_box is not None
        assert btn_box["y"] + btn_box["height"] <= nav_box["y"] + 1, (
            f"add button overlapped by bottom nav: btn bottom {btn_box['y'] + btn_box['height']} > nav top {nav_box['y']}"
        )

        browser.close()


def test_sveltekit_graphs_mobile_chrome_affordance(live_server_layout):
    base = live_server_layout
    with sync_playwright() as p:
        browser = p.chromium.launch()

        # 1. iPhone 13 portrait: visible, >= 44x44, and navigates to /deckbrowser
        context = browser.new_context(**p.devices["iPhone 13"])
        page = context.new_page()
        page.goto(f"{base}/graphs")
        page.wait_for_selector("#ankiweb-spa-back", state="visible", timeout=6000)

        box = page.locator("#ankiweb-spa-back").bounding_box()
        assert box is not None
        assert box["width"] >= 44, f"affordance width {box['width']} < 44"
        assert box["height"] >= 44, f"affordance height {box['height']} < 44"

        page.click("#ankiweb-spa-back")
        page.wait_for_url("**/deckbrowser*", timeout=6000)
        assert "/deckbrowser" in page.url

        # 2. Preserves #night convention
        page.goto(f"{base}/graphs#night")
        page.wait_for_selector("#ankiweb-spa-back", state="visible", timeout=6000)
        page.click("#ankiweb-spa-back")
        page.wait_for_url("**/deckbrowser*", timeout=6000)
        assert "/deckbrowser#night" in page.url
        context.close()

        # 3. Desktop >= 640px: hidden
        d_context = browser.new_context(viewport={"width": 1280, "height": 800})
        d_page = d_context.new_page()
        d_page.goto(f"{base}/graphs")
        d_page.wait_for_selector(".graphs-container", timeout=10000)
        assert not d_page.is_visible("#ankiweb-spa-back")
        d_context.close()

        browser.close()
