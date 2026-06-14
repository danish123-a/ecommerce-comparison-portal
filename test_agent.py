import os
import unittest
import database
import predictor
import scraper
from agent import PriceTrackerAgent

TEST_DB = "test_prices.db"

class TestPriceTrackerAgent(unittest.TestCase):
    def setUp(self):
        # Remove old test DB if it exists
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except PermissionError:
                pass
        self.agent = PriceTrackerAgent(TEST_DB)

    def tearDown(self):
        # Cleanup database connection and file
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except PermissionError:
                pass

    def test_database_init(self):
        """Verifies database initializes tables successfully."""
        products = database.get_all_products(TEST_DB)
        self.assertEqual(len(products), 0)

    def test_scraper_detection(self):
        """Verifies URL platform detection works correctly."""
        self.assertEqual(scraper.detect_platform("https://www.amazon.in/dp/B0BY8MCQ9S"), "Amazon")
        self.assertEqual(scraper.detect_platform("https://www.flipkart.com/apple-iphone-15/p/itm"), "Flipkart")
        self.assertEqual(scraper.detect_platform("https://example.com/product"), "Generic")

    def test_price_cleaning(self):
        """Verifies currency cleaning functions."""
        self.assertEqual(scraper.clean_price("₹1,24,999.00"), 124999.0)
        self.assertEqual(scraper.clean_price("$499.95"), 499.95)
        self.assertEqual(scraper.clean_price("   99   "), 99.0)

    def test_linear_regression(self):
        """Verifies regression math in predictor."""
        # Simple line: y = 2x + 10
        x = [0.0, 1.0, 2.0, 3.0, 4.0]
        y = [10.0, 12.0, 14.0, 16.0, 18.0]
        slope, intercept = predictor.calculate_linear_regression(x, y)
        self.assertAlmostEqual(slope, 2.0)
        self.assertAlmostEqual(intercept, 10.0)

    def test_agent_track_mock_product(self):
        """Verifies product creation and status tracking."""
        url = "https://www.amazon.in/test-product"
        prod_id, title, price, image_url, is_mock = self.agent.track_new_product(
            url, target_price=500.0, force_title="Test Phone", force_price=600.0
        )
        self.assertTrue(is_mock)
        self.assertEqual(title, "Test Phone")
        self.assertEqual(price, 600.0)
        
        prod = database.get_product(TEST_DB, prod_id)
        self.assertIsNotNone(prod)
        self.assertEqual(prod['title'], "Test Phone")
        self.assertEqual(prod['target_price'], 500.0)
        self.assertEqual(prod['latest_price'], 600.0)

    def test_predictor_downward_trend(self):
        """Verifies downward price trend recommendation."""
        # Setup product
        url = "https://www.amazon.in/test-product"
        prod_id, _, _, _, _ = self.agent.track_new_product(
            url, target_price=500.0, force_title="Test Item", force_price=1000.0
        )
        
        # Seed downward pattern
        self.agent.seed_mock_price_history(prod_id, days=10, trend_pattern="downward")
        
        # Get prediction
        res = self.agent.get_prediction(prod_id)
        prediction = res['prediction']
        
        self.assertEqual(prediction['status'], 'success')
        self.assertEqual(prediction['recommendation'], 'WAIT')
        self.assertTrue("trending down" in prediction['reason'].lower() or "trending downwards" in prediction['reason'].lower())

    def test_predictor_buy_at_lowest(self):
        """Verifies recommendation when price is near historical low."""
        # Setup product
        url = "https://www.amazon.in/test-product"
        prod_id, _, _, _, _ = self.agent.track_new_product(
            url, target_price=None, force_title="Test Item", force_price=800.0
        )
        
        # Clear default first log so we have absolute control over the sequence
        conn = database.get_connection(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM price_history WHERE product_id = ?", (prod_id,))
        conn.commit()
        conn.close()
        
        # Log stable prices near historic low
        import datetime
        base_time = datetime.datetime.now() - datetime.timedelta(days=5)
        prices = [800.0, 800.0, 801.0, 800.0, 802.0, 801.0]
        
        for i, p in enumerate(prices):
            ts = (base_time + datetime.timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")
            database.log_price(TEST_DB, prod_id, p, timestamp=ts)
        
        res = self.agent.get_prediction(prod_id)
        prediction = res['prediction']
        
        self.assertEqual(prediction['recommendation'], 'BUY')
        self.assertTrue("lowest recorded price" in prediction['reason'].lower())

    def test_brand_default_mapping(self):
        """Verifies generic categories are mapped to default premium brands."""
        self.assertEqual(self.agent.process_natural_language_query("what is the price of shoes"), "Campus Shoes")
        self.assertEqual(self.agent.process_natural_language_query("check price of clothes"), "Zara Clothes")
        self.assertEqual(self.agent.process_natural_language_query("compare price of phone"), "Apple Phone")
        # Ensure specific brand queries are NOT changed
        self.assertEqual(self.agent.process_natural_language_query("what is price of nike sneakers"), "what is price of nike sneakers")

    def test_mock_search_results(self):
        """Verifies mock search returns results for platforms."""
        results = scraper.get_mock_search_results("shoes")
        # 10 items for Amazon, 10 for Flipkart, 10 for Meesho, 10 for Google = 40 total
        self.assertEqual(len(results), 40)
        platforms = [r['platform'] for r in results]
        self.assertIn("Amazon", platforms)
        self.assertIn("Flipkart", platforms)
        self.assertIn("Meesho", platforms)
        self.assertIn("Google", platforms)
        self.assertIsNotNone(results[0]['image_url'])
        self.assertIsNotNone(results[0]['price'])

    def test_html_report_generation(self):
        """Verifies HTML comparison report is created successfully."""
        results = [
            {"title": "Test Shoes Amazon", "price": 1200.0, "platform": "Amazon", "image_url": "http://img.com/1", "url": "http://amazon.com/1"},
            {"title": "Test Shoes Flipkart", "price": 1100.0, "platform": "Flipkart", "image_url": "http://img.com/2", "url": "http://flipkart.com/2"}
        ]
        report_file = "test_comparison_report.html"
        
        # Remove old test report if exists
        if os.path.exists(report_file):
            try:
                os.remove(report_file)
            except Exception:
                pass
            
        try:
            # We bypass opening browser inside test by overriding webbrowser.open to no-op
            import webbrowser
            original_open = webbrowser.open
            webbrowser.open = lambda url: True
            
            self.agent.generate_comparison_report("Test Shoes", results, output_file=report_file)
            
            # Restore
            webbrowser.open = original_open
            
            self.assertTrue(os.path.exists(report_file))
            with open(report_file, "r", encoding="utf-8") as f:
                content = f.read()
                self.assertIn("Test Shoes", content)
                self.assertIn("Test Shoes Flipkart", content)
                self.assertIn("Test Shoes Amazon", content)
                self.assertIn("₹1,100.00", content)
        finally:
            if os.path.exists(report_file):
                try:
                    os.remove(report_file)
                except Exception:
                    pass

if __name__ == '__main__':
    unittest.main()
