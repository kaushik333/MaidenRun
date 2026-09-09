import stat
from pathlib import Path

import pytest

from app.aws.credentials import NoCredentialsError, get_session, load_credentials, save_credentials
from app.core.config import settings

FAKE_ACCESS_KEY = "AKIAFAKEACCESSKEY00"
FAKE_SECRET_KEY = "SuperSecretValueThatMustNeverBeLogged"

# Credentials-path isolation (so this never touches the real
# ~/.agent-feedback-deploy/credentials.json) comes from the autouse fixture
# in conftest.py.


def test_save_credentials_writes_owner_only_file():
    save_credentials(FAKE_ACCESS_KEY, FAKE_SECRET_KEY)

    path = Path(settings.aws_credentials_path)
    assert path.is_file()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == (stat.S_IRUSR | stat.S_IWUSR)


def test_load_credentials_returns_none_when_uncached():
    assert load_credentials() is None


def test_load_credentials_returns_what_was_saved():
    save_credentials(FAKE_ACCESS_KEY, FAKE_SECRET_KEY)
    assert load_credentials() == (FAKE_ACCESS_KEY, FAKE_SECRET_KEY)


def test_get_session_without_args_reuses_cached_credentials():
    get_session(FAKE_ACCESS_KEY, FAKE_SECRET_KEY)  # first call: caches
    session = get_session()  # second call: no args, reuses the cache

    creds = session.get_credentials()
    assert creds.access_key == FAKE_ACCESS_KEY
    assert creds.secret_key == FAKE_SECRET_KEY


def test_get_session_raises_when_nothing_cached_and_nothing_provided():
    with pytest.raises(NoCredentialsError):
        get_session()


def test_get_session_uses_provided_credentials_directly():
    session = get_session(FAKE_ACCESS_KEY, FAKE_SECRET_KEY)

    creds = session.get_credentials()
    assert creds.access_key == FAKE_ACCESS_KEY
    assert creds.secret_key == FAKE_SECRET_KEY


def test_get_session_uses_the_requested_region():
    session = get_session(FAKE_ACCESS_KEY, FAKE_SECRET_KEY, region="eu-west-1")
    assert session.region_name == "eu-west-1"


def test_get_session_falls_back_to_default_region_when_none_given():
    session = get_session(FAKE_ACCESS_KEY, FAKE_SECRET_KEY)
    assert session.region_name == settings.aws_default_region


def test_get_session_region_is_not_cached_with_credentials():
    # Region is a per-deploy choice (which region to launch *into* this
    # time), not a property of the credentials — a later call with a
    # different region must not be stuck on whatever the first call used.
    get_session(FAKE_ACCESS_KEY, FAKE_SECRET_KEY, region="us-east-1")
    session = get_session(region="ap-southeast-1")  # reuses cached creds, new region
    assert session.region_name == "ap-southeast-1"
