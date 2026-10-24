# 🤖 Gemini PR Reviewer

A robust, enterprise-grade GitHub Action for automated AI code reviews on Pull Requests powered by **Google Gemini via Google AI Studio**.

Includes automatic **exponential backoff retry** for busy spikes (`503 UNAVAILABLE` / `429 RESOURCE_EXHAUSTED`) and **multi-model fallback** (e.g. `gemini-3.8-flash` $\to$ `gemini-3.8-flash-lite`).

---

## 🚀 Quick Start

Add `.github/workflows/gemini_review.yml` to your repository:

```yaml
name: Gemini PR Reviewer

on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  review:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write

    steps:
      - name: Checkout Repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Gemini AI Review
        uses: ahlun4211/gemini-pr-reviewer@main
        with:
          gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
          primary-model: 'gemini-3.8-flash'
          fallback-model: 'gemini-3.8-flash-lite'
          max-retries: '3'
```

---

## ⚙️ Inputs Reference

| Input | Description | Default | Required |
|---|---|---|---|
| `gemini-api-key` | Google AI Studio Gemini API Key | N/A | **Yes** |
| `github-token` | GitHub Token for posting review comments | `${{ github.token }}` | No |
| `primary-model` | Primary model to query | `gemini-3.8-flash` | No |
| `fallback-model` | Model to fall back to when primary is congested | `gemini-3.8-flash-lite` | No |
| `max-retries` | Max retry attempts per model before failover | `3` | No |
| `retry-delay` | Base backoff delay in seconds | `20` | No |
| `python-version` | Python runtime version | `3.11` | No |

---

## 🛡️ Key Features

1. **Multi-Model Fallback**:
   When high demand causes `503 UNAVAILABLE` or `429` rate limits on the primary model, the action automatically fails over to the fallback model.
2. **Exponential Backoff**:
   Transients spikes are handled smoothly with increasing backoff delays instead of failing the build immediately.
3. **Smart Review Actions**:
   Inspects the model's recommendation and issues native GitHub review outcomes:
   - `[APPROVED]` $\to$ `--approve`
   - `[CHANGES_REQUESTED]` $\to$ `--request-changes`
   - Warnings/Errors $\to$ neutral `--comment`
4. **Zero Repository Boilerplate**:
   No python dependencies or diff parsing scripts needed in individual repos.

---

## 🔑 Setup API Key

1. Generate a free API key at [Google AI Studio](https://aistudio.google.com/).
2. Add it to your repository secrets:
   - Go to **Settings** > **Secrets and variables** > **Actions**.
   - Create secret `GEMINI_API_KEY`.
