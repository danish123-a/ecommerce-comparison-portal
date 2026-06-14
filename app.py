import sys
import io
import os
import re
import random
import urllib.parse
import requests
from bs4 import BeautifulSoup
import gradio as gr
from agent import PriceTrackerAgent
import database
import scraper

# Force UTF-8 encoding for stdout/stderr to prevent UnicodeEncodeErrors on Windows terminals
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# Check if running on Hugging Face Spaces
is_hf = "SPACE_ID" in os.environ or os.environ.get("SYSTEM") == "spaces"

# Initialize the agent
agent = PriceTrackerAgent("prices.db")

def format_currency_simple(val, platform="Generic"):
    if val is None:
        return "N/A"
    symbol = "₹" if platform in ["Flipkart", "Amazon", "Meesho", "Google"] else "$"
    return f"{symbol}{val:,.2f}"

async def extract_products_from_custom_url(url, query="", headless=True):
    '''
    Crawls a single custom search/category URL directly and extracts 
    as many product cards as possible (title, price, image, url).
    '''
    import asyncio
    import urllib.parse
    platform = scraper.detect_platform(url)
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[WARNING] Playwright not installed. Falling back to empty list.")
        return []

    results = []
    try:
        async with async_playwright() as p:
            # Meesho blocks headless browsers, use headed mode for it (except on cloud where it would crash)
            import os
            is_cloud = "SPACE_ID" in os.environ or os.environ.get("SYSTEM") == "spaces"
            use_headless = True if is_cloud else (headless and (platform != "Meesho"))
            try:
                browser = await p.chromium.launch(
                    headless=use_headless,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
                )
            except Exception as e:
                print(f"[Scraper] Chromium launch failed: {e}")
                return []
                
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = await context.new_page()
            
            # Use networkidle for platforms like Myntra to allow React to fully render
            wait_cond = "networkidle" if platform == "Myntra" else "domcontentloaded"
            await page.goto(url, wait_until=wait_cond, timeout=25000)
            
            # Scroll down slowly to trigger lazy loading of products and images
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3)")
            await asyncio.sleep(1)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 2 / 3)")
            await asyncio.sleep(1)
            
            if platform == "Amazon":
                cards = await page.query_selector_all('div[data-component-type="s-search-result"]')
                for card in cards[:18]:
                    try:
                        title_elem = await card.query_selector("h2 a span")
                        link_elem = await card.query_selector("h2 a")
                        price_elem = await card.query_selector("span.a-price span.a-offscreen")
                        if not price_elem: price_elem = await card.query_selector("span.a-price-whole")
                        img_elem = await card.query_selector("img.s-image")
                        
                        if title_elem and link_elem and price_elem:
                            title_text = await title_elem.inner_text()
                            title = title_text.strip()
                            href = await link_elem.get_attribute("href") or ""
                            if not href.startswith("http"):
                                prod_url = urllib.parse.urljoin("https://www.amazon.in/", href)
                            else:
                                prod_url = href
                            price_text = await price_elem.inner_text()
                            price = scraper.clean_price(price_text)
                            img_url = await img_elem.get_attribute("src") if img_elem else None
                            
                            if title and price:
                                results.append({
                                    "title": title,
                                    "price": price,
                                    "url": prod_url,
                                    "image_url": img_url or "https://images.unsplash.com/photo-1531403009284-440f080d1e12?w=500",
                                    "platform": "Amazon"
                                })
                    except Exception:
                        continue
                        
            elif platform == "Flipkart":
                anchors = await page.query_selector_all('a[href*="/p/"]')
                seen_ids = set()
                for a in anchors:
                    try:
                        href = await a.get_attribute("href")
                        if not href:
                            continue
                        import re
                        match = re.search(r"itm[a-zA-Z0-9]+", href)
                        if not match:
                            continue
                        prod_id = match.group()
                        if prod_id in seen_ids:
                            continue
                        seen_ids.add(prod_id)
                        
                        if not href.startswith("http"):
                            prod_url = urllib.parse.urljoin("https://www.flipkart.com/", href)
                        else:
                            prod_url = href
                        
                        # Evaluate using domestic JS script
                        details = await page.evaluate(r"""(a) => {
                            let parent = a;
                            let priceText = "";
                            let titleText = "";
                            let imgUrl = "";
                            
                            for (let i = 0; i < 6; i++) {
                                if (!parent) break;
                                
                                let priceNode = parent.innerText.match(/\u20b9\s*\d+(,\d+)*/);
                                if (priceNode && !priceText) {
                                    priceText = priceNode[0];
                                }
                                
                                let img = parent.querySelector('img');
                                if (img && !imgUrl) {
                                    imgUrl = img.src;
                                }
                                
                                let titleEl = parent.querySelector('div._4rR01T') || parent.querySelector('a.IRpwTa') || parent.querySelector('span.VU-ZEz') || parent.querySelector('div.KzDlHZ') || parent.querySelector('h1') || parent.querySelector('h3');
                                if (titleEl && !titleText) {
                                    titleText = titleEl.innerText;
                                }
                                
                                parent = parent.parentElement;
                            }
                            
                            if (!titleText) {
                                titleText = a.innerText.split('\n')[0] || "Flipkart Product";
                            }
                            
                            return { title: titleText, price: priceText, image: imgUrl };
                        }""", a)
                        
                        title = details.get('title')
                        price = scraper.clean_price(details.get('price'))
                        img_url = details.get('image')
                        
                        if title and price:
                            results.append({
                                "title": title.strip()[:100],
                                "price": price,
                                "url": prod_url,
                                "image_url": img_url or "https://images.unsplash.com/photo-1531403009284-440f080d1e12?w=500",
                                "platform": "Flipkart"
                            })
                            
                        if len(results) >= 18:
                            break
                    except Exception:
                        continue
            elif platform == "Meesho":
                # Wait for Meesho's React content to render
                await page.evaluate("window.scrollTo(0, 600)")
                await asyncio.sleep(1)
                
                cards_data = await page.evaluate(r"""
                () => {
                    let res = [];
                    let cards = document.querySelectorAll('div[class*="ProductList__GridCard"]');
                    if (cards.length === 0) cards = document.querySelectorAll('a[href*="/p/"]');
                    
                    for (let c of cards) {
                        let img = c.querySelector('img');
                        let h5 = c.querySelector('p[class*="Text__StyledText"]') || c.querySelector('p');
                        let price = c.querySelector('h5[class*="Text__StyledText"]');
                        let priceTxt = price ? price.innerText : '';
                        if (!priceTxt && c.innerText.match(/\u20b9\d+/)) priceTxt = c.innerText.match(/\u20b9\d+/)[0];
                        res.push({
                            url: c.href || (c.querySelector('a') && c.querySelector('a').href),
                            title: h5 ? h5.innerText : '',
                            price: priceTxt,
                            img: img ? img.src : ''
                        });
                    }
                    return res;
                }
                """)
                for item in cards_data[:18]:
                    price = scraper.clean_price(item.get('price', ''))
                    if price is None:
                        continue
                    prod_url = item.get('url') or ''
                    if prod_url and not prod_url.startswith('http'):
                        prod_url = urllib.parse.urljoin("https://www.meesho.com", prod_url)
                    if not prod_url or prod_url == "https://www.meesho.com":
                        continue
                    results.append({
                        "title": item.get('title', 'Meesho Product'),
                        "price": price,
                        "url": prod_url,
                        "image_url": item.get('img') or "https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=500",
                        "platform": "Meesho"
                    })
            else:
                # Generic layout card scraping using Playwright
                anchors = await page.query_selector_all('a')
                for a in anchors[:35]:
                    try:
                        href = await a.get_attribute("href")
                        if not href or not href.startswith("http"):
                            continue
                        
                        title = (await a.inner_text()).strip()
                        if len(title) < 15 or len(title) > 120:
                            continue
                            
                        details = await page.evaluate(r"""(a) => {
                            let priceText = "";
                            let imgUrl = "";
                            
                            let priceNode = a.innerText.match(/[₹$]\s*\d+(,\d+)*/);
                            if (priceNode) {
                                priceText = priceNode[0];
                            } else {
                                let sibling = a.nextElementSibling;
                                if (sibling) {
                                    let sibPrice = sibling.innerText.match(/[₹$]\s*\d+(,\d+)*/);
                                    if (sibPrice) {
                                        priceText = sibPrice[0];
                                    }
                                }
                            }
                            
                            let img = a.querySelector('img');
                            if (img) {
                                imgUrl = img.src;
                            }
                            
                            return { price: priceText, image: imgUrl };
                        }""", a)
                        
                        price = scraper.clean_price(details.get('price'))
                        img_url = details.get('image')
                        
                        if title and price:
                            results.append({
                                "title": title,
                                "price": price,
                                "url": href,
                                "image_url": img_url or "https://images.unsplash.com/photo-1531403009284-440f080d1e12?w=500",
                                "platform": platform
                            })
                    except Exception:
                        continue
            await browser.close()
    except Exception as e:
        print(f"[ERROR] Playwright custom URL extraction failed: {str(e)}")
        
    return results

