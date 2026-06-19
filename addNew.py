"""
stv_scrape.py — Đọc truyện từ sheet "list" trên Google Sheets, scrape nội dung
từ sangtacviet.app và lưu vào sheet riêng theo bookId.

Sheet "list" — các cột tham chiếu:
  A: url ("host/bookId")    I: state ("true" = cần scrape)
  Cột UPDATE (theo header): ghi "X giờ trước"

Sheet riêng từng truyện (tên = bookId):
  A: chapterId   B: tên chương   C: nội dung

Yêu cầu:
  - credentials.json (Google Service Account)
  - Chrome thật cài sẵn trên máy (Playwright dùng channel="chrome")
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
COOKIE_FILE         = "stv_cookies.json"
CREDENTIALS_FILE    = "credentials.json"
SPREADSHEET_ID      = "1rCGTw4GdGlR4K-H7hDk8TjjnGh1jL3NgNZLRQ_h8jY8"
STV_BASE            = "https://sangtacviet.app"
CHAR_LIMIT          = 35000
DELAY_BETWEEN_CHAPS = 10.0
DELAY_ON_RATELIMIT  = 60.0
MAX_RETRIES         = 3

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


# ══════════════════════════════════════════════════════════════════════════════
# COOKIES
# ══════════════════════════════════════════════════════════════════════════════

def load_cookies(session: requests.Session):
    """Nạp cookies từ stv_cookies.json. Trả về True nếu có cookies đăng nhập."""
    if not Path(COOKIE_FILE).exists():
        print(f"  [cookies] Không có {COOKIE_FILE} — sẽ tự bootstrap cho từng truyện")
        return False

    try:
        with open(COOKIE_FILE, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            print(f"  [cookies] {COOKIE_FILE} rỗng — sẽ tự bootstrap")
            return False
        cookies = json.loads(content)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [cookies] {COOKIE_FILE} không đọc được ({e}) — sẽ tự bootstrap")
        return False

    for c in cookies:
        session.cookies.set(
            c["name"], c["value"],
            domain=c.get("domain", "sangtacviet.app").lstrip("."),
            path=c.get("path", "/"),
        )
    print(f"  [cookies] Loaded {len(cookies)} cookies")

    # Có cookie đăng nhập STV (access/useri2) hoặc cf_clearance → cookies "xịn"
    cookie_names = {c["name"].lower() for c in cookies}
    has_login = bool(cookie_names & {
        "cf_clearance", "access", "useri2", "member", "memberid", "userid", "uid"
    })
    if has_login:
        print(f"  [cookies] Phát hiện cookies đăng nhập — ưu tiên dùng, bỏ qua bootstrap")
    return has_login


def bootstrap_cookies_for_novel(
    session: requests.Session,
    book_host: str,
    book_id: str,
) -> tuple[bool, str]:
    """
    Mở Chrome thật qua Playwright để server cấp cookies riêng cho truyện này.
    Trả về (success, update_time).
    """
    print(f"  [bootstrap] Lấy cookies cho {book_host}/{book_id}...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [bootstrap] Playwright chưa cài: pip install playwright && playwright install chrome")
        return False, ""

    novel_url = f"{STV_BASE}/truyen/{book_host}/1/{book_id}/"

    try:
        with sync_playwright() as p:
            # headless=False vì STV detect headless qua navigator.webdriver
            # channel="chrome" vì Chromium bundled cũng bị detect
            browser = p.chromium.launch(
                headless=False,
                channel="chrome",
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="vi-VN",
            )
            # Inject cookies ngôn ngữ trước để tránh popup chọn ngôn ngữ
            context.add_cookies([
                {"name": "foreignlang", "value": "vi",
                 "domain": "sangtacviet.app", "path": "/"},
                {"name": "transmode",   "value": "name",
                 "domain": "sangtacviet.app", "path": "/"},
            ])
            page = context.new_page()

            # Bước 1: Homepage → server cấp PHPSESSID
            page.goto(STV_BASE + "/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)

            # Bước 2: Trang truyện → server cấp _acx riêng cho bookId
            print(f"  [bootstrap] Loading {novel_url}")
            page.goto(novel_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_selector("a.listchapitem", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(1500)

            # Extract "Chương mới: X giờ trước" / "3 ngày trước" / "hôm qua"...
            update_time = ""
            try:
                update_time = page.evaluate("""
                    () => {
                        const txt = document.body.innerText || '';
                        const m = txt.match(/Chương mới[:\\s]*([^\\n\\r]+)/i);
                        if (!m) return '';
                        let val = m[1].trim();
                        val = val.split(/\\s{2,}|\\t|Cập nhật/i)[0].trim();
                        return val;
                    }
                """) or ""
                if update_time:
                    print(f"  [bootstrap] Update time: {update_time}")
            except Exception as e:
                print(f"  [bootstrap] Get update time error: {e}")

            # Bước 3: Lấy chapter ID đầu qua AJAX (DOM không có ID)
            first_chap_id = None
            try:
                api_url = (
                    f"{STV_BASE}/index.php"
                    f"?ngmar=chapterlist&h={book_host}&bookid={book_id}"
                    f"&sajax=getchapterlist&force=true"
                )
                page.evaluate(f"fetch('{api_url}')")
                page.wait_for_timeout(3000)
                resp_text = page.evaluate(f"""
                    async () => {{
                        const r = await fetch('{api_url.replace("&force=true", "")}');
                        return await r.text();
                    }}
                """)
                if resp_text and resp_text.strip().startswith("{"):
                    obj = json.loads(resp_text)
                    if obj.get("code") == 1 and "data" in obj:
                        entries = obj["data"].split("-//-")
                        if entries:
                            parts = entries[0].split("-/-")
                            if len(parts) >= 3:
                                first_chap_id = parts[1].strip()
            except Exception as e:
                print(f"  [bootstrap] Get chapter list error: {e}")

            # Bước 4: Mở trang chương đầu → JS set _ac, _gac vào document.cookie
            if first_chap_id:
                chap_url = f"{STV_BASE}/truyen/{book_host}/1/{book_id}/{first_chap_id}/"
                page.goto(chap_url, wait_until="domcontentloaded", timeout=30000)
                # Click qua popup ngôn ngữ và nút tải chương nếu có
                for selector in ('.seloption[value="vi"]', 'text=Nhấp vào để tải'):
                    try:
                        page.click(selector, timeout=3000)
                        page.wait_for_timeout(1000)
                    except Exception:
                        pass
                # Chờ nội dung render (i[h] cho qidian/fanqie, text plain cho dich/sangtac)
                try:
                    page.wait_for_function(
                        """
                        () => {
                            const el = document.querySelector('#maincontent')
                                    || document.querySelector('[id^="cld-"]');
                            if (!el) return false;
                            const txt = el.innerText || '';
                            return txt.length > 200
                                && !txt.includes('Nhấp vào để tải')
                                && !txt.includes('Vui lòng xác nhận');
                        }
                        """,
                        timeout=30000,
                    )
                except Exception:
                    pass
                page.wait_for_timeout(3000)

            pw_cookies = context.cookies()
            browser.close()

        # Xoá cookies STV cũ trong session, nạp bộ mới
        for dom in ("sangtacviet.app", ".sangtacviet.app"):
            try:
                session.cookies.clear(domain=dom, path="/")
            except Exception:
                pass
        for c in pw_cookies:
            if "sangtacviet" not in c.get("domain", ""):
                continue
            session.cookies.set(
                c["name"], c["value"],
                domain="sangtacviet.app",
                path=c.get("path", "/"),
            )
        session.cookies.set("foreignlang", "vi",   domain="sangtacviet.app", path="/")
        session.cookies.set("transmode",   "name", domain="sangtacviet.app", path="/")
        session.cookies.set("hstamp", str(int(time.time())),
                            domain="sangtacviet.app", path="/")

        ac  = session.cookies.get("_ac",       domain="sangtacviet.app")
        acx = session.cookies.get("_acx",      domain="sangtacviet.app")
        php = session.cookies.get("PHPSESSID", domain="sangtacviet.app")
        print(f"  [bootstrap] _ac={ac[:16] if ac else 'N/A'}  "
              f"_acx={acx[:16] if acx else 'N/A'}  "
              f"PHPSESSID={php[:12] if php else 'N/A'}")

        return bool(ac and acx and php), update_time

    except Exception as e:
        print(f"  [bootstrap] Lỗi Playwright: {e}")
        return False, ""


# ══════════════════════════════════════════════════════════════════════════════
# DANH SÁCH CHƯƠNG
# ══════════════════════════════════════════════════════════════════════════════

def format_update_time(dt_str: str) -> str:
    """
    Chuyển chuỗi ngày giờ VN (vd '2026-03-08 08:48:20') thành 'X tuần trước'.
    Giống logic web STV. dt_str là giờ địa phương VN (UTC+7).
    """
    from datetime import datetime, timezone, timedelta
    try:
        dt = datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M:%S")
        # dt là giờ VN → gán tzinfo +7
        dt = dt.replace(tzinfo=timezone(timedelta(hours=7)))
        now = datetime.now(timezone(timedelta(hours=7)))
        diff = int((now - dt).total_seconds())
    except Exception:
        return ""
    if diff < 0:
        diff = 0
    week = diff // 604800
    day  = diff // 86400
    hour = diff // 3600
    minute = diff // 60
    if week > 0:
        return f"{week} tuần trước"
    if day > 0:
        return f"{day} ngày trước"
    if hour > 0:
        return f"{hour} giờ trước"
    if minute > 0:
        return f"{minute} phút trước"
    return "vừa xong"


def get_update_time(session: requests.Session, book_host: str, book_id: str) -> str:
    """
    Lấy update time từ HTML trang truyện (element id='lastupdatetime').
    Chứa chuỗi ngày giờ VN, vd '2026-03-08 08:48:20'.
    """
    url = f"{STV_BASE}/truyen/{book_host}/1/{book_id}/"
    try:
        r = session.get(url, headers=HEADERS, timeout=20)
        m = re.search(
            r"id=['\"]lastupdatetime['\"][^>]*>([^<]+)<",
            r.text,
        )
        if m:
            return format_update_time(m.group(1))
    except Exception:
        pass
    return ""


def fetch_chapter_list(session: requests.Session, book_host: str, book_id: str) -> list[dict]:
    """
    Lấy danh sách chương qua AJAX API.
    Pattern: gửi force=true để trigger server fetch, sau đó poll lấy kết quả.
    """
    referer = f"{STV_BASE}/truyen/{book_host}/1/{book_id}/"
    headers = {**HEADERS, "Referer": referer, "X-Requested-With": "XMLHttpRequest"}
    base_api = (
        f"{STV_BASE}/index.php"
        f"?ngmar=chapterlist&h={book_host}&bookid={book_id}&sajax=getchapterlist"
    )

    def parse(text: str) -> list[dict]:
        """
        Format: {"code":1,"data":"flag-/-chapterId-/- name -//-flag-/-..."}
        """
        if not text.strip():
            return []
        try:
            obj = json.loads(text)
            if obj.get("code") == 1 and "data" in obj:
                result = []
                for entry in obj["data"].split("-//-"):
                    parts = entry.strip().split("-/-")
                    if len(parts) >= 3:
                        chap_id, chap_name = parts[1].strip(), parts[2].strip()
                        if chap_id and chap_name:
                            result.append({"id": chap_id, "name": chap_name})
                if result:
                    return result
        except Exception:
            pass

        # Fallback: parse HTML
        doc = BeautifulSoup(text, "html.parser")
        items = doc.select("a.listchapitem")
        result = []
        for i, a in enumerate(items, 1):
            name = a.get("title", "").strip() or a.text.strip()
            if name:
                result.append({"id": a.get("id", "").strip() or str(i), "name": name})
        return result

    # Trigger
    try:
        session.get(base_api + "&force=true", headers=headers, timeout=20)
    except Exception as e:
        print(f"  [detail] Trigger error: {e}")

    # Poll với delay tăng dần
    for i, wait in enumerate([3, 5, 8], 1):
        time.sleep(wait)
        try:
            r = session.get(base_api, headers=headers, timeout=20)
            chapters = parse(r.text)
            if chapters:
                print(f"  [detail] Found {len(chapters)} chapters")
                return chapters
        except Exception as e:
            print(f"  [detail] Poll {i} error: {e}")

    print(f"  [detail] Thất bại sau 3 lần poll")
    return []


# ══════════════════════════════════════════════════════════════════════════════
# NỘI DUNG MỘT CHƯƠNG
# ══════════════════════════════════════════════════════════════════════════════

def get_chapter_content(
    session: requests.Session,
    book_host: str,
    book_id: str,
    chapter_id: str,
) -> tuple[str, str] | str | None:
    """Trả về (title, content) | "RATE_LIMIT" | None."""
    chapter_url = f"{STV_BASE}/truyen/{book_host}/1/{book_id}/{chapter_id}/"
    session.cookies.set("hstamp", str(int(time.time())), domain="sangtacviet.app", path="/")

    # Tham số `exts` cần thiết cho host=sangtac/dich (server check để trả text full)
    # Format: clientWidth^fontColor^bgColor (mô phỏng browser desktop default)
    if book_host in ("sangtac", "dich"):
        exts = "1140^-16777216^-1383213"
    else:
        exts = ""

    api_url = (
        f"{STV_BASE}/index.php"
        f"?bookid={book_id}&h={book_host}&c={chapter_id}"
        f"&ngmar=readc&sajax=readchapter&sty=1&exts={exts}"
    )
    # Body POST: host sangtac/dich cần "rescan=true&k=" để force server dịch text
    # (giống nút "Nội dung không đầy đủ?, nhấp để hệ thống tải lại" trên web)
    post_body = "rescan=true&k=" if book_host in ("sangtac", "dich") else ""
    try:
        resp = session.post(
            api_url,
            data=post_body,
            headers={
                **HEADERS,
                "Content-Type":   "application/x-www-form-urlencoded",
                "Content-Length": str(len(post_body)),
                "Origin":         STV_BASE,
                "Referer":        chapter_url,
            },
            timeout=60,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"    [POST] ERROR: {e}")
        return None

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
        if 'Vui lòng đăng nhập để đọc chương vip Khởi điểm.' in err:
            return "VIP"
        return None

    raw_html      = data.get("data", "")
    chapter_title = data.get("chaptername", "").strip()

    # Host sangtac/dich trả plain text với phụ âm bị thay bằng codepoint PUA
    # (server STV obfuscation, font fenc render đúng glyph trên browser)
    if book_host in ("sangtac", "dich") and "<i" not in raw_html:
        # Convert HTML → plain text
        text = re.sub(r'<br\s*/?>', '\n', raw_html, flags=re.IGNORECASE)
        text = BeautifulSoup(text, "html.parser").get_text()
        # Decode PUA → ký tự Việt thật (cần font fenc của truyện này)
        decoder = get_pua_decoder(book_host, book_id)
        if decoder:
            text = decoder.decode(text)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        return chapter_title, text

    # Các host khác mà thiếu <i> → fallback Playwright (chương intro/giới thiệu)
    if "<i" not in raw_html:
        print(f"    [extract] HTML không có <i> tag → fallback Playwright")
        content = extract_text_via_playwright(book_host, book_id, chapter_id)
        if content:
            return chapter_title, content

    return chapter_title, extract_text(raw_html)


def extract_text_via_playwright(book_host: str, book_id: str, chapter_id: str) -> str:
    """
    Fallback: dùng Chrome thật mở trang chương, lấy innerText sau khi JS render xong.
    Dùng cho các chương đặc biệt mà API trả HTML đã bị strip ký tự.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""

    chap_url = f"{STV_BASE}/truyen/{book_host}/1/{book_id}/{chapter_id}/"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False, channel="chrome",
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(user_agent=HEADERS["User-Agent"], locale="vi-VN")
            context.add_cookies([
                {"name": "foreignlang", "value": "vi", "domain": "sangtacviet.app", "path": "/"},
                {"name": "transmode",   "value": "name", "domain": "sangtacviet.app", "path": "/"},
            ])
            page = context.new_page()
            page.goto(chap_url, wait_until="domcontentloaded", timeout=30000)
            for sel in ('.seloption[value="vi"]', 'text=Nhấp vào để tải'):
                try:
                    page.click(sel, timeout=3000)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
            page.wait_for_timeout(5000)  # chờ JS render font glyphs

            content = page.evaluate("""
                () => {
                    const el = document.querySelector('#content-container .contentbox')
                            || document.querySelector('#content-container');
                    return el ? el.innerText.trim() : '';
                }
            """) or ""
            browser.close()
            return content
    except Exception as e:
        print(f"    [extract-pw] error: {e}")
        return ""


