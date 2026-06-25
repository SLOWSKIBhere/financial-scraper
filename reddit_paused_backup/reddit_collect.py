import os
import json
import time
import logging
from datetime import datetime, timezone
import hashlib
from typing import List, Dict, Optional, Tuple

# Calculate absolute paths relative to script location
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE_PATH = os.path.join(SCRIPT_DIR, ".env")
CONFIG_FILE_PATH = os.path.join(SCRIPT_DIR, "reddit_config.json")
LOG_FILE_PATH = os.path.join(SCRIPT_DIR, "reddit_scraper.log")

# Setup outputs directory
OUTPUTS_DIR = os.path.join(SCRIPT_DIR, "outputs")
os.makedirs(OUTPUTS_DIR, exist_ok=True)

REPORT_JSON_PATH = os.path.join(OUTPUTS_DIR, "reddit_practices_report.json")
REPORT_MD_PATH = os.path.join(OUTPUTS_DIR, "reddit_practices_report.md")
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
logger = logging.getLogger("RedditCollector")

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
    # Handle if run from outside the directory
    import sys
    sys.path.append(SCRIPT_DIR)
    from reddit_models import (
        RedditComment,
        RedditPost,
        SubredditResult,
        RedditPracticeReport,
        CollectorMetrics
    )

# Load environment variables manually to avoid dependency on python-dotenv
def load_env_vars() -> Dict[str, str]:
    env_vars = {}
    if os.path.exists(ENV_FILE_PATH):
        with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    env_vars[key.strip()] = val.strip()
    return env_vars

def anonymize_username(username: Optional[str]) -> str:
    """Anonymize usernames to adhere to safety guidelines (no private profile data)."""
    if not username:
        return "[deleted]"
    # Generate a consistent but non-reversible salt/hash
    hashed = hashlib.sha256(username.encode("utf-8")).hexdigest()[:8]
    return f"anon_user_{hashed}"

