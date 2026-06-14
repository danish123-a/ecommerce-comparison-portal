import os
import random
import webbrowser
import urllib.parse
from datetime import datetime, timedelta
import database
import scraper
import predictor

class PriceTrackerAgent:
    def __init__(self, db_path="prices.db"):
        self.db_path = db_path
        database.init_db(self.db_path)

    def track_new_product(self, url, target_price=None, force_title=None, force_price=None, force_image=None):
        """
        Attempts to scrape a product from the URL and starts tracking it.
        If force parameters are provided, it bypasses live scraping.
        Returns: (product_id, title, price, image_url, is_mocked)
        """
        if force_title and force_price is not None:
            # Seed a mock product directly
            platform = scraper.detect_platform(url)
            image = force_image or "https://images.unsplash.com/photo-1531403009284-440f080d1e12?w=500"
            product_id = database.add_product(self.db_path, url, platform, force_title, target_price, image)
            database.log_price(self.db_path, product_id, force_price)
            return product_id, force_title, force_price, image, True

        # Live scrape
        title, price, platform, image_url, _coupons, error = scraper.scrape_url(url)
        if error and not title:
            raise ValueError(f"Could not scrape product information: {error}")
            
        if price is None:
            raise ValueError(f"Could not extract price from the product page. Details: {error or 'Unknown issue'}")

        product_id = database.add_product(self.db_path, url, platform, title, target_price, image_url)
        database.log_price(self.db_path, product_id, price)
        return product_id, title, price, image_url, False

    def update_tracked_prices(self):
        """
        Updates the current price and image of all products in the database.
        Returns a list of updates status dicts.
        """
        products = database.get_all_products(self.db_path)
        updates = []
        
        for prod in products:
            p_id = prod['id']
            url = prod['url']
            old_price = prod['latest_price']
            
            # Scrape live details
            title, price, platform, image_url, _coupons, error = scraper.scrape_url(url)
            
            if error or price is None:
                updates.append({
                    "product_id": p_id,
                    "title": prod['title'],
                    "status": "failed",
                    "error": error or "Could not retrieve price",
                    "old_price": old_price,
                    "new_price": None
                })
            else:
                # Log new price and update image if missing
                database.log_price(self.db_path, p_id, price)
                if image_url:
                    database.add_product(self.db_path, url, platform, prod['title'], prod['target_price'], image_url)
                
                # Check for target price notification
                target_met = False
                if prod['target_price'] and price <= prod['target_price']:
                    target_met = True
                    
                updates.append({
                    "product_id": p_id,
                    "title": prod['title'],
                    "status": "success",
                    "old_price": old_price,
                    "new_price": price,
                    "target_price": prod['target_price'],
                    "target_met": target_met,
                    "error": None
                })
                
        return updates

    def get_prediction(self, product_id):
        """
        Retrieves prediction stats and recommendations for a product.
        """
        product = database.get_product(self.db_path, product_id)
        if not product:
            raise ValueError(f"Product with ID {product_id} not found.")
            
        history = database.get_price_history(self.db_path, product_id)
        prediction = predictor.predict_price(history, product['target_price'])
        
        return {
            "product": product,
            "prediction": prediction,
            "history_length": len(history)
        }

    def seed_mock_price_history(self, product_id, days=30, trend_pattern="downward"):
        """
        Seeds artificial historical price data for a product over a given number of days.
        Useful to test predictions.
        """
        product = database.get_product(self.db_path, product_id)
        if not product:
            raise ValueError(f"Product with ID {product_id} not found.")

        current_price = product['latest_price'] or 1000.0
        base_price = current_price
        
        start_date = datetime.now() - timedelta(days=days)
        
        # Clear existing history
        conn = database.get_connection(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM price_history WHERE product_id = ?", (product_id,))
        conn.commit()
        conn.close()

        # Seed data
        for day in range(days + 1):
            timestamp = (start_date + timedelta(days=day)).strftime("%Y-%m-%d %H:%M:%S")
            progress = day / days
            
            if trend_pattern == "downward":
                price = base_price * (1.25 - 0.25 * progress)
                price += random.uniform(-0.01, 0.01) * base_price
            elif trend_pattern == "upward":
                price = base_price * (0.80 + 0.20 * progress)
                price += random.uniform(-0.01, 0.01) * base_price
            elif trend_pattern == "discount":
                if 0.4 <= progress <= 0.6:
                    price = base_price * 0.75
                else:
                    price = base_price
                price += random.uniform(-0.01, 0.01) * base_price
            else:  # volatile
                price = base_price * (1.0 + random.uniform(-0.08, 0.08))

            price = round(price, 2)
            database.log_price(self.db_path, product_id, price, timestamp=timestamp)
        
        database.log_price(self.db_path, product_id, current_price, timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        return f"Successfully seeded {days + 1} historical price entries for product {product_id} with pattern '{trend_pattern}'."

    def process_natural_language_query(self, query):
        """
        Interprets a user's natural language search query and maps generic items
        to high/medium level brands if a brand name is missing.
        """
        query_clean = query.lower().strip()
        
        # Check for category indicators and default brands
        # 1. Shoes
        if any(w in query_clean for w in ["shoe", "shoes", "boot", "sneaker", "footwear", "slipper"]):
            brands = ["campus", "puma", "adidas", "nike", "reebok", "sparx", "woodland"]
            if not any(b in query_clean for b in brands):
                # Brand missing, inject default brand "Campus"
                return "Campus Shoes"
                
        # 2. Clothes
        elif any(w in query_clean for w in ["cloth", "clothes", "clothing", "shirt", "tshirt", "jeans", "pant", "jacket", "hoodie"]):
            brands = ["zara", "h&m", "hm", "levis", "levi", "us polo", "allen solly", "roadster"]
            if not any(b in query_clean for b in brands):
                # Brand missing, inject default brand "Zara"
                return "Zara Clothes"
                
        # 3. Mobile Phones
        elif any(w in query_clean for w in ["phone", "mobile", "smartphone", "iphone", "samsung", "oneplus"]):
            brands = ["apple", "iphone", "samsung", "oneplus", "realme", "redmi", "xiaomi"]
            if not any(b in query_clean for b in brands):
                # Brand missing, inject default brand "Apple"
                return "Apple Phone"
                
        # Return original query if we didn't map it, or if it already had a brand
        return query

    def generate_comparison_report(self, query, results, output_file="comparison_report.html"):
        """
        Generates a premium, responsive, glassmorphic HTML comparison report.
        Displays price comparison, platform badges, product names, and active product images.
        """
        # Calculate stats
        valid_prices = [r["price"] for r in results if r["price"] is not None]
        cheapest_price = min(valid_prices) if valid_prices else 0.0
        
        cheapest_item = None
        for r in results:
            if r["price"] == cheapest_price:
                cheapest_item = r
                break
                
        html_cards = []
        for idx, item in enumerate(results):
            platform = item["platform"]
            price_val = item["price"]
            
            # Platform specific styling
            badge_color = "#FF9900" if platform == "Amazon" else ("#2874F0" if platform == "Flipkart" else ("#F43397" if platform == "Meesho" else "#64748B"))
            currency_symbol = "₹" if platform in ["Amazon", "Flipkart", "Meesho"] else "$"
            price_display = f"{currency_symbol}{price_val:,.2f}" if price_val is not None else "N/A"
            
            # Highlighting cheap products
            is_cheapest = (price_val == cheapest_price and cheapest_price > 0)
            cheapest_badge = '<div class="cheapest-tag">Cheapest</div>' if is_cheapest else ''
            card_class = 'product-card highlight-card' if is_cheapest else 'product-card'
            
            title_trunc = item["title"][:70] + "..." if len(item["title"]) > 73 else item["title"]
            img_url = item["image_url"] or "https://images.unsplash.com/photo-1531403009284-440f080d1e12?w=500"
            
            html_cards.append(f"""
            <div class="{card_class}">
                {cheapest_badge}
                <div class="card-image-container">
                    <a href="{item['url']}" target="_blank" style="display: block; width: 100%; height: 100%;">
                        <img class="product-image" src="{img_url}" alt="{item['title']}">
                    </a>
                </div>
                <div class="card-content">
                    <span class="platform-badge" style="background-color: {badge_color};">{platform}</span>
                    <h3 class="product-title" title="{item['title']}">{title_trunc}</h3>
                    <div class="price-section">
                        <span class="price-value">{price_display}</span>
                    </div>
                    <a class="buy-button" href="{item['url']}" target="_blank">View on {platform}</a>
                </div>
            </div>
            """)
            
        cards_html = "\n".join(html_cards)
        
        # Build full HTML
        now_str = datetime.now().strftime("%B %d, %Y at %I:%M %p")
        
        cheapest_summary = ""
        if cheapest_item:
            currency_symbol = "₹" if cheapest_item['platform'] in ['Flipkart', 'Amazon', 'Meesho'] else "$"
            cheapest_summary = f"""
            <div class="summary-container">
                <h2>🏆 Deal Recommendation</h2>
                <p>The best deal is <strong>{cheapest_item['title'][:60]}...</strong> on <strong>{cheapest_item['platform']}</strong> for <span class="highlight">{currency_symbol}{cheapest_price:,.2f}</span>!</p>
            </div>
            """
            
        html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>E-Commerce Price Comparison: {query}</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-color: #0f172a;
            --card-bg: rgba(30, 41, 59, 0.7);
            --border-color: rgba(255, 255, 255, 0.08);
            --text-color: #f8fafc;
            --text-muted: #94a3b8;
            --primary-glow: linear-gradient(135deg, #38bdf8, #818cf8);
            --gold-glow: linear-gradient(135deg, #fbbf24, #f59e0b);
        }}
        
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        
        body {{
            background-color: var(--bg-color);
            background-image: 
                radial-gradient(at 10% 10%, rgba(56, 189, 248, 0.08) 0px, transparent 50%),
                radial-gradient(at 90% 90%, rgba(129, 140, 248, 0.08) 0px, transparent 50%);
            color: var(--text-color);
            font-family: 'Outfit', sans-serif;
            min-height: 100vh;
            padding: 2rem 1rem;
        }}
        
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        
        header {{
            text-align: center;
            margin-bottom: 3rem;
        }}
        
        header h1 {{
            font-size: 2.8rem;
            font-weight: 800;
            background: var(--primary-glow);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.5rem;
        }}
        
        header p {{
            color: var(--text-muted);
            font-size: 1.1rem;
            font-weight: 300;
        }}
        .summary-container {{
            background: rgba(30, 41, 59, 0.85);
            border: 1px solid var(--border-color);
            border-left: 5px solid #fbbf24;
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 2.5rem;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.2);
            backdrop-filter: blur(10px);
        }}
        
        .summary-container h2 {{
            font-size: 1.4rem;
            margin-bottom: 0.5rem;
            font-weight: 600;
            color: #fbbf24;
        }}
        
        .summary-container p {{
            font-size: 1.1rem;
            color: #e2e8f0;
        }}
        
        .summary-container .highlight {{
            font-weight: 600;
            color: #fbbf24;
            font-size: 1.25rem;
        }}
        
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 2rem;
        }}
        
        .product-card {{
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            position: relative;
            backdrop-filter: blur(12px);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.15);
        }}
        
        .product-card:hover {{
            transform: translateY(-8px);
            border-color: rgba(255, 255, 255, 0.15);
            box-shadow: 0 12px 30px rgba(0, 0, 0, 0.3);
        }}
        
        .highlight-card {{
            border-color: rgba(251, 191, 36, 0.4);
            box-shadow: 0 0 15px rgba(251, 191, 36, 0.15);
        }}
        
        .highlight-card:hover {{
            border-color: rgba(251, 191, 36, 0.8);
            box-shadow: 0 0 25px rgba(251, 191, 36, 0.3);
        }}
        
        .cheapest-tag {{
            position: absolute;
            top: 15px;
            right: 15px;
            background: var(--gold-glow);
            color: #000000;
            font-weight: 600;
            font-size: 0.8rem;
            padding: 0.25rem 0.75rem;
            border-radius: 50px;
            z-index: 10;
            box-shadow: 0 2px 10px rgba(245, 158, 11, 0.3);
        }}
        
        .card-image-container {{
            width: 100%;
            height: 200px;
            background-color: #ffffff;
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
            border-bottom: 1px solid var(--border-color);
        }}
        
        .product-image {{
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
            transition: transform 0.5s ease;
        }}
        
        .product-card:hover .product-image {{
            transform: scale(1.05);
        }}
        
        .card-content {{
            padding: 1.5rem;
            display: flex;
            flex-direction: column;
            flex-grow: 1;
        }}
        
        .platform-badge {{
            display: inline-block;
            align-self: flex-start;
            color: #ffffff;
            font-size: 0.75rem;
            font-weight: 600;
            padding: 0.2rem 0.6rem;
            border-radius: 6px;
            margin-bottom: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        
        .product-title {{
            font-size: 1.05rem;
            font-weight: 400;
            line-height: 1.4;
            color: #e2e8f0;
            margin-bottom: 1.25rem;
            height: 3rem;
            overflow: hidden;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
        }}
        
        .price-section {{
            margin-top: auto;
            margin-bottom: 1.25rem;
        }}
        
        .price-value {{
            font-size: 1.6rem;
            font-weight: 600;
            background: linear-gradient(135deg, #ffffff, #94a3b8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        
        .highlight-card .price-value {{
            background: var(--gold-glow);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        
        .buy-button {{
            display: block;
            width: 100%;
            padding: 0.75rem;
            text-align: center;
            text-decoration: none;
            color: #ffffff;
            font-weight: 600;
            border-radius: 10px;
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.1);
            transition: all 0.2s ease;
        }}
        
        .buy-button:hover {{
            background: var(--primary-glow);
            border-color: transparent;
            box-shadow: 0 4px 15px rgba(129, 140, 248, 0.4);
        }}
        
        .highlight-card .buy-button {{
            background: rgba(251, 191, 36, 0.1);
            border-color: rgba(251, 191, 36, 0.3);
            color: #fbbf24;
        }}
        
        .highlight-card .buy-button:hover {{
            background: var(--gold-glow);
            border-color: transparent;
            color: #000000;
            box-shadow: 0 4px 15px rgba(245, 158, 11, 0.4);
        }}
        
        footer {{
            text-align: center;
            margin-top: 4rem;
            color: var(--text-muted);
            font-size: 0.9rem;
            font-weight: 300;
        }}
        
        @media (max-width: 600px) {{
            header h1 {{
                font-size: 2.2rem;
            }}
            body {{
                padding: 1.5rem 0.75rem;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>E-Commerce Price Comparison</h1>
            <p>Search Results for "<strong>{query}</strong>" • Generated on {now_str}</p>
        </header>
        
        {cheapest_summary}
        
        <div class="grid">
            {cards_html}
        </div>
        
        <footer>
            E-Commerce Price Tracker & Predictor Agent • Pure Python Scraper & Analytics
        </footer>
    </div>
</body>
</html>
"""
        
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(html_template)
            
        print(f"\n[HTML REPORT] Generated visual comparison page at '{output_file}'")
        
        # Open in web browser automatically
        try:
            url_path = "file://" + urllib.parse.quote(os.path.abspath(output_file).replace('\\', '/'))
            webbrowser.open(url_path)
            print("[INFO] Opening report automatically in your web browser...")
        except Exception as e:
            print(f"[WARNING] Could not open browser automatically: {str(e)}")
            
        return output_file
