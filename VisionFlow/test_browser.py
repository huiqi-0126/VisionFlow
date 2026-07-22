from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        
        # Listen for console messages
        page.on("console", lambda msg: print(f"Browser Console: {msg.text}"))
        page.on("pageerror", lambda err: print(f"Browser Error: {err}"))
        
        page.goto("http://localhost:8000/planner")
        
        # Click the button
        print("Clicking button...")
        page.locator("text=开始生成30天规划").click()
        
        page.wait_for_timeout(2000)
        browser.close()

run()
