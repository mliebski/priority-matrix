# Priority Matrix

Turns a to-do list into a decision about what matters. You paste your list, and a local AI coach walks you through it one item at a time with short questions, then suggests a square in the four-quadrant (Eisenhower) matrix:

|                   | Urgent     | Not urgent |
|-------------------|------------|------------|
| **Important**     | **Do**     | **Schedule** |
| **Not important** | **Delegate** | **Drop** |

- **You decide.** The coach asks and suggests; nothing moves until you place it. You can disagree in words or pick a square yourself at any time.
- **Importance is measured against your goals.** You write down what matters to you in Settings, and the coach uses that.
- **It doesn't guess.** Its first reply is always a question, and after three answers without a clear picture it hands the choice back to you.
- **It works without the AI.** If Ollama is down you can still add, drag and place items.
- **Private.** Items are only sent to your own Ollama server, and everything is stored in one SQLite file on the box.

## Start it

You need Docker on the box and Ollama running natively. If `budget-tracker` already works on this box, Docker and Ollama are already set up, so skip step 2.

1. **Download the code.** The repository is private, so the box needs its own read-only key. GitHub allows a deploy key on only one repository, so this one gets a new key and a separate SSH name, which leaves any existing `github.com` setup alone. Log in to the box and run:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/matrix_deploy -N "" -C "matrix box"
   printf 'Host github-matrix\n  HostName github.com\n  IdentityFile ~/.ssh/matrix_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
   cat ~/.ssh/matrix_deploy.pub
   ```
   On GitHub, open the repository → **Settings → Deploy keys → Add deploy key**. Paste the line printed by `cat`, and leave *Allow write access* **unticked**. Then:
   ```bash
   git clone git@github-matrix:mliebski/priority-matrix.git
   cd priority-matrix
   ```
2. **One-time Ollama setup.** Install Ollama and a model, then let containers reach it:
   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ollama pull llama3.2
   sudo systemctl edit ollama
   ```
   Add these two lines in the editor, save, then run `sudo systemctl restart ollama`:
   ```
   [Service]
   Environment="OLLAMA_HOST=0.0.0.0:11434"
   ```
   Ollama has no password, so with ufw enabled allow only the Docker networks:
   ```bash
   sudo ufw allow from 172.16.0.0/12 to any port 11434 proto tcp
   ```
3. **Start the app** from this folder:
   ```bash
   docker compose up -d --build
   ```
4. Open `http://<box-ip>:8010` from any browser on your network.
5. Open **Settings**, describe what matters to you, pick a model and save.

The app has no login, so anyone on your home network can open it. Don't forward its port from your router.

## Configuration

To change a setting, copy `.env.example` to `.env`, edit it and run `docker compose up -d` again.

| Setting | Default | Purpose |
|---------|---------|---------|
| `APP_PORT` | `8010` | Port the app uses on the box |
| `TZ` | `America/Toronto` | Time zone for "today" and "overdue" |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Ollama address. Change it only if Ollama runs on another computer |
| `OLLAMA_MODEL` | none | Model to use if none is picked in Settings |

If no model is picked or set and only one is installed, the app uses that one. The model picked in Settings always wins.

## Using it

1. Type an item or paste your whole list, one per line. Bullets and checkboxes such as `- [ ]` are removed.
2. Press **Sort these with the coach**. Answer its questions, then accept the suggestion or pick another square. The next item opens by itself.
3. Set a due date on an item if it has one. The coach takes it into account.
4. Work from the matrix: **Do** first, and protect time for **Schedule**. Click a card to change or rethink it, drag it to move it, or tick the circle when it's finished.

On a CPU-only box the first answer can take a minute while the model loads. Later ones are quicker, because the model stays in memory for 15 minutes.

## How the coach decides

The model judges two things separately, as yes, no or unsure: is the item important, and is it urgent? It also words the questions. The app then applies fixed rules:

- Important means it serves your stated goals or responsibilities, or that never doing it has a lasting cost.
- Urgent means a real deadline or consequence in about the next few days. Pressure from other people doesn't count.
- The first reply is always a question, however sure the model is.
- A suggestion appears only when both judgments are settled, and the app, not the model, works out the square.
- After three answers with no clear picture, the coach stops asking and leaves the choice to you.

A placed card shows the coach's reason only if you followed its suggestion. If you choose a different square, the reason is cleared. To change what the coach looks for, edit the prompt in `app/coach.py`.

## Troubleshooting

| What you see | Cause | Fix |
|--------------|-------|-----|
| **Failed to fetch** when adding an item | The page can't reach the app: the server stopped, or `index.html` was opened as a file | Check `docker compose ps`, and use the `http://` address rather than a `file://` one |
| **AI off: Ollama is not reachable** | The container can't reach Ollama | Check that Ollama is running and listening on `0.0.0.0`, and check the ufw rule from step 2 |
| **AI off: Pick a model in Settings** | Several models are installed and none is chosen | Choose one in Settings |
| **Model is not installed** | The chosen model was removed | Pick another in Settings, or pull it again with `ollama pull` |
| **Thinking…** runs for a long time | The model is loading, or is too large for the box | Wait up to 5 minutes, or use a smaller model |

The app keeps working when the AI is off, so you can still sort items yourself. To see the app's log, run `docker compose logs -f app`.

## Updating and backups

```bash
git pull
docker compose up -d --build
```

Your data lives in the Docker volume `data`, which rebuilding doesn't touch. `docker compose down -v` deletes it. To copy the database off the container:

```bash
docker compose cp app:/data/matrix.db ./matrix-backup.db
```

## Development

On Windows, in PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt pytest httpx
.venv\Scripts\python -m pytest tests
```

To try the app without Docker or a model, start a stand-in for Ollama in one window:

```powershell
.venv\Scripts\python tests\fake_ollama.py 8798
```

Then start the app in a second window and open `http://localhost:8000`. `DB_PATH` is needed because the default is the container's `/data` folder.

```powershell
$env:DB_PATH = "matrix.db"
$env:OLLAMA_URL = "http://127.0.0.1:8798"
.venv\Scripts\python -m uvicorn app.main:app
```

The stand-in plays a short scripted conversation, so it shows the flow but says nothing about how good a real model's questions are.

```
app/
  main.py     web API
  coach.py    the conversation: prompt, and when a suggestion is justified
  ai.py       client for the local Ollama server
  db.py       SQLite storage
  static/     the page
tests/
```
