import os
import sys
import time
from datetime import datetime
from google import genai
from google.genai import types


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
    fallback_model = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")
    max_retries = int(os.environ.get("GEMINI_MAX_RETRIES", "4"))
    base_delay = int(os.environ.get("GEMINI_RETRY_DELAY", "20"))
    diff_path = os.environ.get("DIFF_FILE", "pr_diff.txt")

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

    current_date = datetime.now().strftime("%Y-%m-%d")
    system_instruction = f"""
    You are an expert, strict, and highly capable Code Reviewer.
    Your job is to review code changes (diffs) and provide feedback.
    The current date is {current_date}.

    CRITICAL RULES YOU MUST ENFORCE:
    1. Code Quality: Code must align with architectural standards and be clean.
    2. Testing: Unit tests must be added or updated to cover new logic.
    3. Security: Absolutely NO hardcoded secrets, API keys, or passwords.
    4. Completeness: Ensure the logic completely solves the implied feature without missing pieces.

    INSTRUCTIONS:
    - Analyze the provided DIFF carefully.
    - If there are violations of the rules above, point them out clearly.
    - End your review with exactly one of these tokens on a new line: [APPROVED] or [CHANGES_REQUESTED]
    """

    prompt = f"""
    Please review the following git diff:

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
        model_retries = max(1, max_retries // len(candidate_models)) if len(candidate_models) > 1 else max_retries

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
                    print(f"Exhausted retries on {current_model} ({e}).")

        if success or review_text:
            break
        elif model_idx < len(candidate_models) - 1:
            print(f"Switching to fallback model: {candidate_models[model_idx + 1]}...")

    if not success and not review_text:
        review_text = (
            f"> [!WARNING]\n"
            f"> Automated AI review temporarily unavailable after testing "
            f"models ({', '.join(candidate_models)}). Please proceed with manual review."
        )

    # Save output files
    with open("review_output.md", "w") as f:
        f.write(f"### AI Code Review ({used_model} via Google AI Studio)\n\n")
        f.write(review_text)

    review_action = "comment"
    if "[APPROVED]" in review_text:
        review_action = "approve"
    elif "[CHANGES_REQUESTED]" in review_text:
        review_action = "request-changes"

    with open("review_action.txt", "w") as f:
        f.write(review_action)


if __name__ == "__main__":
    main()
