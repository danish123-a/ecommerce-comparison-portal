import re
import json
import time
import hashlib
import urllib.parse
import requests
import asyncio
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Simple TTL result cache (avoids re-scraping the same query within 5 min) ──
_CACHE = {}          # key -> (timestamp, results)
_CACHE_TTL = 300     # seconds (5 minutes)

def _cache_key(fn_name, query, headless):
    raw = f"{fn_name}:{query.strip().lower()}:{headless}"
    return hashlib.md5(raw.encode()).hexdigest()

def _cache_get(key):
    entry = _CACHE.get(key)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None

def _cache_set(key, value):
    _CACHE[key] = (time.time(), value)

# A collection of random user agents to rotate and prevent quick blocking
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
]

def get_headers():
    """Generates headers simulating a real browser request."""
    import random
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "DNT": "1"
    }

COUPON_EXTRACTOR_JS = """() => {
    let coupons = [];
    
    function isExpired(text) {
        let textLower = text.toLowerCase();
        if (textLower.includes("expired") || textLower.includes("ended") || textLower.includes("invalid")) {
            return true;
        }
        return false;
    }
    
    // Amazon coupons
    let amazonCoupons = document.querySelectorAll('#couponBadge, #applicablePromotionListId, .pct-coupons, [id*="coupon"], [class*="coupon"]');
    for (let el of amazonCoupons) {
        let text = el.innerText.trim();
        if (text && text.length > 5 && text.length < 200 && !isExpired(text)) {
            let m = text.match(/[A-Z0-9]{5,10}/);
            let code = m ? m[0] : "APPLY_ON_CHECKOUT";
            coupons.push({
                code: code,
                description: text.replace(/\\n/g, " "),
                source: "Amazon Coupon"
            });
        }
    }
    
    // Flipkart offers
    let flipkartOffers = document.querySelectorAll('li.yN+eNk, li.available-offers, div.available-offers, ._3TT44I');
    for (let el of flipkartOffers) {
        let text = el.innerText.trim();
        if (text && text.length > 5 && text.length < 200 && !isExpired(text)) {
            let m = text.match(/code\\s+([A-Z0-9]{4,10})/i);
            let code = m ? m[1] : "T&C APPLY";
            coupons.push({
                code: code,
                description: text.replace(/\\n/g, " "),
                source: "Flipkart Offer"
            });
        }
    }
    
    // General offers
    let generalSelectors = [
        '[class*="offer"]', '[class*="coupon"]', '[class*="promo"]', '[class*="voucher"]',
        '[id*="offer"]', '[id*="coupon"]', '[id*="promo"]', '[id*="voucher"]'
    ];
    let seenTexts = new Set();
    for (let selector of generalSelectors) {
        let elements = document.querySelectorAll(selector);
        for (let el of elements) {
            if (el.children.length === 0 || (el.children.length < 3 && el.innerText.length < 150)) {
                let text = el.innerText.trim();
                if (text && text.length > 10 && text.length < 150 && !isExpired(text)) {
                    if (text.match(/\\d+%\\s*off/i) || text.match(/save\\s*[₹$]\\d+/i) || text.match(/coupon/i) || text.match(/promo/i)) {
                        if (!seenTexts.has(text)) {
                            seenTexts.add(text);
                            let codeMatch = text.match(/code\\s*:\\s*([A-Z0-9]{4,12})/i) || text.match(/\\b([A-Z0-9]{5,12})\\b/);
                            let code = codeMatch ? codeMatch[1] : "AUTO_APPLY";
                            coupons.push({
                                code: code,
                                description: text.replace(/\\n/g, " "),
                                source: "Promo Code/Offer"
                            });
                        }
                    }
                }
            }
        }
    }
    
    // Remove duplicates
    let uniqueCoupons = [];
    let uniqueKeys = new Set();
    for (let c of coupons) {
        let key = (c.code + c.description).toLowerCase();
        if (!uniqueKeys.has(key)) {
            uniqueKeys.add(key);
            uniqueCoupons.push(c);
        }
    }
    
    return uniqueCoupons.slice(0, 5);
}"""

def detect_platform(url):
    """Detects the e-commerce platform from the URL."""
    url_lower = url.lower()
    if "amazon" in url_lower:
        return "Amazon"
    elif "flipkart" in url_lower:
        return "Flipkart"
    elif "myntra" in url_lower:
        return "Myntra"
    else:
        return "Generic"

def clean_price(price_str):
    """Cleans a price string and converts it to a float."""
    if not price_str:
        return None
    # Remove currency symbols ($, ₹, etc.), commas, spaces, and other non-numeric text
    price_str = price_str.replace(",", "").replace("₹", "").replace("$", "").strip()
    match = re.search(r"\d+(\.\d+)?", price_str)
    if match:
        return float(match.group())
    return None

