# Running it

Written for someone who is not going to enjoy debugging a Python environment.
The Docker route needs one command and installs nothing on your machine.

---

## 1. Where the code lives

The code is delivered as a **git patch** (`discern-latest.patch`) because this
session cannot push to GitHub — the Claude GitHub App is not installed on the
account, so the token it has is read-only. Once that is fixed the patch step
disappears and you just `git pull`.

Put the repository somewhere permanent — not Downloads, not a temp folder.
Something like `~/code/discern_platform` (macOS/Linux) or
`C:\Users\<you>\code\discern_platform` (Windows).

```bash
git clone https://github.com/Pxpranay/discern_platform.git
cd discern_platform
git am /path/to/discern-latest.patch
```

`git am` replays the commits with their messages and authorship intact, so the
history reads as if it had been pushed normally. If it reports that a patch is
already applied, you already have that work — `git am --skip` moves past it.

**Then push it, so the patch dance is over:**

```bash
git push -u origin main
```

You have push rights on your own account; only this session doesn't. After that
push, GitHub is the source of truth and the patch file can be deleted.

---

## 2. Run it — Docker route

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/), open
it once so the engine is running, then from the repository folder:

```bash
make up
```

On Windows without `make`, the same thing:

```bash
copy .env.example .env
docker compose up --build
```

Wait for `Ready.  http://localhost:8000/` in the log, then open
**http://localhost:8000/** and log in as **`demo` / `discern2026`**.

What that one command did:

| | |
|---|---|
| started PostgreSQL 16 and Redis | as containers, nothing installed on your machine |
| created the schema | ~50 tables, plus the append-only database triggers |
| seeded twelve roles | from the process design's role table, CEO included |
| created the `demo` login | administrator, so you can see every screen |
| loaded a worked project | using Discern's real LINAC Building Rev 0 and Rev 1 BOQ files |

Everyday commands after that:

```bash
make up      # start (safe to repeat — will not overwrite your data)
make down    # stop, keep the data
make reset   # stop and delete the database, so the next `make up` starts clean
```

The demo project loads **only when the database has no project in it**. Once you
start entering your own records, restarts leave them alone.

---

## 3. Run it — without Docker

Only worth it if you intend to change code. You need Python 3.12, PostgreSQL 16
and Redis installed and running.

```bash
cp .env.example .env          # then set POSTGRES_HOST=127.0.0.1
pip install -r requirements.txt
createdb discern
python manage.py bootstrap    # migrate + seed + demo project
python manage.py runserver 0.0.0.0:8000
```

`bootstrap` is the same command Docker runs, and is equally safe to repeat.

---

## 4. What to look at first

Log in as `demo` — an administrator, so nothing is hidden. A route through it
that shows the design rather than just the screens:

1. **`/portfolio/`** — every project, sorted **worst margin first**. The number
   you would want on a Monday morning.
2. **`/projects/`** → pick the project → **`/projects/<id>/dashboard/`** — BOQ
   status, site progress, purchase movement, schedule and margin by lot, on one
   page. Every figure is a link down to the entries behind it.
3. **`/boq/`** → open the released revision → **the reconciliation view**. This
   is the piece with no Odoo equivalent: it compares the new revision against
   *what has already been committed, purchased and received*, not against the
   previous revision's text. Six outcomes, each with what to do about it.
4. **`/procurement/`** — a request, an RFQ to three vendors, the comparison
   statement, and an award that is deliberately **not** forced to the lowest
   price. The comparison is frozen onto the award so the decision stays
   auditable.
5. **`/receipts/`** — record a receipt, then verify it. Nothing becomes cost
   until a Site Engineer verifies it; that is the control, and the screens make
   the two steps separate on purpose.
6. **`/admin-panel/`** — users, roles, the capability matrix, project
   assignments. Change who can do what here; it takes effect immediately, no
   deploy.
7. **`/m/`** on your phone (or a narrow browser window) — the site screens:
   receive, verify, log progress, submit an expense.

To see the whole flow as text instead of clicking, `make demo` prints the
end-to-end walkthrough with every number computed by the real services.

---

## 5. Things worth knowing before you judge it

- **The CEO signs everything.** Every final approval — kickoff, BOQ release,
  purchase order, procurement request, schedule extension, expenses, subcontract
  certification — routes to the CEO role. Managers prepare, the CEO signs. If
  BOQ release turns out to be too frequent for that, move it to another role in
  `/admin-panel/roles/` — no code change, no deploy.
- **The data is demo data.** Real names, real BOQ files, invented commercials.
  Nothing here is a record of an actual Discern transaction.
- **It is not deployed anywhere.** This runs on your machine only. Putting it on
  a server is separate work: real authentication policy, backups with rehearsed
  restores, and monitoring on outbox lag and how often the BOQ ceiling blocks.
- **Approvals and thresholds are settings, not code.** `APPROVAL_THRESHOLD`,
  `RFQ_MINIMUM_VENDORS`, `RFQ_MINIMUM_VENDORS_BELOW_VALUE` in `.env`.

---

## 6. When it goes wrong

| What you see | What it means |
|---|---|
| `port 5432 already allocated` | You already have PostgreSQL running locally. Stop it, or change the `db` port mapping in `docker-compose.yml` to `"5433:5432"`. |
| `port 8000 already in use` | Something else has the port. Change the `web` mapping to `"8001:8000"` and use http://localhost:8001. |
| Page loads but has no data | The demo skipped because a project already existed. `make reset && make up` to start clean. |
| `Cannot connect to the Docker daemon` | Docker Desktop is not running. Open it and wait for the whale icon to settle. |
| `git am` fails with a conflict | Your `main` has diverged from what the patch was built on. `git am --abort`, then send me what `git log --oneline -5` says. |
| Login rejected | The seed did not finish. Check the `web` container log for a traceback; `docker compose logs web`. |

Anything else: `docker compose logs web` is the first place to look, and the
traceback in it usually names the exact file and line.
