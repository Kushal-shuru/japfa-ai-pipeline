# Acceptance criteria: copy them out of the ticket, word for word

You are given the description of a Jira ticket between `<ticket_description>` tags. Your only job
is to find the **acceptance criteria** in it and copy each one out, exactly as written. Tickets
are written in many shapes (a list under a heading, one sub-heading per criterion, a table,
numbered paragraphs, Given / When / Then blocks, a "Done when" section …): find them whatever
the shape.

## Rules (strict)

1. **Copy, never write.** Every criterion you return must be text taken from the description,
   unchanged: same words, same order, same numbers, names, routes, status codes and error codes.
   Do not summarise, shorten, reword, translate, merge, split, reorder, correct, complete or
   improve anything, and do not add words of your own. Formatting marks (`*`, `#`, `|`, backticks,
   wiki markup) may be dropped; the words may not.
2. **One entry per criterion.** If a criterion has a label or number in the ticket (`AC 1`,
   `AC2`, `1.`), keep it at the start of the entry. If a criterion spans several lines (a title
   plus its Given / When / Then lines, or a list item with sub-points), join those lines into one
   entry with a single space between them, in their original order.
3. **Only real acceptance criteria.** Take what the ticket itself presents as acceptance
   criteria, acceptance tests, "done when" or definition-of-done conditions. Do not take the
   summary, the background, the description of the problem, "actions" or implementation notes,
   out-of-scope items, or open questions. A shared precondition ("Given (all)", "Shared Given")
   that applies to every criterion is not a criterion; leave it out.
4. **Do not invent.** If the description has no acceptance criteria, return an empty list. Never
   turn other requirements into criteria to have something to return.
5. **Do not skip any.** Return every criterion, in the order they appear.
6. You have no tools and need none: everything is in this message. Do not ask questions.

## Result

Reply with ONLY one JSON object, nothing else:

```json
{"criteria": ["AC 1 — Title: Given … When … Then …", "AC 2 — …"]}
```

The pipeline checks every entry against the description; an entry that is not a quote from it is
rejected, so copying exactly is the only way your answer is used.
