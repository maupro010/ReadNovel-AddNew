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
DELAY_BETWEEN_CHAPS  = 3.0    # giây nghỉ giữa các chương (tăng từ 0.8 để tránh rate limit)
DELAY_ON_RATELIMIT   = 30.0   # giây nghỉ khi bị rate limit (code=21)
REFRESH_AC_EVERY     = 5      # refresh _ac sau mỗi N chương thành công
MAX_RETRIES          = 3       # số lần retry khi lỗi mạng

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
    """Nạp toàn bộ cookies từ stv_cookies.json vào session."""
    if not Path(COOKIE_FILE).exists():
        raise FileNotFoundError(f"Không tìm thấy {COOKIE_FILE}.")
    with open(COOKIE_FILE, encoding="utf-8") as f:
        cookies = json.load(f)
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
    print(f"  [cookies] _acx={acx[:16] if acx else 'N/A'}  PHPSESSID={php[:12] if php else 'N/A'}  _ac={ac[:16] if ac else 'N/A'}")


def auto_refresh_cookies(session: requests.Session) -> bool:
    """
    Tự động lấy cookies mới từ sangtacviet.app bằng Playwright headless.
    Không cần đăng nhập. Chạy được cả local lẫn GitHub Actions.
    Lưu cookies mới vào COOKIE_FILE và nạp vào session.
    """
    print("  [auto-refresh] Cookies hết hạn. Tự động lấy cookies mới bằng Playwright...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [auto-refresh] Playwright chưa cài. Chạy: pip install playwright && playwright install chromium")
        return False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],  # cần cho GitHub Actions
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/147.0.0.0 Safari/537.36",
                locale="vi-VN",
            )
            page = context.new_page()

            # Bước 1: load trang chủ để lấy PHPSESSID + _acx
            print("  [auto-refresh] Step 1/2: Loading homepage...")
            page.goto(STV_BASE + "/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            # Bước 2: load trang list truyện để _acx được server kích hoạt
            print("  [auto-refresh] Step 2/2: Loading truyen page...")
            page.goto(STV_BASE + "/truyen/qidian/1/1033972532/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            pw_cookies = context.cookies()
            browser.close()

        # Lọc chỉ lấy cookies của sangtacviet.app
        cookie_list = [
            {
                "name":     c["name"],
                "value":    c["value"],
                "domain":   c["domain"].lstrip("."),
                "path":     c.get("path", "/"),
                "expires":  c.get("expires", -1),
                "httpOnly": c.get("httpOnly", False),
                "secure":   c.get("secure", False),
                "sameSite": c.get("sameSite", "Lax"),
            }
            for c in pw_cookies
            if "sangtacviet" in c.get("domain", "")
        ]

        if not cookie_list:
            print("  [auto-refresh] Không lấy được cookies từ Playwright")
            return False

        # Lưu vào file (để lần sau dùng lại)
        Path(COOKIE_FILE).write_text(
            json.dumps(cookie_list, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        # Nạp vào session hiện tại
        session.cookies.clear()
        for c in cookie_list:
            session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])

        acx = session.cookies.get("_acx", domain="sangtacviet.app")
        php = session.cookies.get("PHPSESSID", domain="sangtacviet.app")
        print(f"  [auto-refresh] OK — {len(cookie_list)} cookies | "
              f"_acx={acx[:16] if acx else 'N/A'}  PHPSESSID={php[:12] if php else 'N/A'}")
        return True

    except Exception as e:
        print(f"  [auto-refresh] Lỗi Playwright: {e}")
        return False


def refresh_ac_cookies(session: requests.Session, page_url: str):
    """
    GET trang chương để server set _ac mới qua Set-Cookie header.
    requests.Session tự động lưu cookie này vào jar.
    """
    try:
        session.get(page_url, headers=HEADERS, timeout=30)
        session.cookies.set("foreignlang", "vi",   domain="sangtacviet.app", path="/")
        session.cookies.set("transmode",   "name", domain="sangtacviet.app", path="/")
    except Exception as e:
        print(f"    [refresh_ac] ERROR: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# LẤY DANH SÁCH CHƯƠNG TỪ TRANG STV
# ══════════════════════════════════════════════════════════════════════════════

def renew_session(session: requests.Session):
    """
    Lấy PHPSESSID mới bằng cách GET trang chủ STV.
    Xóa PHPSESSID cũ trước để server cấp session ID mới hoàn toàn.
    """
    print("  [session] Renewing PHPSESSID via homepage...")
    try:
        # Xóa PHPSESSID cũ để server cấp mới
        try:
            session.cookies.clear(domain="sangtacviet.app", path="/", name="PHPSESSID")
        except Exception:
            pass

        r = session.get(STV_BASE + "/", headers=HEADERS, timeout=15)
        new_php = session.cookies.get("PHPSESSID", domain="sangtacviet.app")
        print(f"  [session] New PHPSESSID: {new_php[:10]}..." if new_php else "  [session] No PHPSESSID in response")

        # Lấy _ac/_gac từ trang chủ
        gac_m = re.search(r'document\.cookie\s*=\s*"_gac=([^;]+);', r.text)
        ac_m  = re.search(r'document\.cookie\s*=\s*"_ac=([^;]+);',  r.text)
        if gac_m:
            session.cookies.set("_gac", gac_m.group(1), domain="sangtacviet.app", path="/")
        if ac_m:
            session.cookies.set("_ac",  ac_m.group(1),  domain="sangtacviet.app", path="/")
    except Exception as e:
        print(f"  [session] WARNING renew failed: {e}")


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

    # Bước 1: refresh _ac qua Set-Cookie + cập nhật hstamp
    refresh_ac_cookies(session, chapter_url)
    session.cookies.set("hstamp", str(int(time.time())), domain="sangtacviet.app", path="/")

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
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin":       STV_BASE,
                "Referer":      chapter_url,
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
        # Trả về chuỗi đặc biệt để caller biết đây là rate limit
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

        # ── Fetch danh sách chương từ trang STV ─────────────────────────────
        chapter_list = fetch_chapter_list(session, book_host, book_id)
        if not chapter_list:
            print(f"  [Novel] Không lấy được danh sách chương, bỏ qua.")
            continue

        # ── Scrape từng chương ───────────────────────────────────────────────
        saved = 0
        chap_count = 0  # đếm số chương đã xử lý để refresh _ac định kỳ

        for chap in chapter_list:
            chap_id   = chap["id"]
            chap_name = chap["name"]

            # Bỏ qua chương đã có
            if chap_id in existing_ids:
                continue

            # Refresh _ac mỗi REFRESH_AC_EVERY chương để tránh rate limit
            if chap_count > 0 and chap_count % REFRESH_AC_EVERY == 0:
                chapter_url = f"{STV_BASE}/truyen/{book_host}/1/{book_id}/{chap_id}/"
                print(f"  [refresh] Refresh _ac sau {chap_count} chương...")
                refresh_ac_cookies(session, chapter_url)
                time.sleep(2)

            print(f"  [Chap] {chap_name} (id={chap_id})")

            # Retry loop
            result = None
            cookie_refreshed = False  # chỉ auto-refresh 1 lần per chương
            for attempt in range(1, MAX_RETRIES + 1):
                result = get_chapter_content(session, book_host, book_id, chap_id)
                if result == "RATE_LIMIT":
                    print(f"    [Rate limit] Nghỉ {DELAY_ON_RATELIMIT}s rồi thử lại...")
                    time.sleep(DELAY_ON_RATELIMIT)
                    chapter_url = f"{STV_BASE}/truyen/{book_host}/1/{book_id}/{chap_id}/"
                    refresh_ac_cookies(session, chapter_url)
                    time.sleep(2)
                    result = None
                    continue
                if result is not None:
                    break
                # Lỗi thông thường (code=5/4002) — thử auto-refresh cookies lần đầu
                if not cookie_refreshed:
                    print(f"    [Retry {attempt}/{MAX_RETRIES}] Thử auto-refresh cookies...")
                    if auto_refresh_cookies(session):
                        cookie_refreshed = True
                    else:
                        time.sleep(2)
                else:
                    print(f"    [Retry {attempt}/{MAX_RETRIES}] Thất bại, thử lại...")
                    time.sleep(2)

            if result is None or result == "RATE_LIMIT":
                print(f"    [SKIP] Bỏ qua sau {MAX_RETRIES} lần thử")
                worksheet.append_row([chap_id, chap_name, ""])
                chap_count += 1
                time.sleep(DELAY_BETWEEN_CHAPS)
                continue

            _, content = result

            # Thay \n bằng | để lưu vào Sheets (Sheets mất \n khi đọc về qua API)
            # App Android sẽ convert | thành \n khi đọc
            content_for_sheet = content.replace("\n", "|")

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