# ──── PUA Decoder cho host=sangtac/dich ──────────────────────────────────
# Server thay phụ âm Việt bằng codepoint PUA (U+E000-U+F8FF). Font fenc tải từ
# /ctp/fenc/<hash>.ttf có glyph tại PUA codepoint với outline giống ký tự thật.
# → Build mapping bằng cách match outline; decode text vỡ → tiếng Việt đầy đủ.

class PUADecoder:
    def __init__(self, font_bytes: bytes):
        from fontTools.ttLib import TTFont
        from fontTools.pens.recordingPen import RecordingPen
        from io import BytesIO

        ft = TTFont(BytesIO(font_bytes))
        cmap = ft.getBestCmap()
        glyph_set = ft.getGlyphSet()

        def outline_key(name):
            if name not in glyph_set:
                return None
            pen = RecordingPen()
            glyph_set[name].draw(pen)
            return str(pen.value) if pen.value else None

        # Build dict outline → ký tự thật (ASCII + Latin Extended + Vietnamese)
        real_outlines = {}
        for cp in (list(range(0x20, 0x7F))
                 + list(range(0x00C0, 0x0180))
                 + list(range(0x1EA0, 0x1EFA))):
            ch = chr(cp)
            name = cmap.get(cp)
            if name:
                key = outline_key(name)
                if key and key not in real_outlines:
                    real_outlines[key] = ch

        # Build PUA → real char mapping
        self.mapping = {}
        for cp, name in cmap.items():
            if 0xE000 <= cp <= 0xF8FF:
                key = outline_key(name)
                if key in real_outlines:
                    self.mapping[chr(cp)] = real_outlines[key]

    def decode(self, text: str) -> str:
        return ''.join(self.mapping.get(ch, ch) for ch in text)


