# Reddit Scraping Practices Learning Module

This is a safe, educational Reddit post collector designed to help you analyze what developer communities are discussing regarding web scraping best practices, API controls, rate-limiting, and ethical data collection.

This module is built strictly for educational trend analysis. It is fully compliant with Reddit's official API policies and strictly follows white-hat user protection rules.

---

## 🛠️ Step-by-Step Setup

### Step 1: Install Dependencies
This module uses Reddit's official Python wrapper (**PRAW**) and **Pydantic v2** for secure type-safety.
To install the required libraries, run:
```bash
pip install praw pydantic httpx
```

### Step 2: Create Reddit API Credentials
1. Go to Reddit's App Preferences portal: [https://www.reddit.com/prefs/apps](https://www.reddit.com/prefs/apps)
2. Log in with your Reddit account.
3. Click the **"are you a developer? create an app..."** or **"create another app..."** button.
4. Fill in the fields:
   - **Name**: `LearningScrapingPractices` (or any custom name)
   - **App Type**: Select the **script** option.
   - **Description**: `Safe learning script for trend analysis.`
   - **about url**: Leave blank or enter a personal link.
   - **redirect uri**: `http://localhost:8080` (this is a placeholder required by Reddit for scripts but not actively used for simple OAuth scripts).
5. Click **"create app"**.
6. Note down your keys:
   - **Client ID**: The string located directly under "personal use script" (usually 14 characters).
   - **Client Secret**: The string next to "secret" (usually 27 characters).

### Step 3: Configure your Environment
1. Copy the `.env.example` file in this folder to `.env`:
   ```bash
   copy .env.example .env
   ```
2. Open `.env` and fill in the values:
   ```env
   REDDIT_CLIENT_ID=your_14_char_id
   REDDIT_CLIENT_SECRET=your_27_char_secret
   REDDIT_USER_AGENT=learning-reddit-practices-script/0.1 by your_username
   ```

---

## 🚀 Running the Collector

Run the script from your terminal:
```bash
python c:\Users\16p30\.antigravity\reddit\reddit_collect.py
```

### Script Execution Modes:
- **Standby Mode (Awaiting Configuration)**: If `.env` is empty or missing, the script will safely initialize, write a clean, empty `reddit_practices_report.json` and exit with friendly setup guidelines. It will **never** fake results.
- **Active Mode**: Once valid credentials are configured, the script queries the approved subreddits (e.g. `r/webscraping`, `r/learnpython`), extracts post metadata matching scraping keywords, validates all records through Pydantic, and saves the outputs:
  - `reddit/outputs/reddit_practices_report.json` - Raw validated structural JSON.
  - `reddit/outputs/reddit_practices_report.md` - Formatted human-readable report.
  - `reddit/outputs/reddit_metrics.json` - Complete execution metadata and performance records.
  - `reddit/reddit_scraper.log` - Step-by-step logging.

---

## 🔒 Hard Safety & Compliance Policies

For educational integrity, safety, and strict policy alignment, this script **cannot** and **will not**:
- **Impersonate regular users**: It runs strictly via Reddit's official Oauth client (PRAW).
- **Rotate User-Agents**: It uses a single, identifiable, custom user-agent following Reddit's guidelines.
- **Use proxies or bypass security**: It does not rotate proxies, mock sessions, or bypass Cloudflare/CAPTCHAs.
- **Scrape private data**: It only processes public subreddits, public posts, and public comments. It never accesses profiles, private DMs, deleted contents, or passwords.
- **Expose Usernames**: It dynamically hashes or redacts all usernames by default (`anon_user_...`) to protect privacy.
- **Overload Servers**: It uses a strict 3-second pacing delay between subreddit requests and adheres strictly to API rate limits.
