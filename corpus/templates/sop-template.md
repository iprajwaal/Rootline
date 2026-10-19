| Document title | <Procedure title> |
|---|---|
| Document number | SOP-AREA-NNN |
| Revision | NN |
| Effective date | YYYY-MM-DD |
| Owner | Team that owns this procedure |
| Services | Services this procedure applies to |
| Alerts | Alerts that should lead an engineer here |
| Supersedes | SOP-AREA-NNN, or None |

# Standard operating procedure

## <Procedure title>

## Table of contents

1. Purpose
2. Scope
3. When to use
4. Definitions and abbreviations
5. Roles and responsibilities
6. Related documents
7. Prerequisites
8. Procedure overview
9. Procedure
10. Verification
11. Rollback
12. Troubleshooting
13. Escalation
14. Security considerations
15. Compliance and standards
16. Review and update
17. Records

Appendix A. Revision history

## 1. Purpose

What this procedure fixes or does, in one or two sentences.

## 2. Scope

This procedure applies to:
- The services, environments and situations it covers.

This procedure does not apply to:
- Situations that need a different procedure. Name that procedure.

## 3. When to use

- Alert names that point here.
- Symptoms an engineer would see.
- Error text from logs, quoted exactly.

## 4. Definitions and abbreviations

**Term**: What it means in this procedure.

## 5. Roles and responsibilities

| Role | Responsibility |
|---|---|
| On-call engineer | Runs the procedure and records each step in the incident channel |
| Service owner | Approves changes that affect customers |

## 6. Related documents

- SOP or RCA IDs, with a short note on how they relate.

## 7. Prerequisites

- Access, tools or approvals needed before starting.

## 8. Procedure overview

A short summary of the stages: confirm the problem, apply the fix, verify, and roll back if needed.

## 9. Procedure

Run the steps in order. Do not skip or reorder steps. Stop and escalate if a result does not match the expected result.

| Step | Action | Command | Expected result | Responsibility | Automation |
|---|---|---|---|---|---|
| 1 | Read-only step that confirms the problem | `exact command` | What the output should show | On-call engineer | check:name |
| 2 | Step that changes state | `exact command` | What should change | On-call engineer | action:name |

## 10. Verification

- How to confirm the fix worked, with numbers where possible.

## 11. Rollback

| Step | Action | Command | Expected result | Responsibility | Automation |
|---|---|---|---|---|---|
| 1 | How to undo the change | `exact command` | What should change back | On-call engineer | |

## 12. Troubleshooting

| Symptom | Action |
|---|---|
| A result that does not match the expected result | What to check or do next |

## 13. Escalation

- Who to page, and when.

## 14. Security considerations

- Secrets, access and data handling rules that apply to this procedure.

## 15. Compliance and standards

- Change management, audit or regulatory rules that apply.

## 16. Review and update

How often this procedure is reviewed, and what triggers an early review, such as an incident where it did not work.

## 17. Records

Where evidence of each run is kept, such as the incident ticket and channel.

## Appendix A. Revision history

| Revision | Date | Author | Change |
|---|---|---|---|
| 01 | YYYY-MM-DD | Name | First version |
