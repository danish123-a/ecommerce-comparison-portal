from datetime import datetime

def parse_timestamp(ts_str):
    """Parses standard SQLite timestamps into datetime objects."""
    # Try common formats
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(ts_str, fmt)
        except ValueError:
            continue
    # Fallback to date only
    try:
        return datetime.strptime(ts_str[:10], "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Could not parse timestamp format: {ts_str}")

def calculate_linear_regression(x, y):
    """
    Calculates the slope and intercept of a simple linear regression line.
    y = m * x + c
    """
    n = len(x)
    if n < 2:
        return 0.0, 0.0

    sum_x = sum(x)
    sum_y = sum(y)
    sum_xx = sum(xi * xi for xi in x)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))

    denominator = (n * sum_xx) - (sum_x * sum_x)
    if denominator == 0:
        return 0.0, sum_y / n  # Flat slope, average value

    slope = (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept

def predict_price(price_history, target_price=None):
    """
    Analyzes historical price logs and provides prediction and buying recommendations.
    price_history: list of dicts with keys 'price' and 'timestamp'
    target_price: float, optional target price set by user
    """
    n = len(price_history)
    
    if n == 0:
        return {
            "status": "error",
            "message": "No price history available."
        }
        
    prices = [h['price'] for h in price_history]
    timestamps = [parse_timestamp(h['timestamp']) for h in price_history]
    
    current_price = prices[-1]
    min_price = min(prices)
    max_price = max(prices)
    avg_price = sum(prices) / n
    
    if n < 3:
        # Recommendations with minimal data
        recommendation = "NEEDS_DATA"
        reason = f"Only {n} price data point(s) recorded. We need at least 3 data points over different intervals to determine price trends."
        
        # If target price exists and current is below it, we can still advise buying
        if target_price and current_price <= target_price:
            recommendation = "BUY"
            reason = f"Current price ({current_price}) is below your target price ({target_price})! Buy now."
            
        return {
            "status": "insufficient_data",
            "data_points_count": n,
            "current_price": current_price,
            "min_price": min_price,
            "max_price": max_price,
            "avg_price": avg_price,
            "recommendation": recommendation,
            "reason": reason
        }

    # Normalize timestamps to days elapsed since the first record
    start_time = timestamps[0]
    days_elapsed = [(t - start_time).total_seconds() / 86400.0 for t in timestamps]
    
    # Calculate regression line (price vs. days elapsed)
    slope, intercept = calculate_linear_regression(days_elapsed, prices)
    
    # Predict prices in 7 days and 30 days
    last_day = days_elapsed[-1]
    pred_7d = max(0.0, slope * (last_day + 7) + intercept)
    pred_30d = max(0.0, slope * (last_day + 30) + intercept)
    
    # Price change percent from first to last
    price_change_pct = ((current_price - prices[0]) / prices[0]) * 100.0 if prices[0] > 0 else 0.0
    
    # Decision Matrix for recommendation:
    # 1. Target price threshold
    if target_price and current_price <= target_price:
        recommendation = "BUY"
        reason = f"RECOMMENDED: The current price is {current_price:.2f}, which is below or equal to your target price of {target_price:.2f}."
    # 2. Currently at/near lowest price
    elif current_price <= min_price * 1.02:
        # Near historic low
        if slope < -0.1:
            recommendation = "WAIT"
            reason = "HOLD / WAIT: Although the price is near a historical low, it is still actively trending downwards. It may drop further."
        else:
            recommendation = "BUY"
            reason = f"RECOMMENDED: The price is currently at or near its lowest recorded price ({min_price:.2f}). Excellent time to buy!"
    # 3. Trending downwards significantly
    elif slope < -1.0:  # Dropping more than $1 / Rs 1 per day on average
        recommendation = "WAIT"
        reason = f"HOLD / WAIT: The price is trending down by approximately {abs(slope):.2f} units per day. Waiting for it to bottom out is advised."
    # 4. Trending upwards significantly and currently above minimum
    elif slope > 1.0 and current_price > min_price * 1.05:
        recommendation = "WAIT"
        reason = f"HOLD / WAIT: The price is trending upwards (+{slope:.2f}/day) and is currently {((current_price - min_price)/min_price)*100:.1f}% higher than its recorded low. Wait for a pullback/discount."
    # 5. Flat/stable trend
    else:
        if current_price < avg_price:
            recommendation = "BUY"
            reason = f"RECOMMENDED: Price is stable and currently below the average historical price ({avg_price:.2f})."
        else:
            recommendation = "WAIT"
            reason = f"HOLD / WAIT: Price is stable but currently slightly higher than the average historical price ({avg_price:.2f})."

    return {
        "status": "success",
        "data_points_count": n,
        "current_price": current_price,
        "min_price": min_price,
        "max_price": max_price,
        "avg_price": avg_price,
        "price_change_pct": price_change_pct,
        "slope_per_day": slope,
        "predicted_price_7d": pred_7d,
        "predicted_price_30d": pred_30d,
        "recommendation": recommendation,
        "reason": reason
    }