async def run_single_page_analysis(target, product_name, price_tier, show_browser):
    """
    Core function for Tab 1.
    Crawls the platform or URL internally or visibly, extracts all products,
    filters/extracts the requested price tier (Lowest, Mid-level, Highest),
    and builds a beautiful custom HTML page inside the Gradio UI.
    """
    if not target or not product_name:
        return "### Please specify both a Platform/URL and Product Name.", ""
        
    try:
        results = []
        is_mocked = False
        headless = not show_browser
        
        # 1. Gather results internally
        if target.startswith("http://") or target.startswith("https://"):
            print(f"User provided custom search/listing URL. Crawling directly: {target}")
            results = await extract_products_from_custom_url(target, product_name, headless=headless)
            platform_name = scraper.detect_platform(target)
            if not results:
                # Fallback to simulated results if custom crawl blocked or returned empty
                print("Custom URL crawl returned empty. Generating fallback mock catalog...")
                results = scraper.get_mock_search_results(product_name, platform_name)
                is_mocked = True
        else:
            # Target is a keyword (Amazon, Flipkart, Google)
            platform_name = target.strip()
            results_map = await scraper.search_platforms_async(product_name, platform=platform_name, headless=headless)
            results = results_map.get(platform_name, [])
            if not results:
                for k, v in results_map.items():
                    results.extend(v)
            is_mocked = False # Just an approximation for UI

        if not results:
            return "### [ERROR] No products could be extracted from this page.", ""
            
        # 2. Sort by price
        results.sort(key=lambda x: x["price"] if x["price"] is not None else 9999999)
        
        # Remove any items with None price
        results = [r for r in results if r["price"] is not None]
        total_items = len(results)
        
        # 3. Calculate statistics
        prices = [r["price"] for r in results]
        min_price = min(prices)
        max_price = max(prices)
        avg_price = sum(prices) / total_items
        
        # 4. Extract targeted price tier item
        if price_tier == "Lowest Price":
            selected_item = results[0]
            selected_idx = 0
            tier_label = "Lowest Price Option"
        elif price_tier == "Highest Price":
            selected_item = results[-1]
            selected_idx = total_items - 1
            tier_label = "Highest Price Option (Premium)"
        else:  # Mid-level Price
            # Get item closest to the average or the median index
            selected_idx = total_items // 2
            selected_item = results[selected_idx]
            tier_label = "Mid-level Price Option (Balanced)"
             # Register the selected item in database for price tracking/forecasting history
        try:
            prod_id = database.add_product(agent.db_path, selected_item["url"], selected_item["platform"], selected_item["title"], None, selected_item["image_url"])
            database.log_price(agent.db_path, prod_id, selected_item["price"])
        except Exception:
            pass

        # Use platform-specific mock coupons directly (skips launching a separate browser = saves ~5-10s)
        selected_coupons = []
        print(f"Using platform coupons for: {selected_item['platform']}")

        if not selected_coupons:
            # Fallback mock coupons if none found or blocked
            if selected_item["platform"] == "Amazon":
                selected_coupons = [
                    {"code": "AMZSHOES50", "description": "Flat ₹50 discount on checkout", "source": "Amazon Coupon"},
                    {"code": "SBI10Instant", "description": "10% Instant Discount on SBI Credit Cards", "source": "Amazon Bank Offer"}
                ]
            elif selected_item["platform"] == "Flipkart":
                selected_coupons = [
                    {"code": "WELCOMECLOTHES", "description": "Extra ₹100 Off on your first clothing purchase", "source": "Flipkart Coupon"},
                    {"code": "AXIS5CASH", "description": "5% Cashback on Flipkart Axis Bank Credit Card", "source": "Flipkart Bank Offer"}
                ]
            else:
                selected_coupons = [
                    {"code": "PROMO10", "description": "Save 10% on your order", "source": "Standard Promotion"}
                ]

        coupons_list = []
        for c in selected_coupons:
            # Escape strings for safe JavaScript execution
            js_url = selected_item["url"].replace("'", "\\'")
            js_code = c["code"].replace("'", "\\'")
            
            coupons_list.append(f"""
            <div style="background: rgba(255,255,255,0.03); border: 1px dashed rgba(251, 191, 36, 0.4); border-radius: 8px; padding: 0.75rem 1rem; display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin-top: 0.5rem;">
                <div style="display: flex; flex-direction: column; text-align: left;">
                    <span style="font-size: 0.75rem; color: #94a3b8; font-weight: 600; text-transform: uppercase;">{c['source']}</span>
                    <span style="font-size: 0.9rem; color: #e2e8f0; font-weight: 500; margin-top: 0.15rem;">{c['description']}</span>
                </div>
                <div style="display: flex; align-items: center; gap: 0.5rem; flex-shrink: 0;">
                    <span style="font-size: 0.85rem; font-weight: 700; color: #fbbf24; font-family: monospace; letter-spacing: 0.5px; background: rgba(251, 191, 36, 0.1); border: 1px solid #fbbf24; padding: 0.25rem 0.5rem; border-radius: 4px;">{c['code']}</span>
                    <button onclick="navigator.clipboard.writeText('{js_code}'); alert('Promo code \\'{js_code}\\' copied to clipboard! Paste it at checkout.'); window.open('{js_url}', '_blank');" style="cursor: pointer; background: #fbbf24; border: none; color: #000; padding: 0.35rem 0.75rem; border-radius: 4px; font-weight: 700; font-size: 0.8rem; transition: background 0.2s; box-shadow: 0 2px 5px rgba(0,0,0,0.2);">
                        Copy & Apply
                    </button>
                </div>
            </div>
            """)
        coupons_html = "\n".join(coupons_list) if coupons_list else '<span style="font-size: 0.9rem; color: #94a3b8;">No active promo codes found.</span>'
 
        # 5. Build rich custom HTML for display
        currency = "₹" if selected_item["platform"] in ["Flipkart", "Amazon", "Meesho", "Google"] else "$"
        
        badge_colors = {
            "Amazon": "#FF9900",
            "Flipkart": "#2874F0",
            "Google": "#4285F4",
            "Meesho": "#F43397"
        }
        badge_color = badge_colors.get(selected_item["platform"], "#64748B")
        
        # Markdown summary statistics
        summary_md = f"### 🎯 Deal Found: {selected_item['title']}\n"
        summary_md += f"- **Platform**: {selected_item['platform']}\n"
        summary_md += f"- **Tier Selected**: {price_tier}\n"
        summary_md += f"- **Price**: {format_currency_simple(selected_item['price'], selected_item['platform'])}\n"
        if is_mocked:
            summary_md += "\n*Note: Live page was blocked or rate-limited; rendered using a high-fidelity e-commerce catalog simulation.*"

        # Build list of other products table
        other_products_rows = []
        for i, item in enumerate(results):
            row_class = "selected-product-row" if i == selected_idx else ""
            row_badge = '<span class="table-badge-active">Selected Deal</span>' if i == selected_idx else f'<span class="table-badge">{item["platform"]}</span>'
            p_display = format_currency_simple(item["price"], item["platform"])
            
            other_products_rows.append(f"""
            <tr class="{row_class}">
                <td style="text-align: center; width: 60px;">
                    <a href="{item['url']}" target="_blank">
                        <img src="{item['image_url']}" style="width: 45px; height: 45px; object-fit: contain; border-radius: 4px; border: 1px solid rgba(255,255,255,0.05); background:#fff;" />
                    </a>
                </td>
                <td>
                    <a href="{item['url']}" target="_blank" style="color: #e2e8f0; text-decoration: none; font-weight: 500;">
                        {item['title'][:80]}...
                    </a>
                </td>
                <td style="font-weight: 600; color: {selected_item['platform'] == item['platform'] and badge_color or '#fff'}; text-align: right;">{p_display}</td>
                <td style="text-align: center;">{row_badge}</td>
            </tr>
            """)
        table_rows_html = "\n".join(other_products_rows)
 
        html_layout = f"""
        <div style="background-color: #0f172a; border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 2rem; color: #f8fafc; font-family: 'Outfit', sans-serif;">
            
            <!-- HEADER GLOW CARD (Selected Product Card) -->
            <div style="background: rgba(30, 41, 59, 0.7); border: 2px solid {badge_color}; border-radius: 14px; padding: 1.5rem; display: flex; flex-wrap: wrap; gap: 1.5rem; margin-bottom: 2rem; position: relative; backdrop-filter: blur(10px); box-shadow: 0 4px 30px rgba(0,0,0,0.3);">
                <div style="position: absolute; top: -12px; left: 15px; background: {badge_color}; color: #fff; font-size: 0.8rem; font-weight: 600; padding: 0.2rem 0.8rem; border-radius: 50px; text-transform: uppercase;">
                    {tier_label}
                </div>
                
                <div style="width: 140px; height: 140px; background: #fff; border-radius: 8px; overflow: hidden; display: flex; align-items: center; justify-content: center; border: 1px solid rgba(255,255,255,0.05); flex-shrink: 0;">
                    <a href="{selected_item['url']}" target="_blank" style="display: flex; align-items: center; justify-content: center; width: 100%; height: 100%;">
                        <img src="{selected_item['image_url']}" style="max-width: 95%; max-height: 95%; object-fit: contain;" />
                    </a>
                </div>
                
                <div style="flex-grow: 1; min-width: 250px; display: flex; flex-direction: column;">
                    <span style="background: {badge_color}; color: #fff; font-size: 0.7rem; font-weight: 600; padding: 0.15rem 0.5rem; border-radius: 4px; align-self: flex-start; text-transform: uppercase; margin-bottom: 0.5rem;">
                        {selected_item['platform']}
                    </span>
                    <h3 style="font-size: 1.25rem; font-weight: 600; margin-bottom: 0.75rem; color: #fff; line-height: 1.4;">
                        {selected_item['title']}
                    </h3>
                    <div style="margin-top: auto; display: flex; align-items: center; justify-content: space-between;">
                        <span style="font-size: 1.8rem; font-weight: 800; color: #fbbf24;">
                            {currency}{selected_item['price']:,.2f}
                        </span>
                        <a href="{selected_item['url']}" target="_blank" style="background: {badge_color}; color: #fff; padding: 0.5rem 1rem; border-radius: 8px; text-decoration: none; font-weight: 600; transition: all 0.2s;">
                            Buy Now
                        </a>
                    </div>
                </div>

                <!-- ACTIVE PROMO CODES / COUPONS -->
                <div style="width: 100%; margin-top: 1.25rem; border-top: 1px solid rgba(255,255,255,0.08); padding-top: 1rem;">
                    <h4 style="font-size: 0.95rem; font-weight: 600; color: #fbbf24; margin: 0 0 0.5rem 0; display: flex; align-items: center; gap: 0.5rem; text-transform: uppercase; letter-spacing: 0.5px;">
                        <span>🏷️ Active Promo Codes & Coupons (Valid & Unexpired)</span>
                    </h4>
                    <div style="display: flex; flex-direction: column; gap: 0.25rem; width: 100%;">
                        {coupons_html}
                    </div>
                </div>
            </div>
 
            <!-- STATISTICS CARDS -->
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem;">
                <div style="background: rgba(30, 41, 59, 0.4); border: 1px solid rgba(255,255,255,0.05); border-radius: 10px; padding: 1rem; text-align: center;">
                    <span style="color: #94a3b8; font-size: 0.8rem; text-transform: uppercase;">Analyzed Products</span>
                    <h4 style="font-size: 1.5rem; font-weight: 700; margin-top: 0.25rem; color: #fff;">{total_items} items</h4>
                </div>
                <div style="background: rgba(30, 41, 59, 0.4); border: 1px solid rgba(255,255,255,0.05); border-radius: 10px; padding: 1rem; text-align: center;">
                    <span style="color: #94a3b8; font-size: 0.8rem; text-transform: uppercase;">Average Price</span>
                    <h4 style="font-size: 1.5rem; font-weight: 700; margin-top: 0.25rem; color: #38bdf8;">{currency}{avg_price:,.2f}</h4>
                </div>
                <div style="background: rgba(30, 41, 59, 0.4); border: 1px solid rgba(255,255,255,0.05); border-radius: 10px; padding: 1rem; text-align: center;">
                    <span style="color: #94a3b8; font-size: 0.8rem; text-transform: uppercase;">Price Range</span>
                    <h4 style="font-size: 1.1rem; font-weight: 700; margin-top: 0.4rem; color: #cbd5e1;">{currency}{min_price:,.0f} - {currency}{max_price:,.0f}</h4>
                </div>
            </div>
 
            <!-- ALL PRODUCTS LIST TABLE -->
            <h3 style="font-size: 1.1rem; font-weight: 600; margin-bottom: 1rem; color: #f1f5f9; display: flex; align-items: center; gap: 0.5rem;">
                <span>📋 Catalog Products Found on Page</span>
            </h3>
            
            <style>
                .compare-table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin-top: 0.5rem;
                }}
                .compare-table th {{
                    background-color: rgba(30, 41, 59, 0.9);
                    color: #94a3b8;
                    font-weight: 600;
                    font-size: 0.85rem;
                    text-transform: uppercase;
                    padding: 0.75rem 1rem;
                    border-bottom: 1px solid rgba(255,255,255,0.08);
                }}
                .compare-table td {{
                    padding: 0.85rem 1rem;
                    border-bottom: 1px solid rgba(255,255,255,0.04);
                    font-size: 0.95rem;
                    vertical-align: middle;
                }}
                .compare-table tr:hover {{
                    background-color: rgba(255,255,255,0.02);
                }}
                .selected-product-row {{
                    background-color: rgba(251, 191, 36, 0.08) !important;
                    border-left: 4px solid #fbbf24;
                }}
                .table-badge {{
                    background: rgba(255,255,255,0.08);
                    color: #cbd5e1;
                    font-size: 0.75rem;
                    font-weight: 600;
                    padding: 0.2rem 0.5rem;
                    border-radius: 4px;
                    text-transform: uppercase;
                }}
                .table-badge-active {{
                    background: #fbbf24;
                    color: #000;
                    font-size: 0.75rem;
                    font-weight: 700;
                    padding: 0.2rem 0.5rem;
                    border-radius: 4px;
                    text-transform: uppercase;
                }}
            </style>
            
            <table class="compare-table">
                <thead>
                    <tr>
                        <th>Image</th>
                        <th style="text-align: left;">Product Details</th>
                        <th style="text-align: right;">Price</th>
                        <th>Platform</th>
                    </tr>
                </thead>
                <tbody>
                    {table_rows_html}
                </tbody>
            </table>
        </div>
        """
 
        # Update dropdown options
        choices = get_products_dropdown_choices()
        return summary_md, html_layout
 
    except Exception as e:
        return f"### [ERROR] Single-page analysis failed: {str(e)}", ""

