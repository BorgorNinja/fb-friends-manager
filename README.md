# fb-friends-manager

Selenium + ChromeDriver tool that scrapes your Facebook friends list into a CSV with full audit columns.

---

## Requirements

- Python 3.11+
- Google Chrome installed
- ChromeDriver (auto-downloaded via `webdriver-manager`, or supply your own)

```bash
pip install -r requirements.txt
```

---

## Usage

### Basic (opens Chrome, you log in manually)
```bash
python fb_friends_manager.py
```

### Use existing Chrome session (recommended)
Pass your Chrome `--user-data-dir` so you're already logged into Facebook:

```bash
# macOS
python fb_friends_manager.py me \
  --profile-dir "$HOME/Library/Application Support/Google/Chrome/Default"

# Linux
python fb_friends_manager.py me \
  --profile-dir "$HOME/.config/google-chrome/Default"

# Windows
python fb_friends_manager.py me \
  --profile-dir "%LOCALAPPDATA%\Google\Chrome\User Data\Default"
```

### Scrape another user's friends (only visible mutual friends)
```bash
python fb_friends_manager.py someusername -o someusername_friends.csv
```

### All options
```
positional arguments:
  username              Facebook username (default: 'me')

options:
  -o, --output          Output CSV path          (default: fb_friends.csv)
  -p, --profile-dir     Chrome user-data-dir     (optional but recommended)
  --chromedriver        Path to chromedriver     (auto-downloaded if omitted)
  --headless            Run headless Chrome
  --enrich              Visit each profile to extract location & work (slow)
```

---

## CSV columns

| Column | Description |
|---|---|
| `name` | Friend's display name |
| `profile_url` | Clean Facebook profile URL |
| `profile_id` | Vanity slug or numeric UID |
| `mutual_friends` | Mutual friend count (if visible) |
| `location` | Location (populated with `--enrich`) |
| `work` | Work/education (populated with `--enrich`) |
| `record_hash` | SHA-256 fingerprint (first 16 chars) of name + id |
| `scraped_at_utc` | ISO-8601 UTC timestamp of the scrape |
| `scrape_session_id` | Unique ID for this run |
| `source_page` | Friends list URL that was scraped |
| `row_index` | Position in the scraped list |
| `is_duplicate` | `True` if the same profile_id appeared more than once |

---

## Notes

- **Log in first.** Facebook requires authentication. Use `--profile-dir` to reuse an existing session.
- **Headless not recommended.** Facebook is more likely to block headless browsers.
- **`--enrich` is slow.** It visits every profile page individually.
- This tool is for personal data management of your own friends list. Always respect Facebook's Terms of Service.

---

## License

MIT
