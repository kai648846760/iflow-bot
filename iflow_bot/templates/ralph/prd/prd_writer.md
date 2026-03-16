You are drafting a PRD in Markdown based on the prompt and clarifications.

Input prompt:
{user_prompt}

Clarifying questions and answers:
{qa_block}

If `Authoritative selections` are present, they are binding user decisions.
Do not replace them with inferred alternatives.
The PRD must match those selections exactly.

Write a PRD in Markdown with these sections:
- Title
- Introduction
- Goals
- User Stories (bulleted; each story should be one iteration sized)
- Functional Requirements
- Non-Goals
- Design Considerations (optional)
- Technical Considerations (optional)
- Success Metrics
- Open Questions

For each user story, include:
- Description (As a [user], I want [feature] so that [benefit])
- Role (one of: researcher, qa, engineer, writer, devops, debugger)
- Acceptance Criteria (bulleted; must include "Typecheck passes", and if applicable "Tests pass" and "Verify in browser using dev-browser skill")

Keep it concise and concrete. Return Markdown only.
