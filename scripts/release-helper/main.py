import click
import os
import requests
import boto3
import re

from dataclasses import dataclass
from typing import Any, Callable, Optional
from datetime import datetime, timezone, timedelta
from dateutil.parser import parse as parse_datetime


_OPEN_JOB_DESCRIPTION = "OpenJobDescription"
_DEADLINE_CLOUD = "aws-deadline"


@dataclass
class GitHubRepository:
    owner: str
    name: str
    dependencies: Optional[set[str]] = None

    def get_latest_release(self, github_token: str) -> "GitHubRelease":
        """
        Get the most recent release of a specified GitHub repository that is not a prerelease or draft.

        Args:
            owner: The owner/organization of the repository
            repo: The repository name
            github_token: GitHub token for authentication

        Returns:
            GitHubRelease object

        Raises:
            requests.HTTPError: If the API request fails
            ValueError: If no releases are found or no stable releases exist
        """
        # Use the releases endpoint to get all releases, then filter
        url = f"https://api.github.com/repos/{self.owner}/{self.name}/releases"

        try:
            response = requests.get(
                url, headers=_github_api_header_boilerplate(github_token)
            )
            response.raise_for_status()

            releases_data = response.json()

            # Filter out prereleases and drafts, then get the most recent
            stable_releases = [
                release
                for release in releases_data
                if not release.get("prerelease", False)
                and not release.get("draft", False)
            ]

            if not stable_releases:
                raise ValueError(
                    f"Repository {self.owner}/{self.name} has no stable releases"
                )

            # The first release in the filtered list is the most recent stable release
            latest_release = stable_releases[0]

            return GitHubRelease(
                repo=self,
                tag_name=latest_release.get("tag_name"),
                published_at=parse_datetime(latest_release.get("published_at")),
            )

        except requests.HTTPError as e:
            if e.response.status_code == 404:
                raise ValueError(f"Repository {self.owner}/{self.name} not found")
            else:
                raise e
        except requests.RequestException as e:
            raise requests.RequestException(f"Failed to fetch release data: {str(e)}")


_WEEKLY_RELEASES: list[str] = [
    GitHubRepository(_OPEN_JOB_DESCRIPTION, "openjd-model-for-python"),
    GitHubRepository(
        _OPEN_JOB_DESCRIPTION, "openjd-sessions-for-python", {"openjd-model-for-python"}
    ),
    GitHubRepository(
        _OPEN_JOB_DESCRIPTION, "openjd-cli", {"openjd-sessions-for-python"}
    ),
    GitHubRepository(_OPEN_JOB_DESCRIPTION, "openjd-adaptor-runtime-for-python"),
    GitHubRepository(_DEADLINE_CLOUD, "deadline-cloud-test-fixtures"),
    GitHubRepository(_DEADLINE_CLOUD, "deadline-cloud-worker-agent"),
]


@dataclass
class GitHubRelease:
    repo: GitHubRepository
    tag_name: str
    published_at: datetime

    def _is_released_this_week(self) -> bool:
        """
        Check if this release was published during the current calendar week.

        Calendar week is defined as Monday to Sunday, following ISO 8601 standard.

        Returns:
            True if the release was published during the current calendar week, False otherwise
        """
        now = datetime.now(timezone.utc)

        # Ensure published_at is timezone-aware for comparison
        release_date = self.published_at
        if release_date.tzinfo is None:
            # Assume UTC if no timezone info
            release_date = release_date.replace(tzinfo=timezone.utc)

        # Get the current week's Monday (start of week)
        current_week_start = now - timedelta(days=now.weekday())
        current_week_start = current_week_start.replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        # Get the current week's Sunday (end of week)
        current_week_end = current_week_start + timedelta(
            days=6, hours=23, minutes=59, seconds=59, microseconds=999999
        )

        return current_week_start <= release_date <= current_week_end

    def _has_subsequent_releasable_commits(self, github_token: str) -> bool:
        """
        Check if there are commits with 'feat' or 'fix' conventional commit keywords since the given tag.

        Returns:
            True if there are commits with feat/fix keywords since the tag, False otherwise
        """
        # Get commits since the tag
        url = f"https://api.github.com/repos/{self.repo.owner}/{self.repo.name}/compare/{self.tag_name}...HEAD"

        try:
            response = requests.get(
                url, headers=_github_api_header_boilerplate(github_token)
            )
            response.raise_for_status()

            compare_data = response.json()
            commits = compare_data.get("commits", [])

            # Check each commit message for feat or fix keywords
            feat_fix_pattern = re.compile(r"^(feat|fix)(\(.+\))?:", re.IGNORECASE)

            for commit in commits:
                commit_message = commit.get("commit", {}).get("message", "")
                if feat_fix_pattern.match(commit_message):
                    return True

            return False

        except requests.HTTPError as e:
            if e.response.status_code == 404:
                # if comparison fails, assume we need a release to be safe
                return True
            else:
                raise e
        except requests.requestexception as e:
            raise requests.requestexception(f"Failed to fetch commit data: {str(e)}")

    def _needs_release(self, github_token: str) -> bool:
        """
        Determine if a repository needs a release based on:
        1. If there was a release this week, no release needed
        2. If there are no feat/fix commits since the latest release, no release needed
        3. Otherwise, a release is needed

        Returns:
            True if a release is needed, False otherwise
        """
        # If there was already a release this week, no need for another
        if self._is_released_this_week():
            return False

        # Check if there are releasable commits since the latest release
        return self._has_subsequent_releasable_commits(github_token)

    def get_release_status(self, github_token: str) -> "ReleaseStatus":
        date_str = self.published_at.strftime("%Y-%m-%d")
        relative_time = self._format_relative_time()
        release_needed = self._needs_release(github_token)
        return ReleaseStatus(
            self,
            release_needed,
            date_str,
            relative_time,
        )

    def _format_relative_time(self) -> str:
        """
        Format a datetime as a relative time string.

        Args:
            published_at: The datetime to format

        Returns:
            String like "(Today)", "(Yesterday)", "(3 days ago)", "(last week)", "(2 weeks ago)"
        """
        now = datetime.now(timezone.utc)

        # Ensure published_at is timezone-aware for comparison
        if self.published_at.tzinfo is None:
            published_at = self.published_at.replace(tzinfo=timezone.utc)
        else:
            published_at = self.published_at

        # Calculate the difference
        diff = now - published_at
        days_diff = diff.days

        if days_diff == 0:
            return "(Today)"
        elif days_diff == 1:
            return "(Yesterday)"
        elif days_diff < 7:
            return f"({days_diff} days ago)"
        elif days_diff < 14:
            return "(last week)"
        else:
            weeks_diff = days_diff // 7
            return f"({weeks_diff} weeks ago)"


