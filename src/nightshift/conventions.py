"""Fixed conventions (spec NFR-2). Built in, not configurable."""

from datetime import timedelta

TODO = "Todo"
IN_PROGRESS = "In Progress"
IN_REVIEW = "In Review"
DONE = "Done"
REQUIRED_STATUSES = (TODO, IN_PROGRESS, IN_REVIEW)

LABEL = "nightshift"
NEEDS_HUMAN = "nightshift:needs-human"
LABELS = {
    LABEL: ("5319e7", "Nightshift works on this ticket"),
    NEEDS_HUMAN: ("d93f0b", "Nightshift stopped here and needs you"),
}

POLL_INTERVAL = timedelta(seconds=60)
IDLE_NEEDS_YOU = timedelta(minutes=5)
NO_STATE_NEEDS_YOU = timedelta(minutes=2)
GIVE_UP_STOPPING = timedelta(minutes=10)
LIMIT_GRACE = timedelta(minutes=1)
LIMIT_RETRY = timedelta(minutes=30)
REPORT_ONLY = timedelta(days=7)
BRANCH_MAX = 40
TODO_POLLS = 3
GONE_POLLS = 2
FINISHED_POLLS = 2

HERDR_MINOR = (0, 1)
HERDR_TESTED = "0.1.18"
