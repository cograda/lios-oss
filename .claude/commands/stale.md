# Stale Task Detector

Scan for stale, duplicate, and orphaned tasks across the Comar vault.

## Architecture

Delegates scanning to a Haiku subagent. Opus presents findings and actions changes on request.

## Instructions

### Step 1: Delegate to Haiku

Spawn an Agent with `model: "haiku"`. Ask it to:

- Read `vault/Task Backlog.md` (actionable)
- Read `vault/Delegated Tasks.md` (items owed by others)
- Read `vault/Someday.md` (ideas / parked)
- Read the last 10 daily notes from `vault/Daily Notes/Alex/`
- Read the last 5 meeting notes from `vault/Meetings/`

Check for:

**Stale backlog items:**
- Active (non-parked, non-blocked) backlog items that haven't appeared in any daily note's Focus or Active sections in the last 2 weeks
- Delegated items where the person/entity hasn't been mentioned in any meeting or daily note in the last 4 weeks

**Duplicates:**
- Tasks in daily note Active sections that are word-for-word or near-duplicates of backlog items
- Backlog items that say essentially the same thing as each other
- Tasks duplicated across the shared and personal backlogs

**Orphaned items:**
- Tasks in daily/meeting notes that aren't tracked in any backlog and look significant enough to capture

**Hierarchy integrity (Task Backlog files only):**
- Tasks missing a domain tag (must have exactly one of `#home`, `#renovation`, `#kids`, `#finance`, `#admin`)
- Tasks with multiple domain tags
- H2 project headings that aren't `[[wiki links]]`
- Tasks placed under the wrong domain (e.g. a `#kids`-tagged task sitting under `# Home`)

Return each category as a list with the task text and where it was found.

### Step 2: Present findings

Show each category with counts. For each item, show the task and its location. Ask what to do:
- **Stale items:** Archive, move to Someday, re-prioritise, or keep?
- **Duplicates:** Which to keep, which to remove?
- **Orphaned items:** Add to backlog or mark as done?

### Step 3: Action (if requested)

If the user says "clean it up" or similar:
- Remove duplicates (keep the more detailed version)
- Move stale items to Someday or archive entirely
- Add orphaned items to the appropriate backlog
- Move completed items to the matching `* - Done.md` file

This skill works well as part of `/backlog-sweep` or before `/weekly-review`.
