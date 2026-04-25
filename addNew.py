"""
stv_scrape.py - GitHub Action script
Đọc truyện mới nhất từ Google Sheet "list", scrape nội dung từ sangtacviet.app
và lưu vào sheet riêng theo url (host/bookId).

Thay thế vd3.py (metruyencv + Playwright) bằng requests thuần.

Cấu trúc sheet "list":
  Cột 1 (A): url          → "host/bookId"  (ví dụ: "qidian/1039142740")
  Cột 2 (B): title
  Cột 3 (C): author
  Cột 4 (D): desc
  Cột 5 (E): img_url
  Cột 6 (F): max_chapter
  Cột 7 (G): update
  Cột 8 (H): id           → bookId (số nguyên)
  Cột 9 (I): state        → "true" = cần scrape, "false" = đã xong

Cấu trúc sheet riêng của mỗi truyện (tên sheet = url, ví dụ "qidian/1039142740"):
  Cột 1 (A): ID      → chapterId (số)
  Cột 2 (B): NAME    → tên chương
  Cột 3 (C): content → nội dung

Yêu cầu:
  - File credentials.json (Google Service Account)
  - File stv_cookies.json (tạo bằng stv_save_cookies.py)
"""

import re
import os
import json
import time
import requests
from pathlib import Path
from bs4 import BeautifulSoup, NavigableString, Tag

import gspread

# ── Cấu hình ────────────────────────────────────────────────────────────────
COOKIE_FILE          = "stv_cookies.json"
CREDENTIALS_FILE     = "credentials.json"
SPREADSHEET_ID       = "1rCGTw4GdGlR4K-H7hDk8TjjnGh1jL3NgNZLRQ_h8jY8"
STV_BASE             = "https://sangtacviet.app"
CHAR_LIMIT           = 35000   # giới hạn ký tự mỗi ô Google Sheets
DELAY_BETWEEN_CHAPS  = 10.0    # giây nghỉ giữa các chương (tăng để tránh bị block IP)
DELAY_ON_RATELIMIT   = 60.0   # giây nghỉ khi bị rate limit (code=21)
MAX_RETRIES          = 3       # số lần retry khi lỗi

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9,vi;q=0.8,ko;q=0.7",
    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "priority": "u=1, i",
}
# ────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# COOKIES
# ══════════════════════════════════════════════════════════════════════════════

