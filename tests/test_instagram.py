import pytest
from instaloader import exceptions as ix

from sitk.sources.base import AuthError, RateLimited, SourceError
from sitk.sources.instagram import translate_errors

FEEDBACK = ('400 Bad Request - "fail" status, message "feedback_required" when accessing '
            "https://www.instagram.com/api/v1/users/web_profile_info/?username=x")


@pytest.mark.parametrize("exc, expected", [
    (ix.AbortDownloadException(FEEDBACK), AuthError),
    (ix.ConnectionException(FEEDBACK), AuthError),
    (ix.AbortDownloadException("Redirected to login page. You've been logged out"), AuthError),
    (ix.AbortDownloadException("429 Too Many Requests"), RateLimited),
    (ix.TooManyRequestsException("429"), RateLimited),
    (ix.ConnectionException("Please wait a few minutes before you try again."), RateLimited),
    (ix.LoginRequiredException("login required"), AuthError),
    (ix.ProfileNotExistsException("nope"), SourceError),
    (ix.ConnectionException("connection reset"), SourceError),
])
def test_error_translation(exc, expected):
    with pytest.raises(expected):
        with translate_errors():
            raise exc
