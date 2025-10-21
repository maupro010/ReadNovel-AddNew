#-------------------------------------------
#hoàn thiện thông tin truyện cuối cùng list, cập nhật các chương mới của truyện mới
#chạy bằng proxy
#-------------------------------------------

import asyncio
from playwright.async_api import async_playwright
import os
import csv
import gspread
import re

# --- THÔNG TIN ĐĂNG NHẬP ---
LOGIN_EMAIL = os.environ.get('LOGIN_EMAIL')
LOGIN_PASSWORD = os.environ.get('LOGIN_PASSWORD')
# ------------------------------

# --- THÔNG TIN GOOGLE SHEET ---
GOOGLE_SHEET_NAME = "https://docs.google.com/spreadsheets/d/1rCGTw4GdGlR4K-H7hDk8TjjnGh1jL3NgNZLRQ_h8jY8/edit?usp=sharing"
CREDENTIALS_FILE = "credentials.json"
# ------------------------------

# List này sẽ chỉ lưu text từ canvas cho mỗi chương
captured_canvas_texts = []

async def get_max_chapter(page):
    max_seclector = '#app > div:nth-child(2) > div > main > div.space-y-5 > div.block.md\\:flex > div.mb-4.mx-auto.text-center.md\\:mx-0.md\\:text-left > div.space-x-4.mb-6.md\\:mb-8 > button:nth-child(3) > span.absolute.-right-4.-top-4 > span'
    update = '#app > div:nth-child(2) > div > main > div.space-y-5 > div.pb-3 > div.pt-6.px-4.md\\:px-2.grid.grid-cols-1.gap-4.md\\:grid-cols-3 > a:nth-child(3) > div.flex.items-center.text-xs.text-gray-400 > span'
    
    try:
        # Lấy tiêu đề chương
        await page.wait_for_selector(max_seclector, timeout=30000)
        max_chapter = await page.locator(max_seclector).inner_text()

        await page.wait_for_selector(update, timeout=30000)
        update = await page.locator(update).inner_text()


        return max_chapter, update;

    except Exception as e:
        print(f"Lỗi khi lấy số chương: {e}")
        return None

