import os
import json
import time
import logging
import random
import argparse
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple
import httpx
from pydantic import ValidationError

# Calculate absolute paths relative to script location
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE_PATH = os.path.join(SCRIPT_DIR, ".env")
CONFIG_FILE_PATH = os.path.join(SCRIPT_DIR, "reddit_config.json")
LOG_FILE_PATH = os.path.join(SCRIPT_DIR, "reddit_scraper.log")

# Setup outputs directory
OUTPUTS_DIR = os.path.join(SCRIPT_DIR, "outputs")
os.makedirs(OUTPUTS_DIR, exist_ok=True)

REPORT_JSON_PATH = os.path.join(OUTPUTS_DIR, "vibeforge_trends.json")
REPORT_MD_PATH = os.path.join(OUTPUTS_DIR, "vibeforge_trends.md")
METRICS_JSON_PATH = os.path.join(OUTPUTS_DIR, "reddit_metrics.json")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE_PATH, mode="a", encoding="utf-8")
    ]
)
logger = logging.getLogger("RedditJSONScraper")

# Import models
try:
    from reddit_models import (
        RedditComment,
        RedditPost,
        SubredditResult,
        RedditPracticeReport,
        CollectorMetrics
    )
except ImportError:
    import sys
    sys.path.append(SCRIPT_DIR)
    from reddit_models import (
        RedditComment,
        RedditPost,
        SubredditResult,
        RedditPracticeReport,
        CollectorMetrics
    )

# Standard list of user agents to rotate for public endpoints
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
]

def load_env_vars() -> Dict[str, str]:
    """Manually parse .env variables to prevent external dotenv dependency issues."""
    vars_dict = {}
    if os.path.exists(ENV_FILE_PATH):
        with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    vars_dict[key.strip()] = val.strip()
    return vars_dict

