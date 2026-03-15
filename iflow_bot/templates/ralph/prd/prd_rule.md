# Ralph PRD Converter

Input PRD (Markdown):
{prd_md}

Convert the PRD into prd.json (JSON only, no markdown).
Use the Ralph schema below and follow the rules strictly.

Rules:
- Each user story must be completable in ONE iteration. Split if needed.
- Order dependencies logically (schema -> backend -> UI -> polish).
- Acceptance criteria must be concrete and verifiable.
- Always include "Typecheck passes" as the final acceptance criterion in every story.
- If a story has testable logic, also include "Tests pass".
- If UI changes are required, include "Verify in browser using dev-browser skill".
- IDs must be sequential (US-001, US-002...).
- All stories start with "passes": false and empty "notes".
- Each story must include "role" (one of: researcher, qa, engineer, writer, devops, debugger). Infer from the story if not explicit.
- branchName should be "ralph/<feature-name-kebab-case>".

JSON schema (example shape):
{
  "project": "Project Name",
  "branchName": "ralph/feature-name",
  "description": "Feature description",
  "userStories": [
    {
      "id": "US-001",
      "title": "Short title",
      "description": "As a [user], I want [feature] so that [benefit]",
      "acceptanceCriteria": ["Criterion 1", "Typecheck passes"],
      "role": "engineer",
      "priority": 1,
      "passes": false,
      "notes": ""
    }
  ]
}

Return JSON only.
