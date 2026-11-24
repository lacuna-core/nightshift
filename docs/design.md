# Nightshift: design

How Nightshift is built, at a high level. Read this first; it takes about ten minutes.

- *What* it must do is in the [product spec](specs/2026-11-21-initial.md). Codes such as FR-13
  refer to it.
- *Why* things were chosen is in the [decision log](decisions.md).

## 1. The big picture

Nightshift is one loop on your Mac. It has no server and no web interface. It connects tools that
already exist, and each of them stays the authority on its own part.

```mermaid
flowchart LR
    you(["You"])
    subgraph github["GitHub"]
        board["Project boards"]
        issue["Issues and PRs"]
    end
    subgraph mac["Your Mac"]
        ns["Nightshift loop"]
        db[("Ledger<br/>SQLite")]
        herdr["Herdr<br/>terminals"]
        agent["Claude Code<br/>agents"]
        git["Git clones<br/>and worktrees"]
    end

    you -->|"label, rank, review, merge"| github
    you -->|"watch, answer, take over"| herdr
    ns <-->|"read tickets, set status,<br/>label, comment"| github
    ns <-->|"start, watch, interrupt"| herdr
    ns -->|"make and remove worktrees"| git
    ns <--> db
    herdr --- agent
    agent -->|"commit"| git
    agent -->|"push, open PR"| issue
```

| Part | What it decides |
|------|-----------------|
| GitHub | What is to be done, in which order, and what you changed |
| Git | What work exists locally |
| Herdr and Claude Code | Whether an agent is alive, and what it is doing |
| The database | Only what Nightshift itself started and must remember |

**The database is the ledger, the others are the truth.** Nightshift can be killed at any moment,
because on the next poll it compares its ledger with the world again.

## 2. One poll: observe, decide, act

Every 60 seconds, the same steps run:

```mermaid
flowchart LR
    flush["Deliver<br/>outbox"] --> observe["Observe"]
    observe -->|Snapshot| decide["Decide"]
    decide -->|Actions| act["Act"]
    act --> report["Report<br/>for status"]
    report -->|"wait 60 s"| flush

    classDef pure fill:#2e7d32,color:#fff,stroke:#1b5e20
    class decide pure
```

- **Observe** (`observe.py`) reads GitHub, Herdr, Claude Code's transcripts and the database into
  one `Snapshot` of plain data.
- **Decide** (`decide.py`) is a pure function from a snapshot to a list of actions. It reads and
  writes nothing, so the rules are tested with plain data.
- **Act** (`executor.py`) carries the actions out. It reads a ticket again right before it changes
  its status or starts it over. Every step is safe to repeat.

Existing runs are decided before new tickets are picked.

A problem stays as small as possible:

| What fails | What happens |
|------------|--------------|
| One action | Recorded, shown by `status`, decided again on the next poll |
| One board or repo | Skipped; the others go on |
| Herdr or Claude Code | Nothing new is picked; agents are left alone |
| GitHub | The poll ends, and the next ones come less often: up to 15 minutes apart, or when a rate limit resets |
| A bug in one poll | Logged; the loop goes on |

**Nothing is concluded from what could not be read.** An unreadable ticket is not a closed one, and
an unreachable terminal is not a dead agent.

## 3. The code

Dependencies point down. The main ones:

```mermaid
flowchart TB
    cli["cli/<br/>init, check, start, stop, status"]
    wiring["wiring.py<br/>puts it together"]
    checks["checks.py<br/>is the setup complete?"]
    loop["loop.py<br/>poll, back off, stop"]

    observe["observe.py"]
    decide["decide.py"]
    executor["executor.py"]
    cleanup["cleanup.py<br/>safe to remove?"]

    model["model.py, conventions.py<br/>plain data and fixed rules"]
    store["store.py<br/>SQLite"]

    subgraph boundaries["Boundaries: one module per tool"]
        direction LR
        gh["tracker/github.py"]
        herdr["herdr.py"]
        git["git.py"]
        claude["claude.py"]
        notify["notify.py"]
    end

    cli --> wiring
    wiring --> checks
    wiring --> loop
    loop --> observe
    loop --> decide
    loop --> executor
    executor --> cleanup
    observe --> store
    executor --> store
    decide --> model
    observe --> boundaries
    executor --> boundaries
    cleanup --> boundaries
    checks --> boundaries

    classDef pure fill:#2e7d32,color:#fff,stroke:#1b5e20
    class decide,model pure
```