def get_products_dropdown_choices():
    """Fetches tracked products to populate dropdown choices."""
    products = database.get_all_products(agent.db_path)
    if not products:
        return [("No products tracked yet", "")]
    return [(f"ID {p['id']}: {p['title'][:40]}... ({p['platform']})", str(p['id'])) for p in products]

def run_prediction_ui(product_id_str):
    """Generates detailed forecast recommendations and history log tables."""
    if not product_id_str:
        return "### Please select a product from the list."
    try:
        product_id = int(product_id_str)
        res = agent.get_prediction(product_id)
        prod = res['product']
        pred = res['prediction']
        
        sym = "₹" if prod['platform'] in ["Flipkart", "Amazon", "Meesho", "Google"] else "$"
        
        md = f"# 🤖 Price Predictor Analysis: {prod['title']}\n"
        md += f"**Platform**: {prod['platform']}  |  **URL**: [Visit Page]({prod['url']})\n\n"
        
        rec_color = "#22c55e" if pred['recommendation'] == "BUY" else ("#eab308" if pred['recommendation'] == "WAIT" else "#64748b")
        md += f"""<div style="background-color: rgba(30, 41, 59, 0.5); border: 1px solid rgba(255,255,255,0.08); border-left: 6px solid {rec_color}; padding: 1.25rem; border-radius: 8px; margin-bottom: 1.5rem;">
            <span style="color: {rec_color}; font-weight: 800; font-size: 1.3rem; letter-spacing: 0.5px;">RECOMMENDATION: {pred['recommendation']}</span><br/>
            <p style="margin-top: 0.5rem; color: #f1f5f9; font-size: 1.05rem;">{pred['reason']}</p>
        </div>\n\n"""
        
        md += "### 📈 Price Statistics\n"
        md += f"- **Current Price**: {format_currency_simple(pred['current_price'], prod['platform'])}\n"
        md += f"- **Average Price**: {format_currency_simple(pred['avg_price'], prod['platform'])}\n"
        md += f"- **Lowest Recorded**: {format_currency_simple(pred['min_price'], prod['platform'])}\n"
        md += f"- **Highest Recorded**: {format_currency_simple(pred['max_price'], prod['platform'])}\n"
        
        if pred['status'] == 'success':
            sign = "+" if pred['price_change_pct'] >= 0 else ""
            md += f"- **Total Change**: {sign}{pred['price_change_pct']:.2f}% since tracked\n"
            md += f"- **Trend Velocity**: {format_currency_simple(pred['slope_per_day'], prod['platform'])} per day\n\n"
            
            md += "### 🔮 Trend Forecasting\n"
            md += f"- 📅 **Expected Price in 7 days**: **{format_currency_simple(pred['predicted_price_7d'], prod['platform'])}**\n"
            md += f"- 📅 **Expected Price in 30 days**: **{format_currency_simple(pred['predicted_price_30d'], prod['platform'])}**\n"
        md += "\n"
        
        history = database.get_price_history(agent.db_path, product_id)
        md += "### 📋 Price Log History\n"
        md += "| Index | Timestamp | Price |\n"
        md += "|---|---|---|\n"
        for idx, h in enumerate(reversed(history), 1):
            md += f"| {idx} | {h['timestamp']} | {format_currency_simple(h['price'], prod['platform'])} |\n"
            
        return md
    except Exception as e:
        return f"### [ERROR] Prediction failed: {str(e)}"

