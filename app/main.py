from typing import Dict, Any

from playwright.async_api import async_playwright, ViewportSize
import asyncio
import aiohttp
from bs4 import BeautifulSoup
import pandas as pd
import json


# Define the social media domains we care about
TARGET_SOCIAL_DOMAINS = ['facebook.com', 'instagram.com', 'linkedin.com', 'twitter.com', 'x.com']

async def scrape_directory(query):
    results = []

    async with async_playwright() as p:
        # Launching with headless=False so you can visually debug the scrolling
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport=ViewportSize(width= 1280, height= 800),
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        page = await context.new_page()

        print(f"Starting search for: {query}")
        # Format the query for the URL
        url_query = query.replace(' ', '+')
        await page.goto(f"https://www.google.com/maps/search/{url_query}")

        # Wait for the results feed to load
        # Google Maps heavily obfuscates classes, so we rely on ARIA roles where possible
        try:
            await page.wait_for_selector('div[role="feed"]', timeout=10000)
        except Exception:
            print("Failed to find the results feed. The page structure might have changed or loaded too slowly.")
            await browser.close()
            return results

        print("Scrolling through results...")

        # --- The Infinite Scroll Logic ---
        # We need to target the scrollable div and push it down until the end marker appears
        previously_counted = 0
        while True:
            # Count current loaded listings
            cards = await page.locator('div[role="feed"] > div > div > a').all()
            currently_counted = len(cards)

            # Scroll the feed element
            await page.evaluate("""
                const feed = document.querySelector('div[role="feed"]');
                if (feed) {
                    feed.scrollTo(0, feed.scrollHeight);
                }
            """)

            # Allow time for network requests to fetch new results
            await asyncio.sleep(2)

            # Check if we've hit the "You've reached the end of the list" text
            # This text changes by region/language, so looking for the specific DOM node is safer,
            # but for simplicity, we break if no new items loaded after a few tries.
            if currently_counted == previously_counted:
                print("No new items loaded. Reached the end of the feed.")
                break

            previously_counted = currently_counted

        # --- The Extraction Logic ---
        print("Extracting business data...")
        # Get all the clickable business cards in the feed
        listings = await page.locator('div[role="feed"] > div > div > a').all()

        for index, listing in enumerate(listings):
            try:
                # 1. Get the Business Name from the aria-label
                name = await listing.get_attribute('aria-label')

                # 2. To get deeper details (website, phone), we actually need to click the listing
                # and read the side-panel that opens up.
                await listing.click()
                await page.wait_for_timeout(1500)  # Wait for the detail panel to animate in

                # Note: These selectors are highly volatile and often change on Maps.
                # In a production environment, you'd use more robust XPath or text-contains selectors.

                # Extract URL (Looking for the globe icon or 'Website' text)
                website_element = page.locator('a[data-item-id="authority"]')
                website = await website_element.get_attribute('href') if await website_element.count() > 0 else None

                # Extract Phone Number (Looking for 'Phone' icon or text pattern)
                phone_element = page.locator('button[data-tooltip="Copy phone number"] div.fontBodyMedium')
                phone = await phone_element.inner_text() if await phone_element.count() > 0 else None

                # Extract Address
                address_element = page.locator('button[data-tooltip="Copy address"] div.fontBodyMedium')
                address = await address_element.inner_text() if await address_element.count() > 0 else None

                business_data = {
                    "name": name,
                    "address": address,
                    "phone": phone,
                    "website": website
                }

                print(f"Extracted: {name}")
                results.append(business_data)

            except Exception as e:
                print(f"Error extracting item {index}: {e}")
                continue

        await browser.close()
        return results

async def fetch_social_links(session, url):
    """Fetches a single URL and extracts social media links."""
    if not url:
        return {}

    # Standardize URL (add http if missing to prevent connection errors)
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    social_links:Dict[str,Any] = {domain.split('.')[0]: None for domain in TARGET_SOCIAL_DOMAINS}

    # We use a standard browser User-Agent so small business firewalls don't block us
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    try:
        # 10-second timeout. If a local business site takes longer, it's likely dead.
        async with session.get(url, headers=headers, timeout=10, ssl=False) as response:
            if response.status != 200:
                return social_links

            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')

            # Find all anchor tags with href attributes
            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href']

                # Check if the href contains any of our target domains
                for domain in TARGET_SOCIAL_DOMAINS:
                    if domain in href:
                        # Basic cleanup: Ignore generic share links
                        if 'sharer' not in href and 'tweet?' not in href:
                            social_links[domain.split('.')[0]] = href

            return social_links

    except Exception as e:
        # We silently catch exceptions (timeouts, DNS errors) to keep the pipeline moving
        print(f"  [!] Could not fetch {url}: {type(e).__name__}")
        return social_links

async def enrich_business_data(business_data_list):
    """Processes the list of businesses and adds social links to each."""
    print("\nStarting Phase 2: Hunting for social media profiles...")

    # We use a TCPConnector to limit concurrent connections and avoid crashing our local network
    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = []
        for business in business_data_list:
            # Create a task for each website
            task = fetch_social_links(session, business.get('website'))
            tasks.append(task)

        # Run all tasks concurrently
        results = await asyncio.gather(*tasks)

        # Merge the results back into the original data
        for i, business in enumerate(business_data_list):
            business['social_profiles'] = results[i]

    return business_data_list

def export_to_json(data, filename="tiles_distributors.json"):
    """Saves the raw, nested list of dictionaries directly to a JSON file."""
    print(f"\nExporting to {filename}...")
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        print("JSON export complete!")
    except Exception as e:
        print(f"Error saving JSON: {e}")

def export_to_csv(data, filename="tiles_distributors.csv"):
    """Flattens the nested data and exports it to a clean CSV file."""
    print(f"Exporting to {filename}...")
    try:
        if not data:
            print("No data to export.")
            return

        df = pd.DataFrame(data)

        # We must flatten the 'social_profiles' dictionary into separate columns.
        # We use a lambda to ensure that even if a row has no social profiles (None),
        # it doesn't crash the Pandas Series conversion.
        if 'social_profiles' in df.columns:
            social_df = df['social_profiles'].apply(
                lambda x: pd.Series(x) if isinstance(x, dict) else pd.Series()
            )
            # Combine the original dataframe (minus the nested column) with the new flat columns
            df = pd.concat([df.drop(['social_profiles'], axis=1), social_df], axis=1)

        df.to_csv(filename, index=False, encoding='utf-8')
        print("CSV export complete!")
    except Exception as e:
        print(f"Error saving CSV: {e}")

async def main():
    target_query = "Tiles distributors in Rajkot"

    # 1. Phase 1: The Directory Crawler (Playwright)
    print("--- Phase 1: Directory Scraping ---")
    raw_business_data = await scrape_directory(target_query)

    # Check if Phase 1 found anything before proceeding
    if not raw_business_data:
        print("No businesses found or scraper failed. Exiting.")
        return

    # 2. Phase 2: The Social Media Hunter (aiohttp + BeautifulSoup)
    print("\n--- Phase 2: Social Media Enrichment ---")
    enriched_data = await enrich_business_data(raw_business_data)

    # 3. Phase 3: Direct File Export
    print("\n--- Phase 3: File Export ---")
    # Save a JSON backup just in case the CSV flattening drops anything weird
    export_to_json(enriched_data, filename="output.json")

    # Save the final, analytical CSV
    export_to_csv(enriched_data, filename="output.csv")

    print("\nScraping pipeline finished successfully.")

if __name__ == "__main__":
    asyncio.run(main())