#!/usr/bin/env python3
"""
GDELT Codebook Download & Convert Script
Downloads three GDELT Codebooks and converts them to JSON format.

Usage:
    python scripts/download_codebooks.py

Output:
    data/codebooks/cameo_event_codes.json   - CAMEO event codes (~300+ codes)
    data/codebooks/actor_codes.json         - Actor codes (~250+ codes)
    data/codebooks/theme_codes.json         - GKG theme codes (~59000+ codes)
"""

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "data" / "codebooks"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# --- Source URLs ---
CAMEO_EVENT_CODES_URL = "https://www.gdeltproject.org/data/lookups/CAMEO.eventcodes.txt"
ACTOR_CODES_URL = "https://www.gdeltproject.org/data/lookups/CAMEO.country.txt"
THEME_CODES_URL = "http://data.gdeltproject.org/api/v2/guides/LOOKUP-GKGTHEMES.TXT"

# --- Fallback URL if primary fails ---
FALLBACK_THEME_URL = "https://github.com/linwoodc3/gdeltpytools/raw/master/gdeltpytools/data/gkg_themes.txt"
FALLBACK_EVENT_URL = "https://raw.githubusercontent.com/lqb718/NewsEngine/main/data/gdelt_theme_codebook.txt"


def fetch_url(url: str, timeout: int = 60) -> str:
    """Fetch URL content with error handling."""
    print(f"  Fetching: {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            print(f"  ✓ Received {len(data)} bytes")
            return data
    except urllib.error.HTTPError as e:
        print(f"  ✗ HTTP Error {e.code}: {e.reason}")
        raise
    except urllib.error.URLError as e:
        print(f"  ✗ URL Error: {e.reason}")
        raise
    except Exception as e:
        print(f"  ✗ Error: {e}")
        raise


def convert_cameo_events(raw: str) -> dict:
    """Convert CAMEO event codes tab-separated text to dict."""
    result = {}
    code = None
    for line in raw.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("CAMEO"):
            continue
        # Format: CODE\tDESCRIPTION
        parts = line.split("\t", 1)
        if len(parts) == 2:
            key = parts[0].strip()
            value = parts[1].strip()
            # Remove trailing \r
            key = key.rstrip("\r")
            value = value.rstrip("\r")
            if key and value:
                result[key] = value
        elif len(parts) == 1 and parts[0].strip():
            # Some lines might be continuations
            val = parts[0].strip().rstrip("\r")
            if val and code and not val.startswith("[") and not val.endswith("]"):
                result[code] = result.get(code, "") + " " + val
    return result


def convert_actor_codes(raw: str) -> dict:
    """Convert CAMEO country/actor codes to dict."""
    result = {}
    for line in raw.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("CODE"):
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            key = parts[0].strip().rstrip("\r")
            value = parts[1].strip().rstrip("\r")
            if key and value:
                result[key] = value
    return result


def generate_theme_description(code: str) -> str:
    """Generate a human-readable description from a GKG theme code.
    
    GKG theme codes are self-describing hierarchical identifiers.
    This converts e.g. 'WB_696_PUBLIC_SECTOR_MANAGEMENT' to 
    'World Bank - Public Sector Management'.
    """
    # Known prefixes for better descriptions
    prefix_map = {
        "WB_": "World Bank",
        "TAX_": "Taxonomy",
        "EPU_": "Economic Policy Uncertainty",
        "CRISISLEX_": "CrisisLex",
        "SOC_": "Social",
        "UNGP_": "UN Guiding Principles",
        "USPEC_": "US Political/Economic",
        "GENERAL_": "General",
        "MEDIA_": "Media",
        "MANMADE_DISASTER": "Manmade Disaster",
    }
    
    # Find matching prefix
    description = code.replace("_", " ").title()
    for prefix, label in sorted(prefix_map.items(), key=lambda x: -len(x[0])):
        if code.startswith(prefix):
            remainder = code[len(prefix):]
            if remainder:
                description = f"{label} - {remainder.replace('_', ' ').title()}"
            else:
                description = label
            break
    
    return description


def convert_theme_codes(raw: str, use_as_count: bool = True) -> dict:
    """Convert GKG theme codes to dict with descriptions.
    
    The raw format is THEME_CODE<TAB>COUNT.
    We use the count as-is and generate human-readable descriptions.
    """
    result = {}
    for line in raw.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t", 1)
        if len(parts) >= 1:
            code = parts[0].strip().rstrip("\r")
            if code:
                # Generate description from the code name
                description = generate_theme_description(code)
                result[code] = description
    return result


def save_json(data: dict, filepath: Path) -> None:
    """Save dict as formatted JSON."""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  ✓ Saved {len(data)} entries to {filepath}")


def validate_json(filepath: Path) -> bool:
    """Validate JSON file is loadable."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"  ✓ Valid JSON: {len(data)} entries, {filepath.name}")
        return True
    except json.JSONDecodeError as e:
        print(f"  ✗ Invalid JSON: {e}")
        return False


def main():
    print("=" * 60)
    print("GDELT Codebook Download & Convert")
    print("=" * 60)
    
    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nOutput directory: {OUTPUT_DIR}")
    
    success = True
    
    # --- 1. CAMEO Event Codes ---
    print("\n" + "-" * 40)
    print("1. CAMEO Event Codes")
    print("-" * 40)
    try:
        raw = fetch_url(CAMEO_EVENT_CODES_URL)
        cameo_data = convert_cameo_events(raw)
        save_json(cameo_data, OUTPUT_DIR / "cameo_event_codes.json")
        print(f"   Entries: {len(cameo_data)}")
        if len(cameo_data) < 300:
            print(f"   ⚠ Warning: Only {len(cameo_data)} codes (expected 300+)")
    except Exception as e:
        print(f"  ✗ Failed to download CAMEO event codes: {e}")
        success = False
    
    # --- 2. Actor Codes ---
    print("\n" + "-" * 40)
    print("2. Actor Codes (Countries & Regions)")
    print("-" * 40)
    try:
        raw = fetch_url(ACTOR_CODES_URL)
        actor_data = convert_actor_codes(raw)
        save_json(actor_data, OUTPUT_DIR / "actor_codes.json")
        print(f"   Entries: {len(actor_data)}")
        if len(actor_data) < 200:
            print(f"   ⚠ Warning: Only {len(actor_data)} codes (expected 200+)")
    except Exception as e:
        print(f"  ✗ Failed to download Actor codes: {e}")
        success = False
    
    # --- 3. GKG Theme Codes ---
    print("\n" + "-" * 40)
    print("3. GKG Theme Codes")
    print("-" * 40)
    try:
        raw = fetch_url(THEME_CODES_URL)
        theme_data = convert_theme_codes(raw)
        save_json(theme_data, OUTPUT_DIR / "theme_codes.json")
        print(f"   Entries: {len(theme_data)}")
        if len(theme_data) < 50000:
            print(f"   ⚠ Warning: Only {len(theme_data)} codes (expected 50000+)")
    except Exception as e:
        print(f"  ✗ Failed to download Theme codes from primary URL: {e}")
        # Try fallback - use local file
        local_path = PROJECT_ROOT / "data" / "gdelt_theme_codebook.txt"
        if local_path.exists():
            print(f"  → Using local file: {local_path}")
            with open(local_path, "r", encoding="utf-8") as f:
                raw = f.read()
            theme_data = convert_theme_codes(raw)
            save_json(theme_data, OUTPUT_DIR / "theme_codes.json")
            print(f"   Entries: {len(theme_data)}")
        else:
            success = False
    
    # --- Validation ---
    print("\n" + "=" * 60)
    print("Validation")
    print("=" * 60)
    
    expected = {
        "cameo_event_codes.json": (300, "CAMEO event codes"),
        "actor_codes.json": (200, "Actor codes"),
        "theme_codes.json": (50000, "GKG Theme codes"),
    }
    
    all_valid = True
    for filename, (min_count, label) in expected.items():
        filepath = OUTPUT_DIR / filename
        if filepath.exists():
            valid = validate_json(filepath)
            if valid:
                with open(filepath, "r") as f:
                    data = json.load(f)
                count = len(data)
                print(f"  {label}: {count} entries (min {min_count}) {'✓' if count >= min_count else '⚠'}")
                if count < min_count:
                    all_valid = False
            else:
                all_valid = False
        else:
            print(f"  ✗ {filename} not found")
            all_valid = False
    
    if all_valid:
        print("\n✅ ALL CODEBOOKS GENERATED SUCCESSFULLY")
    else:
        print("\n⚠️  SOME CODEBOOKS DID NOT MEET EXPECTATIONS")
    
    return 0 if all_valid else 1


if __name__ == "__main__":
    sys.exit(main())