def load_cookies(session: requests.Session):
    """
    Nạp cookies từ stv_cookies.json nếu có (optional).
    Nếu file thiếu/lỗi, bỏ qua — bootstrap_cookies_for_novel() sẽ tự lấy cookies cho từng truyện.
    """
    if not Path(COOKIE_FILE).exists():
        print(f"  [cookies] Không có {COOKIE_FILE} — sẽ tự bootstrap cho từng truyện")
        return

    try:
        with open(COOKIE_FILE, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            print(f"  [cookies] {COOKIE_FILE} rỗng — sẽ tự bootstrap")
            return
        cookies = json.loads(content)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [cookies] {COOKIE_FILE} không đọc được ({e}) — sẽ tự bootstrap")
        return

    for c in cookies:
        session.cookies.set(
            c["name"], c["value"],
            domain=c.get("domain", "sangtacviet.app").lstrip("."),
            path=c.get("path", "/"),
        )
    acx = session.cookies.get("_acx", domain="sangtacviet.app")
    php = session.cookies.get("PHPSESSID", domain="sangtacviet.app")
    ac  = session.cookies.get("_ac",  domain="sangtacviet.app")
    print(f"  [cookies] Loaded {len(cookies)} cookies")
    print(f"  [cookies] _acx={acx if acx else 'N/A'}  PHPSESSID={php[:12] if php else 'N/A'}  _ac={ac[:16] if ac else 'N/A'}")

# ══════════════════════════════════════════════════════════════════════════════
# AUTO BOOTSTRAP COOKIES PER NOVEL
# ══════════════════════════════════════════════════════════════════════════════

def bootstrap_cookies_for_novel(
    session: requests.Session,
    book_host: str,
    book_id: str,
) -> tuple[bool, str]:
    """
    Mỗi truyện trên STV cần một bộ cookies riêng. Hàm này dùng Playwright
    Chrome thật mở trang truyện + chương đầu để server cấp cookies, nạp vào session.

    Trả về (success, update_time) trong đó update_time là chuỗi "X giờ trước"
    extract từ trang mục lục (rỗng nếu không có).
    """
    print(f"  [bootstrap] Lấy cookies cho truyện {book_host}/{book_id}...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [bootstrap] Playwright chưa cài. pip install playwright && playwright install chromium")
        return False, ""

    novel_url = f"{STV_BASE}/truyen/{book_host}/1/{book_id}/"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,     # ← STV detect headless qua navigator.webdriver
                                    #   → JS không render trang chương → _ac không activate
                channel="chrome",   # ← Chrome thật
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="vi-VN",
            )

            # Inject foreignlang cookie ngay trước khi vào → tránh popup chọn ngôn ngữ
            context.add_cookies([
                {"name": "foreignlang", "value": "vi",
                 "domain": "sangtacviet.app", "path": "/"},
                {"name": "transmode",   "value": "name",
                 "domain": "sangtacviet.app", "path": "/"},
            ])

            page = context.new_page()

            # Bước 1: Mở trang chủ trước để server set PHPSESSID
            print(f"  [bootstrap] Loading homepage...")
            page.goto(STV_BASE + "/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)

            # Bước 2: Mở trang mục lục truyện → server set _acx riêng cho bookId
            print(f"  [bootstrap] Loading {novel_url}")
            page.goto(novel_url, wait_until="domcontentloaded", timeout=30000)

            # Chờ AJAX load xong danh sách chương
            try:
                page.wait_for_selector("a.listchapitem", timeout=15000)
                print(f"  [bootstrap] Chapter list loaded")
            except Exception:
                print(f"  [bootstrap] Chapter list timeout")
            page.wait_for_timeout(1500)

            # Lấy text "Chương mới: X giờ trước" / "3 ngày trước" / "hôm qua" /...
            update_time = ""
            try:
                update_time = page.evaluate("""
                    () => {
                        const txt = document.body.innerText || '';
                        const m = txt.match(/Chương mới[:\\s]*([^\\n\\r]+)/i);
                        if (!m) return '';
                        let val = m[1].trim();
                        // Cắt nếu có text "Cập nhật" hoặc tab/nhiều space sau (do innerText nối element)
                        val = val.split(/\\s{2,}|\\t|Cập nhật/i)[0].trim();
                        return val;
                    }
                """) or ""
                if update_time:
                    print(f"  [bootstrap] Update time: {update_time}")
            except Exception as e:
                print(f"  [bootstrap] Get update time error: {e}")

            # Bước 3: Lấy chapterId đầu tiên qua AJAX API
            # (DOM của STV không lưu chapter ID trong attribute thông thường)
            print(f"  [bootstrap] Fetching chapter list via API in browser context...")
            first_chap_id = None
            try:
                api_url = (
                    f"{STV_BASE}/index.php"
                    f"?ngmar=chapterlist&h={book_host}&bookid={book_id}"
                    f"&sajax=getchapterlist&force=true"
                )
                # Trigger
                page.evaluate(f"fetch('{api_url}')")
                page.wait_for_timeout(3000)
                # Poll
                api_url_poll = api_url.replace("&force=true", "")
                resp_text = page.evaluate(f"""
                    async () => {{
                        const r = await fetch('{api_url_poll}');
                        return await r.text();
                    }}
                """)
                if resp_text and resp_text.strip().startswith("{"):
                    obj = json.loads(resp_text)
                    if obj.get("code") == 1 and "data" in obj:
                        entries = obj["data"].split("-//-")
                        if entries:
                            first_entry = entries[0].split("-/-")
                            if len(first_entry) >= 3:
                                first_chap_id = first_entry[1].strip()
                                print(f"  [bootstrap] First chapter ID: {first_chap_id}")
            except Exception as e:
                print(f"  [bootstrap] Get chapter list error: {e}")

            # Bước 4: Mở trang chương đầu trong Playwright
            # → JS trên trang sẽ set _ac, _gac, hstamp vào document.cookie
            if first_chap_id:
                chap_url = f"{STV_BASE}/truyen/{book_host}/1/{book_id}/{first_chap_id}/"
                print(f"  [bootstrap] Loading first chapter in browser: {chap_url}")
                page.goto(chap_url, wait_until="domcontentloaded", timeout=30000)

                # Xử lý popup ngôn ngữ nếu xuất hiện
                try:
                    page.click('.seloption[value="vi"]', timeout=3000)
                    print(f"  [bootstrap] Clicked Tiếng Việt option")
                    page.wait_for_timeout(1000)
                except Exception:
                    pass

                # Click vào nút "Nhấp vào để tải chương..." nếu có
                try:
                    page.click('text=Nhấp vào để tải', timeout=3000)
                    print(f"  [bootstrap] Clicked load chapter button")
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

                # Chờ tới khi có element nội dung chương
                content_loaded = False
                try:
                    page.wait_for_function(
                        "document.querySelectorAll('i[h]').length > 5 || "
                        "document.querySelector('#content-container .contentbox') && "
                        "document.querySelector('#content-container .contentbox').innerText.length > 200",
                        timeout=30000,
                    )
                    content_loaded = True
                    print(f"  [bootstrap] Chapter content rendered")
                except Exception:
                    print(f"  [bootstrap] Chapter content timeout — chờ thêm")
                page.wait_for_timeout(3000)
            else:
                chap_url = novel_url

            # Lấy tất cả cookies từ context (sau khi JS đã set _ac)
            pw_cookies = context.cookies()
            browser.close()

        # Debug: in tất cả cookies Playwright thấy
        print(f"  [bootstrap] Playwright cookies ({len(pw_cookies)}):")
        for c in pw_cookies:
            print(f"    {c['name']:<25} domain={c.get('domain','?'):<25} value={str(c.get('value',''))[:30]}")

        # Xoá cookies cũ của domain sangtacviet.app trong session
        # requests.Session lưu cookies theo từng domain riêng — cần xoá cả 2 biến thể
        for dom in ("sangtacviet.app", ".sangtacviet.app"):
            try:
                session.cookies.clear(domain=dom, path="/")
            except Exception:
                pass

        # Nạp cookies mới — set với domain="sangtacviet.app" (không dấu chấm)
        # để session.cookies.get(name, domain="sangtacviet.app") tìm thấy
        for c in pw_cookies:
            cdom = c.get("domain", "")
            if "sangtacviet" not in cdom:
                continue
            session.cookies.set(
                c["name"], c["value"],
                domain="sangtacviet.app",   # luôn dùng dạng không dấu chấm
                path=c.get("path", "/"),
            )

        # Đảm bảo foreignlang/transmode luôn có
        session.cookies.set("foreignlang", "vi",   domain="sangtacviet.app", path="/")
        session.cookies.set("transmode",   "name", domain="sangtacviet.app", path="/")

        # Cập nhật hstamp (timestamp client) cho lần dùng tiếp theo
        session.cookies.set("hstamp", str(int(time.time())),
                            domain="sangtacviet.app", path="/")

        # Verify
        acx = session.cookies.get("_acx", domain="sangtacviet.app")
        php = session.cookies.get("PHPSESSID", domain="sangtacviet.app")
        ac  = session.cookies.get("_ac",  domain="sangtacviet.app")
        gac = session.cookies.get("_gac", domain="sangtacviet.app")
        print(f"  [bootstrap] Session cookies — _ac={ac[:16] if ac else 'N/A'}  "
              f"_gac={gac[:16] if gac else 'N/A'}  "
              f"_acx={acx[:16] if acx else 'N/A'}  "
              f"PHPSESSID={php[:12] if php else 'N/A'}")

        return bool(ac and acx and php), update_time

    except Exception as e:
        print(f"  [bootstrap] Lỗi Playwright: {e}")
        return False, ""


# ══════════════════════════════════════════════════════════════════════════════
# LẤY DANH SÁCH CHƯƠNG TỪ TRANG STV
# ══════════════════════════════════════════════════════════════════════════════


def fetch_chapter_list(session: requests.Session, book_host: str, book_id: str) -> list[dict]:
    """
    Lấy danh sách chương qua AJAX API của STV.
    STV dùng pattern async: force=true trigger server fetch, sau đó poll.
    """
    referer = f"{STV_BASE}/truyen/{book_host}/1/{book_id}/"
    headers_with_ref = {
        **HEADERS,
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
    }

    base_api = (
        f"{STV_BASE}/index.php"
        f"?ngmar=chapterlist&h={book_host}&bookid={book_id}&sajax=getchapterlist"
    )

    def parse_response(text: str) -> list[dict]:
        """
        Parse response từ API getchapterlist của STV.
        Format JSON: {"code":1,"data":"flag-\/-chapterId-\/- Tên chương -\/\/-flag-\/-..."}
        Sau json.loads(): delimiter chính = "-//-", trong entry dùng "-/-"
        Mỗi entry: "{vip_flag}-/-{chapterId}-/- {chapterName}"
        """
        raw = text.strip()
        if not raw:
            return []

        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and obj.get("code") == 1 and "data" in obj:
                data_str = obj["data"]
                # Tách từng chương theo -//-
                entries = data_str.split("-//-")
                result = []
                for entry in entries:
                    entry = entry.strip()
                    if not entry:
                        continue
                    # Mỗi entry: "flag-/-chapterId-/- Tên chương"
                    parts = entry.split("-/-")
                    if len(parts) >= 3:
                        chap_id   = parts[1].strip()
                        chap_name = parts[2].strip()
                        if chap_id and chap_name:
                            result.append({"id": chap_id, "name": chap_name})
                if result:
                    return result
        except Exception as e:
            pass

        # Fallback: parse HTML nếu không phải JSON format trên
        doc = BeautifulSoup(raw, "html.parser")
        items = doc.select("a.listchapitem")
        if items:
            result = []
            auto_index = 1
            for a in items:
                name = a.get("title", "").strip() or a.text.strip()
                if not name:
                    auto_index += 1
                    continue
                cid = a.get("id", "").strip() or str(auto_index)
                result.append({"id": cid, "name": name})
                auto_index += 1
            return result

        return []

    # Bước 1: Gửi force=true để trigger server fetch
    force_url = base_api + "&force=true"
    print(f"  [detail] Trigger: {force_url}")
    try:
        session.get(force_url, headers=headers_with_ref, timeout=20)
    except Exception as e:
        print(f"  [detail] Trigger error (bỏ qua): {e}")

    # Bước 2: Poll nhiều lần với delay tăng dần
    poll_url = base_api
    wait_times = [3, 5, 8]
    for i, wait in enumerate(wait_times):
        print(f"  [detail] Chờ {wait}s rồi poll lần {i+1}...")
        time.sleep(wait)
        try:
            r = session.get(poll_url, headers=headers_with_ref, timeout=20)
            print(f"  [detail] Poll {i+1}: status={r.status_code} len={len(r.text)}")
            chapters = parse_response(r.text)
            if chapters:
                print(f"  [detail] Found {len(chapters)} chapters")
                return chapters
            if r.text.strip():
                print(f"  [detail] Response không parse được: {r.text[:200]}")
        except Exception as e:
            print(f"  [detail] Poll error: {e}")

    print(f"  [detail] Thất bại sau {len(wait_times)} lần poll")
    return []


# ══════════════════════════════════════════════════════════════════════════════
# LẤY NỘI DUNG MỘT CHƯƠNG
# ══════════════════════════════════════════════════════════════════════════════

def get_chapter_content(
    session: requests.Session,
    book_host: str,
    book_id: str,
    chapter_id: str,
) -> tuple[str, str] | str | None:
    """
    Lấy nội dung chương qua POST API STV.
    Trả về:
      - (chapter_title, content)  : thành công
      - "RATE_LIMIT"              : bị rate limit (code=21), cần nghỉ dài
      - None                      : lỗi khác
    """
    chapter_url = f"{STV_BASE}/truyen/{book_host}/1/{book_id}/{chapter_id}/"

    # Chỉ cập nhật hstamp, không refresh _ac (dùng _ac từ file cookies)
    session.cookies.set("hstamp", str(int(time.time())), domain="sangtacviet.app", path="/")
    ac_val = session.cookies.get("_ac", domain="sangtacviet.app")
    print(f"    [ac] {ac_val[:20] if ac_val else 'N/A'}")

    # Bước 2: POST API
    api_url = (
        f"{STV_BASE}/index.php"
        f"?bookid={book_id}&h={book_host}&c={chapter_id}"
        f"&ngmar=readc&sajax=readchapter&sty=1&exts="
    )
    try:
        resp = session.post(
            api_url,
            data="",
            headers={
                **HEADERS,
                "Content-Type":   "application/x-www-form-urlencoded",
                "Content-Length": "0",
                "Origin":         STV_BASE,
                "Referer":        chapter_url,
            },
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"    [POST] ERROR: {e}")
        return None

    # Bước 3: parse JSON
    raw = resp.text
    if not raw.startswith("{"):
        idx = raw.find('{"')
        if idx < 0:
            print(f"    [POST] Response không phải JSON: {raw[:100]}")
            return None
        raw = raw[idx:]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"    [POST] JSON decode error: {e}")
        return None

    if str(data.get("code")) != "0":
        err  = data.get("err", data.get("msg", "unknown"))
        code = str(data.get("code"))
        print(f"    [POST] API code={code}: {err}")
        if code == "21":
            return "RATE_LIMIT"
        return None

    chapter_title = data.get("chaptername", "").strip()
    raw_html      = data.get("data", "")
    content       = extract_text(raw_html)
    return chapter_title, content


