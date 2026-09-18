"""The version this build reports, and where the project publishes its releases.

Bumped by hand in the PR that cuts a release, then tagged to match. Not derived from git:
the Docker image carries no .git, and the number has to be one a person can go and look up.

Spotea is self-hosted, so this is only half the story — an instance knows what it is
running but not what has been released since. app/services/update_check.py asks upstream.
"""

APP_VERSION = "1.0.0"

GITHUB_REPO = "sertacartun/spotea"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