def extract_from_json_ld(soup):
    """Attempts to extract price, title, and image from structured JSON-LD schema blocks."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            
            # Check if this is a Product or contains a Product graph
            products = []
            if isinstance(data, dict):
                if data.get("@type") == "Product":
                    products.append(data)
                elif "@graph" in data:
                    for item in data["@graph"]:
                        if item.get("@type") == "Product":
                            products.append(item)
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        products.append(item)
            
            for product in products:
                title = product.get("name")
                offers = product.get("offers")
                image = product.get("image")
                
                # Parse image URL from JSON-LD
                image_url = None
                if image:
                    if isinstance(image, list) and image:
                        image_url = image[0]
                    elif isinstance(image, dict):
                        image_url = image.get("url")
                    else:
                        image_url = image
                
                price = None
                if offers:
                    if isinstance(offers, dict):
                        price = offers.get("price")
                    elif isinstance(offers, list):
                        prices = [o.get("price") for o in offers if o.get("price")]
                        if prices:
                            price = min(prices)
                
                if title and (price or image_url):
                    return str(title).strip(), clean_price(str(price)) if price else None, str(image_url) if image_url else None
        except Exception:
            continue
    return None, None, None

def scrape_amazon(soup):
    """Scrapes Amazon product details (title, price, image) from parsed HTML soup."""
    # Title
    title = None
    title_element = soup.select_one("#productTitle")
    if title_element:
        title = title_element.get_text().strip()
    
    # Price
    price = None
    price_selectors = [
        "span.a-price span.a-offscreen",
        "span#priceblock_ourprice",
        "span#priceblock_dealprice",
        "span.a-price-whole",
        "span#price_inside_buybox",
        "#priceblock_saleprice"
    ]
    for selector in price_selectors:
        elem = soup.select_one(selector)
        if elem:
            price = clean_price(elem.get_text())
            if price:
                break
                
    # Image
    image_url = None
    image_selectors = [
        "img#landingImage",
        "img#imgBlkFront",
        "img#main-image",
        "div#imgTagWrapperId img",
        "div#main-image-container img"
    ]
    for selector in image_selectors:
        elem = soup.select_one(selector)
        if elem:
            # Check for standard src or lazy loaded dynamic image attributes
            image_url = elem.get("data-old-hires") or elem.get("src")
            if elem.get("data-a-dynamic-image"):
                try:
                    # Amazon stores sizes as keys in a JSON string
                    dyn_images = json.loads(elem.get("data-a-dynamic-image"))
                    if dyn_images:
                        image_url = list(dyn_images.keys())[0]
                except Exception:
                    pass
            if image_url and not image_url.startswith("data:image"):
                break
                
    return title, price, image_url

def scrape_flipkart(soup):
    """Scrapes Flipkart product details (title, price, image) from parsed HTML soup."""
    # Title
    title = None
    title_selectors = ["span.B_NuCI", "h1.title", "span.VU-ZEz", "h1 span"]
    for sel in title_selectors:
        elem = soup.select_one(sel)
        if elem:
            title = elem.get_text().strip()
            break
            
    # Price
    price = None
    price_selectors = [
        "div._30jeq3._16Jk6d",
        "div.Nx931e",
        "div._30jeq3",
        "span.y30bFC"
    ]
    for selector in price_selectors:
        elem = soup.select_one(selector)
        if elem:
            price = clean_price(elem.get_text())
            if price:
                break
    if not price:
        meta_price = soup.find("meta", itemprop="price")
        if meta_price:
            price = clean_price(meta_price.get("content"))
            
    # Image
    image_url = None
    image_selectors = [
        "img._396csi",
        "img.DByoEF",
        "img._2r_V1b",
        "div._35KyD6 img",
        "div._2cM5cM img"
    ]
    for selector in image_selectors:
        elem = soup.select_one(selector)
        if elem:
            image_url = elem.get("src")
            if image_url and not image_url.startswith("data:image"):
                break
                
    return title, price, image_url

def scrape_generic(soup):
    """Fallback scraping for title, price, and image using standard meta tags."""
    title = None
    title_tags = [
        ("meta", {"property": "og:title"}),
        ("meta", {"name": "twitter:title"}),
        ("title", {})
    ]
    for tag, attrs in title_tags:
        elem = soup.find(tag, attrs)
        if elem:
            if tag == "title":
                title = elem.get_text()
            else:
                title = elem.get("content")
            if title:
                title = title.strip()
                break
                
    price = None
    price_tags = [
        ("meta", {"property": "og:price:amount"}),
        ("meta", {"property": "product:price:amount"}),
        ("meta", {"itemprop": "price"})
    ]
    for tag, attrs in price_tags:
        elem = soup.find(tag, attrs)
        if elem:
            price = clean_price(elem.get("content"))
            if price:
                break
                
    image_url = None
    image_tags = [
        ("meta", {"property": "og:image"}),
        ("meta", {"name": "twitter:image"}),
        ("link", {"rel": "image_src"})
    ]
    for tag, attrs in image_tags:
        elem = soup.find(tag, attrs)
        if elem:
            if tag == "link":
                image_url = elem.get("href")
            else:
                image_url = elem.get("content")
            if image_url:
                break
                
    return title, price, image_url

def scrape_url(url, headless=True):
    """
    Main function to fetch a URL and extract product title, price, and image.
    Returns: (title, price, platform, image_url, error_message)
    """
    platform = detect_platform(url)
    try:
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800}
                )
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1000)
                html_content = page.content()
                soup = BeautifulSoup(html_content, "html.parser")
                coupons = page.evaluate(COUPON_EXTRACTOR_JS)
                browser.close()
        except Exception as e:
            print(f"[WARNING] Playwright single page scrape failed: {str(e)}. Falling back to requests.")
            response = requests.get(url, headers=get_headers(), timeout=15)
            if response.status_code != 200:
                return None, None, platform, None, f"HTTP Error {response.status_code}"
            soup = BeautifulSoup(response.content, "html.parser")
            coupons = []
            
        # 1. Try JSON-LD first
        title, price, image_url = extract_from_json_ld(soup)
        
        # 2. Try platform-specific parsing
        if not title or not price or not image_url:
            if platform == "Amazon":
                p_title, p_price, p_image = scrape_amazon(soup)
            elif platform == "Flipkart":
                p_title, p_price, p_image = scrape_flipkart(soup)
            else:
                p_title, p_price, p_image = scrape_generic(soup)
                
            title = title or p_title
            price = price or p_price
            image_url = image_url or p_image
            
        # 3. Fallback
        if not title or not price or not image_url:
            f_title, f_price, f_image = scrape_generic(soup)
            title = title or f_title
            price = price or f_price
            image_url = image_url or f_image
            
        # Standard placeholder image if missing
        if not image_url:
            image_url = "https://images.unsplash.com/photo-1531403009284-440f080d1e12?w=500" # generic mockup tech
            
        if not title:
            return None, None, platform, None, "Could not extract product title"
        if not price:
            return title, None, platform, image_url, "Could not extract price"
            
        return title, price, platform, image_url, coupons, None
        
    except Exception as e:
        return None, None, platform, None, f"Unexpected error during scraping: {str(e)}"

# ==============================================================================
# Search Scrapers & Mock Data Generation
# ==============================================================================

def get_mock_search_results(query, target_platform="All"):
    """
    Generates high-quality mock search results when live scraping is blocked or rate-limited.
    Contains 10 items per platform spanning a wide price distribution (lowest, mid-level, highest).
    """
    query_lower = query.lower()
    
    # 1. Determine Product Categories and seed lists
    if "shoe" in query_lower or "boot" in query_lower or "sneaker" in query_lower or "footwear" in query_lower:
        category = "shoes"
        brand = "Campus"
        if "puma" in query_lower: brand = "Puma"
        elif "adidas" in query_lower: brand = "Adidas"
        elif "nike" in query_lower: brand = "Nike"
        
        # 10 items aligned with high-quality shoe and accessory images
        titles = [
            f"{brand} Comfort Cushioned Slides & Slippers",
            f"{brand} Men's Casual Canvas Slip-on Shoes",
            f"{brand} Men's Running & Walking Shoes - Lightweight Athletic Sneakers",
            f"{brand} Active Sports Gym Training Shoes for Men",
            f"{brand} Men's Comfort Lifestyle Sneaker Shoes",
            f"{brand} Outdoor Sport Hiking & Trekking Shoes For Men",
            f"{brand} Breathable Premium Cushioned Running Shoes",
            f"{brand} Premium Leather Designer Lifestyle Sneakers",
            f"{brand} Flagship Pro Racing Carbon-Plate Running Shoes",
            f"{brand} Sport Shoe Laces & Cleaning Kit Pack"
        ]
        prices = [499.0, 799.0, 1499.0, 1899.0, 2499.0, 3299.0, 4999.0, 8999.0, 14999.0, 199.0]
        images = [
            "https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=500",  # Running shoe - orange
            "https://images.unsplash.com/photo-1549298916-b41d501d3772?w=500",  # Canvas shoe - white
            "https://images.unsplash.com/photo-1606107557195-0e29a4b5b4aa?w=500",  # Running shoes - side
            "https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?w=500",  # Sports shoes
            "https://images.unsplash.com/photo-1515955656352-a1fa3ffcd111?w=500",  # Sneakers blue
            "https://images.unsplash.com/photo-1521093470119-a3acdc43374a?w=500",  # Hiking shoes
            "https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=500",  # Premium running
            "https://images.unsplash.com/photo-1491553895911-0055eca6402d?w=500",  # Designer sneaker
            "https://images.unsplash.com/photo-1606107557195-0e29a4b5b4aa?w=500",  # Carbon racing shoe
            "https://images.unsplash.com/photo-1607522370275-f14206abe5d3?w=500"   # Shoe care/cleaning kit
        ]
        urls = {
            # Amazon: Use search URLs matching the exact displayed title to ensure
            # clicking the image shows the same product (no dp/ mismatch bias)
            "Amazon": [
                f"https://www.amazon.in/s?k={brand}+Comfort+Cushioned+Slides+Slippers",
                f"https://www.amazon.in/s?k={brand}+Men+Casual+Canvas+Slip-on+Shoes",
                f"https://www.amazon.in/s?k={brand}+Men+Running+Walking+Shoes",
                f"https://www.amazon.in/s?k={brand}+Active+Sports+Gym+Training+Shoes",
                f"https://www.amazon.in/s?k={brand}+Men+Comfort+Lifestyle+Sneaker",
                f"https://www.amazon.in/s?k={brand}+Outdoor+Sport+Hiking+Trekking+Shoes",
                f"https://www.amazon.in/s?k={brand}+Breathable+Premium+Cushioned+Running+Shoes",
                f"https://www.amazon.in/s?k={brand}+Premium+Leather+Designer+Lifestyle+Sneakers",
                f"https://www.amazon.in/s?k={brand}+Flagship+Pro+Racing+Carbon+Running+Shoes",
                f"https://www.amazon.in/s?k={brand}+Sport+Shoe+Laces+Cleaning+Kit"
            ],
            "Flipkart": [
                "https://www.flipkart.com/search?q=campus+slides+slippers+men",
                "https://www.flipkart.com/search?q=campus+oxyfit+canvas+shoes+men",
                "https://www.flipkart.com/search?q=campus+first+running+shoes+men",
                "https://www.flipkart.com/search?q=campus+gym+training+shoes+men",
                "https://www.flipkart.com/search?q=campus+lifestyle+sneaker+men",
                "https://www.flipkart.com/search?q=campus+hiking+trekking+shoes+men",
                "https://www.flipkart.com/search?q=campus+premium+cushioned+running+shoes",
                "https://www.flipkart.com/search?q=campus+leather+designer+sneakers",
                "https://www.flipkart.com/search?q=campus+carbon+plate+racing+running+shoes",
                "https://www.flipkart.com/search?q=campus+shoe+laces+cleaning+kit"
            ],
            # Myntra: Real direct product URLs and real images to guarantee "exact direct link matching UI image"
            "Myntra": [
                {
                    "title": "Puma Men Black Flyer Flex Running Shoes",
                    "price": 2749.0,
                    "url": "https://www.myntra.com/sports-shoes/puma/puma-men-black-flyer-flex-running-shoes/15228186/buy",
                    "image_url": "https://assets.myntrasassets.com/dpr_1.5,q_60,w_400,c_limit,fl_progressive/assets/images/15228186/2021/8/24/7eb0b5f1-39a0-4384-93e5-829d2f2d91961629807532386-Puma-Men-Black-Flyer-Flex-Running-Shoes-6261629807531778-1.jpg"
                },
                {
                    "title": "HRX by Hrithik Roshan Men White Sneakers",
                    "price": 1499.0,
                    "url": "https://www.myntra.com/casual-shoes/hrx-by-hrithik-roshan/hrx-by-hrithik-roshan-men-white-sneakers/2275365/buy",
                    "image_url": "https://assets.myntrasassets.com/dpr_1.5,q_60,w_400,c_limit,fl_progressive/assets/images/2275365/2019/9/10/ce4696c2-475a-4e2b-a01f-0b0451e06e001568108711467-HRX-by-Hrithik-Roshan-Men-White-Sneakers-8491568108710292-1.jpg"
                },
                {
                    "title": "Nike Men Black Revolution 6 Running Shoes",
                    "price": 3695.0,
                    "url": "https://www.myntra.com/sports-shoes/nike/nike-men-black-revolution-6-running-shoes/15446522/buy",
                    "image_url": "https://assets.myntrasassets.com/dpr_1.5,q_60,w_400,c_limit,fl_progressive/assets/images/15446522/2021/9/22/e1b6f001-f1eb-4ce7-8e62-c1ec5a4b73251632289454157-Nike-Men-Black-Revolution-6-Running-Shoes-9101632289453715-1.jpg"
                },
                {
                    "title": "Campus Men Black Mesh Running Shoes",
                    "price": 999.0,
                    "url": "https://www.myntra.com/sports-shoes/campus/campus-men-black-mesh-running-shoes/14294022/buy",
                    "image_url": "https://assets.myntrasassets.com/dpr_1.5,q_60,w_400,c_limit,fl_progressive/assets/images/14294022/2021/5/11/4eb5a538-cdbb-47db-806f-652a90ee9ed31620719818816-Campus-Men-Black-Mesh-Running-Shoes-9921620719818408-1.jpg"
                },
                {
                    "title": "Red Tape Men White Walking Shoes",
                    "price": 1649.0,
                    "url": "https://www.myntra.com/sports-shoes/red-tape/red-tape-men-white-walking-shoes/14589990/buy",
                    "image_url": "https://assets.myntrasassets.com/dpr_1.5,q_60,w_400,c_limit,fl_progressive/assets/images/14589990/2021/6/22/20058b73-a26b-4e6f-9989-1065c71101881624354228935RedTapeMenWhiteWalkingShoes1.jpg"
                }
            ],
            "Google": [
                f"https://www.google.co.in/search?q={brand}+Comfort+Cushioned+Slides+buy",
                f"https://www.google.co.in/search?q={brand}+Canvas+Slip-on+Shoes+buy",
                f"https://www.google.co.in/search?q={brand}+Running+Walking+Shoes+buy",
                f"https://www.google.co.in/search?q={brand}+Sports+Gym+Training+Shoes+buy",
                f"https://www.google.co.in/search?q={brand}+Lifestyle+Sneaker+buy",
                f"https://www.google.co.in/search?q={brand}+Hiking+Trekking+Shoes+buy",
                f"https://www.google.co.in/search?q={brand}+Premium+Cushioned+Running+Shoes+buy",
                f"https://www.google.co.in/search?q={brand}+Leather+Designer+Sneakers+buy",
                f"https://www.google.co.in/search?q={brand}+Carbon+Plate+Racing+Shoes+buy",
                f"https://www.google.co.in/search?q={brand}+Shoe+Laces+Cleaning+Kit+buy"
            ]
        }
        
    elif "cloth" in query_lower or "shirt" in query_lower or "tshirt" in query_lower or "pant" in query_lower or "jean" in query_lower or "jacket" in query_lower:
        category = "clothes"
        brand = "Zara"
        if "h&m" in query_lower or "hm" in query_lower: brand = "H&M"
        elif "levis" in query_lower or "levi" in query_lower: brand = "Levi's"
        
        # 10 clothes items aligned with matching fashion images
        titles = [
            f"{brand} Solid Crew Neck Sports Fit T-Shirt",
            f"{brand} Printed Casual Round Neck Summer Tee",
            f"{brand} Casual Regular Spread Collar Shirt For Men",
            f"{brand} Premium Cotton Slim Fit Casual Shirt for Men",
            f"{brand} Comfort Stretch Denim Jeans For Men",
            f"{brand} Classic Regular Fit Cotton Denim Jeans",
            f"{brand} Men's Typography Print Hooded Jacket",
            f"{brand} Premium Wool-Blend Single Breasted Blazer",
            f"{brand} Handcrafted Designer Winter Trench Coat",
            f"{brand} Pure Cotton Ankle Socks (Pack of 3)"
        ]
        prices = [499.0, 799.0, 1299.0, 1899.0, 2499.0, 2999.0, 3999.0, 6999.0, 11999.0, 149.0]
        images = [
            "https://images.unsplash.com/photo-1521572267360-ee0c2909d518?w=500", # Crew neck t-shirt (red)
            "https://images.unsplash.com/photo-1583743814966-8936f5b7be1a?w=500", # Printed tee (black)
            "https://images.unsplash.com/photo-1596755094514-f87e34085b2c?w=500", # Spread collar shirt
            "https://images.unsplash.com/photo-1596755094514-f87e34085b2c?w=500", # Slim fit shirt
            "https://images.unsplash.com/photo-1541099649105-f69ad21f3246?w=500", # Denim jeans
            "https://images.unsplash.com/photo-1541099649105-f69ad21f3246?w=500", # Classic jeans
            "https://images.unsplash.com/photo-1551028719-00167b16eac5?w=500", # Hooded jacket
            "https://images.unsplash.com/photo-1593032465175-481ac7f401a0?w=500", # Blazer
            "https://images.unsplash.com/photo-1591047139829-d91aecb6caea?w=500", # Trench coat
            "https://m.media-amazon.com/images/I/81dG2+nZ41L._UL1500_.jpg"  # Pure cotton ankle socks
        ]
        urls = {
            "Amazon": [
                "https://www.amazon.in/dp/B09V7D3G13",
                "https://www.amazon.in/dp/B09V7D3G13",
                "https://www.amazon.in/s?k=Zara+Collar+Shirt",
                "https://www.amazon.in/s?k=Zara+Slim+Shirt",
                "https://www.amazon.in/s?k=Zara+Denim+Jeans",
                "https://www.amazon.in/s?k=Zara+Classic+Jeans",
                "https://www.amazon.in/s?k=Zara+Hooded+Jacket",
                "https://www.amazon.in/s?k=Zara+Blazer",
                "https://www.amazon.in/s?k=Zara+Trench+Coat",
                "https://www.amazon.in/s?k=Zara+Socks"
            ],
            "Flipkart": [
                "https://www.flipkart.com/search?q=Zara+T-shirt",
                "https://www.flipkart.com/search?q=Zara+Printed+T-shirt",
                "https://www.flipkart.com/search?q=Zara+Collar+Shirt",
                "https://www.flipkart.com/search?q=Zara+Slim+Shirt",
                "https://www.flipkart.com/search?q=Zara+Jeans",
                "https://www.flipkart.com/search?q=Zara+Classic+Jeans",
                "https://www.flipkart.com/search?q=Zara+Jacket",
                "https://www.flipkart.com/search?q=Zara+Blazer",
                "https://www.flipkart.com/search?q=Zara+Trench+Coat",
                "https://www.flipkart.com/search?q=Zara+Socks"
            ],
            "Myntra": [
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}"
            ],
            "Google": [
                "https://www.google.co.in/search?q=Zara+T-shirt",
                "https://www.google.co.in/search?q=Zara+Printed+T-shirt",
                "https://www.google.co.in/search?q=Zara+Collar+Shirt",
                "https://www.google.co.in/search?q=Zara+Slim+Shirt",
                "https://www.google.co.in/search?q=Zara+Jeans",
                "https://www.google.co.in/search?q=Zara+Classic+Jeans",
                "https://www.google.co.in/search?q=Zara+Jacket",
                "https://www.google.co.in/search?q=Zara+Blazer",
                "https://www.google.co.in/search?q=Zara+Trench+Coat",
                "https://www.google.co.in/search?q=Zara+Socks"
            ]
        }
        
    elif "phone" in query_lower or "mobile" in query_lower or "iphone" in query_lower or "samsung" in query_lower:
        category = "phones"
        brand = "Apple"
        if "samsung" in query_lower: brand = "Samsung"
        elif "oneplus" in query_lower: brand = "OnePlus"
        
        # 10 phones and accessories aligned with matching hardware images
        titles = [
            f"{brand} Lite Budget friendly Phone (64GB ROM)",
            f"{brand} Mid-Tier Smartphone (5G, 128GB ROM)",
            f"{brand} Flagship Smartphone (5G, 128GB, Premium Camera)",
            f"{brand} Smart Phone (Deep Black, 256 GB)",
            f"{brand} flagship phone (Ocean Blue, 128 GB)",
            f"{brand} Pro Edition Mobile Phone - Ultra Fast Display",
            f"{brand} Pro Max flagship Smartphone (5G, 1TB Storage)",
            f"{brand} USB-C Fast Charger Cable (1m)",
            f"{brand} Premium Liquid Silicone Protective Case",
            f"{brand} Dual Port 35W Fast Charging Adapter"
        ]
        prices = [9999.0, 18999.0, 34999.0, 54999.0, 77999.0, 115999.0, 149999.0, 299.0, 599.0, 999.0]
        images = [
            "https://images.unsplash.com/photo-1511707171634-5f897ff02aa9?w=500", # Budget phone
            "https://images.unsplash.com/photo-1598327105666-5b89351aff97?w=500", # Mid-tier phone
            "https://images.unsplash.com/photo-1565630916779-e303be97b6f5?w=500", # Flagship phone
            "https://images.unsplash.com/photo-1511707171634-5f897ff02aa9?w=500", # Black phone
            "https://images.unsplash.com/photo-1598327105666-5b89351aff97?w=500", # Blue phone
            "https://images.unsplash.com/photo-1565630916779-e303be97b6f5?w=500", # Pro edition
            "https://images.unsplash.com/photo-1511707171634-5f897ff02aa9?w=500", # Pro max phone
            "https://m.media-amazon.com/images/I/61+QZ-E9FPL._SL1500_.jpg", # USB-C cable
            "https://m.media-amazon.com/images/I/61z-v72LFFL._SL1500_.jpg", # Silicone case
            "https://m.media-amazon.com/images/I/51+2+aJ2Q2L._SL1500_.jpg"  # Fast charging adapter
        ]
        urls = {
            "Amazon": [
                "https://www.amazon.in/s?k=Apple+Budget+Phone",
                "https://www.amazon.in/s?k=Apple+Mid-Tier+Phone",
                "https://www.amazon.in/s?k=Apple+Flagship+Phone",
                "https://www.amazon.in/s?k=Apple+Phone+Black",
                "https://www.amazon.in/s?k=Apple+Phone+Blue",
                "https://www.amazon.in/s?k=Apple+Pro+Phone",
                "https://www.amazon.in/s?k=Apple+Pro+Max+Phone",
                "https://www.amazon.in/dp/B0BY8MCQ9S",
                "https://www.amazon.in/dp/B0BY8MCQ9S",
                "https://www.amazon.in/dp/B0BY8MCQ9S"
            ],
            "Flipkart": [
                "https://www.flipkart.com/search?q=Apple+Budget+Phone",
                "https://www.flipkart.com/search?q=Apple+Mid-Tier+Phone",
                "https://www.flipkart.com/search?q=Apple+Flagship+Phone",
                "https://www.flipkart.com/search?q=Apple+Phone+Black",
                "https://www.flipkart.com/search?q=Apple+Phone+Blue",
                "https://www.flipkart.com/search?q=Apple+Pro+Phone",
                "https://www.flipkart.com/search?q=Apple+Pro+Max+Phone",
                "https://www.flipkart.com/search?q=Apple+USB-C+Cable",
                "https://www.flipkart.com/search?q=Apple+Silicone+Case",
                "https://www.flipkart.com/search?q=Apple+Adapter"
            ],
            "Myntra": [
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}",
                f"https://www.myntra.com/{brand.replace(' ', '-')}"
            ],
            "Google": [
                "https://www.google.co.in/search?q=Apple+Phone",
                "https://www.google.co.in/search?q=Apple+Phone",
                "https://www.google.co.in/search?q=Apple+Phone",
                "https://www.google.co.in/search?q=Apple+Phone",
                "https://www.google.co.in/search?q=Apple+Phone",
                "https://www.google.co.in/search?q=Apple+Phone",
                "https://www.google.co.in/search?q=Apple+Phone",
                "https://www.google.co.in/search?q=Apple+USB-C+Cable",
                "https://www.google.co.in/search?q=Apple+Case",
                "https://www.google.co.in/search?q=Apple+Adapter"
            ]
        }
        
    else:
        # Generic category fallback
        brand = "Standard"
        keyword = query.split()[-1] if query.split() else "Item"
        
        titles = [
            f"{brand} Basic {keyword.capitalize()} Pack",
            f"Essential Daily {keyword.capitalize()}",
            f"Enhanced Grip {keyword.capitalize()}",
            f"Portable {keyword.capitalize()}",
            f"Eco Friendly Premium {keyword.capitalize()}",
            f"Smart Tech {keyword.capitalize()}",
            f"{brand} Premium {keyword.capitalize()}",
            f"Advanced {keyword.capitalize()} Bundle",
            f"Smart Tech {keyword.capitalize()} Pro",
            f"Ultra Luxury {keyword.capitalize()}"
        ]
        prices = [199.0, 399.0, 599.0, 999.0, 1499.0, 1899.0, 2499.0, 3999.0, 5999.0, 12999.0]
        images = [
            "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?w=500",
            "https://images.unsplash.com/photo-1526170375885-4d8ecf77b99f?w=500",
            "https://images.unsplash.com/photo-1572635196237-14b3f281503f?w=500",
            "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?w=500",
            "https://images.unsplash.com/photo-1526170375885-4d8ecf77b99f?w=500",
            "https://images.unsplash.com/photo-1572635196237-14b3f281503f?w=500",
            "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?w=500",
            "https://images.unsplash.com/photo-1526170375885-4d8ecf77b99f?w=500",
            "https://images.unsplash.com/photo-1572635196237-14b3f281503f?w=500",
            "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?w=500"
        ]
        
        # Robust Meesho Product Pool using Google "I'm Feeling Lucky" redirects
        # This completely bypasses Meesho's bot protection and dynamically links to the exact active product page via Google's first result.
        meesho_real_pool = [
            {"title": "Puma Men Black Flyer Flex Running Shoes", "price": 2749.0, "url": "https://www.google.com/search?btnI=1&q=site:meesho.com+Puma+Men+Black+Flyer+Flex+Running+Shoes", "image_url": "https://images.unsplash.com/photo-1608231387042-66d1773070a5?w=500"},
            {"title": "HRX by Hrithik Roshan Men White Sneakers", "price": 1499.0, "url": "https://www.google.com/search?btnI=1&q=site:meesho.com+HRX+by+Hrithik+Roshan+Men+White+Sneakers", "image_url": "https://images.unsplash.com/photo-1525966222134-fcfa99b8ae77?w=500"},
            {"title": "Nike Men Black Revolution 6 Running Shoes", "price": 3695.0, "url": "https://www.google.com/search?btnI=1&q=site:meesho.com+Nike+Men+Black+Revolution+6+Running+Shoes", "image_url": "https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=500"},
            {"title": "Campus Men Black Mesh Running Shoes", "price": 999.0, "url": "https://www.google.com/search?btnI=1&q=site:meesho.com+Campus+Men+Black+Mesh+Running+Shoes", "image_url": "https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?w=500"},
            {"title": "Red Tape Men White Walking Shoes", "price": 1649.0, "url": "https://www.google.com/search?btnI=1&q=site:meesho.com+Red+Tape+Men+White+Walking+Shoes", "image_url": "https://images.unsplash.com/photo-1460353581641-37baddab0fa2?w=500"},
            {"title": "HIGHLANDER Men Slim Fit Casual Shirt", "price": 506.0, "url": "https://www.google.com/search?btnI=1&q=site:meesho.com+HIGHLANDER+Men+Slim+Fit+Casual+Shirt", "image_url": "https://images.unsplash.com/photo-1596755094514-f87e32f85e2c?w=500"},
            {"title": "Roadster Men Cotton Casual Shirt", "price": 649.0, "url": "https://www.google.com/search?btnI=1&q=site:meesho.com+Roadster+Men+Cotton+Casual+Shirt", "image_url": "https://images.unsplash.com/photo-1602810318383-e386cc2a3ccf?w=500"},
            {"title": "HERE&NOW Men Printed T-shirt", "price": 499.0, "url": "https://www.google.com/search?btnI=1&q=site:meesho.com+HERE%26NOW+Men+Printed+T-shirt", "image_url": "https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?w=500"}
        ]
        urls = {"Meesho": meesho_real_pool}

    # Standardize target platform mapping
    target_plat_clean = target_platform.lower()
    if "amazon" in target_plat_clean:
        target_platform = "Amazon"
    elif "flipkart" in target_plat_clean:
        target_platform = "Flipkart"
    elif "google" in target_plat_clean:
        target_platform = "Google"
    elif "meesho" in target_plat_clean:
        target_platform = "Meesho"
        
    # Build response array based on requested platforms
    platforms = ["Amazon", "Flipkart", "Meesho", "Google"]
    if target_platform in platforms:
        platforms = [target_platform]
        
    results = []
    query_encoded = urllib.parse.quote(query)
    
    for plat in platforms:
        for i in range(10):
            # If Meesho has specific items, use them, otherwise fall back to generic item generation
            if plat == "Meesho":
                meesho_items = urls.get("Meesho", []) if 'urls' in locals() else []
                if meesho_items and i < len(meesho_items):
                    real_meesho_item = meesho_items[i] if not isinstance(meesho_items[i], str) else None
                    if real_meesho_item:
                        results.append({
                            "title": f"{real_meesho_item['title']}",
                            "price": real_meesho_item['price'],
                            "url": real_meesho_item['url'],
                            "image_url": real_meesho_item['image_url'],
                            "platform": "Meesho"
                        })
                        continue

            # Slightly vary price per platform so they are not identical
            var_price = prices[i]
            if plat == "Flipkart":
                var_price = max(99.0, round(prices[i] * 0.95, 2))
            elif plat == "Myntra":
                var_price = max(99.0, round(prices[i] * 0.90, 2))
            elif plat == "Google":
                var_price = max(99.0, round(prices[i] * 0.92, 2))
                
            # Connect mock products to matching pages (use direct URL if specified, otherwise search redirect)
            if 'urls' in locals() and plat in urls and i < len(urls[plat]) and not isinstance(urls[plat][i], dict):
                prod_url = urls[plat][i]
            else:
                title_encoded = urllib.parse.quote(titles[i])
                if plat == "Amazon":
                    prod_url = f"https://www.amazon.in/s?k={title_encoded}"
                elif plat == "Flipkart":
                    prod_url = f"https://www.flipkart.com/search?q={title_encoded}"
                elif plat == "Myntra":
                    prod_url = f"https://www.myntra.com/{urllib.parse.quote(titles[i].replace(' ', '-').lower())}"
                elif plat == "Google":
                    prod_url = f"https://www.google.co.in/search?q={title_encoded}&tbm=shop"
                elif plat == "Meesho":
                    prod_url = f"https://www.meesho.com/search?q={title_encoded}"
                else:
                    prod_url = f"https://www.google.com/search?q={title_encoded}"
                
            results.append({
                "title": f"[{plat}] " + titles[i],
                "price": var_price,
                "url": prod_url,
                "image_url": images[i],
                "platform": plat
            })
            
    # Sort results
    results.sort(key=lambda x: x["price"])
    return results

async def block_unnecessary_resources(route):
    req = route.request
    res_type = req.resource_type
    # Block fonts and media requests to optimize load speeds. 
    # We allow 'image' because some sites (like Flipkart) need them to render lazy-loaded elements properly.
    if res_type in ["font", "media"]:
        await route.abort()
    elif any(tracker in req.url.lower() for tracker in ["google-analytics", "doubleclick", "facebook", "analytics", "hotjar"]):
        await route.abort()
    else:
        await route.continue_()

async def scrape_search_amazon(browser, query):
    """
    Live Scraper for Amazon.in
    """
    import time
    import urllib.parse
    import asyncio
    
    start = time.time()
    ck = _cache_key("amazon", query, True)
    cached = _cache_get(ck)
    if cached is not None:
        print(f"  [Amazon] [CACHE] Returning {len(cached)} cached results")
        return cached

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800}
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        "if(!window.chrome) window.chrome={runtime:{}};"
    )
    from playwright_stealth import stealth_async
    page = await context.new_page()
    await stealth_async(page)
    await page.route("**/*", block_unnecessary_resources)
    try:
        url = f"https://www.amazon.in/s?k={urllib.parse.quote(query)}"
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await page.evaluate("window.scrollTo(0, 600)")
        await asyncio.sleep(0.1)
        
        cards_data = await page.evaluate(r"""
        () => {
            let items = [];
            let cards = document.querySelectorAll('div[data-component-type="s-search-result"]');
            for (let card of cards) {
                try {
                    let linkEl = card.querySelector('h2 a') || card.querySelector('a.a-link-normal.a-text-normal');
                    let title = "";
                    let href = "";
                    if (linkEl) {
                        href = linkEl.getAttribute('href') || "";
                        title = linkEl.innerText.trim();
                    }
                    if (!title) {
                        let titleEl = card.querySelector('h2');
                        if (titleEl) title = titleEl.innerText.trim();
                    }
                    
                    let dpMatch = href.match(/(\/[\w\-]+\/dp\/[A-Z0-9]{10})/);
                    let cleanHref = dpMatch ? dpMatch[1] : href.split('?')[0];
                    
                    let priceEl = card.querySelector('span.a-price span.a-offscreen') || card.querySelector('span.a-price-whole');
                    let priceText = priceEl ? priceEl.innerText.trim() : '';
                    
                    let imgEl = card.querySelector('img.s-image');
                    let imgUrl = imgEl ? (imgEl.getAttribute('data-old-hires') || imgEl.src || '') : '';
                    
                    if (title && cleanHref && priceText) {
                        items.push({ title, href: cleanHref, price: priceText, image: imgUrl });
                    }
                } catch(e) {}
            }
            return items;
        }
        """)
        products = []
        for item in cards_data[:18]:
            price = clean_price(item.get('price', ''))
            if price is None:
                continue
            prod_url = item['href'] if item['href'].startswith('http') else urllib.parse.urljoin("https://www.amazon.in", item['href'])
            products.append({
                "title": item['title'],
                "price": price,
                "url": prod_url,
                "image_url": item['image'] or "https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=500",
                "platform": "Amazon"
            })
            
        if len(products) > 0:
            _cache_set(ck, products)
        print(f"  [Amazon] Live scrape done - {len(products)} products found ({time.time() - start:.2f}s)")
        return products
    except Exception as e:
        print(f"  [Amazon] Live scrape failed: {str(e)}")
        return []
    finally:
        await context.close()


async def scrape_search_flipkart(browser, query):
    """
    Asynchronously scrapes Flipkart.com.
    Uses resource blocking and stealth settings to achieve optimal speed.
    """
    import time
    import urllib.parse
    start = time.time()
    
    # Cache check
    ck = _cache_key("flipkart", query, True)
    cached = _cache_get(ck)
    if cached is not None:
        print(f"  [Flipkart] [CACHE] Returning {len(cached)} cached results")
        return cached

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800}
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        "if(!window.chrome) window.chrome={runtime:{}};"
    )
    from playwright_stealth import stealth_async
    page = await context.new_page()
    await stealth_async(page)
    await page.route("**/*", block_unnecessary_resources)
    try:
        url = f"https://www.flipkart.com/search?q={urllib.parse.quote(query)}"
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await page.evaluate("window.scrollTo(0, 600)")
        await asyncio.sleep(0.1)
        
        cards_data = await page.evaluate(r"""
        () => {
            let items = [];
            let seen = new Set();
            let cards = document.querySelectorAll('div.bLCLBY, div[data-id]');
            for (let card of cards) {
                try {
                    let a = card.querySelector('a[href*="/p/"]');
                    if (!a) continue;
                    let href = a.getAttribute('href') || "";
                    if (!href) continue;
                    
                    let idMatch = href.match(/itm[a-zA-Z0-9]+/);
                    let id = idMatch ? idMatch[0] : href;
                    if (seen.has(id)) continue;
                    seen.add(id);
                    
                    let titleEl = card.querySelector('a.atJtCj') || card.querySelector('a.IRpwTa') || card.querySelector('div._4rR01T') || card.querySelector('a[title]');
                    let title = titleEl ? (titleEl.innerText || titleEl.getAttribute('title')) : "";
                    if (!title) {
                        let text = card.innerText.split('\\n');
                        title = text[1] || text[0] || 'Flipkart Product';
                    }
                    
                    let priceEl = card.querySelector('div.hZ3P6w') || card.querySelector('div._30jeq3') || card.querySelector('span.y30bFC');
                    let priceText = "";
                    if (priceEl) {
                        priceText = priceEl.innerText;
                    } else {
                        let pm = card.innerText.match(/[₹$]\\s*\\d+(,\\d+)*/);
                        if (pm) priceText = pm[0];
                    }
                    
                    let img = card.querySelector('img');
                    let imgUrl = img ? (img.src || img.getAttribute('data-src') || '') : '';
                    
                    if (title && priceText && href) {
                        items.push({ title: title.trim(), price: priceText.trim(), url: href, image: imgUrl });
                    }
                } catch(e) {}
            }
            return items;
        }
        """)
        products = []
        for item in cards_data[:18]:
            price = clean_price(item.get('price', ''))
            if price is None:
                continue
            prod_url = item['url'] if item['url'].startswith('http') else urllib.parse.urljoin("https://www.flipkart.com", item['url'])
            products.append({
                "title": item['title'][:100],
                "price": price,
                "url": prod_url,
                "image_url": item['image'] or "https://images.unsplash.com/photo-1531403009284-440f080d1e12?w=500",
                "platform": "Flipkart"
            })
        _cache_set(ck, products)
        print(f"  [Flipkart] Live scrape done - {len(products)} products found ({time.time() - start:.2f}s)")
        return products
    except Exception as e:
        print(f"  [Flipkart] Live scrape failed: {str(e)}")
        return []
    finally:
        await context.close()

async def scrape_search_google(browser, query):
    """
    Asynchronously scrapes Google Shopping.
    Uses resource blocking and stealth settings.
    """
    import time
    import urllib.parse
    start = time.time()
    
    # Cache check
    ck = _cache_key("google", query, True)
    cached = _cache_get(ck)
    if cached is not None:
        print(f"  [Google] [CACHE] Returning {len(cached)} cached results")
        return cached

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800}
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        "if(!window.chrome) window.chrome={runtime:{}};"
    )
    from playwright_stealth import stealth_async
    page = await context.new_page()
    await stealth_async(page)
    await page.route("**/*", block_unnecessary_resources)
    try:
        url = f"https://www.google.co.in/search?q={urllib.parse.quote(query)}&tbm=shop"
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await page.evaluate("window.scrollTo(0, 600)")
        await asyncio.sleep(0.1)
        
        cards_data = await page.evaluate("""() => {
            let items = [];
            let cards = document.querySelectorAll('div.sh-dgr__grid-result');
            if (cards.length === 0) {
                cards = document.querySelectorAll('div[data-docid], div.sh-pr__product-results-grid > div');
            }
            if (cards.length === 0) {
                cards = [];
                let allDivs = document.querySelectorAll('div');
                for (let div of allDivs) {
                    if (div.querySelector('a') && div.innerText.match(/[₹$]\\s*\\d+(,\\d+)*/) && div.querySelector('img')) {
                        if (div.querySelectorAll('div').length < 10) {
                            cards.push(div);
                        }
                    }
                }
            }
            
            for (let card of cards) {
                try {
                    let titleEl = card.querySelector('h3, h4, div.X1Yr1, div.tAx7id');
                    let title = titleEl ? titleEl.innerText : "";
                    
                    let priceEl = card.querySelector('span.a8Pemb, span.H1y62d, b');
                    let priceText = "";
                    if (priceEl) {
                        priceText = priceEl.innerText;
                    } else {
                        let m = card.innerText.match(/[₹$]\\s*\\d+(,\\d+)*/);
                        if (m) priceText = m[0];
                    }
                    
                    let a = card.querySelector('a[href*="/shopping/product/"], a[href*="/url?q="], a');
                    let href = a ? a.getAttribute('href') : "";
                    
                    let img = card.querySelector('img');
                    let imgUrl = img ? img.src : "";
                    
                    if (!title && a) {
                        title = a.innerText.split('\\n')[0];
                    }
                    
                    if (title && priceText && href) {
                        items.push({ title: title.trim(), price: priceText.trim(), url: href, image: imgUrl });
                    }
                } catch(e) {}
            }
            return items;
        }""")
        products = []
        for item in cards_data[:18]:
            price = clean_price(item['price'])
            if price is None:
                continue
                
            href = item['url']
            if href.startswith("/"):
                prod_url = f"https://www.google.com{href}"
            elif href.startswith("http"):
                prod_url = href
            else:
                prod_url = href
                
            if "/url?q=" in prod_url:
                parsed_url = urllib.parse.urlparse(prod_url)
                query_params = urllib.parse.parse_qs(parsed_url.query)
                direct_url = query_params.get('q', [None])[0]
                if direct_url:
                    prod_url = direct_url
                    
            products.append({
                "title": item['title'],
                "price": price,
                "url": prod_url,
                "image_url": item['image'] or "https://images.unsplash.com/photo-1531403009284-440f080d1e12?w=500",
                "platform": "Google"
            })
        _cache_set(ck, products)
        print(f"  [Google] Live scrape done - {len(products)} products found ({time.time() - start:.2f}s)")
        return products
    except Exception as e:
        print(f"  [Google] Live scrape failed: {str(e)}")
        return []
    finally:
        await context.close()



def clean_search_query(query):
    """Cleans conversational prefix words to get high-quality search terms."""
    clean = re.sub(r'^(what is the price of|what is price of|check price of|compare price of|find price of|show price of|price of|price|find|search)\s+', '', query, flags=re.IGNORECASE)
    clean = clean.replace("?", "").strip()
    return clean

def search_platforms(query, platform="All", headless=True):
    """
    Searches Amazon, Flipkart, Google, and/or Myntra CONCURRENTLY using
    async Playwright contexts — all platforms run in parallel on a single browser
    so total wait time is minimized to ~3-5 seconds.
    Returns: (list_of_results, is_mocked)
    """
    import asyncio
    query = clean_search_query(query)

    # Normalise platform name (handle typos like "amazone")
    target_platform = platform.strip()
    plat_clean = target_platform.lower()
    if   "amazon"   in plat_clean: target_platform = "Amazon"
    elif "flipkart" in plat_clean: target_platform = "Flipkart"
    elif "google"   in plat_clean: target_platform = "Google"
    elif "myntra"   in plat_clean: target_platform = "Myntra"

    async def async_search():
        from playwright.async_api import async_playwright
        tasks_to_run = {
            "Amazon":   scrape_search_amazon,
            "Flipkart": scrape_search_flipkart,
            "Google":   scrape_search_google,
            "Myntra":   scrape_search_myntra,
        }
        if target_platform != "All":
            tasks_to_run = {k: v for k, v in tasks_to_run.items() if k == target_platform}

        print(f"[PARALLEL] Launching {len(tasks_to_run)} platform scraper(s) concurrently via Async Playwright for '{query}'")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            
            # Fire all scrapes concurrently in the same browser connection
            coros = [fn(browser, query) for fn in tasks_to_run.values()]
            results = await asyncio.gather(*coros, return_exceptions=True)
            await browser.close()
            
            # Map results
            mapped = {}
            for idx, name in enumerate(tasks_to_run.keys()):
                res = results[idx]
                if isinstance(res, Exception):
                    print(f"  [{name}] [ERR] Scraper failed with exception: {res}")
                    mapped[name] = []
                else:
                    mapped[name] = res or []
            return mapped

    # Execute async event loop
    results_map = asyncio.run(async_search())

    requested_plats = ["Amazon", "Flipkart", "Google", "Myntra"] if target_platform == "All" else [target_platform]
    is_mocked = False
    
    # Fill in blanks for individual platforms that returned 0 results due to blocks/captchas
    for plat in requested_plats:
        if not results_map.get(plat):
            print(f"  [{plat}] [FALLBACK] No live results found. Generating high-fidelity mock results...")
            mock_res = get_mock_search_results(query, target_platform=plat)
            results_map[plat] = [r for r in mock_res if r["platform"] == plat]
            is_mocked = True

    combined = []
    for plat in requested_plats:
        combined.extend(results_map.get(plat, []))

    combined.sort(key=lambda x: x["price"] or 999999)
    return combined, is_mocked

async def search_platforms_async(query, platform="All", headless=True):
    import asyncio
    import urllib.parse
    from playwright.async_api import async_playwright
    
    query = clean_search_query(query)
    
    async with async_playwright() as p:
        # Check if we are running in a cloud/headless-only environment like Hugging Face
        import os
        is_cloud = "SPACE_ID" in os.environ or os.environ.get("SYSTEM") == "spaces"
        
        stealth_args = [
            "--disable-blink-features=AutomationControlled", 
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-size=1280,800",
            "--ignore-certificate-errors",
            "--proxy-server='direct://'",
            "--proxy-bypass-list=*"
        ]
        
        # Launch Playwright's own Chromium
        try:
            browser = await p.chromium.launch(
                headless=headless,
                args=stealth_args
            )
            print(f"  [Scraper] Playwright Chromium launched (headless={headless}).")
        except Exception as e:
            print(f"  [Scraper] Chromium launch failed: {e}. Will use mock data.")
            browser = None
        
        # Meesho blocks headless browsers entirely ("Access Denied").
        # We need a separate headed (visible) browser instance for Meesho if possible.
        meesho_browser = None
        try:
            # Force headless=True on cloud environments to prevent X11 crashes
            meesho_headless = True if is_cloud else False
            meesho_browser = await p.chromium.launch(
                headless=meesho_headless,
                args=stealth_args
            )
            print(f"  [Scraper] Meesho browser launched (headless={meesho_headless}).")
        except Exception as e:
            print(f"  [Scraper] Meesho headed launch failed: {e}. Will use mock data for Meesho.")
        
        tasks = []
        task_names = []
        
        if browser:
            tasks.append(scrape_search_amazon(browser, query))
            task_names.append("Amazon")
            tasks.append(scrape_search_flipkart(browser, query))
            task_names.append("Flipkart")
            tasks.append(scrape_search_google(browser, query))
            task_names.append("Google")
        
        if meesho_browser:
            tasks.append(scrape_search_meesho(meesho_browser, query))
            task_names.append("Meesho")
        
        if tasks:
            results_list = await asyncio.gather(*tasks, return_exceptions=True)
        else:
            results_list = []
        
        # Clean up browsers
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if meesho_browser:
            try:
                await meesho_browser.close()
            except Exception:
                pass
    
    # Map results by platform name
    results_map = {}
    for i, res in enumerate(results_list):
        if isinstance(res, Exception):
            print(f"  [{task_names[i]}] [ERR] Scraper exception: {res}")
            continue
        if res and len(res) > 0:
            plat = res[0].get("platform")
            if plat:
                results_map[plat] = res
                print(f"  [{plat}] Got {len(res)} live results.")
                
    requested_plats = ["Amazon", "Flipkart", "Meesho", "Google"] if platform == "All" else [platform]

    for plat in requested_plats:
        if not results_map.get(plat):
            print(f"  [{plat}] [FALLBACK] No live results. Generating mock data...")
            mock_res = get_mock_search_results(query, target_platform=plat)
            filtered_res = [r for r in mock_res if r["platform"] == plat]
            if not filtered_res and mock_res:
                filtered_res = mock_res
            results_map[plat] = filtered_res
            
    return results_map


async def scrape_search_meesho(browser, query):
    """
    Live Scraper for Meesho.com
    """
    import time
    import urllib.parse
    import asyncio
    
    start = time.time()
    ck = _cache_key("meesho", query, True)
    cached = _cache_get(ck)
    if cached is not None:
        print(f"  [Meesho] [CACHE] Returning {len(cached)} cached results")
        return cached

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800},
        locale="en-IN",
        timezone_id="Asia/Kolkata"
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        "if(!window.chrome) window.chrome={runtime:{}};"
        "Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});"
        "Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en-US', 'en']});"
    )
    from playwright_stealth import stealth_async
    page = await context.new_page()
    await stealth_async(page)
    await page.route("**/*", block_unnecessary_resources)
    try:
        url = f"https://www.meesho.com/search?q={urllib.parse.quote(query)}"
        await page.goto(url, wait_until="networkidle", timeout=20000)
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
        products = []
        for item in cards_data[:18]:
            price = clean_price(item.get('price', ''))
            if price is None:
                continue
            prod_url = item['url'] if item['url'] and item['url'].startswith('http') else urllib.parse.urljoin("https://www.meesho.com", item['url'])
            if prod_url == "https://www.meesho.com":
                continue
            products.append({
                "title": item['title'],
                "price": price,
                "url": prod_url,
                "image_url": item['img'] or "https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=500",
                "platform": "Meesho"
            })
            
        if len(products) > 0:
            _cache_set(ck, products)
        print(f"  [Meesho] Live scrape done - {len(products)} products found ({time.time() - start:.2f}s)")
        return products
    except Exception as e:
        print(f"  [Meesho] Live scrape failed: {str(e)}")
        return []
    finally:
        await context.close()
