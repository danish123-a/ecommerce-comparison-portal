import sys
import io
import re
import random

# Force UTF-8 encoding for stdout/stderr to prevent UnicodeEncodeErrors on Windows terminals
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

import argparse
from agent import PriceTrackerAgent
import database
import scraper

def print_header(title):
    print("\n" + "=" * 60)
    print(f" {title.upper()} ".center(60, "="))
    print("=" * 60)

def format_currency(val, platform="Generic"):
    if val is None:
        return "N/A"
    symbol = "₹" if platform in ["Flipkart", "Amazon", "Meesho"] else "$"
    return f"{symbol}{val:.2f}"

def handle_add(agent, args):
    print_header("Adding New Product")
    print(f"URL: {args.url}")
    platform = scraper.detect_platform(args.url)
    if args.target:
        print(f"Target Price: {format_currency(args.target, platform)}")
        
    try:
        if args.mock_title or args.mock_price is not None:
            title = args.mock_title or "Simulated E-Commerce Product"
            price = args.mock_price if args.mock_price is not None else 999.0
            image = args.mock_image or "https://images.unsplash.com/photo-1531403009284-440f080d1e12?w=500"
            print("Mode: Simulated/Mock addition")
            p_id, title, price, image_url, is_mock = agent.track_new_product(
                args.url, args.target, force_title=title, force_price=price, force_image=image
            )
        else:
            print("Mode: Live scraping product page (please wait)...")
            p_id, title, price, image_url, is_mock = agent.track_new_product(args.url, args.target)
            
        print("\n[SUCCESS] Product added successfully!")
        print(f"ID          : {p_id}")
        print(f"Title       : {title[:50]}..." if len(title) > 50 else f"Title       : {title}")
        print(f"Current Price: {format_currency(price, platform)}")
        print(f"Target Price: {format_currency(args.target, platform)}")
        print(f"Image URL   : {image_url}")
    except Exception as e:
        print(f"\n[ERROR] Failed to add product: {str(e)}")
        print("\nTip: If you're getting blocked by anti-scraping protections, you can add a mock product for testing using:")
        print("  python main.py add <url> --mock-title \"My Product\" --mock-price 499.0")

def handle_list(agent, args):
    products = database.get_all_products(agent.db_path)
    print_header(f"Tracked Products ({len(products)} total)")
    
    if not products:
        print("No products are currently tracked. Add one with 'python main.py add <url>'")
        return

    # Print ASCII table headers
    print(f"{'ID':<4} | {'Platform':<9} | {'Current Price':<14} | {'Target Price':<14} | {'Product Title':<30}")
    print("-" * 80)
    for p in products:
        title_trunc = p['title'][:27] + "..." if len(p['title']) > 30 else p['title']
        curr_price_str = format_currency(p['latest_price'], p['platform'])
        target_price_str = format_currency(p['target_price'], p['platform'])
        print(f"{p['id']:<4} | {p['platform']:<9} | {curr_price_str:<14} | {target_price_str:<14} | {title_trunc:<30}")
    print("-" * 80)

def handle_update(agent, args):
    print_header("Updating Tracked Prices")
    print("Scraping latest prices from e-commerce sites. This may take a moment...")
    
    updates = agent.update_tracked_prices()
    
    if not updates:
        print("No products found in database to update.")
        return
        
    print("\nUpdate Results:")
    print("-" * 80)
    for up in updates:
        title_trunc = up['title'][:40] + "..." if len(up['title']) > 43 else up['title']
        if up['status'] == 'success':
            old_p = format_currency(up['old_price'])
            new_p = format_currency(up['new_price'])
            alert_str = ""
            if up['target_met']:
                alert_str = "  *** PRICE DROPPED BELOW TARGET! ***"
            print(f"ID {up['product_id']}: {title_trunc}\n    Price updated: {old_p} -> {new_p}{alert_str}")
        else:
            print(f"ID {up['product_id']}: {title_trunc}\n    [FAILED] Update failed. Error: {up['error']}")
    print("-" * 80)

