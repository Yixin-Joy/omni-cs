import hashlib
import json
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import time
from bs4 import BeautifulSoup

URL = "https://developers.facebook.com/docs/graph-api/changelog/out-of-cycle-changes"
STATE_FILE = "meta_occ_snapshot.json"

def load_last_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"hash": None, "last_check": None}

def save_state(new_hash: str):
    data = {
        "hash": new_hash,
        "last_check": datetime.now().isoformat()
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def fetch_with_selenium():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])

    driver = webdriver.Chrome(options=chrome_options)
    driver.set_page_load_timeout(30)
    driver.get(URL)
    time.sleep(4)
    html = driver.page_source
    driver.quit()
    return html

def get_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def extract_latest_occ(html):
    soup = BeautifulSoup(html, "html.parser")
    all_a = soup.find_all("a")
    items = []
    for a in all_a:
        href = a.get("href")
        text = a.get_text(strip=True)
        if href and "occ-" in href:
            full_link = "https://developers.facebook.com" + href
            items.append({"title": text, "link": full_link})
    if len(items) > 0:
        return items[0]
    return None

def main():
    print("===== Meta Out-of-Cycle 全局變更監測 Demo（Selenium版） =====")
    last_state = load_last_state()
    html = fetch_with_selenium()
    page_text = BeautifulSoup(html,"html.parser").get_text(strip=True)
    current_hash = get_sha256(page_text)

    print(f"\n本次頁面Hash: {current_hash}")
    print(f"上次快照Hash: {last_state['hash']}")

    latest_item = extract_latest_occ(html)

    if last_state["hash"] is None:
        print("\n👉 首次執行，建立初始快照，不觸發告警。")
        save_state(current_hash)
        if latest_item:
            print(f"\n【頁面上記錄的最新全局變更】")
            print(f"變更標題/日期：{latest_item['title']}")
            print(f"詳細文檔鏈接：{latest_item['link']}")
        else:
            print("\n⚠️ 渲染後依舊沒有找到 occ- 連結，DOM結構可能有變")
        return

    if current_hash == last_state["hash"]:
        print("\n✅ 頁面無更新，沒有新增全局變更。")
        if latest_item:
            print(f"\n【頁面上記錄的最後一次全局變更】")
            print(f"變更標題/日期：{latest_item['title']}")
            print(f"詳細文檔鏈接：{latest_item['link']}")
        return
    else:
        print("\n🔥 檢測到out-of-cycle頁面**新增變更**！")
        if latest_item:
            print(f"\n本次新增全局變更")
            print(f"變更標題/日期：{latest_item['title']}")
            print(f"詳細文檔鏈接：{latest_item['link']}")
        save_state(current_hash)

if __name__ == "__main__":
    main()
