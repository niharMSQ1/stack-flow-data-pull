import json
import re
from pathlib import Path
from urllib.parse import urlparse
from .models import *
from playwright.async_api import async_playwright
import requests
import json
from django.utils.timezone import now
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

RAW_JSON_PATH = "raw_cert_links.json"
OUTPUT_DIR = Path("sections_output")
OUTPUT_DIR.mkdir(exist_ok=True)

uuid_pattern = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE
)

def load_existing_jsons():
    existing_data = []
    for json_file in OUTPUT_DIR.glob("*.json"):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                existing_data.append(data)
        except Exception as e:
            print(f"⚠️ Failed to read {json_file}: {e}")
    return existing_data

def extract_filename_from_url(url: str) -> str:
    path_parts = urlparse(url).path.strip("/").split("/")
    if path_parts and uuid_pattern.match(path_parts[-1]):
        path_parts.pop()
    filename = path_parts[-1] if path_parts else "section"
    return f"{filename}.json"

async def get_cert_links(page):
    await page.goto("https://trust.trustcloud.ai/certifications")
    await page.wait_for_selector('a[href^="/certifications/"]')
    cert_links = await page.eval_on_selector_all(
        'a[href^="/certifications/"]',
        'elements => elements.map(el => el.href)'
    )
    cert_links = list(set(cert_links))
    raw_data = [{"title": "TrustShare", "url": link, "items": []} for link in cert_links]
    with open(RAW_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(raw_data, f, indent=2)
    print(f"✅ Found and saved {len(cert_links)} certification links.")
    return raw_data

async def capture_sections_for_all_links():
    existing_jsons = load_existing_jsons()
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        links = await get_cert_links(page)

        for idx, entry in enumerate(links, 1):
            url = entry["url"]
            print(f"\n🔗 ({idx}/{len(links)}) Visiting: {url}")
            sub_page = await context.new_page()
            found = False

            async def handle_response(response):
                nonlocal found
                try:
                    if "sections" in response.url:
                        found = True
                        print(f"✅ Found 'sections' API: {response.url}")
                        json_data = await response.json()
                        if json_data in existing_jsons:
                            print("⛔ Duplicate data found. Skipping save.")
                            return
                        filename = extract_filename_from_url(url)
                        output_path = OUTPUT_DIR / filename
                        with open(output_path, "w", encoding="utf-8") as f:
                            json.dump(json_data, f, ensure_ascii=False, indent=2)
                        print(f"📁 Saved JSON to {output_path}")
                        existing_jsons.append(json_data)
                        results.append({
                            "url": url,
                            "data": json_data
                        })
                except Exception as e:
                    print(f"⚠️ Error processing response: {e}")

            sub_page.on("response", handle_response)

            try:
                await sub_page.goto(url, timeout=30000)
                await sub_page.wait_for_timeout(8000)
            except Exception as e:
                print(f"⚠️ Failed to load {url}: {e}")
            finally:
                if not found:
                    print("❌ No 'sections' API found for this URL.")
                await sub_page.close()

        await browser.close()

    return results


MAX_POLICIES = 26
BASE_URL = "https://www.eramba.org/api/proxy?endpoint=security-policies&action=show&id={}"

def html_to_json(html_content):
    """Convert HTML policy content to structured JSON"""
    soup = BeautifulSoup(html_content, 'html.parser')
    result = {}
    current_section = None
    
    # Remove all <br> tags and replace with newlines
    for br in soup.find_all('br'):
        br.replace_with('\n')
    
    for element in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'ul', 'li']):
        if element.name.startswith('h'):
            # New section found
            current_section = element.get_text().strip()
            result[current_section] = []
        elif current_section:
            if element.name == 'ul':
                # Handle unordered lists
                list_items = [li.get_text().strip() for li in element.find_all('li')]
                result[current_section].extend(list_items)
            elif element.name == 'li':
                # Handle standalone list items
                result[current_section].append(element.get_text().strip())
            elif element.name == 'p':
                # Handle paragraphs
                text = element.get_text().strip()
                if text:
                    result[current_section].append(text)
    
    # Convert lists to strings with bullet points
    for section, content in result.items():
        if all(isinstance(item, str) for item in content):
            result[section] = '\n'.join(f"• {item}" if i > 0 and not item.startswith('•') else item 
                                      for i, item in enumerate(content))
    
    return result

def fetch_policy(policy_id):
    """Fetch and process a single policy"""
    try:
        url = BASE_URL.format(policy_id)
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            try:
                # Try to parse as JSON first
                data = response.json()
                if isinstance(data, dict):
                    return data
                # If not JSON, treat as HTML
                return {
                    "title": "IT Security Policy",  # Default title
                    "description": html_to_json(response.text)
                }
            except ValueError:
                return {
                    "title": "IT Security Policy",
                    "description": html_to_json(response.text)
                }
    except Exception:
        return None
    return None

def fetch_policies_parallel():
    """Fetch policies in parallel"""
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_policy, i): i for i in range(10, 101)}
        
        policies = []
        for future in as_completed(futures):
            policy_data = future.result()
            if policy_data:
                policies.append(policy_data)
                if len(policies) >= MAX_POLICIES:
                    for f in futures:
                        f.cancel()
                    break
    return policies


def ingest_policies_from_eramba(api_url):
    response = requests.get(api_url)
    if response.status_code != 200:
        return {"success": False, "message": f"Failed to fetch data. Status code: {response.status_code}"}

    data = response.json().get("data", [])
    created, updated = 0, 0

    for item in data:
        title = item.get("index", "").strip()
        description = item.get("description", "").strip()
        policy_id = f"ER-{item.get('id')}"
        version = item.get("version", "")
        reference = f"{policy_id}-{version}"

        if not title or not reference:
            continue

        try:
            policy = Policy.objects.get(title=title)
            policy.policy_template = description  # update only the policy_template
            policy.updated_at = now()
            policy.save()
            updated += 1
        except Policy.DoesNotExist:
            Policy.objects.create(
                policy_id=policy_id,
                title=title,
                policy_version=version,
                policy_reference=reference,
                policy_template=description,
                policy_gathered_from="ER"
            )
            created += 1

    return {
        "success": True,
        "message": f"✅ Ingestion completed.",
        "created": created,
        "updated": updated,
        "total": len(data)
    }