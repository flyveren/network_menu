#!/usr/bin/env python3
"""Scrape individual party polling data from Voxmeter bar chart."""

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
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    from webdriver_manager.chrome import ChromeDriverManager
    import PyPDF2
    import pdfplumber
except ImportError as e:
    print(f"[ERROR] Missing required dependency: {e}", file=sys.stderr)
    print("[INFO] Install with: pip install beautifulsoup4 selenium webdriver-manager PyPDF2 pdfplumber", file=sys.stderr)
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


def scrape_party_data(driver: webdriver.Chrome) -> dict[str, list[float]]:
    """Scrape party polling data from the bar chart."""
    url = "https://voxmeter.dk/meningsmalinger/"
    
    # Store dates from header row
    header_dates = {'seneste': None, 'forrige': None}
    
    print(f"[INFO] Fetching data from: {url}", flush=True)
    
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
        
        # Wait longer for JavaScript to render charts
        sleep(10)
        
        # Scroll down to ensure charts are rendered
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
        sleep(3)
        driver.execute_script("window.scrollTo(0, 0);")
        sleep(3)
        
        # Wait a bit more for any lazy-loaded content
        sleep(5)
        
        # Wait for chart to be visible and Highcharts to be loaded
        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'Socialdemokratiet') or contains(text(), 'Telefoninterview')]"))
            )
            print("[INFO] Chart found, waiting for Highcharts...", flush=True)
            
            # Wait for Highcharts to be available and charts to be rendered
            def highcharts_ready(driver):
                try:
                    return driver.execute_script("""
                        return typeof Highcharts !== 'undefined' && 
                               Highcharts.charts && 
                               Highcharts.charts.length > 0 &&
                               Highcharts.charts[0].series &&
                               Highcharts.charts[0].series.length > 0;
                    """)
                except:
                    return False
            
            WebDriverWait(driver, 30).until(highcharts_ready)
            print("[INFO] Highcharts loaded, extracting data...", flush=True)
            sleep(5)  # Give chart extra time to fully render all data
        except Exception as e:
            print(f"[WARNING] Timeout waiting for chart/Highcharts: {e}", flush=True)
        
        # Party names mapping (Danish names to short codes)
        party_mapping = {
            "Socialdemokratiet": "A",
            "Radikale Venstre": "B",
            "Det Konservative Folkeparti": "C",
            "Socialistisk Folkeparti": "F",
            "Liberal Alliance": "I",
            "Moderaterne": "M",
            "Borgernes Parti": "H",
            "Dansk Folkeparti": "O",
            "Venstre": "V",
            "Danmarksdemokraterne": "Æ",
            "Enhedslisten": "Ø",
            "Alternativet": "Å"
        }
        
        result = {}
        
        # Strategy 1: Try to find and download PDF with table data
        print("[INFO] Attempting to find PDF link with table data...", flush=True)
        pdf_url = None
        try:
            # Look for PDF link in elementor-tab-content
            pdf_links = driver.find_elements(By.XPATH, "//a[contains(@href, 'opiniontable/pdf/') or contains(@href, 'Voxmetermaling')]")
            if pdf_links:
                pdf_url = pdf_links[0].get_attribute("href")
                print(f"[INFO] Found PDF link: {pdf_url}", flush=True)
            else:
                # Try to find link by text content
                pdf_links = driver.find_elements(By.XPATH, "//a[contains(@href, '.pdf')]")
                for link in pdf_links:
                    href = link.get_attribute("href") or ""
                    if "voxmeter" in href.lower() and "maling" in href.lower():
                        pdf_url = href
                        print(f"[INFO] Found PDF link by pattern: {pdf_url}", flush=True)
                        break
        except Exception as e:
            print(f"[WARNING] Error finding PDF link: {e}", flush=True)
        
        if pdf_url:
            try:
                import urllib.request
                print(f"[INFO] Downloading PDF from: {pdf_url}", flush=True)
                pdf_path = "/tmp/voxmeter_table.pdf"
                urllib.request.urlretrieve(pdf_url, pdf_path)
                print(f"[INFO] Downloaded PDF, parsing table data...", flush=True)
                
                # Try to extract data from PDF using pdfplumber (better for tables)
                try:
                    with pdfplumber.open(pdf_path) as pdf:
                        for page_num, page in enumerate(pdf.pages):
                            tables = page.extract_tables()
                            if tables:
                                print(f"[INFO] Found {len(tables)} table(s) on page {page_num + 1}", flush=True)
                                for table_idx, table in enumerate(tables):
                                    if not table or len(table) < 2:
                                        continue
                                    
                                    # Debug: Print table structure
                                    print(f"[DEBUG] Table {table_idx} has {len(table)} rows", flush=True)
                                    if len(table) > 0:
                                        print(f"[DEBUG] First row: {table[0][:8] if len(table[0]) > 8 else table[0]}", flush=True)
                                    
                                    # Based on actual PDF structure:
                                    # Table 0: Header row with dates
                                    # Table 1: Party data rows
                                    # Col 1: Party name (e.g., "A – Socialdemokratiet")
                                    # Col 4: Seneste måling (value only, date in header)
                                    # Col 7: Forrige måling (value only, date in header)
                                    # Col 10: 1 måned siden (with date)
                                    # Col 13: Valget 2022
                                    
                                    # Extract dates from header row (table 0)
                                    # Row 0 has labels: ['', '', 'Seneste måling', '', '', 'Forrige måling', '', '', '1 måned siden', '']
                                    # Row 1 has dates: [None, None, '10.11.2025', None, None, '03.11.2025', None, None, '13.10.2025', None]
                                    if table_idx == 0:
                                        if len(table) >= 2:
                                            # Dates are in row 1 (second row)
                                            date_row = table[1]
                                            
                                            # Seneste måling date is in column 2 (index 2)
                                            if len(date_row) > 2:
                                                seneste_date_cell = str(date_row[2] or "").strip()
                                                if seneste_date_cell:
                                                    # Clean up date format (might be DD.MM.YYYY, convert to DD/MM/YYYY or DD/MM)
                                                    seneste_date = seneste_date_cell.replace('.', '/')
                                                    # Remove year if present for consistency (or keep it)
                                                    # For now, keep full date
                                                    header_dates['seneste'] = seneste_date
                                                    print(f"[DEBUG] Found Seneste måling date in column 2: {seneste_date}", flush=True)
                                            
                                            # Forrige måling date is in column 5 (index 5)
                                            if len(date_row) > 5:
                                                forrige_date_cell = str(date_row[5] or "").strip()
                                                if forrige_date_cell:
                                                    # Clean up date format
                                                    forrige_date = forrige_date_cell.replace('.', '/')
                                                    header_dates['forrige'] = forrige_date
                                                    print(f"[DEBUG] Found Forrige måling date in column 5: {forrige_date}", flush=True)
                                        
                                        print(f"[DEBUG] Final header dates: seneste={header_dates['seneste']}, forrige={header_dates['forrige']}", flush=True)
                                        continue
                                    
                                    # Column indices based on structure
                                    seneste_col = 4
                                    forrige_col = 7
                                    maaned_col = 10
                                    valg_col = 13
                                    
                                    # Only process table 1 (party data table)
                                    if table_idx != 1:
                                        continue
                                    
                                    # Get dates from header if available
                                    seneste_date = header_dates.get('seneste')
                                    forrige_date = header_dates.get('forrige')
                                    
                                    print(f"[DEBUG] Using columns: Seneste={seneste_col}, Forrige={forrige_col}, Måned={maaned_col}, Valget={valg_col}", flush=True)
                                    print(f"[DEBUG] Dates from header: seneste={seneste_date}, forrige={forrige_date}", flush=True)
                                    
                                    # Now look for party rows in table 1
                                    for row_idx, row in enumerate(table):
                                        if not row or len(row) < 2:
                                            continue
                                        
                                        # Get second cell (party name/code) - first cell is empty
                                        # Format: "A – Socialdemokratiet" or "B – Radikale Venstre"
                                        party_cell = str(row[1] or "").strip() if len(row) > 1 else ""
                                        
                                        if not party_cell:
                                            continue
                                        
                                        # Try to match party
                                        for party_name, party_code in party_mapping.items():
                                            if party_code in result:
                                                continue
                                            
                                            # Check if party cell contains party code or name
                                            party_cell_lower = party_cell.lower()
                                            party_name_lower = party_name.lower()
                                            
                                            # Match format: "A – Socialdemokratiet" or "B – Radikale Venstre"
                                            if (party_cell.startswith(party_code + " –") or
                                                party_cell.startswith(party_code + " -") or
                                                party_cell.startswith(party_code + " ") or
                                                party_name_lower in party_cell_lower):
                                                
                                                print(f"[DEBUG] Matched party {party_code} ({party_name}) in row {row_idx}: '{party_cell}'", flush=True)
                                                
                                                # Extract values from the 4 columns: Seneste, Forrige, Måned, Valget
                                                values = []
                                                
                                                # Extract Seneste måling (col 4) - date comes from header
                                                seneste_value = None
                                                if seneste_col < len(row):
                                                    seneste_cell = str(row[seneste_col] or "").strip()
                                                    # Extract number (ignore parentheses with changes)
                                                    seneste_nums = re.findall(r'(\d+[,.]?\d*)', seneste_cell)
                                                    for num_str in seneste_nums:
                                                        try:
                                                            val = float(num_str.replace(',', '.'))
                                                            if 0 <= val <= 100:
                                                                seneste_value = val
                                                                values.append(val)
                                                                print(f"[DEBUG] Found Seneste måling for {party_code}: {val}", flush=True)
                                                                break
                                                        except ValueError:
                                                            pass
                                                
                                                # Extract Forrige måling (col 7) - date comes from header
                                                forrige_value = None
                                                if forrige_col < len(row):
                                                    forrige_cell = str(row[forrige_col] or "").strip()
                                                    forrige_nums = re.findall(r'(\d+[,.]?\d*)', forrige_cell)
                                                    for num_str in forrige_nums:
                                                        try:
                                                            val = float(num_str.replace(',', '.'))
                                                            if 0 <= val <= 100:
                                                                forrige_value = val
                                                                values.append(val)
                                                                print(f"[DEBUG] Found Forrige måling for {party_code}: {val}", flush=True)
                                                                break
                                                        except ValueError:
                                                            pass
                                                
                                                # Extract 1 måned siden (col 10)
                                                if maaned_col < len(row):
                                                    maaned_cell = str(row[maaned_col] or "").strip()
                                                    maaned_nums = re.findall(r'(\d+[,.]?\d*)', maaned_cell)
                                                    for num_str in maaned_nums:
                                                        try:
                                                            val = float(num_str.replace(',', '.'))
                                                            if 0 <= val <= 100:
                                                                values.append(val)
                                                                print(f"[DEBUG] Found 1 måned siden for {party_code}: {val}", flush=True)
                                                                break
                                                        except ValueError:
                                                            pass
                                                
                                                # Extract Valget 2022 (col 13)
                                                if valg_col < len(row):
                                                    valg_cell = str(row[valg_col] or "").strip()
                                                    valg_nums = re.findall(r'(\d+[,.]?\d*)', valg_cell)
                                                    for num_str in valg_nums:
                                                        try:
                                                            val = float(num_str.replace(',', '.'))
                                                            if 0 <= val <= 100:
                                                                values.append(val)
                                                                print(f"[DEBUG] Found Valget 2022 for {party_code}: {val}", flush=True)
                                                                break
                                                        except ValueError:
                                                            pass
                                                
                                                # We need exactly 4 values: Seneste måling, Forrige måling, 1 måned siden, Valget 2022
                                                if len(values) >= 4:
                                                    # Store values with dates
                                                    result[party_code] = {
                                                        "values": values[:4],
                                                        "seneste_date": seneste_date,
                                                        "forrige_date": forrige_date,
                                                    }
                                                    print(f"[INFO] Found data for {party_name} ({party_code}) from PDF: {values[:4]} (dates: seneste={seneste_date}, forrige={forrige_date})", flush=True)
                                                elif len(values) >= 1:
                                                    # If we have partial data, pad with last value or 0
                                                    while len(values) < 4:
                                                        values.append(values[-1] if values else 0)
                                                    result[party_code] = {
                                                        "values": values[:4],
                                                        "seneste_date": seneste_date,
                                                        "forrige_date": forrige_date,
                                                    }
                                                    print(f"[INFO] Found partial data for {party_name} ({party_code}) from PDF (padded): {values[:4]}", flush=True)
                                                break
                except Exception as pdf_error:
                    print(f"[WARNING] Error parsing PDF with pdfplumber: {pdf_error}", flush=True)
                    # Fallback to PyPDF2
                    try:
                        with open(pdf_path, 'rb') as pdf_file:
                            pdf_reader = PyPDF2.PdfReader(pdf_file)
                            for page_num, page in enumerate(pdf_reader.pages):
                                text = page.extract_text()
                                # Parse text for party data
                                lines = text.split('\n')
                                for line in lines:
                                    for party_name, party_code in party_mapping.items():
                                        if party_code in result:
                                            continue
                                        if party_code in line[:10] or party_name.lower() in line.lower():
                                            nums = re.findall(r'(\d+[,.]?\d*)', line)
                                            values = []
                                            for num_str in nums:
                                                try:
                                                    val = float(num_str.replace(',', '.'))
                                                    if 0 <= val <= 100 and val not in [2022, 2025, 41, 42, 43, 44, 45]:
                                                        if val not in values:
                                                            values.append(val)
                                                except ValueError:
                                                    pass
                                            if len(values) >= 4:
                                                result[party_code] = values[:4]
                                                print(f"[INFO] Found data for {party_name} ({party_code}) from PDF text: {values[:4]}", flush=True)
                                            elif len(values) >= 1:
                                                # Pad to 4 values
                                                while len(values) < 4:
                                                    values.append(values[-1] if values else 0)
                                                result[party_code] = values[:4]
                                                print(f"[INFO] Found partial data for {party_name} ({party_code}) from PDF text (padded): {values[:4]}", flush=True)
                                            break
                    except Exception as pdf2_error:
                        print(f"[WARNING] Error parsing PDF with PyPDF2: {pdf2_error}", flush=True)
            except Exception as download_error:
                print(f"[WARNING] Error downloading PDF: {download_error}", flush=True)
        
        # Strategy 2: Try to extract from HTML table first, or find table image
        if len(result) < len(party_mapping):
            print("[INFO] PDF extraction incomplete, attempting to extract data from HTML table or table image...", flush=True)
            try:
                page_source = driver.page_source
                soup = BeautifulSoup(page_source, "html.parser")
                
                # First, look for table images (since user said table is an image)
                # Look for the specific Voxmeter opinion poll image
                table_images = []
                
                # Try to find the specific image by src pattern
                try:
                    opinion_images = driver.find_elements(By.XPATH, "//img[contains(@src, 'opinion-') or contains(@src, 'voxmeter')]")
                    table_images.extend(opinion_images)
                    print(f"[INFO] Found {len(opinion_images)} opinion poll image(s) by src pattern", flush=True)
                except:
                    pass
                
                # Also look for images in elementor-widget-container
                try:
                    elementor_images = driver.find_elements(By.XPATH, "//div[contains(@class, 'elementor-widget-container')]//img")
                    for img in elementor_images:
                        src = img.get_attribute("src") or ""
                        width = int(img.get_attribute("width") or "0")
                        height = int(img.get_attribute("height") or "0")
                        # Look for large images that might be the table
                        if (width > 500 and height > 300) or "opinion" in src.lower():
                            if img not in table_images:
                                table_images.append(img)
                                print(f"[INFO] Found elementor image: {src[:100]} ({width}x{height})", flush=True)
                except Exception as e:
                    print(f"[DEBUG] Error finding elementor images: {e}", flush=True)
                
                # Fallback: Look for any large images
                if not table_images:
                    try:
                        all_images = driver.find_elements(By.TAG_NAME, "img")
                        for img in all_images:
                            try:
                                width = int(img.get_attribute("width") or "0")
                                height = int(img.get_attribute("height") or "0")
                                # Table images are usually wide and tall
                                if width > 500 and height > 300:
                                    table_images.append(img)
                            except:
                                pass
                    except:
                        pass
                
                if table_images:
                    print(f"[INFO] Found {len(table_images)} potential table image(s), will try OCR...", flush=True)
                    # OCR will be handled in Strategy 3
                
                # Look for HTML table elements
                tables = soup.find_all("table")
                if not tables:
                    # Look for divs that might contain table-like data
                    tables = soup.find_all("div", class_=re.compile(r"table|grid|row", re.I))
                
                for table in tables:
                    rows = table.find_all("tr") if table.name == "table" else table.find_all("div", class_=re.compile(r"row|tr", re.I))
                    
                    for row in rows:
                        cells = row.find_all(["td", "th", "div"]) if row.name == "tr" else row.find_all("div")
                        if len(cells) < 2:
                            continue
                        
                        # Get first cell (party name)
                        first_cell_text = cells[0].get_text(strip=True)
                        
                        # Try to match party name
                        for party_name, party_code in party_mapping.items():
                            if party_code in result:
                                continue  # Already found this party
                            
                            party_name_lower = party_name.lower()
                            first_cell_lower = first_cell_text.lower()
                            
                            # Check if party name or code matches
                            if (party_name_lower in first_cell_lower or 
                                party_code in first_cell_text or
                                first_cell_lower.startswith(party_code.lower() + " ") or
                                first_cell_lower.startswith(party_code.lower() + "-")):
                                
                                # Extract numbers from remaining cells
                                # Looking for: Valget 2022, Uge 41, Uge 42, Uge 43, Uge 44, Uge 45
                                values = []
                                
                                # Try to find columns with "Valget" or "Uge" headers
                                header_row = None
                                if table.name == "table":
                                    header_row = table.find("thead") or table.find("tr")
                                    if header_row:
                                        headers = header_row.find_all(["th", "td"])
                                        header_texts = [h.get_text(strip=True) for h in headers]
                                        
                                        # Find indices of Valget and Uge columns
                                        valg_idx = None
                                        uge_indices = []
                                        for idx, header in enumerate(header_texts):
                                            if "valget" in header.lower() and "2022" in header:
                                                valg_idx = idx
                                            elif "uge" in header.lower():
                                                uge_num = re.search(r'(\d+)', header)
                                                if uge_num:
                                                    uge_num_val = int(uge_num.group(1))
                                                    if 41 <= uge_num_val <= 45:
                                                        uge_indices.append((idx, uge_num_val))
                                        
                                        # Sort uge_indices by week number
                                        uge_indices.sort(key=lambda x: x[1])
                                        
                                        # Extract values in order: Valget, then Uge 41-45
                                        if valg_idx is not None and valg_idx < len(cells):
                                            valg_text = cells[valg_idx].get_text(strip=True)
                                            valg_num = re.search(r'(\d+[,.]?\d*)', valg_text)
                                            if valg_num:
                                                try:
                                                    val = float(valg_num.group(1).replace(',', '.'))
                                                    if 0 <= val <= 100:
                                                        values.append(val)
                                                except ValueError:
                                                    pass
                                        
                                        for idx, uge_num in uge_indices:
                                            if idx < len(cells):
                                                uge_text = cells[idx].get_text(strip=True)
                                                uge_val = re.search(r'(\d+[,.]?\d*)', uge_text)
                                                if uge_val:
                                                    try:
                                                        val = float(uge_val.group(1).replace(',', '.'))
                                                        if 0 <= val <= 100:
                                                            values.append(val)
                                                    except ValueError:
                                                        pass
                                
                                # Fallback: extract all numbers from row cells
                                if len(values) < 6:
                                    for cell in cells[1:]:  # Skip first cell (party name)
                                        cell_text = cell.get_text(strip=True)
                                        # Look for percentage numbers
                                        nums = re.findall(r'(\d+[,.]?\d*)', cell_text)
                                        for num_str in nums:
                                            try:
                                                val = float(num_str.replace(',', '.'))
                                                # Filter reasonable percentages, exclude years and week numbers
                                                if 0 <= val <= 100 and val not in [2022, 2025, 41, 42, 43, 44, 45]:
                                                    if val not in values:  # Avoid duplicates
                                                        values.append(val)
                                            except ValueError:
                                                pass
                                
                                # Take first 4 values if we have enough (Seneste, Forrige, Måned, Valget)
                                if len(values) >= 4:
                                    result[party_code] = values[:4]
                                    print(f"[INFO] Found data for {party_name} ({party_code}) from table: {values[:4]}", flush=True)
                                    break
                                elif len(values) >= 1:
                                    # Pad to 4 values
                                    while len(values) < 4:
                                        values.append(values[-1] if values else 0)
                                    result[party_code] = values[:4]
                                    print(f"[INFO] Found partial data for {party_name} ({party_code}) from table (padded): {values[:4]}", flush=True)
                                    break
            
                if result:
                    print(f"[INFO] Successfully extracted data for {len(result)} parties from HTML table", flush=True)
            except Exception as e:
                print(f"[WARNING] Error extracting from HTML table: {e}", flush=True)
        
        # Strategy 2: If table extraction didn't work, try screenshot + OCR
        if len(result) < len(party_mapping):
            print("[INFO] Table extraction incomplete, trying screenshot + OCR...", flush=True)
            try:
                from PIL import Image
                import pytesseract
                import io
                
                # Configure pytesseract to use system tesseract binary
                # In Alpine Linux, tesseract is typically at /usr/bin/tesseract
                try:
                    pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'
                except:
                    pass  # Will use default path
                
                # Try to find table image element first
                table_img_element = None
                img_url = None
                
                try:
                    # Look for the specific Voxmeter opinion poll image
                    # The table image is in elementor-widget-container and has pattern: opinion-XX-XX-XXXX-X.png
                    # First try to find by the specific pattern
                    table_img_elements = []
                    
                    # Look for images with the opinion poll pattern (opinion-DD-MM-YYYY-N.png)
                    try:
                        all_imgs = driver.find_elements(By.TAG_NAME, "img")
                        for img_elem in all_imgs:
                            try:
                                src = img_elem.get_attribute("src") or ""
                                width = int(img_elem.get_attribute("width") or "0")
                                height = int(img_elem.get_attribute("height") or "0")
                                
                                # Look for the specific pattern: opinion-XX-XX-XXXX-X.png
                                # And it should be large (table size)
                                if "opinion-" in src.lower() and ".png" in src.lower():
                                    # Check if it matches the pattern (has date-like structure)
                                    if re.search(r'opinion-\d{1,2}-\d{1,2}-\d{4}', src.lower()):
                                        if width > 1000 and height > 400:
                                            table_img_elements.append(img_elem)
                                            print(f"[DEBUG] Found opinion table image by pattern: {src[:100]} ({width}x{height})", flush=True)
                            except:
                                pass
                    except:
                        pass
                    
                    # If not found, try elementor-widget-container
                    if not table_img_elements:
                        elementor_imgs = driver.find_elements(By.XPATH, "//div[contains(@class, 'elementor-widget-container')]//img")
                        for img_elem in elementor_imgs:
                            try:
                                src = img_elem.get_attribute("src") or ""
                                width = int(img_elem.get_attribute("width") or "0")
                                height = int(img_elem.get_attribute("height") or "0")
                                
                                # Look for large images with "opinion" in src
                                if "opinion" in src.lower() and width > 1000 and height > 400:
                                    table_img_elements.append(img_elem)
                                    print(f"[DEBUG] Found opinion table image in elementor: {src[:100]} ({width}x{height})", flush=True)
                                    break
                            except:
                                pass
                    
                    # Final fallback: Look for any large images with "opinion" in src
                    if not table_img_elements:
                        try:
                            opinion_imgs = driver.find_elements(By.XPATH, "//img[contains(@src, 'opinion-')]")
                            for img_elem in opinion_imgs:
                                try:
                                    src = img_elem.get_attribute("src") or ""
                                    width = int(img_elem.get_attribute("width") or "0")
                                    height = int(img_elem.get_attribute("height") or "0")
                                    if width > 1000 and height > 400:
                                        table_img_elements.append(img_elem)
                                        print(f"[DEBUG] Found large opinion image: {src[:100]} ({width}x{height})", flush=True)
                                        break
                                except:
                                    pass
                        except:
                            pass
                    
                    if table_img_elements:
                        table_img_element = table_img_elements[0]
                        img_url = table_img_element.get_attribute("src")
                        print(f"[INFO] Found table image element: {img_url[:100] if img_url else 'no src'}", flush=True)
                        
                        # Get image dimensions
                        try:
                            img_width = int(table_img_element.get_attribute("width") or "0")
                            img_height = int(table_img_element.get_attribute("height") or "0")
                            print(f"[DEBUG] Table image size from attributes: {img_width}x{img_height}", flush=True)
                        except:
                            try:
                                img_width = table_img_element.size['width']
                                img_height = table_img_element.size['height']
                                print(f"[DEBUG] Table image size from element: {img_width}x{img_height}", flush=True)
                            except:
                                img_width = 0
                                img_height = 0
                        
                        # Scroll to image
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", table_img_element)
                        sleep(2)
                        
                        # Try to download image directly from URL if available
                        if img_url and img_url.startswith('http'):
                            try:
                                import urllib.request
                                print(f"[INFO] Downloading image directly from URL...", flush=True)
                                urllib.request.urlretrieve(img_url, "/tmp/voxmeter_table_downloaded.png")
                                img = Image.open("/tmp/voxmeter_table_downloaded.png")
                                print(f"[INFO] Downloaded image size: {img.size[0]}x{img.size[1]}", flush=True)
                                # Skip screenshot and use downloaded image
                                table_img_element = None  # Signal that we already have the image
                            except Exception as download_error:
                                print(f"[WARNING] Could not download image, will use screenshot: {download_error}", flush=True)
                                img_url = None
                        
                        # If download failed, try to zoom in on the image for better OCR
                        if table_img_element and (img_width < 1000 or img_height < 1000):
                            try:
                                driver.execute_script("""
                                    var img = arguments[0];
                                    img.style.transform = 'scale(2)';
                                    img.style.transformOrigin = 'top left';
                                """, table_img_element)
                                sleep(1)
                                print(f"[DEBUG] Zoomed in on table image", flush=True)
                            except Exception as zoom_error:
                                print(f"[DEBUG] Could not zoom: {zoom_error}", flush=True)
                except Exception as e:
                    print(f"[DEBUG] Could not find table image element: {e}", flush=True)
                
                # Take screenshot - either of the image element or full page
                # If we already downloaded the image, skip screenshot
                if 'img' not in locals() or img is None:
                    if table_img_element:
                        # Screenshot just the image element
                        try:
                            location = table_img_element.location
                            size = table_img_element.size
                            screenshot = driver.get_screenshot_as_png()
                            img = Image.open(io.BytesIO(screenshot))
                            # Crop to image element with some padding
                            padding = 20
                            left = max(0, location['x'] - padding)
                            top = max(0, location['y'] - padding)
                            right = left + size['width'] + (2 * padding)
                            bottom = top + size['height'] + (2 * padding)
                            img = img.crop((left, top, right, bottom))
                            
                            # Enhance image for better OCR
                            # Convert to grayscale and increase contrast
                            from PIL import ImageEnhance
                            img = img.convert('L')  # Grayscale
                            enhancer = ImageEnhance.Contrast(img)
                            img = enhancer.enhance(2.0)  # Increase contrast
                            enhancer = ImageEnhance.Sharpness(img)
                            img = enhancer.enhance(2.0)  # Increase sharpness
                            
                            print(f"[INFO] Cropped and enhanced screenshot to image element: {left},{top} to {right},{bottom}", flush=True)
                        except Exception as e:
                            print(f"[WARNING] Could not crop to image element, using full screenshot: {e}", flush=True)
                            screenshot = driver.get_screenshot_as_png()
                            img = Image.open(io.BytesIO(screenshot))
                            # Enhance full screenshot too
                            try:
                                from PIL import ImageEnhance
                                img = img.convert('L')
                                enhancer = ImageEnhance.Contrast(img)
                                img = enhancer.enhance(2.0)
                            except:
                                pass
                    else:
                        # Try to find table area (scroll to it first)
                        try:
                            table_element = driver.find_element(By.XPATH, "//table | //*[contains(@class, 'table')] | //*[contains(text(), 'Valget 2022')] | //*[contains(text(), 'Socialdemokratiet')]")
                            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", table_element)
                            sleep(2)
                            screenshot = driver.get_screenshot_as_png()
                            img = Image.open(io.BytesIO(screenshot))
                        except:
                            # Use full page screenshot
                            screenshot = driver.get_screenshot_as_png()
                            img = Image.open(io.BytesIO(screenshot))
                
                # Save image for debugging (optional)
                try:
                    debug_img_path = "/tmp/voxmeter_table_debug.png"
                    img.save(debug_img_path)
                    print(f"[DEBUG] Saved debug image to {debug_img_path}", flush=True)
                except:
                    pass
                
                # Extract text using OCR with better configuration
                # Try multiple OCR configurations for better results
                ocr_configs = [
                    '--psm 6 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZÆØÅabcdefghijklmnopqrstuvwxyzæøå.,%+-() ',  # Single block with whitelist
                    '--psm 11', # Sparse text (find as much text as possible)
                    '--psm 12', # Sparse text with OSD
                    '--psm 4',  # Assume a single column of text
                    '--psm 3',  # Fully automatic page segmentation
                ]
                
                ocr_text = ""
                for config in ocr_configs:
                    try:
                        test_text = pytesseract.image_to_string(img, lang='dan+eng', config=config)
                        if len(test_text.strip()) > len(ocr_text.strip()):
                            ocr_text = test_text
                            print(f"[DEBUG] OCR with config '{config}' extracted {len(test_text)} chars: {test_text[:200]}", flush=True)
                    except Exception as e:
                        print(f"[DEBUG] OCR config '{config}' failed: {e}", flush=True)
                
                # Fallback to default if no config worked
                if not ocr_text or len(ocr_text.strip()) < 10:
                    try:
                        # Try with just Danish
                        ocr_text = pytesseract.image_to_string(img, lang='dan')
                        if len(ocr_text.strip()) < 10:
                            # Try with just English
                            ocr_text = pytesseract.image_to_string(img, lang='eng')
                    except Exception as e:
                        print(f"[WARNING] Default OCR failed: {e}", flush=True)
                        ocr_text = ""
                
                print(f"[DEBUG] OCR extracted text (first 1000 chars): {ocr_text[:1000]}", flush=True)
                print(f"[DEBUG] OCR extracted text length: {len(ocr_text)} chars", flush=True)
                
                # Parse OCR text for party data
                # The table structure from image description shows:
                # Party | Latest poll | Previous poll | 1 month ago | Election (2022) | ...
                # But we need: Election (2022) value + 5 weekly values (Uge 41-45)
                # The weekly values might be in a separate chart/diagram
                
                if not ocr_text or len(ocr_text.strip()) < 10:
                    print("[WARNING] OCR extracted no or very little text. Image might be too small or low quality.", flush=True)
                else:
                    # Parse OCR text line by line
                    lines = ocr_text.split('\n')
                    party_data = {}  # Store all data for each party
                    
                    # Look for party rows - they typically start with party code (A, B, C, etc.)
                    for i, line in enumerate(lines):
                        line_stripped = line.strip()
                        if len(line_stripped) < 2:
                            continue
                        
                        # Check if line starts with a party code
                        for party_name, party_code in party_mapping.items():
                            if party_code in result:
                                continue  # Already found this party
                            
                            # Check if line starts with party code or contains party name
                            line_lower = line_stripped.lower()
                            if (line_stripped.startswith(party_code) or 
                                line_stripped.startswith(party_code + ' ') or
                                line_stripped.startswith(party_code + '-') or
                                party_code in line_stripped[:5] or  # Party code in first 5 chars
                                party_name.lower() in line_lower):
                                
                                # Extract all numbers from this line and next 2-3 lines (table row)
                                party_lines = [line_stripped]
                                for j in range(i+1, min(i+4, len(lines))):
                                    if lines[j].strip():
                                        party_lines.append(lines[j].strip())
                                
                                all_text = ' '.join(party_lines)
                                print(f"[DEBUG] Processing party {party_code} ({party_name}): {all_text[:200]}", flush=True)
                                
                                # Extract all numbers from the text
                                nums = re.findall(r'(\d+[,.]?\d*)', all_text)
                                
                                values = []
                                for num_str in nums:
                                    try:
                                        val = float(num_str.replace(',', '.'))
                                        # Filter: percentages (0-100), exclude years and week numbers
                                        if 0 <= val <= 100:
                                            # Exclude years and week numbers, but keep percentages
                                            if val not in [2022, 2025, 41, 42, 43, 44, 45]:
                                                # Avoid duplicates and very small values that might be noise
                                                if val not in values and val >= 0.5:
                                                    values.append(val)
                                    except ValueError:
                                        pass
                                
                                if len(values) >= 1:
                                    party_data[party_code] = values
                                    print(f"[DEBUG] Found {len(values)} values for {party_name} ({party_code}): {values[:10]}", flush=True)
                                break
                    
                    # Try to match values to the 6 required: Valget 2022 + Uge 41-45
                    # Since the table shows Election (2022) but not necessarily the 5 weeks,
                    # we'll take the first 6 percentage values we find
                    for party_code, values in party_data.items():
                        if party_code not in result:
                                if len(values) >= 4:
                                    # Take first 4 values (Seneste, Forrige, Måned, Valget)
                                    result[party_code] = values[:4]
                                    party_name = [k for k, v in party_mapping.items() if v == party_code][0]
                                    print(f"[INFO] Found data for {party_name} ({party_code}) via OCR: {values[:4]}", flush=True)
                                elif len(values) >= 1:
                                    # Pad to 4 values
                                    print(f"[DEBUG] Only found {len(values)} values for {party_code} (need 4): {values}", flush=True)
                                    while len(values) < 4:
                                        values.append(values[-1] if values else 0)
                                    result[party_code] = values[:4]
                                    party_name = [k for k, v in party_mapping.items() if v == party_code][0]
                                    print(f"[INFO] Found partial data for {party_name} ({party_code}) via OCR (padded): {values[:4]}", flush=True)
                
            except ImportError:
                print("[WARNING] OCR libraries (PIL/pytesseract) not available. Install with: pip install pillow pytesseract", flush=True)
            except Exception as e:
                print(f"[WARNING] OCR extraction failed: {e}", flush=True)
        
        # Strategy 3: If we still don't have enough data, try Highcharts extraction
        # Highcharts stores data in window.Highcharts.charts array
        if len(result) < len(party_mapping):
            print(f"[INFO] Only found {len(result)}/{len(party_mapping)} parties, trying Highcharts extraction...", flush=True)
            js_code = """
        (function() {
            var result = {};
            var partyNames = {
                'Socialdemokratiet': 'A',
                'Radikale Venstre': 'B',
                'Det Konservative Folkeparti': 'C',
                'Socialistisk Folkeparti': 'F',
                'Borgernes Parti': 'H',
                'Liberal Alliance': 'I',
                'Moderaterne': 'M',
                'Dansk Folkeparti': 'O',
                'Venstre': 'V',
                'Danmarksdemokraterne': 'Æ',
                'Enhedslisten': 'Ø',
                'Alternativet': 'Å'
            };
            
            // Check if Highcharts is available
            if (typeof Highcharts !== 'undefined' && Highcharts.charts) {
                console.log('Found ' + Highcharts.charts.length + ' Highcharts charts');
                
                for (var i = 0; i < Highcharts.charts.length; i++) {
                    var chart = Highcharts.charts[i];
                    if (!chart || !chart.series) {
                        console.log('Chart ' + i + ' has no series');
                        continue;
                    }
                    
                    console.log('Chart ' + i + ' has ' + chart.series.length + ' series');
                    
                    // Log all series names to understand structure
                    for (var s = 0; s < chart.series.length; s++) {
                        var ser = chart.series[s];
                        var serName = ser.name || 'unnamed';
                        var dataLen = ser.data ? ser.data.length : 0;
                        console.log('Series ' + s + ' name: "' + serName + '", data length: ' + dataLen);
                        if (ser.data && ser.data.length > 0) {
                            // Extract just the y value, not the whole object (avoids circular reference)
                            var firstPoint = ser.data[0];
                            var firstY = (typeof firstPoint === 'number') ? firstPoint : (firstPoint.y !== undefined ? firstPoint.y : 'N/A');
                            console.log('Series ' + s + ' first data point y value: ' + firstY);
                        }
                    }
                    
                    // Try both possible chart structures:
                    // Structure 1: Categories = parties, Series = time periods (Valget 2022, Uge 41-45)
                    // Structure 2: Series = parties, Data points = time periods
                    
                    if (chart.xAxis && chart.xAxis[0] && chart.xAxis[0].categories) {
                        var categories = chart.xAxis[0].categories;
                        var categoriesStr = '';
                        for (var c = 0; c < Math.min(categories.length, 10); c++) {
                            categoriesStr += (c > 0 ? ', ' : '') + '"' + categories[c] + '"';
                        }
                        console.log('Chart has ' + categories.length + ' categories: [' + categoriesStr + ']');
                        
                        // Structure 1: Categories are parties, Series are time periods
                        // Iterate through categories (parties)
                        for (var catIdx = 0; catIdx < categories.length; catIdx++) {
                            var category = categories[catIdx];
                            if (!category) continue;
                            
                            var categoryStr = category.toString().toLowerCase();
                            console.log('Processing category ' + catIdx + ': "' + category + '"');
                            
                            // Try to match category to party name
                            for (var partyName in partyNames) {
                                var partyNameLower = partyName.toLowerCase();
                                
                                if (categoryStr.includes(partyNameLower) || categoryStr === partyNameLower) {
                                    var partyValues = [];
                                    
                                    // Get y value for this category from each series (time period)
                                    for (var serIdx = 0; serIdx < chart.series.length; serIdx++) {
                                        var ser = chart.series[serIdx];
                                        if (ser.data && ser.data[catIdx]) {
                                            var point = ser.data[catIdx];
                                            // Point can be a number or an object with y property
                                            var yValue = (typeof point === 'number') ? point : (point.y !== undefined ? point.y : null);
                                            if (yValue !== null && yValue !== undefined && !isNaN(yValue)) {
                                                partyValues.push(yValue);
                                                console.log('Series ' + serIdx + ' (' + (ser.name || 'unnamed') + ') value for ' + partyName + ': ' + yValue);
                                            }
                                        }
                                    }
                                    
                                    var valuesStr = '[' + partyValues.join(', ') + ']';
                                    console.log('Party ' + partyName + ' (category ' + catIdx + ') values: ' + valuesStr);
                                    
                                    // Should have 6 values (one for each time period: Valget 2022 + 5 weeks)
                                    if (partyValues.length >= 6) {
                                        result[partyNames[partyName]] = partyValues.slice(0, 6);
                                        console.log('✓ Found data for ' + partyName + ' (' + partyNames[partyName] + '): ' + valuesStr);
                                    } else if (partyValues.length > 0) {
                                        console.log('✗ Party ' + partyName + ' has only ' + partyValues.length + ' values, need 6');
                                    }
                                    break; // Found this party, move to next category
                                }
                            }
                        }
                        
                        // Structure 2: Series are parties, data points are time periods
                        // If we didn't find data with Structure 1, try Structure 2
                        if (Object.keys(result).length === 0) {
                            console.log('Trying Structure 2: Series = parties');
                            for (var serIdx = 0; serIdx < chart.series.length; serIdx++) {
                                var ser = chart.series[serIdx];
                                if (!ser.data || ser.data.length === 0) continue;
                                
                                var serName = ser.name || '';
                                console.log('Checking series ' + serIdx + ': "' + serName + '"');
                                
                                // Try to match series name to party name
                                for (var partyName in partyNames) {
                                    var partyNameLower = partyName.toLowerCase();
                                    var serNameLower = serName.toLowerCase();
                                    
                                    if (serNameLower.includes(partyNameLower) || serNameLower === partyNameLower) {
                                        var partyValues = [];
                                        // Get all data points from this series (should be 6: Valget 2022 + 5 weeks)
                                        for (var dataIdx = 0; dataIdx < ser.data.length && dataIdx < 6; dataIdx++) {
                                            var point = ser.data[dataIdx];
                                            var yValue = (typeof point === 'number') ? point : (point.y !== undefined ? point.y : null);
                                            if (yValue !== null && yValue !== undefined && !isNaN(yValue)) {
                                                partyValues.push(yValue);
                                            }
                                        }
                                        
                                        if (partyValues.length >= 6) {
                                            result[partyNames[partyName]] = partyValues.slice(0, 6);
                                            var valuesStr2 = '[' + partyValues.slice(0, 6).join(', ') + ']';
                                            console.log('✓ Found data for ' + partyName + ' (' + partyNames[partyName] + ') via series: ' + valuesStr2);
                                        }
                                        break;
                                    }
                                }
                            }
                        }
                    } else {
                        console.log('Chart ' + i + ' has no xAxis categories, trying series-based extraction');
                        // No categories, try to extract from series names directly
                        for (var serIdx = 0; serIdx < chart.series.length; serIdx++) {
                            var ser = chart.series[serIdx];
                            if (!ser.data || ser.data.length === 0) continue;
                            
                            var serName = ser.name || '';
                            for (var partyName in partyNames) {
                                var partyNameLower = partyName.toLowerCase();
                                if (serName.toLowerCase().includes(partyNameLower)) {
                                    var partyValues = [];
                                    for (var dataIdx = 0; dataIdx < ser.data.length && dataIdx < 6; dataIdx++) {
                                        var point = ser.data[dataIdx];
                                        var yValue = (typeof point === 'number') ? point : (point.y !== undefined ? point.y : null);
                                        if (yValue !== null && yValue !== undefined && !isNaN(yValue)) {
                                            partyValues.push(yValue);
                                        }
                                    }
                                    if (partyValues.length >= 6) {
                                        result[partyNames[partyName]] = partyValues.slice(0, 6);
                                        console.log('✓ Found data for ' + partyName + ' (' + partyNames[partyName] + ') via series name');
                                    }
                                    break;
                                }
                            }
                        }
                    }
                    
                    // Fallback: Check if series names contain party names
                    if (Object.keys(result).length === 0) {
                        for (var j = 0; j < chart.series.length; j++) {
                            var series = chart.series[j];
                            if (!series.data || series.data.length === 0) continue;
                            
                            var seriesName = series.name || '';
                            for (var partyName in partyNames) {
                                if (seriesName.includes(partyName)) {
                                    var values = [];
                                    for (var k = 0; k < series.data.length && k < 6; k++) {
                                        var point = series.data[k];
                                        var yValue = (typeof point === 'number') ? point : (point.y !== undefined ? point.y : null);
                                        if (yValue !== null && yValue !== undefined) {
                                            values.push(yValue);
                                        }
                                    }
                                    if (values.length === 6) {
                                        result[partyNames[partyName]] = values;
                                        console.log('Found data for ' + partyName + ' via series name: ' + JSON.stringify(values));
                                    }
                                }
                            }
                        }
                    }
                }
            }
            
            // Alternative: Try to extract from all chart series data points
            // In a grouped bar chart, we might need to iterate through all series and match by category/name
            if (Object.keys(result).length === 0 && typeof Highcharts !== 'undefined' && Highcharts.charts) {
                for (var i = 0; i < Highcharts.charts.length; i++) {
                    var chart = Highcharts.charts[i];
                    if (!chart || !chart.series) continue;
                    
                    // Try to get all data points and match them to parties
                    // Each series might represent a time period, and categories represent parties
                    var allSeriesData = [];
                    for (var s = 0; s < chart.series.length; s++) {
                        if (chart.series[s].data) {
                            allSeriesData.push(chart.series[s].data);
                        }
                    }
                    
                    // If we have categories (party names on x-axis)
                    if (chart.xAxis && chart.xAxis[0] && chart.xAxis[0].categories) {
                        var categories = chart.xAxis[0].categories;
                        for (var catIdx = 0; catIdx < categories.length; catIdx++) {
                            var category = categories[catIdx];
                            for (var partyName in partyNames) {
                                if (category && (category.toString().includes(partyName) || category.toString().toLowerCase().includes(partyName.toLowerCase()))) {
                                    var partyValues = [];
                                    // Get y value for this category from each series (time period)
                                    for (var serIdx = 0; serIdx < chart.series.length && partyValues.length < 6; serIdx++) {
                                        var ser = chart.series[serIdx];
                                        if (ser.data && ser.data[catIdx] && ser.data[catIdx].y !== undefined) {
                                            partyValues.push(ser.data[catIdx].y);
                                        }
                                    }
                                    if (partyValues.length >= 6) {
                                        result[partyNames[partyName]] = partyValues.slice(0, 6);
                                        console.log('Found data for ' + partyName + ' via series data: ' + partyValues.slice(0, 6));
                                    }
                                }
                            }
                        }
                    }
                }
            }
            
            // Alternative: Try to find data in chart configuration
            if (Object.keys(result).length === 0) {
                // Look for script tags with chart configuration
                var scripts = document.querySelectorAll('script');
                for (var s = 0; s < scripts.length; s++) {
                    var scriptText = scripts[s].textContent || scripts[s].innerHTML;
                    if (scriptText.includes('Highcharts') || scriptText.includes('chart')) {
                        // Try to extract series data
                        var seriesMatch = scriptText.match(/series:\\s*\\[([^\\]]+)\\]/);
                        if (seriesMatch) {
                            // Look for data arrays
                            var dataMatches = scriptText.match(/data:\\s*\\[([^\\]]+)\\]/g);
                            if (dataMatches) {
                                for (var d = 0; d < dataMatches.length; d++) {
                                    var dataStr = dataMatches[d];
                                    var numbers = dataStr.match(/\\d+[,.]?\\d*/g);
                                    if (numbers && numbers.length >= 6) {
                                        var values = numbers.slice(0, 6).map(function(n) {
                                            return parseFloat(n.replace(',', '.'));
                                        });
                                        // Try to match with party name nearby
                                        var contextStart = Math.max(0, scriptText.indexOf(dataMatches[d]) - 200);
                                        var context = scriptText.substring(contextStart, scriptText.indexOf(dataMatches[d]) + 200);
                                        for (var partyName in partyNames) {
                                            if (context.includes(partyName) && !result[partyNames[partyName]]) {
                                                result[partyNames[partyName]] = values;
                                                break;
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            
            // Return only primitive values, not objects with circular references
            var safeResult = {};
            for (var key in result) {
                if (result.hasOwnProperty(key) && Array.isArray(result[key])) {
                    safeResult[key] = result[key].slice(); // Copy array
                }
            }
            return safeResult;
        })();
        """
            
            # Get browser console logs to see what Highcharts found
            try:
                js_result = driver.execute_script(js_code)
                print(f"[DEBUG] JavaScript Highcharts extraction result: {js_result}", flush=True)
            except Exception as js_error:
                print(f"[WARNING] JavaScript execution error (might be circular reference): {js_error}", flush=True)
                js_result = {}
            
            # Also get console logs from browser
            try:
                logs = driver.get_log('browser')
                for log in logs:
                    if 'Found' in log['message'] or 'Chart' in log['message'] or 'Series' in log['message'] or 'categories' in log['message'].lower() or 'Party' in log['message'] or '✓' in log['message'] or '✗' in log['message']:
                        print(f"[BROWSER-CONSOLE] {log['message']}", flush=True)
            except:
                pass
            
            if js_result and isinstance(js_result, dict) and len(js_result) > 0:
                for party_code, values in js_result.items():
                    if isinstance(values, list) and len(values) >= 4:
                        # Take first 4 values (Seneste, Forrige, Måned, Valget)
                        result[party_code] = values[:4]
                        print(f"[INFO] Found data for party {party_code} via Highcharts: {values[:4]}", flush=True)
                    elif isinstance(values, list) and len(values) >= 1:
                        # Pad to 4 values
                        while len(values) < 4:
                            values.append(values[-1] if values else 0)
                        result[party_code] = values[:4]
                        print(f"[INFO] Found partial data for party {party_code} via Highcharts (padded): {values[:4]}", flush=True)
            else:
                print(f"[WARNING] Highcharts extraction returned no data or empty result: {js_result}", flush=True)
            
            # For parties that only have Valget 2022 from PDF, try to get uge values from chart
            # by looking at the chart data structure more carefully
            parties_needing_uge = [code for code, vals in result.items() if isinstance(vals, list) and len(vals) == 1]
            if parties_needing_uge:
                print(f"[INFO] Parties needing Uge 41-45 values: {parties_needing_uge}", flush=True)
        
        # If we don't have all parties yet, try hovering over bars
        if len(result) < 6:
            print("[INFO] Attempting to extract data by hovering over chart bars...", flush=True)
            
            # Find SVG elements (Highcharts uses SVG)
            svg_elements = driver.find_elements(By.XPATH, "//svg[contains(@class, 'highcharts') or ancestor::*[contains(@class, 'highcharts')]]")
            if not svg_elements:
                svg_elements = driver.find_elements(By.TAG_NAME, "svg")
            
            print(f"[INFO] Found {len(svg_elements)} SVG elements", flush=True)
            
            for svg in svg_elements:
                svg_text = svg.text
                if "Socialdemokratiet" in svg_text or "Telefoninterview" in svg_text or "Valget 2022" in svg_text:
                    print("[INFO] Found chart SVG, trying to hover over bars...", flush=True)
                    
                    # Find all rectangles (bars) in the SVG
                    bars = svg.find_elements(By.TAG_NAME, "rect")
                    print(f"[INFO] Found {len(bars)} rectangles in SVG", flush=True)
                    
                    # Group bars by party (each party has 6 bars)
                    # Try hovering over bars to get tooltips
                    actions = ActionChains(driver)
                    processed_parties = set()
                    
                    for idx, bar in enumerate(bars):
                        if len(processed_parties) >= 6:
                            break
                            
                        try:
                            # Get bar position to identify which party it belongs to
                            bar_x = bar.get_attribute("x")
                            bar_y = bar.get_attribute("y")
                            bar_width = bar.get_attribute("width")
                            bar_height = bar.get_attribute("height")
                            
                            # Only process bars with reasonable size (actual chart bars)
                            if bar_width and bar_height:
                                try:
                                    w = float(bar_width)
                                    h = float(bar_height)
                                    if w > 5 and h > 5:  # Filter out small UI elements
                                        # Scroll bar into view
                                        driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'instant'});", bar)
                                        sleep(0.3)
                                        
                                        # Hover over bar
                                        actions.move_to_element(bar).pause(0.5).perform()
                                        sleep(0.8)  # Wait for tooltip to appear
                                        
                                        # Look for tooltip
                                        tooltips = driver.find_elements(By.XPATH, 
                                            "//*[contains(@class, 'highcharts-tooltip') or contains(@class, 'tooltip')]")
                                        
                                        if not tooltips:
                                            # Try to find tooltip by text content
                                            tooltips = driver.find_elements(By.XPATH, 
                                                "//*[contains(text(), 'Valget 2022') or contains(text(), 'Uge 41')]")
                                        
                                        if tooltips:
                                            # Get both text and HTML of tooltip
                                            tooltip_text = tooltips[0].text
                                            tooltip_html = tooltips[0].get_attribute("innerHTML") or tooltips[0].get_attribute("outerHTML") or ""
                                            print(f"[DEBUG] Tooltip {idx} (x={bar_x}, y={bar_y}): {tooltip_text[:150]}", flush=True)
                                            if tooltip_html:
                                                print(f"[DEBUG] Tooltip HTML: {tooltip_html[:300]}", flush=True)
                                            
                                            # Parse tooltip for party and values
                                            for party_name, party_code in party_mapping.items():
                                                if party_code not in processed_parties and party_name in tooltip_text:
                                                    # Try to extract from HTML first (more reliable)
                                                    numbers = []
                                                    if tooltip_html:
                                                        # Look for patterns like <b>27.5</b> or <span>27.5%</span> or data-value="27.5"
                                                        html_numbers = re.findall(r'(?:<[^>]*>)?(\d+[,.]?\d*)\s*%?(?:</[^>]*>)?|data-value=["\'](\d+[,.]?\d*)["\']|>(\d+[,.]?\d*)%?<', tooltip_html)
                                                        for match in html_numbers:
                                                            num = match[0] if match[0] else (match[1] if match[1] else match[2])
                                                            if num:
                                                                numbers.append(num)
                                                    
                                                    # Fallback to text extraction
                                                    if len(numbers) < 6:
                                                        text_numbers = re.findall(r'(\d+[,.]?\d*)', tooltip_text)
                                                        numbers.extend(text_numbers)
                                                    
                                                    # Filter to reasonable percentages (0-100)
                                                    valid_numbers = []
                                                    for n in numbers:
                                                        try:
                                                            val = float(n.replace(',', '.'))
                                                            # Exclude years and week numbers
                                                            if 0 <= val <= 100 and val not in [2022, 2025, 41, 42, 43, 44, 45]:
                                                                valid_numbers.append(val)
                                                        except ValueError:
                                                            pass
                                                    
                                                    # Remove duplicates while preserving order
                                                    seen = set()
                                                    unique_numbers = []
                                                    for v in valid_numbers:
                                                        if v not in seen:
                                                            seen.add(v)
                                                            unique_numbers.append(v)
                                                    
                                                    if len(unique_numbers) >= 6:
                                                        result[party_code] = unique_numbers[:6]
                                                        processed_parties.add(party_code)
                                                        print(f"[INFO] Found data for {party_name} ({party_code}): {result[party_code]}", flush=True)
                                                        break
                                        else:
                                            # Try JavaScript to get tooltip
                                            # Try JavaScript to get tooltip with more detail
                                            tooltip_js = """
                                            (function() {
                                                // Try multiple ways to get tooltip
                                                var tooltip = document.querySelector('.highcharts-tooltip, [class*="tooltip"], [class*="highcharts-tooltip-box"]');
                                                if (tooltip) {
                                                    // Get all text including nested elements
                                                    var text = tooltip.innerText || tooltip.textContent || '';
                                                    // Also try to get from Highcharts directly
                                                    if (typeof Highcharts !== 'undefined' && Highcharts.charts) {
                                                        for (var i = 0; i < Highcharts.charts.length; i++) {
                                                            var chart = Highcharts.charts[i];
                                                            if (chart && chart.tooltip) {
                                                                var tooltipText = chart.tooltip.getLabel ? chart.tooltip.getLabel().text : '';
                                                                if (tooltipText) text = tooltipText;
                                                            }
                                                        }
                                                    }
                                                    return text;
                                                }
                                                return '';
                                            })();
                                            """
                                            tooltip_text_js = driver.execute_script(tooltip_js)
                                            if tooltip_text_js and len(tooltip_text_js) > 10:
                                                print(f"[DEBUG] Tooltip via JS {idx}: {tooltip_text_js[:200]}", flush=True)
                                                for party_name, party_code in party_mapping.items():
                                                    if party_code not in processed_parties and party_name in tooltip_text_js:
                                                        # Look for patterns like "27.5" or "27,5" followed by % or newline
                                                        # Also look for patterns like "Valget 2022: 27.5" or "Uge 41: 20.3"
                                                        numbers = re.findall(r'(?:Valget\s+2022|Uge\s+\d+)[:\s]+(\d+[,.]?\d*)|(\d+[,.]?\d*)\s*%', tooltip_text_js)
                                                        # Flatten the tuples and extract numbers
                                                        all_numbers = []
                                                        for match in numbers:
                                                            num = match[0] if match[0] else match[1]
                                                            if num:
                                                                all_numbers.append(num)
                                                        
                                                        # If we didn't find pattern matches, try general number extraction
                                                        if len(all_numbers) < 6:
                                                            all_numbers = re.findall(r'(\d+[,.]?\d*)', tooltip_text_js)
                                                        
                                                        valid_numbers = []
                                                        for n in all_numbers:
                                                            try:
                                                                val = float(n.replace(',', '.'))
                                                                # Filter: percentages should be 0-100, but also exclude years (2022, 2025) and week numbers (41-45)
                                                                if 0 <= val <= 100 and val not in [2022, 2025, 41, 42, 43, 44, 45]:
                                                                    valid_numbers.append(val)
                                                            except ValueError:
                                                                pass
                                                        
                                                        if len(valid_numbers) >= 6:
                                                            result[party_code] = valid_numbers[:6]
                                                            processed_parties.add(party_code)
                                                            print(f"[INFO] Found data for {party_name} ({party_code}) via JS tooltip: {result[party_code]}", flush=True)
                                                            break
                                except (ValueError, TypeError):
                                    continue
                        except Exception as e:
                            if idx < 10:  # Only log first few errors
                                print(f"[WARNING] Error hovering over bar {idx}: {e}", flush=True)
                            continue
        
        # Fallback: Extract from page text/HTML
        if not result:
            print("[INFO] Trying fallback method: extracting from page text...", flush=True)
            page_source = driver.page_source
            soup = BeautifulSoup(page_source, "html.parser")
            
            # Look for script tags with chart data
            scripts = soup.find_all("script")
            for script in scripts:
                if script.string and ("Socialdemokratiet" in script.string or "highcharts" in script.string.lower()):
                    script_text = script.string
                    
                    # Try to find JSON data
                    # Look for patterns like: "Socialdemokratiet": [27.5, 20.3, 20, 21.2, 21.1, 21.1]
                    for party_name, party_code in party_mapping.items():
                        pattern = rf'{re.escape(party_name)}["\']?\s*:\s*\[([\d\s,.\]]+)\]'
                        match = re.search(pattern, script_text, re.IGNORECASE)
                        if match:
                            numbers_str = match.group(1)
                            numbers = re.findall(r'(\d+[,.]?\d*)', numbers_str)
                            if len(numbers) >= 6:
                                try:
                                    values = [float(n.replace(',', '.')) for n in numbers[:6]]
                                    result[party_code] = values
                                    print(f"[INFO] Found data for {party_name} ({party_code}) via script: {values}", flush=True)
                                except ValueError:
                                    pass
            
            # Alternative: Extract from visible text
            if not result:
                body_text = driver.find_element(By.TAG_NAME, "body").text
                
                # Look for patterns in text
                for party_name, party_code in party_mapping.items():
                    # Find section with party name
                    party_section_start = body_text.find(party_name)
                    if party_section_start > 0:
                        # Get surrounding text
                        section = body_text[max(0, party_section_start - 200):party_section_start + 1000]
                        
                        # Look for numbers near party name
                        # Pattern: party name followed by numbers (Valget 2022, Uge 41-45)
                        numbers = re.findall(r'(\d+[,.]?\d*)', section)
                        if len(numbers) >= 6:
                            try:
                                # Filter out years and other non-percentage numbers
                                # Percentages are typically between 0-100
                                valid_numbers = []
                                for n in numbers:
                                    val = float(n.replace(',', '.'))
                                    if 0 <= val <= 100:
                                        valid_numbers.append(val)
                                
                                if len(valid_numbers) >= 6:
                                    result[party_code] = valid_numbers[:6]
                                    print(f"[INFO] Found data for {party_name} ({party_code}) via text: {valid_numbers[:6]}", flush=True)
                            except ValueError:
                                pass
        
        return result
        
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()
        return {}


