---
name: tech-spec
description: Parse and review Technical Specification documents for completeness, consistency, and implementation readiness
allowed-tools: [Read, Glob, Grep, "Bash(python3:*)"]
---

# Tech Spec — Technical Specification Reviewer

You are reviewing a Technical Specification document — a large structured markdown file that defines a software system's requirements, architecture, and implementation details. A tree-sitter-based structural analysis has been injected below.

## Arguments

`$ARGUMENTS` may contain any combination of:

| Flag | Effect |
|------|--------|
| `<file-path>` | Path to the tech spec markdown file (positional, required) |
| `--verbose` | Include full section dump and feature details in parser output |
| `--focus AREA` | Focus review on a specific area (see Focus Mode below) |

## Injected Data

```
!`python3 ~/.claude/skills/tech-spec/scripts/tech_spec_parser.py $ARGUMENTS`
```

## Error Handling

- **If tree-sitter-markdown is unavailable**: The parser automatically falls back to regex-based heading and table extraction. Note in the output that tree-sitter analysis was unavailable and recommend: `pip3 install tree-sitter tree-sitter-markdown`
- **If the file is not found**: The parser will report the error. Confirm the path with the user.
- **If the document is unusually structured**: Some tech specs may use different section numbering or organization. Adapt the analysis to the actual structure found.

## Your Task

Using the structural analysis above, perform a comprehensive review of the Technical Specification with these 8 analysis sections:

### 1. Structure Validation

- Verify all expected tech spec sections are present:
  - **Section 1**: Introduction (Executive Summary, System Overview, Scope, Technology Stack)
  - **Section 2**: Product Requirements (Feature Catalog with F-xxx entries)
  - **Section 3**: Technology Stack (detailed choices)
  - **Section 4**: Process Flowchart (workflow diagrams)
  - **Section 5**: System Architecture (high-level, components, data flow)
  - **Section 6**: System Components Design (detailed specifications)
  - **Section 7**: User Interface Design (UI/UX specs)
  - **Section 8**: Infrastructure (build, deployment, platform)
  - **Section 9**: Appendices (supporting materials)
- Check heading numbering is consistent and sequential
- Flag any missing standard sections
- Note the document's total size and complexity

### 2. Scope Analysis

- Verify In-Scope items (1.3.1) are concrete and measurable
- Verify Out-of-Scope items (1.3.2) are explicitly stated
- Check for scope creep indicators (vague boundaries, "may include", "potentially")
- Flag any features in the catalog (Section 2) that seem outside stated scope
- Assess scope-to-complexity ratio — is the scope realistic for the described system?

### 3. Requirements Completeness

- Verify all features (F-xxx) have required attributes:
  - Feature ID, Name, Category, Priority, Status
  - Overview, Business Value, User Benefits
  - Technical Context
  - Dependencies (Prerequisite Features, System Dependencies, External Dependencies, Integration Requirements)
- Check for orphan features (referenced but not defined)
- Flag features missing acceptance criteria or verification methods
- Identify duplicate or overlapping features

### 4. Architecture Consistency

- Cross-reference components in Section 5 (Architecture) against features in Section 2
- Verify all components have:
  - Clear responsibility statement
  - Defined interfaces
  - Dependency mapping
- Check data flow descriptions match the component inventory
- Flag architectural decisions that lack rationale
- Identify missing integration points between components

### 5. Technology Assessment

- Review technology choices in Sections 1.4 and 3 for:
  - Appropriateness for stated requirements
  - Version compatibility and currency
  - License implications
  - Known limitations or risks
- Flag any technology choices that contradict architectural decisions
- Check for missing technology decisions (database, caching, messaging, etc.)
- Assess technology stack coherence

### 6. Integration Analysis

- Identify all external integration points
- Verify each integration has:
  - Protocol/interface specification
  - Error handling strategy
  - Data exchange format
- Check for missing integrations implied by features
- Flag integrations without fallback strategies
- Assess API surface completeness

### 7. Infrastructure & Deployment

- If Section 8 indicates infrastructure is applicable:
  - Verify deployment architecture is specified
  - Check for missing infrastructure components (CI/CD, monitoring, logging)
  - Assess scalability considerations
  - Review security controls
- If Section 8 indicates infrastructure is N/A (e.g., CLI tools):
  - Verify the justification is sound
  - Check build system is adequately specified

### 8. Risk Assessment

Produce a risk table with the top 5-7 risks identified across all analysis areas:

| # | Risk | Category | Severity | Impact | Recommendation |
|---|------|----------|----------|--------|----------------|
| 1 | ... | Scope/Arch/Tech/Integration | Critical/High/Med/Low | ... | ... |

## Output Format

Structure your review as a markdown document with these sections:

```
## 1. Structure Validation
[findings as bullets]

## 2. Scope Analysis
[findings as bullets with specific section references]

## 3. Requirements Completeness
[findings as bullets]
[feature coverage summary table if applicable]

## 4. Architecture Consistency
[findings as bullets]

## 5. Technology Assessment
[findings as bullets]

## 6. Integration Analysis
[findings as bullets]

## 7. Infrastructure & Deployment
[findings as bullets]

## 8. Risk Assessment
[risk table]

## Verdict
[2-3 paragraph overall assessment:
- Is this tech spec ready for implementation?
- What are the critical gaps to address?
- Overall quality rating: Excellent/Good/Needs Work/Incomplete]
```

## Focus Mode

If `--focus AREA` was specified, concentrate the review on that area:

- `requirements` — Deep-dive into Section 2 features, completeness, and traceability
- `architecture` — Focus on Sections 5-6, component design, and data flow
- `technology` — Analyze Sections 1.4 and 3, technology choices and compatibility
- `scope` — Scrutinize Section 1.3, scope boundaries and feasibility
- `integration` — Focus on external dependencies, APIs, and integration points
- `infrastructure` — Deep-dive into Section 8, deployment and DevOps concerns

Still produce all 8 sections, but weight the analysis toward the focus area.

## Formatting Rules

- Use markdown headers and bullet points
- Reference specific section numbers (e.g., "Section 2.1.2", "Feature F-003") when citing issues
- Keep findings actionable — each bullet should identify a specific problem and suggest a fix
- Use severity indicators: **[CRITICAL]**, **[WARNING]**, **[INFO]** for findings
- Be direct and specific, not generic
- Quote relevant text from the spec when citing issues
