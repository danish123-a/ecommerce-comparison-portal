---
title: E-Commerce Price Finder & Multi-Platform Portal
emoji: 🤖
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
license: mit
---

# E-Commerce Price Finder & Multi-Platform Portal

A premium e-commerce intelligence agent that scrapes search listings from major online platforms (Amazon, Flipkart, Meesho) in parallel, extracts valid coupon/promo codes, offers a visual comparison catalog, and calculates linear regression trend forecasting.

## Features
- **Parallel Scrapers:** Simultaneously queries Amazon (with human-nature stealth browsing), Flipkart, and Meesho.
- **Single Page Analyzer:** Deep analysis of products, price statistics (minimum, maximum, average), and unexpired promo code extraction.
- **Interactive Portal:** 3-column side-by-side platform view with fullscreen toggle on click/double-tap.
- **Price Forecasting:** Standard deviation and linear regression forecast for 7-day and 30-day price trends.

## Deployment on Hugging Face Spaces
This app is ready to deploy directly on Hugging Face Spaces using Docker.

1. Create a new Space on [Hugging Face](https://huggingface.co/new-space).
2. Set the **SDK** type to **Docker** (leave the template blank).
3. Push/upload these files to your Space's repository.
4. Hugging Face will automatically build the container using the provided `Dockerfile` and launch the interface.