Green has no I/O. Not in the picture: `naming.py` (branch names), `project_config.py`
(`nightshift.yaml`), `lock.py` (keeps a second loop from starting) and `proc.py` (runs every
command-line tool).

Only `tracker/` knows that a ticket ID is an issue number, so that another tracker can be added
later.

## 4. A ticket's way through

The normal case, from Todo to merged:

```mermaid
sequenceDiagram
    autonumber
    actor You
    participant G as GitHub
    participant N as Nightshift
    participant D as Ledger
    participant H as Herdr
    participant A as Agent

    You->>G: label, assign to yourself and rank the ticket
    N->>G: poll
    N->>D: run "claimed"
    N->>G: read again, set In Progress
    N->>D: run "provisioning", then choose and record all names
    N->>N: git worktree on a new branch
    N->>H: start a terminal in its own space
    H->>A: claude, with the prompt
    N->>D: run "running"
    A->>A: implement, commit
    A->>G: push, open PR
    loop every poll
        N->>G: is there a ready PR?
        N->>H: has the agent finished its turn?
    end
    N->>D: run "completing"
    N->>G: set In Review
    N->>D: run "done"
    N->>G: comment with the PR
    You->>G: review and merge
    N->>H: close the space, if it is safe
    N->>N: remove worktree and local branch, if it is safe
```

A run is complete only when **both** hold: the PR is open and not a draft, and the agent was idle on
two polls in a row or has exited. One sign alone can mislead.

## 5. The states of a run

A **run** is one attempt by one agent at one ticket. A ticket has at most one current run, which the
database enforces.

```mermaid
stateDiagram-v2
    [*] --> claimed
    claimed --> provisioning: status set
    provisioning --> running: agent started
    running --> waiting: needs you
    waiting --> running: you answered
    running --> completing: PR ready, agent finished
    waiting --> completing: PR ready, agent finished
    completing --> done: In Review

    provisioning --> failed: cannot start
    running --> failed: exit, stall, timeout
    running --> cancelled: you changed the ticket
    waiting --> cancelled: you changed the ticket

    failed --> completing: you helped it along
    cancelled --> completing: you helped it along

    running --> resetting: back to Todo
    waiting --> resetting: back to Todo
    done --> resetting: back to Todo
    failed --> resetting: back to Todo
    cancelled --> resetting: back to Todo
    claimed --> discarded: not eligible any more
    resetting --> discarded: agent ended, PR closed
    discarded --> [*]
```

`waiting` is short for `waiting_for_human`. The picture leaves out the rarer arrows; section 3 of
the spec has the full rules.

| State | Uses a slot | What Nightshift does |
|-------|-------------|----------------------|
| `claimed` to `completing` | yes | Moves it forward, or follows what you did |
| `done`, `failed`, `cancelled` | no | Watches for a merge, a reset, or your help |
| `resetting` | no | Finishes the reset, whatever happens to the ticket meanwhile |
| `discarded` | no | Waits until its leftover worktree is safe to remove |

## 6. Following you

You can do anything to a ticket at any time, and Nightshift follows. For a live run, the first
question answered with yes wins:

```mermaid
flowchart TB
    live(["A live run, on every poll"]) --> label{"Label or your<br/>assignment removed?"}
    label -->|yes| yours["Cancel: it is yours now"]
    label -->|no| merged{"PR merged?"}
    merged -->|yes| finish["Let the agent finish its turn,<br/>then done"]
    merged -->|no| closed{"Issue closed?"}
    closed -->|yes| cancel["Cancel: interrupt the agent,<br/>keep everything"]
    closed -->|no| todo{"Back in Todo?"}
    todo -->|yes| again["Start over"]
    todo -->|no| other{"PR closed, or the ticket<br/>moved or removed?"}
    other -->|yes| cancel
    other -->|no| look["Look at the agent:<br/>complete, waiting, failed, lost"]
```

There are two exceptions. Moving a ticket to In Review when its PR is ready is not a cancel: the
agent finishes its turn first, and the run is `done`. And a closed PR counts only once the agent's
turn is over, because an agent may replace its own PR.

Nightshift never moves a ticket in response to a change of yours. All it may change is the
`nightshift:needs-human` label, plus a comment.

## 7. Surviving a crash

Four habits make a kill at any moment harmless:

1. **Record, then create.** Branch, folder, space and session names are in the database before any
   of them exists. A repeated step finds what the first try made.
2. **Ask the world.** Before it starts an agent, Nightshift looks for a terminal that has the run's
   label.