def extract_text(raw_html: str) -> str:
    """Parse HTML nội dung chương STV → plain text."""
    soup = BeautifulSoup(raw_html, "html.parser")
    parts = []

    def walk(node):
        if isinstance(node, NavigableString):
            t = str(node).strip()
            if t:
                parts.append(t)
        elif isinstance(node, Tag):
            if node.name in ("script", "style", "header"):
                return
            if node.name == "br":
                parts.append("\n")
                return
            if node.name == "i" and node.get("h"):
                parts.append(node.get_text(strip=True))
                return
            for child in node.children:
                walk(child)

    walk(soup)

    result = ""
    for p in parts:
        if p == "\n":
            result = result.rstrip(" ") + "\n"
        elif result.endswith("\n") or not result:
            result += p
        elif p in (')', ']', '】', '.', ',', '!', '?', ':', ';'):
            result += p
        else:
            result += " " + p

    return re.sub(r'\n{3,}', '\n\n', result).strip().replace("·", "")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    # ── Kết nối Google Sheets ────────────────────────────────────────────────
    print("[Sheets] Đang kết nối...")
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open_by_key(SPREADSHEET_ID)
    sheet_list = sh.worksheet("list")
    print("[Sheets] Kết nối thành công!")

    # ── Đọc sheet list, lấy truyện cần scrape (state = "true") ──────────────
    # Lấy truyện cuối cùng có state = "true" (giống logic vd3.py cũ)
    all_urls   = sheet_list.col_values(1)[1:]   # bỏ header
    all_states = sheet_list.col_values(9)[1:]   # cột I

    # Lấy tất cả truyện có state = "true" để xử lý
    pending = [
        (i + 2, url)                            # +2 vì bỏ header + 0-index
        for i, (url, state) in enumerate(zip(all_urls, all_states))
        if state.strip().lower() == "true" and url.strip()
    ]

    if not pending:
        print("[Main] Không có truyện nào cần scrape.")
        return

    print(f"[Main] Tìm thấy {len(pending)} truyện cần scrape.")

    # Tìm vị trí cột UPDATE trong header để ghi thời gian "Chương mới: ..."
    header = sheet_list.row_values(1)
    update_col = None
    for i, h in enumerate(header, 1):
        if h.strip().upper() == "UPDATE":
            update_col = i
            break
    if update_col:
        print(f"[Main] Cột UPDATE = {update_col}")
    else:
        print(f"[Main] Không tìm thấy cột UPDATE — sẽ không ghi thời gian cập nhật")

    # ── Khởi tạo session với cookies STV ────────────────────────────────────
    session = requests.Session()
    session.headers.update(HEADERS)

    # Proxy support (đọc từ env vars, giống vd3.py cũ)
    proxy_server = os.environ.get("PROXY_SERVER")
    proxy_user   = os.environ.get("PROXY_USER")
    proxy_pass   = os.environ.get("PROXY_PASS")
    if proxy_server:
        proxy_url = f"http://{proxy_user}:{proxy_pass}@{proxy_server}" if proxy_user else f"http://{proxy_server}"
        session.proxies = {"http": proxy_url, "https": proxy_url}
        print(f"[Proxy] Đang dùng proxy: {proxy_server}")
    else:
        print("[Proxy] Không có proxy, dùng IP trực tiếp")

    load_cookies(session)
    # Không renew session — dùng nguyên PHPSESSID từ file cookies
    # PHPSESSID phải khớp với _acx trên server

    # ── Xử lý từng truyện ───────────────────────────────────────────────────
    for row_index, novel_url in pending:
        novel_url = novel_url.strip()
        parts = novel_url.split("/")
        if len(parts) < 2:
            print(f"[Main] URL không hợp lệ: {novel_url}, bỏ qua.")
            continue

        book_host = parts[0]   # qidian / fanqie / dich ...
        book_id   = parts[1]   # 1039142740

        print(f"\n{'='*60}")
        print(f"[Novel] {novel_url}  (host={book_host}, id={book_id})")

        # ── Lấy/tạo worksheet cho truyện này ────────────────────────────────
        # Tên sheet = url truyện (ví dụ "qidian/1039142740")
        # Dấu "/" không hợp lệ trong tên sheet Google → dùng bookId làm tên
        sheet_name = book_id
        try:
            worksheet = sh.worksheet(sheet_name)
            print(f"  [Sheets] Dùng sheet có sẵn: '{sheet_name}'")
        except gspread.WorksheetNotFound:
            print(f"  [Sheets] Tạo sheet mới: '{sheet_name}'")
            worksheet = sh.add_worksheet(title=sheet_name, rows="1", cols="3")
            worksheet.append_row(["ID", "NAME", "content"])

        # ── Lấy danh sách chương đã có ──────────────────────────────────────
        existing_ids = set()
        existing_col = worksheet.col_values(1)
        for val in existing_col[1:]:   # bỏ header
            val = val.strip()
            if val:
                existing_ids.add(val)
        print(f"  [Sheets] Đã có {len(existing_ids)} chương")

        # ── Auto-bootstrap cookies riêng cho truyện này ─────────────────────
        # Mỗi truyện cần bộ cookies _ac/_acx riêng do server bind theo bookId
        ok, update_time = bootstrap_cookies_for_novel(session, book_host, book_id)
        if not ok:
            print(f"  [Novel] Không lấy được cookies cho {novel_url}, bỏ qua.")
            continue

        # Ghi update_time vào cột UPDATE của sheet list
        if update_time and update_col:
            try:
                sheet_list.update_cell(row_index, update_col, update_time)
                print(f"  [Sheets] Updated UPDATE col: {update_time}")
            except Exception as e:
                print(f"  [Sheets] Lỗi ghi UPDATE: {e}")

        # ── Fetch danh sách chương từ trang STV ─────────────────────────────
        chapter_list = fetch_chapter_list(session, book_host, book_id)
        if not chapter_list:
            print(f"  [Novel] Không lấy được danh sách chương, bỏ qua.")
            continue

        # ── Scrape từng chương ───────────────────────────────────────────────
        saved = 0
        chap_count = 0
        global_cookie_refreshed = False  # chỉ auto-refresh 1 lần per novel

        for chap in chapter_list:
            chap_id   = chap["id"]
            chap_name = chap["name"]

            # Bỏ qua chương đã có (so sánh ID thực tế, không dùng index)
            if chap_id in existing_ids:
                continue

            
            print(f"  [Chap] {chap_name} (id={chap_id})")

            result = None
            for attempt in range(1, MAX_RETRIES + 1):
                result = get_chapter_content(session, book_host, book_id, chap_id)

                if result == "RATE_LIMIT":
                    print(f"    [Rate limit] Nghỉ {DELAY_ON_RATELIMIT}s rồi bootstrap cookies...")
                    time.sleep(DELAY_ON_RATELIMIT)
                    bootstrap_cookies_for_novel(session, book_host, book_id)  # discard tuple
                    result = None
                    continue

                if result == "IP_BLOCKED":
                    print(f"  [IP BLOCKED] IP bị STV chặn (lỗi 4003). Dừng scrape.")
                    return

                if result is not None:
                    break  # thành công

                # Lỗi 4002 hoặc lỗi khác → bootstrap lại cookies cho truyện này
                print(f"    [Retry {attempt}/{MAX_RETRIES}] Bootstrap lại cookies...")
                bootstrap_cookies_for_novel(session, book_host, book_id)
                time.sleep(2)

            if result is None or result == "RATE_LIMIT":
                print(f"    [SKIP] Bỏ qua sau {MAX_RETRIES} lần thử")
                break

            _, content = result

            # Thay \n bằng | để lưu vào Sheets (Sheets mất \n khi đọc về qua API)
            # App Android sẽ convert | thành \n khi đọc
            content_for_sheet = content

            # Chia nhỏ nếu content quá dài
            if len(content_for_sheet) > CHAR_LIMIT:
                rows = []
                for k in range(0, len(content_for_sheet), CHAR_LIMIT):
                    chunk = content_for_sheet[k: k + CHAR_LIMIT]
                    rows.append([chap_id, chap_name, chunk])
                worksheet.append_rows(rows, value_input_option='RAW')
            else:
                worksheet.append_row([chap_id, chap_name, content_for_sheet], value_input_option='RAW')

            saved += 1
            chap_count += 1
            print(f"    [OK] Đã lưu ({len(content)} ký tự)")
            time.sleep(DELAY_BETWEEN_CHAPS)

        print(f"  [Novel] Hoàn tất: lưu {saved} chương mới")

        # ── Đánh dấu đã xong trong sheet list ───────────────────────────────
        sheet_list.update_cell(row_index, 9, "false")
        print(f"  [Sheets] Đánh dấu state = false cho row {row_index}")

    print("\n[Main] Hoàn tất tất cả truyện.")


if __name__ == "__main__":
    main()
