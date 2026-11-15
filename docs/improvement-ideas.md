# Improvement ideas

Proposals for the book and film bot instances, not implemented features or a
committed roadmap. These build on the existing voting, saved drafts, AI-assisted
entry creation, reminders, attendance tracking, and backup tooling.

The order below is a suggested starting point. Effort is relative: **small** means
a focused change to an existing flow; **medium** means several screens or new
stored state; **large** means a new workflow with scheduling or migration work.
Each first version should work in English, Russian, and German, respect enabled
entry fields, and use the appropriate book or film labels.

## Start here

| Priority | Idea | Why it helps | First version | Effort |
|---|---|---|---|---|
| 1 | Search and filters | Members can find a particular entry or something that fits their time and language preferences. | Search by title and author/director in active entries and the archive. Add filters for page count/runtime and original language; combine them with the existing unvoted filter. | Medium |
| 2 | Explain each member's voting status | Attendance-based counting is easier to trust when people understand whether their vote currently counts. | Show a private “Your vote counts” or “Your vote is saved but currently excluded” explanation, with the member's attendance surplus and the existing rule. Preserve the all-votes fallback when no past meetings exist. | Small |
| 3 | Choose and announce the next entry | A live ranking does not by itself record the club's final decision. | Let an admin select an entry, preview an announcement, and confirm “Next book/film.” Keep it separate from “discussed,” so choosing something does not prematurely archive it. | Medium |
| 4 | Separate notification preferences | Someone may want new suggestions without voting nudges, or reminders without every new entry. | Give new-entry announcements and voting reminders separate settings. Preserve each user's current opt-out during migration and add a reminder cooldown. | Medium |

## Help the club make decisions

### Voting rounds with a deadline

Create a shortlist for a particular meeting, announce a closing time, and save the
result when the round ends. This would make “what won for this month?” independent
of later changes to the general ranking.

First version: one open round per instance, the existing scoring formula, and an
admin-confirmed winner. Define tie handling before opening the round; snapshot
the vote-counting mode and eligible voters so attendance changes do not silently
change a closed result. Keep ordinary catalog votes separate from round votes.
**Effort: large.**

### Show participation alongside the score

Add the number of counted voters and a breakdown of positive, neutral, and negative
votes to entry cards. This helps members distinguish broad support from a high
score based on very few responses.

First version: aggregate counts using the same attendance eligibility as the
ranking, without exposing individual choices or changing the scoring formula.
Avoid turnout percentages until the denominator is clearly defined; users known
to the bot are not necessarily the whole club. **Effort: small.**

### Give older suggestions another chance

Offer an “Explore overlooked entries” view for visible, undiscussed suggestions
with few votes. This can surface worthwhile nominations that rarely appear in
the Top 5.

First version: an on-demand list ordered by fewest votes, then oldest addition,
with the existing voting cards. Introduce automated resurfacing only if members
find the manual view useful. **Effort: small.**

## Support the meeting itself

### Upcoming meeting and RSVPs

Attach a date, time, location or meeting link to the selected book or film, with
“Going,” “Maybe,” and “Can't attend” buttons. Give members one place to check the
plan and organizers an approximate headcount.

First version: one upcoming meeting and editable RSVPs. Keep planned attendance
separate from actual attendance; an RSVP must not increase voting surplus. Add
opt-in meeting reminders later, with cancellation and rescheduling support.
**Effort: medium; scheduled reminders add work.**

### Post-discussion ratings and notes

Let members record what they thought after reading or watching, so the archive
becomes useful for future recommendations.

First version: an optional personal rating and short spoiler-labelled note on
discussed entries, editable by its author. Keep these ratings separate from
“want to read/watch” votes and avoid posting spoiler text in announcements.
**Effort: medium.**

## Improve entry quality

### Add from a link or identifier

Extend the current title-first wizard so a member can start with a catalog link
or a book ISBN. This can reduce ambiguity between works with the same title and
between editions with different page counts.

First version: support one catalog per entity, extract a title and stable
identifier, then reuse the existing preview and manual correction flow. Extend
the current similar-title warning with exact identifier matches; do not merge
entries automatically. **Effort: medium.**

### Make AI suggestions easier to verify

Build on the existing confirmed source page and AI suggestion markers by showing
where a suggested value came from, especially page count, runtime, and year.

First version: preserve the source URL with the suggestions and leave unavailable
values blank for manual entry. Distinguish page-backed values from estimates
without presenting model confidence as proof. **Effort: medium.**

## Make all instances easier to operate

### Named club timezones

Offer a named timezone such as `Europe/Zurich` alongside the existing fixed UTC
offset. This would make local meeting times and attendance date boundaries follow
daylight-saving changes.

First version: an optional timezone setting with the existing offset as the
backward-compatible fallback. Use it consistently for display and attendance,
and test date boundaries and daylight-saving transitions. **Effort: medium.**

### Track notification delivery per recipient

The bot already recovers pending new-entry jobs on restart and retries rate-limit
errors. Extend this with durable recipient-level progress so a partially completed
broadcast can resume more precisely.

First version: store pending/sent/failed delivery status, retry transient failures
with bounded delays, and show an admin summary. Recheck notification preferences
before sending. Document that a crash between Telegram accepting a message and
the database recording success can still cause a duplicate. **Effort: medium.**

### Verify restores and show instance health

Build on the existing SQLite backup script, error alerts, and cross-instance logs
with evidence that backups are usable and each bot is responsive.

First version: a restore-check script that opens a backup in a temporary location,
runs an integrity check, and verifies expected tables without contacting Telegram.
Document which conversation state is outside the database backup. Then add a
compact per-instance report for last successful polling, pending notifications,
and backup age, alerting only when a configured threshold is crossed.
**Effort: medium.**

## Suggested first milestone

Start with search, the personal voting-status explanation, and participation
counts. These improve existing daily interactions without introducing a new
scheduling workflow. Then add selection of the next entry and RSVPs if meeting
coordination is where organizers spend the most time.

Evaluate changes with a few simple questions: can members find and vote on an
entry faster, do fewer people ask why a vote did not count, and does choosing the
next book or film require fewer manual messages? Use aggregate measurements or
direct club feedback; detailed tracking of individual activity is unnecessary.