async def scrape_novel_detail(page):
    """
    Hàm này lấy tiêu đề và toàn bộ thông tin của truyện
    và trả về dưới dạng một dictionary.
    """
    content_selector = '#chapter-content'
    title_selector = 'a.font-semibold.text-lg.text-title'
    img_selector = 'img.w-44'
    author_selector = 'div.mb-6 a'
    desc_selector = '#synopsis > div.text-gray-600.dark\\:text-gray-300.py-4.px-2.md\\:px-1.text-base.break-words'
    max_seclector = '#app > div:nth-child(2) > div > main > div.space-y-5 > div.block.md\\:flex > div.mb-4.mx-auto.text-center.md\\:mx-0.md\\:text-left > div.space-x-4.mb-6.md\\:mb-8 > button:nth-child(3) > span.absolute.-right-4.-top-4 > span'
    update_selector = '#app > div:nth-child(2) > div > main > div.space-y-5 > div.pb-3 > div.pt-6.px-4.md\\:px-2.grid.grid-cols-1.gap-4.md\\:grid-cols-3 > a:nth-child(3) > div.flex.items-center.text-xs.text-gray-400 > span'
    id_selector = '#app > div:nth-child(2) > div > main > div.space-y-5 > div.block.md\\:flex > div.mb-4.mx-auto.text-center.md\\:mx-0.md\\:text-left > div.space-x-4.mb-6.md\\:mb-8 > div'
    # Khởi tạo tất cả các biến với giá trị mặc định là chuỗi rỗng
    title = ""
    img = ""
    author = ""
    desc = ""
    maxChapter = ""
    update = ""
    target_id = ""

    try:
        # --- Lấy tiêu đề ---
        try:
            # Tối ưu: .inner_text() đã bao gồm auto-wait, không cần .wait_for_selector()
            title = await page.locator(title_selector).inner_text(timeout=30000)
            print(f"Đã tìm thấy title: {title.strip()}")
        except Exception as e:
            print(f"⚠️ Lỗi khi lấy title: {e}")

        # --- Lấy img url ---
        try:
            # .get_attribute() cũng đã bao gồm auto-wait
            img_url = await page.locator(img_selector).get_attribute("src", timeout=30000)
            if img_url: # Phải kiểm tra None trước khi gán
                img = img_url
                print(f"Đã tìm thấy img: {img.strip()}")
            else:
                print("⚠️ Đã tìm thấy img_selector, nhưng không có thuộc tính 'src'.")
        except Exception as e:
            print(f"⚠️ Lỗi khi lấy img: {e}")
        
        # --- Lấy author ---
        try:
            author = await page.locator(author_selector).inner_text(timeout=30000)
            print(f"Đã tìm thấy author: {author.strip()}")
        except Exception as e:
            print(f"⚠️ Lỗi khi lấy author: {e}")
        
        # --- Lấy desc ---
        try:
            desc = await page.locator(desc_selector).inner_text(timeout=30000)
            print(f"Đã tìm thấy desc: {desc.strip()}")
        except Exception as e:
            print(f"⚠️ Lỗi khi lấy desc: {e}")

        # --- Lấy maxChapter ---
        try:
            maxChapter = await page.locator(max_seclector).inner_text(timeout=30000)
            print(f"Đã tìm thấy maxChapter: {maxChapter.strip()}")
        except Exception as e:
            print(f"⚠️ Lỗi khi lấy maxChapter: {e}")
        
        # --- Lấy update ---
        try:
            update = await page.locator(update_selector).inner_text(timeout=30000)
            print(f"Đã tìm thấy update: {update.strip()}")
        except Exception as e:
            print(f"⚠️ Lỗi khi lấy update: {e}")
        
        # --- Lấy ID (data-x-data) ---
        try:
            data_x_data = await page.locator(id_selector).get_attribute("data-x-data", timeout=60000)
            if data_x_data:
                match = re.search(r'\(([^)]+)\)', data_x_data)
                if match:
                    target_id = match.group(1).strip()
                    print(f"Đã lấy được ID (Dùng Regex): {target_id}")
                else:
                    print("⚠️ Không tìm thấy ID trong thuộc tính data-x-data.")
            else:
                print("⚠️ Không tìm thấy thuộc tính data-x-data.")
        except Exception as e:
            print(f"⚠️ Lỗi khi lấy ID (data-x-data): {e}")
        
        # Trả về dictionary, .strip() bây giờ đã an toàn vì tất cả đều là chuỗi
        return {
            "title": title.strip(), 
            "img": img.strip(), 
            "author": author.strip(), 
            "desc": desc.strip(), 
            "maxChapter": maxChapter.strip(), 
            "update": update.strip(), 
            "id": target_id.strip()
        }

    except Exception as e:
        # Khối except bên ngoài này sẽ bắt các lỗi thảm họa (ví dụ: page bị đóng)
        print(f"❌ Lỗi nghiêm trọng khi lấy chi tiết truyện: {e}")
        return None

