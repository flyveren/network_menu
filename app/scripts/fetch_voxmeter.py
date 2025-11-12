#!/usr/bin/env python3
"""Scrape political polling data from Voxmeter."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from time import sleep

try:
    from bs4 import BeautifulSoup
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError as e:
    print(f"[ERROR] Missing required dependency: {e}", file=sys.stderr)
    print("[INFO] Install with: pip install beautifulsoup4 selenium webdriver-manager", file=sys.stderr)
    sys.exit(1)


DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def setup_chrome_driver(headless: bool = True) -> webdriver.Chrome:
    """Set up and return a Chrome WebDriver instance."""
    import os
    from pathlib import Path
    
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    # Use system Chromium/ChromeDriver if available (Alpine Linux)
    chromedriver_path = None
    chromium_path = None
    
    # Check for system chromedriver (Alpine)
    for path in ["/usr/bin/chromedriver", "/usr/local/bin/chromedriver"]:
        if Path(path).exists():
            chromedriver_path = path
            break
    
    # Check for system chromium/chrome binary (Alpine)
    for path in ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"]:
        if Path(path).exists():
            chromium_path = path
            chrome_options.binary_location = path
            break
    
    if chromedriver_path:
        service = Service(chromedriver_path)
    else:
        # Fallback to ChromeDriverManager
        service = Service(ChromeDriverManager().install())
    
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.maximize_window()
    return driver


def scrape_voxmeter_polls(driver: webdriver.Chrome | None = None) -> dict[str, float | str]:
    """Scrape current polling data from Voxmeter."""
    url = "https://voxmeter.dk/meningsmalinger/"
    
    print(f"[INFO] Fetching data from: {url}", flush=True)
    
    use_selenium = driver is not None
    if not use_selenium:
        driver = setup_chrome_driver(headless=True)
    
    try:
        driver.get(url)
        sleep(3)  # Wait for initial load
        
        # Try to accept cookies if present
        try:
            accept_button = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Accepter') or contains(text(), 'Accept')]"))
            )
            accept_button.click()
            sleep(1)
        except:
            pass  # No cookie banner or already accepted
        
        # Wait for content to load
        try:
            WebDriverWait(driver, 20).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except:
            pass
        
        # Wait longer for JavaScript to render charts (charts load via external JS)
        sleep(10)
        
        # Scroll down to ensure charts are rendered
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
        sleep(3)
        driver.execute_script("window.scrollTo(0, 0);")
        sleep(3)
        
        # Wait a bit more for any lazy-loaded content
        sleep(5)
        
        # Initialize result dictionary
        result = {
            "red_bloc": None,
            "blue_bloc": None,
            "date": datetime.now(timezone.utc).isoformat(),
            "source": "voxmeter.dk"
        }
        
        # Strategy 1: Wait for specific text elements to appear
        try:
            print("[INFO] Waiting for chart data to load...", flush=True)
            # Wait for "Rød blok" text to appear (indicates charts are loaded)
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'Rød blok')]"))
            )
            print("[INFO] Chart labels found, extracting data...", flush=True)
            sleep(2)  # Give a bit more time for percentages to render
        except Exception as e:
            print(f"[WARNING] Timeout waiting for chart labels: {e}", flush=True)
        
        # Strategy 2: Try to get data directly from JavaScript/DOM using multiple methods
        try:
            # Method A: Find all text nodes and search for the pattern
            js_code_a = """
            (function() {
                var result = {red_bloc: null, blue_bloc: null};
                var bodyText = document.body.innerText || document.body.textContent;
                
                // Find the second occurrence of "I dag" (second chart set)
                var parts = bodyText.split('I dag');
                if (parts.length >= 3) {
                    // Get the second "I dag" section (index 2)
                    var secondSection = parts[2].substring(0, 800);
                    
                    // Check if it contains "Rød blok" and "Blå blok" but NOT "Rød opposition"
                    if (secondSection.includes('Rød blok') && secondSection.includes('Blå blok') && 
                        !secondSection.includes('Rød opposition') && !secondSection.includes('Regeringen')) {
                        
                        // Find percentages - look for pattern: number% ... number%
                        var pctRegex = /(\\d+[,.]?\\d*)\\s*%[\\s\\S]{0,150}?(\\d+[,.]?\\d*)\\s*%/g;
                        var pctMatch = pctRegex.exec(secondSection);
                        
                        if (pctMatch && pctMatch[1] && pctMatch[2]) {
                            result.red_bloc = parseFloat(pctMatch[1].replace(',', '.'));
                            result.blue_bloc = parseFloat(pctMatch[2].replace(',', '.'));
                        } else {
                            // Alternative: find all percentages and take first two
                            var allPcts = secondSection.match(/(\\d+[,.]?\\d*)\\s*%/g);
                            if (allPcts && allPcts.length >= 2) {
                                result.red_bloc = parseFloat(allPcts[0].replace(/[%,]/g, '').replace(',', '.'));
                                result.blue_bloc = parseFloat(allPcts[1].replace(/[%,]/g, '').replace(',', '.'));
                            }
                        }
                    }
                }
                
                return result;
            })();
            """
            
            js_result = driver.execute_script(js_code_a)
            print(f"[DEBUG] JavaScript method A result: {js_result}", flush=True)
            
            if js_result and js_result.get("red_bloc") and js_result.get("blue_bloc"):
                result["red_bloc"] = js_result["red_bloc"]
                result["blue_bloc"] = js_result["blue_bloc"]
                print(f"[INFO] Found polling data via JavaScript method A: Rød blok: {result['red_bloc']}%, Blå blok: {result['blue_bloc']}%", flush=True)
            else:
                # Method B: Use XPath to find elements containing the percentages
                print("[INFO] Trying method B: XPath element search...", flush=True)
                try:
                    # Find element containing "Rød blok"
                    red_bloc_elem = driver.find_element(By.XPATH, "//*[contains(text(), 'Rød blok')]")
                    # Get parent container - try multiple strategies
                    parent = None
                    try:
                        parent = red_bloc_elem.find_element(By.XPATH, "./ancestor::div[contains(@class, 'elementor')][1]")
                    except:
                        try:
                            parent = red_bloc_elem.find_element(By.XPATH, "./ancestor::div[1]")
                        except:
                            parent = red_bloc_elem.find_element(By.XPATH, "./..")
                    
                    parent_text = parent.text if parent else ""
                    
                    # Find "I dag" in this parent and extract percentages after it
                    if "I dag" in parent_text:
                        # Split by "I dag" and get the part that contains percentages
                        parts = parent_text.split("I dag")
                        for part in parts[1:]:  # Skip first part
                            if "Rød blok" in part and "Blå blok" in part and "Rød opposition" not in part:
                                percentages = re.findall(r'(\d+[,.]?\d*)\s*%', part[:300])
                                if len(percentages) >= 2:
                                    result["red_bloc"] = float(percentages[0].replace(",", "."))
                                    result["blue_bloc"] = float(percentages[1].replace(",", "."))
                                    print(f"[INFO] Found polling data via XPath: Rød blok: {result['red_bloc']}%, Blå blok: {result['blue_bloc']}%", flush=True)
                                    break
                except Exception as e:
                    print(f"[WARNING] XPath method failed: {e}", flush=True)
                    
                    # Method C: Search for SVG or canvas elements that might contain chart data
                    print("[INFO] Trying method C: SVG/Canvas search...", flush=True)
                    try:
                        # Look for SVG elements (charts are often rendered as SVG)
                        svg_elements = driver.find_elements(By.TAG_NAME, "svg")
                        print(f"[DEBUG] Found {len(svg_elements)} SVG elements", flush=True)
                        for svg in svg_elements:
                            svg_text = svg.text
                            if "I dag" in svg_text and "Rød blok" in svg_text and "Blå blok" in svg_text:
                                percentages = re.findall(r'(\d+[,.]?\d*)\s*%', svg_text)
                                if len(percentages) >= 2:
                                    # Find the "I dag" section
                                    parts = svg_text.split("I dag")
                                    if len(parts) >= 2:
                                        for part in parts[1:]:
                                            if "Rød blok" in part and "Blå blok" in part:
                                                part_percentages = re.findall(r'(\d+[,.]?\d*)\s*%', part[:200])
                                                if len(part_percentages) >= 2:
                                                    result["red_bloc"] = float(part_percentages[0].replace(",", "."))
                                                    result["blue_bloc"] = float(part_percentages[1].replace(",", "."))
                                                    print(f"[INFO] Found polling data via SVG: Rød blok: {result['red_bloc']}%, Blå blok: {result['blue_bloc']}%", flush=True)
                                                    break
                                    break
                    except Exception as e:
                        print(f"[WARNING] SVG method failed: {e}", flush=True)
                        
                    # Method D: Direct text search in body
                    if result.get("red_bloc") is None:
                        print("[INFO] Trying method D: Direct body text search...", flush=True)
                        try:
                            body_text = driver.find_element(By.TAG_NAME, "body").text
                            # Find all "I dag" occurrences
                            parts = body_text.split("I dag")
                            if len(parts) >= 3:
                                # Get the second "I dag" section (index 2)
                                second_section = parts[2][:500]  # First 500 chars
                                if "Rød blok" in second_section and "Blå blok" in second_section and "Rød opposition" not in second_section:
                                    percentages = re.findall(r'(\d+[,.]?\d*)\s*%', second_section)
                                    if len(percentages) >= 2:
                                        result["red_bloc"] = float(percentages[0].replace(",", "."))
                                        result["blue_bloc"] = float(percentages[1].replace(",", "."))
                                        print(f"[INFO] Found polling data via body text: Rød blok: {result['red_bloc']}%, Blå blok: {result['blue_bloc']}%", flush=True)
                        except Exception as e:
                            print(f"[WARNING] Body text method failed: {e}", flush=True)
        except Exception as e:
            print(f"[WARNING] JavaScript extraction failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
        
        # Get page source after JavaScript execution
        page_source = driver.page_source
        soup = BeautifulSoup(page_source, "html.parser")
        
        # Look for the semi-circular charts showing "I dag" (Today) data
        # The data is likely in JSON format embedded in the page or in specific HTML elements
        # Based on the description: second row of semi-circles shows "I dag" with 47.5% red and 48.4% blue
        
        # Try to find script tags with JSON data
        scripts = soup.find_all("script")
        poll_data = None
        
        for script in scripts:
            if script.string:
                # Look for JSON data containing polling information
                # Common patterns: window.__INITIAL_STATE__, window.__DATA__, etc.
                text = script.string
                
                # Try to find JSON objects with polling data
                # Look for patterns like "rød blok", "blå blok", percentages
                if "rød blok" in text.lower() or "blå blok" in text.lower() or "i dag" in text.lower():
                    # Try to extract JSON
                    json_matches = re.findall(r'\{[^{}]*"rød[^{}]*"blå[^{}]*\}', text, re.IGNORECASE | re.DOTALL)
                    if json_matches:
                        try:
                            poll_data = json.loads(json_matches[0])
                            break
                        except:
                            pass
        
        # Look for elements containing "Rød blok" and "Blå blok" - these are in the second chart set
        # The structure shows: img "I dag" -> generic "I dag" -> generic with percentages
        # We need to find the second chart set (the one with "Rød blok" and "Blå blok" labels)
        
        # Strategy: Find all elements with "I dag" text, then look for the one that's in context with "Rød blok" and "Blå blok"
        page_text = page_source
        
        # Look for the pattern: "I dag" followed by percentages, then "Rød blok" and "Blå blok"
        # The second chart set has "Rød blok" and "Blå blok" labels
        # Pattern: Look for "Rød blok" and "Blå blok" text, then find the nearest "I dag" with percentages
        
        # Find all "I dag" occurrences with two percentages
        # Pattern: "I dag" followed by two percentages (for the second chart set, there are only 2 percentages)
        today_pattern = r'(?:I dag|i dag)[^%]*?(\d+[,.]?\d*)\s*%[^%]*?(\d+[,.]?\d*)\s*%'
        matches = list(re.finditer(today_pattern, page_text, re.IGNORECASE | re.DOTALL))
        
        # Find "Rød blok" and "Blå blok" text positions
        red_bloc_pos = page_text.lower().find("rød blok")
        blue_bloc_pos = page_text.lower().find("blå blok")
        
        if red_bloc_pos > 0 and blue_bloc_pos > 0 and len(matches) >= 2:
            # We want the SECOND "I dag" match (the one in the second chart set)
            # The first chart set has 3 percentages (Rød opposition, Regeringen, Blå opposition)
            # The second chart set has 2 percentages (Rød blok, Blå blok)
            
            # Find matches that have exactly 2 percentages and are before "Rød blok"
            valid_matches = []
            for match in matches:
                match_pos = match.start()
                # Check context to see if this has 2 or 3 percentages
                context = page_text[match_pos:min(match_pos + 500, len(page_text))]
                percentages_in_context = re.findall(r'(\d+[,.]?\d*)\s*%', context)
                
                # The second chart set should have exactly 2 percentages in the "I dag" section
                # and should be before "Rød blok" label
                if len(percentages_in_context) == 2 and match_pos < red_bloc_pos:
                    # Verify this is NOT the first chart set
                    if "rød opposition" not in context.lower() and "regeringen" not in context.lower():
                        valid_matches.append(match)
            
            # Take the last valid match (closest to "Rød blok" label)
            if valid_matches:
                match = valid_matches[-1]
                try:
                    # Convert comma to dot for parsing
                    pct1 = float(match.group(1).replace(",", "."))
                    pct2 = float(match.group(2).replace(",", "."))
                    
                    # In the second chart set, first percentage is "Rød blok", second is "Blå blok"
                    # Based on the browser snapshot, the order is: 47,5% (Rød blok), 48,4% (Blå blok)
                    result["red_bloc"] = pct1
                    result["blue_bloc"] = pct2
                    
                    print(f"[INFO] Found polling data: Rød blok: {result['red_bloc']}%, Blå blok: {result['blue_bloc']}%", flush=True)
                except ValueError:
                    pass
        
        # Try using BeautifulSoup to find the specific structure
        # Look for img elements with alt="I dag" that are in the second chart set
        if result["red_bloc"] is None:
            # Find all img elements with "I dag" in alt text
            img_elements = soup.find_all("img", alt=re.compile(r"i dag", re.IGNORECASE))
            
            # Find the one that's in context with "Rød blok" and "Blå blok"
            for img in img_elements:
                # Look for parent container
                parent = img.find_parent()
                if parent:
                    # Check if this section contains "Rød blok" and "Blå blok" labels
                    parent_text = parent.get_text()
                    
                    # Look for percentages near this img
                    # The structure: img -> generic "I dag" -> generic with percentages
                    # Find percentages in the parent or nearby siblings
                    percentages = re.findall(r'(\d+[,.]?\d*)\s*%', parent_text)
                    
                    # Check if "Rød blok" and "Blå blok" appear after this section
                    if "rød blok" in parent_text.lower() and "blå blok" in parent_text.lower():
                        if len(percentages) >= 2:
                            try:
                                # Find the "I dag" section and get its percentages
                                # Look for the generic element containing "I dag" text
                                today_elem = parent.find(string=re.compile(r"i dag", re.IGNORECASE))
                                if today_elem:
                                    # Get the parent of this text element
                                    today_parent = today_elem.find_parent()
                                    if today_parent:
                                        # Find percentages in siblings or nearby
                                        today_text = today_parent.get_text()
                                        today_percentages = re.findall(r'(\d+[,.]?\d*)\s*%', today_text)
                                        if len(today_percentages) >= 2:
                                            result["red_bloc"] = float(today_percentages[0].replace(",", "."))
                                            result["blue_bloc"] = float(today_percentages[1].replace(",", "."))
                                            print(f"[INFO] Found polling data via BeautifulSoup: Rød blok: {result['red_bloc']}%, Blå blok: {result['blue_bloc']}%", flush=True)
                                            break
                            except (ValueError, AttributeError):
                                continue
        
        # Fallback: Use more specific regex to find the second chart set
        if result["red_bloc"] is None:
            # Look for the pattern in the second chart section
            # The structure: multiple "I dag" sections, we want the one before "Rød blok" label
            # Pattern: Find "I dag" followed by two percentages, then "Rød blok" and "Blå blok" labels
            pattern = r'(?:I dag|i dag)[^%]*?(\d+[,.]?\d*)\s*%[^%]*?(\d+[,.]?\d*)\s*%[^%]{0,500}?Rød blok[^%]{0,200}?Blå blok'
            match = re.search(pattern, page_text, re.IGNORECASE | re.DOTALL)
            if match:
                try:
                    result["red_bloc"] = float(match.group(1).replace(",", "."))
                    result["blue_bloc"] = float(match.group(2).replace(",", "."))
                    print(f"[INFO] Found polling data via regex: Rød blok: {result['red_bloc']}%, Blå blok: {result['blue_bloc']}%", flush=True)
                except ValueError:
                    pass
            
            # Alternative: Look for all "I dag" sections and find the one with two percentages
            # that appears before "Rød blok" label (second chart set)
            if result["red_bloc"] is None:
                # Find position of "Rød blok" label
                red_bloc_label_pos = page_text.find("Rød blok")
                if red_bloc_label_pos > 0:
                    # Look backwards from "Rød blok" to find the nearest "I dag" with percentages
                    search_area = page_text[max(0, red_bloc_label_pos - 2000):red_bloc_label_pos]
                    pattern = r'(?:I dag|i dag)[^%]*?(\d+[,.]?\d*)\s*%[^%]*?(\d+[,.]?\d*)\s*%'
                    matches = list(re.finditer(pattern, search_area, re.IGNORECASE | re.DOTALL))
                    if matches:
                        # Take the last match (closest to the label)
                        match = matches[-1]
                        try:
                            result["red_bloc"] = float(match.group(1).replace(",", "."))
                            result["blue_bloc"] = float(match.group(2).replace(",", "."))
                            print(f"[INFO] Found polling data via reverse search: Rød blok: {result['red_bloc']}%, Blå blok: {result['blue_bloc']}%", flush=True)
                        except ValueError:
                            pass
        
        if result["red_bloc"] is None or result["blue_bloc"] is None:
            print("[WARNING] Could not find polling data via regex. Trying Selenium element search...", flush=True)
            
            # Try using Selenium to find elements directly
            try:
                # Find all img elements with alt="I dag"
                img_elements = driver.find_elements(By.XPATH, "//img[contains(@alt, 'I dag') or contains(@alt, 'i dag')]")
                print(f"[INFO] Found {len(img_elements)} 'I dag' img elements", flush=True)
                
                # We want the second img element (second chart set)
                if len(img_elements) >= 2:
                    second_img = img_elements[1]  # Second chart set
                    
                    # Find the parent container
                    try:
                        # Get the parent element
                        parent = second_img.find_element(By.XPATH, "./..")
                        parent_text = parent.text
                        
                        # Find percentages in the parent text
                        percentages = re.findall(r'(\d+[,.]?\d*)\s*%', parent_text)
                        print(f"[INFO] Found {len(percentages)} percentages in second chart: {percentages}", flush=True)
                        
                        if len(percentages) >= 2:
                            # The second chart set should have exactly 2 percentages
                            # First is Rød blok, second is Blå blok
                            result["red_bloc"] = float(percentages[0].replace(",", "."))
                            result["blue_bloc"] = float(percentages[1].replace(",", "."))
                            print(f"[INFO] Found polling data via Selenium: Rød blok: {result['red_bloc']}%, Blå blok: {result['blue_bloc']}%", flush=True)
                    except Exception as e:
                        print(f"[WARNING] Error extracting from second img: {e}", flush=True)
                        
                        # Fallback: try to find by text content
                        # Look for the section that contains "Rød blok" and "Blå blok" labels
                        try:
                            red_bloc_elem = driver.find_element(By.XPATH, "//*[contains(text(), 'Rød blok')]")
                            # Find the nearest "I dag" section before this
                            # Get all text nodes containing "I dag"
                            all_text = driver.find_element(By.TAG_NAME, "body").text
                            
                            # Find the second occurrence of "I dag" followed by percentages
                            # Split by "I dag" and take the second part
                            parts = all_text.split("I dag")
                            if len(parts) >= 3:  # At least 2 "I dag" sections
                                second_section = parts[2]  # Second "I dag" section
                                percentages = re.findall(r'(\d+[,.]?\d*)\s*%', second_section[:200])  # First 200 chars
                                if len(percentages) >= 2:
                                    result["red_bloc"] = float(percentages[0].replace(",", "."))
                                    result["blue_bloc"] = float(percentages[1].replace(",", "."))
                                    print(f"[INFO] Found polling data via text search: Rød blok: {result['red_bloc']}%, Blå blok: {result['blue_bloc']}%", flush=True)
                        except Exception as e2:
                            print(f"[WARNING] Fallback text search also failed: {e2}", flush=True)
                            
            except Exception as e:
                print(f"[WARNING] Selenium element search failed: {e}", flush=True)
        
        if result["red_bloc"] is None or result["blue_bloc"] is None:
            print("[WARNING] Could not find polling data. The page structure may have changed.", flush=True)
            print("[INFO] Page content preview:", flush=True)
            print(page_source[:2000], flush=True)
        
        return result
        
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()
        return {
            "error": str(e),
            "date": datetime.now(timezone.utc).isoformat(),
            "source": "voxmeter.dk"
        }
    finally:
        # Only close driver if we created it
        if not use_selenium and driver:
            driver.quit()


def write_payload(data: dict, output_path: Path) -> None:
    """Write polling data to JSON file."""
    payload = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "data": data
    }
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    
    print(f"[INFO] Wrote data to {output_path}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Scrape Voxmeter polling data")
    parser.add_argument(
        "--output",
        help="Optional output file path. Defaults to data/voxmeter_polls.json",
    )
    return parser.parse_args(argv)


def main() -> int:
    """Main entry point."""
    args = parse_args(sys.argv[1:])
    
    output_path = (
        Path(args.output) if args.output
        else DATA_DIR / "voxmeter_polls.json"
    )
    
    driver = None
    try:
        print("[INFO] Starting Voxmeter poll scraper...", flush=True)
        print("[INFO] Setting up Chrome driver...", flush=True)
        driver = setup_chrome_driver(headless=True)
        print("[INFO] Chrome driver ready", flush=True)
        
        data = scrape_voxmeter_polls(driver)
        
        write_payload(data, output_path)
        
        if "error" in data:
            print(f"[WARNING] Scraping completed with errors: {data.get('error')}", file=sys.stderr, flush=True)
            return 1
        
        if data.get("red_bloc") is None or data.get("blue_bloc") is None:
            print("[WARNING] No polling data found", file=sys.stderr, flush=True)
            return 1
        
        print(f"[SUCCESS] Scraped polling data: Rød blok: {data.get('red_bloc')}%, Blå blok: {data.get('blue_bloc')}%", flush=True)
        return 0
        
    except KeyboardInterrupt:
        print("\n[INFO] Scraping interrupted by user", file=sys.stderr, flush=True)
        return 130
    except Exception as exc:
        print(f"[ERROR] Scraping failed: {exc}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if driver:
            driver.quit()
            print("[INFO] Browser closed", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