@dataclass
class ReleaseStatus:
    latest_release: GitHubRelease
    release_needed: bool
    date_str: str
    relative_time: str

    def get_report(self) -> str:
        if self.release_needed:
            return f"⭕ {self.latest_release.repo.name} - {self.latest_release.tag_name} - {self.date_str} {self.relative_time}"
        else:
            return f"✅ {self.latest_release.repo.name} - {self.latest_release.tag_name} - {self.date_str} {self.relative_time}"


def _github_api_header_boilerplate(github_token: str) -> dict[str, str]:
    # The intersection of the sets of all headers of requests
    # made to the GitHub API by this application.
    return {
        "User-Agent": "Python Script",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Accept": "application/vnd.github+json",
    }


def _get_github_token(github_token_secret_arn: str, session: boto3.Session) -> str:
    secrets_manager = session.client("secretsmanager")
    response = secrets_manager.get_secret_value(SecretId=github_token_secret_arn)
    return response["SecretString"]


def _default_from_environment(
    env_var_name: str,
) -> Callable[[click.Context, click.Option, Any], Any]:
    # Click callback that defaults to using the value from
    # the given environment variable if no value is provided
    # for the arg and the environment variable is defined.
    def _callback(ctx: click.Context, opt: click.Option, val: Any) -> Any:
        if val is None:
            result = os.environ.get(env_var_name, None)
            if result is None:
                raise click.BadParameter(
                    f"Must provide a value for {opt.name} or set the {env_var_name} environment variable."
                )
            return result
        return val

    return _callback


@click.group(invoke_without_command=True)
@click.option(
    "--github-token-secret-arn",
    required=False,
    type=str,
    help="The ARN of the secret in AWS Secrets Manager that contains your GitHub token.",
    callback=_default_from_environment("GITHUB_TOKEN_SECRET_ARN"),
)
@click.option(
    "--aws-region",
    required=False,
    type=str,
    default="us-west-2",
    help="The AWS region to use in calls made to AWS.",
)
@click.option(
    "--aws-profile",
    required=False,
    type=str,
    default="default",
    help="The AWS profile to use in calls made to AWS.",
)
@click.pass_context
def cli(ctx, github_token_secret_arn: str, aws_region: str, aws_profile: str):
    ctx.ensure_object(dict)

    if aws_profile is not None:
        session = boto3.session.Session(
            profile_name=aws_profile, region_name=aws_region
        )
    else:
        session = boto3.session.Session(region_name=aws_region)

    ctx.obj["github_token"] = _get_github_token(github_token_secret_arn, session)


@cli.command()
@click.pass_context
def get_release_status(ctx):
    github_token = ctx.obj["github_token"]

    with click.progressbar(_WEEKLY_RELEASES, label="Getting Latest Releases") as bar:
        release_statuses = [
            repo.get_latest_release(github_token).get_release_status(github_token)
            for repo in bar
        ]

    print("This week's release status:")
    for status in release_statuses:
        print(f"  {status.get_report()}")


@cli.command()
@click.pass_context
@click.option(
    "--repository-name",
    type=str,
    required=True,
    help="The name of the repository to be released",
)
@click.option(
    "--repository-owner",
    type=str,
    required=True,
    help="The owner of the repository to be released",
)
def initiate_release(ctx, repository_name: str, repository_owner: str):
    github_token = ctx.obj["github_token"]


if __name__ == "__main__":
    cli()
