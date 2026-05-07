from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


README_PATH = Path("README.md")
START_MARKER = "<!-- BLOG-POST-LIST:START -->"
END_MARKER = "<!-- BLOG-POST-LIST:END -->"
CONTRIBUTIONS_START_MARKER = "<!-- GITHUB-CONTRIBUTIONS:START -->"
CONTRIBUTIONS_END_MARKER = "<!-- GITHUB-CONTRIBUTIONS:END -->"
DEFAULT_BLOG_URL = "https://blog.hanclin.to/"
DEFAULT_BLOG_REPOSITORY = "HanClinto/SimpleGitBlog"
DEFAULT_MAX_POSTS = 3
DEFAULT_MAX_CONTRIBUTIONS = 5


@dataclass
class BlogPost:
    title: str
    url: str
    date: str
    excerpt: str
    source: str = "Blog Post"


@dataclass
class Contribution:
    title: str
    url: str
    repository: str
    merged_at: str


@dataclass
class ContributionFeed:
    contributions: list[Contribution]
    repositories: list[str]
    repository_count: int
    pull_request_count: int
    search_url: str
    user_repositories_url: str
    public_repository_count: int | None


def fetch_text(url: str, headers: dict[str, str] | None = None) -> str:
    request = Request(url, headers={"User-Agent": "HanClinto profile updater", **(headers or {})})
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def strip_markdown(value: str) -> str:
    text = re.sub(r"```.*?```", " ", value, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[#>*_~\-]+", " ", text)
    return compact_text(text)


def truncate(value: str, max_chars: int = 180) -> str:
    if len(value) <= max_chars:
        return value
    shortened = value[: max_chars - 1].rsplit(" ", 1)[0].rstrip(".,;: ")
    return f"{shortened}..." if shortened else f"{value[:max_chars - 3]}..."


class BlogIndexParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.posts: list[BlogPost] = []
        self._current: dict[str, str] | None = None
        self._field: str | None = None
        self._field_parts: list[str] = []
        self._in_title_link = False

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        for name, value in attrs:
            if name == "class" and value:
                return set(value.split())
        return set()

    @staticmethod
    def _attr(attrs: list[tuple[str, str | None]], attr_name: str) -> str | None:
        for name, value in attrs:
            if name == attr_name:
                return value
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag == "article" and "post-card" in classes:
            self._current = {}
            return

        if self._current is None:
            return

        if tag == "h3" and "post-card__title" in classes:
            self._field = "title"
            self._field_parts = []
        elif tag == "p" and "post-card__excerpt" in classes:
            self._field = "excerpt"
            self._field_parts = []
        elif tag == "time":
            self._field = "date"
            self._field_parts = []
        elif tag == "a" and self._field == "title":
            href = self._attr(attrs, "href")
            if href:
                self._current["url"] = urljoin(self.base_url, href)
            self._in_title_link = True
        elif tag == "a" and "source-badge" in classes:
            self._field = "source"
            self._field_parts = []

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._field:
            self._field_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return

        if tag == "a" and self._in_title_link:
            self._in_title_link = False
            return

        if self._field and tag in {"h3", "p", "time", "a"}:
            value = compact_text("".join(self._field_parts))
            if value:
                self._current[self._field] = value
            self._field = None
            self._field_parts = []

        if tag == "article":
            title = self._current.get("title", "")
            url = self._current.get("url", "")
            if title and url:
                self.posts.append(
                    BlogPost(
                        title=title,
                        url=url,
                        date=self._current.get("date", ""),
                        excerpt=self._current.get("excerpt", ""),
                        source=self._current.get("source", "Blog Post"),
                    )
                )
            self._current = None
            self._field = None
            self._field_parts = []
            self._in_title_link = False


def posts_from_blog_homepage(blog_url: str) -> list[BlogPost]:
    html = fetch_text(blog_url)
    parser = BlogIndexParser(blog_url)
    parser.feed(html)
    parser.close()
    return parser.posts


def posts_from_github_issues(blog_url: str, repository: str, max_posts: int) -> list[BlogPost]:
    api_url = f"https://api.github.com/repos/{repository}/issues?state=open&sort=created&direction=desc&per_page={max(max_posts * 3, 10)}"
    token = os.environ.get("BLOG_REPO_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    raw_issues = json.loads(fetch_text(api_url, headers=headers))
    posts: list[BlogPost] = []
    for issue in raw_issues:
        if "pull_request" in issue:
            continue
        labels = {label.get("name", "") for label in issue.get("labels", [])}
        if labels & {"draft", "hidden"}:
            continue
        number = issue.get("number")
        title = issue.get("title", "")
        if not number or not title:
            continue
        created_at = issue.get("created_at", "")
        date = created_at[:10] if created_at else ""
        excerpt = truncate(strip_markdown(issue.get("body") or ""))
        posts.append(
            BlogPost(
                title=title,
                url=urljoin(blog_url, f"posts/gh-{number}/"),
                date=date,
                excerpt=excerpt,
            )
        )
    return posts


def load_posts(blog_url: str, repository: str, max_posts: int) -> list[BlogPost]:
    errors: list[str] = []
    for loader in (
        lambda: posts_from_blog_homepage(blog_url),
        lambda: posts_from_github_issues(blog_url, repository, max_posts),
    ):
        try:
            posts = loader()
            if posts:
                return posts[:max_posts]
            errors.append("loader returned no posts")
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            errors.append(str(exc))

    raise RuntimeError("Could not load blog posts: " + " | ".join(errors))


def github_contributions_query(user: str) -> str:
    return f"author:{user} is:pr is:merged -user:{user}"


def github_contributions_url(user: str) -> str:
    return "https://github.com/search?" + urlencode({
        "q": github_contributions_query(user),
        "type": "pullrequests",
    })


def github_user_repositories_url(user: str) -> str:
    return f"https://github.com/{user}?" + urlencode({"tab": "repositories"})


def github_headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_public_repository_count(user: str, headers: dict[str, str]) -> int | None:
    try:
        payload = json.loads(fetch_text(f"https://api.github.com/users/{user}", headers=headers))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    count = payload.get("public_repos")
    return count if isinstance(count, int) else None


def github_issue_search_items(query: str, headers: dict[str, str]) -> tuple[int, list[dict]]:
    items: list[dict] = []
    total_count = 0
    page = 1
    per_page = 100
    while page <= 10:
        api_url = (
            "https://api.github.com/search/issues?"
            + urlencode({"q": query, "sort": "updated", "order": "desc", "per_page": per_page, "page": page})
        )
        payload = json.loads(fetch_text(api_url, headers=headers))
        if page == 1:
            total_count = int(payload.get("total_count", 0))
        page_items = payload.get("items", [])
        if not page_items:
            break
        items.extend(page_items)
        if len(items) >= min(total_count, 1000):
            break
        page += 1
    return total_count, items


def posts_from_github_contributions(user: str, max_contributions: int) -> ContributionFeed:
    search_url = github_contributions_url(user)
    user_repositories_url = github_user_repositories_url(user)
    headers = github_headers()
    public_repository_count = github_public_repository_count(user, headers)
    empty_feed = ContributionFeed([], [], 0, 0, search_url, user_repositories_url, public_repository_count)
    if max_contributions <= 0:
        return empty_feed

    query = github_contributions_query(user)
    pull_request_count, items = github_issue_search_items(query, headers)
    contributions: list[Contribution] = []
    repositories: set[str] = set()
    for item in items:
        repo_url = item.get("repository_url", "")
        repository = repo_url.rsplit("/repos/", 1)[-1] if "/repos/" in repo_url else repo_url.rsplit("/", 2)[-1]
        if repository.lower().startswith(f"{user.lower()}/"):
            continue
        title = item.get("title", "")
        url = item.get("html_url", "")
        if not title or not url or not repository:
            continue
        repositories.add(repository)
        if len(contributions) < max_contributions:
            contributions.append(
                Contribution(
                    title=title,
                    url=url,
                    repository=repository,
                    merged_at=(item.get("closed_at") or item.get("updated_at") or "")[:10],
                )
            )
    return ContributionFeed(
        contributions=contributions,
        repositories=sorted(repositories, key=str.casefold),
        repository_count=len(repositories),
        pull_request_count=pull_request_count or len(contributions),
        search_url=search_url,
        user_repositories_url=user_repositories_url,
        public_repository_count=public_repository_count,
    )


def escape_markdown(value: str) -> str:
    return value.replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def source_icon(source: str) -> str:
    normalized = compact_text(source)
    icon_parts: list[str] = []
    for char in normalized:
        if icon_parts:
            if char.isspace() or ord(char) <= 127:
                break
            icon_parts.append(char)
        elif ord(char) > 127:
            icon_parts.append(char)
    return "".join(icon_parts)


def render_posts(posts: Iterable[BlogPost]) -> str:
    lines: list[str] = []
    for post in posts:
        source = source_icon(post.source)
        title = escape_markdown(post.title)
        icon = f"{source} " if source else ""
        date = f" - _({post.date})_ -" if post.date else ""
        lines.append(f"- {icon}[{title}]({post.url}){date}")
        if post.excerpt:
            lines.append(f"  {escape_markdown(truncate(post.excerpt))}")
    return "\n".join(lines)


def render_contributions(feed: ContributionFeed) -> str:
    repo_word = "repository" if feed.repository_count == 1 else "repositories"
    pr_word = "PR" if feed.pull_request_count == 1 else "PRs"
    stat_parts = [
        f"[{feed.pull_request_count} merged public {pr_word}]({feed.search_url})",
        f"[{feed.repository_count} outside {repo_word}](#outside-repositories)",
    ]
    if feed.public_repository_count is not None:
        personal_repo_word = "repository" if feed.public_repository_count == 1 else "repositories"
        stat_parts.append(
            f"[{feed.public_repository_count} public personal {personal_repo_word}]({feed.user_repositories_url})"
        )
    lines = [" - ".join(stat_parts) + ".", ""]
    if not feed.contributions:
        lines.append("- No recent merged public PRs found.")
        return "\n".join(lines)

    for contribution in feed.contributions:
        title = escape_markdown(contribution.title)
        repository = escape_markdown(contribution.repository)
        date = f" - _({contribution.merged_at})_" if contribution.merged_at else ""
        lines.append(f"- [{title}]({contribution.url}){date} - {repository}")
    if feed.repositories:
        lines.extend([
            "",
            "<details>",
            "<summary>Outside repositories</summary>",
            "",
            "<a id=\"outside-repositories\"></a>",
            "",
        ])
        for repository in feed.repositories:
            escaped_repository = escape_markdown(repository)
            lines.append(f"- [{escaped_repository}](https://github.com/{repository})")
        lines.append("</details>")
    return "\n".join(lines)


def replace_marked_section(readme: str, start_marker: str, end_marker: str, rendered_content: str) -> str:
    replacement = f"{start_marker}\n{rendered_content}\n{end_marker}"
    pattern = re.compile(f"{re.escape(start_marker)}.*?{re.escape(end_marker)}", re.DOTALL)
    if not pattern.search(readme):
        raise RuntimeError(f"README is missing {start_marker} / {end_marker} markers")
    return pattern.sub(replacement, readme)


def main() -> int:
    blog_url = os.environ.get("BLOG_URL", DEFAULT_BLOG_URL).strip() or DEFAULT_BLOG_URL
    if not blog_url.endswith("/"):
        blog_url += "/"
    repository = os.environ.get("BLOG_REPOSITORY", DEFAULT_BLOG_REPOSITORY).strip() or DEFAULT_BLOG_REPOSITORY
    max_posts = int(os.environ.get("MAX_BLOG_POSTS", DEFAULT_MAX_POSTS))
    contribution_user = os.environ.get("GITHUB_CONTRIBUTIONS_USER", "HanClinto").strip() or "HanClinto"
    max_contributions = int(os.environ.get("MAX_GITHUB_CONTRIBUTIONS", DEFAULT_MAX_CONTRIBUTIONS))

    posts = load_posts(blog_url, repository, max_posts)
    contribution_feed = posts_from_github_contributions(contribution_user, max_contributions)
    readme = README_PATH.read_text(encoding="utf-8")
    updated = replace_marked_section(readme, START_MARKER, END_MARKER, render_posts(posts))
    updated = replace_marked_section(
        updated,
        CONTRIBUTIONS_START_MARKER,
        CONTRIBUTIONS_END_MARKER,
        render_contributions(contribution_feed),
    )
    README_PATH.write_text(updated, encoding="utf-8")
    print(
        f"Updated README with {len(posts)} post(s) from {blog_url} and "
        f"{len(contribution_feed.contributions)} contribution(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
