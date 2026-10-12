import stat
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import UnauthorizedSSOTokenError
from fastapi.testclient import TestClient
from moto import mock_aws

from app.aws.credentials import NoCredentialsError, credential_source, get_session, load_credentials, save_credentials
from app.core.config import settings
from app.main import app

FAKE_ACCESS_KEY = "AKIAFAKEACCESSKEY00"
FAKE_SECRET_KEY = "SuperSecretValueThatMustNeverBeLogged"

# Credentials-path isolation (so this never touches the real
# ~/.maidenrun/credentials.json) comes from the autouse fixture
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


PROFILE_KEY = "AKIAPROFILEKEY000000"


def _write_profile(tmp_path: Path, monkeypatch, name: str = "maidenrun") -> None:
    """A real ~/.aws/credentials file with one profile, as `aws configure --profile` writes it."""
    credentials_file = tmp_path / "aws-credentials"
    credentials_file.write_text(
        f"[{name}]\naws_access_key_id = {PROFILE_KEY}\naws_secret_access_key = profile-secret\n"
    )
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials_file))
    monkeypatch.setattr(settings, "aws_profile", name)


def test_configured_profile_takes_priority_over_a_cached_key(tmp_path, monkeypatch):
    save_credentials(FAKE_ACCESS_KEY, FAKE_SECRET_KEY)
    _write_profile(tmp_path, monkeypatch)

    session = get_session()
    assert session.get_credentials().access_key == PROFILE_KEY
    assert credential_source(session) == "AWS profile 'maidenrun'"


def test_a_key_pasted_in_the_request_beats_the_profile(tmp_path, monkeypatch):
    _write_profile(tmp_path, monkeypatch)
    assert get_session(FAKE_ACCESS_KEY, FAKE_SECRET_KEY).get_credentials().access_key == FAKE_ACCESS_KEY


def test_cached_key_is_used_when_there_is_no_profile():
    save_credentials(FAKE_ACCESS_KEY, FAKE_SECRET_KEY)
    session = get_session()
    assert session.get_credentials().access_key == FAKE_ACCESS_KEY
    assert credential_source(session) == "saved access key"


def test_environment_variables_are_part_of_the_chain(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAENVKEY0000000000")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-secret")
    session = get_session()
    assert session.get_credentials().access_key == "AKIAENVKEY0000000000"
    assert credential_source(session) == "AWS environment variables"


def test_missing_named_profile_gives_a_clear_error(monkeypatch):
    monkeypatch.setattr(settings, "aws_profile", "does-not-exist")
    with pytest.raises(NoCredentialsError, match="'does-not-exist'.*wasn't found"):
        get_session()


def test_expired_sso_session_says_how_to_log_in_again(monkeypatch):
    def expired_sso(self):
        raise UnauthorizedSSOTokenError()

    monkeypatch.setattr(boto3.Session, "get_credentials", expired_sso)
    with pytest.raises(NoCredentialsError, match="aws sso login"):
        get_session()


@mock_aws
def test_aws_identity_endpoint_reports_the_profile_in_use(tmp_path, monkeypatch):
    _write_profile(tmp_path, monkeypatch)
    response = TestClient(app).get("/aws-identity")
    body = response.json()
    assert body["available"] is True
    assert body["source"] == "AWS profile 'maidenrun'"
    assert body["arn"].startswith("arn:aws:")


def test_aws_identity_endpoint_explains_when_nothing_is_configured():
    body = TestClient(app).get("/aws-identity").json()
    assert body["available"] is False
    assert "AWS_PROFILE" in body["error"]


def test_an_empty_aws_profile_env_var_is_treated_as_unset(monkeypatch):
    # docker-compose.yml passes AWS_PROFILE=${AWS_PROFILE:-} through, so it is
    # "" whenever the user never set one — that must not break pasted keys.
    monkeypatch.setenv("AWS_PROFILE", "")
    session = get_session(FAKE_ACCESS_KEY, FAKE_SECRET_KEY)
    assert session.get_credentials().access_key == FAKE_ACCESS_KEY

    monkeypatch.setenv("AWS_PROFILE", "")
    assert get_session().get_credentials().access_key == FAKE_ACCESS_KEY  # the cached key, via the chain