_pua_decoder_cache = {}

def get_pua_decoder(book_host: str, book_id: str):
    """Tải font fenc từ STV qua Playwright, build decoder, cache."""
    cache_key = f"{book_host}/{book_id}"
    if cache_key in _pua_decoder_cache:
        return _pua_decoder_cache[cache_key]

    print(f"    [pua] Tải font fenc cho {cache_key}...")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False, channel="chrome",
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(user_agent=HEADERS["User-Agent"], locale="vi-VN")
            context.add_cookies([
                {"name": "foreignlang", "value": "vi", "domain": "sangtacviet.app", "path": "/"},
                {"name": "transmode",   "value": "name", "domain": "sangtacviet.app", "path": "/"},
            ])
            page = context.new_page()
            page.goto(f"{STV_BASE}/truyen/{book_host}/1/{book_id}/",
                      wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            # Lấy URL font fenc cho 'nunito' từ cssoutput.css
            css_text = page.evaluate("""
                async () => {
                    const r = await fetch('/ctp/cssoutput.css?time=' + Date.now());
                    return await r.text();
                }
            """)
            m = re.search(r"font-family:\s*'nunito'\s*;\s*src:\s*url\('([^']+)'\)", css_text)
            if not m:
                m = re.search(r"src:\s*url\('(/ctp/fenc/[^']+\.ttf)'\)", css_text)
            if not m:
                print(f"    [pua] Không tìm thấy URL font fenc")
                browser.close()
                return None

            font_url = m.group(1)
            if not font_url.startswith("http"):
                font_url = STV_BASE + font_url

            # Fetch binary qua browser context (bypass anti-bot)
            font_b64 = page.evaluate(f"""
                async () => {{
                    const r = await fetch('{font_url}');
                    const buf = await r.arrayBuffer();
                    const arr = new Uint8Array(buf);
                    let bin = '';
                    for (let i = 0; i < arr.length; i++) bin += String.fromCharCode(arr[i]);
                    return btoa(bin);
                }}
            """)
            browser.close()

        import base64
        font_bytes = base64.b64decode(font_b64)
        print(f"    [pua] Font: {len(font_bytes)} bytes, build decoder...")
        decoder = PUADecoder(font_bytes)
        print(f"    [pua] Decoder ready: {len(decoder.mapping)} mappings")
        _pua_decoder_cache[cache_key] = decoder
        return decoder
    except Exception as e:
        print(f"    [pua] Lỗi: {e}")
        return None


def solve_captcha_manually(session, book_host: str, book_id: str, chap_id: str) -> bool:
    """
    Mở Chrome với profile thật để user click captcha. Dùng persistent context
    (profile riêng cho scraper) giúp Cloudflare tin tưởng hơn so với context tạm.
    """
    chap_url = f"{STV_BASE}/truyen/{book_host}/1/{book_id}/{chap_id}/"

    # Trên CI không có người click captcha → skip ngay
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        print(f"    [Captcha] Môi trường CI, không thể giải captcha thủ công — skip")
        return False

    # Profile Chrome riêng cho scraper (giữ cookies Cloudflare qua các lần chạy)
    profile_dir = str(Path.home() / ".stv_chrome_profile")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # persistent context = dùng profile thật, KHÔNG phải context tạm
            # → Cloudflare tin tưởng hơn, ít bị "Xác minh thất bại"
            context = p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False,
                channel="chrome",
                user_agent=HEADERS["User-Agent"],
                locale="vi-VN",
                args=["--disable-blink-features=AutomationControlled"],
                no_viewport=True,
            )
            # Nạp cookies session hiện tại
            for c in session.cookies:
                if "sangtacviet" in c.domain:
                    try:
                        context.add_cookies([{
                            "name": c.name, "value": c.value,
                            "domain": "sangtacviet.app", "path": "/",
                        }])
                    except Exception:
                        pass

            page = context.pages[0] if context.pages else context.new_page()
            page.goto(chap_url, wait_until="domcontentloaded", timeout=60000)

            for sel in ('.seloption[value="vi"]', 'text=Nhấp vào để tải'):
                try:
                    page.click(sel, timeout=3000)
                    page.wait_for_timeout(1500)
                except Exception:
                    pass

            print(f"    [Captcha] >>> Click 'I'm not a robot' trên cửa sổ Chrome vừa mở <<<")
            print(f"    [Captcha] Nếu vẫn 'Xác minh thất bại', thử bấm nút reload (mũi tên tròn) rồi click lại")
            print(f"    [Captcha] Đang chờ tối đa 180s...")

            success = False
            for i in range(180):
                time.sleep(1)
                loaded = page.evaluate("""
                    () => {
                        const el = document.querySelector('#maincontent') 
                                || document.querySelector('[id^="cld-"]');
                        if (!el) return false;
                        const txt = el.innerText || '';
                        return txt.length > 200
                            && !txt.includes('Nhấp vào để tải')
                            && !txt.includes('Vui lòng xác nhận')
                            && !txt.includes('Tải quá thời gian');
                    }
                """)
                if loaded:
                    print(f"    [Captcha] Đã pass + chương load sau {i+1}s")
                    success = True
                    break

            pw_cookies = context.cookies()
            context.close()

        if success:
            for c in pw_cookies:
                if "sangtacviet" in c.get("domain", ""):
                    session.cookies.set(
                        c["name"], c["value"],
                        domain="sangtacviet.app", path=c.get("path", "/"),
                    )
            session.cookies.set("hstamp", str(int(time.time())),
                                domain="sangtacviet.app", path="/")
        return success
    except Exception as e:
        print(f"    [Captcha] Lỗi: {e}")
        return False


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
                # Inner text là dạng được render hiển thị trên web
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
    print("[Sheets] Đang kết nối...")
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open_by_key(SPREADSHEET_ID)
    sheet_list = sh.worksheet("list")
    print("[Sheets] Kết nối thành công!")

    # Đọc danh sách truyện cần scrape (state = "true")
    all_urls   = sheet_list.col_values(1)[1:]
    all_states = sheet_list.col_values(9)[1:]
    pending = [
        (i + 2, url)
        for i, (url, state) in enumerate(zip(all_urls, all_states))
        if state.strip().lower() == "true" and url.strip()
    ]
    if not pending:
        print("[Main] Không có truyện nào cần scrape.")
        return
    print(f"[Main] Tìm thấy {len(pending)} truyện cần scrape.")

    # Helper: lookup lại row hiện tại (tránh ghi sai khi user thêm/xoá row)
    def find_current_row(novel_url: str) -> int | None:
        try:
            for i, u in enumerate(sheet_list.col_values(1), 1):
                if u.strip() == novel_url.strip():
                    return i
        except Exception as e:
            print(f"  [Sheets] find_current_row error: {e}")
        return None

    # Tìm cột UPDATE để ghi "Chương mới: ..."
    header = sheet_list.row_values(1)
    update_col = next(
        (i for i, h in enumerate(header, 1) if h.strip().upper() == "UPDATE"),
        None
    )
    if update_col:
        print(f"[Main] Cột UPDATE = {update_col}")
    else:
        print(f"[Main] Không tìm thấy cột UPDATE — sẽ không ghi thời gian cập nhật")

    # Khởi tạo session
    session = requests.Session()
    session.headers.update(HEADERS)

    proxy_server = os.environ.get("PROXY_SERVER")
    proxy_user   = os.environ.get("PROXY_USER")
    proxy_pass   = os.environ.get("PROXY_PASS")
    if proxy_server:
        proxy_url = (
            f"http://{proxy_user}:{proxy_pass}@{proxy_server}"
            if proxy_user else f"http://{proxy_server}"
        )
        session.proxies = {"http": proxy_url, "https": proxy_url}
        print(f"[Proxy] Đang dùng proxy: {proxy_server}")
    else:
        print("[Proxy] Không có proxy, dùng IP trực tiếp")

    has_login_cookies = load_cookies(session)

    # Xử lý từng truyện
    for row_index, novel_url in pending:
        novel_url = novel_url.strip()
        parts = novel_url.split("/")
        if len(parts) < 2:
            print(f"[Main] URL không hợp lệ: {novel_url}, bỏ qua.")
            continue
        book_host, book_id = parts[0], parts[1]

        print(f"\n{'='*60}")
        print(f"[Novel] {novel_url}  (host={book_host}, id={book_id})")

        # Worksheet riêng cho truyện này (tên = bookId)
        try:
            worksheet = sh.worksheet(book_id)
            print(f"  [Sheets] Dùng sheet có sẵn: '{book_id}'")
        except gspread.WorksheetNotFound:
            print(f"  [Sheets] Tạo sheet mới: '{book_id}'")
            worksheet = sh.add_worksheet(title=book_id, rows="1", cols="3")
            worksheet.append_row(["ID", "NAME", "content"])

        # Danh sách chương đã có
        existing_ids = {v.strip() for v in worksheet.col_values(1)[1:] if v.strip()}
        print(f"  [Sheets] Đã có {len(existing_ids)} chương")

        # Nếu có cookies đăng nhập từ browser → dùng luôn, không bootstrap Playwright
        # (cookies login đã pass Cloudflare; bootstrap Playwright bị captcha chặn)
        update_time = ""
        if has_login_cookies:
            print(f"  [Novel] Dùng cookies đăng nhập, bỏ qua bootstrap")
            update_time = get_update_time(session, book_host, book_id)
        else:
            ok, update_time = bootstrap_cookies_for_novel(session, book_host, book_id)
            if not ok:
                print(f"  [Novel] Không lấy được cookies, bỏ qua.")
                continue

        # Ghi update_time vào sheet list (re-lookup row)
        if update_time and update_col:
            try:
                current_row = find_current_row(novel_url)
                if current_row is None:
                    print(f"  [Sheets] Row của '{novel_url}' đã bị xoá, bỏ qua ghi UPDATE")
                else:
                    if current_row != row_index:
                        print(f"  [Sheets] Row đã thay đổi: {row_index} → {current_row}")
                        row_index = current_row
                    sheet_list.update_cell(current_row, update_col, update_time)
                    print(f"  [Sheets] Updated UPDATE: {update_time}")
            except Exception as e:
                print(f"  [Sheets] Lỗi ghi UPDATE: {e}")

        # Lấy danh sách chương từ STV
        chapter_list = fetch_chapter_list(session, book_host, book_id)
        if not chapter_list:
            print(f"  [Novel] Không lấy được danh sách chương, bỏ qua.")
            continue

        # Scrape từng chương
        saved = 0
        for chap in chapter_list:
            chap_id, chap_name = chap["id"], chap["name"]
            if chap_id in existing_ids:
                continue

            print(f"  [Chap] {chap_name} (id={chap_id})")

            # Retry với delay tăng dần khi gặp lỗi
            result = None
            for attempt in range(1, MAX_RETRIES + 1):
                result = get_chapter_content(session, book_host, book_id, chap_id)

                if result == "RATE_LIMIT":
                    print(f"    [Captcha] Server yêu cầu xác nhận. Mở Chrome để bạn click captcha...")
                    if solve_captcha_manually(session, book_host, book_id, chap_id):
                        result = None
                        continue
                    else:
                        print(f"    [Captcha] Không pass được captcha")
                        break

                if result == "VIP":
                    # Chương VIP cần đăng nhập — bỏ qua chương này, tiếp tục chương sau
                    print(f"    [VIP] Chương cần đăng nhập, bỏ qua chương này")
                    break

                if result is not None:
                    break

                # Delay tăng dần: 30s, 60s, 120s — chờ server hồi phục
                wait = 30 * (2 ** (attempt - 1))
                print(f"    [Retry {attempt}/{MAX_RETRIES}] Chờ {wait}s rồi bootstrap lại...")
                time.sleep(wait)

                # Bootstrap, verify đủ cookies (cần cả _ac, _acx, PHPSESSID)
                for boot_attempt in range(3):
                    ok, _ = bootstrap_cookies_for_novel(session, book_host, book_id)
                    if ok:
                        break
                    print(f"    [Bootstrap retry {boot_attempt+1}/3] Cookies chưa đủ, chờ 10s...")
                    time.sleep(10)

            # Chương VIP → bỏ qua chương này nhưng tiếp tục các chương sau
            if result == "VIP":
                continue

            if result is None or result == "RATE_LIMIT":
                print(f"    [SKIP] Bỏ qua sau {MAX_RETRIES} lần thử")
                break

            _, content = result

            # Chia nhỏ nếu vượt giới hạn ô Sheets
            if len(content) > CHAR_LIMIT:
                rows = [
                    [chap_id, chap_name, content[k: k + CHAR_LIMIT]]
                    for k in range(0, len(content), CHAR_LIMIT)
                ]
                worksheet.append_rows(rows, value_input_option='RAW')
            else:
                worksheet.append_row([chap_id, chap_name, content], value_input_option='RAW')

            saved += 1
            print(f"    [OK] Đã lưu ({len(content)} ký tự)")
            time.sleep(DELAY_BETWEEN_CHAPS)

        print(f"  [Novel] Hoàn tất: lưu {saved} chương mới")

    print("\n[Main] Hoàn tất tất cả truyện.")


if __name__ == "__main__":
    main()