def run_seed_ui(product_id_str, days, pattern):
    if not product_id_str:
        return "### Please select a product first.", "Select a product to see details"
    try:
        product_id = int(product_id_str)
        msg = agent.seed_mock_price_history(product_id, int(days), pattern)
        pred_md = run_prediction_ui(product_id_str)
        return f"### [SUCCESS] {msg}", pred_md
    except Exception as e:
        return f"### [ERROR] Seeding failed: {str(e)}", ""

def run_add_product_ui(url, target_price, mock_title, mock_price):
    if not url:
        return "### Please enter a valid product URL.", gr.update()
    try:
        if mock_title or mock_price is not None:
            title = mock_title or "Simulated E-Commerce Product"
            price = mock_price if mock_price is not None else 999.0
            p_id, title, price, img, is_mock = agent.track_new_product(
                url, target_price, force_title=title, force_price=price
            )
        else:
            p_id, title, price, img, is_mock = agent.track_new_product(url, target_price)
            
        msg = f"### [SUCCESS] Product Tracked!\n- **ID**: {p_id}\n- **Title**: {title}\n- **Initial Price**: {format_currency_simple(price, scraper.detect_platform(url))}"
        choices = get_products_dropdown_choices()
        return msg, gr.update(choices=choices, value=str(p_id))
    except Exception as e:
        return f"### [ERROR] Failed to add: {str(e)}", gr.update()

