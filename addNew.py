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

INIT_SCRIPT = """
(() => {
    // Một hàm chung để xử lý việc bắt text
    const textCapture = function(originalMethod, ...args) {
        const text = args[0];
        
        // Chỉ cần text tồn tại và không phải là chuỗi rác
        if (text && text.trim() && !text.includes('Cwm fjordbank gly')) {
            // 'this' là context, 'this.canvas' là thẻ <canvas> DOM
            const canvas = this.canvas; 
            
            // Lấy text đã có (nếu canvas vẽ nhiều dòng) và nối thêm
            let currentText = canvas.getAttribute('data-captured-text') || '';
            
            // Thêm một khoảng trắng để các từ không dính vào nhau
            canvas.setAttribute('data-captured-text', currentText + text.trim() + ' ');
        }
        
        // Luôn gọi hàm gốc
        originalMethod.apply(this, args);
    };

    // --- Patch fillText ---
    try {
        const originalFillText = CanvasRenderingContext2D.prototype.fillText;
        CanvasRenderingContext2D.prototype.fillText = function(...args) {
            textCapture.call(this, originalFillText, ...args);
        };
    } catch (e) {
        console.error('Failed to patch fillText:', e);
    }

    // --- Patch strokeText (cho chắc) ---
    try {
        const originalStrokeText = CanvasRenderingContext2D.prototype.strokeText;
        CanvasRenderingContext2D.prototype.strokeText = function(...args) {
            textCapture.call(this, originalStrokeText, ...args);
        };
    } catch (e) {
        console.error('Failed to patch strokeText:', e);
    }
})();
"""

