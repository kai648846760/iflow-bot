You are gathering clarifying questions for a PRD. Ask 3-5 essential questions only.

Rules:
- Follow the session language policy exactly.
- Each question must include 3-5 lettered options (A, B, C...).
- Options must be concrete and mutually exclusive when possible.
- If you must include an "Other" option, make it the last option.
- Do not ask about tools or implementation choices unless required.

Return JSON only in this exact shape:
{
  "questions": [
    {
      "id": 1,
      "question": "...",
      "options": {
        "A": "...",
        "B": "...",
        "C": "..."
      }
    }
  ]
}

User prompt:
{user_prompt}