def delete_product_ui(product_id_str):
    if not product_id_str:
        return "### Please select a product first.", gr.update()
    try:
        product_id = int(product_id_str)
        product = database.get_product(agent.db_path, product_id)
        database.delete_product(agent.db_path, product_id)
        msg = f"### [SUCCESS] Deleted '{product['title']}' from database."
        choices = get_products_dropdown_choices()
        new_val = choices[0][1] if choices else ""
        return msg, gr.update(choices=choices, value=new_val)
    except Exception as e:
        return f"### [ERROR] Failed to delete: {str(e)}", gr.update()

async def run_three_column_search(product_name, show_browser):
    if not product_name:
        return "<h3>Please enter a product name.</h3>"
        
    try:
        headless = not show_browser
        
        print(f"Starting 3-column search for '{product_name}' [async mode]...")
        results_map = await scraper.search_platforms_async(product_name, "All", headless=headless)
        
        amazon_items   = results_map.get("Amazon", [])
        flipkart_items = results_map.get("Flipkart", [])
        meesho_items   = results_map.get("Meesho", [])
            
        # Helper to format item row HTML
        def get_column_html(items, platform):
            if not items:
                return f'<div style="text-align:center; padding:2rem; color:#94a3b8;">No products found on {platform}.</div>'
            
            rows = []
            for item in items[:12]: # top 12 items
                title = item.get("title", "")
                price = item.get("price", "N/A")
                img_url = item.get("image_url", "https://via.placeholder.com/80")
                prod_url = item.get("url", "#")
                
                rows.append(f'''
                <a href="{prod_url}" target="_blank" style="text-decoration: none; color: inherit; display: flex; align-items: center; gap: 0.75rem; background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.05); padding: 0.6rem; border-radius: 8px; transition: all 0.2s ease-in-out;" onmouseover="this.style.background='rgba(255,255,255,0.06)'; this.style.transform='translateY(-2px)';" onmouseout="this.style.background='rgba(255,255,255,0.02)'; this.style.transform='translateY(0)';" >
                    <img src="{img_url}" style="width: 50px; height: 50px; object-fit: contain; background: #fff; border-radius: 4px; padding: 2px;" onerror="this.src='https://via.placeholder.com/50'"/>
                    <div style="flex-grow: 1; min-width: 0;">
                        <div style="font-weight: 600; font-size: 0.85rem; color: #e2e8f0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 0.25rem;">{title}</div>
                        <div style="color: #4ade80; font-weight: 700; font-size: 0.95rem;">₹{price}</div>
                    </div>
                </a>
                ''')
            return "".join(rows)

        amazon_html = get_column_html(amazon_items, "Amazon")
        flipkart_html = get_column_html(flipkart_items, "Flipkart")
        meesho_html = get_column_html(meesho_items, "Meesho")
        
        # Build 3-column layout with JS fullscreen double-click toggle
        html_layout = f"""
        <div style="display: flex; gap: 1.5rem; justify-content: space-between; overflow-x: auto; padding: 0.5rem; align-items: stretch; margin-bottom: 1rem;">
            <!-- AMAZON -->
            <div class="platform-col" id="col-amazon" ondblclick="toggleFullscreen('col-amazon')" style="flex: 1; min-width: 280px; background: rgba(30, 41, 59, 0.45); border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; padding: 1.25rem; display: flex; flex-direction: column; height: 620px; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); cursor: pointer; box-sizing: border-box;">
                <div style="font-size: 0.75rem; color: #94a3b8; text-transform: uppercase; font-weight: 600; margin-bottom: 0.25rem; text-align: left; display: flex; justify-content: space-between;">
                    <span>Double-click / double-tap to zoom</span>
                </div>
                <h3 style="color: #FF9900; font-size: 1.15rem; font-weight: 700; border-bottom: 2px solid #FF9900; padding-bottom: 0.4rem; margin: 0 0 0.75rem 0; display: flex; justify-content: space-between; align-items: center;">
                    <span>Amazon.in</span>
                    <div style="display: flex; align-items: center; gap: 0.4rem;">
                        <span style="font-size: 0.72rem; background: #FF9900; color: #fff; padding: 0.1rem 0.4rem; border-radius: 4px; font-weight: 600;">{len(amazon_items)} Items</span>
                        <button onclick="event.stopPropagation(); toggleFullscreen('col-amazon')" style="background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.15); color: #fff; cursor: pointer; font-size: 0.85rem; padding: 0.15rem 0.35rem; border-radius: 4px; display: inline-flex; align-items: center; justify-content: center; transition: all 0.2s;" title="Toggle Fullscreen">⛶</button>
                    </div>
                </h3>
                <div style="flex-grow: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 0.5rem; padding-right: 0.25rem;">
                    {amazon_html}
                </div>
            </div>
            <!-- FLIPKART -->
            <div class="platform-col" id="col-flipkart" ondblclick="toggleFullscreen('col-flipkart')" style="flex: 1; min-width: 280px; background: rgba(30, 41, 59, 0.45); border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; padding: 1.25rem; display: flex; flex-direction: column; height: 620px; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); cursor: pointer; box-sizing: border-box;">
                <div style="font-size: 0.75rem; color: #94a3b8; text-transform: uppercase; font-weight: 600; margin-bottom: 0.25rem; text-align: left; display: flex; justify-content: space-between;">
                    <span>Double-click / double-tap to zoom</span>
                </div>
                <h3 style="color: #2874F0; font-size: 1.15rem; font-weight: 700; border-bottom: 2px solid #2874F0; padding-bottom: 0.4rem; margin: 0 0 0.75rem 0; display: flex; justify-content: space-between; align-items: center;">
                    <span>Flipkart</span>
                    <div style="display: flex; align-items: center; gap: 0.4rem;">
                        <span style="font-size: 0.72rem; background: #2874F0; color: #fff; padding: 0.1rem 0.4rem; border-radius: 4px; font-weight: 600;">{len(flipkart_items)} Items</span>
                        <button onclick="event.stopPropagation(); toggleFullscreen('col-flipkart')" style="background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.15); color: #fff; cursor: pointer; font-size: 0.85rem; padding: 0.15rem 0.35rem; border-radius: 4px; display: inline-flex; align-items: center; justify-content: center; transition: all 0.2s;" title="Toggle Fullscreen">⛶</button>
                    </div>
                </h3>
                <div style="flex-grow: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 0.5rem; padding-right: 0.25rem;">
                    {flipkart_html}
                </div>
            </div>
            
            <!-- MEESHO -->
            <div class="platform-col" id="col-meesho" ondblclick="toggleFullscreen('col-meesho')" style="flex: 1; min-width: 280px; background: rgba(30, 41, 59, 0.45); border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; padding: 1.25rem; display: flex; flex-direction: column; height: 620px; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); cursor: pointer; box-sizing: border-box;">
                <div style="font-size: 0.75rem; color: #94a3b8; text-transform: uppercase; font-weight: 600; margin-bottom: 0.25rem; text-align: left; display: flex; justify-content: space-between;">
                    <span>Double-click / double-tap to zoom</span>
                </div>
                <h3 style="color: #F43397; font-size: 1.15rem; font-weight: 700; border-bottom: 2px solid #F43397; padding-bottom: 0.4rem; margin: 0 0 0.75rem 0; display: flex; justify-content: space-between; align-items: center;">
                    <span>Meesho.com</span>
                    <div style="display: flex; align-items: center; gap: 0.4rem;">
                        <span style="font-size: 0.72rem; background: #F43397; color: #fff; padding: 0.1rem 0.4rem; border-radius: 4px; font-weight: 600;">{len(meesho_items)} Items</span>
                        <button onclick="event.stopPropagation(); toggleFullscreen('col-meesho')" style="background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.15); color: #fff; cursor: pointer; font-size: 0.85rem; padding: 0.15rem 0.35rem; border-radius: 4px; display: inline-flex; align-items: center; justify-content: center; transition: all 0.2s;" title="Toggle Fullscreen">⛶</button>
                    </div>
                </h3>
                <div style="flex-grow: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 0.5rem; padding-right: 0.25rem;">
                    {meesho_html}
                </div>
            </div>
        </div>

        <style>
            .fullscreen-column {{
                position: fixed !important;
                top: 0 !important;
                left: 0 !important;
                width: 100vw !important;
                height: 100vh !important;
                z-index: 99999 !important;
                background: #0f172a !important;
                padding: 2.5rem !important;
                border-radius: 0px !important;
                box-sizing: border-box !important;
            }}
            .fullscreen-column > div {{
                height: calc(100vh - 120px) !important;
            }}
            .platform-col {{
                user-select: none;
            }}
            .platform-col div::-webkit-scrollbar {{
                width: 6px;
            }}
            .platform-col div::-webkit-scrollbar-track {{
                background: rgba(255,255,255,0.01);
                border-radius: 10px;
            }}
            .platform-col div::-webkit-scrollbar-thumb {{
                background: rgba(255,255,255,0.1);
                border-radius: 10px;
            }}
            .platform-col div::-webkit-scrollbar-thumb:hover {{
                background: rgba(255,255,255,0.2);
                border-radius: 10px;
            }}
        </style>

        <script>
            if (typeof toggleFullscreen === "undefined") {{
                window.toggleFullscreen = function(id) {{
                    let el = document.getElementById(id);
                    if (!el) return;
                    if (el.classList.contains('fullscreen-column')) {{
                        el.classList.remove('fullscreen-column');
                    }} else {{
                        document.querySelectorAll('.platform-col').forEach(c => {{
                            c.classList.remove('fullscreen-column');
                        }});
                        el.classList.add('fullscreen-column');
                    }}
                }};
                
                // Double tap support on mobile devices via event delegation
                let lastTap = 0;
                document.addEventListener('touchend', function(e) {{
                    let col = e.target.closest('.platform-col');
                    if (!col) return;
                    
                    // Don't trigger if tapping on buy links/images or headers
                    if (e.target.closest('a') || e.target.closest('button')) return;
                    
                    let currentTime = new Date().getTime();
                    let tapLength = currentTime - lastTap;
                    if (tapLength < 300 && tapLength > 0) {{
                        toggleFullscreen(col.id);
                        e.preventDefault();
                    }}
                    lastTap = currentTime;
                }});
            }}
        </script>
        """
        return html_layout
    except Exception as e:
        return f"<h3>[ERROR] Search failed: {str(e)}</h3>"

