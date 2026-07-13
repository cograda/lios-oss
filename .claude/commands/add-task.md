Add a task to the appropriate backlog.

Arguments: $ARGUMENTS

1. Parse the task from the arguments. If no arguments, ask what the task is.

2. All tasks go in the single unified backlog: `vault/Task Backlog.md`. (The `Alex/`·`Sam/` files are retired pointer stubs — never write there. Genuine ideas/nice-to-haves go in `vault/Someday.md`; things owed to us by others go in `vault/Delegated Tasks.md`.)

3. Determine priority from context:
   - 🔺 Urgent — needs doing today/tomorrow
   - ⏫ High — this week
   - 🔼 Medium — planned (default)
   - 🔽 Low — nice to have

4. Determine domain + category. Pick exactly one domain (`#home`/`#renovation`/`#kids`/`#finance`/`#admin`) and place the task under that `#` domain heading in `Task Backlog.md`, under the best-matching `## [[Project]]` sub-heading. If none fits, add directly under the domain.

5. Parse due date if mentioned. Convert relative dates to absolute (YYYY-MM-DD).

6. Format the task:
   ```markdown
   - [ ] Task description ⏫ 📅 2026-04-15 #tag
   ```
   Only include the date emoji if a due date was specified.

7. Add the task under the appropriate section in the backlog. Don't add to the top or bottom of the file — find the right section.

8. If the task relates to a person or entity, use `[[wiki links]]` in the description:
   ```markdown
   - [ ] Book [[Finn]] swimming lessons for summer term 🔼 #school
   ```

9. Show what was added and where.

Examples:
- `/add-task book Finn's swimming for summer` → shared backlog, Kids section, `- [ ] Book [[Finn]] swimming lessons for summer term 🔼 #school`
- `/add-task write up solar panel research` → Alex backlog, Side Projects, `- [ ] Write up solar panel research 🔼`
- `/add-task NCT by end of April` → shared backlog, Vehicle & Home, `- [ ] Car NCT 🔼 📅 2026-04-30 #vehicle`
- `/add-task urgently call the plumber about the leak` → shared backlog, Household, `- [ ] Call plumber about the leak 🔺 #home`
