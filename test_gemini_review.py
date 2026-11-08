import os
import unittest
from unittest.mock import MagicMock, patch

from google.genai import types
from gemini_review import (
    is_transient_error,
    is_transient_busy_error,
    has_bypass_keyword,
    extract_linked_issue_numbers,
    count_prior_missing_issue_reviews,
    build_missing_issue_banner,
)


class TestTransientErrorDetection(unittest.TestCase):
    def test_transient_socket_and_connection_disconnects(self):
        """Verify socket disconnects, connection resets, and dropped connections are recognized as transient."""
        socket_errors = [
            "Server disconnected without sending a response.",
            "Remote end closed connection without response",
            "Connection reset by peer",
            "Broken pipe (errno 32)",
            "Socket timeout after 120000ms",
            "Read timed out",
            "Connection refused by remote host",
            "Unexpected EOF occurred in violation of protocol",
            "deadline_exceeded while awaiting response",
            "ECONNRESET: socket hang up",
            "ECONNREFUSED",
        ]
        for err in socket_errors:
            with self.subTest(err=err):
                self.assertTrue(is_transient_error(err), f"Expected transient for: {err}")
                self.assertTrue(is_transient_busy_error(err), f"Expected backward-compatible alias to match: {err}")

    def test_transient_busy_and_quota_errors(self):
        """Verify 503, 429, and quota limit errors continue to be recognized as transient."""
        busy_errors = [
            "503 Service Unavailable",
            "The service is currently unavailable due to high demand.",
            "429 Resource has been exhausted (e.g. check quota).",
            "Rate limit reached for gemini-3.8-flash",
            "Spikes in demand caused temporary outage",
            "Model is overloaded. Please try again later.",
            "Quota exceeded for quota metric",
        ]
        for err in busy_errors:
            with self.subTest(err=err):
                self.assertTrue(is_transient_error(err), f"Expected transient for: {err}")

    def test_non_transient_errors(self):
        """Verify non-transient errors (like bad credentials or bad arguments) are not treated as transient."""
        non_transient = [
            "400 Invalid argument: prompt exceeds maximum tokens",
            "401 API_KEY_INVALID: User has provided an invalid API key",
            "403 PERMISSION_DENIED: Method not allowed",
            "404 Not Found: Model gemini-invalid does not exist",
            "SyntaxError: invalid syntax",
        ]
        for err in non_transient:
            with self.subTest(err=err):
                self.assertFalse(is_transient_error(err), f"Expected non-transient for: {err}")


class TestModelFallbackAndTimeout(unittest.TestCase):
    @patch.dict(os.environ, {
        "GEMINI_API_KEY": "test-key",
        "GEMINI_TIMEOUT_SECONDS": "90",
        "GEMINI_MODEL": "gemini-3.8-flash",
        "GEMINI_FALLBACK_MODEL": "gemini-2.5-flash",
        "GEMINI_MAX_RETRIES": "2",
        "GEMINI_RETRY_DELAY": "0",
    })
    @patch("gemini_review.genai.Client")
    @patch("gemini_review.attempt_review")
    @patch("gemini_review.fetch_pr_context")
    @patch("gemini_review.extract_linked_issue_numbers")
    @patch("gemini_review.fetch_issue_details")
    @patch("os.path.exists")
    def test_fallback_on_socket_disconnect(
        self,
        mock_exists,
        mock_fetch_issues,
        mock_extract_issues,
        mock_fetch_pr_context,
        mock_attempt_review,
        mock_genai_client,
    ):
        """
        Verify that when the primary model experiences a socket disconnect
        ('Server disconnected without sending a response.'), retries are attempted
        and execution falls back to the secondary model.
        """
        from gemini_review import main

        mock_exists.return_value = True
        mock_fetch_pr_context.return_value = {
            "title": "Fix bug (#1)",
            "body": "Fixes #1",
            "headRefName": "fix/1",
            "closingIssuesReferences": [{"number": 1}],
            "reviews": [],
        }
        mock_extract_issues.return_value = [1]
        mock_fetch_issues.return_value = ([{
            "number": 1,
            "title": "Sample Issue",
            "body": "Description",
            "state": "OPEN",
            "labels": [],
            "comments": [],
        }], False)

        # Primary model fails with socket disconnect, fallback model succeeds
        mock_attempt_review.side_effect = [
            Exception("Server disconnected without sending a response."),
            Exception("Server disconnected without sending a response."),
            "#### Review Output\n[APPROVED]",
        ]

        # Mock diff file content
        with patch("builtins.open", unittest.mock.mock_open(read_data="diff content")) as mock_file:
            main()

        # Verify Client initialized with timeout (90s = 90000ms)
        mock_genai_client.assert_called_once()
        _, kwargs = mock_genai_client.call_args
        self.assertEqual(kwargs["api_key"], "test-key")
        self.assertIsInstance(kwargs["http_options"], types.HttpOptions)
        self.assertEqual(kwargs["http_options"].timeout, 90000)

        # Verify attempt_review was called for both primary and fallback models
        called_models = [call.args[1] for call in mock_attempt_review.call_args_list]
        self.assertIn("gemini-3.8-flash", called_models)
        self.assertIn("gemini-2.5-flash", called_models)


if __name__ == "__main__":
    unittest.main()