3. **State and signals together.** They are written in one transaction: the outbox, below.
4. **Re-check, then repeat.** Nothing is assumed from before the crash.

### The outbox

A failed run must also be labelled, commented on and notified about. These go into an outbox in the
**same transaction** as the state change, and are delivered afterwards:

```mermaid
sequenceDiagram
    participant E as Executor
    participant D as Database
    participant G as GitHub

    rect rgba(46, 125, 50, 0.15)
        note over E,D: one transaction
        E->>D: state is failed
        E->>D: outbox gets label, comment, notification
    end
    E->>G: label
    E->>D: label leaves the outbox
    note over E,G: Killed here? The next start goes on with the comment.
    E->>G: comment, with a hidden mark
    E->>D: comment leaves the outbox
```

Each comment carries a hidden mark with its run and event. A comment whose mark is already on the
issue is not posted again.

### Why an agent is gone

After a reboot or a crash the question is not only *whether* an agent is gone, but *why*:

```mermaid
flowchart LR
    t{"The run's<br/>terminal?"}
    t -->|"gone"| closed["You closed it:<br/>cancelled"]
    t -->|"Claude Code in it"| alive["Alive:<br/>left alone"]
    t -->|"same shell, Claude Code ended"| exited["It exited:<br/>failed"]
    t -->|"a fresh shell"| lost["Lost:<br/>resumed once"]
    t -->|"Claude Code not started yet"| starting["Starting: needs you<br/>after 2 minutes"]
    t -->|"Herdr does not answer"| unknown["Unknown:<br/>nothing happens"]
```

Gone counts only on two polls in a row. If there is a ready PR, the run is complete whichever way
its agent went.

## 8. Removing things safely

This is the one place where work could be lost, so it is the most careful one. Removing needs a
**reason** and must be **safe**:

```mermaid
flowchart TB
    reason{"A reason?<br/>PR merged, or started over, or<br/>issue closed and tab closed by you"}
    reason -->|no| leave["Leave it"]
    reason -->|yes| inuse{"In use?<br/>agent busy, or Claude Code<br/>outside its tab"}
    inuse -->|yes| keep["Keep it, say why once,<br/>look again on every poll"]
    inuse -->|no| work{"Anything to lose?<br/>changes, untracked files, other branch,<br/>rebase or merge, submodules"}
    work -->|yes| keep
    work -->|no| branch{"After a merge: is everything<br/>on the branch in the PR?"}
    branch -->|no| keep
    branch -->|"yes, or not a merge"| week{"First week?"}
    week -->|yes| say["Only report it"]
    week -->|no| space["Close the space"]
    space --> recheck{"Still safe?"}
    recheck -->|no| keep
    recheck -->|yes| remove["git worktree remove,<br/>never forced"]
```

- What cannot be told counts as unsafe.
- After starting over it is stricter: files that git ignores, and tabs you opened, also keep it.
- The local branch is deleted only after a merge. Branches on GitHub are never deleted.
- A run you took over is never touched.

## 9. What is stored

Everything is in the home, `~/.nightshift/` unless `NIGHTSHIFT_HOME` names another; each home
is a separate instance. Repos hold nothing of Nightshift's.

| Where | What |
|-------|------|
| `nightshift.yaml` | Boards and repos. Yours to edit; read at start |
| `nightshift.db`, table `runs` | One row per run, never deleted |
| `nightshift.db`, table `loop_state` | Outbox, usage limit, failed steps, what `status` shows |
| `nightshift.lock` | Held by the running loop; the system frees it however the loop ends |
| `logs/` | JSON lines, with repo, ticket, run and branch on every line about a run |
| `worktrees/` | One folder per run |

How long something has been so (idle on two polls, quiet for 20 minutes) is kept only in memory. A
restart starts those counts over, which only makes Nightshift slower to act. The time a run has
been running is stored.

## 10. How it is tested

| What | How |
|------|-----|
| `decide.py` | Plain data in, actions out |
| Boundaries | GitHub with a mock transport; Herdr and Claude Code with recorded answers |
| Git | Real repos in temporary folders, never mocked |
| The whole loop | `tests/world.py`: GitHub and Herdr faked, git real, a clock the test moves |
| Crashes | Killed at each side effect, restarted, compared with a run that was not |
| The real thing | By hand, on a private sandbox repo and board |

The last one matters: fakes know only what we know. The first tests on a real board showed three
bugs that no other test had caught.
