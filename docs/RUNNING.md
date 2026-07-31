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

## 2. Run it — no Docker

**Use this route if Docker says "virtualization support not detected."** It
needs two installers and no virtual machine. Redis is *not* required: nothing in
a web request queues a background job, so the only thing a worker does is drain
the outbox, and you can do that yourself with one command when you want to.

### 2a. Install Python 3.12

[python.org/downloads](https://www.python.org/downloads/) → run the installer →
**tick "Add python.exe to PATH"** on the first screen. That tickbox is the whole
difficulty; miss it and every later command says `python is not recognized`.

Check it:

```
python --version
```

### 2b. Install PostgreSQL 16

[postgresql.org/download/windows](https://www.postgresql.org/download/windows/)
→ the EDB installer → accept the defaults, **except**: when it asks for a
password for the `postgres` superuser, set one you will remember. Write it down.
You do not need Stack Builder at the end; skip it.

The installer registers PostgreSQL as a Windows service, so it starts with the
machine and you never think about it again.

### 2c. Point the app at it

In the repository folder, copy `.env.example` to `.env` and edit these four
lines to match what you just installed:

```
POSTGRES_DB=discern
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<the password you set during the PostgreSQL install>
POSTGRES_HOST=127.0.0.1
```

`POSTGRES_HOST` is the one people miss — `.env.example` ships with `db`, which
is the Docker container's name and means nothing outside Docker.

### 2d. Run it

```
pip install -r requirements.txt
python manage.py bootstrap
python manage.py runserver 0.0.0.0:8000
```

`bootstrap` creates the `discern` database if it does not exist, migrates the
schema, seeds the roles and the login, and loads the demo project — the same
work the Docker route does. It is safe to re-run: the demo loads only into a
database with no project in it.

Then open **http://localhost:8000/** and log in as **`demo` / `discern2026`**.

Every later start is just the last line:

```
python manage.py runserver 0.0.0.0:8000
```

To start clean, drop the database and bootstrap again:

```
python manage.py bootstrap
```

after deleting it in pgAdmin (installed alongside PostgreSQL) — or from a
terminal, `dropdb -U postgres discern`.

### On Redis

Skipped deliberately. The one background job is publishing outbox events, and
you can run it on demand:

```
python manage.py drain_outbox
```

Same code the worker runs. If you later want it happening automatically, that is
when Redis earns its place.

---

## 3. Run it — Docker route

Docker Desktop on Windows needs hardware virtualization switched on. If it says
**"virtualization support not detected"**, see §7 — it is usually a BIOS setting
plus a Windows feature, both free to change. Or just use §2 and move on.

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
| `virtualization support not detected` | Docker cannot start. Use §2, or fix it per §7. |
| `python is not recognized` | The Python installer's "Add python.exe to PATH" box was not ticked. Re-run the installer, choose Modify, tick it. |
| `connection refused` on port 5432 | PostgreSQL is not running, or `POSTGRES_HOST` still says `db` (the Docker name) instead of `127.0.0.1`. |
| `password authentication failed for user "postgres"` | `POSTGRES_PASSWORD` in `.env` does not match what you set during the PostgreSQL install. |
| `port 5432 already allocated` (Docker) | You already have PostgreSQL installed locally. Stop that service, or change the `db` port mapping in `docker-compose.yml` to `"5433:5432"`. |
| `port 8000 already in use` | Something else has the port. `python manage.py runserver 0.0.0.0:8001` and use http://localhost:8001. |
| Page loads but has no data | The demo skipped because a project already existed. Drop the database and bootstrap again. |
| `Cannot connect to the Docker daemon` | Docker Desktop is not running. Open it and wait for the whale icon to settle. |
| `git am` fails with a conflict | Your `main` has diverged from what the patch was built on. `git am --abort`, then send me what `git log --oneline -5` says. |
| Login rejected | The seed did not finish. Re-run `python manage.py bootstrap` and read the traceback. |

Anything else: the traceback in the terminal names the exact file and line.
Send it over rather than guessing at it.

---

## 7. "Virtualization support not detected"

Docker Desktop on Windows runs Linux containers inside a lightweight virtual
machine, and that needs hardware virtualization — Intel VT-x or AMD-V. Almost
every machine made in the last decade has it; on many it ships **switched off in
the BIOS**, which is what this message means. Nothing is wrong with your laptop.

You do not need to fix this to run the app — §2 avoids Docker entirely. Fix it
only if you want containers for other reasons.

**Check what is actually off.** Ctrl+Shift+Esc → Performance → CPU. Bottom right
says `Virtualization: Enabled` or `Disabled`.

**If it says Disabled** — it is a BIOS setting:

1. Settings → System → Recovery → Advanced startup → **Restart now**
2. Troubleshoot → Advanced options → **UEFI Firmware Settings** → Restart
3. Find the setting. It is named differently by vendor:
   - Intel: **Intel Virtualization Technology**, or **VT-x**
   - AMD: **SVM Mode**
   - Often under Advanced, Configuration, CPU Setup, or Security
4. Enable it, save and exit (usually F10).

**If it says Enabled but Docker still complains**, the Windows features are
missing. Open PowerShell **as Administrator**:

```powershell
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
wsl --set-default-version 2
wsl --install
```

Restart, then start Docker Desktop again.

**Two things that block it regardless:** Windows 10/11 **Home** editions are
fine (Docker uses WSL 2, not Hyper-V), so that is not the problem — but a
corporate-managed laptop may have virtualization locked by policy, and a
Windows VM cannot nest another one unless nested virtualization is enabled on
the host. In either case, §2 is the answer.