def handle_history(agent, args):
    product = database.get_product(agent.db_path, args.id)
    if not product:
        print(f"[ERROR] Product with ID {args.id} not found.")
        return
        
    history = database.get_price_history(agent.db_path, args.id)
    print_header(f"Price History for ID {args.id}")
    print(f"Title: {product['title']}")
    print(f"URL:   {product['url']}\n")
    
    if not history:
        print("No recorded price logs.")
        return
        
    print(f"{'Index':<6} | {'Date & Time':<23} | {'Price':<15}")
    print("-" * 50)
    for idx, h in enumerate(history, 1):
        p_str = format_currency(h['price'], product['platform'])
        print(f"{idx:<6} | {h['timestamp']:<23} | {p_str:<15}")
    print("-" * 50)

def handle_predict(agent, args):
    print_header(f"Agent Recommendation & Price Forecast (ID {args.id})")
    try:
        res = agent.get_prediction(args.id)
        prod = res['product']
        pred = res['prediction']
        
        print(f"Product: {prod['title']}")
        print(f"Platform: {prod['platform']}")
        print(f"URL:     {prod['url']}")
        print(f"Recorded Logs Count: {res['history_length']}\n")
        
        print(f"Current Price : {format_currency(pred['current_price'], prod['platform'])}")
        print(f"Min Price     : {format_currency(pred['min_price'], prod['platform'])}")
        print(f"Max Price     : {format_currency(pred['max_price'], prod['platform'])}")
        print(f"Avg Price     : {format_currency(pred['avg_price'], prod['platform'])}")
        
        if pred['status'] == 'success':
            p_change = pred['price_change_pct']
            change_sign = "+" if p_change >= 0 else ""
            print(f"Overall Change: {change_sign}{p_change:.2f}% since tracking started")
            print(f"Slope (rate)  : {format_currency(pred['slope_per_day'], prod['platform'])} per day")
            print("-" * 60)
            
            print(f"Forecasted Price in 7 days  : {format_currency(pred['predicted_price_7d'], prod['platform'])}")
            print(f"Forecasted Price in 30 days : {format_currency(pred['predicted_price_30d'], prod['platform'])}")
            print("-" * 60)
            
            print(f"RECOMMENDATION: {pred['recommendation']}")
            print(f"REASON        : {pred['reason']}")
            
        else:
            print("-" * 60)
            print(f"RECOMMENDATION: {pred['recommendation']}")
            print(f"REASON        : {pred['reason']}")
            
    except Exception as e:
        print(f"[ERROR] Prediction failed: {str(e)}")

def handle_seed(agent, args):
    print_header(f"Seeding Mock History (ID {args.id})")
    try:
        msg = agent.seed_mock_price_history(args.id, days=args.days, trend_pattern=args.pattern)
        print(f"[SUCCESS] {msg}")
        print("Run 'python main.py predict <id>' to see the price forecasting analysis!")
    except Exception as e:
        print(f"[ERROR] Seeding failed: {str(e)}")

def handle_delete(agent, args):
    print_header(f"Deleting Product (ID {args.id})")
    prod = database.get_product(agent.db_path, args.id)
    if not prod:
        print(f"[ERROR] Product with ID {args.id} not found.")
        return
        
    database.delete_product(agent.db_path, args.id)
    print(f"[SUCCESS] Deleted product '{prod['title']}' and its price history.")

# ==============================================================================
# Upgraded Search & Compare Commands
# ==============================================================================

