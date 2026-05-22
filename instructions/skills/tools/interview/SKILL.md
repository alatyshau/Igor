---
name: interview
description: Focused interview mode that turns a raw Objective into a workable WHAT + WHY formulation. One question per message, maximum uncertainty reduction per question, in-place updates to the Objective's `index.md`. Trigger when the user explicitly asks for an interview on an Objective, or when an agent needs to move a `draft` Objective to `open` and the formulation is too vague to act on. Also applies to refining a fuzzy WHY on an already-open Objective when context shifts, or clarifying the scope of a sub-entity.
---

# Interview Mode

A focused conversational mode whose only purpose is to turn a raw Objective into a formulation you can actually work on. The output is *not* a new document — it is sharper text inside the Objective's existing `index.md`.

## When to invoke

- **User request.** The user explicitly asks for an interview ("опрос по OBJ005", "проведи интервью по WHY OBJ012", etc.).
- **Agent proposal.** When lack of clarity is the active blocker — typically a `draft` Objective with a vague `Цель`, missing `Выходы`, or a `WHY` that doesn't ground decisions — the agent may propose: *"WHY/WHAT здесь не достаточно ясны — провести интервью?"*. The user decides whether to enter the mode.

Interview mode is not active outside these triggers. Applicability is not limited to `draft`: a `WHY` on an `open` Objective can drift as context changes, and a sub-entity (Issue, Suggestion, Task) can need scope clarification. The same rules apply.

## Rules

1. **One question per message.** Never stack two questions. Never queue a numbered list of things to clarify. Send one question, wait for the answer, then choose the next question based on what the answer changed. Stacking questions destroys the uncertainty-reduction loop because the user answers the easy one and the load-bearing one stays open.

2. **Maximize uncertainty reduction.** Each question should aim at the place where the formulation is least sharp — the part where the next answer moves the most. Avoid cosmetic or easy questions while load-bearing ambiguity is still open. If two candidate questions feel equally important, pick the one whose answer would most change what `## WHAT` or `## WHY` looks like.

3. **Update in place.** When an answer sharpens the formulation, edit the Objective's `index.md` directly — only `## WHAT` and `## WHY`. Do not create a separate spec, discovery doc, interview transcript, summary, or side notes anywhere outside the OBJ folder. The OBJ file is the single point of accumulation; every other location is noise.

4. **Only what the user said.** Do not invent context, add reasoning the user did not produce, or pad the formulation with motivational phrasing. If the user said little, `## WHY` stays short. A short truthful formulation beats a long invented one — the latter creates the illusion of clarity and silently misdirects future work.

5. **Open questions become Items, not side files.** Questions the user themselves needs to think through become `[Inn open]` entries in the `## Items` section of the same OBJ. Never a separate file.

6. **Don't push silence.** If the user gives a partial answer or moves on, do not re-ask, rephrase, or chase. Silence is a pause, not a refusal. Resume when the user returns to the topic.

## Stop conditions

The interview ends when any of these is true:

- The user signals stop explicitly ("хватит", "достаточно", "ок, дальше").
- The agent judges the formulation sharp enough to act on. For a `draft` Objective, the agent transitions `draft → open` and announces it as a single change-log line:

  ```
  ! [OBJ012 ParallelChatTracking (open)] draft → open — formulation clear enough to work.
  ```

  The user may push back ("не достаточно, продолжай") — in that case the state reverts to `draft` and the interview continues.
- The user reframes the conversation away from this Objective. Do not drag the interview along; let it end.

## Anti-patterns

- **Multi-question messages.** A single agent turn holding two questions, or a list of clarifications. Pick one.
- **Side spec.** Writing the formulation into `interview_notes.md`, `discovery.md`, `spec.md`, or any file outside the OBJ folder. Forbidden — the OBJ's `index.md` is the only sink.
- **Padding the formulation.** Filling `## WHY` with restatements, motivational phrasing, or implied rationale the user did not produce. The text in the OBJ must come from the user, not from the agent's best guess.
- **Chasing silence.** Re-asking, rephrasing, or nudging when the user has not engaged. Treat silence as a pause.
- **Premature `draft → open`.** Closing the state while `WHY` is still hand-wavy or `Выходы` are speculative. If you would not yourself know what to do first based on the current formulation, it is not ready.

## Background

The interview mode formalizes a pattern originally encountered in the Throne system (git@github.com:gently-whitesnow/throne.git): clarification by tight, single-question loops, with the formulation living *inside* the entity being clarified rather than in a parallel artifact. The operator drives the pace; the agent only proposes the mode when uncertainty is the active blocker.
