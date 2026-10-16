# MayaCardio Assessment: AI Tools Disclosure

**Candidate Assessment Submission — October 2026**  
**Role:** Software Systems Engineer (Device Software · Cloud Services · Clinical Applications)

---

## 1. Acknowledgement of AI Tool Usage

In accordance with the MayaCardio assessment guidelines ("AI tools are welcome with disclosure"), I explicitly disclose the use of AI assistance during the development and testing of this submission.

### Tools Utilized
- **AI Coding Assistant (Antigravity / Gemini 3.8 Flash):** Used as an interactive pair-programming companion for initial scaffolding, generating unit and fault test fixtures, and assisting with syntax verification.

---

## 2. Scope of AI Assistance

1. **Test Fixture & Mock Protocol Scaffolding:**
   - Assisted in generating the structural template for `src/mock/biosignal_mock.py` and `src/common/protocol.py` based on the TCP wire protocol and formula specifications in Section 6.
2. **Formula Translation:**
   - Aided in drafting initial implementations of the 4 peak conditions and 5-second heart rate window algorithm in `src/processing/engine.py`.
3. **Documentation Templating:**
   - Assisted in structuring markdown templates for the architecture design note and failure testing logs.

---

## 3. Human Engineering Validation & Ownership

All architecture decisions, system guarantees, performance optimizations, and debugging were strictly reviewed, verified, and directed by the candidate:

1. **Protocol Verification:**
   - Verified that CRC-32 extraction is computed over the exact transmitted `[` to `]` raw byte slice rather than reserialized JSON strings (preventing checksum mismatches caused by serializer whitespace formatting).
2. **Database Optimization:**
   - Identified that row-by-row updates during catch-up sync caused bottlenecks (~4,900 samples/sec). Re-engineered `mark_samples_synced` to use grouped range-based updates on indexed columns, increasing catch-up throughput to **19,873 samples/second** (>3x requirement).
3. **Crash & Power-Cut Failure Analysis:**
   - Rigorously bounded the power-cut vulnerability window to at most 1.0 second, establishing proof through atomic SQLite transaction boundaries and replay recovery from the mock's 10-second circular buffer.
4. **Independent Test Execution:**
   - Personally executed and validated all 25 automated test cases across unit, integration, and failure injection suites.
   - I am fully prepared to explain, defend, and debug every line of code, data structure, SQL query, and design tradeoff during the technical interview.
