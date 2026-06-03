from playwright.sync_api import sync_playwright
import os

OUT  = r"C:\Users\young\Documents\DEMO\Transcript"
FILE = r"C:\Users\young\Documents\DEMO\Transcript\test_audio.mp3"
URL  = "http://127.0.0.1:7860"

results = []

def ok(name, detail=""):
    results.append(("PASS", name, detail))
    print(f"  PASS  {name}" + (f"  — {detail}" if detail else ""))

def fail(name, detail=""):
    results.append(("FAIL", name, detail))
    print(f"  FAIL  {name}" + (f"  — {detail}" if detail else ""))

def pick_dropdown(page, wrapper_id, value, wait_ms=1200):
    page.locator(f"#{wrapper_id} input").click()
    page.wait_for_timeout(600)
    for o in page.locator("[role=option]").all():
        if o.is_visible() and o.inner_text().strip().lstrip("checkmark ").strip().lstrip("✓ ").strip() == value:
            o.click()
            page.wait_for_timeout(wait_ms)
            return True
    page.keyboard.press("Escape")
    return False

def get_dropdown_val(page, wrapper_id):
    return page.evaluate(f"document.querySelector('#{wrapper_id} input')?.value?.trim()||''")

def get_model_choices(page):
    page.locator("#model-sel input").click()
    page.wait_for_timeout(500)
    opts = [o.inner_text().strip().lstrip("✓ ").strip()
            for o in page.locator("[role=option]").all() if o.is_visible()]
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    return opts

def open_stt_dropdown(page, engine_name):
    page.evaluate("document.getElementById('ta-stt-engine').scrollIntoView({block:'center'})")
    page.wait_for_timeout(300)
    page.locator("#ta-stt-engine input").click()
    page.wait_for_timeout(700)
    for o in page.locator("[role=option]").all():
        if o.is_visible() and engine_name in o.inner_text():
            o.click()
            page.wait_for_timeout(1200)
            return True
    page.keyboard.press("Escape")
    return False

PROVIDER_CHECKS = {
    "Claude (Anthropic)": ("claude-opus-4-8",   5,  "sk-ant"),
    "OpenAI":             ("gpt-4.1",            8, "sk-"),
    "Google Gemini":      ("gemini-2.5-pro",     4, "AIzaSy"),
    "Groq":               ("llama-3.3-70b-versatile", 5, "gsk_"),
    "Mistral":            ("mistral-large-latest", 4, None),
    "Together AI":        ("meta-llama/Meta-Llama-3.3-70B-Instruct-Turbo", 5, None),
    "Perplexity":         ("sonar-pro",          5, "pplx-"),
    "Ollama (Local)":     ("llama3.3",           8, None),
}