class RedditCollectorPipeline:
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
        self.load_config()

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

    def write_blocked_metrics(self, status: str, errors: List[str]):
        """Write final metrics file in case of blocked execution."""
        end_time = datetime.now(timezone.utc)
        self.metrics["execution_end"] = end_time.isoformat()
        self.metrics["duration_seconds"] = (end_time - self.start_time).total_seconds()
        self.metrics["status"] = status
        self.metrics["errors"].extend(errors)
        
        try:
            with open(METRICS_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(self.metrics, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to write metrics output: {e}")

    def generate_empty_report(self):
        """Generate empty structures to represent safe initialization."""
        report = RedditPracticeReport(
            results=[],
            generated_at=datetime.now(timezone.utc).isoformat()
        )
        with open(REPORT_JSON_PATH, "w", encoding="utf-8") as f:
            f.write(report.model_dump_json(indent=2))
            
        md_content = (
            "# Safe Reddit Collector Report\n\n"
            f"*Generated at: {report.generated_at}*\n\n"
            "[!] Status: Awaiting Reddit API Credentials\n\n"
            "This script is currently in educational standby mode. Please configure your Reddit OAuth API credentials "
            "in `.env` according to the instructions in `README_REDDIT.md` to collect data.\n"
        )
        with open(REPORT_MD_PATH, "w", encoding="utf-8") as f:
            f.write(md_content)

    def run(self):
        logger.info("Starting safe Reddit collector pipeline...")
        env = load_env_vars()
        
        client_id = env.get("REDDIT_CLIENT_ID", "").strip()
        client_secret = env.get("REDDIT_CLIENT_SECRET", "").strip()
        user_agent = env.get("REDDIT_USER_AGENT", "").strip()

        # Check for presence of PRAW
        try:
            import praw
        except ImportError:
            error_msg = "PRAW package is not installed. Please run: pip install praw"
            logger.error(error_msg)
            self.write_blocked_metrics("BLOCKED_DEPENDENCY", [error_msg])
            self.generate_empty_report()
            print(f"\n[X] SETUP REQUIRED: {error_msg}")
            print("Please see README_REDDIT.md for setup guidelines.\n")
            return False

        # Validate credentials exist
        if not client_id or not client_secret or not user_agent:
            error_msg = "Reddit API credentials are not configured in reddit/.env."
            logger.warning(error_msg)
            self.write_blocked_metrics("BLOCKED_CREDENTIALS", [error_msg])
            self.generate_empty_report()
            print("\n[!] AWAITING CONFIGURATION:")
            print("To securely query the Reddit API, you must configure your OAuth keys.")
            print(f"1. Copy .env.example to .env inside {SCRIPT_DIR}")
            print("2. Add your REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET values.")
            print("3. Refer to README_REDDIT.md for detailed registration steps.\n")
            return False

        # Initialize PRAW
        try:
            logger.info("Initializing Reddit API client...")
            reddit = praw.Reddit(
                client_id=client_id,
                client_secret=client_secret,
                user_agent=user_agent
            )
            
            # Simple read-only call to check credentials
            reddit.read_only = True
            # Try to resolve a neutral front-page call to test authentication
            list(reddit.front.hot(limit=1))
            logger.info("Successfully authenticated with Reddit API.")
        except Exception as e:
            error_msg = f"Reddit API Authentication failed: {str(e)}"
            logger.error(error_msg)
            self.write_blocked_metrics("BLOCKED_AUTHENTICATION", [error_msg])
            self.generate_empty_report()
            print(f"\n[X] AUTHENTICATION ERROR: {error_msg}")
            print("Please verify the credentials in your .env file.\n")
            return False

        # If authenticated, execute collection
        collected_results = []
        delay = self.limits.get("request_delay_seconds", 2)
        max_posts = self.limits.get("max_posts_per_subreddit", 25)
        max_comments = self.limits.get("max_comments_per_post", 10)

        for sub_name in self.subreddits:
            logger.info(f"Checking Subreddit: r/{sub_name}...")
            time.sleep(delay)  # Pacing and respecting rate limits
            
            try:
                subreddit = reddit.subreddit(sub_name)
                sub_posts = []
                
                # Fetch hot posts in the subreddit up to limit
                for post in subreddit.hot(limit=max_posts):
                    self.metrics["total_posts_checked"] += 1
                    
                    # Search post text and title for configured keywords
                    title_lower = post.title.lower()
                    selftext_lower = post.selftext.lower() if post.selftext else ""
                    
                    matched = [kw for kw in self.keywords if kw.lower() in title_lower or kw.lower() in selftext_lower]
                    
                    if matched:
                        logger.info(f"Matched post: '{post.title[:50]}...' in r/{sub_name}")
                        self.metrics["matched_posts_count"] += 1
                        collected_at = datetime.now(timezone.utc).isoformat()
                        
                        # Fetch matched comments for the post
                        comments_list = []
                        post.comments.replace_more(limit=0)  # Safe comment expansion, avoid huge queries
                        for comm in post.comments[:max_comments]:
                            body_lower = comm.body.lower() if comm.body else ""
                            # Check if the comment mentions keywords
                            if any(kw.lower() in body_lower for kw in self.keywords):
                                self.metrics["matched_comments_count"] += 1
                                # Limit body size for safety/storage
                                body_ex = comm.body[:250] + "..." if len(comm.body) > 250 else comm.body
                                
                                comment_model = RedditComment(
                                    id=comm.id,
                                    post_id=post.id,
                                    body_excerpt=body_ex,
                                    score=comm.score,
                                    created_utc=comm.created_utc,
                                    collected_at=collected_at
                                )
                                comments_list.append(comment_model)

                        post_excerpt = post.selftext[:500] + "..." if len(post.selftext) > 500 else post.selftext
                        
                        post_model = RedditPost(
                            id=post.id,
                            title=post.title,
                            selftext_excerpt=post_excerpt if post.selftext else None,
                            score=post.score,
                            comments_count=post.num_comments,
                            url=post.url,
                            permalink=f"https://reddit.com{post.permalink}",
                            created_utc=post.created_utc,
                            subreddit=sub_name,
                            collected_at=collected_at,
                            keywords_matched=matched,
                            comments=comments_list
                        )
                        sub_posts.append(post_model)

                sub_result = SubredditResult(subreddit=sub_name, posts=sub_posts)
                collected_results.append(sub_result)
                self.metrics["subreddits_scraped"].append(sub_name)

            except Exception as e:
                error_msg = f"Error processing r/{sub_name}: {str(e)}"
                logger.error(error_msg)
                self.metrics["errors"].append(error_msg)

        # Generate outputs
        end_time = datetime.now(timezone.utc)
        self.metrics["execution_end"] = end_time.isoformat()
        self.metrics["duration_seconds"] = (end_time - self.start_time).total_seconds()
        self.metrics["status"] = "SUCCESS" if not self.metrics["errors"] else "PARTIAL_SUCCESS"

        # 1. Save Report JSON
        report = RedditPracticeReport(
            results=collected_results,
            generated_at=end_time.isoformat()
        )
        try:
            with open(REPORT_JSON_PATH, "w", encoding="utf-8") as f:
                f.write(report.model_dump_json(indent=2))
        except Exception as e:
            self.metrics["errors"].append(f"Failed to write report JSON: {str(e)}")

        # 2. Save Metrics JSON
        try:
            with open(METRICS_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(self.metrics, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to write metrics: {e}")

        # 3. Save Report Markdown
        try:
            self.generate_markdown_report(report)
        except Exception as e:
            logger.error(f"Failed to write report markdown: {e}")

        logger.info(f"Pipeline executed successfully. Status: {self.metrics['status']}")
        return True

    def generate_markdown_report(self, report: RedditPracticeReport):
        lines = [
            "# Reddit Scraping Practices Report",
            f"*Generated At: {report.generated_at}*",
            "",
            "This report summarizes what developers and practitioners discuss regarding scraping best practices, blocks, and API constraints on Reddit.",
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
                lines.append("No posts matching scraping keywords found in this run.")
                lines.append("")
                continue

            for post in result.posts:
                lines.append(f"#### 📝 [{post.title}]({post.permalink})")
                lines.append(f"- **Score**: {post.score} | **Comments**: {post.comments_count}")
                lines.append(f"- **Keywords**: `{', '.join(post.keywords_matched)}`")
                if post.selftext_excerpt:
                    lines.append(f"> {post.selftext_excerpt[:300].strip()}...")
                
                if post.comments:
                    lines.append("##### Key Comments Discussing Best Practices:")
                    for comm in post.comments:
                        lines.append(f"- **Score {comm.score}**: {comm.body_excerpt.strip()}")
                lines.append("")

        with open(REPORT_MD_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

if __name__ == "__main__":
    collector = RedditCollectorPipeline()
    collector.run()
