from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

class RedditComment(BaseModel):
    id: str = Field(..., description="Unique comment identifier")
    post_id: str = Field(..., description="Identifier of the parent post")
    body_excerpt: str = Field(..., description="Redacted/truncated body excerpt of the public comment")
    score: int = Field(..., description="Public net score of the comment")
    created_utc: float = Field(..., description="Timestamp of comment creation in UTC")
    collected_at: str = Field(..., description="Timestamp of when data was collected")

class RedditPost(BaseModel):
    id: str = Field(..., description="Unique Reddit post identifier (fullname)")
    title: str = Field(..., description="Post title")
    selftext_excerpt: Optional[str] = Field(None, description="Truncated body excerpt of public post text")
    score: int = Field(..., description="Public net score of the post")
    comments_count: int = Field(..., description="Total comment count on the post")
    url: str = Field(..., description="Target outbound URL or post link")
    permalink: str = Field(..., description="Link to post on Reddit")
    created_utc: float = Field(..., description="Timestamp of post creation in UTC")
    subreddit: str = Field(..., description="Subreddit name")
    collected_at: str = Field(..., description="Timestamp of when data was collected")
    keywords_matched: List[str] = Field(default=[], description="Keywords matched in title or selftext")
    comments: List[RedditComment] = Field(default=[], description="List of matched comments")

class SubredditResult(BaseModel):
    subreddit: str = Field(..., description="Name of the subreddit")
    posts: List[RedditPost] = Field(default=[], description="List of validated posts matching keywords")

class RedditPracticeReport(BaseModel):
    results: List[SubredditResult] = Field(default=[], description="Subreddit compiled records")
    generated_at: str = Field(..., description="ISO timestamp of when the report was compiled")

class CollectorMetrics(BaseModel):
    execution_start: str = Field(..., description="Execution start timestamp")
    execution_end: str = Field(..., description="Execution end timestamp")
    duration_seconds: float = Field(..., description="Total pipeline execution duration")
    subreddits_scraped: List[str] = Field(default=[], description="List of subreddits successfully checked")
    total_posts_checked: int = Field(default=0, description="Total posts inspected across subreddits")
    matched_posts_count: int = Field(default=0, description="Number of posts matching configuration keywords")
    matched_comments_count: int = Field(default=0, description="Number of comments captured and validated")
    status: str = Field(..., description="Final pipeline execution status")
    errors: List[str] = Field(default=[], description="List of logged execution errors")
