import asyncio
import aiohttp
from bs4 import BeautifulSoup
import pandas as pd
import json
import urllib.parse
from typing import Dict, Any
from playwright.async_api import async_playwright, ViewportSize

MAX_ITEMS = 10

# Define the social media domains we care about
TARGET_SOCIAL_DOMAINS = ['facebook.com', 'instagram.com', 'linkedin.com', 'twitter.com', 'x.com']


async def scrape_directory(query):
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport=ViewportSize(width=1280, height=800),
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        page = await context.new_page()

        print(f"\nStarting search for: {query}")

        import urllib.parse
        url_query = urllib.parse.quote_plus(query)
        await page.goto(f"https://www.google.com/maps/search/{url_query}")

        try:
            consent_button = page.locator('button:has-text("Reject all"), button:has-text("Accept all")')
            if await consent_button.count() > 0:
                await consent_button.first.click()
                await page.wait_for_timeout(1000)

            await page.wait_for_selector('div[role="feed"]', timeout=15000)
        except Exception:
            print("\nFailed to find the results feed. Check if the query yielded results or if blocked.")
            await browser.close()
            return results

        print("\nScrolling through results...")

        previously_counted = 0
        scroll_retries = 0
        max_scroll_retries = 3

        while True:
            cards = await page.locator('div[role="feed"] > div > div > a').all()
            currently_counted = len(cards)

            await page.evaluate("""
                const feed = document.querySelector('div[role="feed"]');
                if (feed) {
                    feed.scrollTo(0, feed.scrollHeight);
                }
            """)

            await asyncio.sleep(2.5)

            if currently_counted == previously_counted:
                scroll_retries += 1
                if scroll_retries >= max_scroll_retries:
                    print("\nReached the end of the feed or no more items loading.")
                    break
            else:
                scroll_retries = 0

            previously_counted = currently_counted

        print("\nExtracting business data...")
        listings = await page.locator('div[role="feed"] > div > div > a').all()

        count = 1
        for index, listing in enumerate(listings):
            if count > MAX_ITEMS:
                break

            try:
                name = await listing.get_attribute('aria-label')
                if not name:
                    continue

                print(f"\nProcessing Item {count}: {name}")

                await listing.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)

                # CRITICAL FIX: Use JavaScript click immediately to bypass invisible Google overlays
                await listing.evaluate("node => node.click()")

                match_found = False
                clean_name = name.strip().lower()

                # Poll every 500ms for up to 8 seconds (16 attempts)
                for attempt in range(16):
                    await page.wait_for_timeout(500)
                    h1_texts = await page.locator('h1:visible').all_inner_texts()

                    for text in h1_texts:
                        clean_text = text.strip().lower()
                        # Fuzzy match: check if the first 10 chars match
                        if len(clean_name) > 5 and (clean_name[:10] in clean_text or clean_text[:10] in clean_name):
                            match_found = True
                            break
                        elif clean_name == clean_text:
                            match_found = True
                            break

                    if match_found:
                        # Give the panel an extra 1.5s to fully render the phone/website DOM nodes
                        await page.wait_for_timeout(1500)
                        break

                if not match_found:
                    print(f"  [!] Panel took too long to load for '{name}'. Skipping to prevent saving false data.")
                    continue

                # Ensure variables are reset for this loop iteration
                website = None
                phone = None
                address = None
                maps_link = page.url

                # 1. Extract Website
                try:
                    web_loc = page.locator('a[data-item-id="authority"]:visible')
                    await web_loc.wait_for(state='visible', timeout=1500)
                    website = await web_loc.first.get_attribute('href')
                except Exception:
                    pass

                # 2. Extract Phone
                try:
                    phone_loc = page.locator('button[data-tooltip*="phone"]:visible div.fontBodyMedium')
                    await phone_loc.wait_for(state='visible', timeout=1500)
                    phone = await phone_loc.first.inner_text()
                except Exception:
                    pass

                # 3. Extract Address
                try:
                    addr_loc = page.locator('button[data-tooltip*="address"]:visible div.fontBodyMedium')
                    await addr_loc.wait_for(state='visible', timeout=1500)
                    address = await addr_loc.first.inner_text()
                except Exception:
                    pass

                business_data = {
                    "name": name,
                    "address": address,
                    "phone": phone,
                    "website": website,
                    "maps_link": maps_link
                }

                results.append(business_data)
                count += 1

            except Exception as e:
                print(f"\nError extracting item {index} ({name}): {e}")
                continue

        await browser.close()
        return results


async def fetch_social_links(session, url):
    if not url:
        return {domain.split('.')[0]: None for domain in TARGET_SOCIAL_DOMAINS}

    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    social_links: Dict[str, Any] = {domain.split('.')[0]: None for domain in TARGET_SOCIAL_DOMAINS}
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    try:
        async with session.get(url, headers=headers, timeout=10, ssl=False) as response:
            if response.status != 200:
                return social_links

            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')

            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href']
                for domain in TARGET_SOCIAL_DOMAINS:
                    if domain in href:
                        if 'sharer' not in href and 'tweet?' not in href:
                            social_links[domain.split('.')[0]] = href

            return social_links
    except Exception:
        return social_links


async def enrich_business_data(business_data_list):
    print("\nStarting Phase 2: Hunting for social media profiles...")
    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [fetch_social_links(session, biz.get('website')) for biz in business_data_list]
        results = await asyncio.gather(*tasks)

        for i, business in enumerate(business_data_list):
            business['social_profiles'] = results[i]

    return business_data_list


def export_to_json(data, filename="output.json"):
    print(f"\nExporting to {filename}...")
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        print("\nJSON export complete!")
    except Exception as e:
        print(f"\nError saving JSON: {e}")


def export_to_csv(data, filename="output.csv"):
    print(f"\nExporting to {filename}...")
    try:
        if not data:
            print("\nNo data to export.")
            return

        df = pd.DataFrame(data)

        if 'social_profiles' in df.columns:
            social_df = df['social_profiles'].apply(
                lambda x: pd.Series(x) if isinstance(x, dict) else pd.Series()
            )
            df = pd.concat([df.drop(['social_profiles'], axis=1), social_df], axis=1)

        df.to_csv(filename, index=False, encoding='utf-8')
        print("\nCSV export complete!")
    except Exception as e:
        print(f"Error saving CSV: {e}")


async def main():
    target_query = input("Enter query: ")

    print("\n--- Phase 1: Directory Scraping ---")
    raw_business_data = await scrape_directory(target_query)

    if not raw_business_data:
        print("\nNo businesses found or scraper failed. Exiting.")
        return

    print("\n--- Phase 2: Social Media Enrichment ---")
    enriched_data = await enrich_business_data(raw_business_data)

    print("\n--- Phase 3: File Export ---")
    export_to_json(enriched_data, filename="output.json")
    export_to_csv(enriched_data, filename="output.csv")

    print("\nScraping pipeline finished successfully.")


if __name__ == "__main__":
    asyncio.run(main())
