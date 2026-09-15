Search the vault for notes, tasks, or information.

Arguments: $ARGUMENTS

If no arguments, ask what to search for.

## Search strategy

### Step 1 — Keyword search (fast, free)

Use grep to search markdown files for the query terms:

```bash
grep -ril "search terms" --include="*.md" vault/ | grep -v "node_modules\|\.obsidian\|\.git\|\.claude\|\.tools\|\.embeddings\|Archive"
```

If this returns good results (relevant file names and content), present them and stop.

### Step 2 — Semantic search via MCP (if keyword search is insufficient)

Call the **`vault_search`** MCP tool with the query and a limit of 10.

This searches the server's pgvector index (384-dim embeddings of all vault files) using cosine similarity.

### Step 3 — Present results

For each relevant result:
- Show the file path (relative to vault root)
- Show a 2-3 line preview of the most relevant content
- If it's a task file, show the task status

Group results by type:
- **Notes** — from Daily Notes, Personal, Household, Reference
- **Meetings** — from Meetings/
- **Tasks** — items in backlogs matching the query

### Step 4 — Offer to read

Ask if the user wants to open any of the results.

## Examples

- `/find Finn ear` → grep first, finds task files and kids notes
- `/find renovation flooring decisions` → grep may miss context, fall back to semantic
- `/find that conversation about the kitchen layout` → semantic search, meeting notes
- `/find Sam blog ideas` → grep matches the file name directly
