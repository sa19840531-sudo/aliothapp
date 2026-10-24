# Web Guide Page Branch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Discover App guide links from trusted Web captures, observe a landing page in an independent Playwright session, and save optional `guide_page` evidence without changing main exploration outcomes.

**Architecture:** Source-page classification registers bounded guide candidates. An optional branch runs with its own browser lifecycle and safe read-only network policy; branch failure is recorded and skipped. A confirmed landing has a supplemental Capture linked to its source and appears in a separate report section.

**Tech Stack:** FastAPI Python, Playwright, PostgreSQL, pytest.

**Spec:** Conversation design and user constraints through 2026-09-29.

## Global Constraints

- Change Web exploration only; leave App and H5 behavior and files untouched.
- Preserve ready first-screen evidence, ten-menu default, main page classification and task status.
- Cross-origin guide links are candidates; direct APK/store resources are not `guide_page`.
- Branch timeouts or failures are recorded and skipped; no login credentials or main cookies enter its context.
- Keep existing frozen v13 Web catalog snapshots valid when publishing v14.
- Comments and docs use UTF-8 Chinese. No new checkpoint stages.

## Review Focus

- A homepage footer App link must not relabel the homepage as `guide_page`.
- A resource URL or private-network redirect must not create a guide Capture.
- A branch crash or timeout must not alter main coverage or task outcome.
- Captured screenshot, DOM, final URL and page identity must refer to one landing page.
- Old frozen Web policy snapshots must still parse exactly as before.

---

### Task 1: Guide type and candidates

**Files:** Web catalog Python/JSON, new candidate module, Web catalog and candidate tests.

**Interfaces:** `select_web_guide_candidates(source_capture_id, source_url, snapshot) -> tuple[WebGuideCandidate, ...]`.

- [x] Write failing tests for v14 catalog, v13 restoration, positive App entry, footer false positive, APK resource and URL safety.
- [x] Run targeted tests and observe failures.
- [x] Implement the Web-only catalog type and bounded candidate selector.
- [x] Run targeted tests and observe passes.

### Task 2: Isolated observation and evidence

**Files:** New Web guide browser/assessment/capture modules, Web-only persistence adapter/repository, tests.

**Interfaces:** `WebGuideBranch.attempt(candidate, source) -> WebGuideOutcome`.

- [x] Write failing tests for independent context, blocked unsafe destination, identity-consistent artifacts, uncertain assessment and failure isolation.
- [x] Run targeted tests and observe failures.
- [x] Implement read-only landing observation, classification, supplemental Capture and branch outcome persistence.
- [x] Run targeted tests and observe passes.

### Task 3: Main-flow integration and report

**Files:** Web runtime composition/stage discovery/effects/runner, Web report snapshot/explanation, Web tests and specialist rules doc.

**Interfaces:** Register at trusted source capture; run asynchronously; drain/skip without changing main status.

- [x] Write failing integration tests for schedule, main continuation, failure skip and separate report projection.
- [x] Run targeted tests and observe failures.
- [x] Wire optional branch and report evidence; update Web regression rules.
- [x] Run targeted Web suites and observe passes.

### Task 4: Seven-site validation and review

**Files:** Web test report under `docs` if live evidence is available.

- [x] Exercise seven sites with the bounded Web exploration flow and inspect source, branch and main outcomes.
- [x] Run Web regression checks, inspect the full diff and request independent code review.
- [x] Fix important findings, rerun relevant tests, and report all limitations explicitly.