def execute_compare_by_urls(agent, urls, query="Custom URL Comparison"):
    print(f"\nCrawling and comparing {len(urls)} explicit product URLs...")
    results = []
    
    for idx, url in enumerate(urls, 1):
        print(f"[{idx}/{len(urls)}] Scraping {scraper.detect_platform(url)}: {url[:60]}...")
        try:
            # We track each of these in the database so history is created
            p_id, title, price, image_url, is_mock = agent.track_new_product(url)
            results.append({
                "title": title,
                "price": price,
                "platform": scraper.detect_platform(url),
                "image_url": image_url,
                "url": url
            })
        except Exception as e:
            # If live scraping is blocked, seed a mock item to maintain comparison capability
            print(f"  [Notice] Live scrape blocked/failed: {str(e)}. Seeding mock entry for comparison.")
            plat = scraper.detect_platform(url)
            title = f"Simulated {plat} Product"
            price = random.choice([1299.0, 1599.0, 1899.0, 2499.0])
            img = "https://images.unsplash.com/photo-1531403009284-440f080d1e12?w=500"
            p_id, title, price, image_url, is_mock = agent.track_new_product(
                url, force_title=title, force_price=price, force_image=img
            )
            results.append({
                "title": title,
                "price": price,
                "platform": plat,
                "image_url": image_url,
                "url": url
            })
            
    # Sort results
    results.sort(key=lambda x: x["price"] if x["price"] is not None else 9999999)
    
    # Print comparison table
    print_header("Comparison Results")
    print(f"{'Platform':<10} | {'Price':<12} | {'Title':<45}")
    print("-" * 75)
    for r in results:
        price_str = format_currency(r["price"], r["platform"])
        t_trunc = r["title"][:42] + "..." if len(r["title"]) > 45 else r["title"]
        print(f"{r['platform']:<10} | {price_str:<12} | {t_trunc:<45}")
    print("-" * 75)
    
    # Generate HTML
    agent.generate_comparison_report(query, results)

def execute_compare_by_query(agent, query):
    # Clean up sentence query patterns
    clean_query = re.sub(r'^(what is the price of|what is price of|check price of|compare price of|find|search|price of|price)\s+', '', query, flags=re.IGNORECASE)
    clean_query = clean_query.replace("?", "").strip()
    
    # Apply NLP mapping for brand defaults
    processed_query = agent.process_natural_language_query(clean_query)
    if processed_query != clean_query:
        print(f"Auto-mapped generic query to: '{processed_query}'")
        
    # Search platforms
    results, is_mocked = scraper.search_platforms(processed_query)
    
    # Add items to the tracking database for historical purposes
    for r in results:
        try:
            database.add_product(agent.db_path, r["url"], r["platform"], r["title"], None, r["image_url"])
            # Log this price point
            database.log_price(agent.db_path, database.add_product(agent.db_path, r["url"], r["platform"], r["title"]), r["price"])
        except Exception:
            pass
            
    # Print comparison table
    print_header(f"Comparison Results for '{processed_query}'")
    print(f"{'Platform':<10} | {'Price':<12} | {'Title':<45}")
    print("-" * 75)
    for r in results:
        price_str = format_currency(r["price"], r["platform"])
        t_trunc = r["title"][:42] + "..." if len(r["title"]) > 45 else r["title"]
        print(f"{r['platform']:<10} | {price_str:<12} | {t_trunc:<45}")
    print("-" * 75)
    
    # Generate HTML
    agent.generate_comparison_report(processed_query, results)

def run_interactive_loop(agent):
    print_header("E-Commerce Prompt Agent Shell")
    print("Welcome! Type what you want to compare or ask price queries.")
    print("Examples:")
    print("  - 'what is price of campus shoes'")
    print("  - 'what is price of Zara clothes'")
    print("  - 'compare: https://amazon.in/dp/xyz https://flipkart.com/p/abc'")
    print("Type 'exit' or 'quit' to close.")
    print("=" * 60)
    
    while True:
        try:
            prompt = input("\nAgent > ").strip()
            if not prompt:
                continue
            if prompt.lower() in ["exit", "quit", "q"]:
                print("Closing Agent Shell. Goodbye!")
                break
                
            # Check if prompt contains multiple URLs
            urls = re.findall(r'https?://[^\s\u200b]+', prompt)
            
            if urls:
                # Compare URLs
                execute_compare_by_urls(agent, urls, "Interactive URL Comparison")
            else:
                # Treat prompt as search query and call query execution directly
                execute_compare_by_query(agent, prompt)
                
        except KeyboardInterrupt:
            print("\nClosing Agent Shell. Goodbye!")
            break
        except Exception as e:
            print(f"[Error in Agent Process]: {str(e)}")