class RedditJSONCollector:
    def __init__(self):
        self.start_time = datetime.now(timezone.utc)
        self.metrics = {
            "execution_start": self.start_time.isoformat(),
            "execution_end": "",
            "duration_seconds": 0.0,
            "subreddits_scraped": [],
            "total_posts_checked": 0,
            "matched_posts_count": 0,
            "matched_comments_count": 0,
            "status": "INIT",
            "errors": []
        }
        self.subreddits = []
        self.keywords = []
        self.limits = {}
        self.oauth_token = None
        self.use_oauth = False
        self.user_agent = "VibeForgeTrendsCollector/1.0"
        
        self.load_config()
        self.initialize_auth()

    def load_config(self):
        try:
            with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
                self.subreddits = config.get("subreddits", [])
                self.keywords = config.get("keywords", [])
                self.limits = config.get("limits", {})
            logger.info(f"Loaded config: {len(self.subreddits)} subreddits, {len(self.keywords)} keywords.")
        except Exception as e:
            error_msg = f"Failed to load config: {str(e)}"
            logger.error(error_msg)
            self.metrics["errors"].append(error_msg)
            self.metrics["status"] = "ERROR_CONFIG"

    def initialize_auth(self):
        """Check for Reddit OAuth credentials in .env and authenticate if present."""
        env = load_env_vars()
        client_id = env.get("REDDIT_CLIENT_ID", "").strip()
        client_secret = env.get("REDDIT_CLIENT_SECRET", "").strip()
        configured_ua = env.get("REDDIT_USER_AGENT", "").strip()

        if configured_ua:
            self.user_agent = configured_ua

        if client_id and client_secret:
            logger.info("Found Reddit client credentials in .env. Attempting to get OAuth token...")
            try:
                auth = (client_id, client_secret)
                headers = {"User-Agent": self.user_agent}
                data = {"grant_type": "client_credentials"}
                
                with httpx.Client(follow_redirects=True) as client:
                    response = client.post(
                        "https://www.reddit.com/api/v1/access_token",
                        auth=auth,
                        data=data,
                        headers=headers,
                        timeout=10.0
                    )
                    
                    if response.status_code == 200:
                        token_data = response.json()
                        self.oauth_token = token_data.get("access_token")
                        if self.oauth_token:
                            self.use_oauth = True
                            logger.info("Successfully authenticated. Using secure oauth.reddit.com API.")
                            return
                    
                    logger.warning(f"OAuth request failed with status code {response.status_code}. Falling back to public API.")
            except Exception as e:
                logger.error(f"Failed to authenticate with Reddit API: {e}. Falling back to public API.")
        else:
            logger.info("No credentials found in .env. Operating in unauthenticated fallback mode.")

    def write_metrics_and_outputs(self, status: str, errors: List[str], results: List[SubredditResult]):
        """Compile and write execution outputs securely."""
        end_time = datetime.now(timezone.utc)
        self.metrics["execution_end"] = end_time.isoformat()
        self.metrics["duration_seconds"] = (end_time - self.start_time).total_seconds()
        self.metrics["status"] = status
        self.metrics["errors"].extend(errors)

        # 1. Save Metrics JSON
        try:
            with open(METRICS_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(self.metrics, f, indent=2)
            logger.info(f"Metrics saved to {METRICS_JSON_PATH}")
        except Exception as e:
            logger.error(f"Failed to write metrics JSON: {e}")

        # 2. Save Report JSON
        report = RedditPracticeReport(
            results=results,
            generated_at=end_time.isoformat()
        )
        try:
            with open(REPORT_JSON_PATH, "w", encoding="utf-8") as f:
                f.write(report.model_dump_json(indent=2))
            logger.info(f"VibeForge trends report saved to {REPORT_JSON_PATH}")
        except Exception as e:
            logger.error(f"Failed to write report JSON: {e}")

        # 3. Save Report Markdown
        try:
            self.generate_markdown_report(report)
            logger.info(f"VibeForge trends markdown saved to {REPORT_MD_PATH}")
        except Exception as e:
            logger.error(f"Failed to write report markdown: {e}")

    def generate_markdown_report(self, report: RedditPracticeReport):
        lines = [
            "# VibeForge Developer & Scraping Trends Report",
            f"*Generated At: {report.generated_at}*",
            "",
            "This report aggregates builder observations, web scraping trends, and developer discussions gathered directly from Reddit.",
            "",
            "## 📊 High-Level Metrics",
            f"- **Total Subreddits Checked**: {len(self.metrics['subreddits_scraped'])}",
            f"- **Total Posts Checked**: {self.metrics['total_posts_checked']}",
            f"- **Matched Posts**: {self.metrics['matched_posts_count']}",
            f"- **Matched Comments**: {self.metrics['matched_comments_count']}",
            "",
            "## 📂 Compiled Subreddit Records"
        ]

        for result in report.results:
            lines.append(f"### r/{result.subreddit}")
            if not result.posts:
                lines.append("No posts matching keywords found in this subreddit.")
                lines.append("")
                continue

            for post in result.posts:
                lines.append(f"#### 📝 [{post.title}]({post.permalink})")
                lines.append(f"- **Score**: {post.score} | **Comments**: {post.comments_count}")
                lines.append(f"- **Keywords**: `{', '.join(post.keywords_matched)}`")
                if post.selftext_excerpt:
                    lines.append(f"> {post.selftext_excerpt}")
                
                if post.comments:
                    lines.append("##### Matched Key Comments:")
                    for comm in post.comments:
                        lines.append(f"- **Score {comm.score}**: {comm.body_excerpt}")
                lines.append("")

        with open(REPORT_MD_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def fetch_json(self, path: str, params: Dict = None) -> Tuple[Optional[Dict], Optional[int]]:
        """Fetch JSON data from Reddit, using OAuth endpoints if authenticated, else public fallback."""
        if params is None:
            params = {}

        if self.use_oauth:
            url = f"https://oauth.reddit.com{path}"
            headers = {
                "Authorization": f"Bearer {self.oauth_token}",
                "User-Agent": self.user_agent
            }
        else:
            # Strip trailing slash and add .json for public URL
            clean_path = path.rstrip("/")
            url = f"https://www.reddit.com{clean_path}.json"
            headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.5"
            }

        try:
            with httpx.Client(follow_redirects=True) as client:
                logger.info(f"Querying URL: {url}")
                response = client.get(url, headers=headers, params=params, timeout=12.0)
                return response.json() if response.status_code == 200 else None, response.status_code
        except Exception as e:
            logger.error(f"Network error requesting {url}: {e}")
            self.metrics["errors"].append(f"Network error: {str(e)}")
            return None, None

    def collect(self) -> List[SubredditResult]:
        collected_results = []
        delay = self.limits.get("request_delay_seconds", 2)
        max_posts = self.limits.get("max_posts_per_subreddit", 25)
        max_comments = self.limits.get("max_comments_per_post", 10)
        has_block_occurred = False

        for sub_name in self.subreddits:
            logger.info(f"Querying subreddit: r/{sub_name}...")
            time.sleep(delay)

            path = f"/r/{sub_name}/hot"
            feed_data, status_code = self.fetch_json(path, params={"limit": max_posts})
            
            if status_code == 403:
                has_block_occurred = True

            if not feed_data:
                logger.warning(f"Could not parse feed for r/{sub_name} (HTTP {status_code})")
                continue

            self.metrics["subreddits_scraped"].append(sub_name)
            children = feed_data.get("data", {}).get("children", [])
            sub_posts = []

            for child in children:
                if child.get("kind") != "t3":
                    continue
                self.metrics["total_posts_checked"] += 1
                post_data = child.get("data", {})

                title = post_data.get("title", "")
                selftext = post_data.get("selftext", "")
                
                title_lower = title.lower()
                selftext_lower = selftext.lower()
                matched = [kw for kw in self.keywords if kw.lower() in title_lower or kw.lower() in selftext_lower]

                if matched:
                    logger.info(f"Match found in r/{sub_name}: '{title[:50]}'")
                    self.metrics["matched_posts_count"] += 1
                    post_id = post_data.get("id")
                    collected_at = datetime.now(timezone.utc).isoformat()

                    # Query post comments listing
                    time.sleep(delay)
                    comments_path = f"/r/{sub_name}/comments/{post_id}"
                    comments_payload, _ = self.fetch_json(comments_path, params={"limit": max_comments})
                    post_comments = []

                    if comments_payload and isinstance(comments_payload, list) and len(comments_payload) > 1:
                        comment_listing = comments_payload[1]
                        comm_children = comment_listing.get("data", {}).get("children", [])
                        
                        for comm_child in comm_children:
                            if comm_child.get("kind") != "t1":
                                continue
                            comm_data = comm_child.get("data", {})
                            body = comm_data.get("body", "")
                            
                            if any(kw.lower() in body.lower() for kw in self.keywords):
                                self.metrics["matched_comments_count"] += 1
                                body_ex = body[:250] + "..." if len(body) > 250 else body
                                try:
                                    comment_model = RedditComment(
                                        id=comm_data.get("id", ""),
                                        post_id=post_id,
                                        body_excerpt=body_ex,
                                        score=comm_data.get("score", 0),
                                        created_utc=comm_data.get("created_utc", 0.0),
                                        collected_at=collected_at
                                    )
                                    post_comments.append(comment_model)
                                except ValidationError as ve:
                                    logger.debug(f"Pydantic Validation failed for comment: {ve}")

                    # Limit post content size
                    post_excerpt = selftext[:500] + "..." if len(selftext) > 500 else selftext
                    try:
                        post_model = RedditPost(
                            id=post_data.get("name", f"t3_{post_id}"),
                            title=title,
                            selftext_excerpt=post_excerpt if selftext else None,
                            score=post_data.get("score", 0),
                            comments_count=post_data.get("num_comments", 0),
                            url=post_data.get("url", ""),
                            permalink=f"https://reddit.com{post_data.get('permalink', '')}",
                            created_utc=post_data.get("created_utc", 0.0),
                            subreddit=sub_name,
                            collected_at=collected_at,
                            keywords_matched=matched,
                            comments=post_comments
                        )
                        sub_posts.append(post_model)
                    except ValidationError as ve:
                        logger.warning(f"Pydantic validation failed for post: {ve}")

            sub_result = SubredditResult(subreddit=sub_name, posts=sub_posts)
            collected_results.append(sub_result)

        if has_block_occurred and not self.use_oauth:
            print("\n" + "="*80)
            print("⚠️  REDDIT PUBLIC ENDPOINT BLOCK DETECTED (HTTP 403 Forbidden)")
            print("Reddit's network security is blocking unauthenticated public JSON requests.")
            print("\nTo bypass this block, please configure a free Reddit API app:")
            print("1. Go to https://www.reddit.com/prefs/apps")
            print("2. Scroll to the bottom and click 'create another app...'")
            print("3. Enter a name (e.g. 'vibeforge-scraper'), select 'script',")
            print("   and set the redirect URI to 'http://localhost:8080'.")
            print("4. Copy the Client ID (the text under the app name) and the Client Secret.")
            print("5. Open your '.env' file at:")
            print(f"   {ENV_FILE_PATH}")
            print("6. Fill in the credentials:")
            print("   REDDIT_CLIENT_ID=your_client_id")
            print("   REDDIT_CLIENT_SECRET=your_client_secret")
            print("="*80 + "\n")

        return collected_results

    def merge_to_workspace(self, results: List[SubredditResult]):
        """Merge harvested trends into the root financial_report.json folder matching schema."""
        root_dir = os.path.dirname(SCRIPT_DIR)
        report_file_path = os.path.join(root_dir, "financial_report.json")

        logger.info(f"Looking to merge trends array with root unified report: {report_file_path}")

        # Translate scraped results to standard news items schema
        new_items = []
        for subreddit_res in results:
            for post in subreddit_res.posts:
                published_str = datetime.fromtimestamp(post.created_utc, timezone.utc).isoformat()
                item = {
                    "title": post.title,
                    "summary": post.selftext_excerpt if post.selftext_excerpt else "",
                    "url": post.permalink,
                    "published": published_str,
                    "fetched_at": post.collected_at,
                    "source": "Reddit_Trends"
                }
                new_items.append(item)

        if not new_items:
            logger.info("No matching Reddit Trends matched to append. Skipping merge step.")
            return

        # Load existing output file
        workspace_data = {"results": [], "errors": []}
        if os.path.exists(report_file_path):
            try:
                with open(report_file_path, "r", encoding="utf-8") as f:
                    workspace_data = json.load(f)
            except Exception as e:
                logger.error(f"Failed to read existing report file: {e}")

        # Find or create source key results block
        reddit_trends_block = None
        for res_block in workspace_data.get("results", []):
            if res_block.get("source") == "Reddit_Trends":
                reddit_trends_block = res_block
                break

        if reddit_trends_block is None:
            reddit_trends_block = {"source": "Reddit_Trends", "articles": []}
            if "results" not in workspace_data:
                workspace_data["results"] = []
            workspace_data["results"].append(reddit_trends_block)

        # Merge unique items based on URL
        existing_urls = {item.get("url") for item in reddit_trends_block["articles"]}
        merged_count = 0
        for item in new_items:
            if item["url"] not in existing_urls:
                reddit_trends_block["articles"].append(item)
                existing_urls.add(item["url"])
                merged_count += 1

        logger.info(f"Merged {merged_count} new trends articles under 'Reddit_Trends' source key.")

        # Save back to file
        try:
            with open(report_file_path, "w", encoding="utf-8") as f:
                json.dump(workspace_data, f, indent=2)
            logger.info("Workspace database successfully updated with merged trends.")
        except Exception as e:
            logger.error(f"Failed to write merged output file: {e}")

def main():
    parser = argparse.ArgumentParser(description="Query public Reddit endpoints to parse developer trends without API keys.")
    parser.add_argument("--merge", action="store_true", help="Merge scraped trends under Reddit_Trends key inside root unified JSON report.")
    args = parser.parse_args()

    collector = RedditJSONCollector()
    results = collector.collect()
    
    status = "SUCCESS" if not collector.metrics["errors"] else "PARTIAL_SUCCESS"
    if not collector.metrics["subreddits_scraped"]:
        status = "FAILED_BLOCKED"
        logger.error("All endpoints returned empty payload. Datacenter or IP blocking active.")
    
    collector.write_metrics_and_outputs(status, [], results)

    if args.merge or True:  # Default to merge to maintain parity
        collector.merge_to_workspace(results)

if __name__ == "__main__":
    main()