# List này sẽ chỉ lưu text từ canvas cho mỗi chương
captured_canvas_texts = []

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
    max_seclector2 = '#app > div:nth-child(2) > main > div.space-y-5 > div.block.md\\:flex > div.mb-4.mx-auto.text-center.md\\:mx-0.md\\:text-left > div.space-x-4.mb-6.md\\:mb-8 > button:nth-child(3) > span.absolute.-right-4.-top-4 > span'
    update_selector2 = '#app > div:nth-child(2) > main > div.space-y-5 > div.pb-3 > div.pt-6.px-4.md\\:px-2.grid.grid-cols-1.gap-4.md\\:grid-cols-3 > a:nth-child(3) > div.flex.items-center.text-xs.text-gray-400 > span'
    id_selector2 = '#app > div:nth-child(2) > main > div.space-y-5 > div.block.md\\:flex > div.mb-4.mx-auto.text-center.md\\:mx-0.md\\:text-left > div.space-x-4.mb-6.md\\:mb-8 > div'
    
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
            maxChapter = await page.locator(max_seclector2).inner_text(timeout=30000)
            print(f"Đã tìm thấy maxChapter: {maxChapter.strip()}")
        except:
            try:
                maxChapter = await page.locator(max_seclector).inner_text(timeout=30000)
                print(f"Đã tìm thấy maxChapter: {maxChapter.strip()}")
            except Exception as e:
                print(f"⚠️ Lỗi khi lấy maxChapter: {e}")
        
        # --- Lấy update ---
        try:
            update = await page.locator(update_selector2).inner_text(timeout=30000)
            print(f"Đã tìm thấy update: {update.strip()}")
        except:
            try:
                update = await page.locator(update_selector).inner_text(timeout=30000)
                print(f"Đã tìm thấy update: {update.strip()}")
            except Exception as e:
                print(f"⚠️ Lỗi khi lấy update: {e}")
        
        # --- Lấy ID (data-x-data) ---
        try:
            data_x_data = await page.locator(id_selector2).get_attribute("data-x-data", timeout=60000)
            if data_x_data:
                match = re.search(r'\(([^)]+)\)', data_x_data)
                if match:
                    target_id = match.group(1).strip()
                    print(f"Đã lấy được ID (Dùng Regex): {target_id}")
                else:
                    print("⚠️ Không tìm thấy ID trong thuộc tính data-x-data.")
            else:
                print("⚠️ Không tìm thấy thuộc tính data-x-data.")
        except:
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
    Hàm này lấy tiêu đề và nội dung chương.
    1. Dùng wait_for_function để đảm bảo canvas đã được INIT_SCRIPT xử lý.
    2. Dùng evaluate để đọc DOM và thuộc tính 'data-captured-text' từ canvas.
    """
    content_selector = '#chapter-content'
    title_selector = 'h2.text-center'
    try:
        # Lấy tiêu đề chương
        title = await page.locator(title_selector).inner_text()
        print(f"Đã tìm thấy tiêu đề: {title.strip()}")

        await page.wait_for_selector(content_selector, timeout=3000)
        print("Container nội dung đã xuất hiện. Đang chờ canvas vẽ...")

        # === (THAY ĐỔI 2) LOGIC CHỜ MỚI - ĐÁNG TIN CẬY ===
        # Chờ cho đến khi BẤT KỲ canvas nào có thuộc tính (nghĩa là việc vẽ đã bắt đầu)
        # Hoặc cho đến khi không tìm thấy canvas nào
        await page.wait_for_function(f"""
            () => {{
                const container = document.querySelector('{content_selector}');
                if (!container) return false;
                
                const canvases = container.querySelectorAll('canvas');
                
                // Nếu không có canvas, coi như đã xong (trả về true)
                if (canvases.length === 0) return true; 
                
                // Trả về true nếu BẤT KỲ (some) canvas nào có thuộc tính
                // (nghĩa là INIT_SCRIPT đã bắt đầu hoạt động)
                return Array.from(canvases).some(canvas => canvas.hasAttribute('data-captured-text'));
            }}
        """, timeout=10000) # Cho tối đa 10 giây để BẮT ĐẦU vẽ

        print("Canvas đã BẮT ĐẦU vẽ. Chờ 500ms để ổn định...")
        # Thêm một khoảng chờ ngắn để TẤT CẢ các canvas khác vẽ xong (nếu có)
        # await page.wait_for_timeout(300)
        print("Đã ổn định. Bắt đầu thu thập...")
        # =======================================================


        # === (THAY ĐỔI 3) LOGIC EVALUATE MỚI - ĐỌC ATTRIBUTE ===
        # Logic này không cần mảng toàn cục nữa, chỉ cần đọc DOM
        full_content = await page.evaluate(f"""
        (async () => {{
            const contentDiv = document.querySelector('{content_selector}');
            if (!contentDiv) return "[LỖI: Không tìm thấy container nội dung]";
            
            // Không cần mảng toàn cục, không cần iterator
            
            const final_content_parts = [];
            const nodes = Array.from(contentDiv.childNodes);
            
            nodes.forEach(node => {{
                if (node.nodeType === 3) {{ // 3 == Node.TEXT_NODE (Text thô)
                    const text = node.textContent.trim();
                    if (text) final_content_parts.push(text);
                
                }} else if (node.nodeType === 1) {{ // 1 == Node.ELEMENT_NODE (Thẻ HTML)
                    
                    if (node.tagName.toUpperCase() === 'CANVAS') {{
                        // ĐỌC TRỰC TIẾP TỪ THUỘC TÍNH
                        const canvasText = node.getAttribute('data-captured-text');
                        if (canvasText) {{
                            final_content_parts.push(canvasText.trim());
                        }}

                    }} else if (node.tagName.toUpperCase() === 'BR') {{
                        final_content_parts.push('\\n');
                    }} else if (node.tagName.toUpperCase() === 'DIV') {{
                        const divText = node.textContent.trim();
                        if (divText) final_content_parts.push(divText);
                    }}
                }}
            }});
            
            let result = final_content_parts.join('\\n').replace(/\\n+/g, '\\n\\n');
            // Sửa lỗi f-string/regex: dùng {{...}}
            result = result.replace(/(\\n\\n ?){{2,}}/g, '\\n\\n');
            
            return result;
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
        await page.add_init_script(INIT_SCRIPT)
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
            # existing_stt_list = sheet_list.col_values(1)
            # list_novel = existing_stt_list[1:]
            ID = sheet_list.col_values(1)[-1]
            max_chapter = sheet_list.col_values(6)[-1]
            # print("Đang lấy thông tin truyện")
            # await page.goto("https://metruyencv.com/truyen/"+ID, wait_until="domcontentloaded")
            # scraped_data = await scrape_novel_detail(page)
            # if scraped_data:
            #     sheet_list.update_cell(list_novel.index(ID)+2, 2, scraped_data['title'])
            #     sheet_list.update_cell(list_novel.index(ID)+2, 3, scraped_data['author'])
            #     sheet_list.update_cell(list_novel.index(ID)+2, 4, scraped_data['desc'])
            #     sheet_list.update_cell(list_novel.index(ID)+2, 5, scraped_data['img'])
            #     sheet_list.update_cell(list_novel.index(ID)+2, 6, scraped_data['maxChapter'])
            #     sheet_list.update_cell(list_novel.index(ID)+2, 7, scraped_data['update'])
            #     sheet_list.update_cell(list_novel.index(ID)+2, 8, scraped_data['id'])

            # max_chapter = scraped_data['maxChapter']

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
            j = 1
            chapter = int(max_chapter)
            while j<=chapter:
                # >>> KIỂM TRA NẾU CHƯƠNG ĐÃ TỒN TẠI THÌ BỎ QUA <<<
                if i in existing_chapters:
                    i+=1
                    j+=1
                    continue

                chapter_url = BASE_URL.format(i)
                print(f"\n--- Đang xử lý chương {i}: {chapter_url} ---")
                
                captured_canvas_texts.clear()
                
                await page.goto(chapter_url, wait_until="domcontentloaded")
                
                scraped_data = await scrape_chapter_content(page)
                
                if scraped_data:
                    worksheet.append_row([j, scraped_data['title'], scraped_data['content']])
                    print(f"Đã lưu thành công chương {i} vào Google Sheet")
                    j+=1
                else:
                    # worksheet.append_row([i, 'title', 'content'])
                    print(f"Bỏ qua chương {i} do không lấy được nội dung.")
                    # chapter+=1
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
            sheet_list.update_cell(list_novel.index(ID)+2, 9, 'false')
            await browser.close()

# Chạy script
if __name__ == "__main__":
    asyncio.run(main())