async def scrape_chapter_content(page):
    """
    Hàm này lấy tiêu đề và nội dung chương bằng cách thực hiện toàn bộ logic
    bên trong trình duyệt để đảm bảo đúng thứ tự.
    """
    content_selector = '#chapter-content'
    title_selector = 'h2.text-center'
    try:
        # Lấy tiêu đề chương
        await page.wait_for_selector(title_selector, timeout=30000)
        title = await page.locator(title_selector).inner_text()
        print(f"Đã tìm thấy tiêu đề: {title.strip()}")

        await page.wait_for_selector(content_selector, timeout=30000)
        print("Container nội dung đã xuất hiện. Bắt đầu thu thập...")

        # === LOGIC MỚI ===
        # Toàn bộ việc bắt text canvas và ghép nối được thực hiện trong một lần evaluate
        full_content = await page.evaluate(f"""
        (async () => {{
            const contentDiv = document.querySelector('{content_selector}');
            if (!contentDiv) return "[LỖI: Không tìm thấy container nội dung]";

            // Bước 1: Tạo một mảng tạm để lưu text từ canvas theo đúng thứ tự
            const capturedCanvasTexts = [];
            const originalFillText = CanvasRenderingContext2D.prototype.fillText;

            // Bước 2: Ghi đè (override) hàm fillText để bắt text
            // Text sẽ được đẩy vào mảng tạm ở trên
            CanvasRenderingContext2D.prototype.fillText = function(...args) {{
                const text = args[0];
                if (text && !text.includes('Cwm fjordbank gly')) {{
                    capturedCanvasTexts.push(text.trim());
                }}
                originalFillText.apply(this, args);
            }};

            // Bước 3: Đợi một khoảng ngắn để trình duyệt có thời gian vẽ lên canvas
            // Điều này kích hoạt hàm fillText đã bị ghi đè của chúng ta
            await new Promise(r => setTimeout(r, 300));

            // Bước 4: Khôi phục lại hàm fillText gốc để tránh ảnh hưởng các trang khác
            CanvasRenderingContext2D.prototype.fillText = originalFillText;

            // Bước 5: Bây giờ mới đọc cấu trúc DOM và ghép nối kết quả
            const final_content_parts = [];
            const canvasIterator = capturedCanvasTexts.values(); // Tạo iterator cho mảng canvas
            
            const nodes = Array.from(contentDiv.childNodes);
            nodes.forEach(node => {{
                if (node.nodeType === Node.TEXT_NODE) {{
                    const text = node.textContent.trim();
                    if (text) final_content_parts.push(text);
                }} else if (node.nodeType === Node.ELEMENT_NODE) {{
                    if (node.tagName.toUpperCase() === 'CANVAS') {{
                        const canvasText = canvasIterator.next().value;
                        if(canvasText) final_content_parts.push(canvasText);

                    }} else if (node.tagName.toUpperCase() === 'BR') {{
                        final_content_parts.push('\\n');
                    }} else if (node.tagName.toUpperCase() === 'DIV') {{
                        const divText = node.textContent.trim();
                        if (divText) final_content_parts.push(divText);
                    }}
                }}
            }});
            
            return final_content_parts.join('\\n').replace(/\\n+/g, '\\n\\n');
        }})()
        """)
        
        return { "title": title.strip(), "content": full_content.strip().replace("·","") }

    except Exception as e:
        print(f"Lỗi khi lấy nội dung chương: {e}")
        return None