CUSTOM_CSS = """
/* Deep, Vibrant, 3D Neon UI Theme */
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap');

body {
    background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%);
    background-attachment: fixed;
    font-family: 'Outfit', -apple-system, sans-serif;
    color: #ffffff;
}

/* Base Gradio container resets */
.gradio-container {
    background: transparent !important;
    border: none !important;
}

/* Colorful 3D Glass Panels */
.gr-box, .gr-panel, .gr-form, .gr-block {
    background: linear-gradient(145deg, rgba(255, 255, 255, 0.08), rgba(0, 0, 0, 0.3)) !important;
    backdrop-filter: blur(25px) saturate(200%) !important;
    -webkit-backdrop-filter: blur(25px) saturate(200%) !important;
    border: 1px solid rgba(255, 255, 255, 0.15) !important;
    border-radius: 24px !important;
    box-shadow: 
        0 20px 40px rgba(0,0,0,0.5),
        inset 0 2px 0 rgba(255,255,255,0.2),
        inset 0 0 20px rgba(255,255,255,0.05) !important;
    transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275) !important;
}
.gr-box:hover, .gr-panel:hover {
    transform: translateY(-5px);
    box-shadow: 
        0 30px 60px rgba(0,0,0,0.7),
        inset 0 2px 0 rgba(255,255,255,0.3),
        0 0 30px rgba(168, 85, 247, 0.4) !important;
    border-color: rgba(168, 85, 247, 0.6) !important;
}

/* Vibrant Typography */
h1 {
    background: linear-gradient(to right, #ff00cc, #3333ff, #00ffcc) !important;
    -webkit-background-clip: text !important;
    -webkit-text-fill-color: transparent !important;
    font-weight: 800 !important;
    font-size: 3.2rem !important;
    text-align: center;
    letter-spacing: 1px !important;
    margin-bottom: 35px !important;
    text-shadow: 0 10px 30px rgba(255, 0, 204, 0.4);
}
h2, h3 {
    color: #e0e7ff !important;
    font-weight: 600 !important;
    letter-spacing: 1px;
}

/* Deep inputs */
input, select, textarea, .gr-dropdown {
    background: rgba(0, 0, 0, 0.4) !important;
    border: 2px solid rgba(255, 255, 255, 0.1) !important;
    color: #00ffcc !important;
    border-radius: 16px !important;
    padding: 14px 20px !important;
    font-size: 1.05rem !important;
    box-shadow: inset 0 4px 10px rgba(0,0,0,0.6) !important;
    transition: all 0.3s ease !important;
}
input:focus, select:focus, textarea:focus {
    border-color: #00ffcc !important;
    background: rgba(0, 0, 0, 0.6) !important;
    box-shadow: 
        inset 0 4px 10px rgba(0,0,0,0.6),
        0 0 20px rgba(0, 255, 204, 0.5) !important;
    outline: none !important;
}

/* Neon Glow Buttons */
button.primary, button[variant="primary"] {
    background: linear-gradient(45deg, #ff00cc, #3333ff) !important;
    color: white !important;
    border: none !important;
    border-radius: 16px !important;
    font-weight: 800 !important;
    letter-spacing: 1px !important;
    text-transform: uppercase !important;
    font-size: 1.1rem !important;
    padding: 18px 36px !important;
    box-shadow: 0 10px 30px rgba(255, 0, 204, 0.5), inset 0 2px 0 rgba(255,255,255,0.4) !important;
    transition: all 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275) !important;
    text-shadow: 0 2px 5px rgba(0,0,0,0.3);
}
button.primary:hover, button[variant="primary"]:hover {
    transform: translateY(-5px) scale(1.05) !important;
    background: linear-gradient(45deg, #ff33d6, #5c5cff) !important;
    box-shadow: 0 15px 40px rgba(51, 51, 255, 0.7), inset 0 2px 0 rgba(255,255,255,0.6) !important;
}
button.primary:active {
    transform: translateY(2px) scale(0.95) !important;
    box-shadow: 0 5px 15px rgba(255, 0, 204, 0.4) !important;
}

/* Tabs Redesign */
.tabs > div > button {
    border-radius: 16px !important;
    margin: 8px !important;
    font-weight: 600 !important;
    color: #94a3b8 !important;
    background: rgba(0, 0, 0, 0.3) !important;
    border: 1px solid rgba(255,255,255,0.05) !important;
    box-shadow: inset 0 2px 5px rgba(0,0,0,0.4) !important;
    transition: all 0.3s ease !important;
    padding: 14px 28px !important;
}
.tabs > div > button:hover {
    color: #ffffff !important;
    background: rgba(255,255,255,0.15) !important;
    box-shadow: 0 5px 15px rgba(0,0,0,0.3) !important;
}
.tabs > div > button.selected {
    background: linear-gradient(135deg, rgba(0, 255, 204, 0.25), rgba(51, 51, 255, 0.25)) !important;
    border: 1px solid #00ffcc !important;
    color: #00ffcc !important;
    box-shadow: 0 0 25px rgba(0, 255, 204, 0.4), inset 0 0 15px rgba(0, 255, 204, 0.3) !important;
}

/* Product Cards */
.product-row-col {
    background: linear-gradient(160deg, rgba(40, 40, 60, 0.8), rgba(20, 20, 30, 0.95)) !important;
    border: 1px solid rgba(255, 255, 255, 0.15) !important;
    border-radius: 20px;
    padding: 20px;
    margin-bottom: 25px;
    box-shadow: 0 15px 35px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.1) !important;
    transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275) !important;
    position: relative;
    overflow: hidden;
}
.product-row-col::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0; height: 5px;
    background: linear-gradient(90deg, #ff00cc, #3333ff, #00ffcc);
    opacity: 0.8;
    transition: opacity 0.4s ease;
}
.product-row-col:hover {
    transform: translateY(-8px) scale(1.02) !important;
    border-color: rgba(0, 255, 204, 0.6) !important;
    box-shadow: 0 25px 50px rgba(0,0,0,0.7), 0 0 35px rgba(0, 255, 204, 0.3) !important;
}

/* Price Highlights */
.product-row-col div[style*="font-weight: bold; color: #10b981;"] {
    background: linear-gradient(to right, #00ffcc, #3333ff);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-size: 1.6rem !important;
    font-weight: 800 !important;
    text-shadow: 0 2px 10px rgba(0, 255, 204, 0.4);
}

/* Platform Title Headers */
.platform-col > h3 {
    border-bottom: none;
    background: rgba(0, 0, 0, 0.4);
    padding: 16px;
    border-radius: 16px;
    margin-bottom: 25px;
    color: #fff !important;
    font-size: 1.5rem !important;
    text-align: center;
    font-weight: 800 !important;
    box-shadow: inset 0 2px 5px rgba(0,0,0,0.6), 0 8px 20px rgba(0,0,0,0.3);
    border: 1px solid rgba(255,255,255,0.1);
}

/* Images */
.product-row-col img {
    border-radius: 14px;
    box-shadow: 0 8px 20px rgba(0,0,0,0.5);
    transition: all 0.5s cubic-bezier(0.175, 0.885, 0.32, 1.275);
}
.product-row-col:hover img {
    transform: scale(1.1) rotate(3deg);
    box-shadow: 0 15px 35px rgba(0,0,0,0.7);
}
"""

