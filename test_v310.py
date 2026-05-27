"""Test v3.10 — REST API endpoints + UI dropdown model switching."""
import time, sys, json
import urllib.request
from playwright.sync_api import sync_playwright

BASE_API = "http://localhost:8000"
BASE_UI  = "http://localhost:7860"
results  = []

def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    msg = f"[{status}] {label}" + (f" -- {detail}" if detail else "")
    results.append((status, msg))
    print(msg)

def get_json(path):
    with urllib.request.urlopen(f"{BASE_API}{path}", timeout=10) as r:
        return json.loads(r.read())

def select_dropdown(page, elem_id: str, value: str) -> bool:
    """Select a Gradio 6 dropdown item by typing + clicking the matching LI."""
    inp = page.locator(f"#{elem_id} input").first
    inp.click()
    time.sleep(0.3)
    page.keyboard.press("Control+a")
    page.keyboard.press("Backspace")
    inp.type(value, delay=40)
    time.sleep(0.7)
    matches = page.evaluate(f"""
        () => {{
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_TEXT, null
            );
            const out = [];
            let node;
            while (node = walker.nextNode()) {{
                if (node.nodeValue.trim() === '{value}') {{
                    const p = node.parentElement;
                    if (p && p.tagName === 'LI') {{
                        const r = p.getBoundingClientRect();
                        out.push({{ x: r.x + r.width/2, y: r.y + r.height/2 }});
                    }}
                }}
            }}
            return out;
        }}
    """)
    if matches:
        page.mouse.click(matches[0]['x'], matches[0]['y'])
        time.sleep(0.8)
        return True
    return False

# ── REST API ───────────────────────────────────────────────────────────────────
print("\n-- REST API --")

h = get_json("/health")
check("Health OK", h.get("status") == "ok", h.get("version"))

providers = get_json("/api/providers")
check("All 8 AI providers",           set(providers.keys()) == {"claude","openai","gemini","groq","mistral","together","perplexity","ollama"})
check("OpenAI: gpt-4.1",              "gpt-4.1"                    in providers["openai"]["models"])
check("OpenAI: gpt-4.1-mini",         "gpt-4.1-mini"               in providers["openai"]["models"])
check("OpenAI: o3",                   "o3"                          in providers["openai"]["models"])
check("Gemini: gemini-2.5-pro",       "gemini-2.5-pro"              in providers["gemini"]["models"])
check("Gemini: gemini-2.5-flash",     "gemini-2.5-flash"            in providers["gemini"]["models"])
check("Groq: deepseek-r1",            any("deepseek" in m for m in   providers["groq"]["models"]))

stt = get_json("/api/stt-providers")
check("STT: 6 providers",             {"whisper","deepgram","assemblyai","groq_whisper","openai_whisper","google_stt"} == set(stt.keys()))
check("Deepgram key_required",        stt["deepgram"]["key_required"] == True)
check("Deepgram default nova-2",      stt["deepgram"]["default_model"] == "nova-2")

schema   = get_json("/openapi.json")
schemas  = schema.get("components", {}).get("schemas", {})
body_key = next((k for k in schemas if "transcribe_async" in k), None)
check("Transcribe schema exists",     body_key is not None)
if body_key:
    props = set(schemas[body_key].get("properties", {}).keys())
    for param in ["stt_provider","stt_api_key","stt_model","ai_provider","ai_model","ai_api_key"]:
        check(f"Param '{param}' documented", param in props)

with urllib.request.urlopen(f"{BASE_API}/docs", timeout=10) as r:
    check("Swagger /docs loads", r.status == 200)

# ── Gradio UI ─────────────────────────────────────────────────────────────────
print("\n-- Gradio UI --")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(BASE_UI, wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)

    # ── Provider dropdown opens and lists all providers ────────────────────────
    page.locator("#provider-sel").click()
    time.sleep(0.6)
    content = page.content()
    check("Provider list: OpenAI",        "OpenAI"        in content)
    check("Provider list: Google Gemini", "Google Gemini" in content)
    check("Provider list: Groq",          "Groq"          in content)
    page.keyboard.press("Escape")
    time.sleep(0.3)

    # ── Switch to OpenAI — model should update to gpt-4.1 ─────────────────────
    ok = select_dropdown(page, "provider-sel", "OpenAI")
    check("OpenAI provider selected", ok)
    model_val = page.locator("#model-sel input").first.input_value()
    check("Model updated to gpt-4.1 after OpenAI select",
          model_val == "gpt-4.1", f"got: {model_val!r}")
    page.screenshot(path="shot_openai_selected.png")

    # ── Open model dropdown — verify OpenAI models are listed ─────────────────
    page.locator("#model-sel input").first.click()
    time.sleep(0.8)
    model_items = page.evaluate("""
        () => {
            return [...document.querySelectorAll('[role=option]')]
                .map(el => el.textContent.trim().replace(/^[\\s\\u2713]+/, '').trim());
        }
    """)
    check("Model list has gpt-4o",    "gpt-4o"    in model_items, str(model_items[:5]))
    check("Model list has o3",        "o3"         in model_items)
    page.screenshot(path="shot_openai_models.png")
    page.keyboard.press("Escape")
    time.sleep(0.3)

    # ── Switch to Gemini — model should update to gemini-2.5-pro ──────────────
    ok2 = select_dropdown(page, "provider-sel", "Google Gemini")
    check("Google Gemini provider selected", ok2)
    gemini_model = page.locator("#model-sel input").first.input_value()
    check("Model updated to gemini-2.5-pro after Gemini select",
          gemini_model == "gemini-2.5-pro", f"got: {gemini_model!r}")

    # ── Deepgram radio still present ──────────────────────────────────────────
    check("Deepgram radio option present",
          page.locator("text=Deepgram (Cloud)").count() > 0)

    # ── No ghost ovals ────────────────────────────────────────────────────────
    empty = sum(1 for el in page.locator("[data-testid='html']").all()
                if el.is_visible() and el.inner_text().strip() == "")
    check("No ghost HTML blocks", empty == 0, f"{empty} found")

    page.screenshot(path="shot_v310_final.png")
    browser.close()

# ── Summary ───────────────────────────────────────────────────────────────────
print()
print("=" * 54)
passed = sum(1 for s, _ in results if s == "PASS")
failed = sum(1 for s, _ in results if s == "FAIL")
print(f"  {passed} passed  |  {failed} failed")
print("=" * 54)
sys.exit(0 if failed == 0 else 1)