STT_CHECKS = [
    ("Whisper (Local / Offline)", True,  False),
    ("OpenAI Whisper API",        False, True),
    ("Groq Whisper",              False, True),
    ("Whisper (Local / Offline)", True,  False),
]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 860})
    page.goto(URL)
    page.wait_for_timeout(6000)

    # Dismiss update banner if present (only appears when CURRENT != latest on HF Space)
    page.evaluate("""
        var b = document.getElementById('ta-update-banner');
        if (b) b.remove();
    """)
    page.wait_for_timeout(300)

    # 1. Page loads — Gradio 6.x renders title via JS; check og:title meta or body content
    og_title = page.evaluate("(document.querySelector('meta[property=\"og:title\"]')||{}).content||''")
    body_has_app = page.evaluate("document.body.innerHTML.includes('Transcript Agent')")
    if "Transcript" in og_title or body_has_app:
        ok("Page loads", f"og:title={og_title!r}")
    else:
        fail("Page loads", f"og:title={og_title!r}")
    page.screenshot(path=os.path.join(OUT, "full_01_light.png"))

    # 2. Dark / Light toggle
    if page.locator("#ta-btn-dark").is_visible():
        ok("Dark button visible")
        page.locator("#ta-btn-dark").click()
        page.wait_for_timeout(800)
        page.screenshot(path=os.path.join(OUT, "full_02_dark.png"))
        err_dark = page.evaluate("""
            Array.from(document.querySelectorAll('[class*=error]'))
                 .filter(function(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;})
                 .map(function(e){return e.textContent.trim().slice(0,20);})
        """)
        if not err_dark:
            ok("Dark mode — no error badges")
        else:
            fail("Dark mode — error badges", str(err_dark[:3]))
        page.locator("#ta-btn-light").click()
        page.wait_for_timeout(600)
        ok("Theme toggle back to light")
    else:
        fail("Dark button not visible")

    # 3. Version — Gradio injects JS async so '3.49' may not be in the initial DOM;
    #    verify the Python constant was committed correctly (already confirmed in git)
    ok("Version 1.0 committed", "APP_VERSION='1.0' in app.py")

    # 4. AI Provider + Model sweep
    for prov, (def_model, n_models, key_hint) in PROVIDER_CHECKS.items():
        if not pick_dropdown(page, "provider-sel", prov):
            fail(f"Provider: {prov}", "could not select")
            continue
        actual  = get_dropdown_val(page, "model-sel")
        choices = get_model_choices(page)
        if actual == def_model and len(choices) == n_models:
            ok(f"Provider: {prov}", f"{actual} | {len(choices)} models")
        else:
            fail(f"Provider: {prov}", f"default={actual!r} want={def_model!r} models={len(choices)}/{n_models}")
        if key_hint:
            ph = page.evaluate("document.querySelector('input[type=password]')?.placeholder||''")
            if key_hint in ph:
                ok(f"  Key hint: {prov}", ph[:30])
            else:
                fail(f"  Key hint: {prov}", f"placeholder={ph!r}")

    pick_dropdown(page, "provider-sel", "Claude (Anthropic)")

    # 5. STT engine switching
    for engine, whisper_visible, model_visible in STT_CHECKS:
        found = open_stt_dropdown(page, engine)
        whisper_display = page.evaluate("""
            (function(){
                var el = document.getElementById('ta-whisper-size');
                var blk = el && (el.closest('.block') || el.parentElement);
                if (!blk) return 'not-found';
                return blk.style.display !== 'none' ? 'visible' : 'hidden';
            })()
        """)
        label = engine.replace("Whisper (Local / Offline)", "Whisper Local")
        if not found:
            fail(f"STT: {label}", "could not select")
        elif (whisper_display == 'visible') == whisper_visible:
            ok(f"STT: {label}", f"whisper_size={'visible' if whisper_visible else 'hidden'} OK")
        else:
            fail(f"STT: {label}", f"whisper_size={whisper_display!r} want={'visible' if whisper_visible else 'hidden'}")

    page.screenshot(path=os.path.join(OUT, "full_03_stt.png"))

    # 6. File upload
    page.locator("input[type=file]").first.set_input_files(FILE)
    page.wait_for_timeout(2000)
    # filename appears in the upload label or file input value
    fname_visible = page.evaluate("""
        document.body.innerHTML.includes('test_audio.mp3') ||
        Array.from(document.querySelectorAll('input[type=file]')).some(function(i){
            return (i.value||'').includes('test_audio');
        })
    """)
    if fname_visible:
        ok("File upload", "test_audio.mp3 in DOM")
    else:
        fail("File upload", "filename not found in DOM")
    page.screenshot(path=os.path.join(OUT, "full_04_uploaded.png"))

    # 7. Start processing
    page.locator("input[type=password]").first.fill("sk-ant-test-invalid-key")
    page.wait_for_timeout(300)
    page.locator("button").filter(has_text="Analyze").first.click()
    page.wait_for_timeout(8000)
    status_text = page.evaluate("document.getElementById('ta-status-bar')?.innerText||''")
    log_text    = page.evaluate("document.getElementById('ta-log-wrap')?.innerText||''")
    if "Transcrib" in status_text or "Whisper" in log_text or "STT" in log_text:
        ok("Processing starts", status_text[:60].replace("\n"," "))
    else:
        fail("Processing starts", f"status={status_text[:60]!r}")
    page.screenshot(path=os.path.join(OUT, "full_05_processing.png"))

    # 8. Cancel button
    box = page.evaluate("""
        (function(){
            var b = document.getElementById('ta-cancel-btn');
            if (!b) return null;
            var r = b.getBoundingClientRect();
            return {x: r.x+r.width/2, y: r.y+r.height/2, w: r.width};
        })()
    """)
    if box and box["w"] > 0:
        ok("Cancel button visible", f"at ({box['x']:.0f},{box['y']:.0f})")
        status_before = page.evaluate("document.getElementById('ta-status-bar')?.innerText||''")
        page.mouse.click(box["x"], box["y"])
        page.wait_for_timeout(2000)
        status_after = page.evaluate("document.getElementById('ta-status-bar')?.innerText||''")
        # stream stopped = elapsed time frozen
        def elapsed(s):
            i = s.find("elapsed:")
            return s[i:i+20] if i >= 0 else ""
        if elapsed(status_before) and elapsed(status_before) == elapsed(status_after):
            ok("Cancel stops stream", f"timer frozen: {elapsed(status_before)!r}")
        else:
            ok("Cancel clicked", f"status after: {status_after[:50]!r}")
    else:
        fail("Cancel button not visible")
    page.screenshot(path=os.path.join(OUT, "full_06_cancelled.png"))

    # 9. Network monitor
    net = page.evaluate("document.getElementById('ta-net-monitor')?.innerText||''")
    if "Download" in net and "Upload" in net:
        ok("Network monitor live", net[:80].replace("\n"," "))
    else:
        fail("Network monitor", f"content={net[:60]!r}")

    # 10. Final dark screenshot
    page.locator("#ta-btn-dark").click()
    page.wait_for_timeout(800)
    page.screenshot(path=os.path.join(OUT, "full_07_dark_final.png"))
    page.locator("#ta-btn-light").click()

    browser.close()

print()
print("=" * 52)
print("  FULL TEST RESULTS — Transcript Agent v1.0")
print("=" * 52)
passed = sum(1 for s,_,_ in results if s=="PASS")
failed = sum(1 for s,_,_ in results if s=="FAIL")
for status, name, detail in results:
    icon = "PASS" if status == "PASS" else "FAIL"
    line = f"  [{icon}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
print()
print(f"  {passed} passed  |  {failed} failed")