def write_payload(data: dict, output_path: Path) -> None:
    """Write party polling data to JSON file and database."""
    scraped_at = datetime.now(timezone.utc).isoformat()
    
    # Normalize data format - convert dict format to list format for JSON compatibility
    normalized_data = {}
    for party_code, party_data in data.items():
        if isinstance(party_data, dict) and "values" in party_data:
            # New format with dates
            normalized_data[party_code] = party_data["values"]
        elif isinstance(party_data, list):
            # Old format (list of values)
            normalized_data[party_code] = party_data
        else:
            # Unknown format, skip
            continue
    
    payload = {
        "scraped_at": scraped_at,
        "data": normalized_data,
        "format": {
            "description": "4 values per party: [Seneste måling, Forrige måling, 1 måned siden, Valget 2022]",
            "parties": {
                "A": "Socialdemokratiet",
                "B": "Radikale Venstre",
                "C": "Det Konservative Folkeparti",
                "F": "Socialistisk Folkeparti",
                "H": "Borgernes Parti",
                "I": "Liberal Alliance",
                "M": "Moderaterne",
                "O": "Dansk Folkeparti",
                "V": "Venstre",
                "Æ": "Danmarksdemokraterne",
                "Ø": "Enhedslisten",
                "Å": "Alternativet"
            }
        }
    }
    
    # Add dates to payload if available
    dates_data = {}
    for party_code, party_data in data.items():
        if isinstance(party_data, dict) and "values" in party_data:
            dates_data[party_code] = {
                "seneste_date": party_data.get("seneste_date"),
                "forrige_date": party_data.get("forrige_date"),
            }
    if dates_data:
        payload["dates"] = dates_data
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    
    print(f"[INFO] Wrote data to {output_path}", flush=True)
    
    # Save to database
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from db_helper import insert_polling_data
        
        saved_count = 0
        for party_code, party_data in data.items():
            if isinstance(party_data, dict) and "values" in party_data:
                values = party_data["values"]
                if len(values) >= 4:
                    polling_data = {
                        "party_code": party_code,
                        "seneste_maaling_value": values[0],
                        "seneste_maaling_date": party_data.get("seneste_date"),
                        "forrige_maaling_value": values[1],
                        "forrige_maaling_date": party_data.get("forrige_date"),
                        "maaned_siden_value": values[2],
                        "valget_2022_value": values[3],
                        "scraped_at": scraped_at,
                    }
                    if insert_polling_data(polling_data):
                        saved_count += 1
            elif isinstance(party_data, list) and len(party_data) >= 4:
                # Old format without dates
                polling_data = {
                    "party_code": party_code,
                    "seneste_maaling_value": party_data[0],
                    "seneste_maaling_date": None,
                    "forrige_maaling_value": party_data[1],
                    "forrige_maaling_date": None,
                    "maaned_siden_value": party_data[2],
                    "valget_2022_value": party_data[3],
                    "scraped_at": scraped_at,
                }
                if insert_polling_data(polling_data):
                    saved_count += 1
        
        if saved_count > 0:
            print(f"[INFO] Saved {saved_count} party polling records to database", flush=True)
        else:
            print(f"[WARNING] No polling data saved to database", flush=True)
    except Exception as e:
        print(f"[WARNING] Failed to save polling data to database: {e}", flush=True)
        import traceback
        traceback.print_exc()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Scrape Voxmeter party polling data")
    parser.add_argument(
        "--output",
        help="Optional output file path. Defaults to data/voxmeter_parties.json",
    )
    return parser.parse_args(argv)


def main() -> int:
    """Main entry point."""
    args = parse_args(sys.argv[1:])
    
    output_path = (
        Path(args.output) if args.output
        else DATA_DIR / "voxmeter_parties.json"
    )
    
    driver = None
    try:
        print("[INFO] Starting Voxmeter party data scraper...", flush=True)
        print("[INFO] Setting up Chrome driver...", flush=True)
        driver = setup_chrome_driver(headless=True)
        print("[INFO] Chrome driver ready", flush=True)
        
        data = scrape_party_data(driver)
        
        write_payload(data, output_path)
        
        if not data:
            print("[WARNING] No party data found", file=sys.stderr, flush=True)
            return 1
        
        print(f"[SUCCESS] Scraped data for {len(data)} parties:", flush=True)
        for party_code, values in data.items():
            print(f"  {party_code}: {values}", flush=True)
        
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