async def main():
    """
    Hàm chính điều khiển toàn bộ quá trình: đăng nhập, duyệt và lưu chương.
    """
    # Lấy thông tin proxy từ biến môi trường
    PROXY_SERVER = os.environ.get('PROXY_SERVER')
    PROXY_USER = os.environ.get('PROXY_USER')
    PROXY_PASS = os.environ.get('PROXY_PASS')

    proxy_settings = None
    if PROXY_SERVER:
        proxy_settings = {
            "server": f"http://{PROXY_SERVER}",
            "username": PROXY_USER,
            "password": PROXY_PASS
        }
        print(f"--- Đang sử dụng proxy: {PROXY_SERVER} ---")
    else:
        print("--- Không tìm thấy thông tin proxy, chạy không qua proxy ---")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            proxy=proxy_settings
        )
        context = await browser.new_context()
        page = await context.new_page()
        page.set_default_timeout(60000) # 90 giây

        try:
            # --- PHẦN 1: ĐĂNG NHẬP (Chỉ chạy một lần) ---
            print("Bắt đầu quá trình đăng nhập...")
            await page.goto("https://metruyencv.com", wait_until="domcontentloaded", timeout=60000)
            
            menu_icon_locator = page.locator('svg:has(path[d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5"])')
            await menu_icon_locator.wait_for(state="visible", timeout=15000)
            await menu_icon_locator.click()
            await page.get_by_role("button", name="Đăng nhập").click()
            await page.get_by_placeholder("email").fill(LOGIN_EMAIL)
            await page.get_by_placeholder("password").fill(LOGIN_PASSWORD)
            await page.get_by_role("button", name="Đăng nhập").click()
            print("Đăng nhập thành công!")
            await page.wait_for_timeout(5000) # Tăng thời gian chờ

            
            # --- PHẦN 0: KẾT NỐI VÀ KIỂM TRA GOOGLE SHEET ---
            print("Đang kết nối tới Google Sheets...")
            gc = gspread.service_account(filename=CREDENTIALS_FILE)
            sh = gc.open_by_url(GOOGLE_SHEET_NAME)
            print("Kết nối thành công!")
            sheet_list = sh.worksheet("list")
            existing_stt_list = sheet_list.col_values(1)
            list_novel = existing_stt_list[1:]
            ID = list_novel[-1]

            print("Đang lấy thông tin truyện")
            await page.goto("https://metruyencv.com/truyen/"+ID, wait_until="domcontentloaded")
            scraped_data = await scrape_novel_detail(page)
            if scraped_data:
                sheet_list.update_cell(list_novel.index(ID)+2, 2, scraped_data['title'])
                sheet_list.update_cell(list_novel.index(ID)+2, 3, scraped_data['author'])
                sheet_list.update_cell(list_novel.index(ID)+2, 4, scraped_data['desc'])
                sheet_list.update_cell(list_novel.index(ID)+2, 5, scraped_data['img'])
                sheet_list.update_cell(list_novel.index(ID)+2, 6, scraped_data['maxChapter'])
                sheet_list.update_cell(list_novel.index(ID)+2, 7, scraped_data['update'])
                sheet_list.update_cell(list_novel.index(ID)+2, 8, scraped_data['id'])

            max_chapter = scraped_data['maxChapter']

            sheet_title = ID
            existing_chapters = set()
            try:
                worksheet = sh.worksheet(sheet_title)
                print(f"Đã tìm thấy sheet có sẵn: '{sheet_title}'")
                existing_stt_list = worksheet.col_values(1)
                existing_chapters = {int(stt) for stt in existing_stt_list if stt.isdigit()}
                print(f"Các chương đã có trong sheet: {sorted(list(existing_chapters))}")
            except gspread.WorksheetNotFound:
                print(f"Không tìm thấy sheet. Đang tạo sheet mới: '{sheet_title}'")
                worksheet = sh.add_worksheet(title=sheet_title, rows="1", cols="3")
                worksheet.append_row(['ID', 'NAME', 'content'])
            
            print("✅ Kết nối thành công.")
            
            BASE_URL = "https://metruyencv.com/truyen/"+ID+"/chuong-{}"
            # --- PHẦN 2: DUYỆT QUA CÁC CHƯƠNG ---
            i = 1
            chapter = int(max_chapter)
            while i<=chapter:
                # >>> KIỂM TRA NẾU CHƯƠNG ĐÃ TỒN TẠI THÌ BỎ QUA <<<
                if i in existing_chapters:
                    i+=1
                    continue

                chapter_url = BASE_URL.format(i)
                print(f"\n--- Đang xử lý chương {i}: {chapter_url} ---")
                
                captured_canvas_texts.clear()
                
                await page.goto(chapter_url, wait_until="domcontentloaded")
                
                scraped_data = await scrape_chapter_content(page)
                
                if scraped_data:
                    worksheet.append_row([i, scraped_data['title'], scraped_data['content']])
                    print(f"Đã lưu thành công chương {i} vào Google Sheet")
                else:
                    worksheet.append_row([i, 'title', 'content'])
                    print(f"Bỏ qua chương {i} do không lấy được nội dung.")
                    chapter+=1                    
                i+=1
                

        except Exception as e:
            print(f"❌ Đã xảy ra lỗi nghiêm trọng: {e}")
            try:
                await page.screenshot(path='screenshots/00_ERROR.png')
                print("Đã chụp ảnh màn hình lỗi.")
            except Exception as screenshot_error:
                print(f"Không thể chụp ảnh màn hình: {screenshot_error}")

        finally:
            print("\nQuá trình đã hoàn tất. Đóng trình duyệt.")
            await browser.close()

# Chạy script
if __name__ == "__main__":
    asyncio.run(main())
