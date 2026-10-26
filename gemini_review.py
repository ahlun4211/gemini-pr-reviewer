from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from google import genai
from google.genai import types

MISSING_ISSUE_MARKER = "<!-- gemini-pr-reviewer:missing-issue -->"


def is_transient_busy_error(err_str: str) -> bool:
    """Checks if the exception indicates transient capacity / high demand / rate limits."""
    lowered = err_str.lower()
    return any(
        indicator in lowered
        for indicator in [
            "503",
            "unavailable",
            "high demand",
            "429",
            "resource_exhausted",
            "overloaded",
            "spikes in demand",
            "temporarily unavailable",
            "quota exceeded",
            "rate limit",
        ]
    )


def run_gh_command(args: list[str]) -> str | None:
    """Runs a GitHub CLI command and returns stdout if successful, or None on error."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout.strip()
    except Exception as e:
        print(f"Warning: gh command {' '.join(args)} failed: {e}")
        return None


def fetch_pr_context(repo: str, pr_number: str) -> dict:
    """Fetches PR metadata (title, body, linked issues, and prior reviews)."""
    context = {
        "title": "",
        "body": "",
        "headRefName": "",
        "closingIssuesReferences": [],
        "reviews": [],
    }

    # 1. Read webhook event payload if available as baseline fallback
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        try:
            with open(event_path, "r") as f:
                event_data = json.load(f)
            pr_payload = event_data.get("pull_request", {})
            context["title"] = pr_payload.get("title") or ""
            context["body"] = pr_payload.get("body") or ""
            context["headRefName"] = (pr_payload.get("head") or {}).get("ref") or ""
        except Exception as e:
            print(f"Warning: Failed to parse GITHUB_EVENT_PATH: {e}")

    if not repo or not pr_number:
        return context

    # 2. Query gh pr view (with closingIssuesReferences first, then fallback without it)
    out = run_gh_command(
        [
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "title,body,headRefName,closingIssuesReferences,reviews",
        ]
    )
    if out is None:
        out = run_gh_command(
            [
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "title,body,headRefName,reviews",
            ]
        )

    if out:
        try:
            data = json.loads(out)
            context["title"] = data.get("title") or context["title"]
            context["body"] = data.get("body") or context["body"]
            context["headRefName"] = data.get("headRefName") or context["headRefName"]
            if "closingIssuesReferences" in data and data["closingIssuesReferences"]:
                context["closingIssuesReferences"] = data["closingIssuesReferences"]
            if "reviews" in data and data["reviews"]:
                context["reviews"] = data["reviews"]
        except Exception as e:
            print(f"Warning: Failed to parse gh pr view output: {e}")

    return context


def extract_linked_issue_numbers(pr_context: dict) -> list[int]:
    """Extracts candidate linked issue numbers from closingIssuesReferences and PR title/body."""
    issue_numbers: list[int] = []

    def add_num(n: int):
        if n > 0 and n not in issue_numbers:
            issue_numbers.append(n)

    # 1. Native GitHub closing issue references
    for ref in pr_context.get("closingIssuesReferences") or []:
        if isinstance(ref, dict) and isinstance(ref.get("number"), int):
            add_num(ref["number"])

    # 2. Parse PR title and body for issue references (#123 or .../issues/123)
    text_to_scan = f"{pr_context.get('title', '')}\n{pr_context.get('body', '')}"

    for match in re.finditer(r"github\.com/[^/\s]+/[^/\s]+/issues/(\d+)", text_to_scan):
        add_num(int(match.group(1)))

    for match in re.finditer(r"(?:^|[\s(\[/:])#(\d+)\b", text_to_scan):
        add_num(int(match.group(1)))

    return issue_numbers


def fetch_issue_details(repo: str, issue_numbers: list[int]) -> list[dict]:
    """Fetches the original issue specification (title, body, labels, comments) for each linked issue."""
    if not repo or not issue_numbers:
        return []

    issues: list[dict] = []
    for num in issue_numbers:
        # Try gh issue view (GraphQL - automatically excludes PRs)
        out = run_gh_command(
            [
                "issue",
                "view",
                str(num),
                "--repo",
                repo,
                "--json",
                "number,title,body,state,labels,comments",
            ]
        )
        if out:
            try:
                issue_data = json.loads(out)
                issues.append(issue_data)
                continue
            except Exception as e:
                print(f"Warning: Failed to parse gh issue view for #{num}: {e}")

        # Fallback to REST API if GraphQL failed
        rest_out = run_gh_command(["api", f"repos/{repo}/issues/{num}"])
        if rest_out:
            try:
                rest_data = json.loads(rest_out)
                # Ensure it's an actual issue and not a pull request
                if "pull_request" not in rest_data:
                    comments_list = []
                    comments_out = run_gh_command(["api", f"repos/{repo}/issues/{num}/comments"])
                    if comments_out:
                        try:
                            raw_comments = json.loads(comments_out)
                            for c in raw_comments:
                                comments_list.append(
                                    {
                                        "author": {"login": (c.get("user") or {}).get("login", "unknown")},
                                        "body": c.get("body", ""),
                                    }
                                )
                        except Exception:
                            pass
                    issues.append(
                        {
                            "number": rest_data.get("number", num),
                            "title": rest_data.get("title", ""),
                            "body": rest_data.get("body", ""),
                            "state": rest_data.get("state", ""),
                            "labels": [{"name": lbl.get("name", "")} for lbl in (rest_data.get("labels") or [])],
                            "comments": comments_list,
                        }
                    )
            except Exception as e:
                print(f"Warning: Failed to parse REST issue #{num}: {e}")

    return issues


def has_bypass_keyword(pr_context: dict, bypass_keyword: str) -> bool:
    """Checks if the bypass keyword is present in the PR description or title."""
    if not bypass_keyword:
        return False
    combined = f"{pr_context.get('title', '')}\n{pr_context.get('body', '')}"
    return bypass_keyword.lower() in combined.lower()


def count_prior_missing_issue_reviews(reviews: list[dict]) -> int:
    """Counts how many prior reviews on this PR flagged a missing linked issue."""
    count = 0
    for rev in reviews or []:
        body = (rev.get("body") or "") if isinstance(rev, dict) else ""
        if MISSING_ISSUE_MARKER in body or "No Linked GitHub Issue Found" in body:
            count += 1
    return count


def build_missing_issue_banner(unlinked_review_count: int, bypass_keyword: str) -> str:
    """Builds the strict-mode warning banner when no issue is linked to the PR."""
    lines = [
        MISSING_ISSUE_MARKER,
        "> [!WARNING]",
        "> ### ❌ Strict Issue Verification: No Linked GitHub Issue Found",
        "> This pull request does not reference an original GitHub Issue. To ensure all requested features and acceptance criteria from the original issue are properly verified (and not just the PR description), please link the issue in your PR description (e.g., `Closes #<issue-number>` or `Fixes #<issue-number>`).",
    ]
    if unlinked_review_count >= 2:
        lines.extend(
            [
                ">",
                f"> 💡 **Bypass Option (Review #{unlinked_review_count} without a linked issue):** "
                f"If this PR intentionally does not have a corresponding issue (e.g., a minor chore, docs update, or hotfix), "
                f"you can bypass this requirement by adding `{bypass_keyword}` anywhere in the PR description.",
            ]
        )
    return "\n".join(lines) + "\n\n---\n\n"


def format_issues_for_prompt(issues: list[dict]) -> str:
    """Formats fetched GitHub Issues into a structured markdown block for the LLM prompt."""
    blocks = []
    for issue in issues:
        num = issue.get("number")
        title = issue.get("title") or "(Untitled)"
        state = issue.get("state") or "OPEN"
        labels = [
            lbl.get("name", "")
            for lbl in (issue.get("labels") or [])
            if isinstance(lbl, dict) and lbl.get("name")
        ]
        label_str = f" | Labels: {', '.join(labels)}" if labels else ""
        body = (issue.get("body") or "").strip() or "(No issue description provided.)"

        comment_lines = []
        for idx, c in enumerate(issue.get("comments") or [], start=1):
            if isinstance(c, dict) and (c.get("body") or "").strip():
                author = (c.get("author") or {}).get("login", "user")
                comment_lines.append(f"  - Comment #{idx} (@{author}): {c['body'].strip()}")

        comments_section = (
            "\n\n### Clarifying Issue Comments:\n" + "\n".join(comment_lines)
            if comment_lines
            else ""
        )

        blocks.append(
            f"## Original Issue #{num}: {title} (State: {state}{label_str})\n"
            f"### Original Issue Description & Acceptance Criteria:\n"
            f"{body}"
            f"{comments_section}"
        )
    return "\n\n---\n\n".join(blocks)


def attempt_review(client: genai.Client, model: str, prompt: str, system_instruction: str) -> str:
    """Generates code review for a given model."""
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.1,
        safety_settings=[
            types.SafetySetting(
                category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"
            ),
        ],
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=config,
    )
    return response.text or "No review generated."


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Missing GEMINI_API_KEY environment variable.")
        sys.exit(1)

    primary_model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    fallback_model = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash-lite")
    max_retries = int(os.environ.get("GEMINI_MAX_RETRIES", "3"))
    base_delay = int(os.environ.get("GEMINI_RETRY_DELAY", "20"))
    diff_path = os.environ.get("DIFF_FILE", "pr_diff.txt")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    bypass_keyword = os.environ.get("BYPASS_KEYWORD", "BYPASS-LINK-ISSUE")

    if not os.path.exists(diff_path):
        print(f"Diff file not found at {diff_path}.")
        with open("review_output.md", "w") as f:
            f.write(f"Diff file not found at `{diff_path}`.")
        with open("review_action.txt", "w") as f:
            f.write("comment")
        return

    with open(diff_path, "r") as f:
        diff_content = f.read()

    if not diff_content.strip():
        print("No diff found. Exiting.")
        with open("review_output.md", "w") as f:
            f.write("No code changes detected in this PR.")
        with open("review_action.txt", "w") as f:
            f.write("comment")
        return

    # Fetch PR metadata & linked issues
    pr_context = fetch_pr_context(repo, pr_number)
    bypass_active = has_bypass_keyword(pr_context, bypass_keyword)
    candidate_issue_nums = extract_linked_issue_numbers(pr_context)
    linked_issues = fetch_issue_details(repo, candidate_issue_nums)

    missing_issue_strict = (len(linked_issues) == 0) and (not bypass_active)
    prior_missing_count = count_prior_missing_issue_reviews(pr_context.get("reviews", []))
    unlinked_review_count = prior_missing_count + 1 if missing_issue_strict else 0

    if linked_issues:
        print(f"Found linked original issue(s): {[i.get('number') for i in linked_issues]}")
    elif bypass_active:
        print(f"No linked issue found, but '{bypass_keyword}' is present in PR description. Bypassing strict issue check.")
    else:
        print(
            f"Strict Mode: No linked issue found (unlinked review #{unlinked_review_count}). "
            f"Will request changes until issue is linked or bypassed."
        )

    current_date = datetime.now().strftime("%Y-%m-%d")

    if linked_issues:
        issue_context_block = format_issues_for_prompt(linked_issues)
        issue_rule = (
            "1. Original Issue Alignment & Feature Completeness (HIGHEST PRIORITY):\n"
            "   - Treat the ORIGINAL GITHUB ISSUE(S) provided below as the single source of truth — NOT the PR description.\n"
            "   - Development agents or developers may misinterpret the original issue and write a PR description that matches their flawed implementation. You must verify the code diff directly against the original issue requirements and comments.\n"
            "   - Include a dedicated '#### 📋 Issue Requirements Checklist' section in your review that lists every feature, requirement, and acceptance criterion from the original issue(s) and marks each as `✅ Implemented`, `⚠️ Partial`, or `❌ Missing / Misinterpreted`.\n"
            "   - If ANY requirement from the original issue is missing, only partially implemented, or misinterpreted, you MUST output `[CHANGES_REQUESTED]`."
        )
    elif bypass_active:
        issue_context_block = (
            f"No GitHub Issue is linked (developer explicitly bypassed strict issue linking via `{bypass_keyword}`)."
        )
        issue_rule = (
            "1. Completeness: Ensure the code changes completely and accurately solve the goal described in the PR title and description."
        )
    else:
        issue_context_block = (
            "ERROR: No original GitHub Issue is linked to this PR, and no bypass keyword was provided."
        )
        issue_rule = (
            "1. Missing Linked Issue (STRICT VIOLATION):\n"
            "   - This repository requires every PR to link its original GitHub Issue so features can be verified against the original specification.\n"
            "   - Still perform a full, thorough review of the code diff below for quality, testing, and security so the developer receives complete feedback, wrapped up with `[CHANGES_REQUESTED]`."
        )

    system_instruction = f"""
    You are an expert, strict, and highly capable Code Reviewer.
    Your job is to review pull request code changes (diffs) against the original issue specification and provide actionable feedback.
    The current date is {current_date}.

    CRITICAL RULES YOU MUST ENFORCE:
    {issue_rule}
    2. Code Quality: Code must align with architectural standards, handle edge cases, and be clean and maintainable.
    3. Testing: Unit tests must be added or updated to cover new features, bug fixes, and edge cases.
    4. Security: Absolutely NO hardcoded secrets, API keys, passwords, or security vulnerabilities.

    INSTRUCTIONS:
    - First, evaluate alignment with the Original Issue (if linked) before trusting the PR description.
    - Analyze the provided DIFF carefully against all critical rules above.
    - Point out any missing features, misinterpreted requirements, bugs, or rule violations clearly.
    - End your review with exactly one of these tokens on a new line: [APPROVED] or [CHANGES_REQUESTED]
    """

    pr_title = pr_context.get("title") or "(No PR title)"
    pr_body = (pr_context.get("body") or "").strip() or "(No PR description provided)"

    prompt = f"""
    # Original GitHub Issue Specification (Source of Truth)
    {issue_context_block}

    # Pull Request Metadata (Developer / Agent Claim)
    **PR Title:** {pr_title}
    **PR Description:**
    {pr_body}

    # Git Diff (Actual Implementation)
    Please review the following git diff against the Original Issue and critical rules:

    ```diff
    {diff_content}
    ```
    """

    client = genai.Client(api_key=api_key)

    # Candidate models: try primary, then fallback
    candidate_models = [primary_model]
    if fallback_model and fallback_model != primary_model:
        candidate_models.append(fallback_model)

    review_text = ""
    used_model = primary_model
    success = False

    for model_idx, current_model in enumerate(candidate_models):
        used_model = current_model
        print(f"Attempting review with model: {current_model}")

        # Distribute retries between models: primary gets most retries, fallback gets remaining
        model_retries = max(2, max_retries // len(candidate_models)) if len(candidate_models) > 1 else max_retries

        for attempt in range(1, model_retries + 1):
            print(f"[{attempt}/{model_retries}] Sending diff to {current_model} via Google AI Studio...")
            try:
                review_text = attempt_review(client, current_model, prompt, system_instruction)
                success = True
                break
            except Exception as e:
                err_str = str(e)
                if not is_transient_busy_error(err_str):
                    print(f"Non-transient error on {current_model}: {e}")
                    review_text = (
                        f"> [!WARNING]\n"
                        f"> Automated AI review encountered an error ({e}). "
                        f"Please proceed with manual review."
                    )
                    break

                # If transient error and still have retries on this model
                if attempt < model_retries:
                    delay = min(base_delay * attempt, 90)
                    print(
                        f"{current_model} is currently busy / experiencing high demand. "
                        f"Waiting {delay}s before retry ({attempt}/{model_retries})..."
                    )
                    time.sleep(delay)
                else:
                    print(f"Exhausted retries on {current_model}.")

        if success or (review_text and not is_transient_busy_error(err_str)):
            break
        elif model_idx < len(candidate_models) - 1:
            print(f"Switching to fallback model: {candidate_models[model_idx + 1]}...")

    if not success and not review_text:
        review_text = (
            f"> [!WARNING]\n"
            f"> Automated AI review temporarily unavailable after testing "
            f"models ({', '.join(candidate_models)}). Please proceed with manual review."
        )

    # Enforce strict missing-issue policy deterministically if no issue is linked and not bypassed
    if missing_issue_strict:
        banner = build_missing_issue_banner(unlinked_review_count, bypass_keyword)
        if "[APPROVED]" in review_text:
            review_text = review_text.replace("[APPROVED]", "[CHANGES_REQUESTED]")
        elif "[CHANGES_REQUESTED]" not in review_text:
            review_text = review_text.rstrip() + "\n\n[CHANGES_REQUESTED]\n"
        review_text = banner + review_text

    # Save output files
    with open("review_output.md", "w") as f:
        f.write(f"### AI Code Review ({used_model} via Google AI Studio)\n\n")
        f.write(review_text)

    review_action = "comment"
    if missing_issue_strict:
        review_action = "request-changes"
    elif "[APPROVED]" in review_text:
        review_action = "approve"
    elif "[CHANGES_REQUESTED]" in review_text:
        review_action = "request-changes"

    with open("review_action.txt", "w") as f:
        f.write(review_action)


if __name__ == "__main__":
    main()