def handle_compare(agent, args):
    if args.interactive or (not args.query and not args.urls):
        run_interactive_loop(agent)
    elif args.urls:
        execute_compare_by_urls(agent, args.urls, args.query or "Product URL Comparison")
    elif args.query:
        execute_compare_by_query(agent, args.query)

# ==============================================================================
# Main Controller Entrypoint
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="E-commerce Price Tracker & Predictor Agent CLI")
    parser.add_argument("--db", default="prices.db", help="Path to SQLite database file")
    
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommands")
    
    # Subcommand add
    add_parser = subparsers.add_parser("add", help="Add a product URL to track")
    add_parser.add_argument("url", help="E-commerce product URL")
    add_parser.add_argument("--target", type=float, help="Target price to trigger alert")
    add_parser.add_argument("--mock-title", help="Specify a mock title instead of scraping")
    add_parser.add_argument("--mock-price", type=float, help="Specify an initial mock price instead of scraping")
    add_parser.add_argument("--mock-image", help="Specify a mock image url")
    
    # Subcommand list
    subparsers.add_parser("list", help="List all tracked products")
    
    # Subcommand update
    subparsers.add_parser("update", help="Update prices for all tracked products")
    
    # Subcommand history
    hist_parser = subparsers.add_parser("history", help="Show price logs history for a product")
    hist_parser.add_argument("id", type=int, help="Tracked product ID")
    
    # Subcommand predict
    pred_parser = subparsers.add_parser("predict", help="Predict price trend and get purchasing recommendation")
    pred_parser.add_argument("id", type=int, help="Tracked product ID")
    
    # Subcommand seed
    seed_parser = subparsers.add_parser("seed", help="Seed dummy price history for testing forecasting")
    seed_parser.add_argument("id", type=int, help="Tracked product ID")
    seed_parser.add_argument("--days", type=int, default=30, help="Number of days to backdate (default: 30)")
    seed_parser.add_argument("--pattern", choices=["downward", "upward", "volatile", "discount"], default="downward", 
                             help="Trend pattern style (default: downward)")
                             
    # Subcommand delete
    del_parser = subparsers.add_parser("delete", help="Remove product from tracker")
    del_parser.add_argument("id", type=int, help="Tracked product ID")

    # Upgraded Subcommand compare
    comp_parser = subparsers.add_parser("compare", help="Compare prices and images across platforms")
    comp_parser.add_argument("--query", "-q", help="Search query or prompt (e.g. 'what is price of clothes')")
    comp_parser.add_argument("--urls", "-u", nargs="+", help="Explicit URLs of products to compare")
    comp_parser.add_argument("--interactive", "-i", action="store_true", help="Launch interactive prompt agent shell")

    args = parser.parse_args()
    agent = PriceTrackerAgent(args.db)

    # Command Router
    if args.command == "add":
        handle_add(agent, args)
    elif args.command == "list":
        handle_list(agent, args)
    elif args.command == "update":
        handle_update(agent, args)
    elif args.command == "history":
        handle_history(agent, args)
    elif args.command == "predict":
        handle_predict(agent, args)
    elif args.command == "seed":
        handle_seed(agent, args)
    elif args.command == "delete":
        handle_delete(agent, args)
    elif args.command == "compare":
        handle_compare(agent, args)

if __name__ == "__main__":
    main()