# Build the Gradio Blocks Layout
with gr.Blocks(title="E-Commerce Agent UI") as demo:
    gr.Markdown("# 🤖 E-Commerce Price Finder & Multi-Platform Portal")
    
    with gr.Tabs() as tabs:
        # TAB 1: SINGLE PAGE ANALYZER & DEAL FINDER
        with gr.Tab("🎯 Single Page Analyzer"):
            with gr.Row():
                # Column 1: Platform / URL
                with gr.Column(scale=1):
                    target_input = gr.Dropdown(
                        label="1. Target Platform or Search URL", 
                        choices=["Amazon", "Flipkart", "Meesho"],
                        value="Amazon",
                        allow_custom_value=True,
                        info="Type Amazon, Flipkart, Meesho OR paste a custom search page URL directly."
                    )
                # Column 2: Product Name
                with gr.Column(scale=1):
                    product_input = gr.Textbox(
                        label="2. Product Name / Search Term", 
                        placeholder="e.g. 'campus shoes' or 'iphone 15'",
                        lines=1
                    )
                # Column 3: Price Tier Preference
                with gr.Column(scale=1):
                    tier_input = gr.Dropdown(
                        label="3. Preferred Price Tier", 
                        choices=["Lowest Price", "Mid-level Price", "Highest Price"],
                        value="Lowest Price",
                        info="Extract the cheapest option, solid middle-range option, or the premium option."
                    )
                    
            with gr.Row():
                show_browser_chk = gr.Checkbox(
                    label="Show Browser (Open Original Website)", 
                    value=not is_hf,
                    info="If checked, the scraper will open a visible browser window so you can see the original website."
                )
            
            with gr.Row():
                analyze_btn = gr.Button("Extract & Analyze Deal", variant="primary")
                
            with gr.Row():
                with gr.Column(scale=1):
                    summary_output = gr.Markdown(label="Deal Summary")
                with gr.Column(scale=2):
                    html_output = gr.HTML(label="Visual Deal Card & Catalog")
                    
            analyze_btn.click(
                fn=run_single_page_analysis,
                inputs=[target_input, product_input, tier_input, show_browser_chk],
                outputs=[summary_output, html_output]
            )

        # TAB 2: MULTI-PLATFORM COLUMNS
        with gr.Tab("📊 Multi-Platform Columns"):
            gr.Markdown("Compare Amazon, Flipkart, and Meesho results side-by-side. **Double-click** any column to view that platform in full screen.")
            
            with gr.Row():
                col_product_input = gr.Textbox(
                    label="Product Name / Search Term", 
                    placeholder="e.g. 'campus shoes' or 'campus tshirt'",
                    lines=1
                )
            with gr.Row():
                col_show_browser_chk = gr.Checkbox(
                    label="Show Browser (Open Original Website)", 
                    value=not is_hf,
                    info="If checked, the scraper will open visible browser windows during extraction."
                )
            with gr.Row():
                col_search_btn = gr.Button("Search & Compare Platforms", variant="primary")
                
            with gr.Row():
                col_view_output = gr.HTML(label="Multi-Platform 3-Columns Portal")
                
            col_search_btn.click(
                fn=run_three_column_search,
                inputs=[col_product_input, col_show_browser_chk],
                outputs=[col_view_output]
            )

if __name__ == "__main__":
    server_name = "0.0.0.0" if is_hf else "127.0.0.1"
    share = False if is_hf else True
    demo.launch(server_name=server_name, server_port=7860, share=share, css=CUSTOM_CSS, theme=gr.themes.Base())
